"""DEV-M11-G02: analysis protocol / acceptance plans and their evaluation.

Covers AC-02: the analysis protocol and its acceptance criteria are pure
metadata on the template -- instance-data thresholds bound by universal
shape rules (``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS5: templates,
never universal thresholds) -- and are frozen separately from execution.
``evaluate_acceptance`` is a pure function of recorded measurement facts
and the plan's thresholds: the ordered ``ANALYSIS_ACCEPTANCE_RULES``
contract decides every criterion, and pending measurements are the input
of the Assumption Registry pathway, never a silent skip.

Every test name contains "character" (DEV-M11-G02 naming rule).
"""

from __future__ import annotations

from typing import Any

import pytest

from scientific_reproduction.core.models import Assumption
from scientific_reproduction.core.rules.assumptions import StrictLabel
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    ACCEPTANCE_PARAMETER_RULES,
    ACCEPTANCE_PARAMETERS,
    ANALYSIS_ACCEPTANCE_RULES,
    AcceptanceAssessment,
    AnalysisPlan,
    CharacterizationKind,
    CheckOutcome,
    InvalidAcceptanceCriteriaError,
    InvalidAnalysisPlanError,
    PXRDCharacterizationTemplate,
    SCXRDCharacterizationTemplate,
    SpectroscopyCharacterizationTemplate,
    TGACharacterizationTemplate,
    assumptions_for_missing_measurements,
    evaluate_acceptance,
)


def _pxrd_plan() -> AnalysisPlan:
    """A complete PXRD analysis plan (instance-data thresholds)."""
    return AnalysisPlan(
        protocol=(
            "background subtraction, peak search, phase identification"
            " against reference patterns"
        ),
        protocol_steps=(
            "background subtract",
            "search peaks",
            "match reference phases",
        ),
        acceptance_parameters={
            "pxrd_phase_score_min": 0.8,
            "pxrd_peak_tolerance_deg": 0.2,
            "pxrd_intensity_score_min": 0.7,
            "pxrd_batch_consistency_min": 0.7,
        },
    )


@pytest.fixture
def pxrd_character_template() -> PXRDCharacterizationTemplate:
    """A complete PXRD template with a full analysis plan."""
    return PXRDCharacterizationTemplate(
        template_id="pxrd-fdm201-activated-298k",
        title="FDM-201 activated PXRD",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 50.0,
            "step_size_deg": 0.02,
            "scan_temperature_K": 298.0,
        },
        analysis=_pxrd_plan(),
    )


@pytest.fixture
def passing_pxrd_measurements() -> dict[str, float]:
    """Recorded measurement facts that satisfy the plan's thresholds."""
    return {
        "reference_phase_score": 0.95,
        "max_peak_position_deviation_deg": 0.08,
        "intensity_pattern_score": 0.9,
        "batch_consistency_score": 0.93,
    }


# ---------------------------------------------------------------------------
# AnalysisPlan validation
# ---------------------------------------------------------------------------


def test_character_analysis_plan_validates_protocol_and_steps() -> None:
    """A plan records a non-empty protocol and ordered steps."""
    plan = AnalysisPlan(
        protocol="phase identification against reference patterns",
        protocol_steps=("search peaks", "match reference phases"),
        acceptance_parameters={"pxrd_phase_score_min": 0.8},
    )
    assert plan.protocol_steps == ("search peaks", "match reference phases")
    assert plan.acceptance_parameters == {"pxrd_phase_score_min": 0.8}
    assert plan.frozen is False


def test_character_analysis_plan_rejects_empty_protocol() -> None:
    """An empty protocol or empty step list is a stable value error."""
    with pytest.raises(InvalidAnalysisPlanError):
        AnalysisPlan(protocol="   ", protocol_steps=("a",))
    with pytest.raises(InvalidAnalysisPlanError):
        AnalysisPlan(protocol="protocol", protocol_steps=())
    with pytest.raises(InvalidAnalysisPlanError):
        AnalysisPlan(protocol="protocol", protocol_steps=("a", "   "))


