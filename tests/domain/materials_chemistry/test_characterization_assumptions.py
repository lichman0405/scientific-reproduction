"""DEV-M11-G02: missing metadata and pending measurements enter the
Assumption Registry.

AC-01/AC-02: missing required raw data / instrument metadata of a
characterization template -- and the pending measurement facts of an
acceptance/identity-check evaluation -- are routed through the EXISTING
Assumption Registry pathway: real ``core.models.Assumption`` records
decided and read back through the real
``core.rules.assumptions.assumption_effect`` / ``evaluate_strict_label``
APIs (never a parallel store), with the assumption refs carried on the
template. A2 defaults follow 16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5.

Every test name contains "character" (DEV-M11-G02 naming rule).
"""

from __future__ import annotations

from typing import Any

import pytest

from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
)
from scientific_reproduction.core.rules.assumptions import (
    StrictLabel,
    evaluate_strict_label,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    AnalysisPlan,
    MissingMeasurementRouting,
    MissingMetadataRouting,
    PXRDCharacterizationTemplate,
    apply_assumption_routing,
    assumptions_for_missing_measurements,
    assumptions_for_missing_metadata,
    capture_characterization,
    missing_metadata,
)


@pytest.fixture
def incomplete_pxrd_character_template() -> PXRDCharacterizationTemplate:
    """A PXRD template missing its scan range and step size."""
    return PXRDCharacterizationTemplate(
        template_id="pxrd-2-incomplete",
        title="Incomplete PXRD capture",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "scan_temperature_K": 298.0,
        },
    )


def test_character_missing_metadata_are_detected(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """Required but unrecorded metadata is the routing input (AC-01)."""
    assert missing_metadata(incomplete_pxrd_character_template) == (
        "two_theta_min_deg",
        "two_theta_max_deg",
        "step_size_deg",
    )


def test_character_missing_metadata_route_to_real_assumption_records(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-02: every missing metadata parameter becomes a real Assumption."""
    routing = assumptions_for_missing_metadata(incomplete_pxrd_character_template)
    assert isinstance(routing, MissingMetadataRouting)
    assert routing.missing_parameters == (
        "two_theta_min_deg",
        "two_theta_max_deg",
        "step_size_deg",
    )
    assert len(routing.assumptions) == 3
    for entry in routing.assumptions:
        assert isinstance(entry, Assumption)
        assert entry.parameter in routing.missing_parameters
        assert is_valid_id(entry.assumption_id, "assumption"), entry.assumption_id
        # The default classification for missing scientific metadata is A2
        # (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5).
        assert (
            entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        )
        assert entry.rationale
        assert entry.source_refs == []
    assert routing.assumption_refs == tuple(
        entry.assumption_id for entry in routing.assumptions
    )


def test_character_routing_reads_back_through_real_assumption_api(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """The strict label is decided by the real core assumption evaluator."""
    routing = assumptions_for_missing_metadata(incomplete_pxrd_character_template)
    label_assessment = evaluate_strict_label(routing.assumptions)
    assert routing.strict_label_assessment.label == label_assessment.label
    assert routing.strict_label_assessment.matched_label_rule_id == (
        label_assessment.matched_label_rule_id
    )
    # An A2 dominates: NOT_STRICT via R-STRICT-2 (08-STRICT-RECOVERY-CLOSURE SS3).
    assert routing.strict_label_assessment.label is StrictLabel.NOT_STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-2"
    for effect in routing.effects:
        assert effect.effect.value == "DISQUALIFIES_PURE_STRICT"
        assert effect.rule_id == "R-EFF-1"


def test_character_assumption_records_are_schema_conformant(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """Routed assumptions persist through the real schema persistence gate."""
    routing = assumptions_for_missing_metadata(incomplete_pxrd_character_template)
    for entry in routing.assumptions:
        assert validate_and_reject("assumption", entry.to_dict()) is None


def test_character_routing_with_a1_classifies_strict_with_assumptions() -> None:
    """A1 with reliable method evidence -> STRICT_WITH_ASSUMPTIONS (R-STRICT-3)."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-3",
        title="PXRD missing step size",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 50.0,
            "scan_temperature_K": 298.0,
        },
    )
    routing = assumptions_for_missing_metadata(
        template,
        classification=AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
        rationale="0.02 deg is the instrument's default step size",
        source_refs=["instrument-calibration-2024"],
    )
    assert routing.missing_parameters == ("step_size_deg",)
    entry = routing.assumptions[0]
    assert entry.classification is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
    assert entry.source_refs == ["instrument-calibration-2024"]
    assert entry.strict_status_effect is not None
    assert entry.strict_status_effect.value == "STRICT_WITH_ASSUMPTIONS"
    assert routing.strict_label_assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-3"


def test_character_routing_with_a0_keeps_pure_strict() -> None:
    """A0 technical defaults keep the pure strict label (R-STRICT-4)."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-4",
        title="PXRD missing scan temperature",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 50.0,
            "step_size_deg": 0.02,
        },
    )
    routing = assumptions_for_missing_metadata(
        template,
        classification=AssumptionClassification.A0_TECHNICAL_DEFAULT,
    )
    assert routing.missing_parameters == ("scan_temperature_K",)
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-4"
    assert routing.effects[0].effect.value == "NONE"


def test_character_routing_applies_refs_to_template(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """apply_assumption_routing carries the safe refs on the template (AC-02)."""
    routing = assumptions_for_missing_metadata(incomplete_pxrd_character_template)
    routed = apply_assumption_routing(incomplete_pxrd_character_template, routing)
    assert routed.assumption_refs == routing.assumption_refs
    # The input template is untouched: nothing is mutated, nothing frozen.
    assert incomplete_pxrd_character_template.assumption_refs == ()
    assert routed.frozen is False
    # The refs survive protocol capture.
    assert capture_characterization(routed)["assumption_refs"] == list(
        routing.assumption_refs
    )


def test_character_missing_measurements_route_to_real_assumptions() -> None:
    """AC-02: pending acceptance measurements become real Assumption records."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-5",
        title="PXRD missing facts",
        analysis=AnalysisPlan(
            protocol="phase identification",
            protocol_steps=("match reference phases",),
            acceptance_parameters={"pxrd_phase_score_min": 0.8},
        ),
    )
    routing = assumptions_for_missing_measurements(
        template, {"reference_phase_score": 0.95}
    )
    assert isinstance(routing, MissingMeasurementRouting)
    # The stable sorted order the acceptance assessment records: the
    # routing is byte-identical to the evaluation's pending list.
    assert routing.missing_measurements == (
        "batch_consistency_score",
        "intensity_pattern_score",
        "max_peak_position_deviation_deg",
    )
    for entry in routing.assumptions:
        assert isinstance(entry, Assumption)
        assert entry.parameter in routing.missing_measurements
    assert routing.assumption_refs == tuple(
        entry.assumption_id for entry in routing.assumptions
    )
    # The routed refs can be applied to the template like metadata refs.
    routed = apply_assumption_routing(template, routing)
    assert routed.assumption_refs == routing.assumption_refs


def test_character_routing_is_deterministic(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """Same template -> identical assumption ids and refs on every call."""
    first = assumptions_for_missing_metadata(incomplete_pxrd_character_template)
    second = assumptions_for_missing_metadata(incomplete_pxrd_character_template)
    assert first.assumption_refs == second.assumption_refs
    assert first.assumptions[0].assumption_id == second.assumptions[0].assumption_id


def test_character_routing_is_empty_for_complete_templates() -> None:
    """A complete template routes nothing: empty assumptions, pure strict."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-complete",
        title="Complete PXRD capture",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 50.0,
            "step_size_deg": 0.02,
            "scan_temperature_K": 298.0,
        },
    )
    routing = assumptions_for_missing_metadata(template)
    assert routing.missing_parameters == ()
    assert routing.assumptions == ()
    assert routing.assumption_refs == ()
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-1"


def test_character_routing_accepts_explicit_classification_and_goal_ids() -> None:
    """Explicit classification, rationale and affected goal ids are recorded."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-6",
        title="PXRD missing scan temperature",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 50.0,
            "step_size_deg": 0.02,
        },
    )
    routing = assumptions_for_missing_metadata(
        template,
        classification=AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        rationale="scan temperature not disclosed in the published protocol",
        source_refs=["doi-ref"],
        affected_goal_ids=["goal-1"],
    )
    entry = routing.assumptions[0]
    assert entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
    assert (
        entry.rationale
        == "scan temperature not disclosed in the published protocol"
    )
    assert entry.source_refs == ["doi-ref"]
    assert entry.affected_goal_ids == ["goal-1"]


def test_character_routing_type_boundaries_raise_type_error(
    incomplete_pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """Non-classification, malformed refs and bad measurements are TypeError."""
    bad_classification: Any = "A2"
    bad_refs: Any = "not-a-sequence"
    bad_goal_ids: Any = [1]
    bad_measurements: Any = ["reference_phase_score"]
    with pytest.raises(TypeError):
        assumptions_for_missing_metadata(
            incomplete_pxrd_character_template,
            classification=bad_classification,
        )
    with pytest.raises(TypeError):
        assumptions_for_missing_metadata(
            incomplete_pxrd_character_template,
            source_refs=bad_refs,
        )
    with pytest.raises(TypeError):
        assumptions_for_missing_metadata(
            incomplete_pxrd_character_template,
            affected_goal_ids=bad_goal_ids,
        )
    with pytest.raises(TypeError):
        assumptions_for_missing_measurements(
            incomplete_pxrd_character_template, bad_measurements
        )
    bad_routing: Any = "nope"
    with pytest.raises(TypeError):
        apply_assumption_routing(incomplete_pxrd_character_template, bad_routing)
