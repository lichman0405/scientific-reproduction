"""Tests for the Source x Claim assessment hook and hard gates (DEV-M2-G03,
AC-02 and AC-03).

Covered behaviors:
  * AC-02 -- ``assess`` takes a source and a claim together and produces the
    structured Authority/Reliability/Directness triple as the frozen
    ``EvidenceAssessment`` model, whose serialized form satisfies
    ``schemas/evidence.schema.yaml``; the same source against different
    claims and different sources against the same claim produce distinct
    (deterministic) assessments;
  * authority is rubric-derived from the source type with explicit override
    for the source types the rubric leaves to judgment (review,
    database_record, other); directness is a per-(source, claim) 0-4 input;
  * AC-03 -- hard-gate predicates (reliability gate, directness gate,
    recovery-hypothesis eligibility, acceptance support, independent-source
    counting) are standalone functions that read only the raw axes and
    never the weighted display score: they accept assessments with any
    ranking score (including none) and their results are invariant under
    ranking_score changes;
  * malformed assessment inputs (missing axis, wrong types) are rejected
    loudly.
"""

from __future__ import annotations

import pytest

from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    EvidenceAssessment,
    ResearchSource,
    SourceType,
)
from scientific_reproduction.core.rules.evidence import (
    AUTHORITY_BY_SOURCE_TYPE,
    DEFAULT_RANKING_WEIGHTS,
    EvidenceRulesError,
    ReliabilityChecklist,
    acceptance_support_qualifies,
    assess,
    authority_grade,
    count_independent_qualifying_sources,
    directness_gate_passes,
    ranking_score,
    recovery_hypothesis_eligible,
    reliability_gate_passes,
    reliability_score,
)
from scientific_reproduction.core.schema_validation import validate_object


def _checklist() -> ReliabilityChecklist:
    return ReliabilityChecklist(
        checklist_ref="RCHK-TEST-001",
        raw_data_available=True,
        method_complete=True,
        independent_replication_performed=True,
        uncertainty_reported=True,
        independent_external_validation=False,
        data_internally_consistent=True,
        conclusion_supported_by_data=True,
        material_identity_controlled=True,
        known_retraction_correction_defect=False,
    )


def _source(source_type: SourceType, source_id: str = "SRC-1") -> ResearchSource:
    return ResearchSource(
        source_id=source_id,
        source_type=source_type,
        title="A test source",
        provenance="test fixture",
    )


def _assessment(**axes: int) -> EvidenceAssessment:
    """A bare assessment with the given axes and an arbitrary ranking score."""
    return EvidenceAssessment(
        authority=axes.get("authority", 3),
        reliability=axes.get("reliability", 3),
        directness=axes.get("directness", 2),
        reliability_checklist_ref="RCHK-TEST-001",
        ranking_score=axes.get("ranking_score", 50.0),
    )


# -- AC-02: Source x Claim assessment hook ------------------------------------


def test_assess_produces_the_structured_axis_triple() -> None:
    result = assess(
        source=_source(SourceType.PEER_REVIEWED_PAPER),
        claim_id="CLAIM-ADS-001",
        checklist=_checklist(),
        directness=3,
    )
    assert isinstance(result, EvidenceAssessment)
    assert result.authority == 3  # rubric: strong peer-reviewed scholarly source
    assert result.reliability == reliability_score(_checklist())  # checklist-derived
    assert result.directness == 3
    assert result.reliability_checklist_ref == "RCHK-TEST-001"
    assert result.ranking_score == ranking_score(3, result.reliability, 3)


def test_assess_accepts_a_bare_source_type() -> None:
    result = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="CLAIM-1",
        checklist=_checklist(),
        directness=4,
    )
    assert result.authority == 4
    assert result.directness == 4


def test_assessment_serializes_to_a_schema_valid_evidence_document() -> None:
    result = assess(
        source=_source(SourceType.TARGET_PAPER),
        claim_id="CLAIM-ADS-001",
        checklist=_checklist(),
        directness=4,
    )
    # The scoring API emits the exact assessment shape the frozen evidence
    # schema requires (authority/reliability/directness/checklist ref), so a
    # full evidence document built from it validates cleanly.
    document = {
        "evidence_id": "EVID-1",
        "source_id": "SRC-1",
        "claim_id": "CLAIM-ADS-001",
        "finding": "the source reports the claimed uptake",
        "assessment": result.to_dict(),
    }
    assert validate_object("evidence", document) == []


def test_assess_is_source_x_claim_not_a_global_score() -> None:
    checklist = _checklist()
    # The same source assessed against different claims yields different
    # assessments (directness is claim-specific).
    for_claim_a = assess(
        source=_source(SourceType.PEER_REVIEWED_PAPER),
        claim_id="CLAIM-A",
        checklist=checklist,
        directness=4,
    )
    for_claim_b = assess(
        source=_source(SourceType.PEER_REVIEWED_PAPER),
        claim_id="CLAIM-B",
        checklist=checklist,
        directness=1,
    )
    assert for_claim_a.directness == 4
    assert for_claim_b.directness == 1
    assert for_claim_a != for_claim_b
    # ... and different sources against the same claim are distinct too.
    from_informal = assess(
        source=_source(SourceType.INFORMAL),
        claim_id="CLAIM-A",
        checklist=checklist,
        directness=4,
    )
    assert from_informal.authority == 1
    assert from_informal != for_claim_a