def test_character_analysis_plan_rejects_unknown_acceptance_parameters() -> None:
    """Threshold names must come from the universal vocabulary."""
    with pytest.raises(InvalidAcceptanceCriteriaError, match="unknown acceptance"):
        AnalysisPlan(
            protocol="p",
            protocol_steps=("s",),
            acceptance_parameters={"peak_shift_tolerance": 0.1},
        )


@pytest.mark.parametrize(
    ("parameter", "bad_threshold"),
    [
        ("pxrd_phase_score_min", -0.1),       # R-CHA-AP1 (normalized score)
        ("pxrd_phase_score_min", 1.5),        # R-CHA-AP1
        ("pxrd_peak_tolerance_deg", 0),       # R-CHA-AP2
        ("pxrd_intensity_score_min", 1.01),   # R-CHA-AP3
        ("pxrd_batch_consistency_min", -0.5),  # R-CHA-AP4
        ("scxrd_r_factor_max", 0.0),          # R-CHA-AP5
        ("tga_mass_loss_tolerance_pct", -1.0),  # R-CHA-AP6
        ("spectroscopy_band_tolerance_cm_1", 0),  # R-CHA-AP7
    ],
)
def test_character_acceptance_parameter_rules_reject_bad_thresholds(
    parameter: str, bad_threshold: float
) -> None:
    """Every threshold parameter has exactly one universal shape rule."""
    with pytest.raises(InvalidAcceptanceCriteriaError, match=parameter):
        AnalysisPlan(
            protocol="p",
            protocol_steps=("s",),
            acceptance_parameters={parameter: bad_threshold},
        )


def test_character_acceptance_parameters_table_is_total() -> None:
    """The acceptance-parameter rules cover the vocabulary exactly."""
    ruled = {rule.parameter for rule in ACCEPTANCE_PARAMETER_RULES}
    assert ruled == ACCEPTANCE_PARAMETERS
    assert "pxrd_peak_tolerance_deg" in ACCEPTANCE_PARAMETERS


def test_character_analysis_plan_owns_its_threshold_table() -> None:
    """Mutating the caller's dict cannot leak into the frozen plan."""
    thresholds = {"pxrd_phase_score_min": 0.8}
    plan = AnalysisPlan(
        protocol="p",
        protocol_steps=("s",),
        acceptance_parameters=thresholds,
    )
    thresholds["pxrd_phase_score_min"] = 0.1
    assert plan.acceptance_parameters == {"pxrd_phase_score_min": 0.8}


def test_character_analysis_plan_type_boundaries_raise_type_error() -> None:
    """Non-string protocol and non-dict thresholds are TypeError."""
    bad_protocol: Any = 5
    bad_steps: Any = "one step"
    bad_thresholds: Any = ["pxrd_phase_score_min"]
    with pytest.raises(TypeError):
        AnalysisPlan(protocol=bad_protocol, protocol_steps=("s",))
    with pytest.raises(TypeError):
        AnalysisPlan(protocol="p", protocol_steps=bad_steps)
    with pytest.raises(TypeError):
        AnalysisPlan(protocol="p", protocol_steps=("s",), acceptance_parameters=bad_thresholds)


# ---------------------------------------------------------------------------
# evaluate_acceptance: the contract decides, never a worker (AC-02)
# ---------------------------------------------------------------------------


def test_character_acceptance_pass_when_contract_met(
    pxrd_character_template: PXRDCharacterizationTemplate,
    passing_pxrd_measurements: dict[str, float],
) -> None:
    """Recorded facts meeting the recorded thresholds pass the contract."""
    assessment = evaluate_acceptance(pxrd_character_template, passing_pxrd_measurements)
    assert isinstance(assessment, AcceptanceAssessment)
    assert assessment.outcome is CheckOutcome.PASS
    assert assessment.matched_rule_id == "R-CHA-O3"
    assert assessment.matched_item_id is None
    assert assessment.pending_measurements == ()
    assert assessment.plan_frozen is False
    assert assessment.kind is CharacterizationKind.PXRD


