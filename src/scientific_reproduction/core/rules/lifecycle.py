"""Normative project-phase and Run-lifecycle transition rules (DEV-M2-G01).

This module encodes the frozen specification's lifecycle rule set as pure
data tables plus pure functions. It performs no I/O, uses no randomness,
and never consults the environment: the same inputs always produce the
same answers, on every platform and Python version (AC-03 of DEV-M2-G01).

Normative sources (all frozen; the enums in ``schemas/*.schema.yaml`` are
authoritative for the state sets):

* Project phases -- ``schemas/project.schema.yaml`` (``project_phase``
  enum) and ``04-PROJECT-LIFECYCLE.md`` section 2 (the phase list, given in
  mainline order); section 1 states that ``project_phase`` and
  ``reproduction_outcome`` are strictly separate, so no outcome value is a
  phase.
* Run lifecycle -- ``schemas/run.schema.yaml`` (``lifecycle_state`` enum)
  and ``05-GOAL-RUN-SCHEMA.md`` section 7 ("Recommended Run lifecycle",
  given in mainline order; scientific PASS/FAIL is a review decision stored
  separately, not a lifecycle state); ``13-EXECUTION-MONITOR.md`` section 5
  confirms the arc ``RUNNING_EXTERNAL -> RESULT_AVAILABLE`` with an example
  event.

Rule model
----------
A legal transition is one ordered pair ``(old, new)`` in the exported rule
tables; every other pair is illegal. The tables are built once at import
time by pure deterministic builders over small immutable seed constants and
are exported as ``frozenset`` objects so they cannot be mutated at runtime.

Project phases (14 members):

* mainline forward chain: ``INITIALIZING -> SOURCE_ACQUISITION ->
  REPRODUCTION_INVENTORY -> PLANNING -> PLAN_AUDIT -> PLAN_FROZEN ->
  EXECUTING -> FINAL_VALIDATION -> REPORTING -> COMPLETED``. ``COMPLETED``
  is the single terminal phase and has no outgoing transitions.
* ``REPLANNING`` loop: ``EXECUTING -> REPLANNING -> PLAN_AUDIT``. A revised
  plan must pass ``PLAN_AUDIT`` and ``PLAN_FROZEN`` again before execution
  resumes; jumping straight back to ``EXECUTING`` is an illegal shortcut.
* Suspension states ``PAUSED``, ``WAITING_HUMAN``, ``WAITING_RESOURCE``:
  any active (non-terminal) phase may suspend to any of them, and any of
  them may resume to any active phase. A pure pair table cannot remember
  where a project paused, so resume-to-any-active-phase is the
  deterministic encoding. Suspension states have no transitions between
  themselves and cannot enter ``COMPLETED`` directly.

Run lifecycle (10 members):

* mainline: ``CREATED -> READY -> DISPATCHED -> RUNNING_EXTERNAL ->
  RESULT_AVAILABLE -> ANALYZING -> SUBMITTED_FOR_REVIEW -> CLOSED``.
* ``CANCELLED`` (run abandoned before any result) from every pre-result
  state: ``CREATED``, ``READY``, ``DISPATCHED``, ``RUNNING_EXTERNAL``.
* ``INVALIDATED`` (results produced but not trustworthy; cf. the
  ``invalidate_run_on`` field of ``schemas/retry-policy.schema.yaml``) from
  every result-bearing state: ``RESULT_AVAILABLE``, ``ANALYZING``,
  ``SUBMITTED_FOR_REVIEW``.
* ``CLOSED``, ``CANCELLED`` and ``INVALIDATED`` are terminal: no outgoing
  transitions.

Deliberate strictness (all documented and locked by tests):

* ``old == new`` is never a transition: a transition records a change, and
  no-op state assignments must not enter the audit event log.
* ``RESULT_AVAILABLE -> CANCELLED`` is illegal: a run that produced results
  is invalidated, not cancelled.
* ``CREATED -> INVALIDATED`` is illegal: there is no result to invalidate.
* suspension-to-suspension (e.g. ``PAUSED -> WAITING_RESOURCE``) is
  illegal: changing the wait reason requires resuming to an active phase.
"""

