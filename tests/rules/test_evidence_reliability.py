"""Tests for the checklist-derived Reliability scoring rule (DEV-M2-G03, AC-01).

Covered behaviors:
  * AC-01 -- Reliability is produced only from checklist inputs: the scoring
    API has no parameter that accepts a directly-assigned reliability score
    (calling it with a raw integer raises TypeError), ``assess`` has no
    ``reliability`` parameter, a ``ReliabilityChecklist`` cannot exist
    without a non-empty checklist reference, a raw answer mapping is
    rejected by ``assess`` when no checklist reference is supplied, and
    ``validate_assessment_against_checklist`` verifies that a stored
    assessment's reliability equals the checklist-derived score;
  * the versioned rule maps the nine frozen dimensions deterministically:
    the negative signal (known retraction/correction/methodological defect)
    scores 0, the eight positive dimensions are banded 8->4, 6-7->3, 4-5->2,
    2-3->1, 0-1->0;
  * strict canonical-input handling: missing dimensions, unknown keys,
    non-boolean answers and missing/empty references are rejected loudly;
  * ``ReliabilityChecklist``/dict equivalence and to_dict/from_dict
    round-trips are stable;
  * ``ranking_score`` is a separate, versioned, display-only composite whose
    weights are validated (negative weights or a sum != 1 are rejected).
"""

from __future__ import annotations

import dataclasses

import pytest

from scientific_reproduction.core.models import SourceType
from scientific_reproduction.core.rules.evidence import (
    DEFAULT_RANKING_WEIGHTS,
    NEGATIVE_DIMENSION_KEY,
    RANKING_RULE_VERSION,
    RELIABILITY_CHECKLIST_DIMENSIONS,
    RELIABILITY_RULE_VERSION,
    EvidenceRulesError,
    RankingWeights,
    ReliabilityChecklist,
    ReliabilityChecklistError,
    assess,
    ranking_score,
    reliability_score,
    validate_assessment_against_checklist,
)


def _checklist(**answers: bool) -> ReliabilityChecklist:
    """A canonical checklist with all positive dimensions set (unless overridden)."""
    base: dict[str, bool] = {
        "raw_data_available": True,
        "method_complete": True,
        "independent_replication_performed": True,
        "uncertainty_reported": True,
        "independent_external_validation": True,
        "data_internally_consistent": True,
        "conclusion_supported_by_data": True,
        "material_identity_controlled": True,
        "known_retraction_correction_defect": False,
    }
    base.update(answers)
    return ReliabilityChecklist(checklist_ref="RCHK-TEST-001", **base)


# -- the frozen dimension set (normative: 06-EVIDENCE-SYSTEM.md SS2) ---------


def test_dimensions_cover_the_nine_spec_dimensions() -> None:
    keys = [key for key, _ in RELIABILITY_CHECKLIST_DIMENSIONS]
    # The spec's "should include at least" set, in the spec's order, plus the
    # negative signal last.
    assert keys == [
        "raw_data_available",
        "method_complete",
        "independent_replication_performed",
        "uncertainty_reported",
        "independent_external_validation",
        "data_internally_consistent",
        "conclusion_supported_by_data",
        "material_identity_controlled",
        "known_retraction_correction_defect",
    ]
    assert NEGATIVE_DIMENSION_KEY == "known_retraction_correction_defect"


def test_rule_versions_are_explicit() -> None:
    assert RELIABILITY_RULE_VERSION == "reliability-rule-v1"
    assert RANKING_RULE_VERSION == "ranking-rule-v1"


# -- AC-01: no path produces a reliability score without checklist inputs ----


def test_reliability_score_has_no_direct_assignment_parameter() -> None:
    # The scoring API accepts only checklist inputs; there is no parameter
    # for a directly-assigned reliability score.
    with pytest.raises(TypeError):
        reliability_score(4)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        reliability_score(1.5)  # type: ignore[arg-type]


def test_assess_has_no_reliability_parameter() -> None:
    # The Source x Claim assessment hook derives reliability from the
    # checklist; passing a reliability value directly is rejected by the
    # signature itself.
    checklist = _checklist()
    with pytest.raises(TypeError):
        assess(  # type: ignore[call-arg]
            source=SourceType.TARGET_PAPER,
            claim_id="CLAIM-1",
            checklist=checklist,
            directness=3,
            reliability=4,
        )


