"""DEV-M11-G04: missing computation inputs enter the Assumption Registry.

AC-01: software/method/force-field/functional/convergence inputs that a
computation template does not record are routed through the EXISTING
Assumption Registry pathway -- real ``core.models.Assumption`` records
decided and read back through the real ``core.rules.assumptions``
``assumption_effect`` / ``evaluate_strict_label`` APIs (never a parallel
store), with the assumption refs carried on the template. A2 defaults
follow 16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5.

Every test name contains "comput" (DEV-M11-G04 naming rule).
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
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    DftTemplate,
    GcmcTemplate,
    MissingParameterRouting,
    StructurePreparationTemplate,
    apply_assumption_routing,
    assumptions_for_missing_parameters,
    capture_protocol,
    missing_parameters,
)


@pytest.fixture
def incomplete_dft_execution_template() -> DftTemplate:
    """A DFT execution template missing its functional and convergence inputs."""
    return DftTemplate(
        template_id="dft-2-incomplete",
        title="Incomplete DFT execution",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
        },
    )


def test_comput_missing_parameters_are_detected(
    incomplete_dft_execution_template: DftTemplate,
) -> None:
    """Required but unrecorded scientific inputs are the routing input."""
    assert missing_parameters(incomplete_dft_execution_template) == (
        "functional",
        "energy_cutoff_ev",
        "convergence_tolerance",
    )


def test_comput_missing_parameters_route_to_real_assumption_records(
    incomplete_dft_execution_template: DftTemplate,
) -> None:
    """AC-01: every missing functional/convergence input becomes a real record."""
    routing = assumptions_for_missing_parameters(incomplete_dft_execution_template)
    assert isinstance(routing, MissingParameterRouting)
    assert routing.missing_parameters == (
        "functional",
        "energy_cutoff_ev",
        "convergence_tolerance",
    )
    assert len(routing.assumptions) == 3
    for entry in routing.assumptions:
        assert isinstance(entry, Assumption)
        assert entry.parameter in (
            "functional",
            "energy_cutoff_ev",
            "convergence_tolerance",
        )
        assert is_valid_id(entry.assumption_id, "assumption"), entry.assumption_id
        # The default classification for missing scientific settings is A2
        # (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5).
        assert entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
        assert entry.rationale
        assert entry.source_refs == []
    assert routing.assumption_refs == tuple(
        entry.assumption_id for entry in routing.assumptions
    )


def test_comput_routing_reads_back_through_real_assumption_api(
    incomplete_dft_execution_template: DftTemplate,
) -> None:
    """The strict label is decided by the real core assumption evaluator."""
    routing = assumptions_for_missing_parameters(incomplete_dft_execution_template)
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


def test_comput_assumption_records_are_schema_conformant(
    incomplete_dft_execution_template: DftTemplate,
) -> None:
    """Routed assumptions persist through the real schema persistence gate."""
    routing = assumptions_for_missing_parameters(incomplete_dft_execution_template)
    for entry in routing.assumptions:
        assert validate_and_reject("assumption", entry.to_dict()) is None


def test_comput_routing_with_a1_classifies_strict_with_assumptions() -> None:
    """A1 with reliable method evidence -> STRICT_WITH_ASSUMPTIONS (R-STRICT-3)."""
    template = GcmcTemplate(
        template_id="gcmc-3-missing-seed",
        title="GCMC missing random seed",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "raspa",
            "software_version": "2.0.47",
            "force_field": "UFF",
            "charges": "none",
            "mixing_rules": "lorentz_berthelot",
            "cutoff_angstrom": 12.0,
            "temperature_K": 298.0,
            "pressure_bar": 1.0,
            "equilibration_cycles": 10000,
            "production_cycles": 20000,
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A1_METHODOLOGICAL_DEFAULT,
        rationale="raspa default seed 1 is the packaged default",
        source_refs=["raspa-release-notes-2.0.47"],
    )
    assert routing.missing_parameters == ("seed",)
    entry = routing.assumptions[0]
    assert entry.classification is AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
    assert entry.source_refs == ["raspa-release-notes-2.0.47"]
    effect = entry.strict_status_effect
    assert effect is not None
    assert effect.value == "STRICT_WITH_ASSUMPTIONS"
    assert routing.strict_label_assessment.label is StrictLabel.STRICT_WITH_ASSUMPTIONS
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-3"


def test_comput_routing_with_a0_technical_default_keeps_pure_strict() -> None:
    """A0 technical defaults keep the pure strict label (R-STRICT-4)."""
    template = DftTemplate(
        template_id="dft-3-missing-tolerance",
        title="DFT missing convergence tolerance",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "functional": "PBE",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
            "energy_cutoff_ev": 520.0,
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A0_TECHNICAL_DEFAULT,
    )
    assert routing.missing_parameters == ("convergence_tolerance",)
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-4"
    assert routing.effects[0].effect.value == "NONE"


def test_comput_routing_applies_refs_to_template() -> None:
    """apply_assumption_routing carries the safe refs on the template (AC-01)."""
    template = DftTemplate(
        template_id="dft-4-missing-functional",
        title="DFT missing functional",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
            "energy_cutoff_ev": 520.0,
            "convergence_tolerance": 1e-5,
        },
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


def test_comput_routing_is_deterministic() -> None:
    """Same template -> identical assumption ids and refs on every call."""
    template = DftTemplate(
        template_id="dft-5-missing-functional",
        title="DFT missing functional",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
            "energy_cutoff_ev": 520.0,
            "convergence_tolerance": 1e-5,
        },
    )
    first = assumptions_for_missing_parameters(template)
    second = assumptions_for_missing_parameters(template)
    assert first.assumption_refs == second.assumption_refs
    assert first.assumptions[0].assumption_id == second.assumptions[0].assumption_id


def test_comput_routing_is_empty_for_complete_templates() -> None:
    """A complete template routes nothing: empty assumptions, pure strict."""
    template = DftTemplate(
        template_id="dft-6-complete",
        title="Complete DFT execution",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "functional": "PBE",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
            "energy_cutoff_ev": 520.0,
            "convergence_tolerance": 1e-5,
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ()
    assert routing.assumptions == ()
    assert routing.assumption_refs == ()
    assert routing.strict_label_assessment.label is StrictLabel.STRICT
    assert routing.strict_label_assessment.matched_label_rule_id == "R-STRICT-1"


def test_comput_routing_accepts_explicit_classification_and_goal_ids() -> None:
    """Explicit classification, rationale and affected goal ids are recorded."""
    template = StructurePreparationTemplate(
        template_id="prep-2-missing-disorder",
        title="Structure preparation missing disorder treatment",
        stage=EXECUTION_STAGE,
        parameters={
            "structure_source": "scxrd_solution_model",
            "method": "supercell_construction",
            "software": "pymatgen",
            "software_version": "2024.5.1",
        },
    )
    routing = assumptions_for_missing_parameters(
        template,
        classification=AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
        rationale="disorder treatment not disclosed in the published protocol",
        source_refs=["doi-ref"],
        affected_goal_ids=["goal-1"],
    )
    entry = routing.assumptions[0]
    assert entry.parameter == "disorder_treatment"
    assert entry.classification is AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
    assert (
        entry.rationale
        == "disorder treatment not disclosed in the published protocol"
    )
    assert entry.source_refs == ["doi-ref"]
    assert entry.affected_goal_ids == ["goal-1"]


def test_comput_analysis_missing_validation_inputs_route_to_registry() -> None:
    """AC-01/AC-02: missing analysis validation inputs also route to the registry."""
    template = GcmcTemplate(
        template_id="gcmc-4-analysis",
        title="GCMC analysis missing uncertainty metric",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "absolute_adsorption",
            "convergence_metric": "block_average_drift",
            "convergence_threshold": 0.01,
            "sampling_validation": "production blocks beyond correlation time",
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ("statistical_uncertainty_metric",)
    assert routing.assumptions[0].parameter == "statistical_uncertainty_metric"
    assert routing.strict_label_assessment.label is StrictLabel.NOT_STRICT


def test_comput_routing_type_boundaries_raise_type_error() -> None:
    """Non-AssumptionClassification and malformed refs are TypeError."""
    template = DftTemplate(
        template_id="dft-7-boundary",
        title="Boundary template",
        stage=EXECUTION_STAGE,
        parameters={"software": "vasp"},
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
