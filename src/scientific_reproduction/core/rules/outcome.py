"""Requirement / project outcome aggregation rules (DEV-M2-G06).

Pure logic implementing the frozen outcome aggregation rules of
``04-PROJECT-LIFECYCLE.md`` sections 4-6 against the frozen outcome
vocabulary of ``schemas/requirement.schema.yaml`` (``RequirementOutcome`` /
``MethodReproducibility`` / ``Criticality``) and ``schemas/project.schema.yaml``
(``ReproductionOutcome``; modeled in ``core.models``). No LLM, no randomness,
no wall-clock dependence: the same inputs always yield the same assessments
on every platform and Python version, and every decision is recorded for the
audit trail.

Phase/outcome separation (AC-01)
--------------------------------
``project_phase`` and ``reproduction_outcome`` are strictly separate
(``04-PROJECT-LIFECYCLE.md`` section 1): the phase answers "where is the
workflow now?", the outcome answers "what is the final scientific
reproduction conclusion?". Accordingly this module contains **no function
that accepts a ``ProjectPhase`` and no API that derives an outcome from a
phase**: the aggregators consume only phase-independent Requirement outcomes.
A ``ProjectPhase`` member is rejected at the record boundary
(``OutcomeRecordError``, a ``ValueError`` subclass) and at every public
function boundary (``TypeError``), and no public symbol name or signature
mentions a phase. The tests prove the type-level separation by inspection
of the module and by passing ``ProjectPhase`` members into every public
entry point.

Determination gating (AC-02)
----------------------------
The final outcome remains ``UNDETERMINED`` before the final validation
rules permit determination (``04-PROJECT-LIFECYCLE.md`` section 1: "Before
Final Validation, the outcome should remain ``UNDETERMINED``";
``01-PRODUCT-REQUIREMENTS.md`` items 15-16: "When all Requirements are
closed or validly inconclusive, project enters Final Validation", where the
Supervisor "produces final scientific outcome, method reproducibility
outcome, report and auditable package"). The final validation gate is
encoded **phase-independently**: the project outcome may be determined only
when

* every Requirement is individually determined -- no Requirement outcome is
  still ``OPEN``. A Requirement that has not individually passed aggregates
  to ``UNDETERMINED`` (``R-REQOUT-5``) and pulls the project outcome to
  ``UNDETERMINED`` (``R-PRJ-UND-1``); and
* every ``NOT_REPRODUCED`` Requirement is legitimate under the Closure
  Contract (see the composition note below); a negative closure that is not
  legitimately closed blocks determination.

The determination is compositional: Requirement-level classification first
(``classify_requirement_outcome``), project-level aggregation second
(``aggregate_project_outcome``), exactly as ``01-PRODUCT-REQUIREMENTS.md``
item 15's "Requirements are closed ... then Final Validation" ordering
requires.

Criticality (AC-03)
-------------------
Project-level aggregation consults the criticality of each Requirement
exactly per the locked specification (``04-PROJECT-LIFECYCLE.md`` section 5):
a project closes ``NOT_REPRODUCED_WITHIN_DEFINED_SCOPE`` only when one or
more **Critical** Requirements close ``NOT_REPRODUCED`` under a satisfied
Closure Contract; non-critical Requirement outcomes cannot force that
critical outcome (they can at most produce ``PARTIALLY_REPRODUCED`` when all
Critical Requirements are reproduced).

Composition with the closure rules (DEV-M2-G05)
-----------------------------------------------
The closure hard gate (``rules.closure``) governs the stopping decision
only; the outcome classification is a separate review decision, stored
separately (``05-GOAL-RUN-SCHEMA.md`` section 7; ``rules.closure`` module
docstring). This module composes with ``rules.closure`` by **consuming** its
decision (``ClosureAssessment.closure_allowed``) as a boolean input --
``aggregate_project_outcome`` takes ``closure_allowed`` and never re-derives
closure from the requirement outcomes, so the closure rules stay the single
normative mapping from the closure axes to the stopping decision.

Rule model
----------
Three deliverables, each an ordered rule table (first match wins) with a
trailing default rule so evaluation is total:

1. **Requirement outcome aggregator** -- ``classify_requirement_outcome``
   maps one ``RequirementOutcomeRecord`` to a phase-independent
   ``RequirementClosureState`` through ``REQUIREMENT_OUTCOME_RULES``
   (``R-REQOUT-1`` REPRODUCED -> REPRODUCED; ``R-REQOUT-2``
   REPRODUCED_WITH_RECOVERY -> REPRODUCED; ``R-REQOUT-3`` NOT_REPRODUCED ->
   NOT_REPRODUCED; ``R-REQOUT-4`` INCONCLUSIVE -> INCONCLUSIVE; ``R-REQOUT-5``
   default (OPEN) -> UNDETERMINED).
2. **Project outcome aggregator** -- ``aggregate_project_outcome`` first
   classifies every Requirement through the requirement aggregator (single
   normative mapping) and then runs ``PROJECT_OUTCOME_RULES`` over the
   closure states plus the ``closure_allowed`` flag:
   ``R-PRJ-UND-1`` any Requirement not individually determined ->
   UNDETERMINED (AC-02); ``R-PRJ-UND-2`` a NOT_REPRODUCED Requirement while
   the Closure Contract is evaluated as not satisfied -> UNDETERMINED;
   ``R-PRJ-UND-3`` a NOT_REPRODUCED Requirement while the Closure Contract
   is unassessed -> UNDETERMINED; ``R-PRJ-1`` all Requirements reproduced ->
   FULLY_REPRODUCED; ``R-PRJ-2`` all Critical Requirements reproduced but a
   Required/Supporting Requirement closes NOT_REPRODUCED ->
   PARTIALLY_REPRODUCED; ``R-PRJ-3`` one or more Critical Requirements close
   NOT_REPRODUCED (under a satisfied Closure Contract) ->
   NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (AC-03); ``R-PRJ-4`` a validly
   INCONCLUSIVE Requirement -> INCONCLUSIVE; ``R-PRJ-5`` default (no
   formally reported Requirements) -> UNDETERMINED.
3. **Method reproducibility aggregation hook** -- ``aggregate_method_reproducibility``
   aggregates the per-Requirement ``MethodReproducibility`` ratings through
   ``METHOD_REPRODUCIBILITY_RULES`` (``R-MR-1`` any UNDETERMINED rating ->
   UNDETERMINED; ``R-MR-2`` any INCONCLUSIVE rating -> INCONCLUSIVE;
   ``R-MR-3`` default -> the worst/least reproducible terminal rating). It is
   the aggregation hook of ``04-PROJECT-LIFECYCLE.md`` section 6 ("Project-
   level method reproducibility is an aggregate, not a subjective single
   rating") for the downstream M3+ goals that produce the project's method
   reproducibility outcome and the WP-90 final integration
   (``17-FDM201-REFERENCE-CASE.md``: "method-reproducibility aggregation").

Every assessment records its exact inputs, every rule decision of every
table it consulted, and the id of the matched rule, so any outcome is
reproducible and auditable.

Normative readings (the spec leaves these open; the readings are locked here
and asserted bi-implicationally over exhaustive grids in the tests)
--------------------------------------------------------------------
* **Final validation gate** (AC-02): determination requires every Requirement
  outcome to be terminal (REPRODUCED / REPRODUCED_WITH_RECOVERY /
  NOT_REPRODUCED / INCONCLUSIVE); a Requirement still ``OPEN`` is "not
  individually passed", stays UNDETERMINED and forces the project outcome to
  UNDETERMINED. "Before Final Validation" is encoded by the requirement
  closure states, never by consulting ``ProjectPhase``.
* **Closure gate for every NOT_REPRODUCED Requirement** (AC-03 reading):
  ``08-STRICT-RECOVERY-CLOSURE.md`` section 4 ("A Goal may close
  NOT_REPRODUCED_WITHIN_DEFINED_SCOPE only when the frozen Closure Contract
  is satisfied") is read as governing *any* NOT_REPRODUCED closure, Critical
  or not; ``04-PROJECT-LIFECYCLE.md`` section 5 rule 3 attaches "under a
  satisfied Closure Contract" to the Critical case, and the same legitimacy
  condition is applied to Required/Supporting NOT_REPRODUCED closures, so an
  input NOT_REPRODUCED Requirement whose Closure Contract is not satisfied
  (``closure_allowed=False``) or unassessed (``closure_allowed=None``)
  blocks determination (UNDETERMINED) instead of feeding a partial
  conclusion. The two blocked states are distinguished in the blocking
  reasons, mirroring ``rules.closure`` AC-01.
* **INCONCLUSIVE cap** (reading): ``04-PROJECT-LIFECYCLE.md`` section 5 rule
  4 caps the outcome at INCONCLUSIVE when an *unresolved Critical
  Requirement* is INCONCLUSIVE; the same cap is applied to INCONCLUSIVE
  Requirements of any criticality, because ``01-PRODUCT-REQUIREMENTS.md``
  item 15 admits "validly inconclusive" Requirements into Final Validation
  and an unknown Requirement cannot support a positive conclusion
  (FULLY_REPRODUCED / PARTIALLY_REPRODUCED). A demonstrated NOT_REPRODUCED
  non-critical Requirement with all Criticals reproduced still yields
  PARTIALLY_REPRODUCED (rule 2 fires before the cap).
* **Rule order 3 before 4**: a Critical Requirement closing NOT_REPRODUCED
  under a satisfied Closure Contract dominates a coexisting INCONCLUSIVE
  Critical Requirement: NOT_REPRODUCED_WITHIN_DEFINED_SCOPE does not
  "exceed" INCONCLUSIVE (it is the least favorable conclusion), so rule 3 is
  evaluated before rule 4.
* **REPRODUCED_WITH_RECOVERY counts as reproduced** for project aggregation
  (rules 1-2 of section 5: "close as REPRODUCED or REPRODUCED_WITH_RECOVERY"
  and "all Critical Requirements reproduced"); the recovery distinction is
  carried by the Requirement outcome itself and by the method
  reproducibility axis (``18-TEST-AND-ACCEPTANCE-PLAN.md`` Scenario B:
  "scientific Requirement ``REPRODUCED_WITH_RECOVERY``; method
  reproducibility lower than direct reproducibility").
* **Empty requirement set**: a project with no formally reported
  Requirements has no basis for a final scientific conclusion; the outcome
  stays UNDETERMINED (``R-PRJ-5`` default).
* **Method reproducibility aggregation** (``04-PROJECT-LIFECYCLE.md``
  section 6 is deliberately underspecified -- "an aggregate, not a
  subjective single rating"): the minimal total version is encoded as:
  UNDETERMINED when any per-Requirement rating is UNDETERMINED (no
  determination from incomplete ratings, mirroring AC-02); INCONCLUSIVE when
  any rating is INCONCLUSIVE; otherwise the **worst (least reproducible)**
  terminal rating (the bottleneck reading: the project's method
  reproducibility claim is only as strong as its weakest Requirement, cf.
  Scenario B's "lower than direct reproducibility"). An empty set aggregates
  to UNDETERMINED. Downstream M3+ goals may replace this hook's rule table
  under a bumped ``RULESET_VERSION``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from scientific_reproduction.core.models import (
    Criticality,
    MethodReproducibility,
    ReproductionOutcome,
    ReproductionRequirement,
    RequirementOutcome,
)

__all__ = [
    "RULESET_VERSION",
    # errors
    "OutcomeRulesError",
    "OutcomeRecordError",
    # input models
    "RequirementOutcomeRecord",
    "MethodReproducibilityRecord",
    # requirement outcome aggregator (deliverable 1)
    "RequirementClosureState",
    "RequirementOutcomeRule",
    "RequirementOutcomeRuleDecision",
    "RequirementOutcomeAssessment",
    "REQUIREMENT_OUTCOME_RULES",
    "classify_requirement_outcome",
    # project outcome aggregator (deliverable 2)
    "ProjectOutcomeRule",
    "ProjectOutcomeRuleDecision",
    "ProjectOutcomeAssessment",
    "PROJECT_OUTCOME_RULES",
    "aggregate_project_outcome",
    # method reproducibility aggregation hook (deliverable 3)
    "METHOD_REPRODUCIBILITY_ORDER",
    "MethodReproducibilityRule",
    "MethodReproducibilityRuleDecision",
    "MethodReproducibilityAssessment",
    "METHOD_REPRODUCIBILITY_RULES",
    "aggregate_method_reproducibility",
]

#: Version of the rule tables. Bumped whenever a mapping changes; recorded in
#: every assessment so old outcomes stay interpretable.
RULESET_VERSION: str = "1.0"


class OutcomeRulesError(ValueError):
    """Base error for the outcome rule engine."""


class OutcomeRecordError(OutcomeRulesError):
    """Raised when an outcome record violates the frozen input shape.

    Covers empty requirement ids and criticality / outcome / reproducibility
    values that are not members of the frozen enum classes (a ``ProjectPhase``
    member is rejected here too: phase values are never outcome values, AC-01).
    A malformed record cannot silently change an outcome, so it is rejected
    up front with a stable message.
    """


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

#: Canonical field order of a ``RequirementOutcomeRecord`` (also its
#: to_dict order).
REQUIREMENT_RECORD_FIELDS: tuple[str, ...] = (
    "requirement_id",
    "criticality",
    "outcome",
)


@dataclass(frozen=True)
class RequirementOutcomeRecord:
    """One phase-independent Requirement outcome input.

    Mirrors the frozen requirement vocabulary (``schemas/requirement.schema.yaml``;
    ``core.models.ReproductionRequirement``): the Requirement's id, its
    ``Criticality`` and its final ``RequirementOutcome`` (``OPEN`` when the
    Requirement has not been individually determined). There is no
    ``ProjectPhase`` anywhere: the outcome vocabulary is phase-independent by
    construction (AC-01), and the type-level separation is enforced at the
    record boundary (see ``OutcomeRecordError``).

    The frozen dataclass makes a record hashable and comparable, so "same
    record -> same outcome" is directly testable and the exact input is
    preserved in every assessment (auditability).

    Raises:
        OutcomeRecordError: ``requirement_id`` is not a non-empty string,
            ``criticality`` is not a ``Criticality`` member, or ``outcome``
            is not a ``RequirementOutcome`` member.
    """

    requirement_id: str
    criticality: Criticality
    outcome: RequirementOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not self.requirement_id.strip():
            raise OutcomeRecordError(
                "RequirementOutcomeRecord.requirement_id must be a non-empty"
                f" string, got {self.requirement_id!r}"
            )
        if not isinstance(self.criticality, Criticality):
            raise OutcomeRecordError(
                "RequirementOutcomeRecord.criticality must be a Criticality"
                f" member, got {self.criticality!r}"
            )
        if not isinstance(self.outcome, RequirementOutcome):
            raise OutcomeRecordError(
                "RequirementOutcomeRecord.outcome must be a RequirementOutcome"
                " member (ProjectPhase values are never outcome values, AC-01);"
                f" got {self.outcome!r}"
            )

    @classmethod
    def from_reproduction_requirement(
        cls, requirement: ReproductionRequirement
    ) -> "RequirementOutcomeRecord":
        """Build a record from the frozen ``ReproductionRequirement`` model.

        ``requirement_id``, ``criticality`` and ``outcome`` map directly from
        the model; nothing is invented here.

        Raises:
            TypeError: ``requirement`` is not a ``ReproductionRequirement``.
            OutcomeRecordError: the model's fields violate the record shape
                (cannot happen for a schema-valid model).
        """
        if not isinstance(requirement, ReproductionRequirement):
            raise TypeError(
                "from_reproduction_requirement expects a"
                f" ReproductionRequirement, got {type(requirement).__name__}"
            )
        return cls(
            requirement_id=requirement.requirement_id,
            criticality=requirement.criticality,
            outcome=requirement.outcome,
        )

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the record in canonical field order."""
        return {
            "requirement_id": self.requirement_id,
            "criticality": self.criticality.value,
            "outcome": self.outcome.value,
        }


