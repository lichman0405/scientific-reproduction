"""Plan DAG export and resource blocker mapping (DEV-M4-G05).

Implements the **DAG builder/export** and the **resource blocker mapping**
deliverables of DEV-M4-G05: a deterministic, view-exportable DAG of the
plan's Unit-Process Goals -- nodes, dependency edges with their gate kinds,
topological order, cycle detection -- plus the rule-based mapping from goal
and plan nodes to the resource gaps that block them, grounded in:

* ``05-GOAL-RUN-SCHEMA.md`` SS5: dependency semantics include
  ``hard_gate`` / ``soft_dependency`` / ``informational``, and a dependency
  may specify separately ``execution_gate`` ("must upstream state be
  reached before execution starts?") and ``acceptance_gate`` ("must
  upstream evidence be valid before this Goal may close?");
* ``01-PRODUCT-REQUIREMENTS.md`` SS5 step 7 (dependencies and resources are
  plan inputs) and SS6 (the ``/goals`` view summarizes the Goal DAG and
  current states; ``/goals blocked`` shows blocked Goals and blocker
  objects);
* ``core/models.py`` (frozen): ``Plan`` (goal_ids, resource_ids,
  work_packages), ``GoalContract`` (dependencies: list[GoalDependency],
  resource_ids, acceptance), ``GoalDependency`` (goal_id, type,
  execution_gate, acceptance_gate), ``Resource`` (availability_state,
  blocks_goal_ids), ``AvailabilityState``;
* ``schemas/goal.schema.yaml`` (the dependency item shape) and
  ``schemas/plan.schema.yaml`` (the plan record shape);
* ``planning/plan.py`` (DEV-M4-G04): ``read_plan`` / ``list_goals`` -- the
  DAG is a pure function of the registered state;
* ``planning/resources.py`` (DEV-M4-G05): ``load_resource_registry`` and
  the frozen availability vocabulary ``is_resource_gap`` /
  ``RESOURCE_GAP_STATES``;
* ``core/rules/dependencies.py`` (DEV-M2-G02, frozen): the normative
  blocking semantics -- only an unresolved hard-gated axis blocks, soft
  dependencies are ordering hints, informational dependencies are inert,
  an un-flagged hard edge gates nothing (R-DEP-6). This module classifies
  the *declared* gate kinds for the view; it never re-derives blocking.

Normative readings (locked here, proven in the tests)
-------------------------------------------------------
* **Node set**: the DAG's nodes are the plan's goals -- every registered
  goal contract whose id is in ``plan.goal_ids`` (``in_plan`` True) -- plus
  every registered goal reachable through dependency edges (the transitive
  closure over registered goals, ``in_plan`` False): a dependency edge must
  render, so a registered upstream is a node even when the plan does not
  cover it. A plan goal id without a registered contract is reported
  explicitly in ``missing_goal_contracts`` (never silently dropped -- the
  M4-G02 convention that unresolved references are explicit). The frozen
  models define no Work Package object (``plan.work_packages`` is a
  free-form dict list), so the export does not invent work-package nodes;
  the plan record is carried verbatim in the export context.
* **Edge direction and kinds**: a ``GoalDependency`` is stored on the
  dependent goal; edges render dependency-first (``dependency_goal_id`` ->
  ``dependent_goal_id``) so ``topological_order`` is ready-first:
  a goal's dependencies precede it. Every edge carries the raw model
  fields (``type``, ``execution_gate``, ``acceptance_gate``) verbatim
  **and** a computed ``gate_kind`` from the six-kind vocabulary
  (AC-03): strength (hard_gate / soft_dependency / informational) x
  governance axis (execution / acceptance). The axis is decided by the
  ordered ``GATE_AXIS_RULES`` table (versioned, first match wins, trailing
  total default):
  R-AX-E1 execution-only -> EXECUTION; R-AX-A1 acceptance-only ->
  ACCEPTANCE; R-AX-B1 both flags set -> EXECUTION (the scheduling axis
  /goals execution views render; the raw ``acceptance_gate`` flag
  preserves the acceptance governance verbatim -- nothing is lost, and
  ``core/rules/dependencies.py`` keeps the combined blocking state
  distinct); R-AX-N1 no flags set (total default) -> EXECUTION (execution
  ordering is the baseline semantic of a dependency edge and acceptance
  gating is the optional strengthening, 05 SS5 "may specify separately";
  blocking semantics are unaffected -- they come from the core rules).
* **Blocking vs acceptance (AC-02)**: a resource gap -- any
  availability state other than AVAILABLE (``RESOURCE_GAP_STATES``), or a
  resource reference with no registered record -- can block a goal (and
  the plan), and that blocking is an **execution/scheduling fact** that
  never alters the goal's scientific acceptance: the blocker rules read
  only the resource availability state and the frozen resource/goal edges
  (``goal.resource_ids``, ``resource.blocks_goal_ids``); the goal's
  ``acceptance`` sub-object (criteria_ref, frozen) is carried verbatim and
  never participates in blocking (proven behaviorally in the tests). The
  blocker mapping is rule-based (``BLOCKER_RULES``: versioned, ordered,
  first match wins, trailing total default), one evaluated pair per
  (goal, resource-or-reference), with the full rule trace recorded.
* **Cycle handling**: the export is total -- a dependency cycle (or a
  self-dependency) is detected deterministically (Kahn's algorithm over
  distinct edges) and reported in ``cyclic_goal_ids`` with ``acyclic``
  False and an empty ``topological_order``; no exception is raised, so
  /goals views render the inconsistency explicitly.
* **Export format**: ``plan_dag_to_dict`` / ``export_plan_dag`` produce
  the canonical JSON of the M4-G04 plan registry (sorted keys, 2-space
  indent, trailing newline) -- the /goals view payload. The DAG builder
  is a pure evaluator over registered state: it persists nothing (views
  and dispatchers consume the export; the plan record is the stored
  truth, ``14-STATE-GIT-ARTIFACTS.md`` SS2).

Pure deterministic functions, no randomness, no wall-clock, no LLM;
``TypeError`` at the public boundaries; registry errors
(``ProjectNotInitializedError``, ``PlanNotFoundError``, corrupt-record
``ValueError``) propagate unchanged from ``planning/plan.py`` /
``planning/resources.py`` with their stable messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Sequence

from scientific_reproduction.core.models import (
    AvailabilityState,
    DependencyType,
    GoalContract,
    Plan,
)
from scientific_reproduction.planning.plan import list_goals, read_plan
from scientific_reproduction.planning.resources import (
    RESOURCE_GAP_STATES,
    ResourceRegistry,
    is_resource_gap,
    load_resource_registry,
)

__all__ = [
    "BLOCKER_RULES",
    "BLOCKER_RULESET_VERSION",
    "DAG_EXPORT_VERSION",
    "GATE_AXIS_RULES",
    "GATE_AXIS_RULESET_VERSION",
    "BlockerDecision",
    "BlockerInput",
    "BlockerRule",
    "BlockingVerdict",
    "DAGEdge",
    "DAGNode",
    "GateAxis",
    "GateAxisDecision",
    "GateAxisRule",
    "GateKind",
    "GateKindAssessment",
    "GateKindInput",
    "GoalBlockers",
    "PairAssessment",
    "PlanningDAG",
    "ResourceBlockerMapping",
    "UnresolvedDependencyRef",
    "build_plan_dag",
    "classify_gate_kind",
    "evaluate_resource_blocking",
    "export_plan_dag",
    "plan_dag_to_dict",
    "resource_blocker_mapping",
    "resource_blockers_for_goal",
]

#: Version of the plan DAG export shape. Bumped whenever the export schema
#: changes; recorded in every export so old views stay interpretable.
DAG_EXPORT_VERSION: str = "1.0"

#: Version of the gate-axis rule table. Bumped whenever a rule changes;
#: recorded in every gate-kind assessment.
GATE_AXIS_RULESET_VERSION: str = "1.0"

#: Version of the resource blocker rule table. Bumped whenever a rule
#: changes; recorded in every pair assessment.
BLOCKER_RULESET_VERSION: str = "1.0"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline),
#: the format of the M4-G04 plan registry.
_JSON_INDENT: int = 2

# ---------------------------------------------------------------------------
# Gate vocabulary (AC-03): strength x governance axis
# ---------------------------------------------------------------------------


class GateAxis(StrEnum):
    """The governance axis of a dependency edge.

    The axis is decided from the raw ``execution_gate`` / ``acceptance_gate``
    flags by the ``GATE_AXIS_RULES`` table (execution-only ->
    EXECUTION; acceptance-only -> ACCEPTANCE; both set or neither set ->
    EXECUTION by the documented normative readings in the module
    docstring). EXECUTION gates govern whether upstream state must be
    reached before this goal's execution starts; ACCEPTANCE gates govern
    whether upstream evidence must be valid before this goal may close
    (``05-GOAL-RUN-SCHEMA.md`` SS5).
    """

    EXECUTION = "execution"
    ACCEPTANCE = "acceptance"


class GateKind(StrEnum):
    """The six gate kinds of AC-03: dependency strength x governance axis.

    hard_gate / soft_dependency / informational (``DependencyType``, the
    frozen strength vocabulary of ``05-GOAL-RUN-SCHEMA.md`` SS5) combined
    with the execution / acceptance axis. All six combinations are distinct
    values, so /goals views render each combination distinctly and the
    classification is deterministic (same declared edge -> same kind).
    """

    HARD_EXECUTION = "hard_execution"
    HARD_ACCEPTANCE = "hard_acceptance"
    SOFT_EXECUTION = "soft_execution"
    SOFT_ACCEPTANCE = "soft_acceptance"
    INFORMATIONAL_EXECUTION = "informational_execution"
    INFORMATIONAL_ACCEPTANCE = "informational_acceptance"


#: The six gate kinds indexed by (DependencyType, GateAxis). Total over the
#: vocabulary: every strength x axis combination is representable (AC-03).
_GATE_KINDS: dict[tuple[DependencyType, GateAxis], GateKind] = {
    (DependencyType.HARD_GATE, GateAxis.EXECUTION): GateKind.HARD_EXECUTION,
    (DependencyType.HARD_GATE, GateAxis.ACCEPTANCE): GateKind.HARD_ACCEPTANCE,
    (
        DependencyType.SOFT_DEPENDENCY,
        GateAxis.EXECUTION,
    ): GateKind.SOFT_EXECUTION,
    (
        DependencyType.SOFT_DEPENDENCY,
        GateAxis.ACCEPTANCE,
    ): GateKind.SOFT_ACCEPTANCE,
    (
        DependencyType.INFORMATIONAL,
        GateAxis.EXECUTION,
    ): GateKind.INFORMATIONAL_EXECUTION,
    (
        DependencyType.INFORMATIONAL,
        GateAxis.ACCEPTANCE,
    ): GateKind.INFORMATIONAL_ACCEPTANCE,
}


@dataclass(frozen=True)
class GateKindInput:
    """The declared gate state a gate kind is a pure function of.

    Frozen and hashable so "same declared edge -> same kind" is directly
    testable and the exact input is preserved in every assessment.

    Raises:
        TypeError: a gate flag is not a bool.
    """

    dependency_type: DependencyType
    execution_gate: bool
    acceptance_gate: bool

    def __post_init__(self) -> None:
        if not isinstance(self.dependency_type, DependencyType):
            raise TypeError(
                "GateKindInput.dependency_type must be a DependencyType, got"
                f" {self.dependency_type!r}"
            )
        for name in ("execution_gate", "acceptance_gate"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"GateKindInput.{name} must be a bool, got"
                    f" {type(value).__name__}"
                )


@dataclass(frozen=True)
class GateAxisRule:
    """One entry of the ordered gate-axis rule table."""

    rule_id: str
    description: str
    axis: GateAxis
    predicate: Callable[[GateKindInput], bool]


@dataclass(frozen=True)
class GateAxisDecision:
    """Record of one gate-axis rule evaluation (auditability)."""

    rule_id: str
    description: str
    axis: GateAxis
    matched: bool


#: The ordered gate-axis rule table. First match wins; order is normative
#: (see the module docstring). Predicates are pure functions of the
#: :class:`GateKindInput` only; R-AX-N1 is the trailing total default, so
#: every declared edge yields exactly one axis (and therefore exactly one
#: of the six :class:`GateKind` values -- AC-03 totality).
GATE_AXIS_RULES: tuple[GateAxisRule, ...] = (
    GateAxisRule(
        rule_id="R-AX-E1",
        description=(
            "execution_gate is set and acceptance_gate is not: an"
            " execution-only gate -- upstream state must be reached before"
            " this goal's execution starts (05-GOAL-RUN-SCHEMA.md SS5)"
        ),
        axis=GateAxis.EXECUTION,
        predicate=lambda i: i.execution_gate and not i.acceptance_gate,
    ),
    GateAxisRule(
        rule_id="R-AX-A1",
        description=(
            "acceptance_gate is set and execution_gate is not: an"
            " acceptance-only gate -- upstream evidence must be valid before"
            " this goal may close, while execution may start in parallel"
            " (05-GOAL-RUN-SCHEMA.md SS5)"
        ),
        axis=GateAxis.ACCEPTANCE,
        predicate=lambda i: i.acceptance_gate and not i.execution_gate,
    ),
    GateAxisRule(
        rule_id="R-AX-B1",
        description=(
            "both gate flags are set: the edge is both an execution and an"
            " acceptance gate (the FDM-201 pattern, e.g. the activation hard"
            " gate of examples/fdm-201/goal.example.yaml). The gate kind"
            " reports the execution axis -- the scheduling axis /goals"
            " execution views render -- and the exported edge preserves the"
            " raw acceptance_gate flag verbatim, so the acceptance governance"
            " is never lost (core/rules/dependencies.py keeps the combined"
            " blocking state distinct; this export keeps it distinct through"
            " the raw booleans)"
        ),
        axis=GateAxis.EXECUTION,
        predicate=lambda i: i.execution_gate and i.acceptance_gate,
    ),
    GateAxisRule(
        rule_id="R-AX-N1",
        description=(
            "no gate flag is set (the schema default): the edge is"
            " classified on the execution axis -- execution ordering is the"
            " baseline semantic of a dependency edge and acceptance gating"
            " is the optional strengthening ('may specify separately',"
            " 05-GOAL-RUN-SCHEMA.md SS5); an edge that must not gate"
            " execution declares execution_gate: false. Blocking semantics"
            " are unaffected: they come from core/rules/dependencies.py,"
            " where an un-flagged hard edge gates nothing (R-DEP-6) and"
            " informational edges never block (R-DEP-5) (default, total)"
        ),
        axis=GateAxis.EXECUTION,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class GateKindAssessment:
    """Full, auditable result of one gate-kind classification (AC-03).

    ``input`` is the exact declared gate state; ``gate_kind`` is one of the
    six kinds; ``decisions`` records every rule evaluation of the table (in
    evaluation order); ``matched_rule_id`` names the deciding rule (never
    ``None``: the trailing default always matches); ``ruleset_version``
    records the rule table version.
    """

    input: GateKindInput
    gate_kind: GateKind
    decisions: tuple[GateAxisDecision, ...]
    matched_rule_id: str
    ruleset_version: str = GATE_AXIS_RULESET_VERSION


def classify_gate_kind(
    dependency_type: DependencyType,
    execution_gate: bool,
    acceptance_gate: bool,
) -> GateKindAssessment:
    """Classify one declared dependency edge into its six-kind gate kind.

    Pure and deterministic (AC-03): the kind is a pure function of the
    declared gate state -- the strength comes from ``dependency_type`` and
    the governance axis from the ``GATE_AXIS_RULES`` table. Every declared
    edge yields exactly one kind (the table is total); the returned
    assessment preserves the exact input and the full rule trace.

    Raises:
        TypeError: ``dependency_type`` is not a ``DependencyType``, or a
            gate flag is not a bool.
    """
    gate_input = GateKindInput(
        dependency_type=dependency_type,
        execution_gate=execution_gate,
        acceptance_gate=acceptance_gate,
    )
    decisions: list[GateAxisDecision] = []
    matched_rule_id: str | None = None
    matched_axis = GateAxis.EXECUTION  # unreachable default
    for rule in GATE_AXIS_RULES:
        matched = rule.predicate(gate_input)
        decisions.append(
            GateAxisDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                axis=rule.axis,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_axis = rule.axis
    # R-AX-N1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return GateKindAssessment(
        input=gate_input,
        gate_kind=_GATE_KINDS[(dependency_type, matched_axis)],
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Resource blocker mapping (AC-02): rule-based, stable, deterministic
# ---------------------------------------------------------------------------


class BlockingVerdict(StrEnum):
    """Verdict of one (goal, resource-or-reference) blocker evaluation."""

    BLOCKS = "BLOCKS"
    NOT_BLOCKING = "NOT_BLOCKING"


@dataclass(frozen=True)
class BlockerInput:
    """The state one blocker verdict is a pure function of.

    Frozen and hashable so "same pair -> same verdict" is directly testable
    and the exact input is preserved in every assessment. ``registered`` is
    False exactly when no record with ``resource_id`` exists in the
    registry (a missing resource is a gap by definition, AC-02);
    ``availability_state`` is the stored state of the registered record
    (``None`` when missing); ``explicitly_blocks`` is True iff the
    registered resource's ``blocks_goal_ids`` names the goal (the frozen
    resource -> goal declaration edge); ``required_by_goal`` is True iff the
    goal's ``resource_ids`` names the resource (the goal -> resource
    requirement edge).

    Raises:
        TypeError: a field has the wrong type (goal_id/resource_id not str,
            a flag not bool, availability_state neither an
            ``AvailabilityState`` nor None).
        ValueError: ``goal_id`` / ``resource_id`` is empty, or
            ``registered`` disagrees with ``availability_state``.
    """

    goal_id: str
    resource_id: str
    registered: bool
    availability_state: AvailabilityState | None
    explicitly_blocks: bool
    required_by_goal: bool

    def __post_init__(self) -> None:
        if not isinstance(self.goal_id, str) or not self.goal_id:
            raise ValueError(
                "BlockerInput.goal_id must be a non-empty string, got"
                f" {self.goal_id!r}"
            )
        if not isinstance(self.resource_id, str) or not self.resource_id:
            raise ValueError(
                "BlockerInput.resource_id must be a non-empty string, got"
                f" {self.resource_id!r}"
            )
        for name in ("registered", "explicitly_blocks", "required_by_goal"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(
                    f"BlockerInput.{name} must be a bool, got"
                    f" {type(value).__name__}"
                )
        state = self.availability_state
        if state is not None and not isinstance(state, AvailabilityState):
            raise TypeError(
                "BlockerInput.availability_state must be an AvailabilityState"
                f" or None, got {type(state).__name__}"
            )
        if self.registered != (state is not None):
            raise ValueError(
                "BlockerInput.registered must agree with availability_state:"
                f" registered={self.registered!r} but availability_state="
                f"{self.availability_state!r}"
            )


@dataclass(frozen=True)
class BlockerRule:
    """One entry of the ordered resource blocker rule table."""

    rule_id: str
    description: str
    verdict: BlockingVerdict
    predicate: Callable[[BlockerInput], bool]


@dataclass(frozen=True)
class BlockerDecision:
    """Record of one blocker rule evaluation (auditability)."""

    rule_id: str
    description: str
    verdict: BlockingVerdict
    matched: bool


#: The ordered resource blocker rule table. First match wins; order is
#: normative (see the module docstring): the missing-reference rule first
#: (a missing resource is unresolvable and must block), then the resource's
#: own declaration edge, then the goal's requirement edge -- the
#: declaration is the more specific edge and names the matched rule when
#: both hold -- and R-BLK-4 as the trailing total default (an AVAILABLE
#: resource blocks nothing, even when declared).
BLOCKER_RULES: tuple[BlockerRule, ...] = (
    BlockerRule(
        rule_id="R-BLK-1",
        description=(
            "the goal declares a resource requirement with no registered"
            " record: a missing resource is a gap (AC-02 -- the requirement"
            " cannot be satisfied) and blocks the goal"
        ),
        verdict=BlockingVerdict.BLOCKS,
        predicate=lambda i: not i.registered and i.required_by_goal,
    ),
    BlockerRule(
        rule_id="R-BLK-2",
        description=(
            "the registered resource is a gap (not AVAILABLE) and declares"
            " the goal in its blocks_goal_ids: the resource's own"
            " declaration edge blocks the goal"
        ),
        verdict=BlockingVerdict.BLOCKS,
        predicate=lambda i: (
            i.registered
            and i.availability_state in RESOURCE_GAP_STATES
            and i.explicitly_blocks
        ),
    ),
    BlockerRule(
        rule_id="R-BLK-3",
        description=(
            "the registered resource is a gap (not AVAILABLE) and the goal"
            " requires it in its resource_ids: the goal's requirement edge"
            " blocks the goal"
        ),
        verdict=BlockingVerdict.BLOCKS,
        predicate=lambda i: (
            i.registered
            and i.availability_state in RESOURCE_GAP_STATES
            and i.required_by_goal
        ),
    ),
    BlockerRule(
        rule_id="R-BLK-4",
        description=(
            "no gap edge applies: the resource is AVAILABLE, which blocks"
            " nothing (default, total)"
        ),
        verdict=BlockingVerdict.NOT_BLOCKING,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class PairAssessment:
    """Full, auditable result of one (goal, resource) blocker evaluation.

    ``input`` is the exact evaluated pair; ``verdict`` is
    BLOCKS / NOT_BLOCKING; ``decisions`` records every rule evaluation (in
    evaluation order); ``matched_rule_id`` names the deciding rule (never
    ``None``: the trailing default always matches); ``ruleset_version``
    records the rule table version.
    """

    input: BlockerInput
    verdict: BlockingVerdict
    decisions: tuple[BlockerDecision, ...]
    matched_rule_id: str
    ruleset_version: str = BLOCKER_RULESET_VERSION

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this assessment (the /goals view)."""
        return {
            "goal_id": self.input.goal_id,
            "resource_id": self.input.resource_id,
            "registered": self.input.registered,
            "availability_state": (
                self.input.availability_state.value
                if self.input.availability_state is not None
                else None
            ),
            "explicitly_blocks": self.input.explicitly_blocks,
            "required_by_goal": self.input.required_by_goal,
            "verdict": self.verdict.value,
            "decisions": [
                {
                    "rule_id": d.rule_id,
                    "verdict": d.verdict.value,
                    "matched": d.matched,
                }
                for d in self.decisions
            ],
            "matched_rule_id": self.matched_rule_id,
            "ruleset_version": self.ruleset_version,
        }


def evaluate_resource_blocking(input_: BlockerInput) -> PairAssessment:
    """Evaluate one (goal, resource-or-reference) pair with the rule table.

    Pure and deterministic: the verdict is a pure function of the pair's
    state (AC-02 determinism) -- a gap resource that is required by the
    goal or declares the goal blocks it; an AVAILABLE resource never
    blocks; a missing reference always blocks (R-BLK-1). The returned
    :class:`PairAssessment` preserves the exact input and every rule
    decision.

    Raises:
        TypeError: ``input_`` is not a ``BlockerInput``.
    """
    if not isinstance(input_, BlockerInput):
        raise TypeError(
            "evaluate_resource_blocking expects a BlockerInput, got"
            f" {type(input_).__name__}"
        )
    decisions: list[BlockerDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = BlockingVerdict.NOT_BLOCKING  # unreachable default
    for rule in BLOCKER_RULES:
        matched = rule.predicate(input_)
        decisions.append(
            BlockerDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
    # R-BLK-4 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return PairAssessment(
        input=input_,
        verdict=matched_verdict,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


@dataclass(frozen=True)
class GoalBlockers:
    """The blocker mapping entry of one goal node (AC-02).

    ``blocking_resource_ids`` lists, sorted and unique, every resource id
    whose evaluation verdicts BLOCKS (gap resources -- including missing
    references); ``missing_resource_ids`` lists, sorted and unique, the
    references with no registered record (a subset of the blockers);
    ``blocked`` is True exactly when the blocker set is non-empty.
    ``decisions`` records one :class:`PairAssessment` per evaluated pair,
    sorted by resource id, so the mapping is auditable and deterministic.
    """

    goal_id: str
    blocked: bool
    blocking_resource_ids: tuple[str, ...]
    missing_resource_ids: tuple[str, ...]
    decisions: tuple[PairAssessment, ...]

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this entry (the /goals view)."""
        return {
            "goal_id": self.goal_id,
            "blocked": self.blocked,
            "blocking_resource_ids": list(self.blocking_resource_ids),
            "missing_resource_ids": list(self.missing_resource_ids),
            "decisions": [d.to_dict() for d in self.decisions],
        }


def resource_blockers_for_goal(
    goal: GoalContract, registry: ResourceRegistry
) -> GoalBlockers:
    """Compute the resource gaps that block one goal (AC-02).

    The evaluated pair universe is the union of the goal's requirement
    edges (``goal.resource_ids``) and the resources' declaration edges
    (``resource.blocks_goal_ids`` naming the goal); every pair is evaluated
    by the ``BLOCKER_RULES`` table and the BLOCKS verdicts are the goal's
    blockers. The computation reads only the availability state and the
    frozen edges -- the goal's ``acceptance`` sub-object never
    participates, so blocking never alters scientific acceptance (AC-02,
    proven behaviorally in the tests).

    Raises:
        TypeError: ``goal`` is not a ``GoalContract``, or ``registry`` is
            not a ``ResourceRegistry``.
    """
    if not isinstance(goal, GoalContract):
        raise TypeError(
            f"resource_blockers_for_goal expects a GoalContract, got"
            f" {type(goal).__name__}"
        )
    if not isinstance(registry, ResourceRegistry):
        raise TypeError(
            "resource_blockers_for_goal expects a ResourceRegistry, got"
            f" {type(registry).__name__}"
        )
    required = frozenset(goal.resource_ids)
    by_id = {r.resource_id: r for r in registry.resources}
    declared: set[str] = set()
    for resource in registry.resources:
        if goal.goal_id in resource.blocks_goal_ids:
            declared.add(resource.resource_id)
    pair_ids = sorted(required | declared)
    assessments: list[PairAssessment] = []
    for resource_id in pair_ids:
        registered = by_id.get(resource_id)
        assessments.append(
            evaluate_resource_blocking(
                BlockerInput(
                    goal_id=goal.goal_id,
                    resource_id=resource_id,
                    registered=registered is not None,
                    availability_state=(
                        registered.availability_state
                        if registered is not None
                        else None
                    ),
                    explicitly_blocks=bool(
                        registered is not None
                        and goal.goal_id in registered.blocks_goal_ids
                    ),
                    required_by_goal=resource_id in required,
                )
            )
        )
    blocking = sorted(
        {
            a.input.resource_id
            for a in assessments
            if a.verdict is BlockingVerdict.BLOCKS
        }
    )
    missing = sorted(
        {
            a.input.resource_id
            for a in assessments
            if not a.input.registered
        }
    )
    return GoalBlockers(
        goal_id=goal.goal_id,
        blocked=bool(blocking),
        blocking_resource_ids=tuple(blocking),
        missing_resource_ids=tuple(missing),
        decisions=tuple(assessments),
    )


@dataclass(frozen=True)
class ResourceBlockerMapping:
    """The full resource blocker mapping of a plan (the AC-02 deliverable).

    ``entries`` holds one :class:`GoalBlockers` per mapped goal, sorted by
    goal id; ``plan_blocking_resource_ids`` lists the plan-level gaps (the
    plan record's own ``resource_ids`` that are missing or not AVAILABLE --
    the plan as a node is blocked until they resolve, ``/goals blocked``);
    ``missing_resource_ids`` is the sorted union of every missing reference
    across the goal and plan edges.
    """

    entries: tuple[GoalBlockers, ...]
    plan_blocking_resource_ids: tuple[str, ...]
    missing_resource_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping (the /goals view payload)."""
        return {
            "entries": [e.to_dict() for e in self.entries],
            "plan_blocking_resource_ids": list(self.plan_blocking_resource_ids),
            "missing_resource_ids": list(self.missing_resource_ids),
        }


def resource_blocker_mapping(
    plan: Plan,
    goals: Sequence[GoalContract],
    registry: ResourceRegistry,
) -> ResourceBlockerMapping:
    """Compute the resource blocker mapping for a plan and its goals.

    Pure and deterministic: every goal node of the DAG (and the plan itself)
    maps to the resource gaps that block it, decided by the
    ``BLOCKER_RULES`` table from the registered resource state -- the same
    state always yields the same mapping. A goal's scientific acceptance
    never participates and is never altered (AC-02).

    Args:
        plan: the plan record (its ``resource_ids`` are the plan-level
            edges).
        goals: the goal contracts to map (the DAG nodes), any order.
        registry: the registered resource state.

    Raises:
        TypeError: ``plan`` is not a ``Plan``, ``goals`` is not a sequence
            of ``GoalContract`` (a ``str``/``bytes`` is rejected
            explicitly), or ``registry`` is not a ``ResourceRegistry``.
    """
    if not isinstance(plan, Plan):
        raise TypeError(
            f"resource_blocker_mapping expects a Plan, got"
            f" {type(plan).__name__}"
        )
    if isinstance(goals, (str, bytes)) or not isinstance(goals, Sequence):
        raise TypeError(
            "resource_blocker_mapping expects a sequence of GoalContract,"
            f" got {type(goals).__name__}"
        )
    goals_tuple = tuple(goals)
    for goal in goals_tuple:
        if not isinstance(goal, GoalContract):
            raise TypeError(
                "resource_blocker_mapping expects GoalContract elements,"
                f" got {type(goal).__name__}"
            )
    if not isinstance(registry, ResourceRegistry):
        raise TypeError(
            "resource_blocker_mapping expects a ResourceRegistry, got"
            f" {type(registry).__name__}"
        )
    entries = tuple(
        resource_blockers_for_goal(goal, registry)
        for goal in sorted(goals_tuple, key=lambda g: g.goal_id)
    )
    by_id = {r.resource_id: r for r in registry.resources}
    plan_blocking = sorted(
        r for r in plan.resource_ids if is_resource_gap(by_id.get(r))
    )
    goal_missing = {
        rid for entry in entries for rid in entry.missing_resource_ids
    }
    plan_missing = {r for r in plan.resource_ids if r not in by_id}
    return ResourceBlockerMapping(
        entries=entries,
        plan_blocking_resource_ids=tuple(plan_blocking),
        missing_resource_ids=tuple(sorted(goal_missing | plan_missing)),
    )


# ---------------------------------------------------------------------------
# Plan DAG (nodes, edges, order, cycles) and the export
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnresolvedDependencyRef:
    """A dependency edge whose target has no registered goal contract.

    Reported explicitly (never silently dropped -- the M4-G02 convention):
    a node goal declares a dependency on ``dependency_goal_id`` but no
    record with that id exists in the goals registry, so the edge cannot
    render.
    """

    dependent_goal_id: str
    dependency_goal_id: str

    def to_dict(self) -> dict[str, str]:
        """The canonical mapping of this reference."""
        return {
            "dependent_goal_id": self.dependent_goal_id,
            "dependency_goal_id": self.dependency_goal_id,
        }


@dataclass(frozen=True)
class DAGEdge:
    """One dependency edge of the plan DAG (AC-03).

    Edges render dependency-first: ``dependency_goal_id`` (the upstream)
    must be reached before ``dependent_goal_id`` (the downstream), so
    ``topological_order`` is ready-first. The raw model fields (``type``,
    ``execution_gate``, ``acceptance_gate``) are preserved verbatim and
    ``gate_kind`` is the six-kind classification of
    :func:`classify_gate_kind` (strength x governance axis), with the full
    rule trace in ``gate_kind_assessment``.
    """

    dependency_goal_id: str
    dependent_goal_id: str
    dependency_type: DependencyType
    execution_gate: bool
    acceptance_gate: bool
    gate_kind: GateKind
    gate_kind_assessment: GateKindAssessment

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this edge (the /goals view)."""
        assessment = self.gate_kind_assessment
        return {
            "dependency_goal_id": self.dependency_goal_id,
            "dependent_goal_id": self.dependent_goal_id,
            "type": self.dependency_type.value,
            "execution_gate": self.execution_gate,
            "acceptance_gate": self.acceptance_gate,
            "gate_kind": self.gate_kind.value,
            "gate_kind_assessment": {
                "ruleset_version": assessment.ruleset_version,
                "dependency_type": assessment.input.dependency_type.value,
                "execution_gate": assessment.input.execution_gate,
                "acceptance_gate": assessment.input.acceptance_gate,
                "gate_kind": assessment.gate_kind.value,
                "decisions": [
                    {
                        "rule_id": d.rule_id,
                        "axis": d.axis.value,
                        "matched": d.matched,
                    }
                    for d in assessment.decisions
                ],
                "matched_rule_id": assessment.matched_rule_id,
            },
        }


@dataclass(frozen=True)
class DAGNode:
    """One goal node of the plan DAG.

    ``goal`` is the registered contract verbatim -- including its
    ``acceptance`` sub-object, which is the scientific fact and is never
    altered by blocking (AC-02); ``in_plan`` marks whether the goal id is
    in the plan's ``goal_ids`` (nodes pulled in only through dependency
    edges are False); ``blocked`` / ``blocking_resource_ids`` /
    ``missing_resource_ids`` are the view fields of the node's
    :class:`GoalBlockers` mapping entry.
    """

    goal: GoalContract
    in_plan: bool
    blockers: GoalBlockers

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of this node (the /goals view)."""
        return {
            "goal": self.goal.to_dict(),
            "in_plan": self.in_plan,
            "blocked": self.blockers.blocked,
            "blocking_resource_ids": list(self.blockers.blocking_resource_ids),
            "missing_resource_ids": list(self.blockers.missing_resource_ids),
        }


@dataclass(frozen=True)
class PlanningDAG:
    """The plan DAG export record (the DEV-M4-G05 deliverable).

    A pure function of the registered state: the plan record (read via
    ``planning/plan.py`` ``read_plan``), the registered goal contracts
    (their ``dependencies`` are the edges) and the registered resources
    (the blocker mapping). ``nodes`` is sorted by goal id, ``edges`` by
    (dependency_goal_id, dependent_goal_id); ``topological_order`` is the
    dependency-first (ready-first) order over distinct edges, empty when
    the graph is cyclic; ``acyclic`` and ``cyclic_goal_ids`` (sorted) make
    cycle detection explicit; ``missing_goal_contracts`` lists plan goal
    ids with no registered contract; ``unresolved_dependency_refs`` lists
    dependency targets with no registered contract (sorted);
    ``blockers`` is the :class:`ResourceBlockerMapping`. ``plan`` is the
    exact stored record (version, status, resource_ids, work_packages
    verbatim -- the work-package list is authoring data the frozen models
    leave untyped, so it is carried through untouched, never interpreted).
    """

    export_version: str
    plan: Plan
    nodes: tuple[DAGNode, ...]
    edges: tuple[DAGEdge, ...]
    topological_order: tuple[str, ...]
    acyclic: bool
    cyclic_goal_ids: tuple[str, ...]
    missing_goal_contracts: tuple[str, ...]
    unresolved_dependency_refs: tuple[UnresolvedDependencyRef, ...]
    blockers: ResourceBlockerMapping

    def to_dict(self) -> dict[str, Any]:
        """The canonical mapping of the export (the /goals view payload)."""
        return {
            "export_version": self.export_version,
            "plan": self.plan.to_dict(),
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "topological_order": list(self.topological_order),
            "acyclic": self.acyclic,
            "cyclic_goal_ids": list(self.cyclic_goal_ids),
            "missing_goal_contracts": list(self.missing_goal_contracts),
            "unresolved_dependency_refs": [
                ref.to_dict() for ref in self.unresolved_dependency_refs
            ],
            "blockers": self.blockers.to_dict(),
        }


def build_plan_dag(root: str | Path, version: str) -> PlanningDAG:
    """Build the plan DAG export from the registered state.

    Pure and deterministic: the DAG is a pure function of the registered
    state at ``root`` -- the plan record at ``version`` (``read_plan``),
    the registered goal contracts (``list_goals``) and the registered
    resources (``load_resource_registry``). The same state always yields
    the identical export. Nodes are the plan's goals plus every registered
    goal reachable through dependency edges; edges render dependency-first
    with the six-kind gate classification; the topological order is
    deterministic (Kahn's algorithm, sorted ready set); cycles are
    reported, never raised; the resource blocker mapping attaches to every
    node and the plan (AC-02). Nothing is persisted -- views consume the
    export record or its canonical JSON (``plan_dag_to_dict`` /
    ``export_plan_dag``).

    Args:
        root: the initialized workspace root.
        version: the plan version to export (``v<N>`` or ``v<N>-draft``).

    Returns:
        The frozen :class:`PlanningDAG` export record.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``version`` is not a str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidPlanVersionError: ``version`` is not ``v<N>`` /
            ``v<N>-draft``.
        PlanNotFoundError: no plan record with that version is registered.
        ValueError: a stored plan, goal or resource record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    project_root = Path(root).resolve()
    plan = read_plan(project_root, version)
    goals = list_goals(project_root)
    registry = load_resource_registry(project_root)
    by_id = {g.goal_id: g for g in goals}

    plan_node_ids = sorted(
        gid for gid in plan.goal_ids if gid in by_id
    )
    missing_goal_contracts = tuple(
        sorted(gid for gid in plan.goal_ids if gid not in by_id)
    )

    # Node set: the plan's goals plus the transitive closure over registered
    # dependency targets (an edge must render, so a registered upstream is a
    # node even when the plan does not cover it).
    node_ids = set(plan_node_ids)
    changed = True
    while changed:
        changed = False
        for gid in sorted(node_ids):
            for dependency in by_id[gid].dependencies:
                if (
                    dependency.goal_id in by_id
                    and dependency.goal_id not in node_ids
                ):
                    node_ids.add(dependency.goal_id)
                    changed = True
    sorted_node_ids = tuple(sorted(node_ids))

    node_goals = tuple(by_id[gid] for gid in sorted_node_ids)
    mapping = resource_blocker_mapping(plan, node_goals, registry)
    blockers_by_goal = {entry.goal_id: entry for entry in mapping.entries}

    edges: list[DAGEdge] = []
    unresolved: list[UnresolvedDependencyRef] = []
    for gid in sorted_node_ids:
        for dependency in by_id[gid].dependencies:
            if dependency.goal_id not in node_ids:
                unresolved.append(
                    UnresolvedDependencyRef(
                        dependent_goal_id=gid,
                        dependency_goal_id=dependency.goal_id,
                    )
                )
                continue
            assessment = classify_gate_kind(
                dependency.type,
                dependency.execution_gate,
                dependency.acceptance_gate,
            )
            edges.append(
                DAGEdge(
                    dependency_goal_id=dependency.goal_id,
                    dependent_goal_id=gid,
                    dependency_type=dependency.type,
                    execution_gate=dependency.execution_gate,
                    acceptance_gate=dependency.acceptance_gate,
                    gate_kind=assessment.gate_kind,
                    gate_kind_assessment=assessment,
                )
            )
    edges.sort(key=lambda e: (e.dependency_goal_id, e.dependent_goal_id))
    unresolved.sort(
        key=lambda r: (r.dependent_goal_id, r.dependency_goal_id)
    )

    order, cyclic = _topological_sort(sorted_node_ids, tuple(edges))
    nodes = tuple(
        DAGNode(
            goal=by_id[gid],
            in_plan=gid in plan_node_ids,
            blockers=blockers_by_goal[gid],
        )
        for gid in sorted_node_ids
    )
    return PlanningDAG(
        export_version=DAG_EXPORT_VERSION,
        plan=plan,
        nodes=nodes,
        edges=tuple(edges),
        topological_order=tuple(order),
        acyclic=not cyclic,
        cyclic_goal_ids=tuple(cyclic),
        missing_goal_contracts=missing_goal_contracts,
        unresolved_dependency_refs=tuple(unresolved),
        blockers=mapping,
    )


def plan_dag_to_dict(dag: PlanningDAG) -> dict[str, Any]:
    """Return the canonical mapping of a plan DAG export.

    Raises:
        TypeError: ``dag`` is not a ``PlanningDAG``.
    """
    if not isinstance(dag, PlanningDAG):
        raise TypeError(
            f"plan_dag_to_dict expects a PlanningDAG, got"
            f" {type(dag).__name__}"
        )
    return dag.to_dict()


def export_plan_dag(root: str | Path, version: str) -> str:
    """Export the plan DAG as canonical JSON (the /goals view payload).

    Canonical form: sorted keys, 2-space indent, trailing newline -- the
    exact serialization of the M4-G04 plan registry, so /goals views can
    compare exports byte-for-byte.

    Args:
        root: the initialized workspace root.
        version: the plan version to export.

    Returns:
        The canonical JSON text of the :class:`PlanningDAG` export.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``version`` is not a str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidPlanVersionError: ``version`` is not ``v<N>`` /
            ``v<N>-draft``.
        PlanNotFoundError: no plan record with that version is registered.
        ValueError: a stored plan, goal or resource record is corrupt.
    """
    return (
        json.dumps(plan_dag_to_dict(build_plan_dag(root, version)),
                   indent=_JSON_INDENT, sort_keys=True)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _topological_sort(
    node_ids: tuple[str, ...], edges: tuple[DAGEdge, ...]
) -> tuple[list[str], list[str]]:
    """Deterministic Kahn's algorithm over the distinct edges.

    Returns ``(order, cyclic_ids)``: ``order`` is the dependency-first
    (ready-first) order -- a goal's dependencies precede it -- produced by
    always taking the smallest ready node, so the order is deterministic
    for a given edge set; ``cyclic_ids`` is the sorted set of nodes that
    remain after the algorithm (empty iff the graph is acyclic). Duplicate
    declared edges count once (the same dependency declared twice does not
    double the constraint).
    """
    in_degree: dict[str, int] = {gid: 0 for gid in node_ids}
    dependents: dict[str, set[str]] = {gid: set() for gid in node_ids}
    seen: set[tuple[str, str]] = set()
    for edge in edges:
        pair = (edge.dependency_goal_id, edge.dependent_goal_id)
        if (
            pair[0] in in_degree
            and pair[1] in in_degree
            and pair not in seen
        ):
            seen.add(pair)
            in_degree[pair[1]] += 1
            dependents[pair[0]].add(pair[1])
    ready = sorted(gid for gid, degree in in_degree.items() if degree == 0)
    order: list[str] = []
    remaining = set(node_ids)
    while ready:
        gid = ready.pop(0)
        order.append(gid)
        remaining.discard(gid)
        for downstream in sorted(dependents[gid]):
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                ready.append(downstream)
                ready.sort()
    return order, sorted(remaining)
