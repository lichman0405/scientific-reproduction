"""Plan v1 construction and frozen contracts (DEV-M4-G04).

Every test name contains "freeze" so ``python -m pytest -q
tests/planning -k freeze`` selects the whole suite. The ``ac0N`` sections
map one-to-one to the acceptance criteria of DEV-M4-G04:

* ``ac01`` -- the freeze is prohibited unless the completeness audit
  (evaluated from the *registered state at freeze time*) passes, and the
  prohibition names the offending item ids;
* ``ac02`` -- the frozen Plan and the frozen Goal/Acceptance/Analysis/
  Closure contracts are frozen dataclasses rejecting direct mutation,
  with freeze metadata and an immutable, no-clobber registry;
* ``ac03`` -- the versioned revision creates the next version with
  ``parent_plan_version``, preserves the old record untouched, and
  reports the old version ``SUPERSEDED`` via the versioned rule table
  (a computed lineage status, never a stored mutation).

The deterministic path mirrors ``inventory_helpers``: every fixture uses
fixed identities/timestamps (``FROZEN_AT``) so all freeze records are
deterministic. Helpers are imported read-only from ``inventory_helpers``.
"""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from inventory_helpers import init_project, make_item, make_requirement

from scientific_reproduction.audit.git import current_head
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    AuditStatus,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Confidence,
    DecisionMode,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    Plan,
    PlanInventoryAudit,
    PlanStatus,
    PrimaryOrExploratory,
    StatisticalDesign,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.audit import audit_inventory_registry
from scientific_reproduction.planning.freeze import (
    FreezeProhibitedError,
    GoalFamilyNotDraftError,
    PlanAlreadyFrozenError,
    PlanFreezeResult,
    PlanNotDraftError,
    PlanNotFrozenError,
    PlanStateMismatchError,
    UnresolvedContractReferenceError,
    freeze_plan,
    revise_plan,
)
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    PlanningError,
    ProjectNotInitializedError,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    SUPERSEDED_RULES,
    SUPERSEDED_RULESET_VERSION,
    DuplicateAcceptanceError,
    DuplicateAnalysisProtocolError,
    DuplicateClosureContractError,
    DuplicateGoalError,
    DuplicatePlanVersionError,
    DuplicateStatisticalDesignError,
    InvalidPlanIdError,
    InvalidPlanVersionError,
    InvalidRecordIdError,
    PlanError,
    PlanNotFoundError,
    PlanStatusInput,
    StatisticalDesignNotFoundError,
    build_plan_v1,
    evaluate_plan_status,
    formal_version,
    is_draft_version,
    is_formal_version,
    list_plans,
    list_statistical_designs,
    next_version,
    plan_lineage,
    read_acceptance,
    read_analysis_protocol,
    read_closure_contract,
    read_goal,
    read_plan,
    read_statistical_design,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
    register_plan,
    register_statistical_design,
)

#: Fixed freeze timestamp: every freeze in this suite is deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_goal(
    goal_id: str,
    *,
    requirement_ids: tuple[str, ...] = ("REQ-1",),
    acceptance_id: str = "ACC-1",
    analysis_id: str = "ANL-1",
    closure_id: str | None = "CLS-1",
) -> GoalContract:
    """Build a schema-valid draft goal contract (version ``v1-draft``)."""
    return GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported isotherm ({goal_id}).",
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the formally reported isotherm dataset.",
        requirement_ids=list(requirement_ids),
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref=acceptance_id, frozen=False),
        analysis_protocol_ref=analysis_id,
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref=closure_id,
    )


def make_acceptance(
    acceptance_id: str,
    *,
    goal_id: str = "GOAL-1",
    statistical_design_ref: str | None = None,
) -> AcceptanceCriteria:
    """Build a schema-valid draft acceptance record (version ``v1-draft``)."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=goal_id,
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[
            {
                "metric": "batch_level_uptake",
                "rule": "equivalence_interval",
            }
        ],
        target={
            "metric": "uptake_at_defined_pressure",
            "published_seed_value_cm3_g": 180.5,
        },
        confidence=Confidence.LOW,
        statistical_design_ref=statistical_design_ref,
    )


def make_analysis(analysis_id: str) -> AnalysisProtocolOrResult:
    """Build a schema-valid draft analysis protocol (version ``v1-draft``)."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[{"name": "isotherm_fit"}],
    )


def make_closure(closure_id: str) -> ClosureContract:
    """Build a schema-valid draft closure contract."""
    return ClosureContract(
        closure_id=closure_id,
        frozen=False,
        statistical_sufficiency={"min_valid_n": 3},
        execution_validity={"verified": True},
        diagnosis={"tolerances": {}},
        recovery=ClosureRecovery(),
        literature=ClosureLiterature(),
    )


def make_statistical_design(
    design_id: str, *, goal_id: str = "GOAL-1"
) -> StatisticalDesign:
    """Build a schema-valid draft statistical design (version ``v1-draft``)."""
    return StatisticalDesign(
        design_id=design_id,
        goal_id=goal_id,
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        metrics=["uptake_at_defined_pressure"],
        margin={"type": "equivalence_interval", "relative_pct": None},
        replication=GoalReplication(
            independent_required=True,
            minimum_n=3,
            planned_n_policy="dynamically_planned_n_with_minimum_3",
        ),
        primary_method="equivalence_test",
        alpha=0.05,
        confidence_level=0.95,
    )


