"""Deterministic Monitor session recovery / replacement (DEV-M8-G04,
deliverable).

The Execution Monitor is a project-persistent role with a
high-availability policy (``13-EXECUTION-MONITOR.md`` section 3): when
the original Monitor session disappears and resume fails, a replacement
Monitor session takes over. This module implements the replacement
contract as a deterministic procedure over the monitor's durable state
directory -- the replacement reconstructs the watch and reconciliation
state from the persisted state alone, with **no access to the original
session's conversation** (the M1 recovery discipline: never trust
session state).

Reconstruction (AC-01)
----------------------
:class:`MonitorRecovery` is a deterministic function/class over the
state directory. ``reconstruct()`` builds the replacement view
(:class:`RecoveryPlan`) from three durable sources:

* the watch set from the watched-Run registry files
  (``<state_dir>/watched/<run_id>.json``, DEV-M8-G01);
* the per-run reconciliation progress from the checkpoint
  (``<state_dir>/checkpoint.json``, DEV-M8-G01);
* the completion facts from the Run records (the persisted lifecycle
  state) and the append-only event log (the ``external_status_change``
  records, DEV-M8-G02).

``resume_engine()`` returns the resumable reconcile configuration -- a
:class:`ReconcileEngine` (DEV-M8-G02) bound to the same state directory
with the same injected clock, monitor identity, probe seam, run store
and event log. A **fresh recovery object** over the same state directory
returns the identical plan and the identical engine configuration: no
original-conversation artifact is ever needed (AC-01).

Observation-only reconstruction (AC-02)
---------------------------------------
Reconstruction is observation-only: ``reconstruct()`` never creates,
dispatches or resubmits external jobs, and it writes nothing -- the
durable state stays byte-identical across reconstruction. The
constructor accepts an optional ``dispatch`` hook, the seam through
which a replacement Monitor *session* would create external jobs; the
recovery procedure never invokes it (the tests prove zero dispatch
calls with an injected counter). Resuming reconciliation runs through
the DEV-M8-G02 ``ReconcileEngine``, whose exactly-once machinery
(deterministic event ids, idempotency keys, the completion checkpoint
marker) guarantees the replacement never re-transitions, never re-emits
and never re-dispatches anything already durably recorded (AC-02).

Outage reconciliation (AC-03)
-----------------------------
A completion that occurred while the original Monitor was down -- the
external state became ``RESULT_AVAILABLE`` during the outage, with no
event and no checkpoint recorded yet by the original Monitor -- is
reconciled by the replacement's first reconciliation pass:
``resume_engine().reconcile_all()`` probes the external truth, moves the
Run to ``RESULT_AVAILABLE`` through the real lifecycle transition
machinery, appends the single completion event under the deterministic
idempotency key and records the checkpoint progress -- exactly once
(AC-03).

Determinism, secrets, discipline
--------------------------------
All timestamps come from the injected clock (``now``; no wall clock in
the tested path); ids are generated with ``core.ids.generate_id``; the
recovery procedure writes nothing, and the resumed engine persists only
through the registry, the checkpoint store, the run store and the event
log (canonical sorted JSON through ``atomic_write``). Errors follow the
house paradigm: ``TypeError`` at public type boundaries, the stable
``MonitoringError`` (``ValueError`` subclass) hierarchy otherwise
(``RecoveryError`` -> ``CorruptRecoveryStateError`` for corrupt
reconstruction state, ``RecoveryContractError`` for contract
violations). Corrupt persisted state fails loudly, never silently. The
monitoring subsystem does not import from the adapters package: the
external ids are plain documented fields of the core ``RunExternal``
vocabulary, and the external-state vocabulary is mirrored as plain
documented constants.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import LifecycleState, Run, RunExternal
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    StateBackend,
)
from scientific_reproduction.monitoring.checkpoint import (
    CheckpointRecordError,
    MonitorCheckpointStore,
    MonitorRunCheckpoint,
)
from scientific_reproduction.monitoring.reconcile import (
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
    ExternalStateProbe,
    ReconcileEngine,
)
from scientific_reproduction.monitoring.registry import (
    MONITOR_ID_KIND,
    MonitoringClock,
    MonitoringError,
    WatchedRunRecord,
    WatchedRunRegistry,
    utc_now,
)

__all__ = [
    "CorruptRecoveryStateError",
    "ExternalDispatchHook",
    "MonitorRecovery",
    "RecoveredCompletion",
    "RecoveryContractError",
    "RecoveryError",
    "RecoveryPlan",
]

# ---------------------------------------------------------------------------
# Errors (stable MonitoringError subclasses)
# ---------------------------------------------------------------------------


class RecoveryError(MonitoringError):
    """Base error of the Monitor session recovery / replacement
    procedure."""


class CorruptRecoveryStateError(RecoveryError):
    """Raised when the durable state cannot be reconstructed: a corrupt
    watch entry, a corrupt checkpoint, a watched run with a missing or
    corrupt Run record, a corrupt event log, or internally contradictory
    durable state (e.g. more than one completion event for a run, or a
    completion event for a run whose Run record does not record a
    completion). Corrupt persisted state fails loudly, never silently."""


class RecoveryContractError(RecoveryError):
    """Raised when reconstruction hits a contract violation: a Run
    record whose external identity disagrees with its watch entry (the
    replacement must never resume polling an external run under a
    mismatched identity)."""


# ---------------------------------------------------------------------------
# The injected hooks (external vocabularies mirrored as plain documented
# constants -- the monitoring subsystem never imports the adapters package)
# ---------------------------------------------------------------------------

#: The injected external-job dispatch hook: the seam through which a
#: replacement Monitor *session* would create, dispatch or resubmit an
#: external job for the given external identity, returning the external
#: state observed after dispatch (the probe vocabulary). The recovery
#: procedure must NEVER invoke it: reconstruction is observation-only
#: (AC-02) -- the tests prove zero dispatch calls with an injected
#: counter.
ExternalDispatchHook: TypeAlias = Callable[[RunExternal], str]


# ---------------------------------------------------------------------------
# Run-lifecycle sets (mirror the reconcile engine's sets: RUNNING_EXTERNAL
# -> RESULT_AVAILABLE -> ANALYZING -> ... -- completion is durably recorded
# in the Run record from RESULT_AVAILABLE on)
# ---------------------------------------------------------------------------

#: Lifecycle states in which the Run record itself durably records the
#: external completion.
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
# The reconstructed records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveredCompletion:
    """The completion facts of one watched Run, reconstructed from the
    durable state alone (AC-01): the persisted Run record's lifecycle
    state, whether the completion event is recorded in the event log
    (with its deterministic event id and timestamp) and whether the
    checkpoint records the completion observation. The facts come from
    the Run records and the event log only -- never from any Monitor
    session state.
    """

    run_id: str
    run_state: LifecycleState
    event_id: str | None = None
    event_timestamp: str | None = None
    event_logged: bool = False
    checkpoint_records_completion: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                "RecoveredCompletion.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, "run"):
            raise RecoveryError(
                f"recovered completion run_id {self.run_id!r} is not a valid"
                " run id (sr_run_<32 hex chars>)"
            )
        if not isinstance(self.run_state, LifecycleState):
            raise TypeError(
                "RecoveredCompletion.run_state must be a LifecycleState, got"
                f" {type(self.run_state).__name__}"
            )
        if self.event_logged != (self.event_id is not None):
            raise RecoveryError(
                "RecoveredCompletion.event_logged must be True exactly when"
                " event_id is set"
            )
        if self.event_id is not None and not is_valid_id(self.event_id, "event"):
            raise RecoveryError(
                f"recovered completion event_id {self.event_id!r} is not a"
                " valid event id (sr_event_<32 hex chars>)"
            )
        if self.event_timestamp is not None and (
            not isinstance(self.event_timestamp, str)
            or not self.event_timestamp.strip()
        ):
            raise RecoveryError(
                "RecoveredCompletion.event_timestamp must be a non-empty"
                f" timestamp string when set, got {self.event_timestamp!r}"
            )
        if not isinstance(self.checkpoint_records_completion, bool):
            raise TypeError(
                "RecoveredCompletion.checkpoint_records_completion must be a"
                f" bool, got {type(self.checkpoint_records_completion).__name__}"
            )

    @property
    def completed(self) -> bool:
        """True iff the Run record durably records the completion (the
        run reached ``RESULT_AVAILABLE`` or moved past it)."""
        return self.run_state in _RESULT_RECORDED_RUN_STATES


@dataclass(frozen=True)
class RecoveryPlan:
    """The reconstructed replacement view (AC-01): the full watch set,
    the per-run checkpoint progress and one completion-fact record per
    watched run, all derived from the durable state alone. Every
    collection is in sorted run-id order (deterministic).
    """

    monitor_id: str
    watched: tuple[WatchedRunRecord, ...]
    progress: tuple[MonitorRunCheckpoint, ...]
    completions: tuple[RecoveredCompletion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.monitor_id, str) or not is_valid_id(
            self.monitor_id, MONITOR_ID_KIND
        ):
            raise RecoveryError(
                f"RecoveryPlan.monitor_id {self.monitor_id!r} is not a valid"
                " monitor id (sr_monitor_<32 hex chars>)"
            )
        for name, field_type in (
            ("watched", WatchedRunRecord),
            ("progress", MonitorRunCheckpoint),
            ("completions", RecoveredCompletion),
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                raise TypeError(
                    f"RecoveryPlan.{name} must be a tuple of"
                    f" {field_type.__name__} entries, got"
                    f" {type(value).__name__}"
                )
            for entry in value:
                if not isinstance(entry, field_type):
                    raise TypeError(
                        f"RecoveryPlan.{name} entries must be"
                        f" {field_type.__name__}, got {type(entry).__name__}"
                    )
            run_ids = [entry.run_id for entry in value]
            if run_ids != sorted(run_ids):
                raise RecoveryError(
                    f"RecoveryPlan.{name} must be sorted by run_id"
                    " (deterministic order)"
                )
            if len(set(run_ids)) != len(run_ids):
                raise RecoveryError(
                    f"RecoveryPlan.{name} must not repeat a run_id"
                )
        watched_ids = {entry.run_id for entry in self.watched}
        for completion in self.completions:
            if completion.run_id not in watched_ids:
                raise RecoveryError(
                    f"RecoveryPlan.completions references run"
                    f" {completion.run_id!r} which is not in the watched set"
                )


# ---------------------------------------------------------------------------
# The recovery / replacement procedure
# ---------------------------------------------------------------------------


class MonitorRecovery:
    """The deterministic Monitor session recovery / replacement
    procedure (DEV-M8-G04).

    ``reconstruct()`` rebuilds the replacement view from the durable
    state alone (AC-01) -- the watch set from the registry files, the
    per-run progress from the checkpoint, the completion facts from the
    Run records and the event log -- and is observation-only: it writes
    nothing and never invokes the injected ``dispatch`` hook (AC-02).
    ``resume_engine()`` returns the resumable reconcile configuration:
    a :class:`ReconcileEngine` (DEV-M8-G02) bound to the same state
    directory with the same injected clock, monitor identity, probe
    seam, run store and event log, so the replacement's first
    reconciliation pass reconciles completions that occurred during the
    outage exactly once (AC-03) -- the engine's exactly-once machinery
    is reused, never reimplemented.

    Args:
        state_dir: the monitor's durable state directory (watch entries
            at ``<state_dir>/watched/``, checkpoint at
            ``<state_dir>/checkpoint.json``).
        now: injectable clock producing a timestamp string (default
            ``utc_now``) -- no wall clock in the tested path.
        monitor_id: the Monitor identity (``sr_monitor_<32 hex>``).
            Defaults to the deterministic identity of the state
            directory.
        probe: the injected external-status probe the resumed engine
            polls with (default: the engine's default probe, which
            always reports unknown and can never fabricate completion).
        run_store: the durable Run store (default:
            ``FilesystemStateBackend(state_dir)`` -- runs at
            ``<state_dir>/runs/``, the canonical tree directory).
        event_log: the append-only event log (default:
            ``ProjectEventLog(state_dir)`` -- events at
            ``<state_dir>/events/``, the canonical tree directory).
        dispatch: the external-job dispatch hook of a replacement
            Monitor session. Reconstruction must never invoke it
            (AC-02); it is exposed only as the documented seam.

    Raises:
        TypeError: ``state_dir`` is not a str/Path, or ``now``,
            ``probe`` or ``dispatch`` is not callable, or ``run_store``
            is not a ``StateBackend``, or ``event_log`` is not a
            ``ProjectEventLog``.
        MonitoringError: an injected ``monitor_id`` is not a valid
            monitor id.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        now: MonitoringClock | None = None,
        monitor_id: str | None = None,
        probe: ExternalStateProbe | None = None,
        run_store: StateBackend | None = None,
        event_log: ProjectEventLog | None = None,
        dispatch: ExternalDispatchHook | None = None,
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
        if probe is not None and not callable(probe):
            raise TypeError(
                f"probe must be callable, got {type(probe).__name__}"
            )
        if dispatch is not None and not callable(dispatch):
            raise TypeError(
                f"dispatch must be callable, got {type(dispatch).__name__}"
            )
        if run_store is not None and not isinstance(run_store, StateBackend):
            raise TypeError(
                "run_store must be a StateBackend, got"
                f" {type(run_store).__name__}"
            )
        if event_log is not None and not isinstance(event_log, ProjectEventLog):
            raise TypeError(
                "event_log must be a ProjectEventLog, got"
                f" {type(event_log).__name__}"
            )
        self._state_dir = Path(state_dir)
        self._now_fn = now if now is not None else utc_now
        self._probe = probe
        self._dispatch = dispatch
        # The registry validates the injected monitor_id (stable
        # MonitoringError); the checkpoint store is bound to the same
        # identity so both always agree on who the Monitor is.
        self._registry = WatchedRunRegistry(
            self._state_dir,
            now=self._now_fn,
            monitor_id=monitor_id,
        )
        self._monitor_id = self._registry.monitor_id
        self._checkpoint_store = MonitorCheckpointStore(
            self._state_dir,
            now=self._now_fn,
            monitor_id=self._monitor_id,
        )
        self._run_store = (
            run_store
            if run_store is not None
            else FilesystemStateBackend(self._state_dir)
        )
        self._event_log = (
            event_log
            if event_log is not None
            else ProjectEventLog(self._state_dir)
        )

    # -- identity and injected dependencies ---------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected monitor state directory."""
        return self._state_dir

    @property
    def monitor_id(self) -> str:
        """The Monitor identity this recovery procedure resumes."""
        return self._monitor_id

    @property
    def registry(self) -> WatchedRunRegistry:
        """The watched-Run registry of this recovery procedure."""
        return self._registry

    @property
    def checkpoint_store(self) -> MonitorCheckpointStore:
        """The checkpoint store of this recovery procedure."""
        return self._checkpoint_store

    @property
    def probe(self) -> ExternalStateProbe | None:
        """The injected external-status probe of the resumed engine
        (None keeps the engine's default probe, which never fabricates
        completion)."""
        return self._probe

    @property
    def run_store(self) -> StateBackend:
        """The durable Run store the resumed engine reconciles
        through."""
        return self._run_store

    @property
    def event_log(self) -> ProjectEventLog:
        """The event log the resumed engine appends transition events
        to."""
        return self._event_log

    @property
    def dispatch(self) -> ExternalDispatchHook | None:
        """The injected external-job dispatch hook of a replacement
        Monitor session. Reconstruction must never invoke it (AC-02):
        the recovery procedure is observation-only."""
        return self._dispatch

    # -- reconstruction -----------------------------------------------------

    def reconstruct(self) -> RecoveryPlan:
        """Reconstruct the replacement view from the durable state alone
        (AC-01): the watch set from the registry files, the per-run
        progress from the checkpoint and the completion facts from the
        Run records and the event log. A **fresh recovery object** over
        the same state directory returns the identical plan -- no
        original-conversation artifact is involved.

        Observation-only (AC-02): this procedure writes nothing and
        never invokes the ``dispatch`` hook.

        Raises:
            CorruptRecoveryStateError: the durable state cannot be
                reconstructed (corrupt watch entry, corrupt checkpoint,
                a watched run with a missing/corrupt Run record, a
                corrupt event log, or internally contradictory
                completion state).
            RecoveryContractError: a Run record's external identity
                disagrees with its watch entry.
        """
        try:
            watched = self._registry.list_watched()
        except MonitoringError as exc:
            raise CorruptRecoveryStateError(
                "corrupt reconstruction state: the watched-Run registry"
                f" cannot be read: {exc}"
            ) from exc
        try:
            checkpoint = self._checkpoint_store.load()
        except CheckpointRecordError as exc:
            raise CorruptRecoveryStateError(
                "corrupt reconstruction state: the checkpoint cannot be"
                f" read: {exc}"
            ) from exc
        progress = checkpoint.entries if checkpoint is not None else ()
        try:
            records = self._event_log.list_events()
        except (ValueError, TypeError) as exc:
            raise CorruptRecoveryStateError(
                "corrupt reconstruction state: the event log cannot be"
                f" read: {exc}"
            ) from exc
        # The completion events of the append-only log, per run: the
        # log's idempotency machinery guarantees at most one per run --
        # more than one is internally contradictory durable state.
        completion_records_by_run: dict[str, list[EventRecord]] = {}
        for record in records:
            event = record.event
            if (
                event.event_type == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
                and event.to == EXTERNAL_STATE_RESULT_AVAILABLE
                and event.run_id is not None
            ):
                completion_records_by_run.setdefault(event.run_id, []).append(
                    record
                )
        progress_by_run = {entry.run_id: entry for entry in progress}
        completions: list[RecoveredCompletion] = []
        for watch in watched:
            run = self._read_run(watch.run_id)
            self._check_external_identity(watch, run)
            completion_records = completion_records_by_run.get(
                watch.run_id, ()
            )
            if len(completion_records) > 1:
                raise CorruptRecoveryStateError(
                    "corrupt reconstruction state: more than one completion"
                    f" event recorded for run {watch.run_id!r}; the"
                    " append-only event log can only ever hold one"
                )
            event_record = completion_records[0] if completion_records else None
            if (
                event_record is not None
                and run.lifecycle_state not in _RESULT_RECORDED_RUN_STATES
            ):
                raise CorruptRecoveryStateError(
                    f"corrupt reconstruction state: run {watch.run_id!r} has a"
                    " completion event in the event log but its Run record is"
                    f" at {run.lifecycle_state.value!r}; the durable state is"
                    " internally contradictory"
                )
            entry = progress_by_run.get(watch.run_id)
            checkpoint_records_completion = (
                entry is not None
                and entry.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
                and entry.reconciled_at is not None
            )
            completions.append(
                RecoveredCompletion(
                    run_id=watch.run_id,
                    run_state=run.lifecycle_state,
                    event_id=(
                        event_record.event.event_id
                        if event_record is not None
                        else None
                    ),
                    event_timestamp=(
                        event_record.event.timestamp
                        if event_record is not None
                        else None
                    ),
                    event_logged=event_record is not None,
                    checkpoint_records_completion=(
                        checkpoint_records_completion
                    ),
                )
            )
        return RecoveryPlan(
            monitor_id=self._monitor_id,
            watched=watched,
            progress=progress,
            completions=tuple(completions),
        )

    # -- the resumable reconcile configuration (AC-01/AC-03) -----------------

    def resume_engine(self) -> ReconcileEngine:
        """Return the resumable reconcile configuration: a
        :class:`ReconcileEngine` (DEV-M8-G02) bound to the same state
        directory with the same injected clock, monitor identity, probe
        seam, run store and event log. The replacement Monitor's first
        reconciliation pass runs through this engine, which reconciles
        completions that occurred during the outage exactly once (AC-03)
        and never re-emits anything already durably recorded (AC-02) --
        its exactly-once machinery is reused, never reimplemented.

        A fresh engine is returned on every call (the M1 recovery
        discipline: never share session state between passes).
        """
        return ReconcileEngine(
            self._state_dir,
            now=self._now_fn,
            monitor_id=self._monitor_id,
            probe=self._probe,
            run_store=self._run_store,
            event_log=self._event_log,
        )

    # -- internals ----------------------------------------------------------

    def _read_run(self, run_id: str) -> Run:
        """Read the durable Run record through the injected run store.

        Raises:
            CorruptRecoveryStateError: the run record is missing or
                corrupt (a watched run with no reconstructable Run
                record is corrupt reconstruction state).
        """
        try:
            data = self._run_store.read("run", run_id)
            return Run.from_dict(data)
        except (FileNotFoundError, ValueError, TypeError) as exc:
            raise CorruptRecoveryStateError(
                f"corrupt reconstruction state for run {run_id!r}: its"
                f" durable Run record cannot be read: {exc}"
            ) from exc

    def _check_external_identity(
        self, watch: WatchedRunRecord, run: Run
    ) -> None:
        """The Run record's external identity, when it names one, must
        agree with the watch entry's: a replacement Monitor must never
        resume polling an external run under a mismatched identity.

        Raises:
            RecoveryContractError: the identities disagree.
        """
        run_external = run.external
        if run_external is None:
            # The watch entry is the monitor's durable identity; the run
            # record carries none -- nothing to disagree with.
            return
        watch_external = watch.external
        if run_external.backend != watch_external.backend:
            raise RecoveryContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record backend {run_external.backend!r} vs"
                f" watch backend {watch_external.backend!r}; the replacement"
                " Monitor refuses to resume polling an external run under a"
                " mismatched identity"
            )
        if (
            run_external.job_id is not None
            and run_external.job_id != watch_external.job_id
        ):
            raise RecoveryContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record job_id {run_external.job_id!r} vs"
                f" watch job_id {watch_external.job_id!r}"
            )
        if (
            run_external.dispatch_id is not None
            and run_external.dispatch_id != watch_external.dispatch_id
        ):
            raise RecoveryContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record dispatch_id"
                f" {run_external.dispatch_id!r} vs watch dispatch_id"
                f" {watch_external.dispatch_id!r}"
            )
