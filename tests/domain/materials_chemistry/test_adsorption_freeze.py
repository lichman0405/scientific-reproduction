"""DEV-M11-G03: freezing adsorption templates is Supervisor-only.

Templates RECORD the strict/recovery label (AC-01) and may be proposed by
Research, but freezing is a Supervisor-only decision gated by the frozen
role-action matrix of ``core.permissions`` (DEV-M6-G03): the freeze helper
is a pure function, nothing is ever frozen silently, and the input
template is never mutated. Because execution and analysis templates are
separate instances, each surface is frozen independently (AC-02).

Every test name contains "adsorption" (DEV-M11-G03 naming rule).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass
from typing import Any

import pytest

from scientific_reproduction.core.models import GoalTrack
from scientific_reproduction.core.permissions import (
    PermissionDeniedError,
    Role,
)
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    BetTemplate,
    SingleComponentTemplate,
    capture_protocol,
    freeze_adsorption_template,
)


def _setattr_through_instance(instance: object, name: str, value: object) -> None:
    """setattr through the dataclass __setattr__ (object-typed, ignore-free)."""
    setattr(instance, name, value)


@pytest.fixture
def strict_bet_execution_template() -> BetTemplate:
    """A strict-reproduction BET execution template, not yet frozen."""
    return BetTemplate(
        template_id="bet-1-isotherm-77",
        title="BET isotherm acquisition",
        stage=EXECUTION_STAGE,
        track=GoalTrack.STRICT_REPRODUCTION,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
    )


def test_adsorption_templates_are_proposed_unfrozen() -> None:
    """Nothing is frozen by construction: freezing is an explicit decision."""
    template = BetTemplate(
        template_id="bet-1", title="BET", stage=EXECUTION_STAGE
    )
    assert template.frozen is False
    assert capture_protocol(template)["frozen"] is False


def test_adsorption_supervisor_can_freeze_template(
    strict_bet_execution_template: BetTemplate,
) -> None:
    """The Supervisor may freeze via the plan-freeze action (R-PRM-SUP1)."""
    frozen = freeze_adsorption_template(
        strict_bet_execution_template, role=Role.SUPERVISOR
    )
    assert frozen.frozen is True
    assert frozen.template_id == strict_bet_execution_template.template_id
    # The input template is untouched: pure function, no silent mutation.
    assert strict_bet_execution_template.frozen is False


def test_adsorption_research_cannot_freeze_template(
    strict_bet_execution_template: BetTemplate,
) -> None:
    """Research is denied with the full permission assessment (R-PRM-D1)."""
    with pytest.raises(PermissionDeniedError) as exc_info:
        freeze_adsorption_template(
            strict_bet_execution_template, role=Role.RESEARCH
        )
    assessment = exc_info.value.assessment
    assert assessment.allowed is False
    assert str(exc_info.value)  # the message explains the Supervisor-only rule
    # The template remains unfrozen after the denial.
    assert strict_bet_execution_template.frozen is False


def test_adsorption_freeze_requires_a_frozen_role() -> None:
    """A non-Role argument is a TypeError, not a silent denial."""
    template = BetTemplate(
        template_id="bet-1", title="BET", stage=EXECUTION_STAGE
    )
    bad_role: Any = "supervisor"
    with pytest.raises(TypeError):
        freeze_adsorption_template(template, role=bad_role)


def test_adsorption_freeze_requires_a_template() -> None:
    """A non-template argument is a TypeError at the boundary."""
    bad_template: Any = "bet-1"
    with pytest.raises(TypeError):
        freeze_adsorption_template(bad_template, role=Role.SUPERVISOR)


def test_adsorption_frozen_copy_is_immutable(
    strict_bet_execution_template: BetTemplate,
) -> None:
    """A frozen template copy rejects any field mutation."""
    frozen = freeze_adsorption_template(
        strict_bet_execution_template, role=Role.SUPERVISOR
    )
    assert is_dataclass(frozen)
    for field_name in frozen.__dataclass_fields__:
        with pytest.raises(FrozenInstanceError):
            _setattr_through_instance(frozen, field_name, None)
    # The recorded parameter table is owned by the template: mutating a
    # caller dict after construction cannot leak into the template.
    parameters = dict(strict_bet_execution_template.parameters)
    template = BetTemplate(
        template_id="bet-copy",
        title="Copy",
        stage=EXECUTION_STAGE,
        parameters=parameters,
    )
    parameters["temperature_K"] = 87.4
    assert template.parameters["temperature_K"] == 77.4


def test_adsorption_frozen_flag_records_in_capture() -> None:
    """The freeze state is recorded in the deterministic protocol capture."""
    template = BetTemplate(
        template_id="bet-fz", title="Frozen", stage=EXECUTION_STAGE
    )
    frozen = freeze_adsorption_template(template, role=Role.SUPERVISOR)
    assert capture_protocol(frozen)["frozen"] is True
    assert capture_protocol(template)["frozen"] is False


def test_adsorption_analysis_surface_is_frozen_independently() -> None:
    """AC-02: the analysis surface has its own freeze state."""
    execution = SingleComponentTemplate(
        template_id="isotherm-2-exec",
        title="Isotherm execution",
        stage=EXECUTION_STAGE,
        parameters={"adsorbate": "propene"},
    )
    analysis = SingleComponentTemplate(
        template_id="isotherm-2-analysis",
        title="Isotherm analysis",
        stage=ANALYSIS_STAGE,
        parameters={"property": "uptake"},
    )
    frozen_execution = freeze_adsorption_template(
        execution, role=Role.SUPERVISOR
    )
    assert frozen_execution.frozen is True
    assert analysis.frozen is False
    assert capture_protocol(analysis)["frozen"] is False
    frozen_analysis = freeze_adsorption_template(analysis, role=Role.SUPERVISOR)
    assert frozen_analysis.frozen is True
    assert capture_protocol(frozen_execution)["frozen"] is True