def build_complete_workspace(root: Path) -> Path:
    """Initialize a project with a fully mapped, freeze-eligible state.

    Two formally reported items mapped to two requirements (one goal)
    and the full goal-contract family drafts: goal ``GOAL-1`` with
    acceptance ``ACC-1`` (referencing statistical design ``DESIGN-1``),
    analysis protocol ``ANL-1`` and closure contract ``CLS-1``.
    """
    init_project(root)
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-1",))
    )
    register_inventory_item(
        root, make_item("ITEM-2", requirement_ids=("REQ-2",))
    )
    register_requirement(
        root,
        make_requirement("REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-1",)),
    )
    register_requirement(
        root,
        make_requirement("REQ-2", inventory_items=("ITEM-2",), goal_ids=("GOAL-1",)),
    )
    register_goal(
        root, make_goal("GOAL-1", requirement_ids=("REQ-1", "REQ-2"))
    )
    register_statistical_design(root, make_statistical_design("DESIGN-1"))
    register_acceptance(
        root,
        make_acceptance("ACC-1", goal_id="GOAL-1", statistical_design_ref="DESIGN-1"),
    )
    register_analysis_protocol(root, make_analysis("ANL-1"))
    register_closure_contract(root, make_closure("CLS-1"))
    return root


def freeze_complete(root: Path) -> PlanFreezeResult:
    """Build and freeze the draft of a complete workspace deterministically."""
    return freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)


