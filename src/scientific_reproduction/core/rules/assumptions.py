"""Assumption-effect evaluation rules for strict-reproduction labeling (DEV-M2-G07).

Evaluates how registered assumptions (``schemas/assumption.schema.yaml``,
modeled as ``Assumption`` in ``core/models.py``) affect the scientific
strict-reproduction label of a reproduction, and records every decision for
the audit trail. The vocabulary is frozen:

* classification: ``A0_TECHNICAL_DEFAULT`` / ``A1_METHODOLOGICAL_DEFAULT`` /
  ``A2_SCIENTIFIC_ASSUMPTION`` (``AssumptionClassification``), matching
  08-STRICT-RECOVERY-CLOSURE.md section 3 (Assumption Registry);
* per-assumption strict-status effect: ``NONE`` /
  ``STRICT_WITH_ASSUMPTIONS`` / ``DISQUALIFIES_PURE_STRICT``
  (``StrictStatusEffect``), matching the assumption schema.

There is no LLM, no randomness and no wall-clock dependence anywhere in this
module: the same assumption input always yields the same label (AC-02), every
label is produced by an ordered rule table whose decisions are all recorded
(auditability), and the effect of each classification is fixed by the frozen
vocabulary:

* A0 -- a technical default never changes the scientific strict identity of a
  reproduction: an A0-only assumption set keeps the pure-strict ``STRICT``
  label (AC-01);
* A1 -- an explicitly recorded methodological default classifies the
  reproduction as ``STRICT_WITH_ASSUMPTIONS`` and is carried in the result so
  the classification is auditable (AC-02);
* A2 -- a scientific assumption (one that changes the reproduction's
  scientific meaning) disqualifies pure strict reproduction: whenever any A2
  is present the label is never the pure-strict ``STRICT``, and the A2
  assumption and its effect are recorded (AC-03). A2 dominates over A1: an
  A1+A2 set is NOT_STRICT, never STRICT_WITH_ASSUMPTIONS.

The label model
---------------
The reproduction-level label (``StrictLabel``) is derived from the *set* of
assumptions recorded against the reproduction. Labels are ordered
coarser-to-finer in the rule table below; the first rule whose predicate
matches decides (``matched_label_rule_id``):

1. ``R-STRICT-1``  no assumptions recorded                            -> STRICT
2. ``R-STRICT-2``  any A2 scientific assumption present               -> NOT_STRICT
3. ``R-STRICT-3``  any A1 methodological default present              -> STRICT_WITH_ASSUMPTIONS
4. ``R-STRICT-4``  only A0 technical defaults present (default)       -> STRICT

``STRICT`` therefore holds exactly when the assumption set contains neither
an A2 nor an A1 entry; ``STRICT_WITH_ASSUMPTIONS`` holds exactly when at
least one A1 is present and no A2 is present; ``NOT_STRICT`` holds exactly
when at least one A2 is present. The bi-implication is asserted over the
exhaustive classification grid in the tests.

The per-assumption effect model
-------------------------------
Each assumption in the input is additionally mapped to its frozen
``StrictStatusEffect`` through an ordered rule table
(``ASSUMPTION_EFFECT_RULES``), so the audit trail records, for every single
assumption, which rule produced its effect:

1. ``R-EFF-1``  A2_SCIENTIFIC_ASSUMPTION  -> DISQUALIFIES_PURE_STRICT
2. ``R-EFF-2``  A1_METHODOLOGICAL_DEFAULT -> STRICT_WITH_ASSUMPTIONS
3. ``R-EFF-3``  A0_TECHNICAL_DEFAULT      -> NONE (default)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
    StrictStatusEffect,
)

__all__ = [
    "RULESET_VERSION",
    "StrictLabel",
    "StrictLabelRule",
    "StrictLabelRuleDecision",
    "AssumptionEffectRule",
    "AssumptionEffectDecision",
    "StrictLabelAssessment",
    "ASSUMPTION_EFFECT_RULES",
    "STRICT_LABEL_RULES",
    "assumption_effect",
    "evaluate_strict_label",
]

#: Version of the rule tables. Bumped whenever a mapping changes; recorded in
#: every assessment so old classifications stay interpretable.
RULESET_VERSION: str = "1.0"


class StrictLabel(StrEnum):
    """Scientific strict-reproduction label for a reproduction.

    ``STRICT_WITH_ASSUMPTIONS`` is the exact value of the frozen
    ``StrictStatusEffect`` enum (``schemas/assumption.schema.yaml``); the
    other two labels follow the same spelling convention.
    """

    STRICT = "STRICT"
    STRICT_WITH_ASSUMPTIONS = "STRICT_WITH_ASSUMPTIONS"
    NOT_STRICT = "NOT_STRICT"


@dataclass(frozen=True)
class StrictLabelRule:
    """One entry of the ordered strict-label rule table."""

    rule_id: str
    description: str
    label: StrictLabel
    predicate: Callable[[tuple[Assumption, ...]], bool]


@dataclass(frozen=True)
class StrictLabelRuleDecision:
    """Record of one label-rule evaluation for a given assumption set."""

    rule_id: str
    description: str
    label: StrictLabel
    matched: bool


@dataclass(frozen=True)
class AssumptionEffectRule:
    """One entry of the ordered per-assumption effect rule table."""

    rule_id: str
    description: str
    effect: StrictStatusEffect
    predicate: Callable[[AssumptionClassification], bool]


@dataclass(frozen=True)
class AssumptionEffectDecision:
    """Record of one assumption's strict-status effect (audit trail)."""

    assumption: Assumption
    effect: StrictStatusEffect
    rule_id: str
    description: str