def test_character_acceptance_fail_when_contract_violated(
    pxrd_character_template: PXRDCharacterizationTemplate,
    passing_pxrd_measurements: dict[str, float],
) -> None:
    """A recorded fact outside the threshold fails by the contract."""
    measurements = dict(passing_pxrd_measurements)
    measurements["max_peak_position_deviation_deg"] = 0.5
    assessment = evaluate_acceptance(pxrd_character_template, measurements)
    assert assessment.outcome is CheckOutcome.FAIL
    assert assessment.matched_rule_id == "R-CHA-O1"
    assert assessment.matched_item_id == "R-CHA-A2"
    violated = [d for d in assessment.decisions if d.applied and not d.passed]
    assert [d.rule_id for d in violated] == ["R-CHA-A2"]
    assert "exceeds the recorded tolerance" in (violated[0].detail or "")


def test_character_acceptance_records_every_rule_decision(
    pxrd_character_template: PXRDCharacterizationTemplate,
    passing_pxrd_measurements: dict[str, float],
) -> None:
    """The assessment records the full audit trail of the rule table."""
    assessment = evaluate_acceptance(pxrd_character_template, passing_pxrd_measurements)
    assert len(assessment.decisions) == len(ANALYSIS_ACCEPTANCE_RULES)
    # The four PXRD rules are applied; the other kinds' rules are not.
    applied = [d for d in assessment.decisions if d.applied]
    assert len(applied) == 4
    assert [d.rule_id for d in applied] == [
        "R-CHA-A1",
        "R-CHA-A2",
        "R-CHA-A3",
        "R-CHA-A4",
    ]