def test_checklist_cannot_exist_without_a_reference() -> None:
    answers = {
        "raw_data_available": True,
        "method_complete": True,
        "independent_replication_performed": True,
        "uncertainty_reported": True,
        "independent_external_validation": True,
        "data_internally_consistent": True,
        "conclusion_supported_by_data": True,
        "material_identity_controlled": True,
        "known_retraction_correction_defect": False,
    }
    with pytest.raises(ReliabilityChecklistError):
        ReliabilityChecklist(checklist_ref="", **answers)
    with pytest.raises(ReliabilityChecklistError):
        ReliabilityChecklist(checklist_ref="  ", **answers)


def test_from_dict_requires_a_checklist_reference() -> None:
    data = _checklist().to_dict()
    del data["checklist_ref"]
    with pytest.raises(ReliabilityChecklistError):
        ReliabilityChecklist.from_dict(data)


def test_assess_rejects_answer_mapping_without_reference() -> None:
    # A raw mapping of answers carries no reference: without an explicit
    # checklist_ref the assessment cannot be produced (AC-01).
    answers = _checklist().as_mapping()
    with pytest.raises(ReliabilityChecklistError):
        assess(
            source=SourceType.TARGET_PAPER,
            claim_id="CLAIM-1",
            checklist=answers,
            directness=3,
        )


def test_assess_accepts_answer_mapping_with_explicit_reference() -> None:
    answers = _checklist().as_mapping()
    result = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="CLAIM-1",
        checklist=answers,
        directness=3,
        checklist_ref="RCHK-TEST-001",
    )
    assert result.reliability_checklist_ref == "RCHK-TEST-001"
    assert result.reliability == reliability_score(_checklist())


def test_validate_assessment_against_checklist_links_score_to_inputs() -> None:
    # A checklist scoring 3 (one positive dimension unsatisfied), so a
    # tampered reliability of 4 is detectable.
    checklist = _checklist(independent_external_validation=False)
    assert reliability_score(checklist) == 3
    assessment = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="CLAIM-1",
        checklist=checklist,
        directness=3,
    )
    # A valid assessment is one whose reliability equals the checklist-derived
    # score and whose reference matches the checklist record (AC-01 audit).
    assert validate_assessment_against_checklist(assessment, checklist)

    tampered = dataclasses.replace(assessment, reliability=4)
    assert not validate_assessment_against_checklist(tampered, checklist)

    wrong_ref = dataclasses.replace(assessment, reliability_checklist_ref="OTHER")
    assert not validate_assessment_against_checklist(wrong_ref, checklist)


def test_validate_assessment_against_checklist_requires_an_assessment_ref() -> None:
    checklist = _checklist()
    assessment = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="CLAIM-1",
        checklist=checklist,
        directness=3,
    )
    no_ref = dataclasses.replace(assessment, reliability_checklist_ref="")
    with pytest.raises(ReliabilityChecklistError):
        validate_assessment_against_checklist(no_ref, checklist)
    # A raw answer mapping without a reference cannot be validated either.
    with pytest.raises(ReliabilityChecklistError):
        validate_assessment_against_checklist(assessment, checklist.as_mapping())


# -- the versioned rule mapping ----------------------------------------------


@pytest.mark.parametrize(
    ("satisfied", "expected"),
    [
        (8, 4),
        (7, 3),
        (6, 3),
        (5, 2),
        (4, 2),
        (3, 1),
        (2, 1),
        (1, 0),
        (0, 0),
    ],
)
def test_rule_bands_on_satisfied_positive_dimensions(
    satisfied: int, expected: int
) -> None:
    checklist = _checklist()
    positive = [
        key
        for key, _ in RELIABILITY_CHECKLIST_DIMENSIONS
        if key != NEGATIVE_DIMENSION_KEY
    ]
    for key in positive[satisfied:]:
        checklist = dataclasses.replace(checklist, **{key: False})
    assert reliability_score(checklist) == expected


def test_negative_signal_disqualifies_any_other_answer() -> None:
    checklist = _checklist()
    assert reliability_score(checklist) == 4
    with_defect = dataclasses.replace(
        checklist, known_retraction_correction_defect=True
    )
    assert reliability_score(with_defect) == 0


