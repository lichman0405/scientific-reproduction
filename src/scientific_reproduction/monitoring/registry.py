"""The Monitor's watched-Run registry (DEV-M8-G01, deliverable).

The Execution Monitor (M8) watches external Runs -- Runs dispatched to
external backends (a compute adapter like ``slurm_ssh``, a lab adapter
dispatch, ...). The watched-Run registry is the Monitor-owned durable
record of exactly which external Runs it is watching and under which
external identity, so that a Monitor restart can reconstruct its watch
set from persisted state alone (AC-01 of DEV-M8-G01) -- the same
"fresh instance over the same state directory recovers from the durable
record alone" discipline as the compute adapters.

Layout and recovery
-------------------
One persisted entry per watched run at
``<state_dir>/watched/<run_id>.json``, written through
:func:`core.atomic.atomic_write` (durable-state discipline of the whole
codebase). Every operation re-hydrates from disk (the M1 recovery
discipline: never trust session state). A fresh
:class:`WatchedRunRegistry` over the same state directory returns the
full watch set from ``list_watched()`` / ``get(run_id)`` -- the
reconstruction assertion of AC-01.

Entry vocabulary (AC-03 synergy)
--------------------------------
Each watch entry (:class:`WatchedRunRecord`) references the Run
(``run_id``) and, through the ``external`` :class:`RunExternal` field
(the core external-id vocabulary), the backend, ``dispatch_id`` and/or
``job_id`` and the working directory -- the ids a later reconciliation
needs -- plus the producing adapter identity/version when applicable and
the watch metadata (``watched_at``, ``last_heartbeat_at``). An entry
must name the backend and at least one external id: a watch entry
without an external identity could never be reconciled and is refused
by the constructor and by ``from_dict`` alike.

Determinism, secrets, discipline
--------------------------------
All timestamps come from the injected clock (``now``; no wall clock in
the records), entries are persisted as sorted canonical JSON
(byte-identical for identical inputs), ids are generated with
``core.ids.generate_id`` and the registry never persists credentials:
the records hold external *ids* only, never secrets -- the tests walk
every persisted byte. Errors follow the house paradigm: ``TypeError``
at type boundaries, the stable ``MonitoringError`` (a ``ValueError``
subclass) hierarchy otherwise. The monitoring subsystem does not import
from the adapters package: the external ids are plain documented
fields of the core ``RunExternal`` vocabulary.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Mapping, TypeAlias

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import RunExternal

__all__ = [
    "MONITOR_ID_KIND",
    "MonitoringClock",
    "MonitoringError",
    "WATCH_RECORD_VERSION",
    "WATCHED_STATE_DIR",
    "WatchedRunRecord",
    "WatchedRunRegistry",
    "WatchNotFoundError",
    "WatchRecordError",
    "DuplicateWatchError",
    "derive_monitor_id",
    "utc_now",
    "validate_external_identity",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the durable watch-entry schema (the ``record_version`` key
#: of :class:`WatchedRunRecord`); entries of a different version are
#: refused.
WATCH_RECORD_VERSION: str = "1.0"

#: Directory of the watch entries, relative to the injected state
#: directory: entries live at ``<state_dir>/watched/<run_id>.json``.
WATCHED_STATE_DIR: str = "watched"

#: ID kind of a Monitor instance (``sr_monitor_<32 hex chars>``).
MONITOR_ID_KIND: str = "monitor"


# ---------------------------------------------------------------------------
# Errors and the injectable clock (monitoring-package shared vocabulary)
# ---------------------------------------------------------------------------


class MonitoringError(ValueError):
    """Base error of the monitoring subsystem.

    A ``ValueError`` subclass (house paradigm), so corrupt persisted
    state and contract violations surface as stable, catchable errors
    with stable messages.
    """


class WatchRecordError(MonitoringError):
    """Raised for corrupt watch entries and watch-entry contract
    violations (missing fields, unknown version, invalid ids, missing
    external identity, mistyped or empty fields)."""


class WatchNotFoundError(MonitoringError):
    """Raised when ``get``/``heartbeat`` refer to a run with no watch
    entry."""


class DuplicateWatchError(MonitoringError):
    """Raised when a run is watched again with a different external
    identity than the persisted entry (re-watching the identical entry
    is an idempotent no-op; changing the identity of a watched run
    requires ``unwatch`` first)."""


#: The injectable clock of the monitoring subsystem: a callable
#: producing a timestamp string (mirrors the compute adapters' clock).
MonitoringClock: TypeAlias = Callable[[], str]


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 timestamp string
    (``YYYY-MM-DDTHH:MM:SS+00:00``)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def derive_monitor_id(state_dir: Path) -> str:
    """The deterministic Monitor identity of a state directory.

    The Monitor's identity is a pure function of its durable state
    location (``generate_id("monitor", <resolved state dir>)``), so the
    registry and the checkpoint store over the same directory always
    agree on who the Monitor is, with no randomness and no wall clock.
    """
    return generate_id(MONITOR_ID_KIND, str(state_dir.resolve()))


def validate_external_identity(
    external: RunExternal,
    *,
    error: type[MonitoringError] = WatchRecordError,
) -> None:
    """Enforce the external-identity invariant shared by watch entries
    and checkpoints (AC-03): an entry must name the backend and at least
    one external id (``dispatch_id`` and/or ``job_id``) -- the minimal
    vocabulary a later reconciliation needs. The working directory is
    optional (not every backend has one).

    Raises:
        TypeError: ``external`` is not a ``RunExternal``.
        MonitoringError (``error`` subclass): the backend is missing or
            no external id is named.
    """
    if not isinstance(external, RunExternal):
        raise TypeError(
            "external must be a RunExternal, got"
            f" {type(external).__name__}"
        )
    if external.backend is None:
        raise error(
            "the external backend is required (no backend, no"
            " reconciliation): entries must name the backend and at least"
            " one external id"
        )
    if external.job_id is None and external.dispatch_id is None:
        raise error(
            "at least one external id is required: an entry must name the"
            " backend and dispatch_id and/or job_id so a later"
            " reconciliation can address the external run"
        )


def _canonical_json(data: dict[str, Any]) -> str:
    """Deterministic canonical JSON: identical input dicts always
    produce byte-identical text (sorted keys, stable indent)."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# The durable watch entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchedRunRecord:
    """The durable watch entry of one external Run (AC-01).

    Persisted at ``<state_dir>/watched/<run_id>.json`` and re-hydrated
    from disk on every operation: a fresh registry instance over the
    same state directory reconstructs the watch set from the persisted
    entries alone (AC-01). The entry references the Run (``run_id``)
    and, through the ``external`` :class:`RunExternal` field, the
    external backend, ``dispatch_id``/``job_id`` and working directory
    -- the external ids a later reconciliation needs (AC-03) -- plus
    the producing adapter identity/version when applicable and the
    watch metadata (``watched_at`` when the watch started,
    ``last_heartbeat_at``). The entry carries **no credential fields**
    and no secrets: external ids only.

    Field names are the exact JSON keys of the persisted entry
    (``to_dict`` / ``from_dict`` round-trip them). There is no
    ``schemas/*.schema.yaml`` for runtime monitoring state, so
    ``from_dict`` validates against this documented contract with
    stable errors, mirroring the compute adapters' durable records.
    """

    record_version: ClassVar[str] = WATCH_RECORD_VERSION

    run_id: str
    external: RunExternal
    watched_at: str
    adapter_id: str | None = None
    adapter_version: str | None = None
    last_heartbeat_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                "WatchedRunRecord.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, "run"):
            raise WatchRecordError(
                f"watch entry run_id {self.run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )
        if not isinstance(self.external, RunExternal):
            raise TypeError(
                "WatchedRunRecord.external must be a RunExternal, got"
                f" {type(self.external).__name__}"
            )
        validate_external_identity(self.external)
        if not isinstance(self.watched_at, str) or not self.watched_at.strip():
            raise WatchRecordError(
                "watch entry watched_at must be a non-empty timestamp"
                f" string, got {self.watched_at!r}"
            )
        for name in ("adapter_id", "adapter_version", "last_heartbeat_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise WatchRecordError(
                    f"watch entry {name} must be a non-empty string when"
                    f" set, got {value!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the entry (``None`` optionals
        omitted)."""
        data: dict[str, Any] = {
            "record_version": self.record_version,
            "run_id": self.run_id,
            "external": self.external.to_dict(),
            "watched_at": self.watched_at,
        }
        for key in ("adapter_id", "adapter_version", "last_heartbeat_at"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WatchedRunRecord:
        """Build a watch entry from a plain dict (the watch-entry
        contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            WatchRecordError: a required field is missing or a value
                violates the contract (unknown version, invalid run id,
                missing/incomplete external identity, mistyped or empty
                fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "WatchedRunRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise WatchRecordError(
                    f"watch entry missing required field {name!r}"
                )
            return data[name]

        record_version = required("record_version")
        if record_version != cls.record_version:
            raise WatchRecordError(
                f"watch entry version {record_version!r} is not supported;"
                f" expected {cls.record_version!r}"
            )
        run_id = required("run_id")
        external_raw = required("external")
        if not isinstance(external_raw, Mapping):
            raise WatchRecordError(
                "watch entry field 'external' must be a mapping, got"
                f" {type(external_raw).__name__}"
            )
        try:
            external = RunExternal.from_dict(external_raw)
        except (TypeError, ValueError) as exc:
            raise WatchRecordError(
                f"corrupt watch entry 'external' field: {exc}"
            ) from exc
        watched_at = required("watched_at")
        adapter_id = data.get("adapter_id")
        adapter_version = data.get("adapter_version")
        last_heartbeat_at = data.get("last_heartbeat_at")
        try:
            return cls(
                run_id=run_id,
                external=external,
                watched_at=watched_at,
                adapter_id=adapter_id,
                adapter_version=adapter_version,
                last_heartbeat_at=last_heartbeat_at,
            )
        except (TypeError, ValueError) as exc:
            raise WatchRecordError(
                f"corrupt watch entry: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class WatchedRunRegistry:
    """The Monitor-owned watched-Run registry (AC-01).

    Durable watch entries live at ``<state_dir>/watched/<run_id>.json``,
    one file per watched run, written through ``atomic_write``. Every
    operation re-hydrates from disk; a **fresh registry instance** over
    the same state directory reconstructs the full watch set from the
    persisted entries alone (``list_watched`` / ``get``) -- the AC-01
    reconstruction assertion. Watch re-establishment is idempotent for
    the identical entry (the M8 recovery discipline: reconciliation and
    recovery are idempotent); re-watching a run under a *different*
    external identity is refused until ``unwatch``.

    Args:
        state_dir: the injected state directory (entries at
            ``<state_dir>/watched/``).
        now: injectable clock producing a timestamp string (default
            ``utc_now``); ``heartbeat`` stamps from it -- no wall clock
            in the tested path.
        monitor_id: the Monitor identity (``sr_monitor_<32 hex>``).
            Defaults to the deterministic identity of the state
            directory (``derive_monitor_id``), so the registry and the
            checkpoint store over the same directory agree on the
            Monitor.

    Raises:
        TypeError: ``state_dir`` is not a str/Path, or ``now`` is not
            callable.
        WatchRecordError: an injected ``monitor_id`` is not a valid
            monitor id.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        now: MonitoringClock | None = None,
        monitor_id: str | None = None,
    ) -> None:
        if not isinstance(state_dir, (str, Path)):
            raise TypeError(
                "state_dir must be a str or Path, got"
                f" {type(state_dir).__name__}"
            )
        if now is not None and not callable(now):
            raise TypeError(
                f"now must be callable, got {type(now).__name__}"
            )
        self._state_dir = Path(state_dir)
        self._now_fn = now if now is not None else utc_now
        if monitor_id is not None:
            if not isinstance(monitor_id, str) or not is_valid_id(
                monitor_id, MONITOR_ID_KIND
            ):
                raise WatchRecordError(
                    f"monitor_id {monitor_id!r} is not a valid monitor id"
                    " (sr_monitor_<32 hex chars>)"
                )
            self._monitor_id = monitor_id
        else:
            self._monitor_id = derive_monitor_id(self._state_dir)
        self._watched_dir = self._state_dir / WATCHED_STATE_DIR

    # -- identity and persistence ------------------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected state directory."""
        return self._state_dir

    @property
    def monitor_id(self) -> str:
        """The Monitor identity owning this registry."""
        return self._monitor_id

    @property
    def watched_dir(self) -> Path:
        """The watch-entry directory (``<state_dir>/watched/``)."""
        return self._watched_dir

    def _check_run_id(self, run_id: str) -> None:
        if not isinstance(run_id, str):
            raise TypeError(
                f"run_id must be a str, got {type(run_id).__name__}"
            )
        if not is_valid_id(run_id, "run"):
            raise WatchRecordError(
                f"run id {run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )

    def _entry_path(self, run_id: str) -> Path:
        self._check_run_id(run_id)
        return self._watched_dir / f"{run_id}.json"

    def _write_entry(self, record: WatchedRunRecord) -> None:
        atomic_write(
            self._entry_path(record.run_id), _canonical_json(record.to_dict())
        )

    def _read_entry(self, run_id: str) -> WatchedRunRecord:
        """Re-hydrate one watch entry from disk (the M1 recovery
        discipline: never trust session state)."""
        path = self._entry_path(run_id)
        if not path.is_file():
            if path.exists():
                raise WatchRecordError(
                    f"watch entry at {path} is not a regular file"
                )
            raise WatchNotFoundError(
                f"no watch entry for run {run_id!r} at {path}; call"
                " watch() first"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WatchRecordError(
                f"corrupt watch entry at {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise WatchRecordError(
                f"corrupt watch entry at {path}: expected a JSON object"
            )
        try:
            return WatchedRunRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise WatchRecordError(
                f"corrupt watch entry at {path}: {exc}"
            ) from exc

    # -- the watch-set operations ------------------------------------------

    def watch(self, record: WatchedRunRecord) -> WatchedRunRecord:
        """Persist a watch entry (AC-01).

        Re-watching the identical entry is an idempotent no-op (recovery
        re-establishes the same watch set without error); re-watching
        the same run under a different external identity is refused.

        Raises:
            TypeError: ``record`` is not a ``WatchedRunRecord``.
            WatchRecordError: the record violates the entry contract.
            DuplicateWatchError: the run is already watched with a
                different external identity.
        """
        if not isinstance(record, WatchedRunRecord):
            raise TypeError(
                "record must be a WatchedRunRecord, got"
                f" {type(record).__name__}"
            )
        path = self._entry_path(record.run_id)
        if path.is_file():
            existing = self._read_entry(record.run_id)
            if existing != record:
                raise DuplicateWatchError(
                    f"run {record.run_id!r} is already watched with a"
                    f" different external identity ({existing.external});"
                    " unwatch() first or re-establish the identical entry"
                    " (watch re-establishment is idempotent)"
                )
            return existing
        self._write_entry(record)
        return record

    def unwatch(self, run_id: str) -> None:
        """Remove the watch entry of a run (idempotent: unwatching a run
        that is not watched is a no-op).

        Raises:
            TypeError: ``run_id`` is not a str.
            WatchRecordError: ``run_id`` is not a valid run id.
        """
        self._check_run_id(run_id)
        path = self._watched_dir / f"{run_id}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def get(self, run_id: str) -> WatchedRunRecord:
        """Return the watch entry of one run (re-hydrated from disk).

        Raises:
            TypeError: ``run_id`` is not a str.
            WatchRecordError: ``run_id`` is not a valid run id.
            WatchNotFoundError: the run is not watched.
        """
        self._check_run_id(run_id)
        return self._read_entry(run_id)

    def list_watched(self) -> tuple[WatchedRunRecord, ...]:
        """Return the full watch set, reconstructed from the persisted
        entries alone (AC-01), in sorted run-id order (deterministic).

        A corrupt or foreign entry anywhere under ``<state_dir>/
        watched/`` fails the whole reconstruction loudly (stable
        ``WatchRecordError``) -- the watched directory is Monitor-owned
        and everything in it is a watch entry.
        """
        if not self._watched_dir.is_dir():
            return ()
        records: list[WatchedRunRecord] = []
        for path in sorted(
            self._watched_dir.glob("*.json"), key=lambda p: p.name
        ):
            records.append(self._read_entry(path.stem))
        return tuple(sorted(records, key=lambda entry: entry.run_id))

    def heartbeat(self, run_id: str) -> WatchedRunRecord:
        """Stamp ``last_heartbeat_at`` of a watched run with the
        injected clock and persist the updated entry (watch metadata).

        Raises:
            TypeError: ``run_id`` is not a str.
            WatchRecordError: ``run_id`` is not a valid run id.
            WatchNotFoundError: the run is not watched.
        """
        record = self.get(run_id)
        updated = replace(record, last_heartbeat_at=self._now_fn())
        self._write_entry(updated)
        return updated
