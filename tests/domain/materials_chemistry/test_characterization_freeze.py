"""DEV-M11-G02: analysis protocol/acceptance freezing is Supervisor-only.

AC-02: the analysis protocol and its acceptance criteria are frozen
separately from execution -- ``freeze_analysis_plan`` is a pure,
permission-gated metadata decision (``Action.PLAN_FREEZE`` through the
frozen role-action matrix of ``core.permissions``, DEV-M6-G03) that
requires NO execution artifacts, never mutates its input, and leaves the
template's data-capture freeze state independent. ``freeze_characterization_template``
mirrors the synthesis freeze on the template itself.

Every test name contains "character" (DEV-M11-G02 naming rule).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from typing import Any

import pytest

from scientific_reproduction.core.permissions import (
    PermissionDeniedError,
    Role,
)
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    AnalysisPlan,
    InvalidAnalysisPlanError,
    PXRDCharacterizationTemplate,
    capture_characterization,
    freeze_analysis_plan,
    freeze_characterization_template,
)


@pytest.fixture
def pxrd_freeze_template() -> PXRDCharacterizationTemplate:
    """A PXRD template with a recorded, unfrozen analysis plan."""
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
            protocol="phase identification against reference patterns",
            protocol_steps=("search peaks", "match reference phases"),
            acceptance_parameters={
                "pxrd_phase_score_min": 0.8,
                "pxrd_peak_tolerance_deg": 0.2,
                "pxrd_intensity_score_min": 0.7,
                "pxrd_batch_consistency_min": 0.7,
            },
        ),
    )


def test_character_plans_are_proposed_unfrozen(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """Nothing is frozen by construction: freezing is an explicit decision."""
    assert pxrd_freeze_template.analysis is not None
    assert pxrd_freeze_template.analysis.frozen is False
    assert pxrd_freeze_template.frozen is False
    capture = capture_characterization(pxrd_freeze_template)
    assert capture["analysis"]["frozen"] is False
    assert capture["frozen"] is False


def test_character_supervisor_can_freeze_analysis_plan(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-02: the Supervisor freezes protocol + acceptance (R-PRM-SUP1)."""
    frozen = freeze_analysis_plan(pxrd_freeze_template, role=Role.SUPERVISOR)
    assert frozen.analysis is not None
    assert pxrd_freeze_template.analysis is not None
    assert frozen.analysis.frozen is True
    assert frozen.analysis.protocol == pxrd_freeze_template.analysis.protocol
    assert frozen.analysis.acceptance_parameters == (
        pxrd_freeze_template.analysis.acceptance_parameters
    )
    # The input template is untouched: pure function, no silent mutation.
    assert pxrd_freeze_template.analysis.frozen is False


def test_character_analysis_plan_is_frozen_without_execution(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-02: freezing needs no measurements, no data, no execution.

    The protocol and acceptance criteria are pure metadata: the plan of a
    template with not a single recorded measurement fact or parameter
    value freezes identically.
    """
    bare = PXRDCharacterizationTemplate(
        template_id="pxrd-bare",
        title="Bare template",
        analysis=AnalysisPlan(
            protocol="phase identification against reference patterns",
            protocol_steps=("search peaks", "match reference phases"),
            acceptance_parameters={"pxrd_phase_score_min": 0.8},
        ),
    )
    frozen = freeze_analysis_plan(bare, role=Role.SUPERVISOR)
    assert frozen.analysis is not None
    assert frozen.analysis.frozen is True
    # Freezing the plan does not freeze the template's data capture.
    assert frozen.frozen is False


def test_character_analysis_plan_freeze_is_separate_from_template_freeze(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-02: plan freeze and template freeze are independent states."""
    plan_frozen = freeze_analysis_plan(pxrd_freeze_template, role=Role.SUPERVISOR)
    assert plan_frozen.analysis is not None
    assert plan_frozen.analysis.frozen is True
    assert plan_frozen.frozen is False

    template_frozen = freeze_characterization_template(
        pxrd_freeze_template, role=Role.SUPERVISOR
    )
    assert template_frozen.analysis is not None
    assert template_frozen.frozen is True
    assert template_frozen.analysis.frozen is False

    both = freeze_characterization_template(
        plan_frozen, role=Role.SUPERVISOR
    )
    assert both.analysis is not None
    assert both.frozen is True
    assert both.analysis.frozen is True


def test_character_research_cannot_freeze_analysis_plan(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """Research is denied with the full permission assessment (R-PRM-D1)."""
    with pytest.raises(PermissionDeniedError) as exc_info:
        freeze_analysis_plan(pxrd_freeze_template, role=Role.RESEARCH)
    assessment = exc_info.value.assessment
    assert assessment.allowed is False
    assert str(exc_info.value)
    # The plan remains unfrozen after the denial.
    assert pxrd_freeze_template.analysis is not None
    assert pxrd_freeze_template.analysis.frozen is False


def test_character_workers_cannot_freeze_analysis_plan(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """AC-02: no worker role may freeze protocol or acceptance."""
    for role in (
        Role.EXPERIMENT_WORKER,
        Role.COMPUTATION_WORKER,
        Role.ANALYSIS_WORKER,
        Role.DIAGNOSIS_WORKER,
    ):
        with pytest.raises(PermissionDeniedError):
            freeze_analysis_plan(pxrd_freeze_template, role=role)
    assert pxrd_freeze_template.analysis is not None
    assert pxrd_freeze_template.analysis.frozen is False


def test_character_freeze_requires_a_frozen_role(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """A non-Role argument is a TypeError, not a silent denial."""
    bad_role: Any = "supervisor"
    with pytest.raises(TypeError):
        freeze_analysis_plan(pxrd_freeze_template, role=bad_role)


def test_character_freeze_requires_a_template() -> None:
    """A non-template argument is a TypeError at the boundary."""
    bad_template: Any = "pxrd-1"
    with pytest.raises(TypeError):
        freeze_analysis_plan(bad_template, role=Role.SUPERVISOR)


def test_character_freeze_without_recorded_plan_is_a_stable_error() -> None:
    """A template with no analysis plan cannot freeze one (no placeholder)."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-no-plan",
        title="No plan",
    )
    with pytest.raises(InvalidAnalysisPlanError, match="not recorded"):
        freeze_analysis_plan(template, role=Role.SUPERVISOR)


def test_character_frozen_plan_copy_is_immutable(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """A frozen template copy rejects any field mutation."""
    frozen = freeze_analysis_plan(pxrd_freeze_template, role=Role.SUPERVISOR)
    assert is_dataclass(frozen)
    mutation_value: Any = None
    for field_name in frozen.__dataclass_fields__:
        with pytest.raises(FrozenInstanceError):
            setattr(frozen, field_name, mutation_value)


def test_character_freeze_state_records_in_capture(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """The freeze states are recorded in the deterministic capture."""
    plan_frozen = freeze_analysis_plan(pxrd_freeze_template, role=Role.SUPERVISOR)
    capture = capture_characterization(plan_frozen)
    assert capture["analysis"]["frozen"] is True
    assert capture["frozen"] is False
    both = freeze_characterization_template(plan_frozen, role=Role.SUPERVISOR)
    capture_both = capture_characterization(both)
    assert capture_both["analysis"]["frozen"] is True
    assert capture_both["frozen"] is True


def test_character_freeze_is_pure_and_deterministic(
    pxrd_freeze_template: PXRDCharacterizationTemplate,
) -> None:
    """Freezing twice yields byte-identical frozen copies."""
    first = freeze_analysis_plan(pxrd_freeze_template, role=Role.SUPERVISOR)
    second = freeze_analysis_plan(pxrd_freeze_template, role=Role.SUPERVISOR)
    assert first == second
    assert repr(first) == repr(second)