def test_score_is_deterministic_across_repeated_calls() -> None:
    checklist = _checklist(independent_replication_performed=False)
    assert reliability_score(checklist) == reliability_score(checklist)
    assert reliability_score(checklist) == reliability_score(
        ReliabilityChecklist.from_dict(checklist.to_dict())
    )


def test_every_positive_dimension_contributes_individually() -> None:
    # Flipping any single positive dimension from satisfied to unsatisfied
    # changes the score on the 8 -> 4 boundary, proving each dimension
    # participates in the rule.
    checklist = _checklist()
    positive = [
        key
        for key, _ in RELIABILITY_CHECKLIST_DIMENSIONS
        if key != NEGATIVE_DIMENSION_KEY
    ]
    for key in positive:
        flipped = dataclasses.replace(checklist, **{key: False})
        assert reliability_score(flipped) == 3, f"{key} did not participate"


# -- strict canonical inputs --------------------------------------------------


def test_missing_dimension_is_rejected() -> None:
    data = _checklist().to_dict()
    del data["uncertainty_reported"]
    with pytest.raises(ReliabilityChecklistError):
        reliability_score(data)
    with pytest.raises(ReliabilityChecklistError):
        ReliabilityChecklist.from_dict(data)


def test_unknown_dimension_key_is_rejected() -> None:
    data = _checklist().to_dict()
    data["raw_data_available_typo"] = True
    with pytest.raises(ReliabilityChecklistError):
        reliability_score(data)
    with pytest.raises(ReliabilityChecklistError):
        ReliabilityChecklist.from_dict(data)


def test_non_boolean_answer_is_rejected() -> None:
    data = _checklist().to_dict()
    data["raw_data_available"] = "yes"
    with pytest.raises(ReliabilityChecklistError):
        reliability_score(data)
    with pytest.raises(ReliabilityChecklistError):
        ReliabilityChecklist.from_dict(data)


def test_mapping_with_checklist_ref_key_is_tolerated() -> None:
    # ReliabilityChecklist.to_dict() output (answers + ref) is usable
    # directly by the scoring function.
    checklist = _checklist()
    assert reliability_score(checklist.to_dict()) == reliability_score(checklist)


def test_from_dict_round_trips() -> None:
    checklist = _checklist(independent_replication_performed=False)
    assert ReliabilityChecklist.from_dict(checklist.to_dict()) == checklist


# -- ranking_score: display-only composite (SS3) ------------------------------


def test_ranking_score_default_weights_match_spec() -> None:
    # SS3 default: (0.25*A + 0.45*R + 0.30*D) / 4 * 100
    assert DEFAULT_RANKING_WEIGHTS == RankingWeights(0.25, 0.45, 0.30)
    assert ranking_score(4, 4, 4) == 100.0
    assert ranking_score(0, 0, 0) == 0.0
    assert ranking_score(4, 2, 4) == pytest.approx(
        (0.25 * 4 + 0.45 * 2 + 0.30 * 4) / 4 * 100
    )


def test_ranking_score_weights_are_versioned_and_validated() -> None:
    assert RANKING_RULE_VERSION == "ranking-rule-v1"
    # Custom normalized weights are honored (weights are configurable, SS3).
    custom = RankingWeights(0.5, 0.0, 0.5)
    assert ranking_score(4, 2, 4, weights=custom) == pytest.approx(
        (0.5 * 4 + 0.0 * 2 + 0.5 * 4) / 4 * 100
    )
    # Non-normalized, negative and non-numeric weights are rejected.
    with pytest.raises(EvidenceRulesError):
        ranking_score(4, 4, 4, weights=RankingWeights(0.25, 0.45, 0.31))
    with pytest.raises(EvidenceRulesError):
        ranking_score(4, 4, 4, weights=RankingWeights(-0.5, 1.0, 0.5))
    with pytest.raises(EvidenceRulesError):
        ranking_score(4, 4, 4, weights=RankingWeights("x", 0.45, 0.3))  # type: ignore[arg-type]


def test_ranking_score_rejects_out_of_range_axes() -> None:
    with pytest.raises(EvidenceRulesError):
        ranking_score(5, 4, 4)
    with pytest.raises(EvidenceRulesError):
        ranking_score(4, -1, 4)
    with pytest.raises(EvidenceRulesError):
        ranking_score(4, 4, 9)