def _canonical(data: dict) -> str:
    """The registry's canonical JSON serialization."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# AC-01: the completeness audit gate (from the registered state)
# ---------------------------------------------------------------------------


def test_freeze_ac01_unmapped_item_blocks_freeze_naming_offenders(tmp_path):
    root = init_project(tmp_path)
    register_inventory_item(root, make_item("ITEM-1"))  # formal, unmapped
    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert exc.value.offending_item_ids == ("ITEM-1",)
    assert "ITEM-1" in str(exc.value)
    # AC-01: no record is written by a prohibited freeze.
    assert not (root / "plans" / "v1-draft.json").exists()
    assert not (root / "plans" / "v1.json").exists()


def test_freeze_ac01_ambiguous_item_blocks_freeze_naming_offenders(tmp_path):
    root = init_project(tmp_path)
    # A formal item referencing an unregistered requirement is AMBIGUOUS.
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-MISSING",))
    )
    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert exc.value.offending_item_ids == ("ITEM-1",)
    assert "ITEM-1" in str(exc.value)


def test_freeze_ac01_offender_ids_deterministic_and_sorted(tmp_path):
    root = init_project(tmp_path)
    register_inventory_item(root, make_item("ITEM-2"))
    register_inventory_item(root, make_item("ITEM-1"))
    register_inventory_item(
        root, make_item("ITEM-3", requirement_ids=("REQ-MISSING",))
    )
    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    # Union of unmapped and ambiguous ids, sorted by inventory id.
    assert exc.value.offending_item_ids == ("ITEM-1", "ITEM-2", "ITEM-3")


def test_freeze_ac01_failed_audit_writes_no_records(tmp_path):
    root = build_complete_workspace(tmp_path)
    register_inventory_item(root, make_item("ITEM-3"))  # unmapped
    with pytest.raises(FreezeProhibitedError):
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert not (root / "plans" / "v1-draft.json").exists()
    assert not (root / "plans" / "v1.json").exists()
    # Goal-family drafts are untouched by the failed freeze.
    stored = json.loads((root / "goals" / "GOAL-1.json").read_text(encoding="utf-8"))
    assert stored["frozen"] is False


def test_freeze_ac01_complete_mapping_allows_freeze(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert result.frozen_plan.status is PlanStatus.FROZEN
    assert result.frozen_plan.version == "v1"
    assert audit_inventory_registry(root).freeze_eligible


def test_freeze_ac01_audit_gate_recomputed_from_registered_state_not_snapshot(
    tmp_path,
):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    register_plan(root, draft)  # draft persists a PASS audit snapshot
    assert draft.inventory_audit.status is AuditStatus.PASS
    register_inventory_item(root, make_item("ITEM-3"))  # state changes
    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(root, draft, timestamp=FROZEN_AT)
    # The gate is recomputed from the registered state at freeze time;
    # the embedded snapshot is never trusted.
    assert exc.value.offending_item_ids == ("ITEM-3",)
    assert not (root / "plans" / "v1.json").exists()


def test_freeze_ac01_stale_plan_rejected_before_writes(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    register_inventory_item(root, make_item("ITEM-3"))  # state changed
    with pytest.raises(PlanStateMismatchError):
        freeze_plan(root, draft, timestamp=FROZEN_AT)
    assert not (root / "plans" / "v1-draft.json").exists()
    assert not (root / "plans" / "v1.json").exists()


def test_freeze_ac01_frozen_plan_embeds_recomputed_inventory_audit(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    fresh = audit_inventory_registry(root).plan_inventory_audit()
    assert result.frozen_plan.inventory_audit == fresh
    assert result.frozen_plan.inventory_audit.status is AuditStatus.PASS
    assert result.frozen_plan.inventory_audit.coverage == 1.0
    assert result.frozen_plan.inventory_audit.mapped_items == 2


def test_freeze_ac01_empty_inventory_freezes_vacuously(tmp_path):
    root = init_project(tmp_path)
    result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert result.frozen_plan.status is PlanStatus.FROZEN
    assert result.frozen_plan.goal_ids == []
    assert result.frozen_plan.requirement_ids == []
    assert result.goals == ()
    assert result.acceptance == ()
    assert result.statistical_designs == ()
    assert result.analysis_protocols == ()
    assert result.closure_contracts == ()


# ---------------------------------------------------------------------------
# AC-02: frozen contracts reject direct mutation
# ---------------------------------------------------------------------------


def test_freeze_ac02_plan_and_frozen_records_reject_direct_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    with pytest.raises(FrozenInstanceError):
        draft.status = PlanStatus.FROZEN  # type: ignore[misc]
    result = freeze_complete(root)
    with pytest.raises(FrozenInstanceError):
        result.frozen_plan.status = PlanStatus.DRAFT  # type: ignore[misc]
    stored = read_plan(root, "v1")
    with pytest.raises(FrozenInstanceError):
        stored.frozen_at = None  # type: ignore[misc]


def test_freeze_ac02_frozen_goal_contract_rejects_direct_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = freeze_complete(root).goals[0]
    assert goal.frozen is True
    with pytest.raises(FrozenInstanceError):
        goal.title = "tampered"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        goal.acceptance.frozen = False  # type: ignore[misc]


def test_freeze_ac02_frozen_acceptance_rejects_direct_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    acceptance = freeze_complete(root).acceptance[0]
    with pytest.raises(FrozenInstanceError):
        acceptance.criteria = []  # type: ignore[misc]


def test_freeze_ac02_frozen_analysis_protocol_rejects_direct_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    analysis = freeze_complete(root).analysis_protocols[0]
    with pytest.raises(FrozenInstanceError):
        analysis.frozen = False  # type: ignore[misc]


def test_freeze_ac02_frozen_closure_contract_rejects_direct_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    closure = freeze_complete(root).closure_contracts[0]
    with pytest.raises(FrozenInstanceError):
        closure.closure_allowed = True  # type: ignore[misc]


def test_freeze_ac02_frozen_statistical_design_rejects_direct_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    design = freeze_complete(root).statistical_designs[0]
    assert design.frozen is True
    with pytest.raises(FrozenInstanceError):
        design.metrics = []  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        design.replication.minimum_n = 5  # type: ignore[misc]


def test_freeze_ac02_frozen_plan_carries_freeze_metadata(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    plan = result.frozen_plan
    assert plan.status is PlanStatus.FROZEN
    assert plan.version == "v1"
    assert plan.plan_id == build_plan_v1(root).plan_id
    assert plan.parent_plan_version is None
    assert plan.frozen_at == "2026-06-01T00:00:00Z"
    assert plan.frozen_commit == current_head(root)
    assert plan.frozen_commit is not None  # workspace is a Git repository
    assert result.frozen_at == plan.frozen_at
    assert result.frozen_commit == plan.frozen_commit


def test_freeze_ac02_frozen_goal_family_carries_freeze_metadata(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    goal = result.goals[0]
    assert goal.frozen is True
    assert goal.version == "v1"
    assert goal.frozen_at == "2026-06-01T00:00:00Z"
    assert goal.frozen_commit == result.frozen_commit
    assert goal.acceptance.frozen is True
    assert goal.acceptance.criteria_ref == "ACC-1"
    assert result.acceptance[0].frozen is True
    assert result.acceptance[0].version == "v1"
    assert result.acceptance[0].statistical_design_ref == "DESIGN-1"
    assert result.statistical_designs[0].frozen is True
    assert result.statistical_designs[0].version == "v1"
    assert result.statistical_designs[0].design_id == "DESIGN-1"
    assert result.analysis_protocols[0].frozen is True
    assert result.analysis_protocols[0].protocol_version == "v1"
    assert result.closure_contracts[0].frozen is True


def test_freeze_ac02_freeze_persists_draft_and_frozen_records(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    draft_path = root / "plans" / "v1-draft.json"
    frozen_path = root / "plans" / "v1.json"
    assert draft_path.is_file()
    assert frozen_path.is_file()
    assert json.loads(draft_path.read_text(encoding="utf-8")) == build_plan_v1(
        root
    ).to_dict()
    assert json.loads(frozen_path.read_text(encoding="utf-8")) == result.frozen_plan.to_dict()


def test_freeze_ac02_freeze_tolerates_equal_registered_draft(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    register_plan(root, draft)
    result = freeze_plan(root, draft, timestamp=FROZEN_AT)  # no error
    assert result.frozen_plan.version == "v1"


def test_freeze_ac02_freeze_rejects_conflicting_registered_draft(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    register_plan(root, draft)
    conflicting = replace(draft, goal_ids=["GOAL-9"])
    (root / "plans" / "v1-draft.json").write_text(
        _canonical(conflicting.to_dict()), encoding="utf-8"
    )
    with pytest.raises(PlanStateMismatchError):
        freeze_plan(root, draft, timestamp=FROZEN_AT)


def test_freeze_ac02_freeze_rejects_second_freeze_of_same_version(tmp_path):
    root = build_complete_workspace(tmp_path)
    freeze_complete(root)
    with pytest.raises(PlanAlreadyFrozenError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert "v1" in str(exc.value)


def test_freeze_ac02_freeze_rejects_non_draft_plan(tmp_path):
    root = build_complete_workspace(tmp_path)
    frozen = freeze_complete(root).frozen_plan
    with pytest.raises(PlanNotDraftError):
        freeze_plan(root, frozen, timestamp=FROZEN_AT)


def test_freeze_ac02_freeze_rejects_formal_draft_version(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = replace(build_plan_v1(root), version="v1")
    with pytest.raises(InvalidPlanVersionError):
        freeze_plan(root, draft, timestamp=FROZEN_AT)


def test_freeze_ac02_frozen_goal_family_persisted_in_state(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    assert result.goals[0].frozen is True
    # The registered goal record IS the frozen contract after the freeze:
    # any state reader sees the same frozen variant the freeze returned
    # (an unfrozen draft on disk would make the freeze invisible to
    # workers reading the goal record from state).
    stored_goal = read_goal(root, "GOAL-1")
    assert stored_goal.frozen is True
    assert stored_goal.version == "v1"
    assert stored_goal.frozen_at == result.frozen_at
    assert stored_goal.frozen_commit == result.frozen_commit
    assert stored_goal.acceptance.frozen is True
    assert stored_goal == result.goals[0]
    raw = json.loads((root / "goals" / "GOAL-1.json").read_text(encoding="utf-8"))
    assert raw["frozen"] is True
    assert raw["version"] == "v1"
    assert raw["frozen_at"] == result.frozen_at


def test_freeze_ac02_freeze_persists_the_whole_frozen_goal_family(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    acceptance = read_acceptance(root, "ACC-1")
    assert acceptance == result.acceptance[0]
    assert acceptance.frozen is True
    assert acceptance.version == "v1"
    analysis = read_analysis_protocol(root, "ANL-1")
    assert analysis == result.analysis_protocols[0]
    assert analysis.frozen is True
    assert analysis.protocol_version == "v1"
    closure = read_closure_contract(root, "CLS-1")
    assert closure == result.closure_contracts[0]
    assert closure.frozen is True
    raw = json.loads(
        (root / "acceptance" / "ACC-1.json").read_text(encoding="utf-8")
    )
    assert raw["frozen"] is True
    assert raw["version"] == "v1"
    raw = json.loads(
        (root / "protocols" / "ANL-1.json").read_text(encoding="utf-8")
    )
    assert raw["frozen"] is True
    assert raw["protocol_version"] == "v1"
    raw = json.loads(
        (root / "closure" / "CLS-1.json").read_text(encoding="utf-8")
    )
    assert raw["frozen"] is True


def test_freeze_ac02_register_api_stays_exactly_once_after_freeze(tmp_path):
    root = build_complete_workspace(tmp_path)
    freeze_complete(root)
    # The freeze transitioned the registered records in place; the public
    # register API keeps its exactly-once contract (no re-registration).
    with pytest.raises(DuplicateGoalError):
        register_goal(
            root, make_goal("GOAL-1", requirement_ids=("REQ-1", "REQ-2"))
        )
    with pytest.raises(DuplicateAcceptanceError):
        register_acceptance(root, make_acceptance("ACC-1", goal_id="GOAL-1"))
    with pytest.raises(DuplicateAnalysisProtocolError):
        register_analysis_protocol(root, make_analysis("ANL-1"))
    with pytest.raises(DuplicateClosureContractError):
        register_closure_contract(root, make_closure("CLS-1"))


def test_freeze_ac02_plan_registry_no_clobber(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    register_plan(root, draft)
    with pytest.raises(DuplicatePlanVersionError) as exc:
        register_plan(root, build_plan_v1(root))
    assert "already registered" in str(exc.value)


def test_freeze_ac02_goal_family_registries_no_clobber(tmp_path):
    root = init_project(tmp_path)
    register_goal(root, make_goal("GOAL-1", requirement_ids=("REQ-1",)))
    with pytest.raises(DuplicateGoalError):
        register_goal(root, make_goal("GOAL-1", requirement_ids=("REQ-1",)))
    register_acceptance(root, make_acceptance("ACC-1", goal_id="GOAL-1"))
    with pytest.raises(DuplicateAcceptanceError):
        register_acceptance(root, make_acceptance("ACC-1", goal_id="GOAL-1"))
    register_analysis_protocol(root, make_analysis("ANL-1"))
    with pytest.raises(DuplicateAnalysisProtocolError):
        register_analysis_protocol(root, make_analysis("ANL-1"))
    register_closure_contract(root, make_closure("CLS-1"))
    with pytest.raises(DuplicateClosureContractError):
        register_closure_contract(root, make_closure("CLS-1"))
    register_statistical_design(root, make_statistical_design("DESIGN-1"))
    with pytest.raises(DuplicateStatisticalDesignError):
        register_statistical_design(root, make_statistical_design("DESIGN-1"))


# ---------------------------------------------------------------------------
# AC-03: versioned revision preserves the old record untouched
# ---------------------------------------------------------------------------


def test_freeze_ac03_revision_creates_new_version_with_parent_link(tmp_path):
    root = build_complete_workspace(tmp_path)
    frozen = freeze_complete(root).frozen_plan
    revised = revise_plan(root, frozen)
    assert revised.version == "v2-draft"
    assert revised.parent_plan_version == "v1"
    assert revised.status is PlanStatus.DRAFT
    assert revised.plan_id == frozen.plan_id
    assert revised.goal_ids == frozen.goal_ids
    assert revised.requirement_ids == frozen.requirement_ids
    assert revised.work_packages == frozen.work_packages
    assert revised.resource_ids == frozen.resource_ids
    assert revised.frozen_at is None
    assert revised.frozen_commit is None


def test_freeze_ac03_revision_preserves_old_record_untouched(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    frozen_path = root / "plans" / "v1.json"
    before = frozen_path.read_bytes()
    revise_plan(root, result.frozen_plan)
    assert frozen_path.read_bytes() == before
    assert read_plan(root, "v1") == result.frozen_plan
    assert read_plan(root, "v1").status is PlanStatus.FROZEN
    assert [p.version for p in list_plans(root)] == [
        "v1-draft",
        "v1",
        "v2-draft",
    ]


def test_freeze_ac03_old_plan_reported_superseded_in_lineage(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    revise_plan(root, result.frozen_plan)
    entries = plan_lineage(root)
    assert [e.plan.version for e in entries] == ["v1-draft", "v1", "v2-draft"]
    assert [e.status for e in entries] == [
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
    ]
    old, new = entries[1], entries[2]
    assert old.assessment.matched_rule_id == "R-SUP-P1"
    assert new.assessment.matched_rule_id == "R-SUP-D1"
    # The stored record was never rewritten: SUPERSEDED is a computed
    # lineage status, not a stored mutation.
    assert read_plan(root, "v1").status is PlanStatus.FROZEN


def test_freeze_ac03_lineage_shows_frozen_until_revision(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    entries = plan_lineage(root)
    assert [e.plan.version for e in entries] == ["v1-draft", "v1"]
    assert [e.status for e in entries] == [PlanStatus.DRAFT, PlanStatus.FROZEN]
    assert entries[1].assessment.matched_rule_id == "R-SUP-F1"
    assert entries[1].plan == result.frozen_plan


def test_freeze_ac03_revision_requires_registered_frozen_plan(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)  # never registered
    with pytest.raises(PlanNotFoundError):
        revise_plan(root, draft)
    register_plan(root, draft)
    with pytest.raises(PlanNotFrozenError) as exc:
        revise_plan(root, draft)
    assert "FROZEN" in str(exc.value)


def test_freeze_ac03_revision_rejects_stale_plan_object(tmp_path):
    root = build_complete_workspace(tmp_path)
    frozen = freeze_complete(root).frozen_plan
    tampered = replace(frozen, goal_ids=[])
    with pytest.raises(PlanStateMismatchError):
        revise_plan(root, tampered)


def test_freeze_ac03_revision_of_revision_extends_lineage(tmp_path):
    root = build_complete_workspace(tmp_path)
    v1 = freeze_complete(root).frozen_plan
    v2_draft = revise_plan(root, v1)
    v2 = freeze_plan(root, v2_draft, timestamp=FROZEN_AT).frozen_plan
    assert v2.version == "v2"
    assert v2.parent_plan_version == "v1"
    v3_draft = revise_plan(root, v2)
    assert v3_draft.version == "v3-draft"
    assert v3_draft.parent_plan_version == "v2"
    entries = plan_lineage(root)
    assert [e.plan.version for e in entries] == [
        "v1-draft",
        "v1",
        "v2-draft",
        "v2",
        "v3-draft",
    ]
    assert [e.status for e in entries] == [
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
    ]
    assert read_plan(root, "v1") == v1  # bytes untouched across revisions


def test_freeze_ac03_revision_reopens_goal_family_as_drafts(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = freeze_complete(root)
    revised = revise_plan(root, result.frozen_plan)
    assert revised.version == "v2-draft"
    # The family is re-opened as drafts of the next version: the frozen
    # content is the authoring baseline, freeze metadata is cleared.
    goal = read_goal(root, "GOAL-1")
    assert goal.frozen is False
    assert goal.version == "v2-draft"
    assert goal.frozen_at is None
    assert goal.frozen_commit is None
    assert goal.acceptance.frozen is False
    assert goal.title == result.goals[0].title
    assert goal.analysis_protocol_ref == result.goals[0].analysis_protocol_ref
    acceptance = read_acceptance(root, "ACC-1")
    assert acceptance.frozen is False
    assert acceptance.version == "v2-draft"
    assert acceptance.criteria == result.acceptance[0].criteria
    analysis = read_analysis_protocol(root, "ANL-1")
    assert analysis.frozen is False
    assert analysis.protocol_version == "v2-draft"
    closure = read_closure_contract(root, "CLS-1")
    assert closure.frozen is False


def test_freeze_ac03_each_freeze_persists_its_own_frozen_family(tmp_path):
    root = build_complete_workspace(tmp_path)
    v1 = freeze_complete(root).frozen_plan
    v2_draft = revise_plan(root, v1)
    v2 = freeze_plan(root, v2_draft, timestamp=FROZEN_AT).frozen_plan
    assert v2.version == "v2"
    # The second freeze persists its own frozen family (v2, its stamp).
    goal = read_goal(root, "GOAL-1")
    assert goal.frozen is True
    assert goal.version == "v2"
    assert goal.frozen_at == v2.frozen_at
    assert goal.frozen_commit == v2.frozen_commit
    assert goal.acceptance.frozen is True
    assert read_acceptance(root, "ACC-1").version == "v2"
    assert read_analysis_protocol(root, "ANL-1").protocol_version == "v2"
    # The next revision re-opens the family again for the next version.
    v3_draft = revise_plan(root, v2)
    assert v3_draft.version == "v3-draft"
    goal = read_goal(root, "GOAL-1")
    assert goal.frozen is False
    assert goal.version == "v3-draft"


def test_freeze_ac03_revision_recomputes_audit_from_state(tmp_path):
    root = build_complete_workspace(tmp_path)
    frozen = freeze_complete(root).frozen_plan
    register_inventory_item(root, make_item("ITEM-3", requirement_ids=("REQ-3",)))
    register_requirement(
        root,
        make_requirement(
            "REQ-3", inventory_items=("ITEM-3",), goal_ids=("GOAL-1",)
        ),
    )
    revised = revise_plan(root, frozen)
    assert revised.inventory_audit.mapped_items == 3
    assert revised.inventory_audit.status is AuditStatus.PASS
    # Content baseline is copied from the frozen plan.
    assert revised.requirement_ids == ["REQ-1", "REQ-2"]


def test_freeze_ac03_revision_rejects_duplicate_next_version(tmp_path):
    root = build_complete_workspace(tmp_path)
    frozen = freeze_complete(root).frozen_plan
    next_draft = replace(frozen, version="v2-draft", status=PlanStatus.DRAFT)
    register_plan(root, next_draft)
    with pytest.raises(DuplicatePlanVersionError):
        revise_plan(root, frozen)


def test_freeze_ac03_frozen_plan_deterministic_across_workspaces(tmp_path):
    root_a = build_complete_workspace(tmp_path / "a")
    root_b = build_complete_workspace(tmp_path / "b")
    frozen_a = freeze_complete(root_a).frozen_plan
    frozen_b = freeze_complete(root_b).frozen_plan
    assert frozen_a == frozen_b
    assert frozen_a.frozen_commit == frozen_b.frozen_commit
    assert frozen_a.to_dict() == frozen_b.to_dict()


# ---------------------------------------------------------------------------
# Paradigm: purity, determinism, rule table, boundaries, error hierarchy
# ---------------------------------------------------------------------------


def test_freeze_plan_builder_deterministic_same_state_same_plan(tmp_path):
    root = build_complete_workspace(tmp_path)
    first = build_plan_v1(root)
    second = build_plan_v1(root)
    assert first == second
    assert first.to_dict() == second.to_dict()


def test_freeze_plan_builder_independent_of_registration_order(tmp_path):
    root_a = init_project(tmp_path / "a")
    register_inventory_item(root_a, make_item("ITEM-1", requirement_ids=("REQ-1",)))
    register_inventory_item(root_a, make_item("ITEM-2", requirement_ids=("REQ-2",)))
    register_requirement(
        root_a,
        make_requirement(
            "REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-1",)
        ),
    )
    register_requirement(
        root_a,
        make_requirement(
            "REQ-2", inventory_items=("ITEM-2",), goal_ids=("GOAL-1",)
        ),
    )
    root_b = init_project(tmp_path / "b")
    # Requirements must be registered after the items they map
    # (UnresolvedItemReferenceError), so root_b only varies the relative
    # registration order of items and of requirements.
    register_inventory_item(root_b, make_item("ITEM-2", requirement_ids=("REQ-2",)))
    register_inventory_item(root_b, make_item("ITEM-1", requirement_ids=("REQ-1",)))
    register_requirement(
        root_b,
        make_requirement(
            "REQ-2", inventory_items=("ITEM-2",), goal_ids=("GOAL-1",)
        ),
    )
    register_requirement(
        root_b,
        make_requirement(
            "REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-1",)
        ),
    )
    assert build_plan_v1(root_a).to_dict() == build_plan_v1(root_b).to_dict()


def test_freeze_plan_builder_goal_ids_sorted_distinct_union(tmp_path):
    root = init_project(tmp_path)
    register_inventory_item(root, make_item("ITEM-1", requirement_ids=("REQ-1",)))
    register_inventory_item(root, make_item("ITEM-2", requirement_ids=("REQ-2",)))
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("ITEM-1",),
            goal_ids=("GOAL-2", "GOAL-1"),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-2", inventory_items=("ITEM-2",), goal_ids=("GOAL-1",)
        ),
    )
    plan = build_plan_v1(root)
    assert plan.goal_ids == ["GOAL-1", "GOAL-2"]
    assert plan.requirement_ids == ["REQ-1", "REQ-2"]
    assert plan.inventory_audit.mapped_items == 2


def test_freeze_plan_v1_draft_shape_work_packages_and_resources_empty(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    assert draft.version == INITIAL_PLAN_VERSION
    assert draft.status is PlanStatus.DRAFT
    assert draft.work_packages == []
    assert draft.resource_ids == []
    assert draft.inventory_audit.status is AuditStatus.PASS
    assert draft.inventory_audit.coverage == 1.0
    assert draft.inventory_audit.formally_reported_items == 2


def test_freeze_plan_plan_id_deterministic_and_stable_across_versions(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    assert draft.plan_id.startswith("sr_plan_")
    frozen = freeze_complete(root).frozen_plan
    revised = revise_plan(root, frozen)
    assert draft.plan_id == frozen.plan_id == revised.plan_id


def test_freeze_plan_version_helpers_draft_formal_next():
    assert is_draft_version("v1-draft") is True
    assert is_draft_version("v1") is False
    assert is_formal_version("v1") is True
    assert is_formal_version("v1-draft") is False
    assert formal_version("v1-draft") == "v1"
    assert formal_version("v1") == "v1"
    assert next_version("v1") == "v2"
    assert next_version("v9") == "v10"
    with pytest.raises(InvalidPlanVersionError):
        next_version("v1-draft")
    with pytest.raises(InvalidPlanVersionError):
        is_draft_version("v1.0")
    with pytest.raises(InvalidPlanVersionError):
        formal_version("plan")


def test_freeze_plan_status_ruleset_versioned_and_total():
    assert SUPERSEDED_RULESET_VERSION == "1.0"
    assert [r.rule_id for r in SUPERSEDED_RULES] == [
        "R-SUP-D1",
        "R-SUP-S1",
        "R-SUP-P1",
        "R-SUP-F1",
    ]
    # The trailing rule is a total default: it matches every input.
    default = SUPERSEDED_RULES[-1]
    assert default.rule_id == "R-SUP-F1"
    assert default.predicate(PlanStatusInput(PlanStatus.FROZEN, True)) is True
    assert default.predicate(PlanStatusInput(PlanStatus.DRAFT, False)) is True


def test_freeze_plan_status_first_match_wins():
    draft_with_newer = evaluate_plan_status(PlanStatus.DRAFT, True)
    assert draft_with_newer.status is PlanStatus.DRAFT
    assert draft_with_newer.matched_rule_id == "R-SUP-D1"
    superseded = evaluate_plan_status(PlanStatus.FROZEN, True)
    assert superseded.status is PlanStatus.SUPERSEDED
    assert superseded.matched_rule_id == "R-SUP-P1"
    still_frozen = evaluate_plan_status(PlanStatus.FROZEN, False)
    assert still_frozen.status is PlanStatus.FROZEN
    assert still_frozen.matched_rule_id == "R-SUP-F1"


def test_freeze_plan_status_assessment_records_decision_trace():
    assessment = evaluate_plan_status(PlanStatus.FROZEN, True)
    assert len(assessment.decisions) == len(SUPERSEDED_RULES)
    assert [d.rule_id for d in assessment.decisions] == [
        r.rule_id for r in SUPERSEDED_RULES
    ]
    assert [d.matched for d in assessment.decisions] == [
        False,
        False,
        True,
        True,
    ]
    assert assessment.input == PlanStatusInput(PlanStatus.FROZEN, True)
    assert assessment.ruleset_version == SUPERSEDED_RULESET_VERSION
    assert assessment.status is PlanStatus.SUPERSEDED


def test_freeze_plan_type_error_boundaries(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        build_plan_v1(123)
    with pytest.raises(TypeError):
        register_plan(root, 5)
    with pytest.raises(TypeError):
        read_plan(root, 5)
    with pytest.raises(TypeError):
        list_plans(3)
    with pytest.raises(TypeError):
        register_goal(root, "not-a-goal")
    with pytest.raises(TypeError):
        read_goal(root, 7)
    with pytest.raises(TypeError):
        is_draft_version(5)
    with pytest.raises(TypeError):
        next_version(None)
    with pytest.raises(TypeError):
        evaluate_plan_status("FROZEN", False)
    with pytest.raises(TypeError):
        evaluate_plan_status(PlanStatus.FROZEN, "yes")
    with pytest.raises(TypeError):
        freeze_plan(root, "not-a-plan")
    with pytest.raises(TypeError):
        freeze_plan(root, build_plan_v1(root), timestamp="2026-06-01")
    with pytest.raises(TypeError):
        revise_plan(root, {"version": "v1"})
    # Naive timestamps are rejected (ValueError), like planning/init.
    with pytest.raises(ValueError):
        freeze_plan(
            root, build_plan_v1(root), timestamp=datetime(2026, 1, 1)
        )


def test_freeze_plan_error_hierarchy_and_stable_messages(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(PlanNotFoundError) as first:
        read_plan(root, "v1")
    with pytest.raises(PlanNotFoundError) as second:
        read_plan(root, "v1")
    assert str(first.value) == str(second.value)
    assert isinstance(first.value, PlanError)
    assert isinstance(first.value, PlanningError)
    assert isinstance(first.value, ValueError)
    for error_type in (
        FreezeProhibitedError,
        PlanNotDraftError,
        PlanStateMismatchError,
        PlanAlreadyFrozenError,
        PlanNotFrozenError,
        UnresolvedContractReferenceError,
        GoalFamilyNotDraftError,
        DuplicatePlanVersionError,
        DuplicateStatisticalDesignError,
        StatisticalDesignNotFoundError,
        InvalidPlanVersionError,
        InvalidPlanIdError,
        InvalidRecordIdError,
    ):
        assert issubclass(error_type, ValueError)


def test_freeze_plan_requires_initialized_project(tmp_path):
    bare = tmp_path / "bare"
    minimal = Plan(
        plan_id="sr_plan_0000",
        version=INITIAL_PLAN_VERSION,
        status=PlanStatus.DRAFT,
        inventory_audit=PlanInventoryAudit(
            formally_reported_items=0,
            mapped_items=0,
            unmapped_items=0,
            ambiguous_items=0,
            coverage=0.0,
        ),
        goal_ids=[],
        requirement_ids=[],
    )
    with pytest.raises(ProjectNotInitializedError):
        build_plan_v1(bare)
    with pytest.raises(ProjectNotInitializedError):
        register_plan(bare, minimal)
    with pytest.raises(ProjectNotInitializedError):
        read_plan(bare, "v1")
    with pytest.raises(ProjectNotInitializedError):
        list_plans(bare)
    with pytest.raises(ProjectNotInitializedError):
        freeze_plan(bare, minimal, timestamp=FROZEN_AT)
    with pytest.raises(ProjectNotInitializedError):
        revise_plan(bare, minimal)


def test_freeze_plan_corrupt_registry_records_rejected(tmp_path):
    root = init_project(tmp_path)
    plans_dir = root / "plans"  # created by initialize_project
    (plans_dir / "v1-draft.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt plan record"):
        read_plan(root, "v1-draft")
    (plans_dir / "v1-draft.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt plan record"):
        list_plans(root)


def test_freeze_plan_rejects_unsafe_ids_and_versions(tmp_path):
    root = init_project(tmp_path)
    draft = build_plan_v1(root)
    with pytest.raises(InvalidPlanVersionError):
        register_plan(root, replace(draft, version="plan-v1"))
    with pytest.raises(InvalidPlanVersionError):
        register_plan(root, replace(draft, version="v"))
    with pytest.raises(InvalidPlanIdError):
        register_plan(root, replace(draft, plan_id="../escape"))
    with pytest.raises(InvalidPlanIdError):
        register_plan(root, replace(draft, plan_id="a/b"))
    with pytest.raises(InvalidRecordIdError):
        register_goal(
            root, replace(make_goal("GOAL-1"), goal_id="a/b")
        )


def test_freeze_plan_records_pass_their_schemas(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    validate_and_reject("plan", draft.to_dict())
    result = freeze_complete(root)
    validate_and_reject("plan", result.frozen_plan.to_dict())
    validate_and_reject("goal", result.goals[0].to_dict())
    validate_and_reject("acceptance-criteria", result.acceptance[0].to_dict())
    validate_and_reject("statistical-design", result.statistical_designs[0].to_dict())
    validate_and_reject("analysis", result.analysis_protocols[0].to_dict())
    validate_and_reject("closure-contract", result.closure_contracts[0].to_dict())
    revised = revise_plan(root, result.frozen_plan)
    validate_and_reject("plan", revised.to_dict())


def test_freeze_plan_registry_canonical_json_and_deterministic_listing(tmp_path):
    root = build_complete_workspace(tmp_path)
    draft = build_plan_v1(root)
    register_plan(root, draft)
    raw = (root / "plans" / "v1-draft.json").read_text(encoding="utf-8")
    assert raw == _canonical(draft.to_dict())
    assert json.loads(raw) == draft.to_dict()
    assert list_plans(root) == (draft,)


def test_freeze_plan_unresolved_goal_refs_block_freeze(tmp_path):
    root = build_complete_workspace(tmp_path)
    # A second registered goal with a dangling analysis protocol ref.
    register_goal(
        root,
        make_goal(
            "GOAL-2", requirement_ids=("REQ-1",), analysis_id="ANL-MISSING"
        ),
    )
    with pytest.raises(UnresolvedContractReferenceError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert "GOAL-2" in str(exc.value)
    assert "ANL-MISSING" in str(exc.value)
    assert not (root / "plans" / "v1-draft.json").exists()
    assert not (root / "plans" / "v1.json").exists()


def test_freeze_plan_unresolved_design_ref_blocks_freeze(tmp_path):
    root = build_complete_workspace(tmp_path)
    # An acceptance naming a statistical design that was never registered.
    register_acceptance(
        root,
        make_acceptance(
            "ACC-2", goal_id="GOAL-1", statistical_design_ref="DESIGN-MISSING"
        ),
    )
    with pytest.raises(UnresolvedContractReferenceError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert "ACC-2" in str(exc.value)
    assert "DESIGN-MISSING" in str(exc.value)
    assert not (root / "plans" / "v1-draft.json").exists()
    assert not (root / "plans" / "v1.json").exists()


def test_freeze_plan_statistical_design_registry_read_and_list(tmp_path):
    root = build_complete_workspace(tmp_path)
    design = read_statistical_design(root, "DESIGN-1")
    assert design.design_id == "DESIGN-1"
    assert design.goal_id == "GOAL-1"
    assert design.version == INITIAL_PLAN_VERSION
    assert design.frozen is False
    assert design.primary_method == "equivalence_test"
    assert list_statistical_designs(root) == (design,)
    with pytest.raises(StatisticalDesignNotFoundError):
        read_statistical_design(root, "DESIGN-MISSING")


def test_freeze_plan_unregistered_plan_goal_blocks_freeze(tmp_path):
    root = init_project(tmp_path)
    register_inventory_item(root, make_item("ITEM-1", requirement_ids=("REQ-1",)))
    register_requirement(
        root, make_requirement("REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-2",))
    )
    # The audit passes (item mapped) but the plan references a goal
    # contract that is not registered.
    with pytest.raises(UnresolvedContractReferenceError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert "GOAL-2" in str(exc.value)


def test_freeze_plan_already_frozen_goal_family_blocks_freeze(tmp_path):
    root = build_complete_workspace(tmp_path)
    frozen_goal = replace(
        make_goal("GOAL-2", requirement_ids=("REQ-1",)), frozen=True
    )
    register_goal(root, frozen_goal)
    with pytest.raises(GoalFamilyNotDraftError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    assert "GOAL-2" in str(exc.value)


def test_freeze_plan_frozen_commit_none_outside_git_repository(tmp_path):
    root = build_complete_workspace(tmp_path)
    # Suspend the Git repository (rename, not delete: Git object files are
    # read-only on Windows) so current_head reports "not a repository".
    os.rename(root / ".git", root / ".git-suspended")
    try:
        result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    finally:
        os.rename(root / ".git-suspended", root / ".git")
    assert result.frozen_commit is None
    assert result.frozen_plan.frozen_commit is None
    assert result.frozen_plan.frozen_at == "2026-06-01T00:00:00Z"
