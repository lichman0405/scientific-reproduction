"""The monitor checkpoint and heartbeat records (DEV-M8-G01,
deliverable).

The Monitor's recovery checkpoint and liveness heartbeat:

* :class:`MonitorCheckpoint` -- the recovery checkpoint of the
  Monitor's reconciliation progress: for every watched run it carries
  the run reference and the adapter/external ids needed to reconcile it
  later -- backend, ``dispatch_id``/``job_id``, working directory
  (AC-03 of DEV-M8-G01) -- plus the last observation. Persisted
  durably at ``<state_dir>/checkpoint.json``, validated on read, and
  recovered by a fresh store instance over the same state directory.
* :class:`HeartbeatRecord` -- the Monitor's liveness record (monitor
  id, heartbeat timestamp, watched-run count), persisted at
  ``<state_dir>/heartbeat.json``.

No git involvement (AC-02)
--------------------------
Heartbeat and checkpoint updates are **plain durable state files**
written atomically through :func:`core.atomic.atomic_write` -- the same
durable-state discipline every subsystem uses. They never require (and
never perform) git audit commits: the state directory stays a plain
directory holding exactly the state files, with no git bookkeeping
anywhere (the tests prove it).

Determinism, secrets, discipline
--------------------------------
All timestamps come from the injected clock (``now``); records are
persisted as sorted canonical JSON (byte-identical for identical
inputs); the checkpoint's per-run entries are persisted in sorted
run-id order; the records hold external *ids* only, never credentials.
Errors follow the house paradigm: ``TypeError`` at type boundaries, the
stable ``MonitoringError`` (``ValueError`` subclass) hierarchy
otherwise. The monitoring subsystem does not import from the adapters
package: the external ids are plain documented fields of the core
``RunExternal`` vocabulary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import RunExternal
from scientific_reproduction.monitoring.registry import (
    MONITOR_ID_KIND,
    MonitoringClock,
    MonitoringError,
    _canonical_json,
    derive_monitor_id,
    utc_now,
    validate_external_identity,
)

__all__ = [
    "CHECKPOINT_FILE",
    "CHECKPOINT_VERSION",
    "CheckpointRecordError",
    "HEARTBEAT_FILE",
    "HEARTBEAT_VERSION",
    "HeartbeatRecord",
    "MonitorCheckpoint",
    "MonitorCheckpointStore",
    "MonitorRunCheckpoint",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the durable checkpoint schema (the ``record_version`` key
#: of :class:`MonitorCheckpoint`); checkpoints of a different version
#: are refused.
CHECKPOINT_VERSION: str = "1.0"

#: Version of the durable heartbeat schema (the ``record_version`` key
#: of :class:`HeartbeatRecord`).
HEARTBEAT_VERSION: str = "1.0"

#: File name of the checkpoint, relative to the injected state
#: directory: ``<state_dir>/checkpoint.json``.
CHECKPOINT_FILE: str = "checkpoint.json"

#: File name of the heartbeat, relative to the injected state
#: directory: ``<state_dir>/heartbeat.json``.
HEARTBEAT_FILE: str = "heartbeat.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CheckpointRecordError(MonitoringError):
    """Raised for corrupt checkpoint/heartbeat files and contract
    violations (missing fields, unknown version, invalid ids, missing
    external identity, mistyped or empty fields, a checkpoint saved
    under a different monitor's store)."""


# ---------------------------------------------------------------------------
# The per-run reconciliation progress (AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorRunCheckpoint:
    """The reconciliation progress of one watched external Run.

    ``external`` carries the adapter/external identity needed to
    reconcile the run later -- backend, ``dispatch_id`` and/or
    ``job_id``, working directory (AC-03) -- so a restarted Monitor
    resumes reconciliation from the persisted checkpoint alone.
    ``observed_state``/``observed_at`` record the last external
    observation; ``reconciled_at`` when the run was last reconciled.
    The entry holds external ids only -- never credentials.
    """

    run_id: str
    external: RunExternal
    observed_state: str | None = None
    observed_at: str | None = None
    reconciled_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                "MonitorRunCheckpoint.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, "run"):
            raise CheckpointRecordError(
                f"checkpoint entry run_id {self.run_id!r} is not a valid"
                " run id (sr_run_<32 hex chars>)"
            )
        if not isinstance(self.external, RunExternal):
            raise TypeError(
                "MonitorRunCheckpoint.external must be a RunExternal, got"
                f" {type(self.external).__name__}"
            )
        validate_external_identity(self.external, error=CheckpointRecordError)
        for name in ("observed_state", "observed_at", "reconciled_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise CheckpointRecordError(
                    f"checkpoint entry {name} must be a non-empty string"
                    f" when set, got {value!r}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the entry (``None`` optionals
        omitted)."""
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "external": self.external.to_dict(),
        }
        for key in ("observed_state", "observed_at", "reconciled_at"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MonitorRunCheckpoint:
        """Build a checkpoint entry from a plain dict (the entry
        contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            CheckpointRecordError: a required field is missing or a
                value violates the contract (invalid run id,
                missing/incomplete external identity, mistyped or empty
                fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "MonitorRunCheckpoint.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise CheckpointRecordError(
                    f"checkpoint entry missing required field {name!r}"
                )
            return data[name]

        run_id = required("run_id")
        external_raw = required("external")
        if not isinstance(external_raw, Mapping):
            raise CheckpointRecordError(
                "checkpoint entry field 'external' must be a mapping, got"
                f" {type(external_raw).__name__}"
            )
        try:
            external = RunExternal.from_dict(external_raw)
        except (TypeError, ValueError) as exc:
            raise CheckpointRecordError(
                f"corrupt checkpoint entry 'external' field: {exc}"
            ) from exc
        observed_state = data.get("observed_state")
        observed_at = data.get("observed_at")
        reconciled_at = data.get("reconciled_at")
        try:
            return cls(
                run_id=run_id,
                external=external,
                observed_state=observed_state,
                observed_at=observed_at,
                reconciled_at=reconciled_at,
            )
        except (TypeError, ValueError) as exc:
            raise CheckpointRecordError(
                f"corrupt checkpoint entry: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The recovery checkpoint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorCheckpoint:
    """The recovery checkpoint of the Monitor's reconciliation progress
    (AC-03).

    ``entries`` name, for every run the Monitor reconciles, the
    adapter/external ids needed for reconciliation (backend,
    ``dispatch_id``/``job_id``, working directory -- AC-03) plus the
    last observation, so a restarted Monitor resumes reconciliation
    from the persisted checkpoint alone. The checkpoint is persisted
    durably at ``<state_dir>/checkpoint.json`` (``atomic_write``, no
    git involvement -- AC-02), validated on read, and recovered by a
    fresh store instance over the same state directory.
    """

    record_version: ClassVar[str] = CHECKPOINT_VERSION

    monitor_id: str
    created_at: str
    entries: tuple[MonitorRunCheckpoint, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.monitor_id, str):
            raise TypeError(
                "MonitorCheckpoint.monitor_id must be a str, got"
                f" {type(self.monitor_id).__name__}"
            )
        if not is_valid_id(self.monitor_id, MONITOR_ID_KIND):
            raise CheckpointRecordError(
                f"checkpoint monitor_id {self.monitor_id!r} is not a valid"
                " monitor id (sr_monitor_<32 hex chars>)"
            )
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise CheckpointRecordError(
                "checkpoint created_at must be a non-empty timestamp"
                f" string, got {self.created_at!r}"
            )
        if not isinstance(self.entries, tuple):
            raise TypeError(
                "MonitorCheckpoint.entries must be a tuple of"
                f" MonitorRunCheckpoint entries, got"
                f" {type(self.entries).__name__}"
            )
        for entry in self.entries:
            if not isinstance(entry, MonitorRunCheckpoint):
                raise TypeError(
                    "MonitorCheckpoint.entries entries must be"
                    f" MonitorRunCheckpoint, got {type(entry).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the checkpoint."""
        return {
            "record_version": self.record_version,
            "monitor_id": self.monitor_id,
            "created_at": self.created_at,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MonitorCheckpoint:
        """Build a checkpoint from a plain dict (the checkpoint
        contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            CheckpointRecordError: a required field is missing or a
                value violates the contract (unknown version, invalid
                monitor id, corrupt entries).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "MonitorCheckpoint.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise CheckpointRecordError(
                    f"checkpoint missing required field {name!r}"
                )
            return data[name]

        record_version = required("record_version")
        if record_version != cls.record_version:
            raise CheckpointRecordError(
                f"checkpoint version {record_version!r} is not supported;"
                f" expected {cls.record_version!r}"
            )
        monitor_id = required("monitor_id")
        created_at = required("created_at")
        entries_raw = required("entries")
        if not isinstance(entries_raw, (list, tuple)):
            raise CheckpointRecordError(
                "checkpoint field 'entries' must be a list, got"
                f" {type(entries_raw).__name__}"
            )
        entries: list[MonitorRunCheckpoint] = []
        for index, raw in enumerate(entries_raw):
            if not isinstance(raw, Mapping):
                raise CheckpointRecordError(
                    f"corrupt checkpoint entry {index}: expected a mapping,"
                    f" got {type(raw).__name__}"
                )
            try:
                entries.append(MonitorRunCheckpoint.from_dict(raw))
            except (TypeError, ValueError) as exc:
                raise CheckpointRecordError(
                    f"corrupt checkpoint entry {index}: {exc}"
                ) from exc
        try:
            return cls(
                monitor_id=monitor_id,
                created_at=created_at,
                entries=tuple(entries),
            )
        except (TypeError, ValueError) as exc:
            raise CheckpointRecordError(
                f"corrupt checkpoint: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The heartbeat record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HeartbeatRecord:
    """The Monitor's liveness heartbeat (AC-02 deliverable).

    A plain durable state file (``<state_dir>/heartbeat.json``) written
    atomically on every beat -- never a git commit. Carries the monitor
    identity, the injected heartbeat timestamp and the count of watched
    runs the Monitor is currently tracking.
    """

    record_version: ClassVar[str] = HEARTBEAT_VERSION

    monitor_id: str
    heartbeat_at: str
    watched_run_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.monitor_id, str):
            raise TypeError(
                "HeartbeatRecord.monitor_id must be a str, got"
                f" {type(self.monitor_id).__name__}"
            )
        if not is_valid_id(self.monitor_id, MONITOR_ID_KIND):
            raise CheckpointRecordError(
                f"heartbeat monitor_id {self.monitor_id!r} is not a valid"
                " monitor id (sr_monitor_<32 hex chars>)"
            )
        if not isinstance(self.heartbeat_at, str) or not self.heartbeat_at.strip():
            raise CheckpointRecordError(
                "heartbeat heartbeat_at must be a non-empty timestamp"
                f" string, got {self.heartbeat_at!r}"
            )
        if isinstance(self.watched_run_count, bool) or not isinstance(
            self.watched_run_count, int
        ):
            raise TypeError(
                "HeartbeatRecord.watched_run_count must be an int, got"
                f" {type(self.watched_run_count).__name__}"
            )
        if self.watched_run_count < 0:
            raise CheckpointRecordError(
                "heartbeat watched_run_count must be >= 0, got"
                f" {self.watched_run_count}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the heartbeat."""
        return {
            "record_version": self.record_version,
            "monitor_id": self.monitor_id,
            "heartbeat_at": self.heartbeat_at,
            "watched_run_count": self.watched_run_count,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HeartbeatRecord:
        """Build a heartbeat from a plain dict (the heartbeat contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            CheckpointRecordError: a required field is missing or a
                value violates the contract (unknown version, invalid
                monitor id, mistyped or empty fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "HeartbeatRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise CheckpointRecordError(
                    f"heartbeat missing required field {name!r}"
                )
            return data[name]

        record_version = required("record_version")
        if record_version != cls.record_version:
            raise CheckpointRecordError(
                f"heartbeat version {record_version!r} is not supported;"
                f" expected {cls.record_version!r}"
            )
        monitor_id = required("monitor_id")
        heartbeat_at = required("heartbeat_at")
        watched_run_count = required("watched_run_count")
        try:
            return cls(
                monitor_id=monitor_id,
                heartbeat_at=heartbeat_at,
                watched_run_count=watched_run_count,
            )
        except (TypeError, ValueError) as exc:
            raise CheckpointRecordError(
                f"corrupt heartbeat: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The checkpoint store
# ---------------------------------------------------------------------------


class MonitorCheckpointStore:
    """The durable store of the monitor checkpoint and heartbeat
    (AC-02/AC-03).

    Both records are **plain state files**: the checkpoint at
    ``<state_dir>/checkpoint.json``, the heartbeat at
    ``<state_dir>/heartbeat.json``, each written atomically through
    ``atomic_write`` and re-hydrated from disk on every read (the M1
    recovery discipline). Updates never involve git -- no audit commit,
    no repository bookkeeping; the state directory stays a plain
    directory holding exactly the state files (AC-02, proven by the
    tests). A **fresh store instance** over the same state directory
    recovers the persisted checkpoint and heartbeat (AC-03 recovery).
    The checkpoint's per-run entries are persisted in sorted run-id
    order (deterministic bytes).

    Args:
        state_dir: the injected state directory (``checkpoint.json`` /
            ``heartbeat.json`` at its root).
        now: injectable clock producing a timestamp string (default
            ``utc_now``); ``heartbeat`` stamps from it -- no wall clock
            in the tested path.
        monitor_id: the Monitor identity (``sr_monitor_<32 hex>``).
            Defaults to the deterministic identity of the state
            directory (``derive_monitor_id``), so the registry and the
            checkpoint store over the same directory agree on the
            Monitor. A store only persists its own monitor's
            checkpoint.

    Raises:
        TypeError: ``state_dir`` is not a str/Path, or ``now`` is not
            callable.
        CheckpointRecordError: an injected ``monitor_id`` is not a
            valid monitor id.
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
                raise CheckpointRecordError(
                    f"monitor_id {monitor_id!r} is not a valid monitor id"
                    " (sr_monitor_<32 hex chars>)"
                )
            self._monitor_id = monitor_id
        else:
            self._monitor_id = derive_monitor_id(self._state_dir)

    # -- identity and persistence ------------------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected state directory."""
        return self._state_dir

    @property
    def monitor_id(self) -> str:
        """The Monitor identity owning this store."""
        return self._monitor_id

    @property
    def checkpoint_path(self) -> Path:
        """The checkpoint file (``<state_dir>/checkpoint.json``)."""
        return self._state_dir / CHECKPOINT_FILE

    @property
    def heartbeat_path(self) -> Path:
        """The heartbeat file (``<state_dir>/heartbeat.json``)."""
        return self._state_dir / HEARTBEAT_FILE

    # -- checkpoint ---------------------------------------------------------

    def save(self, checkpoint: MonitorCheckpoint) -> None:
        """Persist a checkpoint as a plain atomic state-file write
        (AC-02/AC-03).

        The per-run entries are persisted in sorted run-id order
        (deterministic bytes). The checkpoint is only ever written
        through ``atomic_write`` -- no git involvement of any kind.

        Raises:
            TypeError: ``checkpoint`` is not a ``MonitorCheckpoint``.
            CheckpointRecordError: the checkpoint belongs to a
                different monitor than this store.
        """
        if not isinstance(checkpoint, MonitorCheckpoint):
            raise TypeError(
                "checkpoint must be a MonitorCheckpoint, got"
                f" {type(checkpoint).__name__}"
            )
        if checkpoint.monitor_id != self._monitor_id:
            raise CheckpointRecordError(
                f"checkpoint monitor_id {checkpoint.monitor_id!r} does not"
                f" match this store's monitor {self._monitor_id!r}; a"
                " store only persists its own monitor's checkpoint"
            )
        ordered = MonitorCheckpoint(
            monitor_id=checkpoint.monitor_id,
            created_at=checkpoint.created_at,
            entries=tuple(
                sorted(checkpoint.entries, key=lambda entry: entry.run_id)
            ),
        )
        atomic_write(
            self.checkpoint_path, _canonical_json(ordered.to_dict())
        )

    def load(self) -> MonitorCheckpoint | None:
        """Return the persisted checkpoint, or None when none was ever
        written. A **fresh store instance** over the same state
        directory recovers the checkpoint from this file alone
        (AC-03).

        Raises:
            CheckpointRecordError: the stored checkpoint is corrupt.
        """
        path = self.checkpoint_path
        if not path.is_file():
            if path.exists():
                raise CheckpointRecordError(
                    f"checkpoint at {path} is not a regular file"
                )
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointRecordError(
                f"corrupt checkpoint at {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise CheckpointRecordError(
                f"corrupt checkpoint at {path}: expected a JSON object"
            )
        try:
            return MonitorCheckpoint.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise CheckpointRecordError(
                f"corrupt checkpoint at {path}: {exc}"
            ) from exc

    # -- heartbeat ----------------------------------------------------------

    def heartbeat(self, watched_run_count: int) -> HeartbeatRecord:
        """Write one heartbeat as a plain atomic state-file write
        (AC-02): the beat is stamped from the injected clock and
        persisted at ``<state_dir>/heartbeat.json`` -- never a git
        commit, no git bookkeeping anywhere.

        Raises:
            TypeError: ``watched_run_count`` is not an int.
            CheckpointRecordError: ``watched_run_count`` is negative.
        """
        record = HeartbeatRecord(
            monitor_id=self._monitor_id,
            heartbeat_at=self._now_fn(),
            watched_run_count=watched_run_count,
        )
        atomic_write(self.heartbeat_path, _canonical_json(record.to_dict()))
        return record

    def load_heartbeat(self) -> HeartbeatRecord | None:
        """Return the persisted heartbeat, or None when none was ever
        written. A fresh store instance over the same state directory
        recovers it from this file alone.

        Raises:
            CheckpointRecordError: the stored heartbeat is corrupt.
        """
        path = self.heartbeat_path
        if not path.is_file():
            if path.exists():
                raise CheckpointRecordError(
                    f"heartbeat at {path} is not a regular file"
                )
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CheckpointRecordError(
                f"corrupt heartbeat at {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise CheckpointRecordError(
                f"corrupt heartbeat at {path}: expected a JSON object"
            )
        try:
            return HeartbeatRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise CheckpointRecordError(
                f"corrupt heartbeat at {path}: {exc}"
            ) from exc