from __future__ import annotations

from enum import Enum

from scientific_reproduction.core.models import LifecycleState, ProjectPhase

__all__ = [
    # rule data
    "PROJECT_PHASE_MAINLINE",
    "PROJECT_PHASE_TRANSITIONS",
    "RUN_MAINLINE",
    "RUN_LIFECYCLE_TRANSITIONS",
    "SUSPENSION_PROJECT_PHASES",
    "ACTIVE_PROJECT_PHASES",
    "TERMINAL_RUN_STATES",
    # errors
    "IllegalTransitionError",
    # predicates and application
    "is_legal_project_phase_transition",
    "is_legal_run_transition",
    "apply_project_phase_transition",
    "apply_run_lifecycle_transition",
    "is_terminal_project_phase",
    "is_terminal_run_state",
    "is_suspension_project_phase",
]


class IllegalTransitionError(ValueError):
    """Raised when a requested transition is not in the normative rule table.

    Attributes:
        subject: the lifecycle kind, e.g. ``"project-phase"`` or
            ``"run-lifecycle"``.
        from_state: the requested source state.
        to_state: the requested target state.
    """

    def __init__(self, subject: str, from_state: Enum, to_state: Enum) -> None:
        self.subject = subject
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal {subject} transition: {from_state.value!r} -> "
            f"{to_state.value!r} is not in the normative rule table "
            f"(see scientific_reproduction.core.rules.lifecycle)"
        )


# ---------------------------------------------------------------------------
# Project phases
# ---------------------------------------------------------------------------

PROJECT_PHASE_MAINLINE: tuple[ProjectPhase, ...] = (
    ProjectPhase.INITIALIZING,
    ProjectPhase.SOURCE_ACQUISITION,
    ProjectPhase.REPRODUCTION_INVENTORY,
    ProjectPhase.PLANNING,
    ProjectPhase.PLAN_AUDIT,
    ProjectPhase.PLAN_FROZEN,
    ProjectPhase.EXECUTING,
    ProjectPhase.FINAL_VALIDATION,
    ProjectPhase.REPORTING,
    ProjectPhase.COMPLETED,
)

SUSPENSION_PROJECT_PHASES: frozenset[ProjectPhase] = frozenset(
    {
        ProjectPhase.PAUSED,
        ProjectPhase.WAITING_HUMAN,
        ProjectPhase.WAITING_RESOURCE,
    }
)

ACTIVE_PROJECT_PHASES: frozenset[ProjectPhase] = frozenset(
    {phase for phase in PROJECT_PHASE_MAINLINE if phase != ProjectPhase.COMPLETED}
) | {ProjectPhase.REPLANNING}


def _build_project_phase_transitions() -> frozenset[tuple[ProjectPhase, ProjectPhase]]:
    """Build the normative project-phase table (pure, deterministic).

    The table is the disjoint union of three documented rule families:

    * the mainline forward chain (``PROJECT_PHASE_MAINLINE`` order);
    * the replanning loop (``EXECUTING -> REPLANNING -> PLAN_AUDIT``);
    * suspension arcs: every active phase <-> every suspension phase.
    """
    mainline = {
        (PROJECT_PHASE_MAINLINE[i], PROJECT_PHASE_MAINLINE[i + 1])
        for i in range(len(PROJECT_PHASE_MAINLINE) - 1)
    }
    replanning = {
        (ProjectPhase.EXECUTING, ProjectPhase.REPLANNING),
        (ProjectPhase.REPLANNING, ProjectPhase.PLAN_AUDIT),
    }
    suspend = {
        (active, suspended)
        for active in ACTIVE_PROJECT_PHASES
        for suspended in SUSPENSION_PROJECT_PHASES
    }
    resume = {
        (suspended, active)
        for suspended in SUSPENSION_PROJECT_PHASES
        for active in ACTIVE_PROJECT_PHASES
    }
    return frozenset(mainline | replanning | suspend | resume)