@dataclass(frozen=True)
class StrictLabelAssessment:
    """Full, auditable result of a strict-label evaluation.

    ``assumptions`` is the exact assumption input (a tuple, so the assessment
    is hashable and comparable); ``effects`` records, in input order, the
    effect decision for every single assumption; ``label_decisions`` records
    the outcome of every label rule (in evaluation order);
    ``matched_label_rule_id`` names the rule that decided the label (``None``
    is impossible: the trailing default rule always matches).
    """

    ruleset_version: str
    assumptions: tuple[Assumption, ...]
    label: StrictLabel
    effects: tuple[AssumptionEffectDecision, ...]
    label_decisions: tuple[StrictLabelRuleDecision, ...]
    matched_label_rule_id: str


#: The ordered per-assumption effect rule table. First match wins; order is
#: normative (see module docstring). Predicates are pure functions of the
#: classification only.
ASSUMPTION_EFFECT_RULES: tuple[AssumptionEffectRule, ...] = (
    AssumptionEffectRule(
        rule_id="R-EFF-1",
        description=(
            "A2 scientific assumption changes the reproduction's scientific "
            "meaning and disqualifies pure strict reproduction"
        ),
        effect=StrictStatusEffect.DISQUALIFIES_PURE_STRICT,
        predicate=lambda c: c is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
    ),
    AssumptionEffectRule(
        rule_id="R-EFF-2",
        description=(
            "A1 methodological default is explicitly recorded and classifies "
            "the reproduction as strict-with-assumptions"
        ),
        effect=StrictStatusEffect.STRICT_WITH_ASSUMPTIONS,
        predicate=lambda c: c is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
    ),
    AssumptionEffectRule(
        rule_id="R-EFF-3",
        description=(
            "A0 technical default does not change the scientific strict "
            "identity (default)"
        ),
        effect=StrictStatusEffect.NONE,
        predicate=lambda c: True,
    ),
)


