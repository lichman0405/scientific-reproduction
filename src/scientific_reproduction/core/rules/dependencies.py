"""Dependency, execution-gate and acceptance-gate evaluation rules (DEV-M2-G02).

Pure logic implementing the frozen dependency semantics of ``05-GOAL-RUN-SCHEMA.md``
section 5 against the frozen dependency item of ``schemas/goal.schema.yaml``
(model: ``core.models.GoalDependency`` / ``core.models.DependencyType``).
No LLM, no randomness, no wall-clock dependence: the same inputs always
yield the same assessments on every platform and Python version.

Normative sources (all frozen)
------------------------------
* ``05-GOAL-RUN-SCHEMA.md`` section 5 -- dependency semantics must include
  ``hard_gate`` / ``soft_dependency`` / ``informational``; a dependency may
  specify separately ``execution_gate`` ("must upstream state be reached
  before execution starts?") and ``acceptance_gate`` ("must upstream evidence
  be valid before this Goal may close?"); the split "allows safe parallelism
  without invalidating final evidence".
* ``schemas/goal.schema.yaml`` -- the dependency item shape: ``goal_id``,
  ``type`` in ``{hard_gate, soft_dependency, informational}``, optional
  ``execution_gate`` / ``acceptance_gate`` booleans (default ``false``).
* ``17-FDM201-REFERENCE-CASE.md`` -- soft dependency example ("PXRD can begin
  on one portion while solvent exchange/activation preparation proceeds as a
  soft dependency") and hard acceptance-gate example ("BET acceptance may
  require a hard acceptance gate on sample identity even if measurement
  execution was started earlier").
* ``20-ARCHITECTURE-DECISIONS.md`` decision 25 -- "Goal dependencies have
  hard/soft/informational semantics and separate execution/acceptance gates".
* ``core/models.py`` -- the frozen ``DependencyType`` enum and
  ``GoalDependency`` model (``execution_gate`` / ``acceptance_gate`` default
  ``False``); nothing is invented here.

Normative readings (the spec leaves these open; the readings are locked here
and asserted bi-implicationally over the exhaustive grid in the tests)
-----------------------------------------------------------------------
* The two gate flags declare *which axes* a dependency gates. Only a
  ``hard_gate`` dependency converts an unresolved gated axis into a block:
  AC-01 ("hard gate blocks execution when unresolved") is exactly
  ``hard_gate`` + ``execution_gate`` + upstream execution state unreached.
* ``soft_dependency`` never blocks any axis: an unresolved gated axis of a
  soft dependency is recorded as ``ORDERING_ONLY`` -- a best-effort ordering
  hint a scheduler may use, never a serialization constraint (AC-02: soft
  dependencies must not incorrectly serialize the DAG).
  ``informational`` never blocks and never influences ordering either: the
  gate flags are inert for the informational kind and the dependency is
  recorded as ``INFORMATIONAL``.
* A ``hard_gate`` dependency whose ``execution_gate`` / ``acceptance_gate``
  flags are both ``False`` gates nothing and is ``SATISFIED`` by definition
  (the schema allows this shape; it contributes nothing to either gate).
* Upstream resolution is evaluated per axis, mirroring the spec's two
  questions: ``execution_resolved`` = the upstream state required for
  execution has been reached; ``acceptance_resolved`` = the upstream evidence
  is valid. The axes are independent, so acceptance can stay blocked after
  execution is allowed (AC-03) -- including the FDM-201 BET case where
  measurement execution started earlier (execution resolved) while the hard
  acceptance gate on sample identity is still unresolved.
* Id lists in the assessments preserve the declared dependency order for
  auditable traces; gate *outcomes* are order-independent (asserted over the
  exhaustive grid in the tests).

Rule model
----------
Three deliverables, one per frozen acceptance axis of DEV-M2-G02, each an
ordered rule table (first match wins) with a trailing default rule so
evaluation is total:

1. **Dependency evaluator** -- ``evaluate_dependency`` maps one dependency
   record to a total ``DependencyState`` through ``DEPENDENCY_STATE_RULES``:
   ``R-DEP-1`` both gated axes unresolved (hard) -> BLOCKS_EXECUTION_AND_ACCEPTANCE;
   ``R-DEP-2`` execution axis unresolved (hard) -> BLOCKS_EXECUTION;
   ``R-DEP-3`` acceptance axis unresolved (hard) -> BLOCKS_ACCEPTANCE;
   ``R-DEP-4`` soft with an unresolved gated axis -> ORDERING_ONLY;
   ``R-DEP-5`` informational -> INFORMATIONAL;
   ``R-DEP-6`` default -> SATISFIED.
2. **Execution gate evaluator** -- ``evaluate_execution_gate`` runs the
   per-dependency ``EXECUTION_GATE_RULES`` over the dependency states
   (``R-EXEC-1``: BLOCKS_EXECUTION / BLOCKS_EXECUTION_AND_ACCEPTANCE states
   block execution; ``R-EXEC-2`` default) and then the aggregate
   ``EXECUTION_OUTCOME_RULES`` (``R-EXEC-G-1``: any blocking dependency ->
   BLOCKED; ``R-EXEC-G-2`` default -> ALLOWED).
3. **Acceptance gate evaluator** -- ``evaluate_acceptance_gate`` runs the
   per-dependency ``ACCEPTANCE_GATE_RULES`` (``R-ACC-1``: BLOCKS_ACCEPTANCE /
   BLOCKS_EXECUTION_AND_ACCEPTANCE states block acceptance; ``R-ACC-2``
   default) and the aggregate ``ACCEPTANCE_OUTCOME_RULES`` (``R-ACC-G-1``:
   any blocking dependency -> BLOCKED; ``R-ACC-G-2`` default -> ALLOWED).

Every assessment records its exact inputs, every rule decision of every
table it consulted, and the id of the matched rule, so any gate outcome is
reproducible and auditable (M2 milestone acceptance: rules are auditable).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from scientific_reproduction.core.models import DependencyType, GoalDependency

__all__ = [
    "RULESET_VERSION",
    # errors
    "DependencyRulesError",
    "DependencyRecordError",
    # input model
    "DependencyRecord",
    # dependency evaluator
    "DependencyState",
    "DependencyStateRule",
    "DependencyStateDecision",
    "DependencyAssessment",
    "DEPENDENCY_STATE_RULES",
    "evaluate_dependency",
    # execution gate evaluator
    "ExecutionGateOutcome",
    "ExecutionGateRule",
    "ExecutionGateDecision",
    "ExecutionGateOutcomeRule",
    "ExecutionGateOutcomeDecision",
    "ExecutionGateAssessment",
    "EXECUTION_GATE_RULES",
    "EXECUTION_OUTCOME_RULES",
    "evaluate_execution_gate",
    # acceptance gate evaluator
    "AcceptanceGateOutcome",
    "AcceptanceGateRule",
    "AcceptanceGateDecision",
    "AcceptanceGateOutcomeRule",
    "AcceptanceGateOutcomeDecision",
    "AcceptanceGateAssessment",
    "ACCEPTANCE_GATE_RULES",
    "ACCEPTANCE_OUTCOME_RULES",
    "evaluate_acceptance_gate",
]

#: Version of the rule tables. Bumped whenever a mapping changes; recorded in
#: every assessment so old gate outcomes stay interpretable.
RULESET_VERSION: str = "1.0"


class DependencyRulesError(ValueError):
    """Base error for the dependency/gate rule engine."""


class DependencyRecordError(DependencyRulesError):
    """Raised when a dependency record violates the frozen input shape.

    Covers empty goal ids, non-boolean gate/resolution flags and a
    ``dependency_type`` that is not a ``DependencyType`` member: a malformed
    record cannot silently change a gate outcome, so it is rejected up front
    with a stable message.
    """


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------

#: Canonical field order of a ``DependencyRecord`` (also its to_dict order).
RECORD_FIELDS: tuple[str, ...] = (
    "goal_id",
    "dependency_type",
    "execution_gate",
    "acceptance_gate",
    "execution_resolved",
    "acceptance_resolved",
)


@dataclass(frozen=True)
class DependencyRecord:
    """One goal dependency plus its upstream resolution state.

    Mirrors ``schemas/goal.schema.yaml`` (``goal_id``, ``type``,
    ``execution_gate``, ``acceptance_gate``) and adds the two-axis upstream
    resolution state answering the spec's two gate questions
    (``05-GOAL-RUN-SCHEMA.md`` section 5):

    * ``execution_resolved`` -- the upstream state required before execution
      starts has been reached;
    * ``acceptance_resolved`` -- the upstream evidence is valid.

    The frozen dataclass makes a record hashable and comparable, so "same
    record -> same gate outcomes" is directly testable and the exact input is
    preserved in every assessment (auditability).

    Raises:
        DependencyRecordError: ``goal_id`` is not a non-empty string,
            ``dependency_type`` is not a ``DependencyType`` member, or a
            gate/resolution flag is not a bool.
    """

    goal_id: str
    dependency_type: DependencyType
    execution_gate: bool = False
    acceptance_gate: bool = False
    execution_resolved: bool = False
    acceptance_resolved: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.goal_id, str) or not self.goal_id.strip():
            raise DependencyRecordError(
                "DependencyRecord.goal_id must be a non-empty string, got"
                f" {self.goal_id!r}"
            )
        if not isinstance(self.dependency_type, DependencyType):
            raise DependencyRecordError(
                "DependencyRecord.dependency_type must be a DependencyType"
                f" member, got {self.dependency_type!r}"
            )
        for name in (
            "execution_gate",
            "acceptance_gate",
            "execution_resolved",
            "acceptance_resolved",
        ):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise DependencyRecordError(
                    f"DependencyRecord.{name} must be a bool, got"
                    f" {type(value).__name__}"
                )

    @classmethod
    def from_goal_dependency(
        cls,
        dependency: GoalDependency,
        *,
        execution_resolved: bool = False,
        acceptance_resolved: bool = False,
    ) -> "DependencyRecord":
        """Build a record from the frozen ``GoalDependency`` model.

        ``goal_id``, ``type`` and the gate flags come from the model; the
        upstream resolution state is supplied separately (the frozen model
        carries no resolution state, and the evaluator must not invent one).

        Raises:
            TypeError: ``dependency`` is not a ``GoalDependency``.
            DependencyRecordError: the model's fields violate the record
                shape (cannot happen for a schema-valid model).
        """
        if not isinstance(dependency, GoalDependency):
            raise TypeError(
                "from_goal_dependency expects a GoalDependency, got"
                f" {type(dependency).__name__}"
            )
        return cls(
            goal_id=dependency.goal_id,
            dependency_type=dependency.type,
            execution_gate=dependency.execution_gate,
            acceptance_gate=dependency.acceptance_gate,
            execution_resolved=execution_resolved,
            acceptance_resolved=acceptance_resolved,
        )

    def to_dict(self) -> dict[str, bool | str]:
        """Plain dict of the record in canonical field order."""
        return {
            name: (
                self.dependency_type.value
                if name == "dependency_type"
                else getattr(self, name)
            )
            for name in RECORD_FIELDS
        }


# ---------------------------------------------------------------------------
# Dependency evaluator (deliverable: dependency evaluator)
# ---------------------------------------------------------------------------


class DependencyState(StrEnum):
    """Total per-dependency classification (see module docstring).

    ``BLOCKS_EXECUTION_AND_ACCEPTANCE`` is a distinct state so the audit
    trail records that a dependency holds *both* gates; the gate evaluators
    decompose it through their own ordered rule tables.
    """

    BLOCKS_EXECUTION = "BLOCKS_EXECUTION"
    BLOCKS_ACCEPTANCE = "BLOCKS_ACCEPTANCE"
    BLOCKS_EXECUTION_AND_ACCEPTANCE = "BLOCKS_EXECUTION_AND_ACCEPTANCE"
    ORDERING_ONLY = "ORDERING_ONLY"
    INFORMATIONAL = "INFORMATIONAL"
    SATISFIED = "SATISFIED"


@dataclass(frozen=True)
class DependencyStateRule:
    """One entry of the ordered dependency-state rule table."""

    rule_id: str
    description: str
    state: DependencyState
    predicate: Callable[[DependencyRecord], bool]


@dataclass(frozen=True)
class DependencyStateDecision:
    """Record of one dependency-state rule evaluation (audit trail)."""

    rule_id: str
    description: str
    state: DependencyState
    matched: bool


@dataclass(frozen=True)
class DependencyAssessment:
    """Full, auditable result of one dependency evaluation.

    ``dependency`` is the exact input record; ``decisions`` records the
    outcome of every rule in the table (in evaluation order);
    ``matched_rule_id`` names the rule that decided the state (``None`` is
    impossible: the trailing default rule always matches).
    """

    ruleset_version: str
    dependency: DependencyRecord
    state: DependencyState
    decisions: tuple[DependencyStateDecision, ...]
    matched_rule_id: str


#: The ordered dependency-state rule table. First match wins; order is
#: normative (see module docstring): the combined state must be decided
#: before either single-axis rule, and the non-blocking kinds before the
#: default. Predicates are pure functions of the record only.
DEPENDENCY_STATE_RULES: tuple[DependencyStateRule, ...] = (
    DependencyStateRule(
        rule_id="R-DEP-1",
        description=(
            "hard dependency with both gated axes unresolved: execution and "
            "acceptance are both blocked"
        ),
        state=DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE,
        predicate=lambda d: (
            d.dependency_type is DependencyType.HARD_GATE
            and d.execution_gate
            and not d.execution_resolved
            and d.acceptance_gate
            and not d.acceptance_resolved
        ),
    ),
    DependencyStateRule(
        rule_id="R-DEP-2",
        description=(
            "hard dependency whose execution-gated axis is unresolved: "
            "execution is blocked (AC-01)"
        ),
        state=DependencyState.BLOCKS_EXECUTION,
        predicate=lambda d: (
            d.dependency_type is DependencyType.HARD_GATE
            and d.execution_gate
            and not d.execution_resolved
        ),
    ),
    DependencyStateRule(
        rule_id="R-DEP-3",
        description=(
            "hard dependency whose acceptance-gated axis is unresolved: "
            "acceptance is blocked"
        ),
        state=DependencyState.BLOCKS_ACCEPTANCE,
        predicate=lambda d: (
            d.dependency_type is DependencyType.HARD_GATE
            and d.acceptance_gate
            and not d.acceptance_resolved
        ),
    ),
    DependencyStateRule(
        rule_id="R-DEP-4",
        description=(
            "soft dependency with an unresolved gated axis: recorded as an "
            "ordering hint, never a block (AC-02)"
        ),
        state=DependencyState.ORDERING_ONLY,
        predicate=lambda d: (
            d.dependency_type is DependencyType.SOFT_DEPENDENCY
            and (
                (d.execution_gate and not d.execution_resolved)
                or (d.acceptance_gate and not d.acceptance_resolved)
            )
        ),
    ),
    DependencyStateRule(
        rule_id="R-DEP-5",
        description=(
            "informational dependency: recorded only, no gating and no "
            "ordering influence (AC-02)"
        ),
        state=DependencyState.INFORMATIONAL,
        predicate=lambda d: d.dependency_type is DependencyType.INFORMATIONAL,
    ),
    DependencyStateRule(
        rule_id="R-DEP-6",
        description=(
            "all gated axes resolved, or the dependency declares no gate on "
            "any axis (default)"
        ),
        state=DependencyState.SATISFIED,
        predicate=lambda d: True,
    ),
)


def evaluate_dependency(dependency: DependencyRecord) -> DependencyAssessment:
    """Evaluate one dependency record into its total dependency state.

    Pure and deterministic: the state is a pure function of the record
    (AC-02 determinism of DEV-M2-G02). The returned
    :class:`DependencyAssessment` records the exact input record and every
    rule decision, so any state is reproducible and auditable.

    Args:
        dependency: the dependency record to evaluate.

    Raises:
        TypeError: ``dependency`` is not a ``DependencyRecord``.

    Returns:
        The full assessment: state plus the auditable trace.
    """
    if not isinstance(dependency, DependencyRecord):
        raise TypeError(
            "evaluate_dependency expects a DependencyRecord, got"
            f" {type(dependency).__name__}"
        )
    decisions: list[DependencyStateDecision] = []
    matched_rule_id: str | None = None
    matched_state = DependencyState.SATISFIED  # unreachable default
    for rule in DEPENDENCY_STATE_RULES:
        matched = rule.predicate(dependency)
        decisions.append(
            DependencyStateDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                state=rule.state,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_state = rule.state
    # R-DEP-6 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return DependencyAssessment(
        ruleset_version=RULESET_VERSION,
        dependency=dependency,
        state=matched_state,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Execution gate evaluator (deliverable: execution gate evaluator)
# ---------------------------------------------------------------------------


class ExecutionGateOutcome(StrEnum):
    """Outcome of the execution gate over a dependency set."""

    BLOCKED = "BLOCKED"
    ALLOWED = "ALLOWED"


@dataclass(frozen=True)
class ExecutionGateRule:
    """One entry of the ordered per-dependency execution-gate rule table.

    The predicate runs on the dependency's evaluated state (not on the raw
    record), so the dependency evaluator stays the single normative mapping
    from records to blocking semantics.
    """

    rule_id: str
    description: str
    blocks_execution: bool
    predicate: Callable[[DependencyState], bool]


@dataclass(frozen=True)
class ExecutionGateDecision:
    """Per-dependency execution-gate decision (audit trail)."""

    dependency: DependencyRecord
    state: DependencyState
    blocks_execution: bool
    rule_id: str
    description: str


@dataclass(frozen=True)
class ExecutionGateOutcomeRule:
    """One entry of the ordered aggregate execution-gate rule table."""

    rule_id: str
    description: str
    outcome: ExecutionGateOutcome
    predicate: Callable[[tuple[ExecutionGateDecision, ...]], bool]


@dataclass(frozen=True)
class ExecutionGateOutcomeDecision:
    """Record of one aggregate execution-gate rule evaluation."""

    rule_id: str
    description: str
    outcome: ExecutionGateOutcome
    matched: bool


@dataclass(frozen=True)
class ExecutionGateAssessment:
    """Full, auditable result of an execution-gate evaluation (AC-01).

    ``dependencies`` is the exact input (a tuple, so the assessment is
    hashable and comparable); ``decisions`` records, in input order, the
    per-dependency gate decision for every dependency; ``outcome_decisions``
    records the aggregate rule table; ``blocking_goal_ids`` lists the
    upstream goals whose unresolved hard execution-gated dependency blocks
    execution (in declared order); ``pending_non_blocking_goal_ids`` lists
    dependencies in ``ORDERING_ONLY`` state -- best-effort ordering hints a
    scheduler may use, never blocks (AC-02).
    """

    ruleset_version: str
    dependencies: tuple[DependencyRecord, ...]
    outcome: ExecutionGateOutcome
    decisions: tuple[ExecutionGateDecision, ...]
    outcome_decisions: tuple[ExecutionGateOutcomeDecision, ...]
    blocking_goal_ids: tuple[str, ...]
    pending_non_blocking_goal_ids: tuple[str, ...]
    matched_rule_id: str

    @property
    def execution_allowed(self) -> bool:
        """True exactly when the execution gate outcome is ALLOWED."""
        return self.outcome is ExecutionGateOutcome.ALLOWED


#: The ordered per-dependency execution-gate rule table. First match wins.
#: ``R-EXEC-1`` matches the two blocking dependency states; ``R-EXEC-2`` is
#: the default, so every dependency yields exactly one decision.
EXECUTION_GATE_RULES: tuple[ExecutionGateRule, ...] = (
    ExecutionGateRule(
        rule_id="R-EXEC-1",
        description=(
            "dependency state blocks execution (BLOCKS_EXECUTION or "
            "BLOCKS_EXECUTION_AND_ACCEPTANCE): a hard execution-gated axis "
            "is unresolved (AC-01)"
        ),
        blocks_execution=True,
        predicate=lambda state: state
        in (
            DependencyState.BLOCKS_EXECUTION,
            DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE,
        ),
    ),
    ExecutionGateRule(
        rule_id="R-EXEC-2",
        description=(
            "dependency does not block execution: resolved hard gate, soft "
            "or informational dependency (default)"
        ),
        blocks_execution=False,
        predicate=lambda state: True,
    ),
)


#: The ordered aggregate execution-gate rule table. ``R-EXEC-G-1`` matches
#: when any dependency blocks execution; ``R-EXEC-G-2`` is the default, so
#: the outcome is total.
EXECUTION_OUTCOME_RULES: tuple[ExecutionGateOutcomeRule, ...] = (
    ExecutionGateOutcomeRule(
        rule_id="R-EXEC-G-1",
        description=(
            "at least one hard execution-gated dependency is unresolved: "
            "execution is blocked (AC-01)"
        ),
        outcome=ExecutionGateOutcome.BLOCKED,
        predicate=lambda decisions: any(d.blocks_execution for d in decisions),
    ),
    ExecutionGateOutcomeRule(
        rule_id="R-EXEC-G-2",
        description=(
            "no hard execution-gated dependency is unresolved: execution is "
            "allowed (default)"
        ),
        outcome=ExecutionGateOutcome.ALLOWED,
        predicate=lambda decisions: True,
    ),
)


def evaluate_execution_gate(
    dependencies: Sequence[DependencyRecord],
) -> ExecutionGateAssessment:
    """Evaluate the execution gate over a dependency set (AC-01/AC-02).

    Execution is allowed exactly when no dependency blocks it; only a
    ``hard_gate`` dependency with ``execution_gate`` set and its execution
    axis unresolved can block (AC-01). Soft and informational dependencies
    never block -- an unresolved soft dependency is recorded as a
    non-blocking ordering hint (``pending_non_blocking_goal_ids``), so it
    must not serialize the DAG (AC-02). Dependencies are processed in the
    declared order (auditable), while the outcome itself is
    order-independent.

    Args:
        dependencies: the goal's dependency records, in declared order.

    Raises:
        TypeError: ``dependencies`` is not a sequence, or an element is not
            a ``DependencyRecord``.

    Returns:
        The full assessment: outcome, per-dependency decisions, aggregate
        decisions and blocking/pending id lists.
    """
    items = _coerce_records(dependencies, "evaluate_execution_gate")
    decisions: list[ExecutionGateDecision] = []
    for item in items:
        state = evaluate_dependency(item).state
        matched_rule = EXECUTION_GATE_RULES[-1]  # unreachable default
        for rule in EXECUTION_GATE_RULES:
            if rule.predicate(state):
                matched_rule = rule
                break
        decisions.append(
            ExecutionGateDecision(
                dependency=item,
                state=state,
                blocks_execution=matched_rule.blocks_execution,
                rule_id=matched_rule.rule_id,
                description=matched_rule.description,
            )
        )
    outcome_decisions: list[ExecutionGateOutcomeDecision] = []
    matched_outcome_rule_id: str | None = None
    matched_outcome = ExecutionGateOutcome.ALLOWED  # unreachable default
    for outcome_rule in EXECUTION_OUTCOME_RULES:
        matched = outcome_rule.predicate(tuple(decisions))
        outcome_decisions.append(
            ExecutionGateOutcomeDecision(
                rule_id=outcome_rule.rule_id,
                description=outcome_rule.description,
                outcome=outcome_rule.outcome,
                matched=matched,
            )
        )
        if matched and matched_outcome_rule_id is None:
            matched_outcome_rule_id = outcome_rule.rule_id
            matched_outcome = outcome_rule.outcome
    # R-EXEC-G-2 (default) always matches, so this can never be None.
    assert matched_outcome_rule_id is not None
    return ExecutionGateAssessment(
        ruleset_version=RULESET_VERSION,
        dependencies=items,
        outcome=matched_outcome,
        decisions=tuple(decisions),
        outcome_decisions=tuple(outcome_decisions),
        blocking_goal_ids=tuple(
            decision.dependency.goal_id
            for decision in decisions
            if decision.blocks_execution
        ),
        pending_non_blocking_goal_ids=tuple(
            decision.dependency.goal_id
            for decision in decisions
            if decision.state is DependencyState.ORDERING_ONLY
        ),
        matched_rule_id=matched_outcome_rule_id,
    )


# ---------------------------------------------------------------------------
# Acceptance gate evaluator (deliverable: acceptance gate evaluator)
# ---------------------------------------------------------------------------


class AcceptanceGateOutcome(StrEnum):
    """Outcome of the acceptance gate over a dependency set."""

    BLOCKED = "BLOCKED"
    ALLOWED = "ALLOWED"


@dataclass(frozen=True)
class AcceptanceGateRule:
    """One entry of the ordered per-dependency acceptance-gate rule table.

    As with the execution gate, the predicate runs on the dependency's
    evaluated state, keeping the dependency evaluator the single normative
    mapping.
    """

    rule_id: str
    description: str
    blocks_acceptance: bool
    predicate: Callable[[DependencyState], bool]


@dataclass(frozen=True)
class AcceptanceGateDecision:
    """Per-dependency acceptance-gate decision (audit trail)."""

    dependency: DependencyRecord
    state: DependencyState
    blocks_acceptance: bool
    rule_id: str
    description: str


@dataclass(frozen=True)
class AcceptanceGateOutcomeRule:
    """One entry of the ordered aggregate acceptance-gate rule table."""

    rule_id: str
    description: str
    outcome: AcceptanceGateOutcome
    predicate: Callable[[tuple[AcceptanceGateDecision, ...]], bool]


@dataclass(frozen=True)
class AcceptanceGateOutcomeDecision:
    """Record of one aggregate acceptance-gate rule evaluation."""

    rule_id: str
    description: str
    outcome: AcceptanceGateOutcome
    matched: bool


@dataclass(frozen=True)
class AcceptanceGateAssessment:
    """Full, auditable result of an acceptance-gate evaluation (AC-03).

    ``dependencies`` is the exact input; ``decisions`` records, in input
    order, the per-dependency acceptance decision; ``outcome_decisions``
    records the aggregate rule table; ``blocking_goal_ids`` lists the
    upstream goals whose unresolved hard acceptance-gated dependency blocks
    closure (in declared order); ``pending_non_blocking_goal_ids`` lists
    soft dependencies with an unresolved gated axis -- preferred-but-not-
    required upstream evidence, never a block (AC-02).
    """

    ruleset_version: str
    dependencies: tuple[DependencyRecord, ...]
    outcome: AcceptanceGateOutcome
    decisions: tuple[AcceptanceGateDecision, ...]
    outcome_decisions: tuple[AcceptanceGateOutcomeDecision, ...]
    blocking_goal_ids: tuple[str, ...]
    pending_non_blocking_goal_ids: tuple[str, ...]
    matched_rule_id: str

    @property
    def acceptance_allowed(self) -> bool:
        """True exactly when the acceptance gate outcome is ALLOWED."""
        return self.outcome is AcceptanceGateOutcome.ALLOWED


#: The ordered per-dependency acceptance-gate rule table. ``R-ACC-1``
#: matches the two blocking dependency states; ``R-ACC-2`` is the default,
#: so every dependency yields exactly one decision.
ACCEPTANCE_GATE_RULES: tuple[AcceptanceGateRule, ...] = (
    AcceptanceGateRule(
        rule_id="R-ACC-1",
        description=(
            "dependency state blocks acceptance (BLOCKS_ACCEPTANCE or "
            "BLOCKS_EXECUTION_AND_ACCEPTANCE): a hard acceptance-gated axis "
            "is unresolved"
        ),
        blocks_acceptance=True,
        predicate=lambda state: state
        in (
            DependencyState.BLOCKS_ACCEPTANCE,
            DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE,
        ),
    ),
    AcceptanceGateRule(
        rule_id="R-ACC-2",
        description=(
            "dependency does not block acceptance: resolved hard gate, soft "
            "or informational dependency (default)"
        ),
        blocks_acceptance=False,
        predicate=lambda state: True,
    ),
)


#: The ordered aggregate acceptance-gate rule table. ``R-ACC-G-1`` matches
#: when any dependency blocks acceptance; ``R-ACC-G-2`` is the default, so
#: the outcome is total.
ACCEPTANCE_OUTCOME_RULES: tuple[AcceptanceGateOutcomeRule, ...] = (
    AcceptanceGateOutcomeRule(
        rule_id="R-ACC-G-1",
        description=(
            "at least one hard acceptance-gated dependency is unresolved: "
            "acceptance remains blocked"
        ),
        outcome=AcceptanceGateOutcome.BLOCKED,
        predicate=lambda decisions: any(d.blocks_acceptance for d in decisions),
    ),
    AcceptanceGateOutcomeRule(
        rule_id="R-ACC-G-2",
        description=(
            "no hard acceptance-gated dependency is unresolved: acceptance "
            "is allowed (default)"
        ),
        outcome=AcceptanceGateOutcome.ALLOWED,
        predicate=lambda decisions: True,
    ),
)


def evaluate_acceptance_gate(
    dependencies: Sequence[DependencyRecord],
) -> AcceptanceGateAssessment:
    """Evaluate the acceptance gate over a dependency set (AC-03).

    Closure is allowed exactly when no dependency blocks acceptance; only a
    ``hard_gate`` dependency with ``acceptance_gate`` set and its acceptance
    axis unresolved can block. The execution and acceptance gates are
    independent axes: an execution-eligible goal can still have unresolved
    acceptance criteria -- whether because a dependency gates only
    acceptance, or because the upstream execution state is reached while its
    evidence is not yet valid (AC-03).

    Args:
        dependencies: the goal's dependency records, in declared order.

    Raises:
        TypeError: ``dependencies`` is not a sequence, or an element is not
            a ``DependencyRecord``.

    Returns:
        The full assessment: outcome, per-dependency decisions, aggregate
        decisions and blocking/pending id lists.
    """
    items = _coerce_records(dependencies, "evaluate_acceptance_gate")
    decisions: list[AcceptanceGateDecision] = []
    for item in items:
        state = evaluate_dependency(item).state
        matched_rule = ACCEPTANCE_GATE_RULES[-1]  # unreachable default
        for rule in ACCEPTANCE_GATE_RULES:
            if rule.predicate(state):
                matched_rule = rule
                break
        decisions.append(
            AcceptanceGateDecision(
                dependency=item,
                state=state,
                blocks_acceptance=matched_rule.blocks_acceptance,
                rule_id=matched_rule.rule_id,
                description=matched_rule.description,
            )
        )
    outcome_decisions: list[AcceptanceGateOutcomeDecision] = []
    matched_outcome_rule_id: str | None = None
    matched_outcome = AcceptanceGateOutcome.ALLOWED  # unreachable default
    for outcome_rule in ACCEPTANCE_OUTCOME_RULES:
        matched = outcome_rule.predicate(tuple(decisions))
        outcome_decisions.append(
            AcceptanceGateOutcomeDecision(
                rule_id=outcome_rule.rule_id,
                description=outcome_rule.description,
                outcome=outcome_rule.outcome,
                matched=matched,
            )
        )
        if matched and matched_outcome_rule_id is None:
            matched_outcome_rule_id = outcome_rule.rule_id
            matched_outcome = outcome_rule.outcome
    # R-ACC-G-2 (default) always matches, so this can never be None.
    assert matched_outcome_rule_id is not None
    return AcceptanceGateAssessment(
        ruleset_version=RULESET_VERSION,
        dependencies=items,
        outcome=matched_outcome,
        decisions=tuple(decisions),
        outcome_decisions=tuple(outcome_decisions),
        blocking_goal_ids=tuple(
            decision.dependency.goal_id
            for decision in decisions
            if decision.blocks_acceptance
        ),
        pending_non_blocking_goal_ids=tuple(
            decision.dependency.goal_id
            for decision in decisions
            if decision.state is DependencyState.ORDERING_ONLY
        ),
        matched_rule_id=matched_outcome_rule_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_records(
    dependencies: Sequence[DependencyRecord], function: str
) -> tuple[DependencyRecord, ...]:
    """Coerce a dependency sequence into a tuple of validated records.

    Raises:
        TypeError: ``dependencies`` is not a sequence (a ``str``/``bytes``
            is rejected explicitly), or an element is not a
            ``DependencyRecord``.
    """
    if isinstance(dependencies, (str, bytes)) or not isinstance(
        dependencies, Sequence
    ):
        raise TypeError(
            f"{function} expects a sequence of DependencyRecord, got"
            f" {type(dependencies).__name__}"
        )
    items = tuple(dependencies)
    for item in items:
        if not isinstance(item, DependencyRecord):
            raise TypeError(
                f"{function} expects DependencyRecord elements, got"
                f" {type(item).__name__}"
            )
    return items
