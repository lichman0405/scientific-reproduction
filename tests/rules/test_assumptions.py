"""Tests for the assumption-effect strict-labeling rules (DEV-M2-G07).

Acceptance coverage:
  * AC-01 -- A0 does not change scientific strict identity: evaluating an
    empty assumption set or an A0-only set keeps the pure-strict ``STRICT``
    label, and a single A0 assumption records ``StrictStatusEffect.NONE``.
  * AC-02 -- A1 is recorded and can classify strict-with-assumptions: any
    assumption set containing an A1 (and no A2) classifies
    ``StrictLabel.STRICT_WITH_ASSUMPTIONS``, a label distinct from pure
    ``STRICT``, and the assessment carries the exact A1 assumption(s) with
    their recorded effect.
  * AC-03 -- A2 prevents pure STRICT labeling: the evaluator never returns
    the pure-strict label when an A2 assumption is present (alone, with A0,
    with A1, or with both), and the result records the A2 assumption and its
    ``DISQUALIFIES_PURE_STRICT`` effect. The grid invariant asserts STRICT
    holds exactly when neither A1 nor A2 is present.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
    StrictStatusEffect,
)
from scientific_reproduction.core.rules.assumptions import (
    ASSUMPTION_EFFECT_RULES,
    RULESET_VERSION,
    STRICT_LABEL_RULES,
    StrictLabel,
    assumption_effect,
    evaluate_strict_label,
)
from tests.core.fixtures import VALID_DOCS

CLASSIFICATIONS = (
    AssumptionClassification.A0_TECHNICAL_DEFAULT,
    AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
    AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
)

EFFECT_BY_CLASSIFICATION: dict[AssumptionClassification, StrictStatusEffect] = {
    AssumptionClassification.A0_TECHNICAL_DEFAULT: StrictStatusEffect.NONE,
    AssumptionClassification.A1_METHODOLOGICAL_DEFAULT: (
        StrictStatusEffect.STRICT_WITH_ASSUMPTIONS
    ),
    AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION: (
        StrictStatusEffect.DISQUALIFIES_PURE_STRICT
    ),
}


def _assumption(
    classification: AssumptionClassification,
    assumption_id: str = "ASM-001",
    strict_status_effect: StrictStatusEffect | None = None,
) -> Assumption:
    """Minimal schema-conformant Assumption registry entry for tests."""
    return Assumption(
        assumption_id=assumption_id,
        parameter="packed_bed_density",
        classification=classification,
        rationale="test rationale",
        source_refs=["SRC-001"],
        strict_status_effect=strict_status_effect,
    )


def _all_assumption_sets(max_assumptions: int = 4) -> list[tuple[Assumption, ...]]:
    """Exhaustive battery: every combination of A0/A1/A2, sizes 0..4."""
    sets: list[tuple[Assumption, ...]] = [()]
    for size in range(1, max_assumptions + 1):
        for values in itertools.product(CLASSIFICATIONS, repeat=size):
            sets.append(
                tuple(
                    _assumption(
                        classification,
                        assumption_id=f"ASM-{size}-{index}-{classification.name}",
                    )
                    for index, classification in enumerate(values)
                )
            )
    return sets


def _expected_label(
    assumptions: tuple[Assumption, ...],
) -> StrictLabel:
    """The label invariant asserted over the grid (see module docstring)."""
    has_a2 = any(
        a.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        for a in assumptions
    )
    has_a1 = any(
        a.classification is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
        for a in assumptions
    )
    if has_a2:
        return StrictLabel.NOT_STRICT
    if has_a1:
        return StrictLabel.STRICT_WITH_ASSUMPTIONS
    return StrictLabel.STRICT


# ---------------------------------------------------------------------------
# Rule table shape (deliverable: assumption effect evaluator)
# ---------------------------------------------------------------------------


def test_assumptions_rulesets_are_versioned_and_total() -> None:
    assert isinstance(RULESET_VERSION, str)
    assert RULESET_VERSION == "1.0"
    # Effect rules: one per classification, total over the classification enum.
    effect_rule_ids = [rule.rule_id for rule in ASSUMPTION_EFFECT_RULES]
    assert len(effect_rule_ids) == len(set(effect_rule_ids)), "rule ids must be unique"
    assert len(ASSUMPTION_EFFECT_RULES) == 3
    for rule in ASSUMPTION_EFFECT_RULES:
        assert isinstance(rule.effect, StrictStatusEffect)
        assert rule.description
    # The trailing default effect rule matches every classification, so the
    # per-assumption effect is total.
    assert (
        ASSUMPTION_EFFECT_RULES[-1].predicate(
            AssumptionClassification.A0_TECHNICAL_DEFAULT
        )
        is True
    )
    # Label rules: 4 with a trailing default, so the label is total too.
    label_rule_ids = [rule.rule_id for rule in STRICT_LABEL_RULES]
    assert len(label_rule_ids) == len(set(label_rule_ids)), "rule ids must be unique"
    assert len(STRICT_LABEL_RULES) == 4
    for rule in STRICT_LABEL_RULES:
        assert isinstance(rule.label, StrictLabel)
        assert rule.description
    assert STRICT_LABEL_RULES[-1].predicate(()) is True


def test_assumptions_strict_label_uses_frozen_vocabulary() -> None:
    # The strict-with-assumptions label is the exact frozen schema value
    # (StrictStatusEffect enum in schemas/assumption.schema.yaml).
    assert StrictLabel.STRICT_WITH_ASSUMPTIONS.value == "STRICT_WITH_ASSUMPTIONS"
    assert (
        StrictLabel.STRICT_WITH_ASSUMPTIONS.value
        == StrictStatusEffect.STRICT_WITH_ASSUMPTIONS.value
    )
    assert StrictLabel.STRICT.value == "STRICT"
    assert StrictLabel.NOT_STRICT.value == "NOT_STRICT"
    assert len(StrictLabel) == 3


def test_assumptions_assessment_model_is_frozen() -> None:
    assessment = evaluate_strict_label(())
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.label = StrictLabel.NOT_STRICT  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC-01: A0 does not change scientific strict identity
# ---------------------------------------------------------------------------


def test_assumptions_empty_set_keeps_pure_strict_label() -> None:
    # No assumptions made: the strict identity is untouched.
    assessment = evaluate_strict_label(())
    assert assessment.label is StrictLabel.STRICT
    assert assessment.matched_label_rule_id == "R-STRICT-1"
    assert assessment.effects == ()


def test_assumptions_a0_only_keeps_pure_strict_label() -> None:
    # An A0 registry entry alone leaves the pure-strict label untouched
    # (AC-01).
    a0 = _assumption(AssumptionClassification.A0_TECHNICAL_DEFAULT)
    assessment = evaluate_strict_label((a0,))
    assert assessment.label is StrictLabel.STRICT
    assert assessment.matched_label_rule_id == "R-STRICT-4"
    # The A0 assumption's recorded effect is NONE.
    (effect,) = assessment.effects
    assert effect.assumption == a0
    assert effect.effect is StrictStatusEffect.NONE
    assert effect.rule_id == "R-EFF-3"


def test_assumptions_a0_only_label_is_identical_to_no_assumptions() -> None:
    # AC-01: adding only A0 entries to a pure-strict classification never
    # changes its label -- both stay STRICT.
    a0 = _assumption(AssumptionClassification.A0_TECHNICAL_DEFAULT, "ASM-A0-1")
    label_with_a0 = evaluate_strict_label((a0,)).label
    label_empty = evaluate_strict_label(()).label
    assert label_with_a0 is label_empty is StrictLabel.STRICT


def test_assumptions_a0_effect_is_none_for_any_a0_entry() -> None:
    for index in range(3):
        a0 = _assumption(
            AssumptionClassification.A0_TECHNICAL_DEFAULT, f"ASM-A0-{index}"
        )
        assert assumption_effect(a0).effect is StrictStatusEffect.NONE
        assert assumption_effect(a0).rule_id == "R-EFF-3"


# ---------------------------------------------------------------------------
# AC-02: A1 is recorded and can classify strict-with-assumptions
# ---------------------------------------------------------------------------


def test_assumptions_a1_classifies_strict_with_assumptions() -> None:
    a1 = _assumption(AssumptionClassification.A1_METHODOLOGICAL_DEFAULT)
    assessment = evaluate_strict_label((a1,))
    # The label is distinct from the pure STRICT label.
    assert assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    assert assessment.label is not StrictLabel.STRICT
    assert assessment.matched_label_rule_id == "R-STRICT-3"


def test_assumptions_a1_is_recorded_in_the_result() -> None:
    # AC-02: the recorded assumption(s) are carried back so the classification
    # is auditable -- exact input, per-assumption effect, and label decision.
    a1 = _assumption(
        AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
        "ASM-DRYING-001",
    )
    assessment = evaluate_strict_label((a1,))
    assert assessment.assumptions == (a1,)
    (effect,) = assessment.effects
    assert effect.assumption == a1
    assert effect.effect is StrictStatusEffect.STRICT_WITH_ASSUMPTIONS
    assert effect.rule_id == "R-EFF-2"
    matched = next(
        decision
        for decision in assessment.label_decisions
        if decision.rule_id == "R-STRICT-3"
    )
    assert matched.matched is True
    assert matched.label is StrictLabel.STRICT_WITH_ASSUMPTIONS


def test_assumptions_a0_plus_a1_classifies_strict_with_assumptions() -> None:
    a0 = _assumption(AssumptionClassification.A0_TECHNICAL_DEFAULT, "ASM-A0-1")
    a1 = _assumption(AssumptionClassification.A1_METHODOLOGICAL_DEFAULT, "ASM-A1-1")
    assessment = evaluate_strict_label((a0, a1))
    assert assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    effects_by_id = {effect.assumption.assumption_id: effect for effect in assessment.effects}
    assert effects_by_id["ASM-A0-1"].effect is StrictStatusEffect.NONE
    assert (
        effects_by_id["ASM-A1-1"].effect
        is StrictStatusEffect.STRICT_WITH_ASSUMPTIONS
    )


def test_assumptions_multiple_a1_still_strict_with_assumptions() -> None:
    a1s = tuple(
        _assumption(
            AssumptionClassification.A1_METHODOLOGICAL_DEFAULT, f"ASM-A1-{index}"
        )
        for index in range(3)
    )
    assessment = evaluate_strict_label(a1s)
    assert assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    assert len(assessment.effects) == 3
    assert all(
        effect.effect is StrictStatusEffect.STRICT_WITH_ASSUMPTIONS
        for effect in assessment.effects
    )


# ---------------------------------------------------------------------------
# AC-03: A2 prevents pure STRICT labeling
# ---------------------------------------------------------------------------


def test_assumptions_a2_never_returns_pure_strict_label() -> None:
    a2 = _assumption(AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION)
    assessment = evaluate_strict_label((a2,))
    # The pure-strict label must never be produced when an A2 is present.
    assert assessment.label is not StrictLabel.STRICT
    assert assessment.label is StrictLabel.NOT_STRICT
    assert assessment.matched_label_rule_id == "R-STRICT-2"


def test_assumptions_a2_is_recorded_with_its_effect() -> None:
    # AC-03: the result records the A2 assumption and its disqualifying
    # effect, so the prevented pure-strict labeling is auditable.
    a2 = _assumption(
        AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        "ASM-PACKING-001",
    )
    assessment = evaluate_strict_label((a2,))
    assert assessment.assumptions == (a2,)
    (effect,) = assessment.effects
    assert effect.assumption == a2
    assert effect.effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT
    assert effect.rule_id == "R-EFF-1"
    matched = next(
        decision
        for decision in assessment.label_decisions
        if decision.rule_id == "R-STRICT-2"
    )
    assert matched.matched is True
    assert matched.label is StrictLabel.NOT_STRICT


@pytest.mark.parametrize(
    "assumptions",
    [
        (AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,),
        (
            AssumptionClassification.A0_TECHNICAL_DEFAULT,
            AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        ),
        (
            AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
            AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        ),
        (
            AssumptionClassification.A0_TECHNICAL_DEFAULT,
            AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
            AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        ),
    ],
    ids=["a2-only", "a0+a2", "a1+a2", "a0+a1+a2"],
)
def test_assumptions_any_a2_combination_never_pure_strict(
    assumptions: tuple[AssumptionClassification, ...],
) -> None:
    # AC-03: with an A2 present -- alone or alongside A0/A1 -- the evaluator
    # never returns the pure-strict label.
    entries = tuple(
        _assumption(classification, assumption_id=f"ASM-{index}")
        for index, classification in enumerate(assumptions)
    )
    assessment = evaluate_strict_label(entries)
    assert assessment.label is not StrictLabel.STRICT
    assert assessment.label is StrictLabel.NOT_STRICT


def test_assumptions_a2_dominates_a1() -> None:
    # The A2 disqualification dominates the A1 strict-with-assumptions
    # classification (R-STRICT-2 precedes R-STRICT-3 in the rule table).
    a1 = _assumption(AssumptionClassification.A1_METHODOLOGICAL_DEFAULT, "ASM-A1-1")
    a2 = _assumption(AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION, "ASM-A2-1")
    assessment = evaluate_strict_label((a1, a2))
    assert assessment.label is StrictLabel.NOT_STRICT
    assert assessment.label is not StrictLabel.STRICT_WITH_ASSUMPTIONS


def test_assumptions_a2_from_frozen_example_disqualifies() -> None:
    # The FDM-201 reference example registers an A2 assumption with
    # DISQUALIFIES_PURE_STRICT; the evaluator's derived effect matches the
    # frozen example and prevents pure STRICT labeling (AC-03).
    example = Assumption.from_dict(VALID_DOCS["assumption"])
    assert example.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
    assessment = evaluate_strict_label((example,))
    assert assessment.label is StrictLabel.NOT_STRICT
    (effect,) = assessment.effects
    assert effect.effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT


# ---------------------------------------------------------------------------
# Exhaustive grid invariant + totality
# ---------------------------------------------------------------------------


def test_assumptions_grid_invariant_over_all_combinations() -> None:
    # The label bi-implication over the exhaustive grid: STRICT holds exactly
    # when neither A1 nor A2 is present; STRICT_WITH_ASSUMPTIONS holds exactly
    # when some A1 and no A2 is present; NOT_STRICT holds exactly when some A2
    # is present (AC-01, AC-02, AC-03 combined).
    for assumptions in _all_assumption_sets():
        assessment = evaluate_strict_label(assumptions)
        assert assessment.label == _expected_label(assumptions)
        assert assessment.assumptions == assumptions


def test_assumptions_every_assumption_set_produces_a_label() -> None:
    # Totality: every input in the exhaustive grid yields exactly one label
    # and a matched rule.
    for assumptions in _all_assumption_sets():
        assessment = evaluate_strict_label(assumptions)
        assert isinstance(assessment.label, StrictLabel)
        assert assessment.matched_label_rule_id in {
            rule.rule_id for rule in STRICT_LABEL_RULES
        }


def test_assumptions_all_labels_reachable_over_grid() -> None:
    outcomes = {evaluate_strict_label(s).label for s in _all_assumption_sets()}
    assert outcomes == {
        StrictLabel.STRICT,
        StrictLabel.STRICT_WITH_ASSUMPTIONS,
        StrictLabel.NOT_STRICT,
    }


# ---------------------------------------------------------------------------
# AC-02 determinism: same input -> identical assessment
# ---------------------------------------------------------------------------


def test_assumptions_same_input_yields_identical_assessment() -> None:
    for assumptions in _all_assumption_sets():
        first = evaluate_strict_label(assumptions)
        second = evaluate_strict_label(assumptions)
        assert first == second
        assert first.label == second.label
        assert first.effects == second.effects
        assert first.label_decisions == second.label_decisions
        assert first.matched_label_rule_id == second.matched_label_rule_id


def test_assumptions_deterministic_across_repeated_evaluations() -> None:
    sample = _all_assumption_sets()[::8]  # representative subset of the grid
    for assumptions in sample:
        reference = evaluate_strict_label(assumptions)
        for _ in range(100):
            assert evaluate_strict_label(assumptions) == reference


def test_assumptions_equivalent_inputs_classify_identically() -> None:
    # Reconstructed (equal) assumption objects must produce the identical
    # assessment (input equivalence determinism).
    for assumptions in _all_assumption_sets():
        rebuilt = tuple(
            Assumption(
                assumption_id=a.assumption_id,
                parameter=a.parameter,
                classification=a.classification,
                rationale=a.rationale,
                source_refs=list(a.source_refs),
            )
            for a in assumptions
        )
        assert evaluate_strict_label(rebuilt) == evaluate_strict_label(assumptions)


def test_assumptions_label_is_order_independent() -> None:
    # The label depends on the assumption set, not its order: any permutation
    # of the input yields the same label and the same per-assumption effects.
    assumptions = (
        _assumption(AssumptionClassification.A1_METHODOLOGICAL_DEFAULT, "ASM-A1-1"),
        _assumption(AssumptionClassification.A0_TECHNICAL_DEFAULT, "ASM-A0-1"),
        _assumption(AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION, "ASM-A2-1"),
    )
    reference = evaluate_strict_label(assumptions)
    for permuted in itertools.permutations(assumptions):
        assessment = evaluate_strict_label(permuted)
        assert assessment.label == reference.label
        assert {
            (effect.assumption.assumption_id, effect.effect)
            for effect in assessment.effects
        } == {
            (effect.assumption.assumption_id, effect.effect)
            for effect in reference.effects
        }


def test_assumptions_duplicate_entries_evaluate_deterministically() -> None:
    # Even a malformed registry with the same entry twice evaluates
    # deterministically and never as pure STRICT once an A2 is duplicated.
    a2 = _assumption(AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION, "ASM-A2-1")
    assessment = evaluate_strict_label((a2, a2))
    assert assessment.label is StrictLabel.NOT_STRICT
    assert len(assessment.effects) == 2
    assert all(
        effect.effect is StrictStatusEffect.DISQUALIFIES_PURE_STRICT
        for effect in assessment.effects
    )


# ---------------------------------------------------------------------------
# Auditability: assessment carries the exact inputs + decisions
# ---------------------------------------------------------------------------


def test_assumptions_assessment_records_exact_inputs() -> None:
    for assumptions in _all_assumption_sets():
        assessment = evaluate_strict_label(assumptions)
        assert assessment.assumptions == assumptions
        assert assessment.ruleset_version == RULESET_VERSION


def test_assumptions_effects_track_every_input_in_order() -> None:
    for assumptions in _all_assumption_sets():
        assessment = evaluate_strict_label(assumptions)
        assert [effect.assumption for effect in assessment.effects] == list(assumptions)
        for effect in assessment.effects:
            assert effect.effect is EFFECT_BY_CLASSIFICATION[effect.assumption.classification]


def test_assumptions_label_decisions_record_every_rule() -> None:
    assumptions = (
        _assumption(AssumptionClassification.A1_METHODOLOGICAL_DEFAULT, "ASM-A1-1"),
        _assumption(AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION, "ASM-A2-1"),
    )
    assessment = evaluate_strict_label(assumptions)
    recorded_ids = [decision.rule_id for decision in assessment.label_decisions]
    assert recorded_ids == [rule.rule_id for rule in STRICT_LABEL_RULES]
    for decision, rule in zip(
        assessment.label_decisions, STRICT_LABEL_RULES, strict=True
    ):
        assert decision.rule_id == rule.rule_id
        assert decision.description == rule.description
        assert decision.label == rule.label


def test_assumptions_matched_rule_is_the_first_true_predicate() -> None:
    # The audit trail is self-consistent for every input in the grid: the
    # matched label rule is the first rule whose predicate holds, and its
    # proposed label is exactly the label returned.
    for assumptions in _all_assumption_sets():
        assessment = evaluate_strict_label(assumptions)
        matched = next(
            decision for decision in assessment.label_decisions if decision.matched
        )
        assert matched.rule_id == assessment.matched_label_rule_id
        for earlier in assessment.label_decisions[: assessment.label_decisions.index(matched)]:
            assert earlier.matched is False
        rule = next(r for r in STRICT_LABEL_RULES if r.rule_id == matched.rule_id)
        assert rule.predicate(assumptions) is True
        assert rule.label == assessment.label
