"""Append-only, ordered, idempotent project event log (DEV-M1-G04).

Layered on ``FilesystemStateBackend`` (DEV-M1-G02), which persists one
JSON file per object under ``base_dir/<tree_dir>/<object_id>.json``
(``SCHEMA_TO_STATE_DIR``). Per that backend's docstring,
append-only/ordering/idempotency semantics are a workflow-layer concern
and are implemented here, on top of the backend's per-object CRUD; the
backend itself is not modified.

Layout
------
``ProjectEventLog`` manages three kinds of files under ``base_dir``:

* ``events/<event_id>.json`` -- the event records themselves, persisted
  through the state backend (schema-validated, canonical JSON, atomic
  writes). Each record carries two log-managed additional properties
  (both permitted by ``schemas/event.schema.yaml``'s
  ``additionalProperties: true``): ``sequence`` (the record's
  deterministic log position) and, when the event was appended with an
  idempotency key, ``idempotency_key``.
* ``_event_log/sequence.json`` -- ``{"next_sequence": N}``: the counter
  that drives sequence assignment. It is an optimization and is written
  *before* the record it numbers, so a crash can only leave an unused
  gap in the sequence, never a repeat; when the counter is missing or
  corrupt it is rebuilt deterministically from the records themselves.
* ``_event_log/idempotency/<sha256>.json`` -- idempotency claims:
  ``{"idempotency_key": K, "event_id": E}``. Claims are created with an
  atomic create-if-absent primitive (``O_CREAT|O_EXCL``), the same
  atomic-create-if-not-exists rule the lease model specifies
  (14-STATE-GIT-ARTIFACTS.md SS4), so a key can never map to two events
  and existing claim content is never clobbered. File names are SHA-256
  digests of the key, so any key string is a safe, collision-free file
  stem; the human-readable key is stored inside the claim for audit.

Append-only (AC-01)
-------------------
The log API exposes no update/replace/delete operations. ``append`` of
an event whose ``event_id`` is already recorded raises
``DuplicateEventIdError`` (unless an idempotency claim already resolves
the submission), and records that have been persisted are never
rewritten or removed by any operation of the log. (The underlying
backend retains its own ``delete`` for operational repair; the log does
not expose it.)

Idempotency (AC-02)
-------------------
``append(event, idempotency_key=K)`` first resolves the claim for ``K``:
if a claim exists and its event record exists, the recorded event is
returned unchanged (``replayed=True``) and the sequence counter is not
advanced -- a duplicate submission never creates a duplicate semantic
event. A claim whose event record does not exist is a stale claim left
by a crash between claim creation and record write; it is reclaimed
through a deterministic rule (mirroring the lease recovery rule of
14-STATE-GIT-ARTIFACTS.md SS4) and the append proceeds.

Ordering (AC-03)
----------------
Every record stores its ``sequence`` at append time. ``list_events``
returns records ordered by ``(sequence, event_id)`` -- primary key
sequence (strictly increasing, never reused), tie-broken by
``event_id`` so the order stays deterministic even for hand-edited
state. The order is fully recoverable from the persisted records alone:
a fresh ``ProjectEventLog`` over the same ``base_dir`` reads the same
records and returns the same order, and each record's ``sequence`` is
verifiable directly from its JSON file on disk. Idempotent
re-submission does not advance the sequence.

Concurrency
-----------
Append operations are serialized by an in-process lock, so concurrent
appends from threads -- including concurrent submissions of the same
idempotency key -- are race-free: at most one record exists per key,
the sequence is never double-assigned, and the final order is
deterministic. Idempotency claim creation additionally uses the atomic
create-if-absent primitive, which keeps the claim mapping safe even if
two *processes* race; the full append (claim + counter + record) is not
cross-process atomic in v0.1 -- cross-process ownership is the concern
of the lease layer (DEV-M1-G03).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import ProjectEvent
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.core.state_backend import FilesystemStateBackend

__all__ = [
    "ProjectEventLog",
    "EventRecord",
    "DuplicateEventIdError",
    "CorruptEventLogError",
]

#: Internal (non-object) files the log manages under ``base_dir/_event_log/``.
_INTERNAL_DIR_NAME = "_event_log"
_SEQUENCE_FILE_NAME = "sequence.json"
_IDEMPOTENCY_DIR_NAME = "idempotency"


class DuplicateEventIdError(ValueError):
    """Raised by ``append`` when the event's ``event_id`` is already recorded.

    Event records are append-only (AC-01): re-append of an existing
    event id is rejected. The supported way to resubmit an event is the
    idempotency key of the original append, which returns the existing
    record instead.
    """


class CorruptEventLogError(ValueError):
    """Raised when log-managed state is not a well-formed log record.

    Covers records without a valid integer ``sequence``, idempotency
    claims whose stored key/event id are malformed, and other state that
    cannot have been written by this log. Like the state backend, the
    log fails loudly on corruption instead of guessing.
    """


@dataclass(frozen=True)
class EventRecord:
    """A recorded event together with its deterministic log position.

    Attributes:
        event: the event as recorded (frozen ``ProjectEvent``).
        sequence: the record's log sequence -- a positive integer,
            strictly increasing across appends, never reused.
        replayed: True when this record was returned by an idempotent
            re-submission: the record already existed, nothing new was
            appended, and the sequence was not advanced.
    """

    event: ProjectEvent
    sequence: int
    replayed: bool = False


class ProjectEventLog:
    """Append-only, ordered, idempotent event log over a state base dir.

    A workflow layer on top of ``FilesystemStateBackend`` (obj_type
    ``"event"``) that adds the append-only/ordering/idempotency
    semantics the backend deliberately does not enforce. Append
    operations are serialized by an in-process lock; a single log
    instance is the supported way to append from multiple threads.

    Args:
        base_dir: root of the state tree (same layout contract as
            ``FilesystemStateBackend``); ``str`` or ``Path``.
        file_mode: optional explicit mode for written event files,
            passed through to the state backend.
    """

    def __init__(
        self, base_dir: str | Path, *, file_mode: int | None = None
    ) -> None:
        self._base_dir = Path(base_dir)
        self._backend = FilesystemStateBackend(self._base_dir, file_mode=file_mode)
        self._internal_dir = self._base_dir / _INTERNAL_DIR_NAME
        self._sequence_path = self._internal_dir / _SEQUENCE_FILE_NAME
        self._idempotency_dir = self._internal_dir / _IDEMPOTENCY_DIR_NAME
        #: Serializes claim resolution, sequence assignment, and record
        #: persistence as one unit, so concurrent appends from threads
        #: are race-free. Not shared across log instances or processes.
        self._lock = threading.Lock()

    # -- public API ----------------------------------------------------------

    def append(
        self,
        event: ProjectEvent,
        *,
        idempotency_key: str | None = None,
    ) -> EventRecord:
        """Append ``event`` and return its record (idempotent on the key).

        Args:
            event: the frozen ``ProjectEvent`` to record. The event must
                satisfy ``schemas/event.schema.yaml``; schema-invalid
                events raise ``SchemaValidationError`` before anything is
                persisted.
            idempotency_key: optional key identifying the *semantic*
                event. A re-submission with the same key returns the
                record of the first submission (``replayed=True``)
                instead of appending a duplicate (AC-02) and does not
                advance the sequence.

        Raises:
            TypeError: ``event`` is not a ``ProjectEvent``, or the
                idempotency key is not a string.
            ValueError: empty idempotency key.
            SchemaValidationError: the event fails the event schema
                (nothing is persisted).
            DuplicateEventIdError: an event with this ``event_id`` is
                already recorded and no idempotency claim resolves the
                submission (AC-01).
            CorruptEventLogError: log-managed state is malformed.
        """
        if not isinstance(event, ProjectEvent):
            raise TypeError(
                f"append expects a ProjectEvent, got {type(event).__name__}"
            )
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise TypeError(
                    "idempotency_key must be a str, got"
                    f" {type(idempotency_key).__name__}"
                )
            if not idempotency_key:
                raise ValueError("idempotency_key must be a non-empty string")
        # Persistence gate first: schema-invalid events are rejected
        # before any claim, counter, or record file is touched.
        validate_and_reject("event", event.to_dict())
        with self._lock:
            if idempotency_key is not None:
                existing = self._resolve_claim_locked(idempotency_key)
                if existing is not None:
                    return EventRecord(
                        event=existing.event,
                        sequence=existing.sequence,
                        replayed=True,
                    )
            if self._backend.exists("event", event.event_id):
                raise DuplicateEventIdError(
                    f"event id {event.event_id!r} is already recorded; event"
                    " records are append-only (resubmit with the original"
                    " idempotency_key to retrieve the existing record)"
                )
            if idempotency_key is not None and not self._create_claim(
                idempotency_key, event.event_id
            ):
                # Lost a claim race with a concurrent writer -- only
                # possible across processes, threads are serialized by
                # the lock. The winner's record is authoritative.
                claim = self._read_claim(idempotency_key)
                if self._backend.exists("event", claim["event_id"]):
                    existing = self._read_record(claim["event_id"])
                    return EventRecord(
                        event=existing.event,
                        sequence=existing.sequence,
                        replayed=True,
                    )
                raise CorruptEventLogError(
                    "concurrent idempotency claim cannot be resolved yet;"
                    " retry once the winning append completes"
                )
            sequence = self._next_sequence_locked()
            record_data = event.to_dict()
            record_data["sequence"] = sequence
            if idempotency_key is not None:
                record_data["idempotency_key"] = idempotency_key
            self._backend.write("event", event.event_id, record_data)
            return EventRecord(event=event, sequence=sequence)

    def get(self, event_id: str) -> EventRecord | None:
        """Return the record for ``event_id``, or None if not recorded."""
        if not self._backend.exists("event", event_id):
            return None
        return self._read_record(event_id)

    def list_events(self) -> list[EventRecord]:
        """Return all records in deterministic order (AC-03).

        Ordering rule: ascending ``sequence``, ties broken by
        ``event_id`` (lexicographic). The order is recoverable from the
        persisted records alone -- a fresh ``ProjectEventLog`` over the
        same base dir returns the identical order.

        Raises:
            ValueError: a file in the event store is not a valid event
                record (propagated from the state backend).
            CorruptEventLogError: a record lacks a valid ``sequence``.
        """
        records = [
            self._read_record(event_id)
            for event_id in self._backend.list_ids("event")
        ]
        records.sort(key=lambda record: (record.sequence, record.event.event_id))
        return records

    # -- sequence assignment (AC-03) -----------------------------------------

    def _next_sequence_locked(self) -> int:
        """Assign the next sequence and persist the advanced counter.

        Counter-first ordering: the counter is written *before* the
        record that receives the number, so after any crash the counter
        is still greater than every persisted sequence -- a crash can
        leave an unused gap but never a reused sequence. A missing or
        corrupt counter is rebuilt deterministically from the records.
        """
        if self._sequence_path.is_file():
            try:
                counter = json.loads(
                    self._sequence_path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, UnicodeDecodeError):
                counter = None
            if (
                isinstance(counter, dict)
                and isinstance(counter.get("next_sequence"), int)
                and not isinstance(counter.get("next_sequence"), bool)
                and counter["next_sequence"] >= 1
            ):
                next_sequence = counter["next_sequence"]
                atomic_write(
                    self._sequence_path,
                    json.dumps(
                        {"next_sequence": next_sequence + 1},
                        indent=2,
                        sort_keys=True,
                    ),
                )
                return next_sequence
        next_sequence = self._max_recorded_sequence() + 1
        atomic_write(
            self._sequence_path,
            json.dumps(
                {"next_sequence": next_sequence + 1},
                indent=2,
                sort_keys=True,
            ),
        )
        return next_sequence

    def _max_recorded_sequence(self) -> int:
        """Highest sequence among recorded events (0 for an empty log)."""
        return max(
            (
                self._read_record(event_id).sequence
                for event_id in self._backend.list_ids("event")
            ),
            default=0,
        )

    # -- idempotency claims (AC-02) ------------------------------------------

    def _resolve_claim_locked(self, key: str) -> EventRecord | None:
        """Return the record claimed by ``key``, or None when free.

        A claim whose event record is missing is a stale claim left by a
        crash between claim creation and record write; it is reclaimed
        (deleted) deterministically so the append can proceed -- the
        same recovery rule the lease model applies to expired leases
        (14-STATE-GIT-ARTIFACTS.md SS4).
        """
        claim_path = self._claim_path(key)
        if not claim_path.is_file():
            return None
        claim = self._read_claim(key)
        event_id = claim["event_id"]
        if not self._backend.exists("event", event_id):
            claim_path.unlink(missing_ok=True)
            return None
        return self._read_record(event_id)

    def _create_claim(self, key: str, event_id: str) -> bool:
        """Atomically create the claim for ``key``; False if it exists.

        Content is written through the atomic create-if-absent
        primitive (``O_CREAT|O_EXCL``): a concurrent winner is
        unambiguous and existing claim content is never clobbered.
        """
        content = json.dumps(
            {"idempotency_key": key, "event_id": event_id},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return _atomic_create_if_absent(self._claim_path(key), content)

    def _claim_path(self, key: str) -> Path:
        # SHA-256 digest of the key as the file stem: any key string is
        # a safe, collision-free file name; the human-readable key
        # itself is stored inside the claim for audit.
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._idempotency_dir / f"{digest}.json"

    def _read_claim(self, key: str) -> dict[str, Any]:
        claim_path = self._claim_path(key)
        try:
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CorruptEventLogError(
                f"idempotency claim for key {key!r} at {claim_path} is"
                f" corrupt: {exc}"
            ) from exc
        if (
            not isinstance(claim, dict)
            or claim.get("idempotency_key") != key
            or not isinstance(claim.get("event_id"), str)
            or not claim["event_id"]
        ):
            raise CorruptEventLogError(
                f"idempotency claim for key {key!r} at {claim_path} is"
                " malformed; expected a mapping with 'idempotency_key'"
                " and a non-empty string 'event_id'"
            )
        return claim

    # -- record access -------------------------------------------------------

    def _read_record(self, event_id: str) -> EventRecord:
        data = self._backend.read("event", event_id)
        sequence = data.get("sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
        ):
            raise CorruptEventLogError(
                f"record {event_id!r} has no valid integer 'sequence'; it"
                " was not written by ProjectEventLog"
            )
        try:
            event = ProjectEvent.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise CorruptEventLogError(
                f"record {event_id!r} is not a valid event: {exc}"
            ) from exc
        return EventRecord(event=event, sequence=sequence)


def _atomic_create_if_absent(path: Path, content: str) -> bool:
    """Create ``path`` containing ``content`` only if absent (atomic).

    Returns True when this call created the file and False when the file
    already existed. ``O_CREAT|O_EXCL`` is the atomic create-if-absent
    primitive on both POSIX and Windows: at most one concurrent caller
    can succeed, and existing content is never overwritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # Never leave a half-written claim behind; the create itself was
        # atomic, only the content could have failed to land.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    return True
