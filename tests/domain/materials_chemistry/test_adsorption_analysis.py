"""DEV-M11-G03: analysis is a separate metadata surface from raw execution.

AC-02: BET/IAST/Qst (and single-component/breakthrough/cycling) analysis is
separate from raw execution -- its own required parameter sets, its own
frozen state, and templates that are distinct instances of the (kind,
stage) vocabulary. Analysis templates capture the fitting/selection inputs
(BET relative-pressure range, IAST pair and composition, Qst temperature
pair and reference loading); execution templates capture the raw
measurement conditions. IAST/Qst raw execution is the single-component
isotherm execution surface (R-ADS-P0).

Every test name contains "adsorption" (DEV-M11-G03 naming rule).
"""

from __future__ import annotations

from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ADSORPTION_PARAMETER_RULES,
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    AdsorptionKind,
    AdsorptionStage,
    BetTemplate,
    IastTemplate,
    QstTemplate,
    SingleComponentTemplate,
    assess_parameter_completeness,
    capture_protocol,
    missing_parameters,
)


def test_adsorption_bet_analysis_is_separate_from_raw_execution() -> None:
    """AC-02: BET analysis records the fitting inputs, not the isotherm."""
    analysis = BetTemplate(
        template_id="bet-2-analysis",
        title="BET surface-area analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "surface_area",
            "model": "bet",
            "relative_pressure_min": 0.05,
            "relative_pressure_max": 0.3,
        },
    )
    execution = BetTemplate(
        template_id="bet-2-execution",
        title="BET isotherm acquisition",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
    )
    assert missing_parameters(analysis) == ()
    assert missing_parameters(execution) == ()
    assert assess_parameter_completeness(analysis).matched_rule_id == "R-ADS-P2"
    assert assess_parameter_completeness(execution).matched_rule_id == "R-ADS-P1"
    # The two surfaces are distinct templates with distinct ids and stages.
    assert analysis.template_id != execution.template_id
    assert analysis.stage is AdsorptionStage.ANALYSIS
    assert execution.stage is AdsorptionStage.EXECUTION


def test_adsorption_analysis_and_execution_surfaces_are_disjoint_per_kind() -> None:
    """AC-02: within a kind, no parameter is required by both surfaces."""
    required = {
        (kind, stage): frozenset(rule.required_parameters)
        for kind in AdsorptionKind
        for stage in AdsorptionStage
        for rule in (
            next(
                candidate
                for candidate in ADSORPTION_PARAMETER_RULES
                if candidate.predicate(kind, stage)
            ),
        )
    }
    for kind in AdsorptionKind:
        execution_set = required[(kind, AdsorptionStage.EXECUTION)]
        analysis_set = required[(kind, AdsorptionStage.ANALYSIS)]
        assert not (execution_set & analysis_set), (
            f"{kind.value} execution and analysis share required parameters"
        )


def test_adsorption_single_component_analysis_validates_data_quality_inputs() -> None:
    """AC-02: analysis captures the validation surface of SS5 data quality."""
    analysis = SingleComponentTemplate(
        template_id="isotherm-2-analysis",
        title="Isotherm analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "uptake",
            "model": "langmuir",
            "convergence_metric": "relative_drift",
            "convergence_threshold": 1e-3,
            "statistical_uncertainty_metric": "standard_error",
            "sampling_validation": "independent material batches",
        },
    )
    assert missing_parameters(analysis) == ()
    assert assess_parameter_completeness(analysis).matched_rule_id == "R-ADS-P4"
    # The same kind's execution surface still records the raw conditions.
    assert missing_parameters(
        SingleComponentTemplate(
            template_id="isotherm-2-execution",
            title="Isotherm execution",
            stage=EXECUTION_STAGE,
        )
    ) == ("adsorbate", "temperature_K", "pressure_kPa")


def test_adsorption_iast_analysis_requires_pair_composition_and_conditions() -> None:
    """AC-02/AC-01: IAST analysis is its own required parameter set."""
    analysis = IastTemplate(
        template_id="iast-2-analysis",
        title="IAST selectivity analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "selectivity",
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "model": "dual_site_langmuir",
            "sampling_validation": "replicate isotherms from independent batches",
        },
    )
    assert missing_parameters(analysis) == ()
    assert assess_parameter_completeness(analysis).matched_rule_id == "R-ADS-P5"


