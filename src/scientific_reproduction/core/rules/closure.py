"""Closure hard-gate evaluation rules (DEV-M2-G05).

Pure logic implementing the frozen Closure Contract of
``08-STRICT-RECOVERY-CLOSURE.md`` section 4 against the frozen
``ClosureContract`` / ``ClosureRecovery`` / ``ClosureLiterature`` models of
``schemas/closure-contract.schema.yaml`` (``core.models``). No LLM, no
randomness, no wall-clock dependence: the same inputs always yield the same
closure decision on every platform and Python version, and every decision is
recorded for the audit trail.

Normative sources (all frozen)
------------------------------
* ``08-STRICT-RECOVERY-CLOSURE.md`` section 4 -- the Closure Contract: a Goal
  may close ``NOT_REPRODUCED_WITHIN_DEFINED_SCOPE`` only when the frozen
  Closure Contract is satisfied; "Do not stop because 'N attempts failed'."
  Required categories: Statistical sufficiency, Execution validity,
  Recovery-space exhaustion (default v0.1 Recovery eligibility:
  Reliability >= 3, Directness >= 2, actionable = true -- evaluated by
  ``rules.evidence.recovery_hypothesis_eligible``) and Research saturation
  (default v0.1 operational saturation rule: two consecutive expansion search
  cycles produce zero new eligible Recovery hypotheses; "This is a governance
  rule, not a universal scientific constant; it must be configurable and
  frozen").
* ``09-RESEARCH-SUBSYSTEM.md`` section 7 -- saturation: the zero-novelty
  cycle rule applies *after all required search families have been covered*.
* ``20-ARCHITECTURE-DECISIONS.md`` decision 20 -- "Closure Contract governs
  stopping; no fixed 'N failures and stop'."
* ``18-TEST-AND-ACCEPTANCE-PLAN.md`` Scenario C and
  ``examples/fdm-201/simulated-scenarios.md`` S6 -- the non-reproduced
  closure scenario: strict failure statistically sufficient, QC valid, all
  eligible hypotheses tested/ruled out, research saturation met -> Closure
  Contract satisfied -> ``NOT_REPRODUCED``.
* ``schemas/closure-contract.schema.yaml`` + ``core/models.py`` -- the frozen
  input vocabulary (``ClosureContract``, ``ClosureRecovery``,
  ``ClosureLiterature``); nothing is invented here.
* ``04-PROJECT-LIFECYCLE.md`` section 3 -- project-level aggregation: one or
  more Critical Requirements close ``NOT_REPRODUCED`` under a satisfied
  Closure Contract.

Normative readings (the spec leaves these open; the readings are locked here
and asserted bi-implicationally over exhaustive grids in the tests)
---------------------------------------------------------------------
* The mandatory gate set is exactly the four axes named in the goal
  contract's objective: statistics sufficiency, valid execution, recovery
  hypothesis exhaustion and research saturation. The spec's fifth category
  "Diagnosis completion ... when appropriate" is conditional and is NOT one
  of the mandatory gates of this evaluator; the frozen model's free-form
  ``diagnosis`` dict is never consulted.
* ``statistical_sufficiency`` / ``execution_validity`` in the frozen schema
  are untyped free-form dicts, so the evaluator cannot derive a boolean from
  them; the caller supplies typed tri-state axis values (``True`` satisfied /
  ``False`` evaluated-and-not-satisfied / ``None`` unresolved).
  ``from_closure_contract`` never invents them.
* Closure is a hard gate that fails when ANY mandatory gate is unresolved
  (AC-01): a gate that is not satisfied -- whether evaluated-and-failing
  (``NOT_SATISFIED``) or unknown (``UNRESOLVED``) -- blocks closure. The two
  failing states are distinguished only in the reason report.
* Recovery exhaustion (AC-03): the axis is satisfied exactly when the pool is
  exhausted (``recovery_hypotheses_remaining == 0``). Any remaining eligible
  hypothesis (> 0) blocks closure and the reason reports the exact count; an
  unassessed pool (``remaining is None``) is unresolved and blocks as well.
  The auditable counts ``eligible_hypotheses_total`` / ``tested_or_ruled_out``
  are recorded but inert to the rules (the frozen schema does not require
  ``remaining == total - tested``, so no cross-field arithmetic is enforced).
* Research saturation: satisfied exactly when all required search families
  are completed AND the consecutive-zero-novelty-cycle count meets the
  configured (frozen) saturation rule (``required_zero_novelty_cycles``,
  schema default 2). Partial knowledge (either input unknown) stays
  unresolved; an evaluated shortfall (families confirmed incomplete, or a
  counted cycle run below the rule) is NOT_SATISFIED.
* The closure contract governs the stopping decision only (may this Goal
  close as not reproduced within the defined scope?); it does not classify
  the scientific outcome (REPRODUCED / NOT_REPRODUCED / ... is a review
  decision, stored separately per ``05-GOAL-RUN-SCHEMA.md`` section 7).

Rule model
----------
One evaluator over four per-axis ordered rule tables plus one aggregate
closure rule table (first match wins; every table ends in a default rule so
evaluation is total):

1. ``STATISTICAL_SUFFICIENCY_RULES`` -- ``R-STAT-1`` evidence sufficient ->
   SATISFIED; ``R-STAT-2`` evidence insufficient -> NOT_SATISFIED;
   ``R-STAT-3`` default -> UNRESOLVED.
2. ``EXECUTION_VALIDITY_RULES`` -- ``R-VALID-1`` execution valid ->
   SATISFIED; ``R-VALID-2`` execution invalid -> NOT_SATISFIED;
   ``R-VALID-3`` default -> UNRESOLVED.
3. ``RECOVERY_EXHAUSTION_RULES`` -- ``R-RECOV-1`` zero eligible hypotheses
   remain -> SATISFIED; ``R-RECOV-2`` eligible hypotheses remain (> 0) ->
   NOT_SATISFIED; ``R-RECOV-3`` default (pool unassessed) -> UNRESOLVED.
4. ``RESEARCH_SATURATION_RULES`` -- ``R-SAT-1`` all required search families
   completed and the zero-novelty rule met -> SATISFIED; ``R-SAT-2`` families
   not completed or the rule not yet met -> NOT_SATISFIED; ``R-SAT-3``
   default -> UNRESOLVED.
5. ``CLOSURE_RULES`` -- ``R-CLOSE-1`` any mandatory gate not satisfied ->
   CLOSURE_BLOCKED (AC-01); ``R-CLOSE-2`` default (all four satisfied) ->
   CLOSURE_ALLOWED.

Every assessment records the exact input record, every rule decision of every
table (``axis_decisions``), one gate decision per mandatory gate
(``gate_decisions``), the aggregate table trace (``rule_decisions``), the
blocking reasons (``closure_reasons`` tuple of gate decisions plus
``blocked_gate_ids`` -- the reason-reporting deliverable) and the matched
aggregate rule, so any closure decision is reproducible and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable

from scientific_reproduction.core.models import ClosureContract

__all__ = [
    "RULESET_VERSION",
    "RECORD_FIELDS",
    "MANDATORY_GATES",
    # errors
    "ClosureRulesError",
    "ClosureRecordError",
    # input model
    "ClosureRecord",
    # gate vocabulary
    "ClosureGateId",
    "ClosureAxisState",
    "ClosureOutcome",
    # per-axis rule tables
    "ClosureAxisRule",
    "ClosureAxisDecision",
    "STATISTICAL_SUFFICIENCY_RULES",
    "EXECUTION_VALIDITY_RULES",
    "RECOVERY_EXHAUSTION_RULES",
    "RESEARCH_SATURATION_RULES",
    "GATE_RULE_TABLES",
    # gate decisions (reason reporting)
    "ClosureGateDecision",
    # aggregate rule table
    "ClosureRule",
    "ClosureRuleDecision",
    "CLOSURE_RULES",
    # assessment and evaluator
    "ClosureAssessment",
    "evaluate_closure",
]

#: Version of the rule tables. Bumped whenever a mapping changes; recorded in
#: every assessment so old closure decisions stay interpretable.
RULESET_VERSION: str = "1.0"

#: Canonical field order of a ``ClosureRecord`` (also its to_dict order).
RECORD_FIELDS: tuple[str, ...] = (
    "statistics_sufficient",
    "execution_valid",
    "recovery_hypotheses_remaining",
    "eligible_hypotheses_total",
    "tested_or_ruled_out",
    "required_search_families_completed",
    "consecutive_zero_novelty_cycles",
    "required_zero_novelty_cycles",
)


class ClosureRulesError(ValueError):
    """Base error for the closure rule engine."""


class ClosureRecordError(ClosureRulesError):
    """Raised when a closure record violates the frozen input shape.

    Covers non-boolean tri-state axis values, negative recovery/literature
    counts and a saturation rule below the schema minimum of one cycle: a
    malformed record cannot silently change a closure decision, so it is
    rejected up front with a stable message.
    """


class ClosureGateId(StrEnum):
    """The four mandatory closure gates (the axes named in the objective)."""

    STATISTICAL_SUFFICIENCY = "statistical_sufficiency"
    EXECUTION_VALIDITY = "execution_validity"
    RECOVERY_HYPOTHESIS_EXHAUSTION = "recovery_hypothesis_exhaustion"
    RESEARCH_SATURATION = "research_saturation"


class ClosureAxisState(StrEnum):
    """Total per-gate classification.

    ``NOT_SATISFIED`` and ``UNRESOLVED`` both block closure (AC-01: closure
    fails when any mandatory gate is unresolved, where "unresolved" means
    not yet satisfied or unknown); they are distinguished in the reason
    report so downstream actors can tell an evaluated failure from an
    unknown.
    """

    SATISFIED = "SATISFIED"
    NOT_SATISFIED = "NOT_SATISFIED"
    UNRESOLVED = "UNRESOLVED"


class ClosureOutcome(StrEnum):
    """Outcome of the closure hard gate over the four mandatory axes."""

    CLOSURE_BLOCKED = "CLOSURE_BLOCKED"
    CLOSURE_ALLOWED = "CLOSURE_ALLOWED"


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureRecord:
    """One closure evaluation input: the four mandatory axes plus auditable counts.

    Axis fields are tri-state (``True`` satisfied / ``False``
    evaluated-and-not-satisfied / ``None`` unresolved), matching the
    normative reading that a gate which is not satisfied -- whether failing
    or unknown -- blocks closure (AC-01). Counts mirror the frozen
    ``ClosureRecovery`` / ``ClosureLiterature`` models
    (``schemas/closure-contract.schema.yaml``).

    * ``statistics_sufficient`` -- statistical evidence is adequate to
      distinguish failure/non-equivalence from insufficient precision.
    * ``execution_valid`` -- all required Runs are valid; no unresolved
      engineering/QC failure explains the result.
    * ``recovery_hypotheses_remaining`` -- eligible recovery hypotheses
      remaining (0 = exhausted); ``eligible_hypotheses_total`` and
      ``tested_or_ruled_out`` are the auditable companion counts of the
      frozen ``ClosureRecovery`` model and never influence the rules.
    * ``required_search_families_completed`` -- all required search families
      have been covered (09-RESEARCH-SUBSYSTEM.md section 7).
    * ``consecutive_zero_novelty_cycles`` -- consecutive expansion search
      cycles producing zero new eligible Recovery hypotheses.
    * ``required_zero_novelty_cycles`` -- the frozen, configurable saturation
      rule (schema default 2; minimum 1).

    The frozen dataclass makes a record hashable and comparable, so "same
    record -> same closure decision" is directly testable and the exact input
    is preserved in every assessment (auditability).

    Raises:
        ClosureRecordError: a tri-state axis is not a bool/None, a count is
            not an int/None or is negative, or ``required_zero_novelty_cycles``
            is below the schema minimum of 1.
    """

    statistics_sufficient: bool | None = None
    execution_valid: bool | None = None
    recovery_hypotheses_remaining: int | None = None
    eligible_hypotheses_total: int | None = None
    tested_or_ruled_out: int | None = None
    required_search_families_completed: bool | None = None
    consecutive_zero_novelty_cycles: int | None = None
    required_zero_novelty_cycles: int = 2

    def __post_init__(self) -> None:
        for name in (
            "statistics_sufficient",
            "execution_valid",
            "required_search_families_completed",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ClosureRecordError(
                    f"ClosureRecord.{name} must be a bool or None, got"
                    f" {type(value).__name__}"
                )
        for name in (
            "recovery_hypotheses_remaining",
            "eligible_hypotheses_total",
            "tested_or_ruled_out",
            "consecutive_zero_novelty_cycles",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                raise ClosureRecordError(
                    f"ClosureRecord.{name} must be an int or None, got"
                    f" {type(value).__name__}"
                )
            if value is not None and value < 0:
                raise ClosureRecordError(
                    f"ClosureRecord.{name} must be >= 0, got {value}"
                )
        required = self.required_zero_novelty_cycles
        if not isinstance(required, int) or isinstance(required, bool):
            raise ClosureRecordError(
                "ClosureRecord.required_zero_novelty_cycles must be an int,"
                f" got {type(required).__name__}"
            )
        if required < 1:
            raise ClosureRecordError(
                "ClosureRecord.required_zero_novelty_cycles must be >= 1 per"
                f" the closure-contract schema, got {required}"
            )

    @classmethod
    def from_closure_contract(
        cls,
        contract: ClosureContract,
        *,
        statistics_sufficient: bool | None = None,
        execution_valid: bool | None = None,
    ) -> "ClosureRecord":
        """Build a record from the frozen ``ClosureContract`` model.

        The recovery axis comes from the model's frozen ``ClosureRecovery``
        (``remaining``, ``eligible_hypotheses_total``, ``tested_or_ruled_out``)
        and the saturation axis from its ``ClosureLiterature``. The two
        tri-state axis values must be supplied explicitly: the frozen
        model's ``statistical_sufficiency`` / ``execution_validity`` fields
        are untyped free-form dicts, and the evaluator must not invent
        booleans from them (normative reading).

        Args:
            contract: the frozen closure contract model.
            statistics_sufficient: tri-state statistics-sufficiency axis.
            execution_valid: tri-state execution-validity axis.

        Raises:
            TypeError: ``contract`` is not a ``ClosureContract``.
            ClosureRecordError: a model field violates the record shape
                (cannot happen for a schema-valid model).
        """
        if not isinstance(contract, ClosureContract):
            raise TypeError(
                "from_closure_contract expects a ClosureContract, got"
                f" {type(contract).__name__}"
            )
        return cls(
            statistics_sufficient=statistics_sufficient,
            execution_valid=execution_valid,
            recovery_hypotheses_remaining=contract.recovery.remaining,
            eligible_hypotheses_total=contract.recovery.eligible_hypotheses_total,
            tested_or_ruled_out=contract.recovery.tested_or_ruled_out,
            required_search_families_completed=(
                contract.literature.required_search_families_completed
            ),
            consecutive_zero_novelty_cycles=(
                contract.literature.consecutive_zero_novelty_cycles
            ),
            required_zero_novelty_cycles=contract.literature.required_zero_novelty_cycles,
        )

    def to_dict(self) -> dict[str, bool | int | None]:
        """Plain dict of the record in canonical field order."""
        return {name: getattr(self, name) for name in RECORD_FIELDS}


# ---------------------------------------------------------------------------
# Per-axis rule tables
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureAxisRule:
    """One entry of an ordered per-gate rule table."""

    rule_id: str
    description: str
    state: ClosureAxisState
    predicate: Callable[[ClosureRecord], bool]


@dataclass(frozen=True)
class ClosureAxisDecision:
    """Record of one per-gate rule evaluation (audit trail)."""

    gate_id: ClosureGateId
    rule_id: str
    description: str
    state: ClosureAxisState
    matched: bool


#: Statistical-sufficiency rule table (first match wins; R-STAT-3 default).
STATISTICAL_SUFFICIENCY_RULES: tuple[ClosureAxisRule, ...] = (
    ClosureAxisRule(
        rule_id="R-STAT-1",
        description=(
            "statistical evidence is adequate to distinguish "
            "failure/non-equivalence from insufficient precision"
        ),
        state=ClosureAxisState.SATISFIED,
        predicate=lambda r: r.statistics_sufficient is True,
    ),
    ClosureAxisRule(
        rule_id="R-STAT-2",
        description=(
            "statistical evidence has been evaluated and is not adequate: "
            "failure/non-equivalence cannot be distinguished from "
            "insufficient precision"
        ),
        state=ClosureAxisState.NOT_SATISFIED,
        predicate=lambda r: r.statistics_sufficient is False,
    ),
    ClosureAxisRule(
        rule_id="R-STAT-3",
        description="statistical sufficiency has not been established (default)",
        state=ClosureAxisState.UNRESOLVED,
        predicate=lambda r: True,
    ),
)

#: Execution-validity rule table (first match wins; R-VALID-3 default).
EXECUTION_VALIDITY_RULES: tuple[ClosureAxisRule, ...] = (
    ClosureAxisRule(
        rule_id="R-VALID-1",
        description=(
            "all required Runs are valid and no unresolved engineering/QC "
            "failure explains the result"
        ),
        state=ClosureAxisState.SATISFIED,
        predicate=lambda r: r.execution_valid is True,
    ),
    ClosureAxisRule(
        rule_id="R-VALID-2",
        description=(
            "required Runs are not all valid or an unresolved "
            "engineering/QC failure explains the result"
        ),
        state=ClosureAxisState.NOT_SATISFIED,
        predicate=lambda r: r.execution_valid is False,
    ),
    ClosureAxisRule(
        rule_id="R-VALID-3",
        description="execution validity has not been established (default)",
        state=ClosureAxisState.UNRESOLVED,
        predicate=lambda r: True,
    ),
)

#: Recovery-exhaustion rule table (first match wins; R-RECOV-3 default).
RECOVERY_EXHAUSTION_RULES: tuple[ClosureAxisRule, ...] = (
    ClosureAxisRule(
        rule_id="R-RECOV-1",
        description=(
            "the eligible recovery-hypothesis pool is exhausted: zero "
            "eligible hypotheses remain (AC-03)"
        ),
        state=ClosureAxisState.SATISFIED,
        predicate=lambda r: r.recovery_hypotheses_remaining == 0,
    ),
    ClosureAxisRule(
        rule_id="R-RECOV-2",
        description=(
            "eligible recovery hypotheses remain: a non-reproduced "
            "reproduction cannot close while any remain (AC-03)"
        ),
        state=ClosureAxisState.NOT_SATISFIED,
        predicate=lambda r: (
            r.recovery_hypotheses_remaining is not None
            and r.recovery_hypotheses_remaining > 0
        ),
    ),
    ClosureAxisRule(
        rule_id="R-RECOV-3",
        description="the recovery-hypothesis pool has not been assessed (default)",
        state=ClosureAxisState.UNRESOLVED,
        predicate=lambda r: True,
    ),
)

#: Research-saturation rule table (first match wins; R-SAT-3 default).
#: Saturation requires the search families to be covered *and* the
#: consecutive zero-novelty-cycle count to meet the frozen, configurable rule
#: (09-RESEARCH-SUBSYSTEM.md section 7).
RESEARCH_SATURATION_RULES: tuple[ClosureAxisRule, ...] = (
    ClosureAxisRule(
        rule_id="R-SAT-1",
        description=(
            "all required search families are completed and the "
            "consecutive zero-novelty-cycle count meets the configured "
            "saturation rule"
        ),
        state=ClosureAxisState.SATISFIED,
        predicate=lambda r: (
            r.required_search_families_completed is True
            and r.consecutive_zero_novelty_cycles is not None
            and r.consecutive_zero_novelty_cycles
            >= r.required_zero_novelty_cycles
        ),
    ),
    ClosureAxisRule(
        rule_id="R-SAT-2",
        description=(
            "required search families are confirmed incomplete, or the "
            "counted zero-novelty cycles do not yet meet the saturation rule"
        ),
        state=ClosureAxisState.NOT_SATISFIED,
        predicate=lambda r: (
            r.required_search_families_completed is False
            or (
                r.consecutive_zero_novelty_cycles is not None
                and r.consecutive_zero_novelty_cycles
                < r.required_zero_novelty_cycles
            )
        ),
    ),
    ClosureAxisRule(
        rule_id="R-SAT-3",
        description="research saturation has not been established (default)",
        state=ClosureAxisState.UNRESOLVED,
        predicate=lambda r: True,
    ),
)

#: The four per-gate tables in normative gate order (also the assessment's
#: ``gate_decisions`` order and the ``blocked_gate_ids`` order).
GATE_RULE_TABLES: tuple[tuple[ClosureGateId, tuple[ClosureAxisRule, ...]], ...] = (
    (ClosureGateId.STATISTICAL_SUFFICIENCY, STATISTICAL_SUFFICIENCY_RULES),
    (ClosureGateId.EXECUTION_VALIDITY, EXECUTION_VALIDITY_RULES),
    (ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION, RECOVERY_EXHAUSTION_RULES),
    (ClosureGateId.RESEARCH_SATURATION, RESEARCH_SATURATION_RULES),
)

#: The mandatory closure gates, in normative evaluation order.
MANDATORY_GATES: tuple[ClosureGateId, ...] = tuple(
    gate_id for gate_id, _ in GATE_RULE_TABLES
)


# ---------------------------------------------------------------------------
# Gate decisions (reason reporting)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureGateDecision:
    """One mandatory gate's resolved state plus its deciding rule.

    The reason-reporting deliverable: ``closure_reasons`` in the assessment
    is a tuple of these decisions (one per blocked gate, in normative gate
    order), and ``blocked_gate_ids`` the corresponding gate ids, so
    downstream actors can act on exactly which gate(s) blocked closure.
    """

    gate_id: ClosureGateId
    state: ClosureAxisState
    rule_id: str
    description: str
    detail: str

    @property
    def satisfied(self) -> bool:
        """True exactly when the gate state is SATISFIED."""
        return self.state is ClosureAxisState.SATISFIED


#: Static per-(gate, state) reason details. The recovery NOT_SATISFIED detail
#: is dynamic (it reports the exact remaining count) and is produced by
#: ``_gate_detail``.
_GATE_STATE_DETAIL: dict[tuple[ClosureGateId, ClosureAxisState], str] = {
    (
        ClosureGateId.STATISTICAL_SUFFICIENCY,
        ClosureAxisState.SATISFIED,
    ): "statistical evidence is adequate to distinguish failure/non-equivalence"
    " from insufficient precision",
    (
        ClosureGateId.STATISTICAL_SUFFICIENCY,
        ClosureAxisState.NOT_SATISFIED,
    ): "statistical evidence is not adequate: failure/non-equivalence cannot be"
    " distinguished from insufficient precision",
    (
        ClosureGateId.STATISTICAL_SUFFICIENCY,
        ClosureAxisState.UNRESOLVED,
    ): "statistical sufficiency has not been established",
    (
        ClosureGateId.EXECUTION_VALIDITY,
        ClosureAxisState.SATISFIED,
    ): "all required Runs are valid and no unresolved engineering/QC failure"
    " explains the result",
    (
        ClosureGateId.EXECUTION_VALIDITY,
        ClosureAxisState.NOT_SATISFIED,
    ): "required Runs are not all valid or an unresolved engineering/QC failure"
    " explains the result",
    (
        ClosureGateId.EXECUTION_VALIDITY,
        ClosureAxisState.UNRESOLVED,
    ): "execution validity has not been established",
    (
        ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
        ClosureAxisState.SATISFIED,
    ): "no eligible recovery hypotheses remain",
    (
        ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
        ClosureAxisState.UNRESOLVED,
    ): "the recovery-hypothesis pool has not been assessed",
    (
        ClosureGateId.RESEARCH_SATURATION,
        ClosureAxisState.SATISFIED,
    ): "all required search families are completed and the configured"
    " zero-novelty-cycle saturation rule is satisfied",
    (
        ClosureGateId.RESEARCH_SATURATION,
        ClosureAxisState.NOT_SATISFIED,
    ): "required search families are not completed or the configured"
    " zero-novelty-cycle saturation rule is not yet satisfied",
    (
        ClosureGateId.RESEARCH_SATURATION,
        ClosureAxisState.UNRESOLVED,
    ): "research saturation has not been established",
}


def _gate_detail(
    gate_id: ClosureGateId, state: ClosureAxisState, record: ClosureRecord
) -> str:
    """Stable, machine-readable reason detail for a gate decision."""
    if (
        gate_id is ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION
        and state is ClosureAxisState.NOT_SATISFIED
    ):
        remaining = record.recovery_hypotheses_remaining
        if remaining is not None:
            return f"eligible recovery hypotheses remaining: {remaining}"
    return _GATE_STATE_DETAIL[(gate_id, state)]


# ---------------------------------------------------------------------------
# Aggregate closure rule table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureRule:
    """One entry of the ordered aggregate closure rule table."""

    rule_id: str
    description: str
    outcome: ClosureOutcome
    predicate: Callable[[tuple[ClosureGateDecision, ...]], bool]


@dataclass(frozen=True)
class ClosureRuleDecision:
    """Record of one aggregate closure-rule evaluation."""

    rule_id: str
    description: str
    outcome: ClosureOutcome
    matched: bool


#: The ordered aggregate closure rule table. ``R-CLOSE-1`` matches when any
#: mandatory gate is not satisfied (AC-01); ``R-CLOSE-2`` is the default, so
#: the outcome is total. There is exactly this pair -- no rule may close a
#: required goal through any other shortcut (AC-02).
CLOSURE_RULES: tuple[ClosureRule, ...] = (
    ClosureRule(
        rule_id="R-CLOSE-1",
        description=(
            "at least one mandatory closure gate is not satisfied "
            "(evaluated-failing or unresolved): closure is blocked (AC-01)"
        ),
        outcome=ClosureOutcome.CLOSURE_BLOCKED,
        predicate=lambda gates: any(
            gate.state is not ClosureAxisState.SATISFIED for gate in gates
        ),
    ),
    ClosureRule(
        rule_id="R-CLOSE-2",
        description=(
            "all four mandatory closure gates are satisfied: closure is "
            "allowed (default)"
        ),
        outcome=ClosureOutcome.CLOSURE_ALLOWED,
        predicate=lambda gates: True,
    ),
)


# ---------------------------------------------------------------------------
# Assessment and evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureAssessment:
    """Full, auditable result of a closure evaluation (AC-01/AC-03).

    ``record`` is the exact input; ``axis_decisions`` records every rule
    evaluation of every per-gate table (in gate order, then table order);
    ``gate_decisions`` records one decision per mandatory gate (in
    ``MANDATORY_GATES`` order); ``rule_decisions`` records the aggregate
    table; ``closure_reasons`` reports the gate decisions that blocked
    closure and ``blocked_gate_ids`` their ids (the reason-reporting
    deliverable); ``matched_rule_id`` names the aggregate rule that decided
    the outcome (``None`` is impossible: the trailing default always
    matches).
    """

    ruleset_version: str
    record: ClosureRecord
    outcome: ClosureOutcome
    axis_decisions: tuple[ClosureAxisDecision, ...]
    gate_decisions: tuple[ClosureGateDecision, ...]
    rule_decisions: tuple[ClosureRuleDecision, ...]
    closure_reasons: tuple[ClosureGateDecision, ...]
    blocked_gate_ids: tuple[ClosureGateId, ...]
    matched_rule_id: str

    @property
    def closure_allowed(self) -> bool:
        """True exactly when the closure outcome is CLOSURE_ALLOWED."""
        return self.outcome is ClosureOutcome.CLOSURE_ALLOWED


def evaluate_closure(record: ClosureRecord) -> ClosureAssessment:
    """Evaluate the closure hard gate over the four mandatory axes.

    Pure and deterministic: the outcome is a pure function of the record
    (AC-02 determinism). Closure is allowed exactly when every mandatory gate
    is satisfied; a gate that is evaluated-failing or unknown blocks closure
    and is reported in ``closure_reasons`` / ``blocked_gate_ids`` (AC-01),
    so downstream actors can act on exactly which gate(s) blocked closure.

    Args:
        record: the closure evaluation input (four axes plus auditable
            counts).

    Raises:
        TypeError: ``record`` is not a ``ClosureRecord``.

    Returns:
        The full assessment: outcome, per-gate decisions, every rule
        decision, and the blocking reasons.
    """
    if not isinstance(record, ClosureRecord):
        raise TypeError(
            "evaluate_closure expects a ClosureRecord, got"
            f" {type(record).__name__}"
        )
    axis_decisions: list[ClosureAxisDecision] = []
    gate_decisions: list[ClosureGateDecision] = []
    for gate_id, rules in GATE_RULE_TABLES:
        matched_rule: ClosureAxisRule | None = None
        for axis_rule in rules:
            matched = axis_rule.predicate(record)
            axis_decisions.append(
                ClosureAxisDecision(
                    gate_id=gate_id,
                    rule_id=axis_rule.rule_id,
                    description=axis_rule.description,
                    state=axis_rule.state,
                    matched=matched,
                )
            )
            if matched and matched_rule is None:
                matched_rule = axis_rule
        # Every table ends in a default rule, so this can never be None.
        assert matched_rule is not None
        gate_decisions.append(
            ClosureGateDecision(
                gate_id=gate_id,
                state=matched_rule.state,
                rule_id=matched_rule.rule_id,
                description=matched_rule.description,
                detail=_gate_detail(gate_id, matched_rule.state, record),
            )
        )
    rule_decisions: list[ClosureRuleDecision] = []
    matched_rule_id: str | None = None
    matched_outcome = ClosureOutcome.CLOSURE_ALLOWED  # unreachable default
    for closure_rule in CLOSURE_RULES:
        matched = closure_rule.predicate(tuple(gate_decisions))
        rule_decisions.append(
            ClosureRuleDecision(
                rule_id=closure_rule.rule_id,
                description=closure_rule.description,
                outcome=closure_rule.outcome,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = closure_rule.rule_id
            matched_outcome = closure_rule.outcome
    # R-CLOSE-2 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    reasons = tuple(
        decision
        for decision in gate_decisions
        if decision.state is not ClosureAxisState.SATISFIED
    )
    return ClosureAssessment(
        ruleset_version=RULESET_VERSION,
        record=record,
        outcome=matched_outcome,
        axis_decisions=tuple(axis_decisions),
        gate_decisions=tuple(gate_decisions),
        rule_decisions=tuple(rule_decisions),
        closure_reasons=reasons,
        blocked_gate_ids=tuple(decision.gate_id for decision in reasons),
        matched_rule_id=matched_rule_id,
    )