@dataclass(frozen=True)
class MethodReproducibilityRecord:
    """One per-Requirement method-reproducibility rating input.

    The per-Requirement/Goal category of ``04-PROJECT-LIFECYCLE.md`` section 6
    (``schemas/requirement.schema.yaml`` ``method_reproducibility`` enum;
    ``core.models.MethodReproducibility``). Scientific outcome and method
    reproducibility are strictly separate axes (section 6), so this record
    carries only the reproducibility rating, never an outcome or a phase.

    Raises:
        OutcomeRecordError: ``requirement_id`` is not a non-empty string, or
            ``reproducibility`` is not a ``MethodReproducibility`` member.
    """

    requirement_id: str
    reproducibility: MethodReproducibility

    def __post_init__(self) -> None:
        if not isinstance(self.requirement_id, str) or not self.requirement_id.strip():
            raise OutcomeRecordError(
                "MethodReproducibilityRecord.requirement_id must be a"
                f" non-empty string, got {self.requirement_id!r}"
            )
        if not isinstance(self.reproducibility, MethodReproducibility):
            raise OutcomeRecordError(
                "MethodReproducibilityRecord.reproducibility must be a"
                " MethodReproducibility member (ProjectPhase values are never"
                " reproducibility values, AC-01); got"
                f" {self.reproducibility!r}"
            )

    @classmethod
    def from_reproduction_requirement(
        cls,
        requirement: ReproductionRequirement,
        *,
        reproducibility: MethodReproducibility | None = None,
    ) -> "MethodReproducibilityRecord":
        """Build a record from the frozen ``ReproductionRequirement`` model.

        The frozen model's ``method_reproducibility`` is optional; when it is
        ``None`` (and no explicit ``reproducibility`` is given), the record
        is canonically UNDETERMINED -- an unrated Requirement is an
        undetermined one, never an invented rating.

        Raises:
            TypeError: ``requirement`` is not a ``ReproductionRequirement``.
            OutcomeRecordError: the resulting reproducibility is not a
                ``MethodReproducibility`` member.
        """
        if not isinstance(requirement, ReproductionRequirement):
            raise TypeError(
                "from_reproduction_requirement expects a"
                f" ReproductionRequirement, got {type(requirement).__name__}"
            )
        if reproducibility is None:
            reproducibility = requirement.method_reproducibility
        if reproducibility is None:
            reproducibility = MethodReproducibility.UNDETERMINED
        return cls(
            requirement_id=requirement.requirement_id,
            reproducibility=reproducibility,
        )

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the record in canonical field order."""
        return {
            "requirement_id": self.requirement_id,
            "reproducibility": self.reproducibility.value,
        }


# ---------------------------------------------------------------------------
# Requirement outcome aggregator (deliverable 1)
# ---------------------------------------------------------------------------


class RequirementClosureState(StrEnum):
    """Phase-independent per-Requirement closure state.

    The requirement-level classification consumed by the project aggregator:
    a Requirement that has not individually passed (outcome ``OPEN``) is
    UNDETERMINED and aggregates to UNDETERMINED (AC-02); ``REPRODUCED``
    covers both ``REPRODUCED`` and ``REPRODUCED_WITH_RECOVERY`` closures.
    """

    REPRODUCED = "REPRODUCED"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    INCONCLUSIVE = "INCONCLUSIVE"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class RequirementOutcomeRule:
    """One entry of the ordered requirement-outcome rule table."""

    rule_id: str
    description: str
    state: RequirementClosureState
    predicate: Callable[[RequirementOutcomeRecord], bool]


@dataclass(frozen=True)
class RequirementOutcomeRuleDecision:
    """Record of one requirement-outcome rule evaluation (audit trail)."""

    rule_id: str
    description: str
    state: RequirementClosureState
    matched: bool


@dataclass(frozen=True)
class RequirementOutcomeAssessment:
    """Full, auditable result of one requirement outcome classification.

    ``record`` is the exact input; ``decisions`` records the outcome of every
    rule in the table (in evaluation order); ``matched_rule_id`` names the
    rule that decided the state (``None`` is impossible: the trailing default
    rule always matches).
    """

    ruleset_version: str
    record: RequirementOutcomeRecord
    state: RequirementClosureState
    decisions: tuple[RequirementOutcomeRuleDecision, ...]
    matched_rule_id: str


#: The ordered requirement-outcome rule table. First match wins; order is
#: normative (see module docstring). Predicates are pure functions of the
#: record only, and ``R-REQOUT-5`` is the default so the mapping is total.
REQUIREMENT_OUTCOME_RULES: tuple[RequirementOutcomeRule, ...] = (
    RequirementOutcomeRule(
        rule_id="R-REQOUT-1",
        description=(
            "Requirement closes REPRODUCED: reproduced"
            " (04-PROJECT-LIFECYCLE.md section 4)"
        ),
        state=RequirementClosureState.REPRODUCED,
        predicate=lambda r: r.outcome is RequirementOutcome.REPRODUCED,
    ),
    RequirementOutcomeRule(
        rule_id="R-REQOUT-2",
        description=(
            "Requirement closes REPRODUCED_WITH_RECOVERY: reproduced via a"
            " recovery track; the recovery distinction is carried by the"
            " Requirement outcome and the method reproducibility axis, not"
            " by the project outcome (18-TEST-AND-ACCEPTANCE-PLAN.md"
            " Scenario B)"
        ),
        state=RequirementClosureState.REPRODUCED,
        predicate=lambda r: r.outcome is RequirementOutcome.REPRODUCED_WITH_RECOVERY,
    ),
    RequirementOutcomeRule(
        rule_id="R-REQOUT-3",
        description=(
            "Requirement closes NOT_REPRODUCED: not reproduced within the"
            " pre-defined and sufficiently explored scope"
            " (08-STRICT-RECOVERY-CLOSURE.md section 5)"
        ),
        state=RequirementClosureState.NOT_REPRODUCED,
        predicate=lambda r: r.outcome is RequirementOutcome.NOT_REPRODUCED,
    ),
    RequirementOutcomeRule(
        rule_id="R-REQOUT-4",
        description="Requirement closes INCONCLUSIVE: validly inconclusive",
        state=RequirementClosureState.INCONCLUSIVE,
        predicate=lambda r: r.outcome is RequirementOutcome.INCONCLUSIVE,
    ),
    RequirementOutcomeRule(
        rule_id="R-REQOUT-5",
        description=(
            "Requirement outcome is still OPEN: the Requirement has not"
            " individually passed and remains UNDETERMINED (default; AC-02)"
        ),
        state=RequirementClosureState.UNDETERMINED,
        predicate=lambda r: True,
    ),
)


def classify_requirement_outcome(
    record: RequirementOutcomeRecord,
) -> RequirementOutcomeAssessment:
    """Classify one Requirement outcome into its closure state.

    Pure and deterministic: the state is a pure function of the record
    (AC-02 determinism). The returned :class:`RequirementOutcomeAssessment`
    records the exact input record and every rule decision, so any
    classification is reproducible and auditable.

    Args:
        record: the phase-independent Requirement outcome input.

    Raises:
        TypeError: ``record`` is not a ``RequirementOutcomeRecord``.

    Returns:
        The full assessment: closure state plus the auditable trace.
    """
    if not isinstance(record, RequirementOutcomeRecord):
        raise TypeError(
            "classify_requirement_outcome expects a RequirementOutcomeRecord,"
            f" got {type(record).__name__}"
        )
    decisions: list[RequirementOutcomeRuleDecision] = []
    matched_rule_id: str | None = None
    matched_state = RequirementClosureState.UNDETERMINED  # unreachable default
    for rule in REQUIREMENT_OUTCOME_RULES:
        matched = rule.predicate(record)
        decisions.append(
            RequirementOutcomeRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                state=rule.state,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_state = rule.state
    # R-REQOUT-5 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return RequirementOutcomeAssessment(
        ruleset_version=RULESET_VERSION,
        record=record,
        state=matched_state,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Project outcome aggregator (deliverable 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectOutcomeRule:
    """One entry of the ordered project-outcome rule table.

    The predicate runs on the classified per-Requirement assessments (so the
    requirement aggregator stays the single normative mapping) plus the
    ``closure_allowed`` flag consumed from ``rules.closure``.
    """

    rule_id: str
    description: str
    outcome: ReproductionOutcome
    predicate: Callable[[tuple[RequirementOutcomeAssessment, ...], bool | None], bool]


@dataclass(frozen=True)
class ProjectOutcomeRuleDecision:
    """Record of one project-outcome rule evaluation (audit trail)."""

    rule_id: str
    description: str
    outcome: ReproductionOutcome
    matched: bool


@dataclass(frozen=True)
class ProjectOutcomeAssessment:
    """Full, auditable result of a project outcome aggregation.

    ``requirements`` is the exact input (a tuple, so the assessment is
    hashable and comparable) and ``closure_allowed`` the exact closure
    decision consumed from ``rules.closure``; ``requirement_assessments``
    records the requirement-level classification of every Requirement (in
    input order); ``rule_decisions`` records the project rule table;
    ``blocking_reasons`` reports, in stable wording, why determination is
    blocked whenever the outcome is UNDETERMINED (mirroring the reason
    reporting of ``rules.closure``); ``matched_rule_id`` names the project
    rule that decided the outcome (``None`` is impossible: the trailing
    default always matches).
    """

    ruleset_version: str
    requirements: tuple[RequirementOutcomeRecord, ...]
    closure_allowed: bool | None
    requirement_assessments: tuple[RequirementOutcomeAssessment, ...]
    rule_decisions: tuple[ProjectOutcomeRuleDecision, ...]
    outcome: ReproductionOutcome
    blocking_reasons: tuple[str, ...]
    matched_rule_id: str

    @property
    def determined(self) -> bool:
        """True exactly when the outcome is not UNDETERMINED."""
        return self.outcome is not ReproductionOutcome.UNDETERMINED


#: The ordered project-outcome rule table. First match wins; order is
#: normative (see module docstring): the two determination gates precede the
#: spec rules, the spec rules follow ``04-PROJECT-LIFECYCLE.md`` section 5
#: numbering (rule 3 before rule 4: a determinate negative Critical closure
#: does not "exceed" INCONCLUSIVE), and ``R-PRJ-5`` is the default so the
#: outcome is total. Predicates are pure functions of the inputs only.
PROJECT_OUTCOME_RULES: tuple[ProjectOutcomeRule, ...] = (
    ProjectOutcomeRule(
        rule_id="R-PRJ-UND-1",
        description=(
            "at least one Requirement is not individually determined"
            " (outcome OPEN): final validation rules do not permit"
            " determination, the final outcome stays UNDETERMINED (AC-02)"
        ),
        outcome=ReproductionOutcome.UNDETERMINED,
        predicate=lambda assessments, closure_allowed: any(
            a.state is RequirementClosureState.UNDETERMINED for a in assessments
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-UND-2",
        description=(
            "a Requirement closes NOT_REPRODUCED while the Closure Contract"
            " is evaluated as not satisfied: the negative closure is not"
            " legitimate (08-STRICT-RECOVERY-CLOSURE.md section 4), so"
            " determination is blocked"
        ),
        outcome=ReproductionOutcome.UNDETERMINED,
        predicate=lambda assessments, closure_allowed: (
            closure_allowed is False
            and any(
                a.state is RequirementClosureState.NOT_REPRODUCED
                for a in assessments
            )
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-UND-3",
        description=(
            "a Requirement closes NOT_REPRODUCED while the Closure Contract"
            " has not been assessed: the negative closure cannot be"
            " confirmed, so determination is blocked"
        ),
        outcome=ReproductionOutcome.UNDETERMINED,
        predicate=lambda assessments, closure_allowed: (
            closure_allowed is None
            and any(
                a.state is RequirementClosureState.NOT_REPRODUCED
                for a in assessments
            )
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-1",
        description=(
            "all formally reported Requirements close as REPRODUCED or"
            " REPRODUCED_WITH_RECOVERY: FULLY_REPRODUCED"
            " (04-PROJECT-LIFECYCLE.md section 5 rule 1)"
        ),
        outcome=ReproductionOutcome.FULLY_REPRODUCED,
        predicate=lambda assessments, closure_allowed: (
            len(assessments) > 0
            and all(
                a.state is RequirementClosureState.REPRODUCED
                for a in assessments
            )
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-2",
        description=(
            "all Critical Requirements reproduced but one or more"
            " Required/Supporting Requirements close NOT_REPRODUCED:"
            " PARTIALLY_REPRODUCED (04-PROJECT-LIFECYCLE.md section 5 rule 2)"
        ),
        outcome=ReproductionOutcome.PARTIALLY_REPRODUCED,
        predicate=lambda assessments, closure_allowed: (
            all(
                a.state is RequirementClosureState.REPRODUCED
                for a in assessments
                if a.record.criticality is Criticality.CRITICAL
            )
            and any(
                a.state is RequirementClosureState.NOT_REPRODUCED
                and a.record.criticality is not Criticality.CRITICAL
                for a in assessments
            )
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-3",
        description=(
            "one or more Critical Requirements close NOT_REPRODUCED under a"
            " satisfied Closure Contract:"
            " NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (04-PROJECT-LIFECYCLE.md"
            " section 5 rule 3; AC-03)"
        ),
        outcome=ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE,
        predicate=lambda assessments, closure_allowed: any(
            a.state is RequirementClosureState.NOT_REPRODUCED
            and a.record.criticality is Criticality.CRITICAL
            for a in assessments
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-4",
        description=(
            "a Requirement is validly INCONCLUSIVE: the project conclusion"
            " cannot exceed INCONCLUSIVE (04-PROJECT-LIFECYCLE.md section 5"
            " rule 4, extended from Critical to every Requirement)"
        ),
        outcome=ReproductionOutcome.INCONCLUSIVE,
        predicate=lambda assessments, closure_allowed: any(
            a.state is RequirementClosureState.INCONCLUSIVE
            for a in assessments
        ),
    ),
    ProjectOutcomeRule(
        rule_id="R-PRJ-5",
        description=(
            "no formally reported Requirements: there is no basis for a"
            " final scientific conclusion (default)"
        ),
        outcome=ReproductionOutcome.UNDETERMINED,
        predicate=lambda assessments, closure_allowed: True,
    ),
)


def _blocking_reasons(
    matched_rule_id: str,
    assessments: tuple[RequirementOutcomeAssessment, ...],
) -> tuple[str, ...]:
    """Stable, machine-readable reasons why determination is blocked.

    Only the UNDETERMINED paths report reasons; determined outcomes report
    none. The reported Requirement ids preserve input order.
    """
    if matched_rule_id == "R-PRJ-UND-1":
        ids = [
            a.record.requirement_id
            for a in assessments
            if a.state is RequirementClosureState.UNDETERMINED
        ]
        return (
            "Requirement(s) not individually determined, final validation"
            f" not reached: {', '.join(ids)}",
        )
    if matched_rule_id in ("R-PRJ-UND-2", "R-PRJ-UND-3"):
        ids = [
            a.record.requirement_id
            for a in assessments
            if a.state is RequirementClosureState.NOT_REPRODUCED
        ]
        if matched_rule_id == "R-PRJ-UND-2":
            return (
                "NOT_REPRODUCED Requirement(s) without a satisfied Closure"
                f" Contract: {', '.join(ids)}",
            )
        return (
            "NOT_REPRODUCED Requirement(s) while the Closure Contract is"
            f" unassessed: {', '.join(ids)}",
        )
    if matched_rule_id == "R-PRJ-5":
        return ("no formally reported Requirements",)
    return ()


def aggregate_project_outcome(
    requirements: Sequence[RequirementOutcomeRecord],
    closure_allowed: bool | None = None,
) -> ProjectOutcomeAssessment:
    """Aggregate the per-Requirement outcomes into the project outcome.

    Pure and deterministic: the outcome is a pure function of the inputs
    (AC-02 determinism). Requirement-level classification happens first
    (compositional determination), then the project rule table decides.
    ``closure_allowed`` is the decision of ``rules.closure.evaluate_closure``
    (``ClosureAssessment.closure_allowed``) -- consumed, never re-derived;
    ``None`` means the Closure Contract has not been assessed and blocks any
    NOT_REPRODUCED-based determination (mirroring the unresolved-gate
    philosophy of ``rules.closure`` AC-01).

    Args:
        requirements: the project's Requirement outcome records, in declared
            order.
        closure_allowed: tri-state closure-contract decision from
            ``rules.closure`` (True satisfied / False evaluated-and-not-
            satisfied / None unassessed).

    Raises:
        TypeError: ``requirements`` is not a sequence, an element is not a
            ``RequirementOutcomeRecord``, or ``closure_allowed`` is not a
            bool or None.

    Returns:
        The full assessment: outcome, per-Requirement classifications, every
        project-rule decision, and the blocking reasons.
    """
    items = _coerce_requirement_records(requirements, "aggregate_project_outcome")
    if closure_allowed is not None and not isinstance(closure_allowed, bool):
        raise TypeError(
            "aggregate_project_outcome expects closure_allowed to be a bool"
            f" or None, got {type(closure_allowed).__name__}"
        )
    requirement_assessments = tuple(
        classify_requirement_outcome(item) for item in items
    )
    rule_decisions: list[ProjectOutcomeRuleDecision] = []
    matched_rule_id: str | None = None
    matched_outcome = ReproductionOutcome.UNDETERMINED  # unreachable default
    for rule in PROJECT_OUTCOME_RULES:
        matched = rule.predicate(requirement_assessments, closure_allowed)
        rule_decisions.append(
            ProjectOutcomeRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                outcome=rule.outcome,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_outcome = rule.outcome
    # R-PRJ-5 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return ProjectOutcomeAssessment(
        ruleset_version=RULESET_VERSION,
        requirements=items,
        closure_allowed=closure_allowed,
        requirement_assessments=requirement_assessments,
        rule_decisions=tuple(rule_decisions),
        outcome=matched_outcome,
        blocking_reasons=_blocking_reasons(matched_rule_id, requirement_assessments),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Method reproducibility aggregation hook (deliverable 3)
# ---------------------------------------------------------------------------

#: The five terminal method-reproducibility ratings in order from best
#: (DIRECTLY_REPRODUCIBLE) to worst (NOT_REPRODUCIBLE). UNDETERMINED and
#: INCONCLUSIVE are non-terminal aggregate states handled by the rules.
METHOD_REPRODUCIBILITY_ORDER: tuple[MethodReproducibility, ...] = (
    MethodReproducibility.DIRECTLY_REPRODUCIBLE,
    MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY,
    MethodReproducibility.REPRODUCIBLE_WITH_METHOD_ADJUSTMENT,
    MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN,
    MethodReproducibility.NOT_REPRODUCIBLE,
)


@dataclass(frozen=True)
class MethodReproducibilityRule:
    """One entry of the ordered method-reproducibility rule table.

    ``reproducibility`` is the value the rule proposes; it is ``None`` only
    for the trailing dynamic default rule (``R-MR-3``), whose value -- the
    worst terminal per-Requirement rating -- depends on the records and is
    computed by the aggregator (see module docstring, normative reading).
    """

    rule_id: str
    description: str
    reproducibility: MethodReproducibility | None
    predicate: Callable[[tuple[MethodReproducibilityRecord, ...]], bool]


@dataclass(frozen=True)
class MethodReproducibilityRuleDecision:
    """Record of one method-reproducibility rule evaluation (audit trail)."""

    rule_id: str
    description: str
    reproducibility: MethodReproducibility
    matched: bool


@dataclass(frozen=True)
class MethodReproducibilityAssessment:
    """Full, auditable result of a method reproducibility aggregation.

    ``records`` is the exact input; ``decisions`` records every rule
    evaluation (the trailing default rule's decision carries the computed
    worst-of value); ``matched_rule_id`` names the deciding rule.
    """

    ruleset_version: str
    records: tuple[MethodReproducibilityRecord, ...]
    reproducibility: MethodReproducibility
    decisions: tuple[MethodReproducibilityRuleDecision, ...]
    matched_rule_id: str


def _worst_reproducibility(
    records: tuple[MethodReproducibilityRecord, ...],
) -> MethodReproducibility:
    """The worst (least reproducible) terminal rating among ``records``.

    Only the five ordered terminal ratings participate; UNDETERMINED /
    INCONCLUSIVE are handled by the rules before this helper is reached. An
    empty or terminal-rating-free set yields UNDETERMINED.
    """
    rated = [
        r.reproducibility
        for r in records
        if r.reproducibility in METHOD_REPRODUCIBILITY_ORDER
    ]
    if not rated:
        return MethodReproducibility.UNDETERMINED
    return max(rated, key=METHOD_REPRODUCIBILITY_ORDER.index)


#: The ordered method-reproducibility rule table. First match wins;
#: ``R-MR-3`` is the default so the aggregation is total. The worst-of value
#: proposed by ``R-MR-3`` is computed by the aggregator (dynamic default).
METHOD_REPRODUCIBILITY_RULES: tuple[MethodReproducibilityRule, ...] = (
    MethodReproducibilityRule(
        rule_id="R-MR-1",
        description=(
            "any Requirement's method reproducibility is UNDETERMINED: the"
            " project-level rating cannot be determined from incomplete"
            " per-Requirement ratings (mirrors the determination gate of"
            " AC-02)"
        ),
        reproducibility=MethodReproducibility.UNDETERMINED,
        predicate=lambda records: any(
            r.reproducibility is MethodReproducibility.UNDETERMINED
            for r in records
        ),
    ),
    MethodReproducibilityRule(
        rule_id="R-MR-2",
        description=(
            "any Requirement's method reproducibility is INCONCLUSIVE: the"
            " project-level rating is INCONCLUSIVE"
        ),
        reproducibility=MethodReproducibility.INCONCLUSIVE,
        predicate=lambda records: any(
            r.reproducibility is MethodReproducibility.INCONCLUSIVE
            for r in records
        ),
    ),
    MethodReproducibilityRule(
        rule_id="R-MR-3",
        description=(
            "project-level method reproducibility is an aggregate: the worst"
            " (least reproducible) terminal per-Requirement rating, the"
            " bottleneck reading of 04-PROJECT-LIFECYCLE.md section 6"
            " (default; the value depends on the records and is computed by"
            " the aggregator)"
        ),
        reproducibility=None,
        predicate=lambda records: True,
    ),
)


def aggregate_method_reproducibility(
    records: Sequence[MethodReproducibilityRecord],
) -> MethodReproducibilityAssessment:
    """Aggregate the per-Requirement method reproducibility ratings.

    The method reproducibility aggregation hook (deliverable 3): given the
    per-Requirement/Goal reproducibility evidence, produces the project-level
    method reproducibility rating per the locked minimal reading (see module
    docstring), for the downstream M3+ goals that produce the project's
    method reproducibility outcome and the WP-90 final integration
    (``17-FDM201-REFERENCE-CASE.md``). The scientific outcome is never
    consulted: method reproducibility is a separate axis
    (``04-PROJECT-LIFECYCLE.md`` section 6).

    Args:
        records: the per-Requirement reproducibility ratings, in declared
            order.

    Raises:
        TypeError: ``records`` is not a sequence, or an element is not a
            ``MethodReproducibilityRecord``.

    Returns:
        The full assessment: aggregated rating plus the auditable trace.
    """
    items = _coerce_method_records(records, "aggregate_method_reproducibility")
    worst = _worst_reproducibility(items)
    decisions: list[MethodReproducibilityRuleDecision] = []
    matched_rule_id: str | None = None
    matched = MethodReproducibility.UNDETERMINED  # unreachable default
    for rule in METHOD_REPRODUCIBILITY_RULES:
        hit = rule.predicate(items)
        value = worst if rule.reproducibility is None else rule.reproducibility
        decisions.append(
            MethodReproducibilityRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                reproducibility=value,
                matched=hit,
            )
        )
        if hit and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched = value
    # R-MR-3 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return MethodReproducibilityAssessment(
        ruleset_version=RULESET_VERSION,
        records=items,
        reproducibility=matched,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_requirement_records(
    requirements: Sequence[RequirementOutcomeRecord], function: str
) -> tuple[RequirementOutcomeRecord, ...]:
    """Coerce a Requirement sequence into a tuple of validated records.

    Raises:
        TypeError: ``requirements`` is not a sequence (a ``str``/``bytes``
            is rejected explicitly), or an element is not a
            ``RequirementOutcomeRecord``.
    """
    if isinstance(requirements, (str, bytes)) or not isinstance(
        requirements, Sequence
    ):
        raise TypeError(
            f"{function} expects a sequence of RequirementOutcomeRecord, got"
            f" {type(requirements).__name__}"
        )
    items = tuple(requirements)
    for item in items:
        if not isinstance(item, RequirementOutcomeRecord):
            raise TypeError(
                f"{function} expects RequirementOutcomeRecord elements, got"
                f" {type(item).__name__}"
            )
    return items


def _coerce_method_records(
    records: Sequence[MethodReproducibilityRecord], function: str
) -> tuple[MethodReproducibilityRecord, ...]:
    """Coerce a reproducibility sequence into a tuple of validated records.

    Raises:
        TypeError: ``records`` is not a sequence (a ``str``/``bytes`` is
            rejected explicitly), or an element is not a
            ``MethodReproducibilityRecord``.
    """
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError(
            f"{function} expects a sequence of MethodReproducibilityRecord,"
            f" got {type(records).__name__}"
        )
    items = tuple(records)
    for item in items:
        if not isinstance(item, MethodReproducibilityRecord):
            raise TypeError(
                f"{function} expects MethodReproducibilityRecord elements,"
                f" got {type(item).__name__}"
            )
    return items