def test_adsorption_qst_analysis_requires_isosteric_temperature_pair() -> None:
    """AC-02: Qst analysis records its low/high temperature pair."""
    analysis = QstTemplate(
        template_id="qst-2-analysis",
        title="Isosteric heat analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "isosteric_heat",
            "adsorbate": "propene",
            "temperature_low_K": 298.0,
            "temperature_high_K": 308.0,
            "reference_loading_mol_kg": 1.5,
        },
    )
    assert missing_parameters(analysis) == ()
    assert assess_parameter_completeness(analysis).matched_rule_id == "R-ADS-P6"


def test_adsorption_iast_and_qst_have_analysis_only_surfaces() -> None:
    """AC-02: IAST/Qst declare no execution rule (R-ADS-P0 decides)."""
    for kind, template_class in (
        (AdsorptionKind.IAST, IastTemplate),
        (AdsorptionKind.QST, QstTemplate),
    ):
        execution = template_class(
            template_id=f"{kind.value}-execution",
            title="Raw execution",
            stage=EXECUTION_STAGE,
        )
        assessment = assess_parameter_completeness(execution)
        assert assessment.matched_rule_id == "R-ADS-P0"
        assert assessment.missing_parameters == ()
        analysis = template_class(
            template_id=f"{kind.value}-analysis",
            title="Analysis",
            stage=ANALYSIS_STAGE,
        )
        assert assess_parameter_completeness(analysis).matched_rule_id != "R-ADS-P0"
        assert assess_parameter_completeness(analysis).missing_parameters


def test_adsorption_analysis_required_sets_cover_all_analysis_kinds() -> None:
    """AC-02: every analysis kind declares its own required parameter set."""
    expected = {
        "bet": (
            "property",
            "model",
            "relative_pressure_min",
            "relative_pressure_max",
        ),
        "single_component": (
            "property",
            "model",
            "convergence_metric",
            "convergence_threshold",
            "statistical_uncertainty_metric",
            "sampling_validation",
        ),
        "iast": (
            "property",
            "adsorbate",
            "co_adsorbate",
            "composition_fraction",
            "temperature_K",
            "pressure_kPa",
            "model",
            "sampling_validation",
        ),
        "qst": (
            "property",
            "adsorbate",
            "temperature_low_K",
            "temperature_high_K",
            "reference_loading_mol_kg",
        ),
        "breakthrough": (
            "property",
            "criterion",
            "sampling_validation",
        ),
        "cycling_stability": (
            "property",
            "criterion",
            "reference_value",
            "tolerance",
        ),
    }
    for kind in AdsorptionKind:
        required = tuple(
            rule.required_parameters
            for rule in ADSORPTION_PARAMETER_RULES
            if rule.predicate(kind, AdsorptionStage.ANALYSIS)
            and rule.rule_id != "R-ADS-P0"
        )
        assert len(required) == 1, f"{kind.value} analysis must match one rule"
        assert required[0] == expected[kind.value], kind.value


def test_adsorption_execution_required_sets_cover_all_execution_kinds() -> None:
    """AC-01: every execution kind declares explicit condition inputs."""
    expected = {
        "bet": ("adsorbate", "temperature_K", "sample_mass_mg"),
        "single_component": ("adsorbate", "temperature_K", "pressure_kPa"),
        "iast": (),
        "qst": (),
        "breakthrough": (
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
        ),
        "cycling_stability": (
            "adsorbate",
            "temperature_K",
            "pressure_kPa",
            "cycle_count",
            "regeneration_protocol",
            "atmosphere",
        ),
    }
    for kind in AdsorptionKind:
        rule = next(
            candidate
            for candidate in ADSORPTION_PARAMETER_RULES
            if candidate.predicate(kind, AdsorptionStage.EXECUTION)
        )
        if rule.rule_id == "R-ADS-P0":
            assert expected[kind.value] == ()
        else:
            assert rule.required_parameters == expected[kind.value], kind.value


def test_adsorption_capture_disambiguates_execution_and_analysis() -> None:
    """AC-02: captures record the surface so the two are distinguishable."""
    execution = BetTemplate(
        template_id="bet-3-execution",
        title="BET isotherm acquisition",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
    )
    analysis = BetTemplate(
        template_id="bet-3-analysis",
        title="BET analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "surface_area",
            "model": "bet",
            "relative_pressure_min": 0.05,
            "relative_pressure_max": 0.3,
        },
    )
    execution_capture = capture_protocol(execution)
    analysis_capture = capture_protocol(analysis)
    assert execution_capture["stage"] == "execution"
    assert analysis_capture["stage"] == "analysis"
    assert execution_capture["kind"] == analysis_capture["kind"] == "bet"
    execution_names = [
        entry["parameter"] for entry in execution_capture["parameter_table"]
    ]
    analysis_names = [
        entry["parameter"] for entry in analysis_capture["parameter_table"]
    ]
    assert execution_names != analysis_names