PROJECT_PHASE_TRANSITIONS: frozenset[tuple[ProjectPhase, ProjectPhase]] = (
    _build_project_phase_transitions()
)


# ---------------------------------------------------------------------------
# Run lifecycle
# ---------------------------------------------------------------------------

RUN_MAINLINE: tuple[LifecycleState, ...] = (
    LifecycleState.CREATED,
    LifecycleState.READY,
    LifecycleState.DISPATCHED,
    LifecycleState.RUNNING_EXTERNAL,
    LifecycleState.RESULT_AVAILABLE,
    LifecycleState.ANALYZING,
    LifecycleState.SUBMITTED_FOR_REVIEW,
    LifecycleState.CLOSED,
)

_PRE_RESULT_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.CREATED,
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
    }
)

_RESULT_BEARING_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
    }
)

TERMINAL_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.CLOSED,
        LifecycleState.CANCELLED,
        LifecycleState.INVALIDATED,
    }
)


def _build_run_transitions() -> frozenset[tuple[LifecycleState, LifecycleState]]:
    """Build the normative Run-lifecycle table (pure, deterministic).

    The table is the disjoint union of three documented rule families:

    * the mainline forward chain (``RUN_MAINLINE`` order);
    * cancellation arcs: every pre-result state -> ``CANCELLED``;
    * invalidation arcs: every result-bearing state -> ``INVALIDATED``.
    """
    mainline = {
        (RUN_MAINLINE[i], RUN_MAINLINE[i + 1]) for i in range(len(RUN_MAINLINE) - 1)
    }
    cancelled = {(state, LifecycleState.CANCELLED) for state in _PRE_RESULT_RUN_STATES}
    invalidated = {
        (state, LifecycleState.INVALIDATED) for state in _RESULT_BEARING_RUN_STATES
    }
    return frozenset(mainline | cancelled | invalidated)


RUN_LIFECYCLE_TRANSITIONS: frozenset[tuple[LifecycleState, LifecycleState]] = (
    _build_run_transitions()
)


# ---------------------------------------------------------------------------
# Predicates and application
# ---------------------------------------------------------------------------


def is_legal_project_phase_transition(old: ProjectPhase, new: ProjectPhase) -> bool:
    """Return whether ``old -> new`` is a normative project-phase transition."""
    return (old, new) in PROJECT_PHASE_TRANSITIONS


def is_legal_run_transition(old: LifecycleState, new: LifecycleState) -> bool:
    """Return whether ``old -> new`` is a normative Run-lifecycle transition."""
    return (old, new) in RUN_LIFECYCLE_TRANSITIONS


def apply_project_phase_transition(old: ProjectPhase, new: ProjectPhase) -> ProjectPhase:
    """Return ``new`` if ``old -> new`` is normative, else raise.

    Raises:
        IllegalTransitionError: the pair is not in the rule table.
    """
    if not is_legal_project_phase_transition(old, new):
        raise IllegalTransitionError("project-phase", old, new)
    return new


def apply_run_lifecycle_transition(old: LifecycleState, new: LifecycleState) -> LifecycleState:
    """Return ``new`` if ``old -> new`` is normative, else raise.

    Raises:
        IllegalTransitionError: the pair is not in the rule table.
    """
    if not is_legal_run_transition(old, new):
        raise IllegalTransitionError("run-lifecycle", old, new)
    return new


def is_terminal_project_phase(phase: ProjectPhase) -> bool:
    """Return whether ``phase`` has no outgoing normative transitions."""
    return all(old != phase for old, _ in PROJECT_PHASE_TRANSITIONS)


def is_terminal_run_state(state: LifecycleState) -> bool:
    """Return whether ``state`` has no outgoing normative transitions."""
    return all(old != state for old, _ in RUN_LIFECYCLE_TRANSITIONS)


def is_suspension_project_phase(phase: ProjectPhase) -> bool:
    """Return whether ``phase`` is a suspension state (PAUSED / WAITING_*)."""
    return phase in SUSPENSION_PROJECT_PHASES
