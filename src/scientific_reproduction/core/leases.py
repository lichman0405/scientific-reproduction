"""Bounded per-object leases with expiry and deterministic recovery
(DEV-M1-G03, acceptance AC-01/AC-02/AC-03).

Lease model (14-STATE-GIT-ARTIFACTS.md SS4)
-------------------------------------------
When one worker owns an executable Goal/Run stage it acquires a lease
that records: the object reference (``obj_type`` + ``object_id``), the
owner session/worker id, the acquisition timestamp, the expiry
timestamp, and a per-grant nonce used to verify ownership on renew and
release.  A lease is **bounded**: it is valid only until
``expires_at``.  Expired leases are reclaimed through the deterministic
recovery rule below -- never through wall-clock guessing.

Filesystem protocol
-------------------
A lease is a single JSON runtime record at
``base_dir/leases/<obj_type>/<object_id>.json`` and *is its own lock*:
no separate lock file is needed for leases, because the expiry is the
staleness mechanism (a crashed holder's lease simply expires, unlike a
lock file which would block forever).

Every state-changing operation uses the atomic create-if-not-exists
protocol from ``core.locks.atomic_create`` (complete content staged
with ``atomic_write``, then ``os.link`` -- see its docstring for why
``os.replace`` alone cannot create-if-absent).  Concretely:

* *acquire*: if the record exists and is valid (``expires_at > now``),
  raise ``LeaseHeldError``.  Otherwise (absent, expired, or corrupt)
  take over: verify the stale record is still in place, unlink it, and
  create the new record with ``atomic_create``.  If the create fails
  with ``FileExistsError`` a concurrent claimer won the race; the
  winner's record is re-read and ``LeaseHeldError`` is raised.  Two
  concurrent claimers therefore cannot both acquire the same valid
  lease (AC-01): exactly one ``os.link`` succeeds.
* *renew*: verify the record is still this exact grant and still valid,
  verify it again immediately before unlinking (compare-and-unlink),
  then recreate it with the new expiry under the same nonce and
  acquisition timestamp.  Renewing an expired or lost lease fails.
* *release*: verify the record is this exact grant, compare-and-unlink,
  and remove it.  Releasing a missing record is a no-op (idempotent);
  releasing a record now held by a different owner refuses to delete it.
* *recovery* (AC-02): a lease whose ``expires_at <= now`` is expired
  and may be claimed by any principal; the claim records the new owner
  deterministically.  The clock is injectable (``now`` callable) so
  expiry behavior is testable without sleeping.

Determinism and the injectable clock
------------------------------------
All validity decisions use the store's ``now`` callable (default
``time.time``).  Tests inject a fixed/advanceable clock, so expired-
lease recovery, renew-failure, and boundary behavior are deterministic
on every platform.

Lease records are runtime records, not schema objects
-----------------------------------------------------
There is no ``schemas/lease.schema.yaml`` (creating one is out of scope
for DEV-M1-G03), so lease files are **not** validated by the schema
gate of ``core.state_backend``.  They are validated here against the
documented lease contract (required fields and types in ``Lease``);
the object ``obj_type`` must still be a known normative type
(``models.SCHEMA_NAMES``), mirroring ``FilesystemStateBackend``.

Known limitation (renew vs. takeover window)
--------------------------------------------
``os.link`` guarantees that at most one party's record exists at any
instant, and a takeover only ever removes a record it re-verified as
expired.  The one residual interleaving is a holder renewing *at the
exact expiry instant* while a claimer's compare-and-unlink lands
between the holder's verification and its unlink: the claimer removes
the (already replaced) record and takes over, and the holder's renewal
fails loudly on its follow-up create (``FileExistsError``) -- the
object never ends up with two records.  With an injected clock the
behavior is fully deterministic.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Self

from scientific_reproduction.core.locks import atomic_create
from scientific_reproduction.core.models import SCHEMA_NAMES
from scientific_reproduction.core.state_backend import UnknownObjectTypeError

__all__ = [
    "LeaseError",
    "LeaseHeldError",
    "LeaseExpiredError",
    "LeaseCorruptError",
    "Lease",
    "LeaseStore",
]


class LeaseError(Exception):
    """Base class for lease failures."""


class LeaseHeldError(LeaseError):
    """The object is leased by another (or a concurrent claim won)."""


class LeaseExpiredError(LeaseError):
    """The lease is expired or no longer held (renew/release on it fails)."""


class LeaseCorruptError(LeaseError):
    """A lease record exists but does not satisfy the lease contract.

    Such a record is not a valid lease: ``acquire`` may claim it like an
    expired one (deterministic recovery), while ``get``/``renew``/
    ``release`` refuse to guess its content.
    """


@dataclass(frozen=True)
class Lease:
    """A bounded per-object lease grant (runtime record, not a schema object).

    Field names are the exact JSON keys of the lease record
    (``Lease.to_dict`` / ``Lease.from_dict`` round-trip them).
    """

    #: Normative object type the lease protects (a ``models.SCHEMA_NAMES``
    #: value, e.g. ``"run"`` or ``"goal"``).
    object_type: str
    #: Object id of the protected object.
    object_id: str
    #: Owner session/worker id.
    owner: str
    #: Per-grant claim token; distinguishes successive grants to the same
    #: owner (a stale release handle can never release a newer grant).
    nonce: str
    #: Epoch seconds when the lease was first acquired (fractional).
    acquired_at: float
    #: Epoch seconds when the lease expires; a lease with
    #: ``expires_at <= now`` is expired and recoverable.
    expires_at: float
    #: Time-to-live in seconds of the grant (renewal policy basis).
    ttl: float

    def is_expired(self, now: float) -> bool:
        """Return True if the lease is expired at ``now``."""
        return self.expires_at <= now

    def remaining(self, now: float) -> float:
        """Return the seconds remaining at ``now`` (negative when expired)."""
        return self.expires_at - now

    def to_dict(self) -> dict[str, Any]:
        """Return the plain JSON-able dict form of the record."""
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "owner": self.owner,
            "nonce": self.nonce,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Build a Lease from a plain dict (the lease contract).

        Raises:
            TypeError: ``data`` is not a mapping or a required field is
                missing or has the wrong type.
            ValueError: a numeric field is not a finite positive number
                where required.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"Lease.from_dict expects a mapping, got {type(data).__name__}"
            )
        for field_name in (
            "object_type",
            "object_id",
            "owner",
            "nonce",
        ):
            if field_name not in data:
                raise TypeError(
                    f"lease record missing required field {field_name!r}"
                )
            if not isinstance(data[field_name], str) or not data[field_name]:
                raise TypeError(
                    f"lease field {field_name!r} must be a non-empty string"
                )
        acquired_at = _require_number("acquired_at", data)
        expires_at = _require_number("expires_at", data)
        ttl = _require_number("ttl", data)
        if ttl <= 0:
            raise ValueError(f"lease field 'ttl' must be positive, got {ttl}")
        return cls(
            object_type=data["object_type"],
            object_id=data["object_id"],
            owner=data["owner"],
            nonce=data["nonce"],
            acquired_at=acquired_at,
            expires_at=expires_at,
            ttl=ttl,
        )


def _require_number(name: str, data: Mapping[str, Any]) -> float:
    """Return ``data[name]`` coerced to a finite float, or raise."""
    if name not in data:
        raise TypeError(f"lease record missing required field {name!r}")
    value = data[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(
            f"lease field {name!r} must be a finite number, got"
            f" {type(value).__name__}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"lease field {name!r} must be finite, got {value}")
    return number


class LeaseStore:
    """Per-object bounded leases on the filesystem (14-STATE-GIT-ARTIFACTS.md SS4).

    Layout: ``base_dir/leases/<obj_type>/<object_id>.json`` -- one
    JSON runtime record per leased object, next to (but separate from)
    the schema-validated object files of ``FilesystemStateBackend``
    (``base_dir/<obj_type>/<object_id>.json``).  All writes go through
    ``atomic_write`` staging + ``os.link`` (``core.locks.atomic_create``).

    Args:
        base_dir: root of the state tree (share the state backend's
            base dir; ``leases/`` is not a normative object type so it
            never collides with object files).  May be ``str`` or
            ``Path``.
        now: injectable clock returning epoch seconds; defaults to
            ``time.time``.  All expiry decisions use this callable so
            behavior is deterministic under tests (no wall-clock
            dependence, no sleeps).
        file_mode: optional explicit mode for lease records, passed
            through to ``atomic_create`` (``None`` keeps the 0o600
            default).
    """

    def __init__(
        self,
        base_dir: str | Path,
        *,
        now: Callable[[], float] | None = None,
        file_mode: int | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self._now_fn = now if now is not None else time.time
        self._file_mode = file_mode

    #: Object IDs become file stems; anything that could escape the
    #: ``<obj_type>`` directory is rejected defensively (mirrors
    #: ``FilesystemStateBackend``).
    _FORBIDDEN_ID_CHARS = ("/", "\\", "\x00")

    # -- validation --------------------------------------------------------

    def _check_obj_type(self, obj_type: str) -> None:
        if obj_type not in SCHEMA_NAMES:
            known = ", ".join(sorted(SCHEMA_NAMES))
            raise UnknownObjectTypeError(
                f"unknown object type {obj_type!r}; expected one of: {known}"
            )

    def _check_object_id(self, object_id: str) -> None:
        if not isinstance(object_id, str) or not object_id:
            raise ValueError("object_id must be a non-empty string")
        if object_id in (".", "..") or any(
            c in self._FORBIDDEN_ID_CHARS for c in object_id
        ):
            raise ValueError(
                f"invalid object_id {object_id!r}: must be a plain file stem"
                " (no path separators, no '.', no '..')"
            )

    def _check_owner(self, owner: str) -> None:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a non-empty string")

    def _check_ttl(self, ttl: float) -> float:
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
            raise TypeError(
                f"ttl must be a finite positive number of seconds, got"
                f" {type(ttl).__name__}"
            )
        seconds = float(ttl)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError(f"ttl must be a finite positive number, got {ttl}")
        return seconds

    def _check_lease(self, lease: Lease) -> None:
        if not isinstance(lease, Lease):
            raise TypeError(
                f"expected a Lease, got {type(lease).__name__}"
            )

    # -- paths -------------------------------------------------------------

    def _lease_path(self, obj_type: str, object_id: str) -> Path:
        self._check_obj_type(obj_type)
        self._check_object_id(object_id)
        return self.base_dir / "leases" / obj_type / f"{object_id}.json"

    # -- persistence -------------------------------------------------------

    def _canonical(self, content: dict[str, Any]) -> str:
        """Deterministic JSON: same record always byte-identical."""
        return json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False)

    def _read_lease(self, path: Path) -> Lease | None:
        """Return the persisted lease, None when absent.

        Raises:
            LeaseCorruptError: the record exists but does not satisfy the
                lease contract (not valid JSON, not a JSON object, or
                missing/mistyped required fields).
        """
        if path.is_symlink():
            raise LeaseCorruptError(
                f"refusing to read symlinked lease record: {path}"
            )
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise LeaseCorruptError(
                f"lease record at {path} is not valid JSON: {exc}"
            ) from exc
        try:
            return Lease.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise LeaseCorruptError(
                f"lease record at {path} does not satisfy the lease contract:"
                f" {exc}"
            ) from exc

    def _compare_and_unlink(self, path: Path, expected: Lease | None) -> bool:
        """Unlink ``path`` only if its content is still ``expected``.

        Re-reads the record immediately before unlinking: if the record
        changed (renewed, taken over, or removed by a concurrent
        principal) nothing is deleted and False is returned.  ``None``
        as ``expected`` matches an absent *or corrupt* record (both are
        stale states safe to remove).
        """
        try:
            current = self._read_lease(path)
        except LeaseCorruptError:
            current = None
        if current != expected:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # already gone: the removal succeeded as far as we care
        except PermissionError:
            # Windows: a concurrent reader can transiently hold an open
            # handle (sharing violation).  Nothing was removed; report
            # the contention so the caller re-decides from a fresh read
            # instead of guessing.
            return False
        return True

    # -- public API --------------------------------------------------------

    def acquire(
        self, obj_type: str, object_id: str, owner: str, ttl: float
    ) -> Lease:
        """Acquire a bounded lease for ``obj_type``/``object_id``.

        If no valid lease exists the record is created atomically
        (create-if-not-exists): exactly one of two concurrent claimers
        succeeds (AC-01).  An absent, expired, or corrupt record is
        claimed deterministically (AC-02); the record then identifies
        the new owner and expiry (AC-03).

        Args:
            obj_type: a known normative object type (``models.SCHEMA_NAMES``).
            object_id: plain file stem identifying the object.
            owner: session/worker id of the claimer (non-empty string).
            ttl: lease duration in seconds (finite, positive).

        Returns:
            The granted ``Lease`` (the on-disk record).

        Raises:
            UnknownObjectTypeError: unknown ``obj_type``.
            ValueError: invalid ``object_id``, ``owner``, or ``ttl``.
            TypeError: wrong argument types.
            LeaseHeldError: a valid lease is held by another principal
                (or a concurrent claimer won the race).
        """
        path = self._lease_path(obj_type, object_id)
        self._check_owner(owner)
        ttl = self._check_ttl(ttl)
        now = self._now_fn()

        # Bounded re-decision loop: every pass starts from a fresh read,
        # so transient contention -- a concurrent claimer's create, or a
        # Windows sharing violation on the takeover unlink -- leads to a
        # new, deterministic decision instead of a stale one.
        for _ in range(3):
            corrupt = False
            try:
                current = self._read_lease(path)
            except LeaseCorruptError:
                current = None
                corrupt = True
            if current is not None and current.expires_at > now:
                raise LeaseHeldError(
                    f"object {obj_type!r}/{object_id!r} is leased by"
                    f" {current.owner!r} until {current.expires_at}"
                )
            if (current is not None or corrupt) and not self._compare_and_unlink(
                path, current
            ):
                continue  # the stale/corrupt record changed or could not
                # be removed: re-decide from a fresh read

            content = {
                "object_type": obj_type,
                "object_id": object_id,
                "owner": owner,
                "nonce": uuid.uuid4().hex,
                "acquired_at": now,
                "expires_at": now + ttl,
                "ttl": ttl,
            }
            try:
                atomic_create(
                    path, self._canonical(content), file_mode=self._file_mode
                )
            except FileExistsError:
                continue  # a concurrent claimer won the create: re-decide
            return Lease.from_dict(content)

        # Retries exhausted under sustained contention: report the current
        # state of the record instead of guessing.
        try:
            final = self._read_lease(path)
        except LeaseCorruptError:
            final = None
        if final is not None and final.expires_at > now:
            raise LeaseHeldError(
                f"object {obj_type!r}/{object_id!r} is leased by"
                f" {final.owner!r} until {final.expires_at}"
            )
        raise LeaseHeldError(
            f"lease for {obj_type!r}/{object_id!r} is contested by concurrent"
            " claimers; retry the acquisition"
        )

    def renew(self, lease: Lease, ttl: float | None = None) -> Lease:
        """Renew a held lease, extending its expiry by ``ttl``.

        ``ttl`` defaults to the lease's original TTL.  The renewal keeps
        the grant identity (``owner``, ``nonce``, ``acquired_at``) and
        writes a new expiry -- the record is verified to still be this
        exact grant immediately before it is replaced, and the create is
        atomic, so a concurrent takeover either wins (this renewal fails
        with ``LeaseHeldError``) or loses (the takeover fails).

        Raises:
            LeaseExpiredError: the lease is expired or its record no
                longer exists (released/lost).
            LeaseHeldError: the record is now held by a different grant
                or a concurrent claimer won the renewal race.
            LeaseCorruptError: the record exists but is unreadable.
            UnknownObjectTypeError / ValueError / TypeError: invalid
                object reference, ``ttl``, or non-``Lease`` argument.
        """
        self._check_lease(lease)
        path = self._lease_path(lease.object_type, lease.object_id)
        if ttl is None:
            ttl = lease.ttl
        ttl = self._check_ttl(ttl)
        now = self._now_fn()

        current = self._read_lease(path)
        if current is None:
            raise LeaseExpiredError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} is no"
                " longer held (record missing)"
            )
        if current != lease:
            if current.owner == lease.owner:
                raise LeaseHeldError(
                    f"lease for {lease.object_type!r}/{lease.object_id!r} was"
                    " superseded by a newer grant for the same owner;"
                    " re-acquire instead of renewing a stale handle"
                )
            raise LeaseHeldError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} is now"
                f" held by {current.owner!r}"
            )
        if current.expires_at <= now:
            raise LeaseExpiredError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} expired"
                f" at {current.expires_at}"
            )
        if not self._compare_and_unlink(path, current):
            # The record changed between verification and removal.
            try:
                other = self._read_lease(path)
            except LeaseCorruptError:
                other = None
            if other is None:
                raise LeaseExpiredError(
                    f"lease for {lease.object_type!r}/{lease.object_id!r}"
                    " vanished during renewal; re-acquire"
                )
            raise LeaseHeldError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} is"
                f" contested during renewal; now held by {other.owner!r}"
            )

        refreshed = Lease(
            object_type=lease.object_type,
            object_id=lease.object_id,
            owner=lease.owner,
            nonce=lease.nonce,
            acquired_at=lease.acquired_at,
            expires_at=now + ttl,
            ttl=ttl,
        )
        try:
            atomic_create(
                path, self._canonical(refreshed.to_dict()), file_mode=self._file_mode
            )
        except FileExistsError:
            raise LeaseHeldError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} was"
                " taken over by a concurrent claimer during renewal"
            ) from None
        return refreshed

    def release(self, lease: Lease) -> None:
        """Release a held lease (idempotent).

        Releasing a lease whose record no longer exists is a no-op.
        Releasing a record now held by a different grant raises
        ``LeaseHeldError`` and deletes nothing.  An own expired record
        is still removed (cleanup); release never fails on an own lease.

        Raises:
            LeaseHeldError: the record is held by a different owner or
                changed concurrently.
            LeaseCorruptError: the record exists but is unreadable.
            UnknownObjectTypeError / ValueError / TypeError: invalid
                object reference or non-``Lease`` argument.
        """
        self._check_lease(lease)
        path = self._lease_path(lease.object_type, lease.object_id)
        current = self._read_lease(path)
        if current is None:
            return  # already released: idempotent
        if current != lease:
            if current.owner == lease.owner:
                raise LeaseHeldError(
                    f"lease for {lease.object_type!r}/{lease.object_id!r} was"
                    " superseded by a newer grant for the same owner; it is"
                    " still held"
                )
            raise LeaseHeldError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} is held"
                f" by {current.owner!r}, not by {lease.owner!r}; refusing to"
                " release it"
            )
        if not self._compare_and_unlink(path, current):
            raise LeaseHeldError(
                f"lease for {lease.object_type!r}/{lease.object_id!r} changed"
                " concurrently during release; nothing was deleted"
            )

    def get(self, obj_type: str, object_id: str) -> Lease | None:
        """Return the current lease record, or None when unleased.

        Raises:
            UnknownObjectTypeError: unknown ``obj_type``.
            ValueError: invalid ``object_id``.
            LeaseCorruptError: the record exists but is unreadable.
        """
        path = self._lease_path(obj_type, object_id)
        return self._read_lease(path)
