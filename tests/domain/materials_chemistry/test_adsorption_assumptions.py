"""DEV-M11-G03: missing adsorption conditions enter the Assumption Registry.

AC-01: temperature/pressure/composition and breakthrough column parameters
that an adsorption template does not record are routed through the EXISTING
Assumption Registry pathway -- real ``core.models.Assumption`` records
decided and read back through the real ``core.rules.assumptions``
``assumption_effect`` / ``evaluate_strict_label`` APIs (never a parallel
store), with the assumption refs carried on the template. A2 defaults
follow 16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5.

Every test name contains "adsorption" (DEV-M11-G03 naming rule).
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
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    BetTemplate,
    BreakthroughTemplate,
    IastTemplate,
    MissingParameterRouting,
    SingleComponentTemplate,
    apply_assumption_routing,
    assumptions_for_missing_parameters,
    capture_protocol,
    missing_parameters,
)


@pytest.fixture
def incomplete_single_component_execution_template() -> SingleComponentTemplate:
    """A single-component execution template missing its conditions (AC-01)."""
    return SingleComponentTemplate(
        template_id="isotherm-2-incomplete",
        title="Incomplete isotherm execution",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
        },
    )


@pytest.fixture
def incomplete_breakthrough_execution_template() -> BreakthroughTemplate:
    """A breakthrough execution template missing its column parameters."""
    return BreakthroughTemplate(
        template_id="breakthrough-2-incomplete",
        title="Incomplete breakthrough execution",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "flow_rate_ml_min": 8.0,
            "adsorbent_mass_mg": 1200.0,
            "detector": "gas_chromatograph",
        },
    )


def test_adsorption_missing_conditions_are_detected(
    incomplete_single_component_execution_template: SingleComponentTemplate,
) -> None:
    """Required but unrecorded temperature/pressure are the routing input."""
    assert missing_parameters(incomplete_single_component_execution_template) == (
        "temperature_K",
        "pressure_kPa",
    )


def test_adsorption_missing_conditions_route_to_real_assumption_records(
    incomplete_single_component_execution_template: SingleComponentTemplate,
) -> None:
    """AC-01: every missing temperature/pressure becomes a real record."""
    routing = assumptions_for_missing_parameters(
        incomplete_single_component_execution_template
    )
    assert isinstance(routing, MissingParameterRouting)
    assert routing.missing_parameters == ("temperature_K", "pressure_kPa")
    assert len(routing.assumptions) == 2
    for entry in routing.assumptions:
        assert isinstance(entry, Assumption)
        assert entry.parameter in ("temperature_K", "pressure_kPa")
        assert is_valid_id(entry.assumption_id, "assumption"), entry.assumption_id
        # The default classification for missing scientific settings is A2
        # (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5).
        assert (
            entry.classification
            is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        )
        assert entry.rationale
        assert entry.source_refs == []
    assert routing.assumption_refs == tuple(
        entry.assumption_id for entry in routing.assumptions
    )


def test_adsorption_routing_reads_back_through_real_assumption_api(
    incomplete_single_component_execution_template: SingleComponentTemplate,
) -> None:
    """The strict label is decided by the real core assumption evaluator."""
    routing = assumptions_for_missing_parameters(
        incomplete_single_component_execution_template
    )
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


def test_adsorption_missing_column_parameters_enter_assumption_registry(
    incomplete_breakthrough_execution_template: BreakthroughTemplate,
) -> None:
    """AC-01: missing breakthrough column parameters are A2-registered."""
    routing = assumptions_for_missing_parameters(
        incomplete_breakthrough_execution_template
    )
    assert routing.missing_parameters == (
        "column_length_mm",
        "column_diameter_mm",
        "dead_volume_ml",
        "regeneration_protocol",
        "cycle_count",
    )
    assert len(routing.assumptions) == 5
    for entry in routing.assumptions:
        assert (
            entry.classification
            is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        )
        assert is_valid_id(entry.assumption_id, "assumption"), entry.assumption_id


def test_adsorption_assumption_records_are_schema_conformant(
    incomplete_single_component_execution_template: SingleComponentTemplate,
) -> None:
    """Routed assumptions persist through the real schema persistence gate."""
    routing = assumptions_for_missing_parameters(
        incomplete_single_component_execution_template
    )
    for entry in routing.assumptions:
        assert validate_and_reject("assumption", entry.to_dict()) is None


def test_adsorption_routing_with_a1_classifies_strict_with_assumptions() -> None:
    """A1 with reliable method evidence -> STRICT_WITH_ASSUMPTIONS (R-STRICT-3)."""
    template = SingleComponentTemplate(
        template_id="isotherm-3-missing-temperature",
        title="Isotherm missing temperature",
        stage=EXECUTION_STAGE,
        parameters={"adsorbate": "propene", "pressure_kPa": 100.0},
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
        rationale="the measurement cell bath temperature is the packaged default",
        source_refs=["instrument-manual"],
    )
    assert routing.missing_parameters == ("temperature_K",)
    entry = routing.assumptions[0]
    assert (
        entry.classification is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
    )
    assert entry.source_refs == ["instrument-manual"]
    effect = entry.strict_status_effect
    assert effect is not None
    assert effect.value == "STRICT_WITH_ASSUMPTIONS"
    assert (
        routing.strict_label_assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    )
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-3"


def test_adsorption_routing_with_a0_technical_default_keeps_pure_strict() -> None:
    """A0 technical defaults keep the pure strict label (R-STRICT-4)."""
    template = IastTemplate(
        template_id="iast-2-missing-composition",
        title="IAST missing composition",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "selectivity",
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "model": "dual_site_langmuir",
            "sampling_validation": "replicate isotherms from independent batches",
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A0_TECHNICAL_DEFAULT,
    )
    assert routing.missing_parameters == ("composition_fraction",)
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-4"
    assert routing.effects[0].effect.value == "NONE"


def test_adsorption_routing_applies_refs_to_template() -> None:
    """apply_assumption_routing carries the safe refs on the template (AC-01)."""
    template = SingleComponentTemplate(
        template_id="isotherm-4-incomplete",
        title="Isotherm incomplete",
        stage=EXECUTION_STAGE,
        parameters={"adsorbate": "propene", "temperature_K": 298.0},
    )
    routing = assumptions_for_missing_parameters(template)
    routed = apply_assumption_routing(template, routing)
    assert routed.assumption_refs == routing.assumption_refs
    # The input template is untouched: nothing is mutated, nothing frozen.
    assert template.assumption_refs == ()
    assert routed.frozen is False
    # The refs survive protocol capture.
    assert capture_protocol(routed)["assumption_refs"] == list(
        routing.assumption_refs
    )


def test_adsorption_routing_is_deterministic() -> None:
    """Same template -> identical assumption ids and refs on every call."""
    template = SingleComponentTemplate(
        template_id="isotherm-5-incomplete",
        title="Isotherm incomplete",
        stage=EXECUTION_STAGE,
        parameters={"adsorbate": "propene", "temperature_K": 298.0},
    )
    first = assumptions_for_missing_parameters(template)
    second = assumptions_for_missing_parameters(template)
    assert first.assumption_refs == second.assumption_refs
    assert first.assumptions[0].assumption_id == second.assumptions[0].assumption_id


def test_adsorption_routing_is_empty_for_complete_templates() -> None:
    """A complete template routes nothing: empty assumptions, pure strict."""
    template = BetTemplate(
        template_id="bet-3-complete",
        title="Complete BET execution",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ()
    assert routing.assumptions == ()
    assert routing.assumption_refs == ()
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-1"


def test_adsorption_routing_accepts_explicit_classification_and_goal_ids() -> None:
    """Explicit classification, rationale and affected goal ids are recorded."""
    template = BreakthroughTemplate(
        template_id="breakthrough-3-missing-flow",
        title="Breakthrough missing flow rate",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "adsorbent_mass_mg": 1200.0,
            "column_length_mm": 220.0,
            "column_diameter_mm": 4.0,
            "dead_volume_ml": 1.2,
            "detector": "gas_chromatograph",
            "regeneration_protocol": "vacuum at elevated temperature",
            "cycle_count": 5,
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        rationale="flow rate not disclosed in the published protocol",
        source_refs=["doi-ref"],
        affected_goal_ids=["goal-1"],
    )
    entry = routing.assumptions[0]
    assert entry.parameter == "flow_rate_ml_min"
    assert (
        entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
    )
    assert entry.rationale == "flow rate not disclosed in the published protocol"
    assert entry.source_refs == ["doi-ref"]
    assert entry.affected_goal_ids == ["goal-1"]


def test_adsorption_analysis_missing_validation_inputs_route_to_registry() -> None:
    """AC-01/AC-02: missing analysis validation inputs also route to the registry."""
    template = IastTemplate(
        template_id="iast-3-analysis",
        title="IAST analysis missing sampling validation",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "selectivity",
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "model": "dual_site_langmuir",
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ("sampling_validation",)
    assert routing.assumptions[0].parameter == "sampling_validation"
    assert routing.strict_label_assessment.label is StrictLabel.NOT_STRICT


def test_adsorption_routing_type_boundaries_raise_type_error() -> None:
    """Non-AssumptionClassification and malformed refs are TypeError."""
    template = SingleComponentTemplate(
        template_id="isotherm-7-boundary",
        title="Boundary template",
        stage=EXECUTION_STAGE,
        parameters={"adsorbate": "propene"},
    )
    bad_classification: Any = "A2"
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(
            template, classification=bad_classification
        )
    bad_source_refs: Any = "not-a-sequence"
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(template, source_refs=bad_source_refs)
    bad_goal_ids: Any = [1]
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(template, affected_goal_ids=bad_goal_ids)
    bad_routing: Any = "nope"
    with pytest.raises(TypeError):
        apply_assumption_routing(template, routing=bad_routing)
    bad_template: Any = "nope"
    with pytest.raises(TypeError):
        assumptions_for_missing_parameters(bad_template)