def test_assess_is_deterministic() -> None:
    args = dict(
        source=_source(SourceType.PEER_REVIEWED_PAPER),
        claim_id="CLAIM-1",
        checklist=_checklist(),
        directness=3,
    )
    assert assess(**args) == assess(**args)


def test_assess_uses_checklist_object_reference_by_default() -> None:
    result = assess(
        source=SourceType.DATASET,
        claim_id="CLAIM-1",
        checklist=_checklist(),
        directness=3,
    )
    assert result.reliability_checklist_ref == "RCHK-TEST-001"


# -- authority rubric ---------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        (SourceType.TARGET_PAPER, 4),
        (SourceType.SUPPLEMENTARY_INFORMATION, 4),
        (SourceType.DATASET, 4),
        (SourceType.STRUCTURE_DEPOSITION, 4),
        (SourceType.STANDARD, 4),
        (SourceType.OFFICIAL_DOCUMENTATION, 4),
        (SourceType.PEER_REVIEWED_PAPER, 3),
        (SourceType.THESIS, 3),
        (SourceType.PREPRINT, 2),
        (SourceType.VENDOR_NOTE, 2),
        (SourceType.INFORMAL, 1),
    ],
)
def test_authority_rubric_maps_fixed_source_types(
    source_type: SourceType, expected: int
) -> None:
    assert authority_grade(source_type) == expected
    result = assess(
        source=_source(source_type),
        claim_id="CLAIM-1",
        checklist=_checklist(),
        directness=2,
    )
    assert result.authority == expected


def test_authority_rubric_leaves_judgment_types_open() -> None:
    for source_type in (SourceType.REVIEW, SourceType.DATABASE_RECORD, SourceType.OTHER):
        assert AUTHORITY_BY_SOURCE_TYPE[source_type] is None
        assert authority_grade(source_type) is None
        with pytest.raises(ValueError):
            assess(
                source=_source(source_type),
                claim_id="CLAIM-1",
                checklist=_checklist(),
                directness=2,
            )


def test_explicit_authority_grade_is_accepted_and_validated() -> None:
    result = assess(
        source=SourceType.DATABASE_RECORD,
        claim_id="CLAIM-1",
        checklist=_checklist(),
        directness=2,
        authority=4,  # an authoritative official database
    )
    assert result.authority == 4
    # An explicit grade outside the 0-4 rubric is rejected.
    with pytest.raises(EvidenceRulesError):
        assess(
            source=SourceType.PEER_REVIEWED_PAPER,
            claim_id="CLAIM-1",
            checklist=_checklist(),
            directness=2,
            authority=5,
        )


def test_directness_must_be_a_0_4_rubric_level() -> None:
    for bad in (5, -1, 1.5, True):
        with pytest.raises(EvidenceRulesError):
            assess(
                source=SourceType.TARGET_PAPER,
                claim_id="CLAIM-1",
                checklist=_checklist(),
                directness=bad,  # type: ignore[arg-type]
            )


def test_assess_type_checks_its_arguments() -> None:
    with pytest.raises(TypeError):
        assess(  # type: ignore[call-arg]
            source="not-a-source",  # type: ignore[arg-type]
            claim_id="CLAIM-1",
            checklist=_checklist(),
            directness=2,
        )
    with pytest.raises(TypeError):
        assess(  # type: ignore[call-arg]
            source=SourceType.TARGET_PAPER,
            claim_id=123,  # type: ignore[arg-type]
            checklist=_checklist(),
            directness=2,
        )


# -- AC-03: hard-gate predicates are independent of the display score --------


def test_gates_accept_assessments_without_any_ranking_score() -> None:
    # An assessment dict straight from a stored record (no ranking_score key
    # at all) is gated on its raw axes.
    bare = {
        "authority": 4,
        "reliability": 3,
        "directness": 2,
        "reliability_checklist_ref": "RCHK-TEST-001",
    }
    assert reliability_gate_passes(bare)
    assert directness_gate_passes(bare)
    assert recovery_hypothesis_eligible(bare)


def test_gate_results_are_invariant_under_ranking_score_changes() -> None:
    # The weighted display score must never influence a hard gate (SS3/AC-03):
    # assessments with identical axes but different ranking scores (or none)
    # produce identical gate outcomes.
    for ranking in (None, 0.0, 49.0, 100.0):
        assessment = EvidenceAssessment(
            authority=4,
            reliability=3,
            directness=2,
            reliability_checklist_ref="RCHK-TEST-001",
            ranking_score=ranking,
        )
        assert reliability_gate_passes(assessment)
        assert directness_gate_passes(assessment)
        assert recovery_hypothesis_eligible(assessment)
    # The same holds for the dict form with no ranking key.
    dict_form = {
        "authority": 4,
        "reliability": 3,
        "directness": 2,
        "reliability_checklist_ref": "RCHK-TEST-001",
    }
    assert recovery_hypothesis_eligible(dict_form)
    assert acceptance_support_qualifies(dict_form)