#: The ordered strict-label rule table. First match wins; order is normative
#: (see module docstring). Predicates are pure functions of the assumption
#: set only. R-STRICT-2 sits before R-STRICT-3 so that an A2 dominates any
#: A1: a set with both is NOT_STRICT, never STRICT_WITH_ASSUMPTIONS (AC-03).
STRICT_LABEL_RULES: tuple[StrictLabelRule, ...] = (
    StrictLabelRule(
        rule_id="R-STRICT-1",
        description="no assumptions recorded: pure strict identity unchanged",
        label=StrictLabel.STRICT,
        predicate=lambda assumptions: len(assumptions) == 0,
    ),
    StrictLabelRule(
        rule_id="R-STRICT-2",
        description=(
            "at least one A2 scientific assumption is present: pure STRICT "
            "labeling is prevented"
        ),
        label=StrictLabel.NOT_STRICT,
        predicate=lambda assumptions: any(
            a.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
            for a in assumptions
        ),
    ),
    StrictLabelRule(
        rule_id="R-STRICT-3",
        description=(
            "at least one A1 methodological default is recorded: classifies "
            "as strict-with-assumptions"
        ),
        label=StrictLabel.STRICT_WITH_ASSUMPTIONS,
        predicate=lambda assumptions: any(
            a.classification is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
            for a in assumptions
        ),
    ),
    StrictLabelRule(
        rule_id="R-STRICT-4",
        description=(
            "only A0 technical defaults (or an empty set) are present: "
            "scientific strict identity unchanged (default)"
        ),
        label=StrictLabel.STRICT,
        predicate=lambda assumptions: True,
    ),
)


def assumption_effect(assumption: Assumption) -> AssumptionEffectDecision:
    """Evaluate the strict-status effect of a single assumption.

    Pure and deterministic: the effect is a pure function of the assumption's
    frozen ``classification``. The returned decision records the exact
    assumption and the rule that produced the effect, so any single
    assumption's recorded effect is auditable.

    Args:
        assumption: the registry entry to evaluate.

    Returns:
        The effect decision: the frozen ``StrictStatusEffect`` plus the
        deciding rule and its description.
    """
    matched_rule: AssumptionEffectRule = ASSUMPTION_EFFECT_RULES[-1]  # unreachable default
    for rule in ASSUMPTION_EFFECT_RULES:
        if rule.predicate(assumption.classification):
            matched_rule = rule
            break
    return AssumptionEffectDecision(
        assumption=assumption,
        effect=matched_rule.effect,
        rule_id=matched_rule.rule_id,
        description=matched_rule.description,
    )


def evaluate_strict_label(assumptions: Sequence[Assumption]) -> StrictLabelAssessment:
    """Evaluate the scientific strict-reproduction label of an assumption set.

    Pure and deterministic: the result depends only on the given assumptions
    (AC-02 determinism). The returned :class:`StrictLabelAssessment` records
    the exact assumption input, the per-assumption effect decisions and every
    label-rule decision, so any label is reproducible and auditable.

    The label is total: every assumption set produces exactly one
    :class:`StrictLabel` (the trailing default rule always matches). An empty
    set and an A0-only set both keep the pure-strict ``STRICT`` label
    (AC-01); any A1 without A2 classifies ``STRICT_WITH_ASSUMPTIONS``
    (AC-02); any A2 never produces ``STRICT`` and instead classifies
    ``NOT_STRICT`` (AC-03).

    Args:
        assumptions: the registered assumptions affecting the reproduction,
            in any order (the label is order-independent).

    Returns:
        The full assessment: label plus the auditable trace.
    """
    items = tuple(assumptions)
    effects = tuple(assumption_effect(assumption) for assumption in items)

    label_decisions: list[StrictLabelRuleDecision] = []
    matched_label_rule_id: str | None = None
    matched_label = StrictLabel.STRICT  # unreachable default
    for rule in STRICT_LABEL_RULES:
        matched = rule.predicate(items)
        label_decisions.append(
            StrictLabelRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                label=rule.label,
                matched=matched,
            )
        )
        if matched and matched_label_rule_id is None:
            matched_label_rule_id = rule.rule_id
            matched_label = rule.label
    # R-STRICT-4 (default) always matches, so this can never be None.
    assert matched_label_rule_id is not None
    return StrictLabelAssessment(
        ruleset_version=RULESET_VERSION,
        assumptions=items,
        label=matched_label,
        effects=effects,
        label_decisions=tuple(label_decisions),
        matched_label_rule_id=matched_label_rule_id,
    )
