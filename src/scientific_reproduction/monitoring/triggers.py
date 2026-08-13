"""Duplicate-trigger protection and the follow-up worker request
(DEV-M8-G05, deliverable).

The Execution Monitor scans watched Runs that reached ``RESULT_AVAILABLE``:
each such run may trigger ONE semantic follow-up -- an analysis/handling
request for the returned result (a follow-up worker request). This module
implements the duplicate-trigger protection registry as a deterministic
durable primitive: at most one trigger per run ever, even under repeated
scans of the same ``RESULT_AVAILABLE`` run (AC-01) and across Monitor
restarts (AC-02), because every trigger decision is a durable record that
a fresh registry instance over the same state directory reconstructs from
the persisted records alone.

Layout and recovery
-------------------
One persisted record per triggered run at
``<state_dir>/trigger/<run_id>.json``, written through
:func:`core.atomic.atomic_write` (durable-state discipline of the whole
codebase) and re-hydrated from disk on every operation (the M1 recovery
discipline: never trust session state). A fresh :class:`TriggerRegistry`
over the same state directory returns the full trigger set from
``list_triggered()`` / ``get(run_id)`` -- the AC-02 reconstruction
assertion.

Exactly-once scan (AC-01)
-------------------------
``scan(run)`` enforces the at-most-one-trigger invariant: the first scan
of a result-bearing Run invokes the injected follow-up hook exactly once
and persists the deterministic trigger record; every repeated scan of the
same run resolves to that single original record and never invokes the
hook again (the tests prove it with a counting hook: one call total
across repeated scans, durable bytes identical on re-scan). The record
carries a deterministic id (``generate_id("trigger", run_id, <kind>)`` --
no randomness) and enough for audit: ``run_id``, ``triggered_at`` (the
injected clock), the trigger kind vocabulary and the follow-up
identity/receipt when the hook returns one.

Only a Run whose durable lifecycle state records ``RESULT_AVAILABLE`` (or
a later result-bearing state; the same set the recovery procedure treats
as durably completed) can be triggered; scanning anything else is
*observed* and *ignored* -- never triggered, never fabricated. The single
trigger-kind vocabulary (``TRIGGER_KINDS``, one member) mirrors the
reconcile engine's ``COMPLETION_SIGNALS`` discipline. The strict issuance
entry point ``trigger(run)`` raises the stable ``TriggerContractError``
for a run that cannot carry a follow-up.

The follow-up seam
------------------
The follow-up is an injected hook (:data:`FollowupHook`, a callable over
the durable ``Run``), never an adapters import: the monitoring subsystem
never imports ``scientific_reproduction.adapters`` (locked by
``tests/monitoring/test_monitoring_surface.py``) and the hook's
vocabulary is plain documented types. A hook exception propagates loudly:
nothing is recorded and the decision stays re-issuable -- the follow-up is
never recorded as a fabricated success. With no hook injected, the scan
still records the durable at-most-once decision (no receipt); the default
configuration can never invoke an adapter.

Determinism, secrets, discipline
--------------------------------
All timestamps come from the injected clock (``now``; no wall clock in
the tested path); records are persisted as sorted canonical JSON
(byte-identical for identical inputs); ids are generated with
``core.ids.generate_id``; the registry never persists credentials -- only
ids, timestamps and the vocabulary, and a hook failure message is never
recorded (the tests walk every persisted byte). Errors follow the house
paradigm: ``TypeError`` at public type boundaries, stable
``MonitoringError`` subclasses otherwise (``TriggerError`` ->
``TriggerContractError`` for lifecycle contract violations,
``CorruptTriggerStateError`` for corrupt trigger records). Corrupt
persisted state fails loudly, never silently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Mapping, TypeAlias

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import LifecycleState, Run
from scientific_reproduction.monitoring.registry import (
    MONITOR_ID_KIND,
    MonitoringClock,
    MonitoringError,
    _canonical_json,
    derive_monitor_id,
    utc_now,
)

__all__ = [
    "CorruptTriggerStateError",
    "FOLLOWUP_TRIGGER_KIND",
    "FollowupHook",
    "ScanOutcome",
    "TRIGGER_ID_KIND",
    "TRIGGER_KINDS",
    "TRIGGER_RECORD_VERSION",
    "TRIGGERED_STATE_DIR",
    "TriggerContractError",
    "TriggerError",
    "TriggerRecord",
    "TriggerRegistry",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the durable trigger-record schema (the ``record_version``
#: key of :class:`TriggerRecord`); records of a different version are
#: refused.
TRIGGER_RECORD_VERSION: str = "1.0"

#: Directory of the trigger records, relative to the injected state
#: directory: records live at ``<state_dir>/trigger/<run_id>.json``.
TRIGGERED_STATE_DIR: str = "trigger"

#: ID kind of a trigger record (``sr_trigger_<32 hex chars>``): the
#: deterministic record id is a pure function of the run and the trigger
#: kind (``generate_id("trigger", run_id, <kind>)``) -- no randomness.
TRIGGER_ID_KIND: str = "trigger"

#: The one trigger kind of this milestone: a run's result became
#: available and the Monitor issues its single follow-up analysis/
#: handling request. The single member of :data:`TRIGGER_KINDS` mirrors
#: the reconcile engine's ``COMPLETION_SIGNALS`` discipline: the trigger
#: vocabulary has exactly one member, and a trigger decision is never
#: fabricated outside it.
FOLLOWUP_TRIGGER_KIND: str = "result_available_followup"

#: The explicit trigger-kind vocabulary: exactly one member. A persisted
#: record naming any other kind is corrupt state.
TRIGGER_KINDS: frozenset[str] = frozenset({FOLLOWUP_TRIGGER_KIND})


# ---------------------------------------------------------------------------
# Errors (stable MonitoringError subclasses)
# ---------------------------------------------------------------------------


class TriggerError(MonitoringError):
    """Base error of the duplicate-trigger protection registry."""


class TriggerContractError(TriggerError):
    """Raised when a run that cannot carry a follow-up is triggered: its
    durable lifecycle state does not record a result (``RESULT_AVAILABLE``
    or later), so a follow-up would be fabricated onto the run. Scanning
    such a run observes and ignores it (never a fabricated trigger);
    ``trigger()`` raises this contract error instead."""


class CorruptTriggerStateError(TriggerError):
    """Raised for corrupt trigger records and trigger-record contract
    violations: unreadable files, malformed JSON, unknown record
    versions, invalid record/run ids, records whose run id disagrees
    with their file name, unknown trigger kinds, mistyped or empty
    fields. Corrupt persisted state fails loudly, never silently."""


# ---------------------------------------------------------------------------
# The injected follow-up hook
# ---------------------------------------------------------------------------

#: The injected follow-up hook: the seam through which the Monitor issues
#: the single semantic follow-up (an analysis/handling worker request)
#: for a result-bearing Run, receiving the durable ``Run`` record and
#: returning the follow-up identity/receipt (or None when the follow-up
#: carries no receipt). Never an adapters import: the hook's vocabulary
#: is plain documented types. A hook exception propagates loudly --
#: nothing is recorded and the decision stays re-issuable.
FollowupHook: TypeAlias = Callable[[Run], str | None]


# ---------------------------------------------------------------------------
# Run-lifecycle sets (mirror the reconcile engine / recovery procedure:
# a result is durably recorded in the Run record from RESULT_AVAILABLE on)
# ---------------------------------------------------------------------------

#: Lifecycle states in which the durable Run record records the result
#: (``RESULT_AVAILABLE`` or later, mirroring the recovery procedure's
#: ``_RESULT_RECORDED_RUN_STATES``): the only runs that can carry a
#: follow-up. Scanning any other run observes and ignores it -- never
#: triggered, never fabricated.
_RESULT_RECORDED_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
        LifecycleState.INVALIDATED,
    }
)


# ---------------------------------------------------------------------------
# The durable trigger record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerRecord:
    """The durable trigger record of one Run (AC-01/AC-02).

    Persisted at ``<state_dir>/trigger/<run_id>.json`` and re-hydrated
    from disk on every operation: a fresh registry instance over the same
    state directory reconstructs the trigger set from the persisted
    records alone (AC-02). The record carries everything needed for
    audit: the deterministic record id (``generate_id("trigger", run_id,
    <kind>)``), the run reference (``run_id``), the trigger kind
    vocabulary (``trigger_kind``), the injected-clock stamp
    (``triggered_at``) and the follow-up identity/receipt returned by
    the injected hook (``followup_id``, absent when the hook returns
    none). The record carries **no credential fields** and no secrets.

    Field names are the exact JSON keys of the persisted record
    (``to_dict`` / ``from_dict`` round-trip them); ``from_dict``
    validates against this documented contract with stable errors.
    """

    record_version: ClassVar[str] = TRIGGER_RECORD_VERSION

    record_id: str
    run_id: str
    trigger_kind: str
    triggered_at: str
    followup_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str):
            raise TypeError(
                "TriggerRecord.record_id must be a str, got"
                f" {type(self.record_id).__name__}"
            )
        if not is_valid_id(self.record_id, TRIGGER_ID_KIND):
            raise CorruptTriggerStateError(
                f"trigger record id {self.record_id!r} is not a valid"
                " trigger id (sr_trigger_<32 hex chars>)"
            )
        if not isinstance(self.run_id, str):
            raise TypeError(
                "TriggerRecord.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, "run"):
            raise CorruptTriggerStateError(
                f"trigger record run_id {self.run_id!r} is not a valid run"
                " id (sr_run_<32 hex chars>)"
            )
        if not isinstance(self.trigger_kind, str):
            raise TypeError(
                "TriggerRecord.trigger_kind must be a str, got"
                f" {type(self.trigger_kind).__name__}"
            )
        if self.trigger_kind not in TRIGGER_KINDS:
            raise CorruptTriggerStateError(
                f"trigger record trigger_kind {self.trigger_kind!r} is not"
                f" a known trigger kind (expected one of"
                f" {sorted(TRIGGER_KINDS)})"
            )
        if not isinstance(self.triggered_at, str) or not self.triggered_at.strip():
            raise CorruptTriggerStateError(
                "trigger record triggered_at must be a non-empty timestamp"
                f" string, got {self.triggered_at!r}"
            )
        if self.followup_id is not None and (
            not isinstance(self.followup_id, str)
            or not self.followup_id.strip()
        ):
            raise CorruptTriggerStateError(
                "trigger record followup_id must be a non-empty string when"
                f" set, got {self.followup_id!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the record (``None`` optionals
        omitted)."""
        data: dict[str, Any] = {
            "record_version": self.record_version,
            "record_id": self.record_id,
            "run_id": self.run_id,
            "trigger_kind": self.trigger_kind,
            "triggered_at": self.triggered_at,
        }
        if self.followup_id is not None:
            data["followup_id"] = self.followup_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TriggerRecord:
        """Build a trigger record from a plain dict (the record
        contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            CorruptTriggerStateError: a required field is missing or a
                value violates the contract (unknown version, invalid
                record/run id, unknown trigger kind, mistyped or empty
                fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "TriggerRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise CorruptTriggerStateError(
                    f"trigger record missing required field {name!r}"
                )
            return data[name]

        record_version = required("record_version")
        if record_version != cls.record_version:
            raise CorruptTriggerStateError(
                f"trigger record version {record_version!r} is not"
                f" supported; expected {cls.record_version!r}"
            )
        record_id = required("record_id")
        run_id = required("run_id")
        trigger_kind = required("trigger_kind")
        triggered_at = required("triggered_at")
        followup_id = data.get("followup_id")
        try:
            return cls(
                record_id=record_id,
                run_id=run_id,
                trigger_kind=trigger_kind,
                triggered_at=triggered_at,
                followup_id=followup_id,
            )
        except (TypeError, ValueError) as exc:
            raise CorruptTriggerStateError(
                f"corrupt trigger record: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The scan outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanOutcome:
    """The outcome of scanning one Run (AC-01).

    Exactly one of the flags is set:

    * ``triggered`` -- this scan issued the follow-up: the injected hook
      was invoked exactly once and the deterministic trigger record was
      persisted.
    * ``replayed`` -- the run's single original trigger record was
      resolved: the record already existed, nothing was invoked and
      nothing was written (``record`` is that original record).
    * ``ignored`` -- the run cannot carry a follow-up: its durable
      lifecycle state does not record a result, so it was observed and
      ignored -- never triggered, never fabricated (``record`` is None).

    ``record`` is the trigger record when the run was triggered or
    replayed, and None when it was ignored.
    """

    run_id: str
    record: TriggerRecord | None
    triggered: bool = False
    replayed: bool = False
    ignored: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                f"ScanOutcome.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, "run"):
            raise TriggerError(
                f"ScanOutcome.run_id {self.run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )
        for name in ("triggered", "replayed", "ignored"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"ScanOutcome.{name} must be a bool, got"
                    f" {type(value).__name__}"
                )
        if sum((self.triggered, self.replayed, self.ignored)) != 1:
            raise TriggerError(
                "ScanOutcome must set exactly one of triggered / replayed"
                " / ignored"
            )
        if self.ignored != (self.record is None):
            raise TriggerError(
                "ScanOutcome.ignored must be True exactly when record is"
                " None (an ignored run has no trigger record)"
            )
        if not self.ignored:
            if not isinstance(self.record, TriggerRecord):
                raise TypeError(
                    "ScanOutcome.record must be a TriggerRecord when the"
                    f" run was triggered or replayed, got"
                    f" {type(self.record).__name__}"
                )
            if self.record.run_id != self.run_id:
                raise TriggerError(
                    f"ScanOutcome.record belongs to run"
                    f" {self.record.run_id!r}, not {self.run_id!r}"
                )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


class TriggerRegistry:
    """The Monitor-owned duplicate-trigger protection registry
    (AC-01/AC-02).

    Durable trigger records live at ``<state_dir>/trigger/<run_id>.json``,
    one file per triggered run, written through ``atomic_write``. Every
    operation re-hydrates from disk; a **fresh registry instance** over
    the same state directory reconstructs the full trigger set from the
    persisted records alone (``list_triggered`` / ``get``) -- the AC-02
    reconstruction assertion.

    ``scan(run)`` is the Monitor's scan operation: a result-bearing Run
    is triggered at most once (the first scan invokes the injected
    follow-up hook and persists the deterministic record; repeated scans
    resolve to that single original record -- AC-01), and any other Run
    is observed and ignored, never triggered, never fabricated.
    ``trigger(run)`` is the strict issuance entry point: it raises the
    stable ``TriggerContractError`` for a run that cannot carry a
    follow-up and is otherwise an idempotent exactly-once issuance.

    Args:
        state_dir: the injected state directory (records at
            ``<state_dir>/trigger/``).
        now: injectable clock producing a timestamp string (default
            ``utc_now``); the trigger stamp comes from it -- no wall
            clock in the tested path.
        monitor_id: the Monitor identity (``sr_monitor_<32 hex>``).
            Defaults to the deterministic identity of the state
            directory (``derive_monitor_id``), so the registry, the
            watched-Run registry and the checkpoint store over the same
            directory agree on the Monitor.
        followup: the injected follow-up hook (default None: the scan
            records the durable at-most-once decision without a
            follow-up receipt -- the default configuration never invokes
            an adapter).

    Raises:
        TypeError: ``state_dir`` is not a str/Path, or ``now`` or
            ``followup`` is not callable.
        TriggerError: an injected ``monitor_id`` is not a valid monitor
            id.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        now: MonitoringClock | None = None,
        monitor_id: str | None = None,
        followup: FollowupHook | None = None,
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
        if followup is not None and not callable(followup):
            raise TypeError(
                f"followup must be callable, got {type(followup).__name__}"
            )
        self._state_dir = Path(state_dir)
        self._now_fn = now if now is not None else utc_now
        if monitor_id is not None:
            if not isinstance(monitor_id, str) or not is_valid_id(
                monitor_id, MONITOR_ID_KIND
            ):
                raise TriggerError(
                    f"monitor_id {monitor_id!r} is not a valid monitor id"
                    " (sr_monitor_<32 hex chars>)"
                )
            self._monitor_id = monitor_id
        else:
            self._monitor_id = derive_monitor_id(self._state_dir)
        self._followup = followup
        self._trigger_dir = self._state_dir / TRIGGERED_STATE_DIR

    # -- identity and injected dependencies ---------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected state directory."""
        return self._state_dir

    @property
    def monitor_id(self) -> str:
        """The Monitor identity owning this registry."""
        return self._monitor_id

    @property
    def trigger_dir(self) -> Path:
        """The trigger-record directory (``<state_dir>/trigger/``)."""
        return self._trigger_dir

    @property
    def followup(self) -> FollowupHook | None:
        """The injected follow-up hook (None keeps the no-receipt
        default: the scan records the durable decision without a
        receipt)."""
        return self._followup

    # -- input validation and persistence -----------------------------------

    def _check_run(self, run: Run) -> None:
        if not isinstance(run, Run):
            raise TypeError(
                f"run must be a Run, got {type(run).__name__}"
            )
        if not is_valid_id(run.run_id, "run"):
            raise TriggerError(
                f"run id {run.run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )

    def _check_run_id(self, run_id: str) -> None:
        if not isinstance(run_id, str):
            raise TypeError(
                f"run_id must be a str, got {type(run_id).__name__}"
            )
        if not is_valid_id(run_id, "run"):
            raise TriggerError(
                f"run id {run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )

    def _record_path(self, run_id: str) -> Path:
        self._check_run_id(run_id)
        return self._trigger_dir / f"{run_id}.json"

    def _write_record(self, record: TriggerRecord) -> None:
        atomic_write(
            self._record_path(record.run_id),
            _canonical_json(record.to_dict()),
        )

    def _read_record(self, run_id: str) -> TriggerRecord:
        """Re-hydrate one trigger record from disk (the M1 recovery
        discipline: never trust session state).

        Raises:
            CorruptTriggerStateError: the record is missing or corrupt
                (unreadable file, malformed JSON, a record whose run id
                disagrees with its file name, or any record-contract
                violation).
        """
        path = self._record_path(run_id)
        if not path.is_file():
            if path.exists():
                raise CorruptTriggerStateError(
                    f"trigger record at {path} is not a regular file"
                )
            raise CorruptTriggerStateError(
                f"no trigger record for run {run_id!r} at {path}; the run"
                " was never triggered"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CorruptTriggerStateError(
                f"corrupt trigger record at {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise CorruptTriggerStateError(
                f"corrupt trigger record at {path}: expected a JSON object"
            )
        try:
            record = TriggerRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise CorruptTriggerStateError(
                f"corrupt trigger record at {path}: {exc}"
            ) from exc
        if record.run_id != run_id:
            raise CorruptTriggerStateError(
                f"corrupt trigger record at {path}: the record names run"
                f" {record.run_id!r}, not {run_id!r}"
            )
        return record

    # -- the trigger-set operations -----------------------------------------

    def scan(self, run: Run) -> ScanOutcome:
        """Scan one Run for its follow-up trigger (AC-01).

        A result-bearing Run (``RESULT_AVAILABLE`` or later) is triggered
        at most once: the first scan invokes the injected follow-up hook
        exactly once and persists the deterministic trigger record;
        every repeated scan of the same run resolves to that single
        original record and never invokes the hook again. Any other Run
        is observed and ignored -- never triggered, never fabricated.

        Args:
            run: the durable Run record to scan (as read from the run
                store).

        Returns:
            The :class:`ScanOutcome` of this scan.

        Raises:
            TypeError: ``run`` is not a ``Run``.
            TriggerError: ``run`` carries an invalid run id.
            CorruptTriggerStateError: an existing trigger record for the
                run is corrupt (corrupt state fails loudly, never
                silently -- it is never overwritten).
            Exception: the injected follow-up hook's exception, when the
                hook fails -- propagated loudly, nothing recorded, the
                decision stays re-issuable.
        """
        self._check_run(run)
        if run.lifecycle_state not in _RESULT_RECORDED_RUN_STATES:
            # Observed and ignored: the run's durable state records no
            # result, so a follow-up would be fabricated -- never.
            return ScanOutcome(run_id=run.run_id, record=None, ignored=True)
        existing = self._existing_record(run.run_id)
        if existing is not None:
            return ScanOutcome(
                run_id=run.run_id, record=existing, replayed=True
            )
        record = self._issue_trigger(run)
        return ScanOutcome(run_id=run.run_id, record=record, triggered=True)

    def trigger(self, run: Run) -> TriggerRecord:
        """Strictly issue the single follow-up trigger of a run.

        Idempotent exactly-once issuance: a run whose durable state
        records a result (``RESULT_AVAILABLE`` or later) is triggered at
        most once -- the first call invokes the injected follow-up hook
        and persists the deterministic record; later calls return that
        single original record.

        Args:
            run: the durable Run record to trigger (as read from the run
                store).

        Returns:
            The :class:`TriggerRecord` governing the run's follow-up
            (the freshly issued record or the single original one).

        Raises:
            TypeError: ``run`` is not a ``Run``.
            TriggerError: ``run`` carries an invalid run id.
            TriggerContractError: the run cannot carry a follow-up: its
                lifecycle state does not record a result (the trigger
                would be fabricated onto the run).
            CorruptTriggerStateError: an existing trigger record for the
                run is corrupt.
            Exception: the injected follow-up hook's exception, when the
                hook fails -- propagated loudly, nothing recorded, the
                decision stays re-issuable.
        """
        self._check_run(run)
        if run.lifecycle_state not in _RESULT_RECORDED_RUN_STATES:
            raise TriggerContractError(
                f"run {run.run_id!r} cannot carry a follow-up: its"
                f" lifecycle state {run.lifecycle_state.value!r} does not"
                " record a result (RESULT_AVAILABLE or later); triggering"
                " it would fabricate a follow-up onto the run"
            )
        existing = self._existing_record(run.run_id)
        if existing is not None:
            return existing
        return self._issue_trigger(run)

    def get(self, run_id: str) -> TriggerRecord | None:
        """Return the trigger record of one run (re-hydrated from disk),
        or None when the run was never triggered.

        Raises:
            TypeError: ``run_id`` is not a str.
            TriggerError: ``run_id`` is not a valid run id.
            CorruptTriggerStateError: the stored record is corrupt.
        """
        path = self._record_path(run_id)
        if not path.is_file():
            if path.exists():
                raise CorruptTriggerStateError(
                    f"trigger record at {path} is not a regular file"
                )
            return None
        return self._read_record(run_id)

    def list_triggered(self) -> tuple[TriggerRecord, ...]:
        """Return the full trigger set, reconstructed from the persisted
        records alone (AC-02), in sorted run-id order (deterministic).

        A corrupt record anywhere under ``<state_dir>/trigger/`` fails
        the whole reconstruction loudly (stable
        ``CorruptTriggerStateError``) -- the trigger directory is
        Monitor-owned and everything in it is a trigger record.
        """
        if not self._trigger_dir.is_dir():
            return ()
        records: list[TriggerRecord] = []
        for path in sorted(
            self._trigger_dir.glob("*.json"), key=lambda p: p.name
        ):
            records.append(self._read_record(path.stem))
        return tuple(sorted(records, key=lambda record: record.run_id))

    # -- internals ----------------------------------------------------------

    def _existing_record(self, run_id: str) -> TriggerRecord | None:
        """The durable trigger record of ``run_id``, or None when the
        run was never triggered (re-hydrated from disk -- never session
        state).

        Raises:
            CorruptTriggerStateError: the record path exists but is not
                a regular file (corrupt state fails loudly, never
                silently -- it is never overwritten).
        """
        path = self._record_path(run_id)
        if not path.is_file():
            if path.exists():
                raise CorruptTriggerStateError(
                    f"trigger record at {path} is not a regular file"
                )
            return None
        return self._read_record(run_id)

    def _issue_trigger(self, run: Run) -> TriggerRecord:
        """The AC-01 issuance for a result-bearing run: invoke the
        injected follow-up hook exactly once, then persist the
        deterministic trigger record (canonical sorted JSON through
        ``atomic_write``).

        The hook is invoked *before* the record is written, so a hook
        failure leaves nothing recorded and the decision stays
        re-issuable (the next scan retries the follow-up); the hook's
        exception propagates loudly and its message is never persisted.
        """
        followup_id = self._invoke_followup(run)
        record = TriggerRecord(
            record_id=generate_id(
                TRIGGER_ID_KIND, run.run_id, FOLLOWUP_TRIGGER_KIND
            ),
            run_id=run.run_id,
            trigger_kind=FOLLOWUP_TRIGGER_KIND,
            triggered_at=self._now_fn(),
            followup_id=followup_id,
        )
        self._write_record(record)
        return record

    def _invoke_followup(self, run: Run) -> str | None:
        """Invoke the injected follow-up hook for ``run`` and return the
        receipt (None when the hook returns none, or when no hook is
        injected).

        Raises:
            TypeError: the hook returned neither a str receipt nor None
                (a hook contract violation).
            TriggerError: the hook returned an empty receipt.
            Exception: any exception raised by the hook -- propagated
                loudly, nothing recorded, the decision stays re-issuable.
        """
        if self._followup is None:
            return None
        receipt = self._followup(run)
        if receipt is None:
            return None
        if not isinstance(receipt, str):
            raise TypeError(
                "the follow-up hook must return a str receipt or None, got"
                f" {type(receipt).__name__}"
            )
        if not receipt.strip():
            raise TriggerError(
                "the follow-up hook must return a non-empty receipt or"
                " None"
            )
        return receipt
