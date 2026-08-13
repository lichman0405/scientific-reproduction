"""DEV-M11-G01: freezing synthesis templates is Supervisor-only.

Templates RECORD the strict/recovery label (AC-01) and may be proposed by
Research, but freezing is a Supervisor-only decision gated by the frozen
role-action matrix of ``core.permissions`` (DEV-M6-G03): the freeze helper
is a pure function, nothing is ever frozen silently, and the input
template is never mutated.

Every test name contains "synth" (DEV-M11-G01 naming rule).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from scientific_reproduction.core.models import GoalTrack
from scientific_reproduction.core.permissions import (
    PermissionDeniedError,
    Role,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    SynthesisUnitProcessTemplate,
    capture_protocol,
    freeze_synthesis_template,
)


@pytest.fixture
def strict_synth_template() -> SynthesisUnitProcessTemplate:
    """A strict-reproduction synthesis template, not yet frozen."""
    return SynthesisUnitProcessTemplate(
        template_id="mof-1-thermal-90-adsorption",
        title="FDM-201 MOF solvothermal synthesis",
        track=GoalTrack.STRICT_REPRODUCTION,
        parameters={
            "metal_source": "zinc acetate dihydrate",
            "organic_linker": "PyBC",
            "solvent": "DMF",
            "temperature_K": 393.0,
            "duration_h": 72.0,
            "stoichiometry": 1.0,
        },
    )


def test_synth_templates_are_proposed_unfrozen() -> None:
    """Nothing is frozen by construction: freezing is an explicit decision."""
    template = SynthesisUnitProcessTemplate(
        template_id="ligand-1",
        title="Ligand synthesis",
    )
    assert template.frozen is False
    assert capture_protocol(template)["frozen"] is False


def test_synth_supervisor_can_freeze_template(
    strict_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """The Supervisor may freeze via the plan-freeze action (R-PRM-SUP1)."""
    frozen = freeze_synthesis_template(strict_synth_template, role=Role.SUPERVISOR)
    assert frozen.frozen is True
    assert frozen.template_id == strict_synth_template.template_id
    # The input template is untouched: pure function, no silent mutation.
    assert strict_synth_template.frozen is False


def test_synth_research_cannot_freeze_template(
    strict_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """Research is denied with the full permission assessment (R-PRM-D1)."""
    with pytest.raises(PermissionDeniedError) as exc_info:
        freeze_synthesis_template(strict_synth_template, role=Role.RESEARCH)
    assessment = exc_info.value.assessment
    assert assessment.allowed is False
    assert str(exc_info.value)  # the message explains the Supervisor-only rule
    # The template remains unfrozen after the denial.
    assert strict_synth_template.frozen is False


def test_synth_freeze_requires_a_frozen_role() -> None:
    """A non-Role argument is a TypeError, not a silent denial."""
    with pytest.raises(TypeError):
        freeze_synthesis_template(
            strict_synth_template, role="supervisor"  # type: ignore[arg-type]
        )


def test_synth_freeze_requires_a_template() -> None:
    """A non-template argument is a TypeError at the boundary."""
    with pytest.raises(TypeError):
        freeze_synthesis_template("mof-1", role=Role.SUPERVISOR)  # type: ignore[arg-type]


def test_synth_frozen_copy_is_immutable(
    strict_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """A frozen template copy rejects any field mutation."""
    frozen = freeze_synthesis_template(strict_synth_template, role=Role.SUPERVISOR)
    assert is_dataclass(frozen)
    for field_name in frozen.__dataclass_fields__:
        with pytest.raises(FrozenInstanceError):
            setattr(frozen, field_name, None)  # type: ignore[misc]
    # The recorded parameter table is owned by the template: mutating a
    # caller dict after construction cannot leak into the frozen template.
    parameters = dict(strict_synth_template.parameters)
    template = SynthesisUnitProcessTemplate(
        template_id="mof-copy",
        title="Copy",
        parameters=parameters,
    )
    parameters["temperature_K"] = 999.0
    assert template.parameters["temperature_K"] == 393.0


def test_synth_frozen_flag_records_in_capture() -> None:
    """The freeze state is recorded in the deterministic protocol capture."""
    template = SynthesisUnitProcessTemplate(template_id="mof-fz", title="Frozen")
    frozen = freeze_synthesis_template(template, role=Role.SUPERVISOR)
    assert capture_protocol(frozen)["frozen"] is True
    assert capture_protocol(template)["frozen"] is False
