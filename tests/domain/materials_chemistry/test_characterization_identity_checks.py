"""DEV-M11-G02: PXRD identity/quality checks as decision records (AC-03).

The PXRD identity/quality checks of ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md``
SS5 (peak-position agreement, phase identification, intensity-pattern
comparison with caution for preferred orientation, and batch consistency)
are REPRESENTED as metadata: the ordered ``PXRD_IDENTITY_CHECKS`` contract
table evaluated over recorded measurement facts and the plan's recorded
thresholds. ``evaluate_identity_checks`` returns a full decision record and
takes NO worker outcome input -- the outcome is decided by the frozen
contract, never a worker self-decision.

Every test name contains "character" (DEV-M11-G02 naming rule).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    CHECK_OUTCOME_RULES,
    PXRD_IDENTITY_CHECKS,
    AnalysisPlan,
    CharacterizationKind,
    CheckOutcome,
    IdentityCheckAssessment,
    InvalidCharacterizationTemplateError,
    PXRDCharacterizationTemplate,
    TGACharacterizationTemplate,
    evaluate_identity_checks,
)

PXRD_CHECK_IDS = ("R-CHA-A1", "R-CHA-A2", "R-CHA-A3", "R-CHA-A4")


@pytest.fixture
def pxrd_identity_template() -> PXRDCharacterizationTemplate:
    """A complete PXRD template with a full identity-check plan."""
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
        analysis=AnalysisPlan(
            protocol=(
                "background subtraction, peak search, phase identification"
                " against reference patterns"
            ),
            protocol_steps=(
                "background subtract",
                "search peaks",
                "match reference phases",
                "compare with reference intensity pattern",
                "compare across batches",
            ),
            acceptance_parameters={
                "pxrd_phase_score_min": 0.8,
                "pxrd_peak_tolerance_deg": 0.2,
                "pxrd_intensity_score_min": 0.7,
                "pxrd_batch_consistency_min": 0.7,
            },
        ),
    )


@pytest.fixture
def passing_identity_facts() -> dict[str, float]:
    """Measurement facts satisfying every identity check by the contract."""
    return {
        "reference_phase_score": 0.95,
        "max_peak_position_deviation_deg": 0.08,
        "intensity_pattern_score": 0.9,
        "batch_consistency_score": 0.93,
    }


def test_character_pxrd_identity_checks_cover_the_spec_criteria() -> None:
    """AC-03: the four spec PXRD checks are representable as metadata.

    16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5 PXRD: phase identification,
    peak-position agreement, intensity-pattern comparison with caution for
    preferred orientation, and batch consistency.
    """
    descriptions = "\n".join(check.description for check in PXRD_IDENTITY_CHECKS)
    assert [check.rule_id for check in PXRD_IDENTITY_CHECKS] == list(PXRD_CHECK_IDS)
    for token in (
        "phase identification",
        "peak-position agreement",
        "preferred orientation",
        "batch consistency",
    ):
        assert token in descriptions, token


def test_character_identity_checks_are_pxrd_only(
    pxrd_identity_template: PXRDCharacterizationTemplate,
) -> None:
    """Identity/quality checks are a PXRD contract, decided for PXRD only."""
    tga = TGACharacterizationTemplate(
        template_id="tga-1",
        title="TGA",
        parameters={},
    )
    with pytest.raises(InvalidCharacterizationTemplateError, match="PXRD contract"):
        evaluate_identity_checks(tga, {"reported_r_factor": 0.04})
    # The PXRD template evaluates fine.
    assessment = evaluate_identity_checks(
        pxrd_identity_template,
        {
            "reference_phase_score": 0.95,
            "max_peak_position_deviation_deg": 0.08,
            "intensity_pattern_score": 0.9,
            "batch_consistency_score": 0.93,
        },
    )
    assert assessment.kind is CharacterizationKind.PXRD


def test_character_identity_checks_have_no_worker_decision_input(
    pxrd_identity_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-03: the signature carries facts and a template -- no outcome.

    The outcome is never an argument: a worker cannot hand
    ``evaluate_identity_checks`` a pass/fail self-decision; the frozen
    contract decides from the recorded measurement facts and the plan's
    recorded thresholds.
    """
    signature = inspect.signature(evaluate_identity_checks)
    assert list(signature.parameters) == ["template", "measurements"]
    # The returned decision record carries the full trace.
    assessment = evaluate_identity_checks(
        pxrd_identity_template,
        {
            "reference_phase_score": 0.95,
            "max_peak_position_deviation_deg": 0.08,
            "intensity_pattern_score": 0.9,
            "batch_consistency_score": 0.93,
        },
    )
    assert isinstance(assessment, IdentityCheckAssessment)
    assert len(assessment.checks) == 4
    assert all(check.applied for check in assessment.checks)