def test_gate_predicates_do_not_take_ranking_weights() -> None:
    # The gate signatures have no score/weight parameters at all.
    with pytest.raises(TypeError):
        reliability_gate_passes(_assessment(), weights=DEFAULT_RANKING_WEIGHTS)  # type: ignore[call-arg]


def test_reliability_gate_thresholds() -> None:
    assert reliability_gate_passes(_assessment(reliability=3))
    assert not reliability_gate_passes(_assessment(reliability=2))
    assert reliability_gate_passes(_assessment(reliability=2), minimum=2)
    assert not reliability_gate_passes(_assessment(reliability=2), minimum=4)
    with pytest.raises(TypeError):
        reliability_gate_passes(_assessment(), minimum=1.0)  # type: ignore[arg-type]


def test_directness_gate_thresholds() -> None:
    assert directness_gate_passes(_assessment(directness=2))
    assert not directness_gate_passes(_assessment(directness=1))
    assert directness_gate_passes(_assessment(directness=1), minimum=1)


def test_recovery_hypothesis_eligibility_v01_default() -> None:
    # v0.1 default (06-EVIDENCE-SYSTEM.md SS4): R >= 3 and D >= 2 and
    # scientifically_actionable.
    assert recovery_hypothesis_eligible(_assessment(reliability=3, directness=2))
    assert not recovery_hypothesis_eligible(_assessment(reliability=2, directness=2))
    assert not recovery_hypothesis_eligible(_assessment(reliability=3, directness=1))
    assert not recovery_hypothesis_eligible(
        _assessment(reliability=3, directness=2),
        scientifically_actionable=False,
    )
    with pytest.raises(TypeError):
        recovery_hypothesis_eligible(
            _assessment(), scientifically_actionable="yes"  # type: ignore[arg-type]
        )


def test_acceptance_support_gate_with_documented_exceptions() -> None:
    # SS4: acceptance-criterion changes typically require R >= 3; the
    # preference for two independent sources is an aggregate concern and the
    # exception clause covers authoritative standards and the target paper
    # itself defining the claimed parameter.
    assert acceptance_support_qualifies(_assessment(reliability=3))
    assert not acceptance_support_qualifies(_assessment(reliability=2))
    assert acceptance_support_qualifies(
        _assessment(reliability=2), authoritative_standard=True
    )
    assert acceptance_support_qualifies(
        _assessment(reliability=1), target_paper_defines=True
    )
    with pytest.raises(TypeError):
        acceptance_support_qualifies(
            _assessment(), authoritative_standard="no"  # type: ignore[arg-type]
        )


def test_independent_qualifying_source_counting() -> None:
    qualifying = _assessment(reliability=3, directness=2)
    failing = _assessment(reliability=2, directness=2)
    evidence_items = [
        ClaimSpecificEvidence(
            evidence_id="E-1",
            source_id="SRC-A",
            claim_id="C-1",
            finding="f",
            assessment=qualifying,
        ),
        ClaimSpecificEvidence(
            evidence_id="E-2",
            source_id="SRC-A",  # same source: mirrors/items count once
            claim_id="C-2",
            finding="f",
            assessment=qualifying,
        ),
        ClaimSpecificEvidence(
            evidence_id="E-3",
            source_id="SRC-B",
            claim_id="C-1",
            finding="f",
            assessment=qualifying,
        ),
        ClaimSpecificEvidence(
            evidence_id="E-4",
            source_id="SRC-C",
            claim_id="C-3",
            finding="f",
            assessment=failing,
        ),
    ]
    assert count_independent_qualifying_sources(evidence_items) == 2
    assert (
        count_independent_qualifying_sources(evidence_items, minimum_reliability=2)
        == 3
    )
    # The mapping (schema-shaped record) form behaves identically.
    as_mappings = [
        {
            "source_id": item.source_id,
            "assessment": item.assessment.to_dict(),
        }
        for item in evidence_items
    ]
    assert count_independent_qualifying_sources(as_mappings) == 2


def test_independent_source_counting_rejects_malformed_records() -> None:
    with pytest.raises(EvidenceRulesError):
        count_independent_qualifying_sources([{"assessment": {"reliability": 3}}])
    with pytest.raises(EvidenceRulesError):
        count_independent_qualifying_sources(
            [{"source_id": "SRC-A", "assessment": "oops"}]
        )
    with pytest.raises(TypeError):
        count_independent_qualifying_sources(["SRC-A"])  # type: ignore[list-item]


def test_gates_reject_malformed_assessment_mappings() -> None:
    with pytest.raises(EvidenceRulesError):
        reliability_gate_passes({"authority": 4, "directness": 2})
    with pytest.raises(EvidenceRulesError):
        directness_gate_passes({"directness": "high"})
    with pytest.raises(TypeError):
        reliability_gate_passes("reliability=3")  # type: ignore[arg-type]
