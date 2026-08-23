"""Planning-layer tests for the runtime compute execution package
(issue #161).

End-to-end across the freeze boundary: a computation Goal carrying the
frozen scientific design (force field, k-point mesh, cutoff,
convergence criteria) is registered as a draft, the plan is frozen, and
the generator builds the runtime execution package **from the frozen
record** (``read_goal``) -- carrying the frozen Goal version and the
scientific parameters verbatim. A draft Goal from the registry is
refused loudly, and the package record lands in the canonical
``compute/`` state tree through the filesystem backend.

Deterministic throughout: ``init_project``/``freeze_plan`` run with
injected identities and timestamps (``init_helpers`` /
``inventory_helpers`` / the fixed ``FROZEN_AT``), no wall clock, no
randomness.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from inventory_helpers import (
    init_project,
    make_item,
    make_requirement,
)

from scientific_reproduction.adapters.compute.package import (
    COMPUTE_EXECUTION_PACKAGE_SCHEMA,
    GoalNotFrozenError,
    build_compute_execution_package,
)
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Confidence,
    DecisionMode,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    PrimaryOrExploratory,
    StatisticalDesign,
)
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_and_reject,
    validate_object,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.freeze import freeze_plan
from scientific_reproduction.planning.init import read_project_state
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    INITIAL_PLAN_VERSION,
    build_plan_v1,
    read_goal,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
    register_statistical_design,
)

#: Fixed freeze timestamp: every freeze in this suite is deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: The frozen computation design the generator must carry verbatim:
#: force field, k-point mesh, cutoff and convergence criteria.
SCIENTIFIC_INPUTS: list[dict[str, object]] = [
    {"name": "force_field", "value": "UFF"},
    {"name": "k_point_mesh", "value": "2x2x2"},
    {"name": "cutoff", "value": "12.5", "unit": "A"},
    {"name": "convergence", "value": "1e-6", "criterion": "energy_tolerance"},
]


def make_simulation_goal(goal_id: str) -> GoalContract:
    """Build a schema-valid draft computation Goal with the scientific
    parameters frozen into its ``inputs``."""
    return GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported simulation ({goal_id}).",
        unit_process_type="simulation",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the reported GCMC adsorption isotherms.",
        requirement_ids=["REQ-1", "REQ-2"],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=False),
        analysis_protocol_ref="ANL-1",
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref="CLS-1",
        inputs=SCIENTIFIC_INPUTS,
        outputs=[{"name": "uptake.csv"}],
        resource_ids=["RES-021"],
    )


def make_acceptance(acceptance_id: str) -> AcceptanceCriteria:
    """Build a schema-valid draft acceptance record."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id="GOAL-1",
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "uptake_at_defined_pressure", "rule": "equivalence"}],
        target={"metric": "uptake_at_defined_pressure"},
        confidence=Confidence.LOW,
        statistical_design_ref="DESIGN-1",
    )


def make_analysis(analysis_id: str) -> AnalysisProtocolOrResult:
    """Build a schema-valid draft analysis protocol."""
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


def make_statistical_design(design_id: str) -> StatisticalDesign:
    """Build a schema-valid draft statistical design."""
    return StatisticalDesign(
        design_id=design_id,
        goal_id="GOAL-1",
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


def build_simulation_workspace(root: Path) -> Path:
    """Initialize a freeze-eligible project with one computation Goal
    (the full goal-contract family, in draft)."""
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
    register_goal(root, make_simulation_goal("GOAL-1"))
    register_statistical_design(root, make_statistical_design("DESIGN-1"))
    register_acceptance(root, make_acceptance("ACC-1"))
    register_analysis_protocol(root, make_analysis("ANL-1"))
    register_closure_contract(root, make_closure("CLS-1"))
    return root


def freeze_simulation_workspace(root: Path) -> None:
    """Build and freeze the draft plan deterministically."""
    freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)


# ---------------------------------------------------------------------------
# The frozen Goal -> package end-to-end path
# ---------------------------------------------------------------------------


def test_frozen_goal_produces_schema_valid_package_end_to_end(
    tmp_path: Path,
) -> None:
    root = build_simulation_workspace(tmp_path)
    freeze_simulation_workspace(root)

    frozen_goal = read_goal(root, "GOAL-1")
    assert frozen_goal.frozen
    assert frozen_goal.version == "v1"  # the formal version stamped by freeze

    project = read_project_state(root)
    package = build_compute_execution_package(
        frozen_goal,
        package_id="CMP-PKG-1",
        project_id=project.project_id,
        run_id=generate_id("run", "compute-1"),
    )

    # The package carries the frozen scientific design verbatim, the
    # frozen Goal version, and the declared outputs/resources.
    assert package.scientific_parameters == SCIENTIFIC_INPUTS
    assert package.goal_version == "v1"
    assert package.goal_id == "GOAL-1"
    assert package.objective == "Reproduce the reported GCMC adsorption isotherms."
    assert package.declared_outputs == [{"name": "uptake.csv"}]
    assert package.resource_requirements == {"resource_ids": ["RES-021"]}
    assert [entry["parameter"] for entry in package.input_files] == [
        "force_field",
        "k_point_mesh",
        "cutoff",
        "convergence",
    ]

    # Schema-gated: the generated package validates against its schema.
    assert validate_object(COMPUTE_EXECUTION_PACKAGE_SCHEMA, package.to_dict()) == []


def test_generator_refuses_a_draft_goal_from_the_registry(tmp_path: Path) -> None:
    root = build_simulation_workspace(tmp_path)
    draft_goal = read_goal(root, "GOAL-1")
    assert not draft_goal.frozen
    with pytest.raises(GoalNotFrozenError):
        build_compute_execution_package(
            draft_goal,
            package_id="CMP-PKG-1",
            project_id="RP-001",
            run_id=generate_id("run", "compute-1"),
        )


def test_backend_records_the_package_in_the_workspace_compute_tree(
    tmp_path: Path,
) -> None:
    root = build_simulation_workspace(tmp_path)
    freeze_simulation_workspace(root)
    package = build_compute_execution_package(
        read_goal(root, "GOAL-1"),
        package_id="CMP-PKG-1",
        project_id=read_project_state(root).project_id,
        run_id=generate_id("run", "compute-1"),
    )
    doc = package.to_dict()

    backend = FilesystemStateBackend(root)
    backend.write(COMPUTE_EXECUTION_PACKAGE_SCHEMA, "CMP-PKG-1", doc)
    stored = json.loads(
        (root / "compute" / "CMP-PKG-1.json").read_text(encoding="utf-8")
    )
    assert stored == doc

    # Malformed packages are refused loudly and nothing is written.
    malformed = dict(doc)
    del malformed["goal_version"]
    with pytest.raises(SchemaValidationError):
        backend.write(COMPUTE_EXECUTION_PACKAGE_SCHEMA, "CMP-PKG-2", malformed)
    assert not (root / "compute" / "CMP-PKG-2.json").exists()

    # The registered package is the same record the gate validates.
    validate_and_reject(COMPUTE_EXECUTION_PACKAGE_SCHEMA, stored)