def test_character_identity_checks_pass_when_contract_met(
    pxrd_identity_template: PXRDCharacterizationTemplate,
    passing_identity_facts: dict[str, float],
) -> None:
    """Facts inside every recorded threshold pass by the contract."""
    assessment = evaluate_identity_checks(pxrd_identity_template, passing_identity_facts)
    assert assessment.outcome is CheckOutcome.PASS
    assert assessment.matched_rule_id == "R-CHA-O3"
    assert assessment.matched_check_id is None
    assert assessment.pending_measurements == ()
    assert all(check.passed for check in assessment.checks)


def test_character_identity_checks_fail_by_contract_only(
    pxrd_identity_template: PXRDCharacterizationTemplate,
    passing_identity_facts: dict[str, float],
) -> None:
    """A fact outside a recorded threshold fails the deciding check (AC-03).

    The same recorded facts are judged by the recorded threshold -- the
    worker records the deviation, the contract decides the failure.
    """
    measurements = dict(passing_identity_facts)
    measurements["max_peak_position_deviation_deg"] = 0.35
    assessment = evaluate_identity_checks(pxrd_identity_template, measurements)
    assert assessment.outcome is CheckOutcome.FAIL
    assert assessment.matched_rule_id == "R-CHA-O1"
    assert assessment.matched_check_id == "R-CHA-A2"
    failed = [check for check in assessment.checks if check.outcome is CheckOutcome.FAIL]
    assert [check.check_id for check in failed] == ["R-CHA-A2"]
    assert "0.35" in (failed[0].detail or "")


def test_character_identity_checks_pending_without_measurements(
    pxrd_identity_template: PXRDCharacterizationTemplate,
) -> None:
    """Unrecorded facts are PENDING -- routed, never silently skipped.

    AC-03: a check that cannot be decided is never a worker judgment; it
    is a recorded PENDING state whose facts enter the Assumption Registry
    pathway.
    """
    assessment = evaluate_identity_checks(
        pxrd_identity_template, {"reference_phase_score": 0.95}
    )
    assert assessment.outcome is CheckOutcome.PENDING
    assert assessment.matched_rule_id == "R-CHA-O2"
    assert assessment.matched_check_id == "R-CHA-A2"
    assert assessment.pending_measurements == (
        "batch_consistency_score",
        "intensity_pattern_score",
        "max_peak_position_deviation_deg",
    )
    pending = [c for c in assessment.checks if c.outcome is CheckOutcome.PENDING]
    assert [c.check_id for c in pending] == ["R-CHA-A2", "R-CHA-A3", "R-CHA-A4"]


def test_character_identity_checks_fail_dominates_pending(
    pxrd_identity_template: PXRDCharacterizationTemplate,
    passing_identity_facts: dict[str, float],
) -> None:
    """A FAIL decides FAIL even when other checks are pending (R-CHA-O1)."""
    measurements = dict(passing_identity_facts)
    measurements["batch_consistency_score"] = 0.4
    del measurements["intensity_pattern_score"]
    assessment = evaluate_identity_checks(pxrd_identity_template, measurements)
    assert assessment.outcome is CheckOutcome.FAIL
    assert assessment.matched_rule_id == "R-CHA-O1"
    assert assessment.matched_check_id == "R-CHA-A4"
    assert assessment.pending_measurements == ("intensity_pattern_score",)


