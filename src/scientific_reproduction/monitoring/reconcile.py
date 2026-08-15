"""Deterministic external-status reconciliation and transition-event
emission (DEV-M8-G02, deliverable).

The Execution Monitor reconciles every watched external Run against the
external truth reported by its adapter. This module implements that
reconciliation as a deterministic primitive: given the durable monitor
state directory (the watched-Run registry and the checkpoint store from
DEV-M8-G01), the durable Run store, the append-only event log and an
injected external-status probe, it observes each watched run's external
state, moves completed runs to ``RESULT_AVAILABLE`` through the real
lifecycle transition machinery, appends the transition event through the
real event log, and persists the observation as checkpoint progress --
so a Monitor restart reconstructs reconciliation progress from the
durable state alone.

Adapter coupling
----------------
The monitoring subsystem never imports the adapters package (locked by
``tests/monitoring/test_monitoring_surface.py``): the external state is
a plain string vocabulary mirrored from the adapters'
``DispatchState``/``JobState`` reports (``RUNNING_EXTERNAL`` /
``RESULT_AVAILABLE``), injected through the :class:`ExternalStateProbe`
callable. ``reconcile.py`` defines its own constants for that vocabulary
and never imports ``scientific_reproduction.adapters``; tests inject
fake probes.

AC-01 -- external completion moves the Run to RESULT_AVAILABLE exactly
once
----------------------------------------------------------------------
When the probe reports the completion signal
(``EXTERNAL_STATE_RESULT_AVAILABLE``, the only member of
``COMPLETION_SIGNALS``) for a run whose persisted lifecycle state is
``RUNNING_EXTERNAL``, the engine:

1. transitions the Run record to ``RESULT_AVAILABLE`` through
   ``core.transitions.transition`` and persists it through the injected
   run store (the schema-validating, atomic state backend -- the real
   transition machinery, never reimplemented);
2. appends one ``external_status_change`` transition event (the event
   vocabulary of ``13-EXECUTION-MONITOR.md`` section 5) through the real
   append-only ``ProjectEventLog``, under a deterministic idempotency
   key -- so re-submission can never create a duplicate record and the
   log's sequence never advances twice for the same completion;
3. persists the observation in the checkpoint (``observed_state`` /
   ``observed_at`` / ``reconciled_at``), the durable "completion was
   recorded" marker.

A crash between any two steps converges on the same single completion
on restart: a Run record already at ``RESULT_AVAILABLE`` is never
transitioned again, and the idempotency claim of the event log makes the
re-append of the same deterministic event a no-op replay. Re-reconciling
the same progress therefore never re-transitions and never re-emits.

AC-02 -- unknown/temporary adapter state never fabricates completion
-------------------------------------------------------------------
Only an exact match of the probe's return value against
``COMPLETION_SIGNALS`` (``"RESULT_AVAILABLE"``) can trigger the
transition. Every other probe outcome -- ``EXTERNAL_STATE_UNKNOWN``,
``EXTERNAL_STATE_UNAVAILABLE``, a probe exception (transient backend
failure) or any unrecognized backend-specific string -- is *observed*
and *recorded* in the checkpoint but never treated as completion: the
Run stays in its state and no transition event is appended. With no
probe injected, the engine defaults to a probe that always reports
unknown, so the default configuration can never fabricate completion.

AC-03 -- reconciliation is idempotent across restart
----------------------------------------------------
A fresh :class:`ReconcileEngine` over the same state directory and the
same injected run store / event log reconstructs the watch set from the
registry entries, the per-run progress from the checkpoint and the
completion facts from the Run records and the event log. Re-reconciling
the same external runs yields identical durable bytes (Run records,
checkpoint, event records, watch entries -- all canonical sorted JSON
through ``atomic_write``) and identical outcomes for identical inputs.

Determinism and discipline
--------------------------
All timestamps come from the injected clock (``now``); ids are
generated deterministically with ``core.ids.generate_id`` (event ids
are pure functions of the transition); the event reason and the
idempotency key are stable documented constants. The engine persists
nothing itself -- every write goes through the injected/derived
registry, checkpoint store, run store and event log. Errors follow the
house paradigm: ``TypeError`` at public type boundaries, stable
``MonitoringError`` subclasses otherwise (``ReconcileContractError``
for lifecycle/identity contract violations, ``CorruptProgressError``
for corrupt progress state). No credentials are ever persisted: probe
exception messages are never recorded, only the normalized state
vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias, cast

from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LifecycleState,
    ProjectEvent,
    Run,
    RunExternal,
)
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    StateBackend,
)
from scientific_reproduction.core.transitions import transition
from scientific_reproduction.monitoring.checkpoint import (
    MonitorCheckpoint,
    MonitorCheckpointStore,
    MonitorRunCheckpoint,
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
    "COMPLETION_SIGNALS",
    "CorruptProgressError",
    "EXTERNAL_COMPLETION_REASON",
    "EXTERNAL_STATUS_CHANGE_EVENT_TYPE",
    "EXTERNAL_STATE_RESULT_AVAILABLE",
    "EXTERNAL_STATE_RUNNING",
    "EXTERNAL_STATE_UNAVAILABLE",
    "EXTERNAL_STATE_UNKNOWN",
    "ExternalStateProbe",
    "RECONCILE_ACTOR",
    "RECONCILE_COMPLETION_KEY_PREFIX",
    "ReconcileContractError",
    "ReconcileEngine",
    "ReconcileError",
    "ReconcileOutcome",
    "ReconcileSummary",
]

# ---------------------------------------------------------------------------
# The external-state vocabulary (mirrors the adapters' reports; the
# monitoring subsystem never imports the adapters package)
# ---------------------------------------------------------------------------

#: The external state of a dispatched run still executing remotely
#: (mirrors the ``RUNNING_EXTERNAL`` report of the adapters).
EXTERNAL_STATE_RUNNING: str = "RUNNING_EXTERNAL"

#: The external completion signal: the adapter reports the external run
#: finished and its result is available. This is the **only** probe
#: outcome that can move the Run to ``RESULT_AVAILABLE`` (AC-01/AC-02).
EXTERNAL_STATE_RESULT_AVAILABLE: str = "RESULT_AVAILABLE"

#: The probe reports it cannot determine the external state (unknown
#: status): observed and recorded, never treated as completion (AC-02).
EXTERNAL_STATE_UNKNOWN: str = "UNKNOWN"

#: The probe reports the external state is temporarily unavailable (a
#: transient backend failure): observed and recorded, never treated as
#: completion (AC-02).
EXTERNAL_STATE_UNAVAILABLE: str = "TEMPORARY_UNAVAILABLE"

#: The explicit completion signal set. Completion is never fabricated:
#: only an exact probe return of ``EXTERNAL_STATE_RESULT_AVAILABLE``
#: triggers the transition (AC-02) -- any other string (unknown,
#: temporary, or an unrecognized backend-specific value) is a
#: non-completion observation.
COMPLETION_SIGNALS: frozenset[str] = frozenset(
    {EXTERNAL_STATE_RESULT_AVAILABLE}
)

# ---------------------------------------------------------------------------
# The transition-event vocabulary (13-EXECUTION-MONITOR.md section 5)
# ---------------------------------------------------------------------------

#: Actor of the transition events appended by reconciliation: the
#: Execution Monitor (the normative event example's actor).
RECONCILE_ACTOR: str = "execution-monitor"

#: Event type of a detected external status change (the normative event
#: example's ``event_type``); the event carries ``from``/``to`` and
#: ``run_id``.
EXTERNAL_STATUS_CHANGE_EVENT_TYPE: str = "external_status_change"

#: Stable reason of the completion transition event: the monitor
#: observed the external completion signal (it never guesses *why* the
#: external run finished -- that would be scientific interpretation).
EXTERNAL_COMPLETION_REASON: str = "external_completion_observed"

#: Prefix of the deterministic idempotency key under which the
#: completion event is appended (``reconcile.completed:<run_id>``): the
#: event log resolves re-submissions to the single original record, so
#: the completion event is emitted exactly once even across a restart
#: or a crash between steps (AC-01/AC-03).
RECONCILE_COMPLETION_KEY_PREFIX: str = "reconcile.completed"


# ---------------------------------------------------------------------------
# Errors (stable MonitoringError subclasses)
# ---------------------------------------------------------------------------


class ReconcileError(MonitoringError):
    """Base error of the reconciliation primitive."""


class ReconcileContractError(ReconcileError):
    """Raised when reconciliation hits a contract violation: the probe
    reports external completion for a run whose persisted lifecycle
    cannot record it (pre-external states ``CREATED``/``READY``/
    ``DISPATCHED``, or ``CANCELLED`` -- completion must never be
    fabricated onto such a run), or the Run record's external identity
    disagrees with the watch entry's (the monitor would be polling the
    wrong external run)."""


class CorruptProgressError(ReconcileError):
    """Raised when reconciliation progress state is corrupt: a watch
    entry references a run with no run record in the run store, or the
    stored run record cannot be read/parsed. Corrupt persisted state
    fails loudly, never silently."""


# ---------------------------------------------------------------------------
# The injected probe
# ---------------------------------------------------------------------------

#: The injected external-status probe: a callable taking the external
#: identity (a ``RunExternal``) of a watched run and returning the
#: external state vocabulary string (one of the ``EXTERNAL_STATE_*``
#: constants, or any backend-specific string -- only
#: ``EXTERNAL_STATE_RESULT_AVAILABLE`` is a completion signal). A probe
#: raising an exception is a transient probe failure: it is observed as
#: ``EXTERNAL_STATE_UNAVAILABLE`` and never treated as completion.
ExternalStateProbe: TypeAlias = Callable[[RunExternal], str]


def _unknown_probe(_external: RunExternal) -> str:
    """The default probe: always reports unknown.

    With no probe injected the engine can never fabricate completion
    (AC-02): the default configuration observes ``UNKNOWN`` and records
    it, and never moves any run.
    """
    return EXTERNAL_STATE_UNKNOWN


# ---------------------------------------------------------------------------
# Run-lifecycle sets (mirror the normative mainline of
# core/rules/lifecycle.py: RUNNING_EXTERNAL -> RESULT_AVAILABLE -> ...)
# ---------------------------------------------------------------------------

#: Lifecycle states that are strictly *before* the run was handed off to
#: an external backend: an external completion signal observed for a run
#: in one of these states is a contract violation.
_PRE_EXTERNAL_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.CREATED,
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
    }
)

#: Lifecycle states in which the completion is already durably recorded:
#: the run reached ``RESULT_AVAILABLE`` (or moved past it). Observing
#: the external completion signal for such a run is the steady-state
#: re-poll: never re-transition, never re-emit (AC-01/AC-03).
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
# The reconciliation outcome records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileOutcome:
    """The outcome of reconciling one watched external Run.

    Attributes:
        run_id: the reconciled run.
        observed_state: the normalized observation -- the probe's return
            value, or ``EXTERNAL_STATE_UNAVAILABLE`` when the probe
            failed transiently. Never a fabricated completion.
        observed_at: the injected clock stamp of the observation.
        completed: True iff this reconcile pass **performed** the
            transition of the Run to ``RESULT_AVAILABLE`` (the AC-01
            exactly-once transition). Re-observing an already-completed
            run is ``completed=False``.
        transitioned_at: the injected clock stamp of the transition, or
            None when no transition happened this pass.
    """

    run_id: str
    observed_state: str
    observed_at: str
    completed: bool = False
    transitioned_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                f"ReconcileOutcome.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not isinstance(self.observed_state, str) or not self.observed_state:
            raise ReconcileError(
                "ReconcileOutcome.observed_state must be a non-empty string,"
                f" got {self.observed_state!r}"
            )
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ReconcileError(
                "ReconcileOutcome.observed_at must be a non-empty timestamp"
                f" string, got {self.observed_at!r}"
            )
        if not isinstance(self.completed, bool):
            raise TypeError(
                "ReconcileOutcome.completed must be a bool, got"
                f" {type(self.completed).__name__}"
            )
        if self.transitioned_at is not None and (
            not isinstance(self.transitioned_at, str)
            or not self.transitioned_at.strip()
        ):
            raise ReconcileError(
                "ReconcileOutcome.transitioned_at must be a non-empty string"
                f" when set, got {self.transitioned_at!r}"
            )
        if self.completed and self.transitioned_at is None:
            raise ReconcileError(
                "ReconcileOutcome.completed requires transitioned_at to be"
                " set (a performed transition is always stamped)"
            )


@dataclass(frozen=True)
class ReconcileSummary:
    """The outcome of reconciling the full watch set.

    ``outcomes`` is the per-run outcomes in sorted run-id order
    (deterministic); ``completed_count`` is the number of runs that
    performed the completion transition during this pass.
    """

    monitor_id: str
    reconciled_at: str
    outcomes: tuple[ReconcileOutcome, ...] = ()
    completed_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.monitor_id, str) or not is_valid_id(
            self.monitor_id, MONITOR_ID_KIND
        ):
            raise ReconcileError(
                f"ReconcileSummary.monitor_id {self.monitor_id!r} is not a"
                " valid monitor id (sr_monitor_<32 hex chars>)"
            )
        if not isinstance(self.reconciled_at, str) or not self.reconciled_at:
            raise ReconcileError(
                "ReconcileSummary.reconciled_at must be a non-empty timestamp"
                f" string, got {self.reconciled_at!r}"
            )
        if not isinstance(self.outcomes, tuple):
            raise TypeError(
                "ReconcileSummary.outcomes must be a tuple of"
                f" ReconcileOutcome entries, got {type(self.outcomes).__name__}"
            )
        for outcome in self.outcomes:
            if not isinstance(outcome, ReconcileOutcome):
                raise TypeError(
                    "ReconcileSummary.outcomes entries must be"
                    f" ReconcileOutcome, got {type(outcome).__name__}"
                )
        if isinstance(self.completed_count, bool) or not isinstance(
            self.completed_count, int
        ):
            raise TypeError(
                "ReconcileSummary.completed_count must be an int, got"
                f" {type(self.completed_count).__name__}"
            )
        run_ids = [outcome.run_id for outcome in self.outcomes]
        if run_ids != sorted(run_ids):
            raise ReconcileError(
                "ReconcileSummary.outcomes must be sorted by run_id"
                " (deterministic order)"
            )
        if self.completed_count != sum(1 for o in self.outcomes if o.completed):
            raise ReconcileError(
                "ReconcileSummary.completed_count must equal the number of"
                " completed outcomes"
            )


# ---------------------------------------------------------------------------
# The reconciliation engine
# ---------------------------------------------------------------------------


class ReconcileEngine:
    """Deterministic reconciliation of the watched external Runs.

    Reconciles every run of the watched-Run registry (DEV-M8-G01)
    against the injected external-status probe, using the durable Run
    store and the append-only event log, and persists the progress in
    the monitor checkpoint (DEV-M8-G01):

    * AC-01: an external completion signal moves the Run to
      ``RESULT_AVAILABLE`` through the real transition machinery and
      appends one ``external_status_change`` event under a deterministic
      idempotency key -- re-reconciling the same progress never
      re-transitions and never re-emits.
    * AC-02: every non-completion probe outcome (unknown, temporarily
      unavailable, transient probe failure, unrecognized string) is
      observed and recorded in the checkpoint, never treated as
      completion.
    * AC-03: a **fresh engine** over the same state directory and the
      same injected run store / event log reconstructs progress from the
      durable state alone; re-reconciling the same external runs yields
      identical durable bytes and identical outcomes.

    The engine persists nothing itself: every write goes through the
    registry, the checkpoint store, the injected run store and the
    injected event log.

    Args:
        state_dir: the monitor's durable state directory (watch entries
            at ``<state_dir>/watched/``, checkpoint at
            ``<state_dir>/checkpoint.json``).
        now: injectable clock producing a timestamp string (default
            ``utc_now``) -- no wall clock in the tested path.
        monitor_id: the Monitor identity (``sr_monitor_<32 hex>``).
            Defaults to the deterministic identity of the state
            directory.
        probe: the injected external-status probe (default: a probe that
            always reports ``EXTERNAL_STATE_UNKNOWN``, so the default
            configuration can never fabricate completion).
        run_store: the durable Run store the engine reads and
            transitions Run records through (default:
            ``FilesystemStateBackend(state_dir)`` -- runs at
            ``<state_dir>/runs/``, the canonical tree directory).
        event_log: the append-only event log the engine appends
            transition events to (default: ``ProjectEventLog(state_dir)``
            -- events at ``<state_dir>/events/``, the canonical tree
            directory).

    Raises:
        TypeError: ``state_dir`` is not a str/Path, ``now`` or ``probe``
            is not callable, ``run_store`` is not a ``StateBackend``, or
            ``event_log`` is not a ``ProjectEventLog``.
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
        self._probe = probe if probe is not None else _unknown_probe
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
            run_store if run_store is not None else FilesystemStateBackend(
                self._state_dir
            )
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
        """The Monitor identity owning this engine."""
        return self._monitor_id

    @property
    def registry(self) -> WatchedRunRegistry:
        """The watched-Run registry of this engine."""
        return self._registry

    @property
    def checkpoint_store(self) -> MonitorCheckpointStore:
        """The checkpoint store of this engine."""
        return self._checkpoint_store

    @property
    def probe(self) -> ExternalStateProbe:
        """The injected external-status probe."""
        return self._probe

    @property
    def run_store(self) -> StateBackend:
        """The durable Run store this engine reconciles through."""
        return self._run_store

    @property
    def event_log(self) -> ProjectEventLog:
        """The event log this engine appends transition events to."""
        return self._event_log

    # -- reconciliation -----------------------------------------------------

    def reconcile(self, run_id: str) -> ReconcileOutcome:
        """Reconcile one watched external Run and persist the progress.

        The observation is recorded in the checkpoint (AC-03 progress);
        when the probe reports the completion signal (AC-01) the Run is
        moved to ``RESULT_AVAILABLE`` through the real transition
        machinery and exactly one transition event is appended under the
        deterministic idempotency key. Unknown/temporary probe outcomes
        are recorded but never treated as completion (AC-02).

        Args:
            run_id: the watched run to reconcile.

        Returns:
            The :class:`ReconcileOutcome` of this pass.

        Raises:
            TypeError: ``run_id`` is not a str.
            WatchNotFoundError: the run is not watched.
            ReconcileContractError: the probe reports completion for a
                run whose lifecycle cannot record it (pre-external or
                ``CANCELLED``), or the Run record's external identity
                disagrees with the watch entry.
            CorruptProgressError: the run record is missing or corrupt.
        """
        if not isinstance(run_id, str):
            raise TypeError(
                f"run_id must be a str, got {type(run_id).__name__}"
            )
        watch = self._registry.get(run_id)
        stamp = self._now_fn()
        observed = self._observe(watch)
        run = self._read_run(run_id)
        self._check_external_identity(watch, run)

        completed = False
        transitioned_at: str | None = None
        if observed in COMPLETION_SIGNALS:
            run_state = run.lifecycle_state
            if run_state is LifecycleState.RUNNING_EXTERNAL:
                self._record_completion(run, watch, stamp)
                completed = True
                transitioned_at = stamp
            elif run_state in _RESULT_RECORDED_RUN_STATES:
                if not self._checkpoint_records_completion(run_id):
                    # Crash window between the Run write and the
                    # event/checkpoint bookkeeping: the idempotent
                    # re-append converges to the single completion record
                    # (AC-01/AC-03).
                    self._complete_bookkeeping(run_id, watch, stamp)
                else:
                    # Completion already durably recorded (AC-01/AC-03):
                    # the steady-state re-poll refreshes the observation
                    # only -- the event log is never re-emitted.
                    self._update_checkpoint(
                        run_id,
                        watch,
                        observed_state=observed,
                        observed_at=stamp,
                    )
            else:
                raise ReconcileContractError(
                    f"external completion observed for run {run_id!r} whose"
                    f" lifecycle state {run_state.value!r} cannot record a"
                    f" completion (expected RUNNING_EXTERNAL or a"
                    " result-bearing state); reconciliation never fabricates"
                    " a completion onto this run"
                )
        else:
            # AC-02: unknown/temporary/transient probe outcomes are
            # observed and recorded, never treated as completion.
            self._update_checkpoint(
                run_id,
                watch,
                observed_state=observed,
                observed_at=stamp,
            )
        return ReconcileOutcome(
            run_id=run_id,
            observed_state=observed,
            observed_at=stamp,
            completed=completed,
            transitioned_at=transitioned_at,
        )

    def reconcile_all(self) -> ReconcileSummary:
        """Reconcile every watched run (sorted run-id order) and return
        the summary. A contract violation or corrupt progress anywhere
        in the watch set fails the whole pass loudly (deterministic
        sorted order, deterministic error)."""
        outcomes = tuple(
            self.reconcile(record.run_id)
            for record in self._registry.list_watched()
        )
        return ReconcileSummary(
            monitor_id=self._monitor_id,
            reconciled_at=self._now_fn(),
            outcomes=outcomes,
            completed_count=sum(1 for o in outcomes if o.completed),
        )

    # -- internals ----------------------------------------------------------

    def _observe(self, watch: WatchedRunRecord) -> str:
        """Probe the external state of ``watch``.

        A probe exception is a transient probe failure: it carries no
        state information and is observed as
        ``EXTERNAL_STATE_UNAVAILABLE`` (AC-02) -- its message is never
        recorded (no secrets in persisted state). A non-str return is a
        probe contract violation and fails loudly.
        """
        try:
            observed = self._probe(watch.external)
        except Exception:
            return EXTERNAL_STATE_UNAVAILABLE
        if not isinstance(observed, str):
            raise TypeError(
                "the external-status probe must return a str state, got"
                f" {type(observed).__name__}"
            )
        return observed

    def _read_run(self, run_id: str) -> Run:
        """Read the durable Run record through the injected run store.

        Raises:
            CorruptProgressError: the run record is missing or corrupt.
        """
        try:
            data = self._run_store.read("run", run_id)
            return Run.from_dict(data)
        except (FileNotFoundError, ValueError) as exc:
            raise CorruptProgressError(
                f"corrupt reconciliation progress for run {run_id!r}: the"
                f" watch entry references a run record that cannot be read:"
                f" {exc}"
            ) from exc

    def _check_external_identity(
        self, watch: WatchedRunRecord, run: Run
    ) -> None:
        """The Run record's external identity, when it names one, must
        agree with the watch entry's (the monitor must not poll a run
        under a different external identity than its durable record
        claims).

        Raises:
            ReconcileContractError: the identities disagree.
        """
        run_external = run.external
        if run_external is None:
            # The watch entry is the monitor's durable identity; the run
            # record carries none -- nothing to disagree with.
            return
        watch_external = watch.external
        if run_external.backend != watch_external.backend:
            raise ReconcileContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record backend {run_external.backend!r} vs"
                f" watch backend {watch_external.backend!r}; reconciliation"
                " refuses to poll an external run under a mismatched identity"
            )
        if (
            run_external.job_id is not None
            and run_external.job_id != watch_external.job_id
        ):
            raise ReconcileContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record job_id {run_external.job_id!r} vs"
                f" watch job_id {watch_external.job_id!r}"
            )
        if (
            run_external.dispatch_id is not None
            and run_external.dispatch_id != watch_external.dispatch_id
        ):
            raise ReconcileContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record dispatch_id"
                f" {run_external.dispatch_id!r} vs watch dispatch_id"
                f" {watch_external.dispatch_id!r}"
            )

    def _record_completion(
        self, run: Run, watch: WatchedRunRecord, stamp: str
    ) -> None:
        """The AC-01 completion sequence for a run still at
        ``RUNNING_EXTERNAL``: transition the Run record through the real
        transition machinery, append the transition event under the
        deterministic idempotency key, and persist the checkpoint
        progress -- in that order, so a crash between any two steps
        converges to a single completion on the next reconcile."""
        new_state = cast(
            LifecycleState,
            transition(run.lifecycle_state, LifecycleState.RESULT_AVAILABLE),
        )
        updated_run = replace(
            run, lifecycle_state=new_state, updated_at=stamp
        )
        self._run_store.write("run", run.run_id, updated_run.to_dict())
        self._complete_bookkeeping(run.run_id, watch, stamp)

    def _complete_bookkeeping(
        self, run_id: str, watch: WatchedRunRecord, stamp: str
    ) -> None:
        """Append the completion event (idempotent on the deterministic
        key, so re-submission after a crash returns the single original
        record -- exactly-once) and persist the checkpoint progress."""
        self._event_log.append(
            self._completion_event(run_id, watch, stamp),
            idempotency_key=f"{RECONCILE_COMPLETION_KEY_PREFIX}:{run_id}",
        )
        self._update_checkpoint(
            run_id,
            watch,
            observed_state=EXTERNAL_STATE_RESULT_AVAILABLE,
            observed_at=stamp,
        )

    def _checkpoint_records_completion(self, run_id: str) -> bool:
        """True iff the persisted checkpoint already records the external
        completion observation for ``run_id`` (``observed_state`` is the
        completion signal and the entry was reconciled) -- the durable
        exactly-once marker of AC-01. The event log is only touched when
        this marker is absent (first completion or the crash-window
        recovery path)."""
        checkpoint = self._checkpoint_store.load()
        if checkpoint is None:
            return False
        for entry in checkpoint.entries:
            if entry.run_id == run_id:
                return (
                    entry.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
                    and entry.reconciled_at is not None
                )
        return False

    def _completion_event(
        self, run_id: str, watch: WatchedRunRecord, stamp: str
    ) -> ProjectEvent:
        """The deterministic transition event of the completion (the
        event vocabulary of ``13-EXECUTION-MONITOR.md`` section 5):
        actor ``execution-monitor``, event_type
        ``external_status_change``, ``from`` the previous lifecycle
        state, ``to`` ``RESULT_AVAILABLE``, the stable completion
        reason, and a deterministic event id."""
        from_state = LifecycleState.RUNNING_EXTERNAL
        to_state = LifecycleState.RESULT_AVAILABLE
        return ProjectEvent(
            event_id=generate_id(
                "event",
                EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
                run_id,
                from_state.value,
                to_state.value,
            ),
            timestamp=stamp,
            actor=RECONCILE_ACTOR,
            event_type=EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
            object_id=run_id,
            run_id=run_id,
            from_=from_state.value,
            to=to_state.value,
            reason=EXTERNAL_COMPLETION_REASON,
        )

    def _update_checkpoint(
        self,
        run_id: str,
        watch: WatchedRunRecord,
        *,
        observed_state: str,
        observed_at: str,
    ) -> None:
        """Upsert the per-run progress entry (observed state/timestamps
        and the reconciliation stamp) and persist the checkpoint with
        entries in sorted run-id order (deterministic bytes)."""
        checkpoint = self._checkpoint_store.load()
        if checkpoint is None:
            checkpoint = MonitorCheckpoint(
                monitor_id=self._monitor_id,
                created_at=observed_at,
                entries=(),
            )
        entry = MonitorRunCheckpoint(
            run_id=run_id,
            external=watch.external,
            observed_state=observed_state,
            observed_at=observed_at,
            reconciled_at=observed_at,
        )
        entries = tuple(
            sorted(
                [
                    e
                    for e in checkpoint.entries
                    if e.run_id != run_id
                ]
                + [entry],
                key=lambda e: e.run_id,
            )
        )
        self._checkpoint_store.save(
            MonitorCheckpoint(
                monitor_id=self._monitor_id,
                created_at=checkpoint.created_at,
                entries=entries,
            )
        )
