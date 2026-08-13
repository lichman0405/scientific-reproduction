"""Tests for the checklist-driven criticality rules (DEV-M2-G04).

Acceptance coverage:
  * AC-01 -- main-figure location ALONE cannot force CRITICAL: the
    single-condition battery proves that a finding touching only a main
    figure (or only any single checklist input) classifies below CRITICAL,
    and the grid invariant proves CRITICAL holds exactly when a main-figure
    position AND conclusion impact (invalidated main result / changed
    conclusion) are both present.
  * AC-02 -- the same checklist always yields the identical classification:
    every checklist in the exhaustive 2^5 input grid is evaluated repeatedly
    and compared for exact equality (pure, deterministic function).
  * AC-03 -- every classification is traceable to its inputs: the assessment
    records the exact checklist and every rule decision; tests assert the
    recorded inputs equal the inputs given and that the matched rule is the
    first rule whose predicate holds.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from scientific_reproduction.core.models import Criticality
from scientific_reproduction.core.rules.criticality import (
    CRITICALITY_RULES,
    RULESET_VERSION,
    CriticalityChecklist,
    classify_criticality,
)

CHECKLIST_FIELDS = (
    "affects_main_figure",
    "invalidates_main_result",
    "changes_paper_conclusion",
    "affects_required_step",
    "supporting_detail",
)


def _all_checklists() -> list[CriticalityChecklist]:
    """Exhaustive battery: every combination of the five boolean inputs."""
    return [
        CriticalityChecklist(**dict(zip(CHECKLIST_FIELDS, values)))
        for values in itertools.product((False, True), repeat=len(CHECKLIST_FIELDS))
    ]


# ---------------------------------------------------------------------------
# Rule table shape (deliverable: checklist model + rule mapping)
# ---------------------------------------------------------------------------


def test_critical_ruleset_is_versioned_and_total() -> None:
    assert isinstance(RULESET_VERSION, str)
    assert RULESET_VERSION == "1.0"
    rule_ids = [rule.rule_id for rule in CRITICALITY_RULES]
    assert len(rule_ids) == len(set(rule_ids)), "rule ids must be unique"
    assert len(CRITICALITY_RULES) == 7
    for rule in CRITICALITY_RULES:
        assert isinstance(rule.criticality, Criticality)
        assert rule.description
    # The trailing default rule matches every checklist, so classification is
    # total: every checklist yields exactly one of the three enum values.
    assert CRITICALITY_RULES[-1].predicate(CriticalityChecklist()) is True


def test_critical_checklist_model_is_frozen_all_bool() -> None:
    checklist = CriticalityChecklist()
    with pytest.raises(dataclasses.FrozenInstanceError):
        checklist.affects_main_figure = True  # type: ignore[misc]
    for field in dataclasses.fields(CriticalityChecklist):
        assert field.type == "bool", field
        assert field.default is False


def test_critical_checklist_to_dict_round_trips() -> None:
    for checklist in _all_checklists():
        plain = checklist.to_dict()
        assert set(plain) == set(CHECKLIST_FIELDS)
        assert all(isinstance(value, bool) for value in plain.values())
        assert CriticalityChecklist(**plain) == checklist


# ---------------------------------------------------------------------------
# AC-01: main-figure location alone cannot force CRITICAL
# ---------------------------------------------------------------------------


def test_critical_main_figure_location_alone_never_critical() -> None:
    checklist = CriticalityChecklist(affects_main_figure=True)
    assessment = classify_criticality(checklist)
    assert assessment.criticality is not Criticality.CRITICAL
    # A main-figure finding without conclusion impact is REQUIRED, not
    # CRITICAL, and the deciding rule is the main-figure-only rule.
    assert assessment.criticality == Criticality.REQUIRED
    assert assessment.matched_rule_id == "R-REQ-1"


@pytest.mark.parametrize(
    "checklist",
    [
        CriticalityChecklist(affects_main_figure=True),
        CriticalityChecklist(invalidates_main_result=True),
        CriticalityChecklist(changes_paper_conclusion=True),
        CriticalityChecklist(affects_required_step=True),
        CriticalityChecklist(supporting_detail=True),
        CriticalityChecklist(),
    ],
    ids=[
        "main-figure-only",
        "invalidates-main-result-only",
        "changes-conclusion-only",
        "affects-required-step-only",
        "supporting-detail-only",
        "no-impact",
    ],
)
def test_critical_single_condition_alone_is_never_critical(
    checklist: CriticalityChecklist,
) -> None:
    # AC-01: no single checklist input may by itself force CRITICAL.
    assert classify_criticality(checklist).criticality is not Criticality.CRITICAL


def test_critical_requires_main_figure_and_conclusion_impact() -> None:
    # The bi-implication invariant over the exhaustive grid: CRITICAL holds
    # exactly when a main-figure position is combined with at least one
    # conclusion-impacting input (AC-01).
    for checklist in _all_checklists():
        expected_critical = checklist.affects_main_figure and (
            checklist.invalidates_main_result or checklist.changes_paper_conclusion
        )
        assert (
            classify_criticality(checklist).criticality == Criticality.CRITICAL
        ) is expected_critical


def test_critical_main_figure_plus_conclusion_impact_is_critical() -> None:
    assert (
        classify_criticality(
            CriticalityChecklist(
                affects_main_figure=True, invalidates_main_result=True
            )
        ).criticality
        == Criticality.CRITICAL
    )
    assert (
        classify_criticality(
            CriticalityChecklist(
                affects_main_figure=True, changes_paper_conclusion=True
            )
        ).criticality
        == Criticality.CRITICAL
    )
    # ... while main figure plus a non-conclusion input stays below CRITICAL.
    assert (
        classify_criticality(
            CriticalityChecklist(affects_main_figure=True, affects_required_step=True)
        ).criticality
        is not Criticality.CRITICAL
    )


# ---------------------------------------------------------------------------
# AC-02: same checklist -> deterministic classification
# ---------------------------------------------------------------------------


def test_critical_same_checklist_yields_identical_classification() -> None:
    # Property-style battery over the full 2^5 input grid: evaluating the
    # same checklist twice (indeed repeatedly) produces the exact same
    # assessment -- criticality, matched rule and full decision trace.
    for checklist in _all_checklists():
        first = classify_criticality(checklist)
        second = classify_criticality(checklist)
        assert first == second
        assert first.criticality == second.criticality
        assert first.matched_rule_id == second.matched_rule_id
        assert first.decisions == second.decisions


def test_critical_deterministic_across_repeated_evaluations() -> None:
    sample = _all_checklists()[::4]  # representative subset of the grid
    for checklist in sample:
        reference = classify_criticality(checklist)
        for _ in range(100):
            assert classify_criticality(checklist) == reference


def test_critical_equivalent_dict_inputs_classify_identically() -> None:
    # Reconstructing a checklist from its recorded plain-dict form must
    # produce the identical classification (input equivalence determinism).
    for checklist in _all_checklists():
        rebuilt = CriticalityChecklist(**checklist.to_dict())
        assert classify_criticality(rebuilt) == classify_criticality(checklist)


def test_critical_full_enum_coverage_over_grid() -> None:
    outcomes = {
        classify_criticality(checklist).criticality for checklist in _all_checklists()
    }
    assert outcomes == {
        Criticality.CRITICAL,
        Criticality.REQUIRED,
        Criticality.SUPPORTING,
    }


def test_critical_first_matching_rule_wins() -> None:
    # A checklist that satisfies every rule in the table: the FIRST matching
    # rule (R-CRIT-1) decides, deterministically.
    checklist = CriticalityChecklist(
        affects_main_figure=True,
        invalidates_main_result=True,
        changes_paper_conclusion=True,
        affects_required_step=True,
        supporting_detail=True,
    )
    assessment = classify_criticality(checklist)
    assert assessment.criticality == Criticality.CRITICAL
    assert assessment.matched_rule_id == "R-CRIT-1"


# ---------------------------------------------------------------------------
# AC-03: classification inputs remain auditable
# ---------------------------------------------------------------------------


def test_critical_assessment_records_exact_checklist_inputs() -> None:
    # The assessment must carry back the exact checklist that produced it.
    for checklist in _all_checklists():
        assessment = classify_criticality(checklist)
        assert assessment.checklist == checklist
        assert assessment.checklist.to_dict() == checklist.to_dict()


def test_critical_assessment_records_every_rule_decision() -> None:
    checklist = CriticalityChecklist(
        affects_main_figure=True, changes_paper_conclusion=True
    )
    assessment = classify_criticality(checklist)
    recorded_ids = [decision.rule_id for decision in assessment.decisions]
    assert recorded_ids == [rule.rule_id for rule in CRITICALITY_RULES]
    for decision, rule in zip(assessment.decisions, CRITICALITY_RULES, strict=True):
        assert decision.rule_id == rule.rule_id
        assert decision.description == rule.description
        assert decision.criticality == rule.criticality


def test_critical_matched_rule_is_the_first_true_predicate() -> None:
    # The audit trail is self-consistent for every checklist in the grid:
    # the matched rule is the first rule whose predicate holds, and its
    # proposed criticality is exactly the classification returned.
    for checklist in _all_checklists():
        assessment = classify_criticality(checklist)
        matched = next(decision for decision in assessment.decisions if decision.matched)
        assert matched.rule_id == assessment.matched_rule_id
        for earlier in assessment.decisions[: assessment.decisions.index(matched)]:
            assert earlier.matched is False
        rule = next(r for r in CRITICALITY_RULES if r.rule_id == matched.rule_id)
        assert rule.predicate(checklist) is True
        assert rule.criticality == assessment.criticality