def test_character_acceptance_pending_measurements_are_reported(
    pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """Missing measurement facts make the outcome PENDING, never a skip."""
    assessment = evaluate_acceptance(
        pxrd_character_template, {"reference_phase_score": 0.95}
    )
    assert assessment.outcome is CheckOutcome.PENDING
    assert assessment.matched_rule_id == "R-CHA-O2"
    assert assessment.matched_item_id == "R-CHA-A2"
    assert assessment.pending_measurements == (
        "batch_consistency_score",
        "intensity_pattern_score",
        "max_peak_position_deviation_deg",
    )


def test_character_acceptance_pending_measurements_route_to_assumptions(
    pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-02: pending measurements enter the real Assumption Registry."""
    routing = assumptions_for_missing_measurements(
        pxrd_character_template, {"reference_phase_score": 0.95}
    )
    assert routing.missing_measurements == (
        "batch_consistency_score",
        "intensity_pattern_score",
        "max_peak_position_deviation_deg",
    )
    assert len(routing.assumptions) == 3
    for entry in routing.assumptions:
        assert isinstance(entry, Assumption)
        assert entry.parameter in routing.missing_measurements
    # An A2 assumption disqualifies pure strict reproduction (R-STRICT-2).
    assert routing.strict_label_assessment.label is StrictLabel.NOT_STRICT
    assert routing.assumption_refs == tuple(
        entry.assumption_id for entry in routing.assumptions
    )


def test_character_acceptance_evaluates_every_kind() -> None:
    """SCXRD/TGA/spectroscopy acceptance contracts are pure functions too."""
    scxrd = SCXRDCharacterizationTemplate(
        template_id="scxrd-1",
        title="SCXRD",
        analysis=AnalysisPlan(
            protocol="structure solution and refinement",
            protocol_steps=("solve", "refine", "verify"),
            acceptance_parameters={"scxrd_r_factor_max": 0.05},
        ),
    )
    assert evaluate_acceptance(scxrd, {"reported_r_factor": 0.042}).outcome is CheckOutcome.PASS
    assert evaluate_acceptance(scxrd, {"reported_r_factor": 0.09}).outcome is CheckOutcome.FAIL

    tga = TGACharacterizationTemplate(
        template_id="tga-1",
        title="TGA",
        analysis=AnalysisPlan(
            protocol="mass-loss window comparison",
            protocol_steps=("record", "compare"),
            acceptance_parameters={"tga_mass_loss_tolerance_pct": 2.0},
        ),
    )
    measurements = {
        "observed_mass_loss_pct": 31.5,
        "reference_mass_loss_pct": 30.0,
    }
    assert evaluate_acceptance(tga, measurements).outcome is CheckOutcome.PASS
    out_of_window = {"observed_mass_loss_pct": 35.0, "reference_mass_loss_pct": 30.0}
    assert evaluate_acceptance(tga, out_of_window).outcome is CheckOutcome.FAIL

    spectra = SpectroscopyCharacterizationTemplate(
        template_id="spectra-1",
        title="Spectroscopy",
        analysis=AnalysisPlan(
            protocol="identity band comparison",
            protocol_steps=("locate bands", "compare positions"),
            acceptance_parameters={"spectroscopy_band_tolerance_cm_1": 5.0},
        ),
    )
    assert (
        evaluate_acceptance(spectra, {"max_band_position_deviation_cm_1": 2.0}).outcome
        is CheckOutcome.PASS
    )
    assert (
        evaluate_acceptance(spectra, {"max_band_position_deviation_cm_1": 12.0}).outcome
        is CheckOutcome.FAIL
    )


def test_character_acceptance_without_plan_is_pending_not_a_skip() -> None:
    """A template with no recorded analysis plan cannot be evaluated: PENDING."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-no-plan",
        title="No plan",
    )
    assessment = evaluate_acceptance(template, {"reference_phase_score": 0.95})
    assert assessment.outcome is CheckOutcome.PENDING
    assert assessment.plan_frozen is False
    assert len(assessment.pending_measurements) == 4


def test_character_acceptance_threshold_missing_on_plan_is_pending() -> None:
    """A rule whose threshold the plan does not record is PENDING, auditable."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-partial-plan",
        title="Partial plan",
        analysis=AnalysisPlan(
            protocol="peak-position agreement only",
            protocol_steps=("compare peaks",),
            acceptance_parameters={"pxrd_peak_tolerance_deg": 0.2},
        ),
    )
    assessment = evaluate_acceptance(
        template,
        {
            "reference_phase_score": 0.95,
            "max_peak_position_deviation_deg": 0.08,
            "intensity_pattern_score": 0.9,
            "batch_consistency_score": 0.93,
        },
    )
    assert assessment.outcome is CheckOutcome.PENDING
    assert assessment.matched_item_id == "R-CHA-A1"
    detail = next(
        d.detail for d in assessment.decisions if d.rule_id == "R-CHA-A1"
    )
    assert "pxrd_phase_score_min" in (detail or "")


def test_character_acceptance_is_deterministic(
    pxrd_character_template: PXRDCharacterizationTemplate,
    passing_pxrd_measurements: dict[str, float],
) -> None:
    """Same template + same facts -> identical assessments."""
    first = evaluate_acceptance(pxrd_character_template, passing_pxrd_measurements)
    second = evaluate_acceptance(pxrd_character_template, passing_pxrd_measurements)
    assert first == second
    assert first.decisions == second.decisions


def test_character_acceptance_type_boundaries_raise_type_error(
    pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """Non-dict measurements and non-template arguments are TypeError."""
    bad_measurements: Any = ["reference_phase_score"]
    bad_template: Any = "pxrd-1"
    with pytest.raises(TypeError):
        evaluate_acceptance(pxrd_character_template, bad_measurements)
    with pytest.raises(TypeError):
        evaluate_acceptance(bad_template, {"reference_phase_score": 0.95})


def test_character_plan_capture_records_analysis_shape(
    pxrd_character_template: PXRDCharacterizationTemplate,
) -> None:
    """The deterministic capture carries the full analysis plan."""
    from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
        capture_characterization,
    )

    capture = capture_characterization(pxrd_character_template)
    analysis = capture["analysis"]
    assert analysis["protocol"].startswith("background subtraction")
    assert analysis["protocol_steps"] == [
        "background subtract",
        "search peaks",
        "match reference phases",
    ]
    assert analysis["acceptance_parameters"] == {
        "pxrd_peak_tolerance_deg": 0.2,
        "pxrd_phase_score_min": 0.8,
        "pxrd_intensity_score_min": 0.7,
        "pxrd_batch_consistency_min": 0.7,
    }
    assert analysis["frozen"] is False
