"""Checklist-driven criticality classification rules (DEV-M2-G04).

Classifies a reproduction Requirement as ``CRITICAL`` / ``REQUIRED`` /
``SUPPORTING`` (values frozen in ``schemas/requirement.schema.yaml``, modeled
as ``Criticality`` in ``core/models.py``) from a **structured checklist** of
deterministic boolean inputs. There is no LLM, no randomness and no
wall-clock dependence anywhere in this module: the same checklist always
yields the same classification (AC-02), every classification is produced by
an ordered rule table whose decisions are all recorded (AC-03), and a
finding that merely touches a main-figure position can never by itself be
CRITICAL -- CRITICAL additionally requires the finding to invalidate the
paper's main result or change its conclusion (AC-01).

The checklist model
-------------------
Each boolean input answers one audit question about the finding that the
Requirement represents:

``affects_main_figure``
    The finding concerns a result presented in a main-figure (or other
    main-result presentation) position of the target paper. This input alone
    must NEVER classify as CRITICAL (AC-01).
``invalidates_main_result``
    The finding contradicts / falsifies the paper's headline main result.
``changes_paper_conclusion``
    The finding, if taken at face value, changes the paper's stated
    conclusion.
``affects_required_step``
    The finding affects a required reproduction step (a step without which
    the reproduction cannot proceed as planned).
``supporting_detail``
    The finding concerns only supporting material (non-headline,
    non-blocking detail).

The rule mapping
----------------
Rules are evaluated strictly in table order; the first rule whose predicate
matches decides the classification (``matched_rule_id``), and *every* rule
evaluation is recorded in ``CriticalityAssessment.decisions`` so the
classification is fully traceable to its inputs:

1. ``R-CRIT-1``  main-figure position AND main result invalidated            -> CRITICAL
2. ``R-CRIT-2``  main-figure position AND conclusion changed                 -> CRITICAL
3. ``R-REQ-1``   main-figure position only (no conclusion impact)            -> REQUIRED
4. ``R-REQ-2``   main result invalidated or conclusion changed (no figure)   -> REQUIRED
5. ``R-REQ-3``   affects a required reproduction step                        -> REQUIRED
6. ``R-SUP-1``   supporting detail only                                      -> SUPPORTING
7. ``R-SUP-2``   default: no checklist impact recorded                       -> SUPPORTING

CRITICAL therefore holds exactly when ``affects_main_figure`` AND
(``invalidates_main_result`` OR ``changes_paper_conclusion``) -- the
bi-implication is asserted over the full input grid in the tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from scientific_reproduction.core.models import Criticality

__all__ = [
    "RULESET_VERSION",
    "CriticalityChecklist",
    "CriticalityRule",
    "CriticalityRuleDecision",
    "CriticalityAssessment",
    "CRITICALITY_RULES",
    "classify_criticality",
]

#: Version of the rule table. Bumped whenever the mapping changes; recorded
#: in every assessment so old classifications stay interpretable (AC-03).
RULESET_VERSION: str = "1.0"

#: Field evaluation order of the checklist (also the canonical to_dict order).
CHECKLIST_FIELDS: tuple[str, ...] = (
    "affects_main_figure",
    "invalidates_main_result",
    "changes_paper_conclusion",
    "affects_required_step",
    "supporting_detail",
)


@dataclass(frozen=True)
class CriticalityChecklist:
    """Structured, auditable checklist inputs for criticality classification.

    Every field is a plain boolean (``False`` when the finding does not
    exhibit that property). The frozen dataclass makes a checklist hashable
    and comparable, so "same checklist -> same classification" is directly
    testable (AC-02) and the exact input is preserved in the assessment
    (AC-03).
    """

    affects_main_figure: bool = False
    invalidates_main_result: bool = False
    changes_paper_conclusion: bool = False
    affects_required_step: bool = False
    supporting_detail: bool = False

    def to_dict(self) -> dict[str, bool]:
        """Plain dict of the checklist answers in canonical field order."""
        return {name: getattr(self, name) for name in CHECKLIST_FIELDS}


@dataclass(frozen=True)
class CriticalityRuleDecision:
    """Record of one rule evaluation for a given checklist (AC-03)."""

    rule_id: str
    description: str
    criticality: Criticality
    matched: bool


@dataclass(frozen=True)
class CriticalityAssessment:
    """Full, auditable result of a criticality classification (AC-03).

    ``checklist`` is the exact input checklist that produced the
    classification; ``decisions`` records the outcome of every rule in the
    table (in evaluation order); ``matched_rule_id`` names the rule that
    decided the classification (``None`` is impossible: the final default
    rule always matches).
    """

    checklist: CriticalityChecklist
    criticality: Criticality
    decisions: tuple[CriticalityRuleDecision, ...]
    matched_rule_id: str


@dataclass(frozen=True)
class CriticalityRule:
    """One entry of the ordered criticality rule table."""

    rule_id: str
    description: str
    criticality: Criticality
    predicate: Callable[[CriticalityChecklist], bool]


#: The ordered rule table. First match wins; order is normative (see module
#: docstring). Predicates are pure functions of the checklist only.
CRITICALITY_RULES: tuple[CriticalityRule, ...] = (
    CriticalityRule(
        rule_id="R-CRIT-1",
        description="finding affects a main-figure position and invalidates the main result",
        criticality=Criticality.CRITICAL,
        predicate=lambda c: c.affects_main_figure and c.invalidates_main_result,
    ),
    CriticalityRule(
        rule_id="R-CRIT-2",
        description="finding affects a main-figure position and changes the paper's conclusion",
        criticality=Criticality.CRITICAL,
        predicate=lambda c: c.affects_main_figure and c.changes_paper_conclusion,
    ),
    CriticalityRule(
        rule_id="R-REQ-1",
        description=(
            "finding affects a main-figure position without invalidating the main "
            "result or changing the conclusion (main-figure location alone)"
        ),
        criticality=Criticality.REQUIRED,
        predicate=lambda c: c.affects_main_figure,
    ),
    CriticalityRule(
        rule_id="R-REQ-2",
        description=(
            "finding invalidates the main result or changes the conclusion outside "
            "a main-figure position"
        ),
        criticality=Criticality.REQUIRED,
        predicate=lambda c: c.invalidates_main_result or c.changes_paper_conclusion,
    ),
    CriticalityRule(
        rule_id="R-REQ-3",
        description="finding affects a required reproduction step",
        criticality=Criticality.REQUIRED,
        predicate=lambda c: c.affects_required_step,
    ),
    CriticalityRule(
        rule_id="R-SUP-1",
        description="finding is only a supporting detail",
        criticality=Criticality.SUPPORTING,
        predicate=lambda c: c.supporting_detail,
    ),
    CriticalityRule(
        rule_id="R-SUP-2",
        description="no checklist impact recorded (default)",
        criticality=Criticality.SUPPORTING,
        predicate=lambda c: True,
    ),
)


def classify_criticality(checklist: CriticalityChecklist) -> CriticalityAssessment:
    """Classify a checklist into CRITICAL / REQUIRED / SUPPORTING.

    Pure and deterministic: the result depends only on ``checklist``
    (AC-02). The returned :class:`CriticalityAssessment` records the exact
    input checklist and every rule decision, so any classification is
    reproducible and auditable (AC-03).

    Args:
        checklist: structured boolean checklist describing the finding.

    Returns:
        The full assessment: classification plus the auditable trace.
    """
    decisions: list[CriticalityRuleDecision] = []
    matched_rule_id: str | None = None
    matched_criticality = Criticality.SUPPORTING  # unreachable default
    for rule in CRITICALITY_RULES:
        matched = rule.predicate(checklist)
        decisions.append(
            CriticalityRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                criticality=rule.criticality,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_criticality = rule.criticality
    # R-SUP-2 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return CriticalityAssessment(
        checklist=checklist,
        criticality=matched_criticality,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )
