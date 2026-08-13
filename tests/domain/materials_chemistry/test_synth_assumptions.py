"""DEV-M11-G01: missing scientific parameters enter the Assumption Registry.

AC-02: missing required scientific parameters of a synthesis template are
routed through the EXISTING Assumption Registry pathway -- real
``core.models.Assumption`` records decided and read back through the real
``core.rules.assumptions.assumption_effect`` / ``evaluate_strict_label``
APIs (never a parallel store), with the assumption refs carried on the
template. A2 defaults follow 16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5.

Every test name contains "synth" (DEV-M11-G01 naming rule).
"""

from __future__ import annotations

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
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ActivationTemplate,
    MissingParameterRouting,
    SolventExchangeTemplate,
    SynthesisUnitProcessKind,
    SynthesisUnitProcessTemplate,
    apply_assumption_routing,
    assumptions_for_missing_parameters,
    capture_protocol,
    missing_parameters,
)


@pytest.fixture
def incomplete_mof_synth_template() -> SynthesisUnitProcessTemplate:
    """A MOF synthesis template missing its metal source and duration."""
    return SynthesisUnitProcessTemplate(
        template_id="mof-2-incomplete",
        title="Incomplete MOF synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
        parameters={
            "organic_linker": "PyBC",
            "solvent": "DMF",
            "temperature_K": 393.0,
            "stoichiometry": 1.0,
        },
    )


def test_synth_missing_parameters_are_detected(
    incomplete_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """Required but unrecorded scientific parameters are the routing input."""
    assert missing_parameters(incomplete_mof_synth_template) == (
        "metal_source",
        "duration_h",
    )


def test_synth_missing_parameters_route_to_real_assumption_records(
    incomplete_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """AC-02: every missing parameter becomes a real Assumption record."""
    routing = assumptions_for_missing_parameters(incomplete_mof_synth_template)
    assert isinstance(routing, MissingParameterRouting)
    assert routing.missing_parameters == ("metal_source", "duration_h")
    assert len(routing.assumptions) == 2
    for entry in routing.assumptions:
        assert isinstance(entry, Assumption)
        assert entry.parameter in ("metal_source", "duration_h")
        assert is_valid_id(entry.assumption_id, "assumption"), entry.assumption_id
        # The default classification for missing scientific parameters is A2
        # (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5).
        assert entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        assert entry.rationale
        assert entry.source_refs == []
    assert routing.assumption_refs == tuple(
        entry.assumption_id for entry in routing.assumptions
    )


def test_synth_routing_reads_back_through_real_assumption_api(
    incomplete_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """The strict label is decided by the real core assumption evaluator."""
    routing = assumptions_for_missing_parameters(incomplete_mof_synth_template)
    # Re-evaluate the routed records with the real API and compare.
    label_assessment = evaluate_strict_label(routing.assumptions)
    assert routing.strict_label_assessment.label == label_assessment.label
    assert routing.strict_label_assessment.matched_label_rule_id == (
        label_assessment.matched_label_rule_id
    )
    # An A2 dominates: NOT_STRICT via R-STRICT-2 (08-STRICT-RECOVERY-CLOSURE SS3).
    assert routing.strict_label_assessment.label is StrictLabel.NOT_STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-2"
    # Each effect is decided by the real per-assumption API.
    for effect in routing.effects:
        assert effect.effect.value == "DISQUALIFIES_PURE_STRICT"
        assert effect.rule_id == "R-EFF-1"


def test_synth_assumption_records_are_schema_conformant(
    incomplete_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """Routed assumptions persist through the real schema persistence gate."""
    routing = assumptions_for_missing_parameters(incomplete_mof_synth_template)
    for entry in routing.assumptions:
        assert validate_and_reject("assumption", entry.to_dict()) is None


def test_synth_routing_with_a1_methodological_default_classifies_strict_with_assumptions() -> None:
    """A1 with reliable method evidence -> STRICT_WITH_ASSUMPTIONS (R-STRICT-3)."""
    template = ActivationTemplate(
        template_id="activation-2",
        title="Activation missing pressure",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
        rationale="1e-3 mbar turbopump base pressure is the instrument default",
        source_refs=["instrument-calibration-2024"],
    )
    assert routing.missing_parameters == ("pressure_mbar",)
    entry = routing.assumptions[0]
    assert entry.classification is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
    assert entry.source_refs == ["instrument-calibration-2024"]
    assert entry.strict_status_effect.value == "STRICT_WITH_ASSUMPTIONS"
    assert routing.strict_label_assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-3"


def test_synth_routing_with_a0_technical_default_keeps_pure_strict() -> None:
    """A0 technical defaults keep the pure strict label (R-STRICT-4)."""
    template = SolventExchangeTemplate(
        template_id="exchange-1",
        title="Exchange missing soaking duration",
        parameters={
            "solvent": "methanol",
            "exchange_cycles": 3,
            "temperature_K": 298.0,
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A0_TECHNICAL_DEFAULT,
    )
    assert routing.missing_parameters == ("soaking_duration_h",)
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-4"
    assert routing.effects[0].effect.value == "NONE"


def test_synth_routing_applies_refs_to_template() -> None:
    """apply_assumption_routing carries the safe refs on the template (AC-02)."""
    template = ActivationTemplate(
        template_id="activation-3",
        title="Activation missing pressure",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    routing = assumptions_for_missing_parameters(template)
    routed = apply_assumption_routing(template, routing)
    assert routed.assumption_refs == routing.assumption_refs
    # The input template is untouched: nothing is mutated, nothing frozen.
    assert template.assumption_refs == ()
    assert routed.frozen is False
    # The refs survive protocol capture.
    assert capture_protocol(routed)["assumption_refs"] == list(routing.assumption_refs)


def test_synth_routing_is_deterministic() -> None:
    """Same template -> identical assumption ids and refs on every call."""
    template = ActivationTemplate(
        template_id="activation-4",
        title="Activation missing pressure",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    first = assumptions_for_missing_parameters(template)
    second = assumptions_for_missing_parameters(template)
    assert first.assumption_refs == second.assumption_refs
    assert first.assumptions[0].assumption_id == second.assumptions[0].assumption_id


def test_synth_routing_is_empty_for_complete_templates() -> None:
    """A complete template routes nothing: empty assumptions, pure strict."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-complete",
        title="Complete MOF synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
        parameters={
            "metal_source": "zinc acetate dihydrate",
            "organic_linker": "PyBC",
            "solvent": "DMF",
            "temperature_K": 393.0,
            "duration_h": 72.0,
            "stoichiometry": 1.0,
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ()
    assert routing.assumptions == ()
    assert routing.assumption_refs == ()
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-1"


def test_synth_routing_accepts_explicit_classification_and_goal_ids() -> None:
    """Explicit classification, rationale and affected goal ids are recorded."""
    template = ActivationTemplate(
        template_id="activation-5",
        title="Activation missing pressure",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        rationale="pressure not disclosed in the published protocol",
        source_refs=["doi-ref"],
        affected_goal_ids=["goal-1"],
    )
    entry = routing.assumptions[0]
    assert entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
    assert entry.rationale == "pressure not disclosed in the published protocol"
    assert entry.source_refs == ["doi-ref"]
    assert entry.affected_goal_ids == ["goal-1"]


def test_synth_routing_type_boundaries_raise_type_error() -> None:
    """Non-AssumptionClassification and malformed refs are TypeError."""
    template = ActivationTemplate(
        template_id="activation-6",
        title="Activation missing pressure",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(
            template, classification="A2"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(
            template, source_refs="not-a-sequence"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(
            template, affected_goal_ids=[1]  # type: ignore[list-item]
        )
    with pytest.raises(TypeError):
        apply_assumption_routing(template, routing="nope")  # type: ignore[arg-type]
