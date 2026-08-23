"""The durable supervisor inbox of arrived Result Packages (issue #163,
deliverable).

Nothing notified the Supervisor when the experiment team returned a
Result Package: completion was recorded as an event only and the
Supervisor adjudicated by manually polling persisted state. The
supervisor inbox fixes that with a durable mailbox the Supervisor reads
on every wake-up:

* :class:`SupervisorInboxEntry` -- one inbox entry per arrived Result
  Package: the run id, the dispatch id (when the backend has one), the
  id of the completion transition event, the injected filing timestamp
  and the ``pending`` review flag. Persisted at
  ``<state_dir>/supervisor-inbox/<run_id>.json``.
* :class:`SupervisorInbox` -- the durable inbox store: ``file_entry``
  files an arrival, ``list_inbox`` returns the pending entries in
  deterministic sorted run-id order, ``mark_reviewed`` flips the
  pending flag once the Supervisor has adjudicated the entry.

The writer is the monitor-facing completion flow: the reconciliation
engine (``monitoring.reconcile``) files the entry in the same
bookkeeping step that appends the completion transition event, so
exactly one entry exists per completion (AC-01 of issue #163) and
re-reconciliation after a crash can never create a duplicate (the entry
is deterministically keyed by run id / completion event id).

No git involvement (AC-02 of the checkpoint design)
---------------------------------------------------
Inbox entries are **plain durable state files** written atomically
through :func:`core.atomic.atomic_write` -- the same durable-state
discipline every subsystem uses. They never require (and never perform)
git audit commits: the state directory stays a plain directory holding
exactly the state files, with no git bookkeeping anywhere (the tests
prove it).

Determinism, secrets, discipline
--------------------------------
The entry's ``injected_at`` timestamp is produced by the filing
principal from its injected clock (the reconciliation engine stamps it
from the engine clock -- no wall clock in the tested path); entries are
persisted as sorted canonical JSON (byte-identical for identical
inputs); entries are listed in sorted run-id order (deterministic).
The entry holds external *ids* only (run id, dispatch id, completion
event id), never credentials. Errors follow the house paradigm:
``TypeError`` at type boundaries, the stable ``MonitoringError``
(``ValueError`` subclass) hierarchy otherwise. The monitoring subsystem
does not import from the adapters package: the dispatch id is a plain
documented field of the core ``RunExternal`` vocabulary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Mapping

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.monitoring.registry import (
    MonitoringError,
    _canonical_json,
)

__all__ = [
    "DuplicateInboxEntryError",
    "INBOX_ENTRY_VERSION",
    "INBOX_STATE_DIR",
    "InboxEntryNotFoundError",
    "InboxRecordError",
    "SupervisorInbox",
    "SupervisorInboxEntry",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the durable inbox-entry schema (the ``record_version`` key
#: of :class:`SupervisorInboxEntry`); entries of a different version are
#: refused.
INBOX_ENTRY_VERSION: str = "1.0"

#: Directory of the inbox entries, relative to the injected state
#: directory: entries live at ``<state_dir>/supervisor-inbox/
#: <run_id>.json``.
INBOX_STATE_DIR: str = "supervisor-inbox"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InboxRecordError(MonitoringError):
    """Raised for corrupt inbox entries and inbox-entry contract
    violations (missing fields, unknown version, invalid ids, mistyped
    or empty fields, an entry whose dispatch id is not a string)."""


class InboxEntryNotFoundError(MonitoringError):
    """Raised when ``mark_reviewed`` refers to a run with no inbox
    entry."""


class DuplicateInboxEntryError(MonitoringError):
    """Raised when an inbox entry is filed for a run that already has
    an entry for a **different** completion event (re-filing the entry
    of the identical completion is an idempotent no-op). One arrival
    can never produce two inbox entries."""


# ---------------------------------------------------------------------------
# The durable inbox entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorInboxEntry:
    """The durable inbox entry of one arrived Result Package.

    Persisted at ``<state_dir>/supervisor-inbox/<run_id>.json`` and
    re-hydrated from disk on every operation: a fresh
    :class:`SupervisorInbox` over the same state directory
    reconstructs the inbox from the persisted entries alone. The entry
    references the completed Run (``run_id``), the dispatch id the
    backend reported the result under (``dispatch_id``; None for
    backends without a dispatch id), the id of the completion
    transition event the event log recorded (``completion_event_id``),
    the injected filing timestamp (``injected_at``) and the review flag
    (``pending``: True until the Supervisor has adjudicated the
    arrival). The entry carries **no credential fields** and no
    secrets: ids and timestamps only.

    Field names are the exact JSON keys of the persisted entry
    (``to_dict`` / ``from_dict`` round-trip them). There is no
    ``schemas/*.schema.yaml`` for runtime monitoring state, so
    ``from_dict`` validates against this documented contract with
    stable errors, mirroring the checkpoint records.
    """

    record_version: ClassVar[str] = INBOX_ENTRY_VERSION

    run_id: str
    completion_event_id: str
    injected_at: str
    pending: bool = True
    dispatch_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                "SupervisorInboxEntry.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, "run"):
            raise InboxRecordError(
                f"inbox entry run_id {self.run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )
        if not isinstance(self.completion_event_id, str):
            raise TypeError(
                "SupervisorInboxEntry.completion_event_id must be a str, got"
                f" {type(self.completion_event_id).__name__}"
            )
        if not is_valid_id(self.completion_event_id, "event"):
            raise InboxRecordError(
                "inbox entry completion_event_id"
                f" {self.completion_event_id!r} is not a valid event id"
                " (sr_event_<32 hex chars>)"
            )
        if not isinstance(self.injected_at, str) or not self.injected_at.strip():
            raise InboxRecordError(
                "inbox entry injected_at must be a non-empty timestamp"
                f" string, got {self.injected_at!r}"
            )
        if not isinstance(self.pending, bool):
            raise TypeError(
                "SupervisorInboxEntry.pending must be a bool, got"
                f" {type(self.pending).__name__}"
            )
        if self.dispatch_id is not None and (
            not isinstance(self.dispatch_id, str) or not self.dispatch_id.strip()
        ):
            raise InboxRecordError(
                "inbox entry dispatch_id must be a non-empty string when"
                f" set, got {self.dispatch_id!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the entry (the ``dispatch_id``
        optional omitted when None)."""
        data: dict[str, Any] = {
            "record_version": self.record_version,
            "run_id": self.run_id,
            "completion_event_id": self.completion_event_id,
            "injected_at": self.injected_at,
            "pending": self.pending,
        }
        if self.dispatch_id is not None:
            data["dispatch_id"] = self.dispatch_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SupervisorInboxEntry:
        """Build an inbox entry from a plain dict (the entry contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            InboxRecordError: a required field is missing or a value
                violates the contract (unknown version, invalid run or
                event id, mistyped or empty fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "SupervisorInboxEntry.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise InboxRecordError(
                    f"inbox entry missing required field {name!r}"
                )
            return data[name]

        record_version = required("record_version")
        if record_version != cls.record_version:
            raise InboxRecordError(
                f"inbox entry version {record_version!r} is not supported;"
                f" expected {cls.record_version!r}"
            )
        run_id = required("run_id")
        completion_event_id = required("completion_event_id")
        injected_at = required("injected_at")
        pending = required("pending")
        dispatch_id = data.get("dispatch_id")
        try:
            return cls(
                run_id=run_id,
                completion_event_id=completion_event_id,
                injected_at=injected_at,
                pending=pending,
                dispatch_id=dispatch_id,
            )
        except (TypeError, ValueError) as exc:
            raise InboxRecordError(
                f"corrupt inbox entry: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The inbox store
# ---------------------------------------------------------------------------


class SupervisorInbox:
    """The durable supervisor inbox of arrived Result Packages (issue
    #163).

    Durable inbox entries live at
    ``<state_dir>/supervisor-inbox/<run_id>.json``, one file per
    arrived Result Package, written through ``atomic_write``. Every
    operation re-hydrates from disk; a **fresh store instance** over
    the same state directory reconstructs the inbox from the persisted
    entries alone (``list_inbox``). Filing is idempotent per
    completion: re-filing the entry of the identical completion (same
    run id and completion event id) is a no-op that returns the
    persisted entry -- re-reconciliation after a crash can never create
    a duplicate entry -- while filing a different completion event for
    a run that already has an entry is refused.

    The inbox is a **plain state-file mailbox**: entries are plain
    JSON files written atomically (the checkpoint AC-02 design) and
    never involve git. The Supervisor reads the pending entries on
    every wake-up (``list_inbox``) and marks each adjudicated arrival
    reviewed (``mark_reviewed``). The store stamps nothing itself: the
    entry's ``injected_at`` timestamp is supplied by the filing
    principal from its injected clock (the reconciliation engine stamps
    it from the engine clock).

    Args:
        state_dir: the injected state directory (entries at
            ``<state_dir>/supervisor-inbox/``).

    Raises:
        TypeError: ``state_dir`` is not a str/Path.
    """

    def __init__(self, state_dir: str | Path) -> None:
        if not isinstance(state_dir, (str, Path)):
            raise TypeError(
                "state_dir must be a str or Path, got"
                f" {type(state_dir).__name__}"
            )
        self._state_dir = Path(state_dir)
        self._inbox_dir = self._state_dir / INBOX_STATE_DIR

    # -- identity and persistence ------------------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected state directory."""
        return self._state_dir

    @property
    def inbox_dir(self) -> Path:
        """The inbox-entry directory (``<state_dir>/supervisor-inbox/``)."""
        return self._inbox_dir

    def _check_run_id(self, run_id: str) -> None:
        if not isinstance(run_id, str):
            raise TypeError(
                f"run_id must be a str, got {type(run_id).__name__}"
            )
        if not is_valid_id(run_id, "run"):
            raise InboxRecordError(
                f"run id {run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )

    def _entry_path(self, run_id: str) -> Path:
        self._check_run_id(run_id)
        return self._inbox_dir / f"{run_id}.json"

    def _write_entry(self, entry: SupervisorInboxEntry) -> None:
        atomic_write(
            self._entry_path(entry.run_id), _canonical_json(entry.to_dict())
        )

    def _read_entry(self, run_id: str) -> SupervisorInboxEntry:
        """Re-hydrate one inbox entry from disk (the M1 recovery
        discipline: never trust session state)."""
        path = self._entry_path(run_id)
        if not path.is_file():
            if path.exists():
                raise InboxRecordError(
                    f"inbox entry at {path} is not a regular file"
                )
            raise InboxEntryNotFoundError(
                f"no inbox entry for run {run_id!r} at {path}"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InboxRecordError(
                f"corrupt inbox entry at {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise InboxRecordError(
                f"corrupt inbox entry at {path}: expected a JSON object"
            )
        try:
            return SupervisorInboxEntry.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise InboxRecordError(
                f"corrupt inbox entry at {path}: {exc}"
            ) from exc

    # -- the inbox operations ----------------------------------------------

    def file_entry(self, entry: SupervisorInboxEntry) -> SupervisorInboxEntry:
        """Persist one inbox entry for an arrived Result Package.

        The entry is deterministically keyed by its run id and
        completion event id (issue #163): re-filing the entry of the
        identical completion is an idempotent no-op that returns the
        persisted entry -- re-reconciliation after a crash can never
        create a duplicate entry -- while filing a *different*
        completion event for a run that already has an entry is refused
        (one arrival, one entry).

        Raises:
            TypeError: ``entry`` is not a ``SupervisorInboxEntry``.
            InboxRecordError: the entry violates the entry contract.
            DuplicateInboxEntryError: the run already has an entry for
                a different completion event.
        """
        if not isinstance(entry, SupervisorInboxEntry):
            raise TypeError(
                "entry must be a SupervisorInboxEntry, got"
                f" {type(entry).__name__}"
            )
        path = self._entry_path(entry.run_id)
        if path.is_file():
            existing = self._read_entry(entry.run_id)
            if existing.completion_event_id != entry.completion_event_id:
                raise DuplicateInboxEntryError(
                    f"run {entry.run_id!r} already has an inbox entry for"
                    f" completion event {existing.completion_event_id!r};"
                    " filing a different completion event"
                    f" {entry.completion_event_id!r} for the same run would"
                    " create a duplicate arrival record"
                )
            return existing
        self._write_entry(entry)
        return entry

    def list_inbox(self) -> tuple[SupervisorInboxEntry, ...]:
        """Return the **pending** inbox entries -- the arrivals the
        Supervisor has not adjudicated yet -- reconstructed from the
        persisted entries alone, in sorted run-id order (deterministic).

        Reviewed entries (``pending`` False) stay persisted for the
        audit trail but are no longer pending work: they are excluded
        from the listing, so the Supervisor's next wake-up only sees
        what still needs adjudication.

        A corrupt or foreign entry anywhere under ``<state_dir>/
        supervisor-inbox/`` fails the whole listing loudly (stable
        ``InboxRecordError``) -- the inbox directory is a durable
        mailbox and everything in it is an inbox entry.
        """
        if not self._inbox_dir.is_dir():
            return ()
        entries: list[SupervisorInboxEntry] = []
        for path in sorted(
            self._inbox_dir.glob("*.json"), key=lambda p: p.name
        ):
            entries.append(self._read_entry(path.stem))
        return tuple(
            sorted(
                (entry for entry in entries if entry.pending),
                key=lambda entry: entry.run_id,
            )
        )

    def mark_reviewed(self, run_id: str) -> SupervisorInboxEntry:
        """Mark the inbox entry of ``run_id`` reviewed (``pending``
        False) and persist the updated entry.

        Idempotent: marking an already-reviewed entry is a pure no-op
        (the persisted bytes stay identical -- nothing is re-stamped).

        Raises:
            TypeError: ``run_id`` is not a str.
            InboxRecordError: ``run_id`` is not a valid run id.
            InboxEntryNotFoundError: the run has no inbox entry.
        """
        self._check_run_id(run_id)
        entry = self._read_entry(run_id)
        if not entry.pending:
            return entry
        updated = replace(entry, pending=False)
        self._write_entry(updated)
        return updated
