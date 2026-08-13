"""DEV-M11-G03: BET/adsorption/IAST/Qst/breakthrough/cycling templates.

The adsorption template pack: frozen dataclasses with strict
``__post_init__`` validation (TypeError at the type boundaries,
``ValueError``-subclass ``InvalidAdsorptionTemplateError`` for value
violations), ordered first-match-wins rule tables with a trailing total
default and a validated ruleset, and safe registry ids (FND-M9-G02-01).
AC-01: temperature/pressure/composition are explicit Unit Process
condition inputs, declared by the universal ``ADSORPTION_PARAMETER_RULES``
table per (kind, stage) pair; missing required parameters are the input of
the Assumption Registry pathway, never a construction error.

Every test name contains "adsorption" (DEV-M11-G03 naming rule).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from scientific_reproduction.core.models import GoalTrack
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ADSORPTION_PARAMETER_RULES,
    ADSORPTION_RULESET_VERSION,
    ADSORPTION_VALUE_RULES,
    ANALYSIS_STAGE,
    BET_KIND,
    CYCLING_STABILITY_KIND,
    EXECUTION_STAGE,
    IAST_KIND,
    QST_KIND,
    AdsorptionKind,
    AdsorptionStage,
    BetTemplate,
    BreakthroughTemplate,
    CyclingStabilityTemplate,
    IastTemplate,
    InvalidAdsorptionTemplateError,
    QstTemplate,
    SingleComponentTemplate,
    assess_parameter_completeness,
    capture_protocol,
    missing_parameters,
    validate_adsorption_rulesets,
    validate_template_values,
)


@pytest.fixture
def bet_execution_template() -> BetTemplate:
    """A complete BET raw-isotherm execution template (AC-01 conditions)."""
    return BetTemplate(
        template_id="bet-1-isotherm-77",
        title="BET isotherm acquisition",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
    )


@pytest.fixture
def single_component_execution_template() -> SingleComponentTemplate:
    """A complete single-component execution template (AC-01)."""
    return SingleComponentTemplate(
        template_id="isotherm-1-point",
        title="Single-component isotherm point",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
        },
    )


@pytest.fixture
def breakthrough_execution_template() -> BreakthroughTemplate:
    """A complete dynamic-breakthrough execution template (AC-01/AC-03)."""
    return BreakthroughTemplate(
        template_id="breakthrough-1-run",
        title="Dynamic breakthrough run",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "flow_rate_ml_min": 8.0,
            "adsorbent_mass_mg": 1200.0,
            "column_length_mm": 220.0,
            "column_diameter_mm": 4.0,
            "dead_volume_ml": 1.2,
            "detector": "gas_chromatograph",
            "regeneration_protocol": "vacuum at elevated temperature",
            "cycle_count": 5,
        },
    )


def test_adsorption_kind_and_stage_vocabulary_cover_all_families() -> None:
    """The six capability families and two metadata surfaces are declared."""
    assert set(AdsorptionKind) == {
        AdsorptionKind.BET,
        AdsorptionKind.SINGLE_COMPONENT,
        AdsorptionKind.IAST,
        AdsorptionKind.QST,
        AdsorptionKind.BREAKTHROUGH,
        AdsorptionKind.CYCLING_STABILITY,
    }
    assert AdsorptionKind.BET.value == "bet"
    assert AdsorptionKind.CYCLING_STABILITY.value == "cycling_stability"
    assert set(AdsorptionStage) == {
        AdsorptionStage.EXECUTION,
        AdsorptionStage.ANALYSIS,
    }
    assert EXECUTION_STAGE is AdsorptionStage.EXECUTION
    assert ANALYSIS_STAGE is AdsorptionStage.ANALYSIS


def test_adsorption_ruleset_validation_returns_unique_total_ids() -> None:
    """The ordered tables are total and the ids are unique."""
    ids = validate_adsorption_rulesets()
    assert ids == tuple(
        rule.rule_id for rule in ADSORPTION_PARAMETER_RULES
    ) + tuple(rule.rule_id for rule in ADSORPTION_VALUE_RULES)
    assert len(set(ids)) == len(ids)
    assert ADSORPTION_RULESET_VERSION == "1.0"


def test_adsorption_rule_tables_cover_every_kind_stage_pair() -> None:
    """The parameter table is a total function of the (kind, stage) pairs."""
    pairs = [
        (kind, stage)
        for kind in AdsorptionKind
        for stage in AdsorptionStage
    ]
    for kind, stage in pairs:
        matching = [
            rule
            for rule in ADSORPTION_PARAMETER_RULES
            if rule.predicate(kind, stage)
        ]
        assert matching, f"no rule matches {kind.value}/{stage.value}"
        # First match wins: the matched rule is the earliest one.
        assert matching[0] is next(
            rule
            for rule in ADSORPTION_PARAMETER_RULES
            if rule.predicate(kind, stage)
        )
    # The trailing total default (R-ADS-P0) matches every pair.
    for kind, stage in pairs:
        assert ADSORPTION_PARAMETER_RULES[-1].predicate(kind, stage)
    assert ADSORPTION_PARAMETER_RULES[-1].rule_id == "R-ADS-P0"


def test_adsorption_value_rule_table_is_total_over_required_parameters() -> None:
    """Every required parameter name has exactly one universal value rule."""
    validate_adsorption_rulesets()
    value_ruled = {rule.parameter for rule in ADSORPTION_VALUE_RULES}
    required = {
        parameter
        for rule in ADSORPTION_PARAMETER_RULES
        for parameter in rule.required_parameters
    }
    assert required <= value_ruled
    value_parameters = [rule.parameter for rule in ADSORPTION_VALUE_RULES]
    assert len(value_parameters) == len(set(value_parameters))


def test_adsorption_bet_execution_requires_adsorbate_temperature_and_mass() -> None:
    """BET execution records the gas, the temperature and the sample mass."""
    template = BetTemplate(
        template_id="bet-empty",
        title="Empty BET",
        stage=EXECUTION_STAGE,
    )
    assert missing_parameters(template) == (
        "adsorbate",
        "temperature_K",
        "sample_mass_mg",
    )
    assessment = assess_parameter_completeness(template)
    assert assessment.matched_rule_id == "R-ADS-P1"
    assert assessment.missing_parameters == (
        "adsorbate",
        "temperature_K",
        "sample_mass_mg",
    )


def test_adsorption_single_component_execution_requires_temperature_pressure() -> None:
    """AC-01: temperature and pressure are explicit execution conditions."""
    template = SingleComponentTemplate(
        template_id="isotherm-empty",
        title="Empty isotherm",
        stage=EXECUTION_STAGE,
    )
    assert missing_parameters(template) == (
        "adsorbate",
        "temperature_K",
        "pressure_kPa",
    )
    assert assess_parameter_completeness(template).matched_rule_id == "R-ADS-P3"
    # The completeness assessment records every rule decision (audit trail):
    # the specific rule and the trailing total default both match, and the
    # first match in table order decides.
    decisions = assess_parameter_completeness(template).decisions
    assert len(decisions) == len(ADSORPTION_PARAMETER_RULES)
    matched_ids = [decision.rule_id for decision in decisions if decision.matched]
    assert matched_ids == ["R-ADS-P3", "R-ADS-P0"]


def test_adsorption_breakthrough_execution_requires_full_column_surface() -> None:
    """AC-01: flow, column geometry, dead volume and detector are explicit."""
    template = BreakthroughTemplate(
        template_id="breakthrough-empty",
        title="Empty breakthrough",
        stage=EXECUTION_STAGE,
    )
    assert missing_parameters(template) == (
        "adsorbate",
        "co_adsorbate",
        "composition_fraction",
        "temperature_K",
        "pressure_kPa",
        "flow_rate_ml_min",
        "adsorbent_mass_mg",
        "column_length_mm",
        "column_diameter_mm",
        "dead_volume_ml",
        "detector",
        "regeneration_protocol",
        "cycle_count",
    )
    assert assess_parameter_completeness(template).matched_rule_id == "R-ADS-P7"


def test_adsorption_iast_analysis_requires_composition_conditions() -> None:
    """AC-01: IAST records the pair, composition, temperature and pressure."""
    template = IastTemplate(
        template_id="iast-empty",
        title="Empty IAST",
        stage=ANALYSIS_STAGE,
    )
    assert missing_parameters(template) == (
        "property",
        "adsorbate",
        "co_adsorbate",
        "composition_fraction",
        "temperature_K",
        "pressure_kPa",
        "model",
        "sampling_validation",
    )
    assert assess_parameter_completeness(template).matched_rule_id == "R-ADS-P5"


def test_adsorption_qst_analysis_requires_temperature_pair() -> None:
    """Qst records the low/high isosteric pair and the reference loading."""
    template = QstTemplate(
        template_id="qst-empty",
        title="Empty Qst",
        stage=ANALYSIS_STAGE,
    )
    assert missing_parameters(template) == (
        "property",
        "adsorbate",
        "temperature_low_K",
        "temperature_high_K",
        "reference_loading_mol_kg",
    )
    assert assess_parameter_completeness(template).matched_rule_id == "R-ADS-P6"


def test_adsorption_cycling_stability_execution_requires_atmosphere() -> None:
    """Cycling/stability records the atmosphere and regeneration protocol."""
    template = CyclingStabilityTemplate(
        template_id="cycling-empty",
        title="Empty cycling",
        stage=EXECUTION_STAGE,
    )
    assert missing_parameters(template) == (
        "adsorbate",
        "temperature_K",
        "pressure_kPa",
        "cycle_count",
        "regeneration_protocol",
        "atmosphere",
    )
    assert assess_parameter_completeness(template).matched_rule_id == "R-ADS-P9"


def test_adsorption_iast_and_qst_execution_are_decided_by_total_default() -> None:
    """AC-02: IAST/Qst raw execution uses the isotherm execution surface.

    Neither pair declares its own execution rule: the trailing total
    default (R-ADS-P0) decides them, so their raw execution surface is the
    single-component isotherm template.
    """
    iast_execution = IastTemplate(
        template_id="iast-exec",
        title="IAST execution",
        stage=EXECUTION_STAGE,
    )
    qst_execution = QstTemplate(
        template_id="qst-exec",
        title="Qst execution",
        stage=EXECUTION_STAGE,
    )
    assert assess_parameter_completeness(iast_execution).matched_rule_id == "R-ADS-P0"
    assert missing_parameters(iast_execution) == ()
    assert assess_parameter_completeness(qst_execution).matched_rule_id == "R-ADS-P0"
    assert missing_parameters(qst_execution) == ()


def test_adsorption_complete_templates_have_no_missing_parameters(
    bet_execution_template: BetTemplate,
    single_component_execution_template: SingleComponentTemplate,
    breakthrough_execution_template: BreakthroughTemplate,
) -> None:
    """Complete templates assess empty: nothing routes to the registry."""
    assert missing_parameters(bet_execution_template) == ()
    assert missing_parameters(single_component_execution_template) == ()
    assert missing_parameters(breakthrough_execution_template) == ()


def test_adsorption_constructor_applies_value_rules_to_present_parameters() -> None:
    """Value violations of recorded parameters fail construction (V-rules)."""
    cases = [
        {"temperature_K": -5.0},  # R-ADS-V3
        {"pressure_kPa": -0.1},  # R-ADS-V6
        {"composition_fraction": 1.5},  # R-ADS-V7
        {"relative_pressure_min": 0.0},  # R-ADS-V8 (strict fraction)
        {"relative_pressure_max": 1.0},  # R-ADS-V9
        {"sample_mass_mg": 0.0},  # R-ADS-V10
        {"flow_rate_ml_min": -1.0},  # R-ADS-V12
        {"dead_volume_ml": -0.5},  # R-ADS-V15
        {"cycle_count": 0},  # R-ADS-V18
        {"atmosphere": "helium"},  # R-ADS-V19 (controlled vocabulary)
        {"convergence_threshold": 0.0},  # R-ADS-V24
        {"tolerance": -1.0},  # R-ADS-V29
    ]
    for parameters in cases:
        with pytest.raises(InvalidAdsorptionTemplateError):
            SingleComponentTemplate(
                template_id="bad-values",
                title="Bad values",
                stage=EXECUTION_STAGE,
                parameters={"adsorbate": "propene", **parameters},
            )


def test_adsorption_value_validation_assessment_records_decisions(
    single_component_execution_template: SingleComponentTemplate,
) -> None:
    """validate_template_values is auditable and deterministic."""
    assessment = validate_template_values(single_component_execution_template)
    assert assessment.template_id == "isotherm-1-point"
    assert assessment.violations == ()
    assert assessment.matched_rule_id is None
    assert assessment.ruleset_version == ADSORPTION_RULESET_VERSION
    applied = {
        decision.rule_id
        for decision in assessment.decisions
        if decision.applied
    }
    assert applied == {"R-ADS-V1", "R-ADS-V3", "R-ADS-V6"}


def test_adsorption_template_rejects_unsafe_template_ids() -> None:
    """Template ids are safe single registry path segments (FND-M9-G02-01)."""
    for template_id in (
        "bet/1",
        "bet\\1",
        "bet 1",
        "bet*1",
        "bet?1",
        "bet[1]",
        ".",
        "..",
        "",
    ):
        with pytest.raises(InvalidAdsorptionTemplateError):
            BetTemplate(
                template_id=template_id,
                title="Unsafe id",
                stage=EXECUTION_STAGE,
            )


def test_adsorption_template_class_rejects_foreign_kinds() -> None:
    """A template class fixes its kind; other kinds are rejected."""
    with pytest.raises(InvalidAdsorptionTemplateError):
        BetTemplate(
            template_id="bet-wrong-kind",
            title="Wrong kind",
            stage=EXECUTION_STAGE,
            kind=IAST_KIND,
        )
    # The classes fix their own kinds when the caller omits the field.
    assert BetTemplate(
        template_id="bet-kind", title="Kind", stage=EXECUTION_STAGE
    ).kind is BET_KIND
    assert IastTemplate(
        template_id="iast-kind", title="Kind", stage=ANALYSIS_STAGE
    ).kind is IAST_KIND
    assert QstTemplate(
        template_id="qst-kind", title="Kind", stage=ANALYSIS_STAGE
    ).kind is QST_KIND
    assert CyclingStabilityTemplate(
        template_id="cycling-kind", title="Kind", stage=EXECUTION_STAGE
    ).kind is CYCLING_STABILITY_KIND


def test_adsorption_template_type_boundaries_raise_type_error() -> None:
    """Wrong types are TypeError at the boundary, never silent coercion."""
    bad_values: dict[str, tuple[Any, dict[str, Any]]] = {
        "template_id": (123, {}),
        "title": (None, {}),
        "stage": ("execution", {}),
        "kind": ("bet", {}),
        "track": ("STRICT", {}),
        "parameters": ([], {}),
        "assumption_refs": ("not-a-tuple", {}),
        "frozen": ("yes", {}),
        "notes": (3, {}),
    }
    for field_name, (bad_value, extra) in bad_values.items():
        with pytest.raises(TypeError):
            BetTemplate(
                template_id="bet-bad",
                title="Bad boundary",
                stage=EXECUTION_STAGE,
                **{field_name: bad_value},
                **extra,
            )


def test_adsorption_templates_are_frozen_dataclasses(
    bet_execution_template: BetTemplate,
) -> None:
    """A template rejects any field mutation after construction."""
    with pytest.raises(FrozenInstanceError):
        setattr(bet_execution_template, "title", "mutated")


def test_adsorption_templates_are_proposed_on_strict_track_by_default() -> None:
    """The default track label is the frozen STRICT_REPRODUCTION label."""
    template = BetTemplate(
        template_id="bet-track",
        title="Track",
        stage=EXECUTION_STAGE,
    )
    assert template.track is GoalTrack.STRICT_REPRODUCTION


def test_adsorption_capture_protocol_records_condition_inputs(
    single_component_execution_template: SingleComponentTemplate,
) -> None:
    """AC-01: the capture records the explicit condition inputs verbatim."""
    capture = capture_protocol(single_component_execution_template)
    assert capture["template_id"] == "isotherm-1-point"
    assert capture["stage"] == "execution"
    assert capture["kind"] == "single_component"
    assert capture["track"] == "STRICT_REPRODUCTION"
    assert capture["frozen"] is False
    assert capture["parameter_table"] == [
        {"parameter": "adsorbate", "value": "propene"},
        {"parameter": "pressure_kPa", "value": 100.0},
        {"parameter": "temperature_K", "value": 298.0},
    ]
    assert capture["assumption_refs"] == []
    assert capture["results"] is None
    assert capture["notes"] is None


def test_adsorption_capture_parameter_table_is_sorted_deterministically() -> None:
    """The parameter table is sorted: same template -> identical capture."""
    shuffled = SingleComponentTemplate(
        template_id="isotherm-shuffled",
        title="Shuffled",
        stage=EXECUTION_STAGE,
        parameters={
            "pressure_kPa": 100.0,
            "adsorbate": "propene",
            "temperature_K": 298.0,
        },
    )
    capture = capture_protocol(shuffled)["parameter_table"]
    assert [entry["parameter"] for entry in capture] == [
        "adsorbate",
        "pressure_kPa",
        "temperature_K",
    ]
