"""Integration: the M4 planning primitives compose into one end-to-end flow
(DEV-M4-G06).

Goal contract DEV-M4-G06 (frozen, verbatim):
  * goal_id: DEV-M4-G06
  * milestone: M4
  * title: Run planning primitive integration acceptance
  * objective: Integrate initialization, inventory, audit, freeze and DAG
    behavior on synthetic fixture.
  * AC-01: Incomplete synthetic inventory fails freeze.
  * AC-02: Complete inventory produces frozen Plan v1.
  * AC-03: Frozen plan can be reloaded from filesystem state and audited.

The unit suites (tests/planning/) prove each layer alone; this module runs
the whole pipeline on one synthetic fixture, end to end, with the REAL
modules (nothing is stubbed or mocked):

* initialization -- ``planning.init.initialize_project`` creates the
  workspace tree, the ``project.yaml`` state record, and the Git audit
  checkpoint (DEV-M4-G01);
* inventory -- ``planning.inventory`` registers the item/requirement
  registry records (DEV-M4-G02);
* audit -- ``planning.audit.audit_inventory_registry`` recomputes the
  completeness verdict from the registered state (DEV-M4-G03);
* freeze -- ``planning.plan.build_plan_v1`` + ``planning.freeze.freeze_plan``
  gate the freeze on the audit and persist the frozen Plan v1
  (DEV-M4-G04);
* DAG -- ``planning.dag.build_plan_dag`` exports the frozen plan's
  semantic DAG: dependency edges with gate kinds, deterministic
  topological order and the resource blocker mapping (DEV-M4-G05).

The synthetic fixture is built by one deterministic helper chain
(``build_incomplete_workspace`` -> ``complete_inventory``), so AC-01 and
AC-02 exercise the *same* flow: the incomplete workspace is the same one
that later freezes after its inventory is completed. Every test uses fixed
timestamps and identities -- no wall clock, no randomness, no network.

Every test name contains "planning" (``test_planning_flow_*``) so the goal
verification command ``python -m pytest -q tests/planning tests/integration``
selects the whole suite.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity, current_head
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    AuditStatus,
    AvailabilityState,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Confidence,
    Criticality,
    DecisionMode,
    DependencyType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    InventoryItemType,
    MappingStatus,
    PlanStatus,
    PrimaryOrExploratory,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    Resource,
    ResourceType,
)
from scientific_reproduction.planning.audit import audit_inventory_registry
from scientific_reproduction.planning.dag import (
    DAGNode,
    GateKind,
    PlanningDAG,
    build_plan_dag,
)
from scientific_reproduction.planning.freeze import (
    FreezeProhibitedError,
    PlanFreezeResult,
    freeze_plan,
)
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    PlanningError,
    initialize_project,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    build_plan_v1,
    plan_lineage,
    read_goal,
    read_plan,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
    register_plan,
)
from scientific_reproduction.planning.resources import register_resource

#: Deterministic author/committer identity used by every init behind the
#: planning fixtures (mirrors ``tests/planning/inventory_helpers.py``).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed init timestamp for the project state/event/git records.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp: every freeze in this suite is deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: The exact ISO text the fixed freeze timestamp formats to.
FROZEN_AT_ISO = "2026-06-01T00:00:00Z"

#: Primary target DOI used to initialize test projects
#: (``17-FDM201-REFERENCE-CASE.md``).
DOI = "10.1039/D5TA00771B"


# ---------------------------------------------------------------------------
# Synthetic fixture builders (frozen model helpers, mirroring the
# tests/planning helpers -- no new state types)
# ---------------------------------------------------------------------------


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_item(
    inventory_id: str,
    *,
    source_id: str = "SRC-TARGET-PAPER",
    item_type: InventoryItemType = InventoryItemType.EXPERIMENT,
    formal_report: bool = True,
    description: str = "Single-component C3H6 adsorption isotherm for FDM-201 at 298 K.",
    source_location: str | None = "main adsorption figure, 'Adsorption isotherms' section",
    requirement_ids: tuple[str, ...] = (),
    mapping_status: MappingStatus = MappingStatus.UNMAPPED,
    ambiguity_notes: str | None = None,
) -> ReproductionInventoryItem:
    """Build a frozen ReproductionInventoryItem with compact defaults."""
    return ReproductionInventoryItem(
        inventory_id=inventory_id,
        source_id=source_id,
        item_type=item_type,
        formal_report=formal_report,
        description=description,
        mapping_status=mapping_status,
        source_location=source_location,
        requirement_ids=list(requirement_ids),
        ambiguity_notes=ambiguity_notes,
    )


def make_requirement(
    requirement_id: str,
    *,
    statement: str = "Reproduce the reported single-component adsorption isotherm.",
    inventory_items: tuple[str, ...] = (),
    goal_ids: tuple[str, ...] = (),
    criticality: Criticality = Criticality.REQUIRED,
    outcome: RequirementOutcome = RequirementOutcome.OPEN,
) -> ReproductionRequirement:
    """Build a frozen ReproductionRequirement with compact defaults."""
    return ReproductionRequirement(
        requirement_id=requirement_id,
        statement=statement,
        inventory_items=list(inventory_items),
        criticality=criticality,
        goal_ids=list(goal_ids),
        outcome=outcome,
    )


def make_goal(
    goal_id: str,
    *,
    requirement_ids: tuple[str, ...] = ("REQ-1",),
    dependencies: tuple[GoalDependency, ...] = (),
    resource_ids: tuple[str, ...] = (),
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
        dependencies=list(dependencies),
        acceptance=GoalAcceptance(criteria_ref=acceptance_id, frozen=False),
        analysis_protocol_ref=analysis_id,
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref=closure_id,
        resource_ids=list(resource_ids),
    )


def make_acceptance(
    acceptance_id: str, *, goal_id: str = "GOAL-1"
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


def make_resource(
    resource_id: str,
    availability_state: AvailabilityState,
    *,
    blocks_goal_ids: tuple[str, ...] = (),
    name: str | None = None,
) -> Resource:
    """Build a frozen Resource with compact defaults."""
    return Resource(
        resource_id=resource_id,
        name=name or f"resource {resource_id}",
        resource_type=ResourceType.REAGENT,
        availability_state=availability_state,
        blocks_goal_ids=list(blocks_goal_ids),
    )


def build_incomplete_workspace(root: Path) -> Path:
    """The synthetic INCOMPLETE fixture: a project whose inventory holds one
    formally reported item whose requirement is not yet registered.

    ``ITEM-1`` is formal and references requirement ``REQ-1``, but the
    requirement authoring is incomplete -- no requirement record exists.
    The completeness audit rules the item AMBIGUOUS (R-MAP-A1: an
    unresolved mapping cannot be decided) and FAILs (R-AUD-A1): exactly
    the incomplete state AC-01 freezes against. The completion step
    (``complete_inventory``) registers the missing requirements on the
    same workspace, resolving the reference.
    """
    init_project(root)
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-1",))
    )
    return root


def complete_inventory(root: Path) -> Path:
    """Complete the synthetic fixture: map every formal item to a goal.

    Registers the missing inventory item, the requirements mapping both
    formal items to goal ``GOAL-1`` (resolving ``ITEM-1``'s previously
    unresolvable ``REQ-1`` reference), and the full goal-contract family
    drafts (goal, acceptance criteria, analysis protocol, closure
    contract) that the freeze consumes (01-PRODUCT-REQUIREMENTS.md SS5
    steps 7-8). After this step every formal item is MAPPED (R-MAP-M1)
    and the audit PASSes (R-AUD-P1).
    """
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
    register_goal(root, make_goal("GOAL-1", requirement_ids=("REQ-1", "REQ-2")))
    register_acceptance(root, make_acceptance("ACC-1", goal_id="GOAL-1"))
    register_analysis_protocol(root, make_analysis("ANL-1"))
    register_closure_contract(root, make_closure("CLS-1"))
    return root


def build_complete_workspace(root: Path) -> Path:
    """The synthetic COMPLETE fixture: the incomplete workspace completed.

    ``complete_inventory`` runs on the same workspace
    ``build_incomplete_workspace`` created, so AC-02 exercises the same
    flow AC-01 rejected.
    """
    return complete_inventory(build_incomplete_workspace(root))


def freeze_complete(root: Path) -> PlanFreezeResult:
    """Build and freeze the draft of a complete workspace deterministically."""
    return freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)


def build_dag_workspace(root: Path) -> Path:
    """The synthetic DAG fixture: a complete workspace with two goals, one
    hard dependency edge and one procured (gap) resource.

    ``GOAL-1`` requires resource ``RES-1`` (PROCURE -- a gap); ``GOAL-2``
    hard-depends on ``GOAL-1`` (execution gate) and requires ``RES-2``
    (AVAILABLE -- never a blocker). The draft plan carries both resource
    ids, so the frozen Plan v1 exports a semantic DAG: one edge, a
    dependency-first topological order, one blocked goal node and one
    plan-level resource gap.
    """
    init_project(root)
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-1", "REQ-2"))
    )
    register_requirement(
        root,
        make_requirement("REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-1",)),
    )
    register_requirement(
        root,
        make_requirement("REQ-2", inventory_items=("ITEM-1",), goal_ids=("GOAL-2",)),
    )
    register_goal(
        root,
        make_goal(
            "GOAL-1",
            requirement_ids=("REQ-1",),
            resource_ids=("RES-1",),
            acceptance_id="ACC-1",
            closure_id="CLS-1",
        ),
    )
    register_goal(
        root,
        make_goal(
            "GOAL-2",
            requirement_ids=("REQ-2",),
            dependencies=(
                GoalDependency(
                    goal_id="GOAL-1",
                    type=DependencyType.HARD_GATE,
                    execution_gate=True,
                    acceptance_gate=False,
                ),
            ),
            resource_ids=("RES-2",),
            acceptance_id="ACC-2",
            closure_id="CLS-2",
        ),
    )
    register_acceptance(root, make_acceptance("ACC-1", goal_id="GOAL-1"))
    register_acceptance(root, make_acceptance("ACC-2", goal_id="GOAL-2"))
    register_analysis_protocol(root, make_analysis("ANL-1"))
    register_closure_contract(root, make_closure("CLS-1"))
    register_closure_contract(root, make_closure("CLS-2"))
    register_resource(
        root,
        make_resource(
            "RES-1", AvailabilityState.PROCURE, blocks_goal_ids=("GOAL-1",)
        ),
    )
    register_resource(
        root,
        make_resource(
            "RES-2", AvailabilityState.AVAILABLE, blocks_goal_ids=("GOAL-2",)
        ),
    )
    draft = dataclasses.replace(
        build_plan_v1(root), resource_ids=["RES-1", "RES-2"]
    )
    register_plan(root, draft)
    freeze_plan(root, draft, timestamp=FROZEN_AT)
    return root


def node_by_id(dag: PlanningDAG, goal_id: str) -> DAGNode:
    """The DAG node of ``goal_id`` (test convenience)."""
    for node in dag.nodes:
        if node.goal.goal_id == goal_id:
            return node
    raise AssertionError(f"no node for goal {goal_id!r} in the DAG")


# ---------------------------------------------------------------------------
# AC-01: incomplete synthetic inventory fails freeze
# ---------------------------------------------------------------------------


def test_planning_flow_ac01_incomplete_inventory_fails_freeze(tmp_path) -> None:
    """AC-01: the real freeze rejects the incomplete synthetic fixture.

    The incomplete workspace holds one formally reported item whose
    requirement is not registered, so the real completeness audit
    (recomputed from the registered state) rules it AMBIGUOUS and FAILs
    with the item as offender; the real freeze raises
    ``FreezeProhibitedError`` -- the freeze error of
    ``planning.freeze.freeze_plan`` -- naming the offending item id, and
    writes no plan record.
    """
    root = build_incomplete_workspace(tmp_path)

    # The real audit gate: recomputed from the registered state.
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.FAIL
    assert not audit.freeze_eligible
    assert audit.offending_item_ids == ("ITEM-1",)
    assert audit.matched_rule_id == "R-AUD-A1"

    with pytest.raises(FreezeProhibitedError) as exc:
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)

    # The real freeze error: a planning error (ValueError subclass) with
    # the offending item ids structurally and in the stable message.
    assert isinstance(exc.value, PlanningError)
    assert isinstance(exc.value, ValueError)
    assert exc.value.offending_item_ids == ("ITEM-1",)
    assert "ITEM-1" in str(exc.value)

    # A failed freeze writes no plan records (draft or frozen).
    assert not (root / "plans" / "v1-draft.json").exists()
    assert not (root / "plans" / "v1.json").exists()


# ---------------------------------------------------------------------------
# AC-02: complete inventory produces frozen Plan v1
# ---------------------------------------------------------------------------


def test_planning_flow_ac02_complete_inventory_produces_frozen_plan_v1(
    tmp_path,
) -> None:
    """AC-02: completing the same synthetic fixture freezes Plan v1.

    The flow starts from the very workspace AC-01 rejected: the first
    freeze attempt raises the real ``FreezeProhibitedError``; after the
    inventory is completed through the fixture's completion step, the
    same flow (``build_plan_v1`` -> ``freeze_plan``) persists the frozen
    Plan v1 record (``plans/v1.json``) with the freeze stamp, a PASS
    inventory audit and the real pre-freeze git HEAD as ``frozen_commit``.
    """
    root = build_incomplete_workspace(tmp_path)

    # Same flow as AC-01: still fails while the inventory is incomplete.
    with pytest.raises(FreezeProhibitedError):
        freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)

    # Complete the inventory on the same workspace.
    complete_inventory(root)

    # The same flow now produces the frozen Plan v1.
    result = freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)
    frozen = result.frozen_plan
    assert frozen.version == "v1"
    assert frozen.status is PlanStatus.FROZEN
    assert frozen.frozen_at == FROZEN_AT_ISO
    # The workspace is a real Git repository: the freeze records the
    # pre-freeze HEAD (the freeze itself never writes Git state).
    assert frozen.frozen_commit == current_head(root)

    # The frozen record embeds the recomputed PASS inventory audit.
    assert frozen.inventory_audit.status is AuditStatus.PASS
    assert frozen.inventory_audit.formally_reported_items == 2
    assert frozen.inventory_audit.mapped_items == 2
    assert frozen.inventory_audit.unmapped_items == 0
    assert frozen.inventory_audit.ambiguous_items == 0
    assert frozen.inventory_audit.coverage == 1.0

    # The Plan v1 record is persisted on the filesystem at plans/v1.json.
    v1_path = root / "plans" / "v1.json"
    assert v1_path.is_file()
    record = json.loads(v1_path.read_text(encoding="utf-8"))
    assert record["version"] == "v1"
    assert record["status"] == "FROZEN"
    assert record["frozen_at"] == FROZEN_AT_ISO
    # The draft record is written alongside and stays a draft.
    draft = read_plan(root, "v1-draft")
    assert draft.status is PlanStatus.DRAFT
    # The goal-contract drafts are never rewritten by the freeze.
    goal_record = json.loads(
        (root / "goals" / "GOAL-1.json").read_text(encoding="utf-8")
    )
    assert goal_record["frozen"] is False


# ---------------------------------------------------------------------------
# AC-03: reload from filesystem state and audit
# ---------------------------------------------------------------------------


def test_planning_flow_ac03_frozen_plan_reloaded_from_filesystem_and_audited(
    tmp_path,
) -> None:
    """AC-03: the frozen Plan v1 reloads from the workspace and audits.

    The frozen plan is re-read from the persisted filesystem state
    (``read_plan`` on a fresh call -- nothing is reused from the freeze
    result in memory), the persisted record's embedded ``inventory_audit``
    sub-object exists with a PASS verdict, and the recomputed completeness
    audit reflects exactly the frozen plan's audit view (same counts,
    same verdict, the PASS rule as the matched rule).
    """
    root = build_complete_workspace(tmp_path)
    freeze_complete(root)

    # Reload from filesystem state: a fresh read of plans/v1.json.
    reloaded = read_plan(root, "v1")
    assert reloaded.version == "v1"
    assert reloaded.status is PlanStatus.FROZEN
    assert reloaded.frozen_at == FROZEN_AT_ISO
    assert reloaded.frozen_commit is not None
    assert reloaded.goal_ids == ["GOAL-1"]
    assert reloaded.requirement_ids == ["REQ-1", "REQ-2"]

    # The audit entry exists in the persisted record: the plan record
    # embeds the inventory_audit sub-object (the only frozen place the
    # audit result is persisted, planning/audit.py).
    record = json.loads((root / "plans" / "v1.json").read_text(encoding="utf-8"))
    embedded = record["inventory_audit"]
    assert embedded["status"] == "PASS"
    assert embedded["formally_reported_items"] == 2
    assert embedded["mapped_items"] == 2
    assert embedded["coverage"] == 1.0

    # The recomputed audit reflects the frozen plan exactly: the frozen
    # plan's embedded view equals the audit's current plan view.
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.freeze_eligible
    assert audit.matched_rule_id == "R-AUD-P1"
    assert audit.plan_inventory_audit() == reloaded.inventory_audit
    assert audit.summary.formally_reported_items == 2
    assert audit.summary.mapped_items == 2
    assert audit.summary.unmapped_items == 0
    assert audit.summary.ambiguous_items == 0
    assert audit.summary.coverage == 1.0

    # The versioned lineage reports the frozen plan as FROZEN (no newer
    # version registered) and the draft as DRAFT -- a computed status.
    lineage = {entry.plan.version: entry.status for entry in plan_lineage(root)}
    assert lineage["v1-draft"] is PlanStatus.DRAFT
    assert lineage["v1"] is PlanStatus.FROZEN


# ---------------------------------------------------------------------------
# M4-G05 DAG on the frozen plan (milestone acceptance: "frozen Plan v1 and
# semantic DAG")
# ---------------------------------------------------------------------------


def test_planning_flow_dag_frozen_plan_v1_exports_semantic_dag(tmp_path) -> None:
    """The frozen Plan v1 exports the semantic DAG (DEV-M4-G05 integration).

    ``build_plan_dag(root, "v1")`` on the frozen record yields: the plan
    carried verbatim (FROZEN, version v1, resource ids), one hard
    execution-gate edge (GOAL-1 -> GOAL-2), the dependency-first
    topological order, a resource-gap block on the goal that requires the
    PROCURE resource (with the goal's scientific acceptance untouched),
    and a plan-level blocking resource id -- all from the real registered
    state, nothing stubbed.
    """
    root = build_dag_workspace(tmp_path)
    dag = build_plan_dag(root, "v1")

    # The exported plan is the exact frozen record.
    assert dag.plan.version == "v1"
    assert dag.plan.status is PlanStatus.FROZEN
    assert dag.plan.resource_ids == ["RES-1", "RES-2"]

    # Nodes: both goals, no missing contracts, acyclic, one edge.
    assert dag.acyclic
    assert dag.cyclic_goal_ids == ()
    assert dag.missing_goal_contracts == ()
    assert dag.unresolved_dependency_refs == ()
    assert {node.goal.goal_id for node in dag.nodes} == {"GOAL-1", "GOAL-2"}
    assert all(node.in_plan for node in dag.nodes)

    # The single edge: GOAL-2 hard-depends on GOAL-1 with an execution
    # gate -- classified hard_execution, raw flags preserved verbatim.
    assert len(dag.edges) == 1
    edge = dag.edges[0]
    assert edge.dependency_goal_id == "GOAL-1"
    assert edge.dependent_goal_id == "GOAL-2"
    assert edge.dependency_type is DependencyType.HARD_GATE
    assert edge.execution_gate is True
    assert edge.acceptance_gate is False
    assert edge.gate_kind is GateKind.HARD_EXECUTION

    # Topological order is dependency-first (ready-first).
    assert dag.topological_order == ("GOAL-1", "GOAL-2")

    # Resource blocker mapping: the PROCURE resource blocks GOAL-1; the
    # AVAILABLE resource never blocks GOAL-2.
    goal1_node = node_by_id(dag, "GOAL-1")
    goal2_node = node_by_id(dag, "GOAL-2")
    assert goal1_node.blockers.blocked
    assert goal1_node.blockers.blocking_resource_ids == ("RES-1",)
    assert goal1_node.blockers.missing_resource_ids == ()
    assert not goal2_node.blockers.blocked
    assert goal2_node.blockers.blocking_resource_ids == ()
    # Plan-level gap: the frozen plan's resource_ids contain the PROCURE
    # resource, so the plan itself is blocked until it resolves.
    assert dag.blockers.plan_blocking_resource_ids == ("RES-1",)

    # Blocking never alters scientific acceptance: the blocked goal's
    # acceptance sub-object is carried verbatim from the registered draft
    # contract (a gap blocks execution scheduling, never acceptance).
    registered_goal = read_goal(root, "GOAL-1")
    assert goal1_node.goal.acceptance == registered_goal.acceptance
    assert goal1_node.goal.acceptance.criteria_ref == "ACC-1"
    assert goal1_node.goal.acceptance.frozen is False


# ---------------------------------------------------------------------------
# Determinism: identical fixtures produce identical frozen state
# ---------------------------------------------------------------------------


def test_planning_flow_determinism_identical_fixtures_identical_frozen_plan(
    tmp_path,
) -> None:
    """The synthetic flow is deterministic: two identical fixtures freeze
    byte-identical plan records.

    The whole pipeline (init timestamps, git commits, plan ids, audit
    views, freeze stamps) is a pure function of the inputs, so the
    persisted ``v1.json`` / ``v1-draft.json`` records of two identically
    built workspaces are byte-identical -- no wall clock, randomness or
    platform dependence anywhere.
    """
    first = build_complete_workspace(tmp_path / "first")
    second = build_complete_workspace(tmp_path / "second")
    freeze_complete(first)
    freeze_complete(second)

    for version in ("v1-draft", "v1"):
        first_bytes = (first / "plans" / f"{version}.json").read_bytes()
        second_bytes = (second / "plans" / f"{version}.json").read_bytes()
        assert first_bytes == second_bytes
    assert (first / "project.yaml").read_bytes() == (
        second / "project.yaml"
    ).read_bytes()