def test_character_identity_checks_decision_record_is_auditable(
    pxrd_identity_template: PXRDCharacterizationTemplate,
    passing_identity_facts: dict[str, float],
) -> None:
    """Every check decision and the deciding rules are recorded."""
    assessment = evaluate_identity_checks(pxrd_identity_template, passing_identity_facts)
    assert assessment.template_id == "pxrd-fdm201-activated-298k"
    assert [check.check_id for check in assessment.checks] == list(PXRD_CHECK_IDS)
    for check in assessment.checks:
        assert check.applied is True
        assert check.passed is True
        assert check.outcome is CheckOutcome.PASS
        assert check.detail is None
    # The outcome table's order is normative: FAIL, PENDING, then PASS.
    assert [rule.outcome for rule in CHECK_OUTCOME_RULES] == [
        CheckOutcome.FAIL,
        CheckOutcome.PENDING,
        CheckOutcome.PASS,
    ]


def test_character_identity_checks_are_deterministic(
    pxrd_identity_template: PXRDCharacterizationTemplate,
    passing_identity_facts: dict[str, float],
) -> None:
    """Same facts -> byte-identical decision records."""
    first = evaluate_identity_checks(pxrd_identity_template, passing_identity_facts)
    second = evaluate_identity_checks(pxrd_identity_template, passing_identity_facts)
    assert first == second
    assert repr(first) == repr(second)


def test_character_identity_checks_plan_freeze_state_recorded(
    pxrd_identity_template: PXRDCharacterizationTemplate,
    passing_identity_facts: dict[str, float],
) -> None:
    """The decision record carries the plan's freeze state (AC-02 audit)."""
    from scientific_reproduction.core.permissions import Role
    from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
        freeze_analysis_plan,
    )

    frozen = freeze_analysis_plan(pxrd_identity_template, role=Role.SUPERVISOR)
    assessment = evaluate_identity_checks(frozen, passing_identity_facts)
    assert assessment.plan_frozen is True
    unfrozen = evaluate_identity_checks(pxrd_identity_template, passing_identity_facts)
    assert unfrozen.plan_frozen is False


def test_character_identity_checks_type_boundaries_raise_type_error(
    pxrd_identity_template: PXRDCharacterizationTemplate,
) -> None:
    """Non-dict measurements and non-template arguments are TypeError."""
    bad_measurements: Any = ("reference_phase_score",)
    bad_template: Any = "pxrd-1"
    with pytest.raises(TypeError):
        evaluate_identity_checks(pxrd_identity_template, bad_measurements)
    with pytest.raises(TypeError):
        evaluate_identity_checks(bad_template, {"reference_phase_score": 0.95})


def test_character_identity_check_thresholds_are_instance_data() -> None:
    """The threshold VALUES live on the plan, never in the check rules.

    Two templates with the same facts and different recorded tolerances
    get different contract decisions -- the rules are templates, never
    universal thresholds (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5).
    """
    base = {
        "instrument": "diffractometer",
        "radiation_type": "Cu K-alpha",
        "wavelength_A": 1.5406,
        "two_theta_min_deg": 5.0,
        "two_theta_max_deg": 50.0,
        "step_size_deg": 0.02,
        "scan_temperature_K": 298.0,
    }
    strict = PXRDCharacterizationTemplate(
        template_id="pxrd-strict-tolerance",
        title="Strict tolerance",
        parameters=base,
        analysis=AnalysisPlan(
            protocol="peak-position agreement",
            protocol_steps=("compare peaks",),
            acceptance_parameters={
                "pxrd_phase_score_min": 0.8,
                "pxrd_peak_tolerance_deg": 0.1,
                "pxrd_intensity_score_min": 0.7,
                "pxrd_batch_consistency_min": 0.7,
            },
        ),
    )
    lenient = PXRDCharacterizationTemplate(
        template_id="pxrd-lenient-tolerance",
        title="Lenient tolerance",
        parameters=base,
        analysis=AnalysisPlan(
            protocol="peak-position agreement",
            protocol_steps=("compare peaks",),
            acceptance_parameters={
                "pxrd_phase_score_min": 0.8,
                "pxrd_peak_tolerance_deg": 0.5,
                "pxrd_intensity_score_min": 0.7,
                "pxrd_batch_consistency_min": 0.7,
            },
        ),
    )
    facts = {
        "reference_phase_score": 0.95,
        "max_peak_position_deviation_deg": 0.3,
        "intensity_pattern_score": 0.9,
        "batch_consistency_score": 0.93,
    }
    assert evaluate_identity_checks(strict, facts).outcome is CheckOutcome.FAIL
    assert evaluate_identity_checks(lenient, facts).outcome is CheckOutcome.PASS
