"""Resource blockers and planning DAG export (DEV-M4-G05).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_dag_ac01_*``: AVAILABLE / PROCURE / OUTSOURCE /
    CAPABILITY_GAP are representable: the four states are the frozen
    ``AvailabilityState`` enum and the resource registry registers, reads
    back and lists a ``Resource`` in every state, round-tripping exactly
    (with the canonical-JSON state file convention of the M4-G02/M4-G04
    registries).
  * AC-02 -- ``test_dag_ac02_*``: a resource gap (any state other than
    AVAILABLE, or a missing resource) can block a Goal -- and the plan --
    without altering scientific acceptance: the blocker rule table reads
    only availability state and the frozen edges (``goal.resource_ids``,
    ``resource.blocks_goal_ids``), the goal's ``acceptance`` sub-object
    never participates and is byte-identical across blocked and unblocked
    exports.
  * AC-03 -- ``test_dag_ac03_*``: the DAG distinguishes hard/soft/
    informational and execution/acceptance gates: the six gate kinds are
    distinct values of the exported edges, decided by the ordered, total,
    versioned ``GATE_AXIS_RULES`` table, with the raw model flags preserved
    verbatim and a deterministic dependency-first topological order.

Plus paradigm tests: determinism (identical exports for identical state),
canonical-JSON export shape for /goals views, TypeError boundaries, the
rule-table totality invariants (``matched_rule_id`` never None, versions
recorded), explicit unresolved-reference and cycle reporting, registry
error propagation (initialization gate, plan version errors, corrupt
records) and the resource registry conventions (immutability, id path
escape validation, schema gate).

Every test name contains "dag" so the goal verification command
``python -m pytest -q tests/planning -k dag`` selects the whole suite.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from inventory_helpers import init_project, make_item, make_requirement

from scientific_reproduction.core.models import (
    AvailabilityState,
    DependencyType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    Plan,
    Resource,
    ResourceType,
)
from scientific_reproduction.planning.dag import (
    BLOCKER_RULES,
    BLOCKER_RULESET_VERSION,
    GATE_AXIS_RULES,
    GATE_AXIS_RULESET_VERSION,
    BlockerInput,
    GateKind,
    PlanningDAG,
    build_plan_dag,
    classify_gate_kind,
    evaluate_resource_blocking,
    export_plan_dag,
    plan_dag_to_dict,
    resource_blocker_mapping,
    resource_blockers_for_goal,
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
    InvalidPlanVersionError,
    PlanNotFoundError,
    build_plan_v1,
    register_goal,
    register_plan,
)
from scientific_reproduction.planning.resources import (
    RESOURCE_GAP_STATES,
    RESOURCES_STATE_DIR,
    DuplicateResourceError,
    InvalidResourceIdError,
    ResourceError,
    ResourceNotFoundError,
    is_resource_gap,
    list_resources,
    load_resource_registry,
    read_resource,
    register_resource,
)

# ---------------------------------------------------------------------------
# Helpers (deterministic fixtures, mirroring inventory_helpers/test_freeze)
# ---------------------------------------------------------------------------

GAP_STATES = (AvailabilityState.PROCURE, AvailabilityState.OUTSOURCE, AvailabilityState.CAPABILITY_GAP)


def make_goal(
    goal_id: str,
    *,
    dependencies: tuple[GoalDependency, ...] = (),
    resource_ids: tuple[str, ...] = (),
    requirement_ids: tuple[str, ...] = ("REQ-1",),
    acceptance_criteria_ref: str = "ACC-1",
    acceptance_frozen: bool = False,
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
        acceptance=GoalAcceptance(
            criteria_ref=acceptance_criteria_ref, frozen=acceptance_frozen
        ),
        analysis_protocol_ref="ANP-1",
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        resource_ids=list(resource_ids),
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


def build_workspace(
    root: Path,
    goals: tuple[GoalContract, ...],
    resources: tuple[Resource, ...] = (),
    *,
    plan_resource_ids: tuple[str, ...] = (),
    register_resources: bool = True,
) -> Path:
    """Initialize a project with items, requirements, goals, resources and
    the draft plan; return ``root``.

    One requirement per goal maps the shared formal item, so
    ``build_plan_v1`` covers exactly the given goals.
    """
    init_project(root)
    register_inventory_item(root, make_item("ITEM-1"))
    for index, goal in enumerate(goals):
        requirement_id = f"REQ-{index + 1}"
        register_requirement(
            root,
            make_requirement(
                requirement_id,
                inventory_items=("ITEM-1",),
                goal_ids=(goal.goal_id,),
            ),
        )
    for goal in goals:
        register_goal(root, goal)
    if register_resources:
        for resource in resources:
            register_resource(root, resource)
    plan = build_plan_v1(root)
    if plan_resource_ids:
        plan = dataclasses.replace(plan, resource_ids=list(plan_resource_ids))
    register_plan(root, plan)
    return root


def build_dag(root: Path) -> PlanningDAG:
    """Build the draft-plan DAG of a workspace deterministically."""
    return build_plan_dag(root, INITIAL_PLAN_VERSION)


def node_by_id(dag: PlanningDAG, goal_id: str) -> object:
    """The DAG node of ``goal_id`` (test convenience)."""
    for node in dag.nodes:
        if node.goal.goal_id == goal_id:
            return node
    raise AssertionError(f"no node for goal {goal_id!r} in the DAG")


def plan_record(root: Path) -> Plan:
    """The registered draft plan record of a workspace."""
    from scientific_reproduction.planning.plan import read_plan

    return read_plan(root, INITIAL_PLAN_VERSION)


# ---------------------------------------------------------------------------
# AC-01: AVAILABLE / PROCURE / OUTSOURCE / CAPABILITY_GAP are representable
# ---------------------------------------------------------------------------


def test_dag_ac01_resource_states_registered_and_round_trip(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    for index, state in enumerate(AvailabilityState):
        resource = make_resource(f"RES-{index}", state)
        registered = register_resource(root, resource)
        assert registered == resource
        assert (root / RESOURCES_STATE_DIR / f"RES-{index}.json").is_file()
        assert read_resource(root, f"RES-{index}") == resource
    listed = list_resources(root)
    assert [r.resource_id for r in listed] == [f"RES-{i}" for i in range(4)]
    assert {r.availability_state for r in listed} == set(AvailabilityState)


def test_dag_ac01_availability_state_vocabulary_is_frozen_enum(tmp_path: Path) -> None:
    # The four states are exactly the frozen AvailabilityState enum
    # (schemas/resource.schema.yaml); nothing is redefined here.
    assert {s.value for s in AvailabilityState} == {
        "AVAILABLE",
        "PROCURE",
        "OUTSOURCE",
        "CAPABILITY_GAP",
    }
    # The gap vocabulary: every state other than AVAILABLE is a gap (AC-02).
    assert RESOURCE_GAP_STATES == frozenset(GAP_STATES)
    root = init_project(tmp_path / "project")
    # A registered resource survives the schema gate in every state.
    for state in AvailabilityState:
        resource = register_resource(root, make_resource(f"RES-{state.value}", state))
        assert resource.availability_state is state


def test_dag_ac01_gap_predicate_recognizes_every_state() -> None:
    for state in GAP_STATES:
        assert is_resource_gap(make_resource(f"RES-{state.value}", state)) is True
    assert (
        is_resource_gap(make_resource("RES-AVAILABLE", AvailabilityState.AVAILABLE))
        is False
    )
    # A missing resource (no registered record) is a gap by definition.
    assert is_resource_gap(None) is True
    with pytest.raises(TypeError, match="must be a Resource or None"):
        is_resource_gap("RES-1")  # type: ignore[arg-type]


def test_dag_ac01_registry_state_files_are_canonical_json(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    resource = make_resource("RES-1", AvailabilityState.CAPABILITY_GAP)
    register_resource(root, resource)
    path = root / RESOURCES_STATE_DIR / "RES-1.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == resource.to_dict()
    # Canonical: sorted keys, 2-space indent, trailing newline.
    assert path.read_text(encoding="utf-8") == (
        json.dumps(resource.to_dict(), indent=2, sort_keys=True) + "\n"
    )
    # Reading back is a round-trip of the exact stored bytes.
    assert read_resource(root, "RES-1").to_dict() == stored


def test_dag_ac01_duplicate_resource_registration_rejected_no_clobber(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_resource(root, make_resource("RES-1", AvailabilityState.AVAILABLE))
    path = root / RESOURCES_STATE_DIR / "RES-1.json"
    original = path.read_text(encoding="utf-8")
    with pytest.raises(DuplicateResourceError, match="already registered"):
        register_resource(
            root, make_resource("RES-1", AvailabilityState.CAPABILITY_GAP)
        )
    # No clobbering: the original record bytes are untouched.
    assert path.read_text(encoding="utf-8") == original


def test_dag_ac01_unsafe_resource_ids_rejected(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    for bad in ("../escape", "a/b", "a\\b", "", ".", ".."):
        with pytest.raises(InvalidResourceIdError, match="invalid resource id"):
            register_resource(
                root, make_resource(bad, AvailabilityState.AVAILABLE)
            )
    # A non-str id is rejected with the stable id error, not a raw TypeError.
    with pytest.raises(InvalidResourceIdError, match="invalid resource id"):
        register_resource(
            root,
            {
                "resource_id": 5,
                "name": "x",
                "resource_type": "reagent",
                "availability_state": "AVAILABLE",
            },
        )


def test_dag_ac01_resource_registry_type_error_boundaries(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    with pytest.raises(TypeError, match="root must be a str or Path"):
        register_resource(123, make_resource("RES-1", AvailabilityState.AVAILABLE))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="resource must be a Resource or a mapping"):
        register_resource(root, "RES-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="resource_id must be a str"):
        read_resource(root, 5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="root must be a str or Path"):
        read_resource(123, "RES-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="root must be a str or Path"):
        list_resources(123)  # type: ignore[arg-type]


def test_dag_ac01_resource_error_hierarchy_and_schema_gate(tmp_path: Path) -> None:
    # ResourceError is a PlanningError/ValueError subclass; the specific
    # errors are ResourceErrors with stable messages (the registry path).
    for error_type in (DuplicateResourceError, ResourceNotFoundError, InvalidResourceIdError):
        assert issubclass(error_type, ResourceError)
        assert issubclass(error_type, ValueError)
    assert issubclass(ResourceError, PlanningError)
    root = init_project(tmp_path / "project")
    # Unknown availability states are rejected at the frozen vocabulary.
    with pytest.raises(ValueError, match="not a valid AvailabilityState"):
        register_resource(
            root,
            {  # type: ignore[arg-type]
                "resource_id": "RES-1",
                "name": "x",
                "resource_type": "reagent",
                "availability_state": "SOLD_OUT",
            },
        )


def test_dag_ac01_resource_registry_gated_and_not_found(tmp_path: Path) -> None:
    uninitialized = tmp_path / "nowhere"
    with pytest.raises(ProjectNotInitializedError, match="no project state"):
        register_resource(
            uninitialized, make_resource("RES-1", AvailabilityState.AVAILABLE)
        )
    root = init_project(tmp_path / "project")
    with pytest.raises(ResourceNotFoundError, match="no resource with id"):
        read_resource(root, "RES-NOPE")
    # An initialized project with no resources has an empty registry
    # (deterministic, never an error).
    assert load_resource_registry(root).resources == ()


# ---------------------------------------------------------------------------
# AC-02: a resource gap blocks a Goal without altering scientific acceptance
# ---------------------------------------------------------------------------


def test_dag_ac02_gap_blocks_goal_without_altering_acceptance(tmp_path: Path) -> None:
    goal = make_goal("GOAL-1", resource_ids=("RES-GAS",))

    available_root = build_workspace(
        tmp_path / "available",
        goals=(goal,),
        resources=(make_resource("RES-GAS", AvailabilityState.AVAILABLE),),
    )
    gap_root = build_workspace(
        tmp_path / "gap",
        goals=(goal,),
        resources=(make_resource("RES-GAS", AvailabilityState.CAPABILITY_GAP),),
    )

    available_dag = build_dag(available_root)
    gap_dag = build_dag(gap_root)
    assert node_by_id(available_dag, "GOAL-1").blockers.blocked is False
    assert node_by_id(gap_dag, "GOAL-1").blockers.blocked is True
    assert node_by_id(gap_dag, "GOAL-1").blockers.blocking_resource_ids == (
        "RES-GAS",
    )
    # The scientific acceptance data is untouched: identical acceptance
    # sub-objects (criteria_ref, frozen) in both exports.
    available_acceptance = node_by_id(available_dag, "GOAL-1").goal.acceptance
    gap_acceptance = node_by_id(gap_dag, "GOAL-1").goal.acceptance
    assert available_acceptance == gap_acceptance
    assert gap_acceptance.criteria_ref == "ACC-1"
    assert gap_acceptance.frozen is False
    # The only difference between the two exports is the blocking fact.
    available_view = json.loads(export_plan_dag(available_root, INITIAL_PLAN_VERSION))
    gap_view = json.loads(export_plan_dag(gap_root, INITIAL_PLAN_VERSION))
    assert available_view["nodes"][0]["goal"] == gap_view["nodes"][0]["goal"]
    assert available_view["nodes"][0]["blocked"] is False
    assert gap_view["nodes"][0]["blocked"] is True
    # The plan records themselves are identical (blocking is not persisted).
    assert available_view["plan"] == gap_view["plan"]


def test_dag_ac02_every_gap_state_blocks_and_acceptance_unchanged(
    tmp_path: Path,
) -> None:
    for state in GAP_STATES:
        root = build_workspace(
            tmp_path / f"gap-{state.value}",
            goals=(make_goal("GOAL-1", resource_ids=("RES-1",)),),
            resources=(make_resource("RES-1", state),),
        )
        dag = build_dag(root)
        node = node_by_id(dag, "GOAL-1")
        assert node.blockers.blocked is True
        assert node.blockers.blocking_resource_ids == ("RES-1",)
        assert node.blockers.missing_resource_ids == ()
        # Acceptance never participates: identical in every gap state.
        assert node.goal.acceptance.criteria_ref == "ACC-1"
        assert node.goal.acceptance.frozen is False


def test_dag_ac02_missing_resource_reference_blocks_goal(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1", resource_ids=("RES-MISSING",)),),
    )
    dag = build_dag(root)
    node = node_by_id(dag, "GOAL-1")
    assert node.blockers.blocked is True
    assert node.blockers.blocking_resource_ids == ("RES-MISSING",)
    assert node.blockers.missing_resource_ids == ("RES-MISSING",)
    # R-BLK-1 decided the pair: the missing reference is a gap by definition.
    pair = node.blockers.decisions[0]
    assert pair.matched_rule_id == "R-BLK-1"
    assert pair.verdict.value == "BLOCKS"
    # The global mapping reports the missing reference too.
    assert dag.blockers.missing_resource_ids == ("RES-MISSING",)
    # Acceptance is untouched by the missing reference.
    assert node.goal.acceptance.criteria_ref == "ACC-1"
    assert node.goal.acceptance.frozen is False


def test_dag_ac02_declared_blocker_blocks_without_requirement(tmp_path: Path) -> None:
    # The resource declares the goal in blocks_goal_ids even though the
    # goal does not require it: the declaration edge blocks (R-BLK-2).
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1"),),
        resources=(
            make_resource(
                "RES-INSTRUMENT",
                AvailabilityState.OUTSOURCE,
                blocks_goal_ids=("GOAL-1",),
            ),
        ),
    )
    dag = build_dag(root)
    node = node_by_id(dag, "GOAL-1")
    assert node.blockers.blocked is True
    assert node.blockers.blocking_resource_ids == ("RES-INSTRUMENT",)
    pair = node.blockers.decisions[0]
    assert pair.matched_rule_id == "R-BLK-2"


def test_dag_ac02_available_declared_resource_never_blocks(tmp_path: Path) -> None:
    # An AVAILABLE resource blocks nothing, even when it declares the goal.
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1", resource_ids=("RES-1",)),),
        resources=(
            make_resource(
                "RES-1",
                AvailabilityState.AVAILABLE,
                blocks_goal_ids=("GOAL-1",),
            ),
        ),
    )
    dag = build_dag(root)
    node = node_by_id(dag, "GOAL-1")
    assert node.blockers.blocked is False
    assert node.blockers.blocking_resource_ids == ()
    pair = node.blockers.decisions[0]
    assert pair.matched_rule_id == "R-BLK-4"
    assert pair.verdict.value == "NOT_BLOCKING"
    assert dag.blockers.missing_resource_ids == ()


def test_dag_ac02_blocking_never_reads_acceptance_state(tmp_path: Path) -> None:
    # Blocking is an execution/scheduling fact: the same gap blocks goals
    # with entirely different scientific acceptance states identically.
    frozen_root = build_workspace(
        tmp_path / "frozen",
        goals=(
            make_goal(
                "GOAL-1",
                resource_ids=("RES-1",),
                acceptance_criteria_ref="ACC-STAT",
                acceptance_frozen=True,
            ),
        ),
        resources=(make_resource("RES-1", AvailabilityState.PROCURE),),
    )
    open_root = build_workspace(
        tmp_path / "open",
        goals=(
            make_goal(
                "GOAL-1",
                resource_ids=("RES-1",),
                acceptance_criteria_ref="ACC-DRAFT",
                acceptance_frozen=False,
            ),
        ),
        resources=(make_resource("RES-1", AvailabilityState.PROCURE),),
    )
    frozen_node = node_by_id(build_dag(frozen_root), "GOAL-1")
    open_node = node_by_id(build_dag(open_root), "GOAL-1")
    assert frozen_node.blockers.blocked is True
    assert open_node.blockers.blocked is True
    assert frozen_node.blockers.blocking_resource_ids == ("RES-1",)
    assert open_node.blockers.blocking_resource_ids == ("RES-1",)
    # The acceptance states themselves differ (as authored) and stay as
    # authored -- blocking does not leak into acceptance in either direction.
    assert frozen_node.goal.acceptance.frozen is True
    assert frozen_node.goal.acceptance.criteria_ref == "ACC-STAT"
    assert open_node.goal.acceptance.frozen is False
    assert open_node.goal.acceptance.criteria_ref == "ACC-DRAFT"


def test_dag_ac02_plan_level_resource_gap_blocks_the_plan(tmp_path: Path) -> None:
    gap_root = build_workspace(
        tmp_path / "gap",
        goals=(make_goal("GOAL-1"),),
        resources=(make_resource("RES-PLAN", AvailabilityState.CAPABILITY_GAP),),
        plan_resource_ids=("RES-PLAN",),
    )
    dag = build_dag(gap_root)
    assert dag.blockers.plan_blocking_resource_ids == ("RES-PLAN",)
    available_root = build_workspace(
        tmp_path / "available",
        goals=(make_goal("GOAL-1"),),
        resources=(make_resource("RES-PLAN", AvailabilityState.AVAILABLE),),
        plan_resource_ids=("RES-PLAN",),
    )
    assert build_dag(available_root).blockers.plan_blocking_resource_ids == ()
    # A missing plan-level reference is a gap and is reported globally.
    missing_root = build_workspace(
        tmp_path / "missing",
        goals=(make_goal("GOAL-1"),),
        plan_resource_ids=("RES-GONE",),
    )
    dag = build_dag(missing_root)
    assert dag.blockers.plan_blocking_resource_ids == ("RES-GONE",)
    assert dag.blockers.missing_resource_ids == ("RES-GONE",)


def test_dag_ac02_mapping_is_rule_based_and_auditable(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1", resource_ids=("RES-A", "RES-B")),),
        resources=(
            make_resource("RES-A", AvailabilityState.AVAILABLE),
            make_resource("RES-B", AvailabilityState.OUTSOURCE),
        ),
    )
    dag = build_dag(root)
    entry = node_by_id(dag, "GOAL-1").blockers
    # One evaluated pair per edge, sorted by resource id, full rule traces.
    assert [d.input.resource_id for d in entry.decisions] == ["RES-A", "RES-B"]
    assert [d.matched_rule_id for d in entry.decisions] == [
        "R-BLK-4",
        "R-BLK-3",
    ]
    assert entry.blocking_resource_ids == ("RES-B",)
    for decision in entry.decisions:
        assert decision.ruleset_version == BLOCKER_RULESET_VERSION
        assert len(decision.decisions) == len(BLOCKER_RULES)
        assert decision.matched_rule_id is not None


# ---------------------------------------------------------------------------
# AC-03: the DAG distinguishes hard/soft/informational and
# execution/acceptance gates
# ---------------------------------------------------------------------------


def test_dag_ac03_six_gate_kinds_are_distinct_exact_values() -> None:
    kinds = {
        classify_gate_kind(DependencyType.HARD_GATE, True, False).gate_kind,
        classify_gate_kind(DependencyType.HARD_GATE, False, True).gate_kind,
        classify_gate_kind(DependencyType.SOFT_DEPENDENCY, True, False).gate_kind,
        classify_gate_kind(DependencyType.SOFT_DEPENDENCY, False, True).gate_kind,
        classify_gate_kind(DependencyType.INFORMATIONAL, True, False).gate_kind,
        classify_gate_kind(DependencyType.INFORMATIONAL, False, True).gate_kind,
    }
    assert kinds == {
        GateKind.HARD_EXECUTION,
        GateKind.HARD_ACCEPTANCE,
        GateKind.SOFT_EXECUTION,
        GateKind.SOFT_ACCEPTANCE,
        GateKind.INFORMATIONAL_EXECUTION,
        GateKind.INFORMATIONAL_ACCEPTANCE,
    }
    assert len(kinds) == 6
    # The axis rules fire per the normative readings (execution-only /
    # acceptance-only declared gates).
    assert (
        classify_gate_kind(DependencyType.HARD_GATE, True, False).matched_rule_id
        == "R-AX-E1"
    )
    assert (
        classify_gate_kind(DependencyType.HARD_GATE, False, True).matched_rule_id
        == "R-AX-A1"
    )


def test_dag_ac03_gate_kind_classification_is_deterministic() -> None:
    first = classify_gate_kind(DependencyType.SOFT_DEPENDENCY, True, False)
    second = classify_gate_kind(DependencyType.SOFT_DEPENDENCY, True, False)
    assert first == second
    assert first.input.execution_gate is True
    assert first.input.acceptance_gate is False
    assert first.gate_kind is GateKind.SOFT_EXECUTION
    assert first.ruleset_version == GATE_AXIS_RULESET_VERSION
    # Every rule evaluation is recorded, in evaluation order.
    assert [d.rule_id for d in first.decisions] == [
        r.rule_id for r in GATE_AXIS_RULES
    ]


def test_dag_ac03_gate_kind_table_is_total() -> None:
    # Every declared shape -- all 3 strengths x all 4 flag combinations --
    # yields exactly one of the six kinds with a full rule trace.
    for dependency_type in DependencyType:
        for execution_gate in (False, True):
            for acceptance_gate in (False, True):
                assessment = classify_gate_kind(
                    dependency_type, execution_gate, acceptance_gate
                )
                assert assessment.gate_kind in GateKind
                assert assessment.matched_rule_id is not None
                assert len(assessment.decisions) == len(GATE_AXIS_RULES)


def test_dag_ac03_both_and_neither_flags_classified_deterministically() -> None:
    # Both flags set: the kind reports the execution axis (R-AX-B1, the
    # scheduling axis) while the raw acceptance flag stays verbatim -- the
    # FDM-201 activation pattern (examples/fdm-201/goal.example.yaml).
    both = classify_gate_kind(DependencyType.HARD_GATE, True, True)
    assert both.gate_kind is GateKind.HARD_EXECUTION
    assert both.matched_rule_id == "R-AX-B1"
    assert both.input.acceptance_gate is True
    # Neither flag set: the schema default classifies on the execution
    # axis (R-AX-N1, the conservative scheduling baseline); raw flags stay
    # False. Blocking semantics are unaffected (core/rules/dependencies.py
    # R-DEP-6: an un-flagged hard edge gates nothing).
    neither = classify_gate_kind(DependencyType.SOFT_DEPENDENCY, False, False)
    assert neither.gate_kind is GateKind.SOFT_EXECUTION
    assert neither.matched_rule_id == "R-AX-N1"
    assert neither.input.execution_gate is False
    assert neither.input.acceptance_gate is False


def test_dag_ac03_dag_edges_carry_all_six_kinds_distinctly(tmp_path: Path) -> None:
    upstreams = ("GOAL-U1", "GOAL-U2", "GOAL-U3", "GOAL-U4", "GOAL-U5", "GOAL-U6")
    dependencies = (
        GoalDependency("GOAL-U1", DependencyType.HARD_GATE, True, False),
        GoalDependency("GOAL-U2", DependencyType.HARD_GATE, False, True),
        GoalDependency("GOAL-U3", DependencyType.SOFT_DEPENDENCY, True, False),
        GoalDependency("GOAL-U4", DependencyType.SOFT_DEPENDENCY, False, True),
        GoalDependency("GOAL-U5", DependencyType.INFORMATIONAL, True, False),
        GoalDependency("GOAL-U6", DependencyType.INFORMATIONAL, False, True),
    )
    root = build_workspace(
        tmp_path / "project",
        goals=(
            *(make_goal(gid) for gid in upstreams),
            make_goal("GOAL-DOWN", dependencies=dependencies),
        ),
    )
    dag = build_dag(root)
    edge_kinds = sorted(
        edge.gate_kind.value for edge in dag.edges
        if edge.dependent_goal_id == "GOAL-DOWN"
    )
    assert edge_kinds == sorted(k.value for k in (
        GateKind.HARD_EXECUTION,
        GateKind.HARD_ACCEPTANCE,
        GateKind.SOFT_EXECUTION,
        GateKind.SOFT_ACCEPTANCE,
        GateKind.INFORMATIONAL_EXECUTION,
        GateKind.INFORMATIONAL_ACCEPTANCE,
    ))
    # Raw model fields ride along verbatim on every exported edge.
    hard_acceptance = next(
        e for e in dag.edges
        if e.dependent_goal_id == "GOAL-DOWN"
        and e.dependency_goal_id == "GOAL-U2"
    )
    assert hard_acceptance.dependency_type is DependencyType.HARD_GATE
    assert hard_acceptance.execution_gate is False
    assert hard_acceptance.acceptance_gate is True
    assert hard_acceptance.gate_kind is GateKind.HARD_ACCEPTANCE


def test_dag_ac03_topological_order_is_dependency_first(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(
            make_goal("GOAL-A"),
            make_goal(
                "GOAL-B",
                dependencies=(
                    GoalDependency("GOAL-A", DependencyType.HARD_GATE, True, False),
                ),
            ),
            make_goal(
                "GOAL-C",
                dependencies=(
                    GoalDependency("GOAL-B", DependencyType.HARD_GATE, True, False),
                ),
            ),
            make_goal(
                "GOAL-D",
                dependencies=(
                    GoalDependency("GOAL-B", DependencyType.SOFT_DEPENDENCY, True, False),
                    GoalDependency("GOAL-C", DependencyType.HARD_GATE, False, True),
                ),
            ),
        ),
    )
    dag = build_dag(root)
    assert dag.acyclic is True
    assert dag.cyclic_goal_ids == ()
    # A goal's dependencies precede it in the ready-first order.
    order = dag.topological_order
    for goal_id, upstream in (("GOAL-B", "GOAL-A"), ("GOAL-C", "GOAL-B"), ("GOAL-D", "GOAL-C")):
        assert order.index(upstream) < order.index(goal_id)
    assert order[0] == "GOAL-A"
    assert len(order) == 4


def test_dag_ac03_cycles_reported_explicitly_not_raised(tmp_path: Path) -> None:
    cyclic_root = build_workspace(
        tmp_path / "cycle",
        goals=(
            make_goal(
                "GOAL-A",
                dependencies=(
                    GoalDependency("GOAL-B", DependencyType.HARD_GATE, True, False),
                ),
            ),
            make_goal(
                "GOAL-B",
                dependencies=(
                    GoalDependency("GOAL-A", DependencyType.HARD_GATE, True, False),
                ),
            ),
        ),
    )
    dag = build_dag(cyclic_root)
    assert dag.acyclic is False
    assert dag.cyclic_goal_ids == ("GOAL-A", "GOAL-B")
    assert dag.topological_order == ()
    # A self-dependency is a cycle of length one.
    self_root = build_workspace(
        tmp_path / "self",
        goals=(
            make_goal(
                "GOAL-S",
                dependencies=(
                    GoalDependency("GOAL-S", DependencyType.HARD_GATE, True, False),
                ),
            ),
        ),
    )
    self_dag = build_dag(self_root)
    assert self_dag.acyclic is False
    assert self_dag.cyclic_goal_ids == ("GOAL-S",)


# ---------------------------------------------------------------------------
# DAG builder, export format and registry integration
# ---------------------------------------------------------------------------


def test_dag_export_is_deterministic_canonical_json(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1", resource_ids=("RES-1",)),),
        resources=(make_resource("RES-1", AvailabilityState.AVAILABLE),),
    )
    first = export_plan_dag(root, INITIAL_PLAN_VERSION)
    second = export_plan_dag(root, INITIAL_PLAN_VERSION)
    assert first == second
    # Canonical form: sorted keys, 2-space indent, trailing newline.
    assert first.endswith("\n")
    payload = json.loads(first)
    assert list(payload) == sorted(payload)
    assert first == json.dumps(payload, indent=2, sort_keys=True) + "\n"


def test_dag_build_is_pure_function_of_registered_state(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(
            make_goal(
                "GOAL-2",
                dependencies=(
                    GoalDependency("GOAL-1", DependencyType.HARD_GATE, True, False),
                ),
            ),
            make_goal("GOAL-1"),
        ),
        resources=(
            make_resource("RES-1", AvailabilityState.AVAILABLE),
            make_resource("RES-2", AvailabilityState.PROCURE),
        ),
    )
    first = build_dag(root)
    second = build_dag(root)
    assert plan_dag_to_dict(first) == plan_dag_to_dict(second)
    # The record is deterministic and fully derived: nodes sorted, edges
    # sorted, blockers attached to every node.
    assert [n.goal.goal_id for n in first.nodes] == ["GOAL-1", "GOAL-2"]
    assert [(e.dependency_goal_id, e.dependent_goal_id) for e in first.edges] == [
        ("GOAL-1", "GOAL-2")
    ]
    assert first.topological_order == ("GOAL-1", "GOAL-2")
    assert first.export_version == "1.0"


def test_dag_external_dependency_node_pulled_into_dag(tmp_path: Path) -> None:
    # GOAL-1 is registered and depended on by GOAL-2, but no requirement
    # maps it, so the plan does not cover it: the edge still renders and
    # the registered upstream becomes a node marked in_plan False.
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("ITEM-1"))
    register_requirement(
        root,
        make_requirement("REQ-1", inventory_items=("ITEM-1",), goal_ids=("GOAL-2",)),
    )
    register_goal(root, make_goal("GOAL-1", requirement_ids=("REQ-1",)))
    register_goal(
        root,
        make_goal(
            "GOAL-2",
            requirement_ids=("REQ-1",),
            dependencies=(
                GoalDependency("GOAL-1", DependencyType.HARD_GATE, True, False),
            ),
        ),
    )
    register_plan(root, build_plan_v1(root))
    dag = build_dag(root)
    assert [n.goal.goal_id for n in dag.nodes] == ["GOAL-1", "GOAL-2"]
    assert node_by_id(dag, "GOAL-1").in_plan is False
    assert node_by_id(dag, "GOAL-2").in_plan is True
    assert dag.topological_order == ("GOAL-1", "GOAL-2")


def test_dag_missing_goal_contracts_reported_explicitly(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("ITEM-1"))
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("ITEM-1",),
            goal_ids=("GOAL-1", "GOAL-GHOST"),
        ),
    )
    register_goal(root, make_goal("GOAL-1", requirement_ids=("REQ-1",)))
    register_plan(root, build_plan_v1(root))
    dag = build_dag(root)
    # The unresolvable plan goal is reported, never silently dropped.
    assert dag.missing_goal_contracts == ("GOAL-GHOST",)
    assert [n.goal.goal_id for n in dag.nodes] == ["GOAL-1"]


def test_dag_unresolved_dependency_refs_reported_explicitly(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(
            make_goal(
                "GOAL-1",
                dependencies=(
                    GoalDependency("GOAL-GHOST", DependencyType.HARD_GATE, True, False),
                ),
            ),
        ),
    )
    dag = build_dag(root)
    assert len(dag.unresolved_dependency_refs) == 1
    ref = dag.unresolved_dependency_refs[0]
    assert (ref.dependent_goal_id, ref.dependency_goal_id) == (
        "GOAL-1",
        "GOAL-GHOST",
    )
    # The edge cannot render and is absent from the exported edges.
    assert dag.edges == ()


def test_dag_export_shape_suitable_for_goals_views(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(
            make_goal("GOAL-1", resource_ids=("RES-1",)),
            make_goal(
                "GOAL-2",
                dependencies=(
                    GoalDependency("GOAL-1", DependencyType.HARD_GATE, True, True),
                ),
            ),
        ),
        resources=(make_resource("RES-1", AvailabilityState.CAPABILITY_GAP),),
    )
    payload = json.loads(export_plan_dag(root, INITIAL_PLAN_VERSION))
    assert set(payload) == {
        "export_version",
        "plan",
        "nodes",
        "edges",
        "topological_order",
        "acyclic",
        "cyclic_goal_ids",
        "missing_goal_contracts",
        "unresolved_dependency_refs",
        "blockers",
    }
    # The /goals blocked view: every node reports its blocking fact, and
    # the goal contract (with its acceptance sub-object) rides along.
    blocked_nodes = [n for n in payload["nodes"] if n["blocked"]]
    assert [n["goal"]["goal_id"] for n in blocked_nodes] == ["GOAL-1"]
    assert blocked_nodes[0]["blocking_resource_ids"] == ["RES-1"]
    assert blocked_nodes[0]["goal"]["acceptance"] == {
        "criteria_ref": "ACC-1",
        "frozen": False,
    }
    # The /goals view: plan context and the deterministic ready-first order.
    assert payload["plan"]["version"] == INITIAL_PLAN_VERSION
    assert payload["topological_order"] == ["GOAL-1", "GOAL-2"]
    assert payload["acyclic"] is True
    # Edges render with the six-kind gate vocabulary plus raw flags.
    assert payload["edges"][0]["gate_kind"] == "hard_execution"
    assert payload["edges"][0]["execution_gate"] is True
    assert payload["edges"][0]["acceptance_gate"] is True
    assert payload["blockers"]["plan_blocking_resource_ids"] == []


def test_dag_build_type_error_boundaries(tmp_path: Path) -> None:
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1"),),
    )
    with pytest.raises(TypeError, match="root must be a str or Path"):
        build_plan_dag(123, INITIAL_PLAN_VERSION)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="version must be a str"):
        build_plan_dag(root, 5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="root must be a str or Path"):
        export_plan_dag(123, INITIAL_PLAN_VERSION)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="plan_dag_to_dict expects a PlanningDAG"):
        plan_dag_to_dict("not-a-dag")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be a DependencyType"):
        classify_gate_kind("hard_gate", True, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="execution_gate must be a bool"):
        classify_gate_kind(DependencyType.HARD_GATE, 1, False)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a BlockerInput"):
        evaluate_resource_blocking("x")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a GoalContract"):
        resource_blockers_for_goal("GOAL-1", load_resource_registry(root))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a ResourceRegistry"):
        resource_blockers_for_goal(make_goal("GOAL-1"), "registry")  # type: ignore[arg-type]
    plan = plan_record(root)
    with pytest.raises(TypeError, match="expects a Plan"):
        resource_blocker_mapping("plan", (make_goal("GOAL-1"),), load_resource_registry(root))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a sequence of GoalContract"):
        resource_blocker_mapping(plan, "GOAL-1", load_resource_registry(root))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects GoalContract elements"):
        resource_blocker_mapping(plan, ("GOAL-1",), load_resource_registry(root))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a ResourceRegistry"):
        resource_blocker_mapping(plan, (make_goal("GOAL-1"),), "registry")  # type: ignore[arg-type]


def test_dag_registry_errors_propagate_with_stable_messages(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotInitializedError, match="no project state"):
        build_plan_dag(tmp_path / "nowhere", INITIAL_PLAN_VERSION)
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1"),),
    )
    with pytest.raises(PlanNotFoundError, match="no plan with version"):
        build_plan_dag(root, "v99")
    with pytest.raises(InvalidPlanVersionError, match="invalid plan version"):
        build_plan_dag(root, "not-a-version")
    # A corrupt plan record is rejected with the stable ValueError.
    (root / "plans" / f"{INITIAL_PLAN_VERSION}.json").write_text(
        "{not json", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt plan record"):
        build_plan_dag(root, INITIAL_PLAN_VERSION)
    # A corrupt resource record is rejected with the stable ValueError too.
    healthy = build_workspace(
        tmp_path / "healthy",
        goals=(make_goal("GOAL-1", resource_ids=("RES-1",)),),
        resources=(make_resource("RES-1", AvailabilityState.AVAILABLE),),
    )
    (healthy / RESOURCES_STATE_DIR / "RES-1.json").write_text(
        "{not json", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt resource record"):
        build_plan_dag(healthy, INITIAL_PLAN_VERSION)


def test_dag_blocker_input_validation_and_totality() -> None:
    # The rule table is total over the pair vocabulary: every input yields
    # a verdict with a matched rule and a full trace. (The input record
    # itself is strict: registered=True requires a state, registered=False
    # requires None.)
    for registered, states in (
        (False, (None,)),
        (True, tuple(AvailabilityState)),
    ):
        for state in states:
            for explicitly_blocks in (False, True):
                for required_by_goal in (False, True):
                    assessment = evaluate_resource_blocking(
                        BlockerInput(
                            goal_id="GOAL-1",
                            resource_id="RES-1",
                            registered=registered,
                            availability_state=state,
                            explicitly_blocks=explicitly_blocks,
                            required_by_goal=required_by_goal,
                        )
                    )
                    assert assessment.matched_rule_id is not None
                    assert len(assessment.decisions) == len(BLOCKER_RULES)
                    assert assessment.ruleset_version == BLOCKER_RULESET_VERSION
    # The input record is strict: empty ids and inconsistent state are
    # rejected with stable messages; wrong types raise TypeError.
    with pytest.raises(ValueError, match="goal_id must be a non-empty string"):
        BlockerInput(
            goal_id="",
            resource_id="RES-1",
            registered=True,
            availability_state=AvailabilityState.AVAILABLE,
            explicitly_blocks=False,
            required_by_goal=False,
        )
    with pytest.raises(ValueError, match="registered must agree"):
        BlockerInput(
            goal_id="GOAL-1",
            resource_id="RES-1",
            registered=True,
            availability_state=None,
            explicitly_blocks=False,
            required_by_goal=False,
        )
    with pytest.raises(TypeError, match="availability_state must be"):
        BlockerInput(
            goal_id="GOAL-1",
            resource_id="RES-1",
            registered=True,
            availability_state="AVAILABLE",  # type: ignore[arg-type]
            explicitly_blocks=False,
            required_by_goal=False,
        )


def test_dag_duplicate_declared_edges_do_not_double_the_constraint(
    tmp_path: Path,
) -> None:
    # The same dependency declared twice is one constraint for ordering
    # (Kahn's algorithm over distinct edges) and renders twice for the
    # auditable declared trace.
    root = build_workspace(
        tmp_path / "project",
        goals=(
            make_goal("GOAL-1"),
            make_goal(
                "GOAL-2",
                dependencies=(
                    GoalDependency("GOAL-1", DependencyType.HARD_GATE, True, False),
                    GoalDependency("GOAL-1", DependencyType.HARD_GATE, True, False),
                ),
            ),
        ),
    )
    dag = build_dag(root)
    assert len(dag.edges) == 2
    assert dag.topological_order == ("GOAL-1", "GOAL-2")
    assert dag.acyclic is True


def test_dag_resources_are_read_only_inputs_of_the_dag(tmp_path: Path) -> None:
    # The DAG builder never writes: registering nothing but the plan and
    # goals leaves the resource registry untouched, and the builder's
    # outputs never mutate the registered state.
    root = build_workspace(
        tmp_path / "project",
        goals=(make_goal("GOAL-1", resource_ids=("RES-1",)),),
        resources=(make_resource("RES-1", AvailabilityState.PROCURE),),
    )
    before = (root / RESOURCES_STATE_DIR / "RES-1.json").read_text(
        encoding="utf-8"
    )
    build_dag(root)
    assert (root / RESOURCES_STATE_DIR / "RES-1.json").read_text(
        encoding="utf-8"
    ) == before
    registry = load_resource_registry(root)
    assert [r.resource_id for r in registry.resources] == ["RES-1"]
    assert registry.resources[0].availability_state is AvailabilityState.PROCURE
