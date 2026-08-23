"""Workspace gate-state wiring: the runtime consumer of the gate engines (issue #144).

Implements the missing **state-reading wiring** for the frozen
dependency-gate rule engines of ``core/rules/dependencies.py``: a
deterministic evaluator that, given the workspace root, reads the
registered Goal contracts, their ``dependencies`` and the registered
Run records, resolves each dependency's ``execution_resolved`` /
``acceptance_resolved`` state from the registered Run lifecycle states,
evaluates the execution and acceptance gates through the existing pure
evaluators (``evaluate_execution_gate`` / ``evaluate_acceptance_gate``)
and returns the per-Goal executable/blocked verdicts with the consulted
``matched_rule_id``. No rule table of ``core/rules/dependencies.py`` is
changed here: this module only supplies the inputs those frozen tables
consume.

Normative grounding (frozen)
----------------------------
* ``05-GOAL-RUN-SCHEMA.md`` section 5 -- a dependency may specify
  separately ``execution_gate`` ("must upstream state be reached before
  execution starts?") and ``acceptance_gate`` ("must upstream evidence
  be valid before this Goal may close?"); only ``hard_gate`` blocks,
  soft dependencies are ordering hints and informational dependencies
  are inert (the frozen semantics of ``core/rules/dependencies.py``).
* ``05-GOAL-RUN-SCHEMA.md`` section 7 -- the Run lifecycle mainline
  ``CREATED -> READY -> DISPATCHED -> RUNNING_EXTERNAL ->
  RESULT_AVAILABLE -> ANALYZING -> SUBMITTED_FOR_REVIEW -> CLOSED``
  plus ``CANCELLED`` / ``INVALIDATED`` (``core.models.LifecycleState``,
  ``core.rules.lifecycle``). "Scientific PASS/FAIL is not a Run
  lifecycle state; it is a review decision stored separately" -- so the
  resolution mappings below read lifecycle states only, never
  ``scientific_review``.
* ``core/rules/dependencies.py`` (DEV-M2-G02) -- the normative blocking
  semantics evaluated here: ``evaluate_execution_gate`` /
  ``evaluate_acceptance_gate`` over ``DependencyRecord`` inputs, with
  the auditable ``matched_rule_id`` per assessment.
* ``core/state_backend.py`` -- Run records live at ``runs/<run_id>.json``
  (``SCHEMA_TO_STATE_DIR``); this module reads them through the real
  ``FilesystemStateBackend`` over the workspace root, the exact store
  ``workers/run_helpers.py`` writes and ``reporting/audit.py`` reads.
  (The facade ``workers.run_helpers.list_runs`` is not used so that
  ``workers.run_helpers`` -- the runtime consumer wired here -- can
  import this module at the top level without a module cycle.)

Normative readings (the spec leaves the run-state -> resolution mapping
open; the readings are locked here and asserted over the run-lifecycle
grid in the tests)
------------------------------------------------------------------------
* **Execution resolution** -- a dependency's ``execution_resolved`` is
  True iff the upstream goal has at least one registered Run in a
  result-bearing state: ``RESULT_AVAILABLE``, ``ANALYZING``,
  ``SUBMITTED_FOR_REVIEW`` or ``CLOSED``. The execution gate asks
  whether the upstream state required before execution starts has been
  reached; the upstream execution has delivered its result exactly when
  one of its runs has produced one. Pre-result states (``CREATED``
  .. ``RUNNING_EXTERNAL``) have produced nothing yet, and
  ``CANCELLED`` / ``INVALIDATED`` runs carry no valid result.
* **Acceptance resolution** -- a dependency's ``acceptance_resolved``
  is True iff the upstream goal has at least one registered Run in
  ``SUBMITTED_FOR_REVIEW`` or ``CLOSED``. The acceptance gate asks
  whether the upstream evidence is valid before this Goal may close;
  the upstream evidence is valid from the moment one of its runs has
  been submitted for review (and remains valid through closure).
  The two axes are independent (AC-03): a goal can be execution-eligible
  while its acceptance gate is still blocked -- the FDM-201 BET pattern
  of ``17-FDM201-REFERENCE-CASE.md``.
* **Unregistered dependencies** -- a dependency whose ``goal_id`` has
  no registered Goal contract can never reach either required state, so
  it is unresolved on both axes (a hard-gated edge therefore blocks) and
  is reported explicitly in
  ``WorkspaceGateReport.unresolved_dependency_goal_ids`` (the M4-G02
  convention: unresolved references are explicit, never silently
  dropped). A registered upstream goal with no registered runs is
  likewise unresolved on both axes.
* **Scope** -- the verdicts answer the dependency-gate question only
  ("is this Goal gated by its dependencies?"). A goal's own run history,
  resource gaps (``planning/dag.py``) and acceptance criteria do not
  participate; every registered goal is reported (like ``list_goals``),
  frozen or not -- the dispatch-facing enforcement of
  ``assert_execution_eligible`` is additionally gated on the frozen
  contract by ``workers.run_helpers.register_run`` (issue #148).

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state: no randomness, no
wall clock, no network, nothing is written. ``TypeError`` at the public
boundaries; ``ValueError`` subclasses with stable messages otherwise;
``from __future__ import annotations``; ``__all__``. Registry errors
(``ProjectNotInitializedError``, ``GoalNotFoundError``,
``InvalidRecordIdError``, corrupt-record ``ValueError``) propagate
unchanged from ``planning/plan.py`` / the state backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from scientific_reproduction.core.models import (
    GoalContract,
    GoalDependency,
    LifecycleState,
    Run,
)
from scientific_reproduction.core.rules.dependencies import (
    AcceptanceGateAssessment,
    DependencyRecord,
    ExecutionGateAssessment,
    evaluate_acceptance_gate,
    evaluate_execution_gate,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)
from scientific_reproduction.planning.plan import (
    GoalNotFoundError,
    list_goals,
    read_goal,
)

__all__ = [
    "ACCEPTANCE_RESOLUTION_RULES",
    "ACCEPTANCE_RESOLUTION_RULESET_VERSION",
    "EXECUTION_RESOLUTION_RULES",
    "EXECUTION_RESOLUTION_RULESET_VERSION",
    "GATE_STATE_VERSION",
    "DependencyResolution",
    "GateStateError",
    "GoalExecutionBlockedError",
    "GoalGateVerdict",
    "ResolutionDecision",
    "ResolutionRule",
    "WorkspaceGateReport",
    "assert_execution_eligible",
    "evaluate_workspace_gates",
    "goal_gate_verdict",
]

#: Version of the gate-state report shape. Bumped whenever the report or
#: its canonical mapping changes; recorded in every report.
GATE_STATE_VERSION: str = "1.0"

#: Version of the execution-resolution rule table; recorded in every
#: dependency resolution.
EXECUTION_RESOLUTION_RULESET_VERSION: str = "1.0"

#: Version of the acceptance-resolution rule table; recorded in every
#: dependency resolution.
ACCEPTANCE_RESOLUTION_RULESET_VERSION: str = "1.0"

#: Run lifecycle states in which a Run has delivered its result: the
#: states whose upstream goal satisfies an execution gate (locked reading
#: above; the vocabulary of ``05-GOAL-RUN-SCHEMA.md`` SS7).
_EXECUTION_RESOLVED_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
    }
)

#: Run lifecycle states in which a Run's evidence is valid: the states
#: whose upstream goal satisfies an acceptance gate (locked reading
#: above; the vocabulary of ``05-GOAL-RUN-SCHEMA.md`` SS7).
_ACCEPTANCE_RESOLVED_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
    }
)


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class GateStateError(ValueError):
    """Base error of the workspace gate-state wiring."""


class GoalExecutionBlockedError(GateStateError):
    """Raised when the execution gate of a goal is BLOCKED (AC-01).

    The stable message names the goal, the deciding aggregate rule
    (``matched_rule_id``) and the unresolved hard-gated upstream goals,
    so the dispatch-facing caller can report exactly what blocks the
    goal -- never ad-hoc reasoning.
    """


# ---------------------------------------------------------------------------
# Upstream resolution rule tables (the state-reading step)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionRule:
    """One entry of an ordered upstream-resolution rule table.

    The predicate runs on the upstream goal's registered run-lifecycle
    profile (the sorted tuple of its runs' lifecycle states) -- the only
    input a resolution is a pure function of. An unregistered upstream
    goal contributes the empty profile.
    """

    rule_id: str
    description: str
    resolved: bool
    predicate: Callable[[tuple[LifecycleState, ...]], bool]


@dataclass(frozen=True)
class ResolutionDecision:
    """Record of one resolution-rule evaluation (audit trail)."""

    rule_id: str
    description: str
    resolved: bool
    matched: bool


#: The ordered execution-resolution rule table. First match wins; the
#: trailing default keeps the mapping total. Predicates are pure
#: functions of the upstream run-state profile only.
EXECUTION_RESOLUTION_RULES: tuple[ResolutionRule, ...] = (
    ResolutionRule(
        rule_id="R-RES-EXEC-1",
        description=(
            "the upstream goal has at least one run in a result-bearing"
            " state (RESULT_AVAILABLE, ANALYZING, SUBMITTED_FOR_REVIEW or"
            " CLOSED): the upstream execution has delivered its result --"
            " the state a downstream execution gate requires"
            " (05-GOAL-RUN-SCHEMA.md SS5/SS7)"
        ),
        resolved=True,
        predicate=lambda states: any(
            state in _EXECUTION_RESOLVED_RUN_STATES for state in states
        ),
    ),
    ResolutionRule(
        rule_id="R-RES-EXEC-2",
        description=(
            "no upstream run has reached a result-bearing state (the"
            " upstream goal has no registered runs, only pre-result runs,"
            " or only CANCELLED / INVALIDATED runs, or the dependency goal"
            " has no registered contract): the upstream execution state is"
            " not reached (default, total)"
        ),
        resolved=False,
        predicate=lambda states: True,
    ),
)

#: The ordered acceptance-resolution rule table. First match wins; the
#: trailing default keeps the mapping total. Predicates are pure
#: functions of the upstream run-state profile only.
ACCEPTANCE_RESOLUTION_RULES: tuple[ResolutionRule, ...] = (
    ResolutionRule(
        rule_id="R-RES-ACC-1",
        description=(
            "the upstream goal has at least one run in SUBMITTED_FOR_REVIEW"
            " or CLOSED: the upstream evidence has been submitted for"
            " review and is valid -- the state a downstream acceptance gate"
            " requires (05-GOAL-RUN-SCHEMA.md SS5/SS7)"
        ),
        resolved=True,
        predicate=lambda states: any(
            state in _ACCEPTANCE_RESOLVED_RUN_STATES for state in states
        ),
    ),
    ResolutionRule(
        rule_id="R-RES-ACC-2",
        description=(
            "no upstream run has been submitted for review (the upstream"
            " goal has no registered runs, only pre-review runs, or only"
            " CANCELLED / INVALIDATED runs, or the dependency goal has no"
            " registered contract): the upstream evidence is not valid yet"
            " (default, total)"
        ),
        resolved=False,
        predicate=lambda states: True,
    ),
)


# ---------------------------------------------------------------------------
# Records: one dependency resolution, one goal verdict, one workspace report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyResolution:
    """The resolved upstream state of one declared dependency (auditable).

    ``dependency`` is the exact declared ``GoalDependency``;
    ``registered`` is False exactly when no Goal contract with that id is
    registered; ``upstream_run_states`` is the sorted profile of the
    upstream goal's registered runs (empty when unregistered or runless);
    the two resolution flags and their decision traces come from the
    ``EXECUTION_RESOLUTION_RULES`` / ``ACCEPTANCE_RESOLUTION_RULES``
    tables (``execution_rule_id`` / ``acceptance_rule_id`` name the
    deciding rules; never None -- the trailing default always matches).
    """

    dependency: GoalDependency
    registered: bool
    upstream_run_states: tuple[LifecycleState, ...]
    execution_resolved: bool
    acceptance_resolved: bool
    execution_decisions: tuple[ResolutionDecision, ...]
    acceptance_decisions: tuple[ResolutionDecision, ...]
    execution_rule_id: str
    acceptance_rule_id: str

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this resolution (the gate view)."""
        return {
            "goal_id": self.dependency.goal_id,
            "type": self.dependency.type.value,
            "execution_gate": self.dependency.execution_gate,
            "acceptance_gate": self.dependency.acceptance_gate,
            "registered": self.registered,
            "upstream_run_states": [state.value for state in self.upstream_run_states],
            "execution_resolved": self.execution_resolved,
            "acceptance_resolved": self.acceptance_resolved,
            "execution_decisions": [
                {
                    "rule_id": d.rule_id,
                    "resolved": d.resolved,
                    "matched": d.matched,
                }
                for d in self.execution_decisions
            ],
            "acceptance_decisions": [
                {
                    "rule_id": d.rule_id,
                    "resolved": d.resolved,
                    "matched": d.matched,
                }
                for d in self.acceptance_decisions
            ],
            "execution_rule_id": self.execution_rule_id,
            "acceptance_rule_id": self.acceptance_rule_id,
            "execution_resolution_ruleset_version": (
                EXECUTION_RESOLUTION_RULESET_VERSION
            ),
            "acceptance_resolution_ruleset_version": (
                ACCEPTANCE_RESOLUTION_RULESET_VERSION
            ),
        }


@dataclass(frozen=True)
class GoalGateVerdict:
    """The per-Goal execution/acceptance gate verdicts (the deliverable).

    ``resolutions`` records, in declared dependency order, the resolved
    upstream state of every dependency (the state-reading step);
    ``execution_assessment`` / ``acceptance_assessment`` are the full,
    auditable assessments of the frozen gate engines
    (``core/rules/dependencies.py``) built from those resolutions --
    including each assessment's ``matched_rule_id``. The assessments are
    the authoritative verdicts: the convenience properties below are
    derived from them, never recomputed.
    """

    goal_id: str
    resolutions: tuple[DependencyResolution, ...]
    execution_assessment: ExecutionGateAssessment
    acceptance_assessment: AcceptanceGateAssessment

    @property
    def execution_allowed(self) -> bool:
        """True exactly when the execution gate outcome is ALLOWED."""
        return self.execution_assessment.execution_allowed

    @property
    def acceptance_allowed(self) -> bool:
        """True exactly when the acceptance gate outcome is ALLOWED."""
        return self.acceptance_assessment.acceptance_allowed

    @property
    def executable(self) -> bool:
        """Alias of ``execution_allowed``: may a run of this goal start?"""
        return self.execution_allowed

    @property
    def blocked(self) -> bool:
        """True exactly when the execution gate blocks the goal (AC-01)."""
        return not self.execution_allowed

    @property
    def execution_blocking_goal_ids(self) -> tuple[str, ...]:
        """The unresolved hard execution-gated upstream goals (declared order)."""
        return self.execution_assessment.blocking_goal_ids

    @property
    def acceptance_blocking_goal_ids(self) -> tuple[str, ...]:
        """The unresolved hard acceptance-gated upstream goals (declared order)."""
        return self.acceptance_assessment.blocking_goal_ids

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this verdict (the gate view)."""
        execution = self.execution_assessment
        acceptance = self.acceptance_assessment
        return {
            "goal_id": self.goal_id,
            "executable": self.executable,
            "execution_outcome": execution.outcome.value,
            "execution_matched_rule_id": execution.matched_rule_id,
            "execution_blocking_goal_ids": list(execution.blocking_goal_ids),
            "execution_pending_non_blocking_goal_ids": list(
                execution.pending_non_blocking_goal_ids
            ),
            "acceptance_allowed": self.acceptance_allowed,
            "acceptance_outcome": acceptance.outcome.value,
            "acceptance_matched_rule_id": acceptance.matched_rule_id,
            "acceptance_blocking_goal_ids": list(acceptance.blocking_goal_ids),
            "acceptance_pending_non_blocking_goal_ids": list(
                acceptance.pending_non_blocking_goal_ids
            ),
            "resolutions": [r.to_dict() for r in self.resolutions],
        }


@dataclass(frozen=True)
class WorkspaceGateReport:
    """The workspace-wide gate report (the state-reading deliverable).

    ``verdicts`` holds one :class:`GoalGateVerdict` per registered goal,
    sorted by goal id (the ``list_goals`` order); every dependency of
    every goal is resolved through the real registries and evaluated
    through the frozen gate engines. ``unresolved_dependency_goal_ids``
    lists, sorted and unique, the dependency targets with no registered
    Goal contract (explicitly unresolved -- a hard-gated edge to one
    blocks its goal; the M4-G02 convention).
    """

    gate_state_version: str
    verdicts: tuple[GoalGateVerdict, ...]
    unresolved_dependency_goal_ids: tuple[str, ...]

    @property
    def executable_goal_ids(self) -> tuple[str, ...]:
        """The goal ids whose execution gate is ALLOWED (sorted)."""
        return tuple(v.goal_id for v in self.verdicts if v.execution_allowed)

    @property
    def blocked_goal_ids(self) -> tuple[str, ...]:
        """The goal ids whose execution gate is BLOCKED (sorted)."""
        return tuple(v.goal_id for v in self.verdicts if v.blocked)

    def verdict_for(self, goal_id: str) -> GoalGateVerdict:
        """The verdict of one goal of this report.

        Raises:
            TypeError: ``goal_id`` is not a str.
            GoalNotFoundError: no registered goal with that id is in the
                report.
        """
        if not isinstance(goal_id, str):
            # Deliberate runtime contract check for dynamically typed
            # callers; static analysis may flag the branch as unreachable
            # because the annotation already says str -- it is not dead code.
            raise TypeError(
                f"goal_id must be a str, got {type(goal_id).__name__}"
            )
        for verdict in self.verdicts:
            if verdict.goal_id == goal_id:
                return verdict
        raise GoalNotFoundError(
            f"no registered goal with id {goal_id!r} is in the gate report"
        )

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this report (the /goals gate view)."""
        return {
            "gate_state_version": self.gate_state_version,
            "unresolved_dependency_goal_ids": list(
                self.unresolved_dependency_goal_ids
            ),
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


# ---------------------------------------------------------------------------
# The state-reading evaluators
# ---------------------------------------------------------------------------


def evaluate_workspace_gates(root: str | Path) -> WorkspaceGateReport:
    """Evaluate the execution/acceptance gates of every registered goal.

    Pure and deterministic: the report is a pure function of the
    registered state at ``root`` -- the registered Goal contracts
    (``planning.plan.list_goals``) with their declared ``dependencies``
    and the registered Run records (the ``runs/`` registry, read through
    the real ``FilesystemStateBackend`` run store). Each dependency's
    ``execution_resolved`` / ``acceptance_resolved`` state is resolved
    by the versioned resolution rule tables from the upstream goal's
    registered run-lifecycle profile, then the goal's execution and
    acceptance gates are evaluated through the frozen pure evaluators
    of ``core/rules/dependencies.py``. Nothing is written.

    Args:
        root: the initialized workspace root.

    Returns:
        The frozen :class:`WorkspaceGateReport`.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        ValueError: a stored goal or run record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        # Deliberate runtime contract check (see verdict_for).
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    goals = list_goals(project_root)
    registered_ids = frozenset(goal.goal_id for goal in goals)
    runs_by_goal = _run_state_profiles(project_root)
    unresolved: set[str] = set()
    verdicts: list[GoalGateVerdict] = []
    for goal in goals:
        verdict, missing = _goal_gate_verdict(goal, registered_ids, runs_by_goal)
        unresolved.update(missing)
        verdicts.append(verdict)
    return WorkspaceGateReport(
        gate_state_version=GATE_STATE_VERSION,
        verdicts=tuple(verdicts),
        unresolved_dependency_goal_ids=tuple(sorted(unresolved)),
    )


def goal_gate_verdict(root: str | Path, goal_id: str) -> GoalGateVerdict:
    """Evaluate the execution/acceptance gate verdict of one goal.

    Pure and deterministic: the same registered state always yields the
    identical verdict (same resolution decisions, same gate assessments,
    same matched rule ids). The goal is read through the real goal
    registry; its dependencies are resolved against the registered goals
    and runs exactly as in :func:`evaluate_workspace_gates`. Nothing is
    written.

    Args:
        root: the initialized workspace root.
        goal_id: the id of the registered goal to evaluate.

    Returns:
        The frozen :class:`GoalGateVerdict`.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``goal_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        GoalNotFoundError: no goal with that id is registered.
        InvalidRecordIdError: ``goal_id`` is not a safe single path
            segment.
        ValueError: a stored goal or run record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        # Deliberate runtime contract check (see verdict_for).
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(goal_id, str):
        # Deliberate runtime contract check (see verdict_for).
        raise TypeError(f"goal_id must be a str, got {type(goal_id).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    goal = read_goal(project_root, goal_id)
    registered_ids = frozenset(g.goal_id for g in list_goals(project_root))
    verdict, _ = _goal_gate_verdict(
        goal, registered_ids, _run_state_profiles(project_root)
    )
    return verdict


def assert_execution_eligible(root: str | Path, goal_id: str) -> GoalGateVerdict:
    """Raise unless the goal's execution gate is ALLOWED.

    The enforcement side of AC-01 for the dispatch-facing flow: returns
    the goal's :class:`GoalGateVerdict` when its execution gate allows,
    and raises :class:`GoalExecutionBlockedError` (stable message naming
    the goal, the deciding aggregate rule and the unresolved hard-gated
    upstream goals) when it blocks. Consulted by
    ``workers.run_helpers.register_run`` before any run is authored --
    the Execute step's "is this Goal gated?" decision is deterministic,
    never ad-hoc.

    Args:
        root: the initialized workspace root.
        goal_id: the id of the registered goal to consult.

    Returns:
        The :class:`GoalGateVerdict` (execution gate ALLOWED).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``goal_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        GoalNotFoundError: no goal with that id is registered.
        InvalidRecordIdError: ``goal_id`` is not a safe single path
            segment.
        GoalExecutionBlockedError: the goal's execution gate is BLOCKED
            (AC-01).
        ValueError: a stored goal or run record is corrupt.
    """
    verdict = goal_gate_verdict(root, goal_id)
    if not verdict.execution_allowed:
        blocking = verdict.execution_blocking_goal_ids
        raise GoalExecutionBlockedError(
            f"goal {goal_id!r} is execution-blocked"
            f" ({verdict.execution_assessment.matched_rule_id}): unresolved"
            f" hard-gated upstream goal(s): {', '.join(blocking)}"
        )
    return verdict


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _goal_gate_verdict(
    goal: GoalContract,
    registered_ids: frozenset[str],
    runs_by_goal: dict[str, tuple[LifecycleState, ...]],
) -> tuple[GoalGateVerdict, set[str]]:
    """Compute one goal's verdict from the in-memory registry snapshots.

    Returns ``(verdict, missing_goal_ids)`` where ``missing_goal_ids``
    holds the goal's dependency targets with no registered contract.
    """
    missing: set[str] = set()
    resolutions: list[DependencyResolution] = []
    records: list[DependencyRecord] = []
    for dependency in goal.dependencies:
        registered = dependency.goal_id in registered_ids
        states = runs_by_goal.get(dependency.goal_id, ())
        execution_resolved, execution_decisions, execution_rule_id = (
            _evaluate_resolution(EXECUTION_RESOLUTION_RULES, states)
        )
        acceptance_resolved, acceptance_decisions, acceptance_rule_id = (
            _evaluate_resolution(ACCEPTANCE_RESOLUTION_RULES, states)
        )
        if not registered:
            missing.add(dependency.goal_id)
        resolutions.append(
            DependencyResolution(
                dependency=dependency,
                registered=registered,
                upstream_run_states=states,
                execution_resolved=execution_resolved,
                acceptance_resolved=acceptance_resolved,
                execution_decisions=execution_decisions,
                acceptance_decisions=acceptance_decisions,
                execution_rule_id=execution_rule_id,
                acceptance_rule_id=acceptance_rule_id,
            )
        )
        records.append(
            DependencyRecord.from_goal_dependency(
                dependency,
                execution_resolved=execution_resolved,
                acceptance_resolved=acceptance_resolved,
            )
        )
    return (
        GoalGateVerdict(
            goal_id=goal.goal_id,
            resolutions=tuple(resolutions),
            execution_assessment=evaluate_execution_gate(records),
            acceptance_assessment=evaluate_acceptance_gate(records),
        ),
        missing,
    )


def _evaluate_resolution(
    rules: Sequence[ResolutionRule],
    states: tuple[LifecycleState, ...],
) -> tuple[bool, tuple[ResolutionDecision, ...], str]:
    """Run one resolution rule table over an upstream run-state profile.

    Returns ``(resolved, decisions, matched_rule_id)``. First match wins;
    the trailing default rule always matches (the table is total).
    """
    decisions: list[ResolutionDecision] = []
    matched_rule_id: str | None = None
    matched_resolved = False  # unreachable default
    for rule in rules:
        matched = rule.predicate(states)
        decisions.append(
            ResolutionDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                resolved=rule.resolved,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_resolved = rule.resolved
    # The trailing default rule always matches, so this can never be None.
    assert matched_rule_id is not None
    return matched_resolved, tuple(decisions), matched_rule_id


def _run_state_profiles(root: Path) -> dict[str, tuple[LifecycleState, ...]]:
    """The registered runs grouped by goal id (sorted state profiles).

    Reads the ``runs/`` registry through the real
    ``FilesystemStateBackend`` run store (the exact store
    ``workers/run_helpers.py`` writes and ``reporting/audit.py`` reads);
    a corrupt record raises the stable ``ValueError``.
    """
    store = FilesystemStateBackend(root)
    profiles: dict[str, list[LifecycleState]] = {}
    for run_id in store.list_ids("run"):
        run = _read_run_record(store, run_id)
        profiles.setdefault(run.goal_id, []).append(run.lifecycle_state)
    return {
        goal_id: tuple(sorted(states, key=lambda state: state.value))
        for goal_id, states in profiles.items()
    }


def _read_run_record(store: FilesystemStateBackend, run_id: str) -> Run:
    """Parse one run record, rejecting corrupt state with a stable error."""
    data = store.read("run", run_id)
    try:
        return Run.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"corrupt run record for {run_id!r}: {exc}") from exc


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )
