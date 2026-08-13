"""DEV-M11-G01: independent-batch planning of synthesis templates (AC-01).

Covers the deterministic independent Runs of a template: batch run ids,
the frozen ``RunType.INDEPENDENT_REPLICATE`` label, the frozen ``n >= 3``
floor family (``BATCH_FLOOR_RULES``, ``DEFAULT_MIN_INDEPENDENT``) and the
below-floor request behavior.

Every test name contains "synth" (DEV-M11-G01 naming rule).
"""

from __future__ import annotations

import pytest

from scientific_reproduction.analysis.replication import DEFAULT_MIN_INDEPENDENT
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import GoalTrack, RunType
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    BATCH_FLOOR_RULES,
    INDEPENDENT_FLOOR,
    BatchReplicationDefaults,
    InvalidBatchPlanError,
    SynthesisUnitProcessKind,
    SynthesisUnitProcessTemplate,
    evaluate_batch_floor,
    plan_independent_batches,
)


@pytest.fixture
def mof_synth_template() -> SynthesisUnitProcessTemplate:
    """A complete FDM-201 MOF synthesis template (instance data only)."""
    return SynthesisUnitProcessTemplate(
        template_id="mof-1-thermal-90-adsorption",
        title="FDM-201 MOF solvothermal synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
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


def test_synth_plan_labels_independent_runs(
    mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """Every planned batch is an independent replicate Run (AC-01)."""
    plan = plan_independent_batches(mof_synth_template, n=3)
    assert plan.n == 3
    assert plan.run_type is RunType.INDEPENDENT_REPLICATE
    assert plan.track is GoalTrack.STRICT_REPRODUCTION
    assert len(plan.batch_run_ids) == 3


def test_synth_plan_run_ids_are_deterministic_and_safe(
    mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """Batch run ids are deterministic safe ids, distinct per batch."""
    first = plan_independent_batches(mof_synth_template, n=3)
    second = plan_independent_batches(mof_synth_template, n=3)
    assert first.batch_run_ids == second.batch_run_ids
    assert len(set(first.batch_run_ids)) == 3
    for run_id in first.batch_run_ids:
        assert is_valid_id(run_id, "run"), run_id


def test_synth_plan_carries_recovery_track_label() -> None:
    """A recovery-track template plans recovery-labeled independent Runs."""
    template = SynthesisUnitProcessTemplate(
        template_id="oxide-7-recovery",
        title="Oxide recovery synthesis",
        track=GoalTrack.RECOVERY,
    )
    plan = plan_independent_batches(template)
    assert plan.track is GoalTrack.RECOVERY
    assert plan.run_type is RunType.INDEPENDENT_REPLICATE


def test_synth_floor_default_is_frozen_family() -> None:
    """The default floor is the frozen n >= 3 family (AC-01)."""
    assert INDEPENDENT_FLOOR == DEFAULT_MIN_INDEPENDENT == 3
    assert BATCH_FLOOR_RULES[0].rule_id == "R-BF-1"
    assert BATCH_FLOOR_RULES[-1].rule_id == "R-BF-2"


def test_synth_floor_evaluation_records_every_rule_decision() -> None:
    """evaluate_batch_floor is total, deterministic and auditable."""
    assessment = evaluate_batch_floor(3, floor=3)
    assert assessment.sufficient is True
    assert assessment.requested_batches == 0
    assert assessment.matched_rule_id == "R-BF-1"
    assert len(assessment.decisions) == len(BATCH_FLOOR_RULES)
    # First match wins; the trailing total default also matches.
    matched = [d for d in assessment.decisions if d.matched]
    assert matched[0].rule_id == "R-BF-1"
    assert matched[-1].rule_id == "R-BF-2"


def test_synth_below_floor_plan_requests_batches_to_reach_floor() -> None:
    """A below-floor plan records the insufficiency and requests the gap."""
    assessment = evaluate_batch_floor(2, floor=3)
    assert assessment.sufficient is False
    assert assessment.requested_batches == 1
    assert assessment.matched_rule_id == "R-BF-2"


def test_synth_plan_below_floor_requests_additional_batches(
    mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """plan_independent_batches surfaces the floor decision on the plan."""
    plan = plan_independent_batches(mof_synth_template, n=2)
    assert plan.floor_assessment.sufficient is False
    assert plan.floor_assessment.requested_batches == 1
    assert plan.floor_assessment.matched_rule_id == "R-BF-2"


def test_synth_plan_respects_explicit_template_floor() -> None:
    """The floor comes from the template's replication defaults."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-floor-5",
        title="Explicit floor five",
        replication=BatchReplicationDefaults(minimum_n=5),
    )
    plan = plan_independent_batches(template, n=3)
    assert plan.floor_assessment.floor == 5
    assert plan.floor_assessment.sufficient is False
    assert plan.floor_assessment.requested_batches == 2
    assert len(plan.batch_run_ids) == 3


def test_synth_plan_at_explicit_floor_is_sufficient() -> None:
    """Planning exactly the explicit floor satisfies R-BF-1."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-floor-5",
        title="Explicit floor five",
        replication=BatchReplicationDefaults(minimum_n=5),
    )
    plan = plan_independent_batches(template, n=5)
    assert plan.floor_assessment.sufficient is True
    assert plan.floor_assessment.requested_batches == 0
    assert plan.floor_assessment.matched_rule_id == "R-BF-1"


@pytest.mark.parametrize("bad_n", [0, -3])
def test_synth_plan_rejects_batch_counts_below_one(bad_n: int) -> None:
    """A plan must plan at least one batch (stable error)."""
    with pytest.raises(InvalidBatchPlanError):
        evaluate_batch_floor(bad_n, floor=3)
    with pytest.raises(InvalidBatchPlanError):
        plan_independent_batches(
            SynthesisUnitProcessTemplate(template_id="t1", title="x"),
            n=bad_n,
        )


def test_synth_plan_rejects_non_int_batch_counts() -> None:
    """Non-int batch counts are TypeError at the boundary."""
    with pytest.raises(TypeError):
        evaluate_batch_floor(2.5, floor=3)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        plan_independent_batches(
            SynthesisUnitProcessTemplate(template_id="t1", title="x"),
            n="3",  # type: ignore[arg-type]
        )


def test_synth_plan_ids_are_template_scoped() -> None:
    """Different templates produce different run id sets (deterministic)."""
    a = SynthesisUnitProcessTemplate(template_id="mof-a", title="A")
    b = SynthesisUnitProcessTemplate(template_id="mof-b", title="B")
    plan_a = plan_independent_batches(a, n=3)
    plan_b = plan_independent_batches(b, n=3)
    assert plan_a.batch_run_ids != plan_b.batch_run_ids
    # Same template id, different batch index -- the run ids differ.
    assert len(set(plan_a.batch_run_ids)) == 3
