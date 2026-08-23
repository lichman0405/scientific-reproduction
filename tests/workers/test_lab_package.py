"""Tests for the Lab Execution Package generator (issue #158).

The frozen Goal Contract comes from the real plan freeze flow
(``context_helpers.frozen_goal``) for the end-to-end case and from
``dataclasses.replace`` for the in-memory record-level cases. The
package's procedure and constraint columns derive from the frozen
goal's typed contract fields (issue #156): ``goal.procedure`` ->
``procedure``, ``execution_constraints.forbidden_changes`` ->
``prohibited_changes``, ``execution_constraints.safety_notes`` ->
``safety_notes``; the environment constraints have no lab-package
field and are not rendered (documented no-op). Every happy-path result
is checked by the generator's own persistence gate
(``validate_and_reject("lab-execution-package", ...)``), and the
milestone benchmark fixtures are re-validated through that same gate --
the same validator the generator uses, not a fork of the benchmark
gate.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from context_helpers import (
    build_complete_workspace,
    frozen_goal,
    init_project,
    make_acceptance,
    make_analysis_protocol,
    make_closure,
    make_goal,
    make_item,
    make_requirement,
    make_resource,
)

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AvailabilityState,
    GoalContract,
    GoalExecutionConstraints,
    GoalProcedureStep,
    LabExecutionPackage,
    ResourceType,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    InvalidPlanVersionError,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
)
from scientific_reproduction.planning.resources import read_resource, register_resource
from scientific_reproduction.workers.context import GoalNotFrozenError
from scientific_reproduction.workers.lab_package import (
    LabPackageBuildError,
    LabPackageDataError,
    LabPackageResourceError,
    generate_lab_execution_package,
)

#: The typed procedure steps the frozen goal carries, following the
#: shared step vocabulary (``action`` + ``inputs``/``outputs``/
#: ``trace_refs``).
PROCEDURE_STEPS: tuple[GoalProcedureStep, ...] = (
    GoalProcedureStep(
        action="Weigh 0.25 g of activated FDM-201 into the sample cell.",
        inputs=["FDM-201"],
        outputs=["loaded_cell"],
        trace_refs=["GOAL-EXE-20"],
    ),
    GoalProcedureStep(
        action="Record the N2 adsorption isotherm at 77 K.",
        inputs=["loaded_cell"],
        outputs=["raw_isotherm"],
    ),
)

#: The typed execution constraints the frozen goal carries.
EXECUTION_CONSTRAINTS = GoalExecutionConstraints(
    environment={"temperature_K": 77},
    forbidden_changes=[
        "Do not change the activation temperature.",
        "Do not change the measurement gas.",
    ],
    safety_notes=["Liquid N2 handling requires cryogenic gloves."],
)


def frozen_record(goal: GoalContract) -> GoalContract:
    """The frozen record a plan freeze produces: frozen, formal version."""
    return replace(goal, frozen=True, version="v1")


def typed_goal() -> GoalContract:
    """An in-memory frozen goal carrying the typed contract fields."""
    return frozen_record(
        make_goal(
            "GOAL-1",
            outputs=({"name": "analysis_input_manifest"},),
            procedure=PROCEDURE_STEPS,
            execution_constraints=EXECUTION_CONSTRAINTS,
        )
    )


def build_typed_goal_workspace(root: Path) -> Path:
    """A freeze-eligible single-goal workspace whose GOAL-1 carries the
    typed contract fields, authored through the real registry and frozen
    by the real plan freeze flow (``context_helpers.frozen_goal``)."""
    init_project(root)
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-1",))
    )
    register_requirement(
        root, make_requirement("REQ-1", goal_ids=("GOAL-1",))
    )
    register_goal(
        root,
        make_goal(
            "GOAL-1",
            outputs=({"name": "analysis_input_manifest"},),
            resource_ids=("RES-1",),
            procedure=PROCEDURE_STEPS,
            execution_constraints=EXECUTION_CONSTRAINTS,
        ),
    )
    register_acceptance(root, make_acceptance())
    register_analysis_protocol(root, make_analysis_protocol("ANP-1"))
    register_closure_contract(root, make_closure())
    register_resource(root, make_resource("RES-1"))
    return root


def package_fixture_files() -> list[Path]:
    """The 10 milestone lab-execution-package fixtures (7 + 3 compute)."""
    pkg_dir = (
        Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "fdm201"
        / "execution_packages"
    )
    return sorted(
        list((pkg_dir / "experiment").glob("*.yaml"))
        + list((pkg_dir / "computation").glob("*.yaml"))
    )


PACKAGE_FILES = package_fixture_files()


def test_generates_schema_valid_package_from_the_real_freeze_flow(
    tmp_path: Path,
) -> None:
    """AC-01: the frozen goal deterministically yields a schema-valid
    package whose procedure and constraint columns derive from the typed
    contract fields the freeze persisted (the typed round trip is proven
    by the frozen record's own content)."""
    root = build_typed_goal_workspace(tmp_path)
    goal = frozen_goal(root, "GOAL-1")
    resource = read_resource(root, "RES-1")

    # The freeze round-tripped the typed fields: what the package derives
    # from is the frozen record's typed content, not the authoring input.
    assert goal.procedure == list(PROCEDURE_STEPS)
    assert goal.execution_constraints == EXECUTION_CONSTRAINTS

    package = generate_lab_execution_package(
        root,
        goal,
        [resource],
        "RUN-2026-001",
        critical_control_variables=(
            {"name": "activation_temperature", "value": "423 K"},
        ),
        required_operator_records=("record-01-synthesis-lab-notebook",),
    )

    assert isinstance(package, LabExecutionPackage)
    assert package.package_id == generate_id(
        "package", read_project_state(root).project_id, "GOAL-1", "RUN-2026-001"
    )
    assert package.project_id == read_project_state(root).project_id
    assert package.goal_id == "GOAL-1"
    assert package.run_id == "RUN-2026-001"
    assert package.objective == goal.objective
    assert package.track == goal.track
    # The package carries the frozen record's exact version.
    assert package.goal_version == goal.version == "v1"
    assert package.required_return == ["analysis_input_manifest"]
    assert package.reagents == [
        {
            "name": "resource RES-1",
            "resource_id": "RES-1",
            "availability_state": AvailabilityState.AVAILABLE.value,
        }
    ]
    assert package.instruments == []
    # Derived from the typed contract fields, not caller-authored.
    assert package.procedure == [step.to_dict() for step in PROCEDURE_STEPS]
    assert package.prohibited_changes == list(
        EXECUTION_CONSTRAINTS.forbidden_changes
    )
    assert package.safety_notes == list(EXECUTION_CONSTRAINTS.safety_notes)
    # The environment constraints have no lab-package field (the schema
    # declares none): documented no-op, the package never carries it.
    assert "environment" not in package.to_dict()
    assert package.critical_control_variables == [
        {"name": "activation_temperature", "value": "423 K"}
    ]
    assert package.required_operator_records == [
        "record-01-synthesis-lab-notebook"
    ]
    # The result passes the generator's own persistence gate.
    validate_and_reject("lab-execution-package", package.to_dict())


def test_package_is_deterministic(tmp_path: Path) -> None:
    """Same inputs in, same package out -- no wall clock, no randomness."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_record(
        make_goal(
            "GOAL-1",
            procedure=PROCEDURE_STEPS,
            execution_constraints=EXECUTION_CONSTRAINTS,
        )
    )
    resource = read_resource(root, "RES-1")

    first = generate_lab_execution_package(
        root, goal, [resource], "RUN-2026-001"
    )
    second = generate_lab_execution_package(
        root, goal, [resource], "RUN-2026-001"
    )
    assert first.to_dict() == second.to_dict()
    # The resource input order does not matter: tables are id-sorted.
    reordered = generate_lab_execution_package(
        root, goal, list(reversed([resource])), "RUN-2026-001"
    )
    assert reordered.to_dict() == first.to_dict()


def test_derivation_from_the_typed_goal_fields(tmp_path: Path) -> None:
    """The typed contract fields project onto the package columns exactly:
    procedure steps -> procedure (goal order, serialized step vocabulary),
    forbidden_changes -> prohibited_changes, safety_notes -> safety_notes;
    the environment constraints are not rendered."""
    root = init_project(tmp_path)
    package = generate_lab_execution_package(
        root, typed_goal(), [], "RUN-2026-001"
    )

    assert package.procedure == [step.to_dict() for step in PROCEDURE_STEPS]
    assert package.procedure[0]["trace_refs"] == ["GOAL-EXE-20"]
    assert package.procedure[1]["trace_refs"] == []
    assert package.prohibited_changes == [
        "Do not change the activation temperature.",
        "Do not change the measurement gas.",
    ]
    assert package.safety_notes == [
        "Liquid N2 handling requires cryogenic gloves."
    ]
    assert "environment" not in package.to_dict()


def test_migration_defaults_derive_as_empty_columns(tmp_path: Path) -> None:
    """A frozen goal with the documented migration state (empty typed
    fields) yields an explicitly empty procedure and empty constraint
    columns -- the derivation never fabricates content."""
    root = init_project(tmp_path)
    package = generate_lab_execution_package(
        root, frozen_record(make_goal("GOAL-1")), [], "RUN-2026-001"
    )
    assert package.procedure == []
    assert package.prohibited_changes == []
    assert package.safety_notes == []


def test_typed_goal_field_violations_are_rejected(tmp_path: Path) -> None:
    """A frozen record that violates the shared step vocabulary or the
    constraint-column rules is refused loudly -- the generator derives,
    it does not repair."""
    root = init_project(tmp_path)

    blank_action = frozen_record(
        make_goal(
            "GOAL-1", procedure=(GoalProcedureStep(action="  "),)
        )
    )
    with pytest.raises(LabPackageBuildError, match="'action'"):
        generate_lab_execution_package(root, blank_action, [], "RUN-2026-001")

    non_str_input = frozen_record(
        make_goal(
            "GOAL-1",
            procedure=(
                GoalProcedureStep(action="mix", inputs=cast(Any, [1])),
            ),
        )
    )
    with pytest.raises(LabPackageBuildError, match="'inputs'"):
        generate_lab_execution_package(root, non_str_input, [], "RUN-2026-001")

    non_step = replace(
        frozen_record(make_goal("GOAL-1")),
        procedure=cast(Any, ("not a step",)),
    )
    with pytest.raises(TypeError, match="GoalProcedureStep"):
        generate_lab_execution_package(root, non_step, [], "RUN-2026-001")

    non_constraints = replace(
        frozen_record(make_goal("GOAL-1")),
        execution_constraints=cast(Any, "not constraints"),
    )
    with pytest.raises(TypeError, match="GoalExecutionConstraints"):
        generate_lab_execution_package(
            root, non_constraints, [], "RUN-2026-001"
        )

    empty_forbidden = frozen_record(
        make_goal(
            "GOAL-1",
            execution_constraints=GoalExecutionConstraints(
                forbidden_changes=["  "]
            ),
        )
    )
    with pytest.raises(LabPackageBuildError, match="forbidden_changes"):
        generate_lab_execution_package(
            root, empty_forbidden, [], "RUN-2026-001"
        )

    empty_note = frozen_record(
        make_goal(
            "GOAL-1",
            execution_constraints=GoalExecutionConstraints(safety_notes=[""]),
        )
    )
    with pytest.raises(LabPackageBuildError, match="safety_notes"):
        generate_lab_execution_package(root, empty_note, [], "RUN-2026-001")

    non_str_forbidden = replace(
        frozen_record(make_goal("GOAL-1")),
        execution_constraints=GoalExecutionConstraints(
            forbidden_changes=cast(Any, ["ok", 1])
        ),
    )
    with pytest.raises(TypeError, match="forbidden_changes"):
        generate_lab_execution_package(
            root, non_str_forbidden, [], "RUN-2026-001"
        )


def test_rejects_a_non_frozen_goal(tmp_path: Path) -> None:
    """The package derives from the frozen Goal Contract only."""
    root = init_project(tmp_path)
    draft = make_goal("GOAL-1")
    with pytest.raises(GoalNotFrozenError, match="frozen goal contract"):
        generate_lab_execution_package(root, draft, [], "RUN-2026-001")


def test_rejects_a_frozen_goal_with_a_draft_version(tmp_path: Path) -> None:
    """A frozen record without a formal version cannot be dispatched."""
    root = init_project(tmp_path)
    draft_frozen = replace(make_goal("GOAL-1"), frozen=True)
    with pytest.raises(LabPackageBuildError, match="formal version"):
        generate_lab_execution_package(root, draft_frozen, [], "RUN-2026-001")


def test_rejects_a_goal_with_a_malformed_version(tmp_path: Path) -> None:
    root = init_project(tmp_path)
    malformed = replace(make_goal("GOAL-1"), frozen=True, version="vX")
    with pytest.raises(InvalidPlanVersionError):
        generate_lab_execution_package(root, malformed, [], "RUN-2026-001")


def test_a_goal_resource_without_a_record_raises(tmp_path: Path) -> None:
    """Referenced resources resolve to provided records -- nothing dropped."""
    root = init_project(tmp_path)
    goal = frozen_record(make_goal("GOAL-1", resource_ids=("RES-1", "RES-2")))
    with pytest.raises(LabPackageResourceError, match="RES-1"):
        generate_lab_execution_package(root, goal, [], "RUN-2026-001")
    with pytest.raises(LabPackageResourceError, match="RES-2"):
        generate_lab_execution_package(
            root, goal, [make_resource("RES-1")], "RUN-2026-001"
        )


def test_resource_tables_render_by_type_and_sort_by_id(tmp_path: Path) -> None:
    """REAGENT/CONSUMABLE -> reagents, INSTRUMENT -> instruments; the rest
    is not rendered; unreferenced records are excluded."""
    root = init_project(tmp_path)
    goal = frozen_record(
        make_goal(
            "GOAL-1",
            resource_ids=(
                "RES-INSTR",
                "RES-CONSUMABLE",
                "RES-REAGENT",
                "RES-SERVICE",
            ),
        )
    )
    reagent = make_resource("RES-REAGENT")
    consumable = replace(
        make_resource("RES-CONSUMABLE"),
        resource_type=ResourceType.CONSUMABLE,
        notes="keep at 4 C",
        human_gate_required=True,
    )
    instrument = replace(
        make_resource("RES-INSTR"), resource_type=ResourceType.INSTRUMENT
    )
    service = replace(
        make_resource("RES-SERVICE"), resource_type=ResourceType.EXTERNAL_SERVICE
    )
    unreferenced = make_resource("RES-UNREFERENCED")

    package = generate_lab_execution_package(
        root,
        goal,
        [unreferenced, service, instrument, reagent, consumable],
        "RUN-2026-001",
    )

    assert package.reagents == [
        {
            "name": "resource RES-CONSUMABLE",
            "resource_id": "RES-CONSUMABLE",
            "availability_state": "AVAILABLE",
            "notes": "keep at 4 C",
            "human_gate_required": True,
        },
        {
            "name": "resource RES-REAGENT",
            "resource_id": "RES-REAGENT",
            "availability_state": "AVAILABLE",
        },
    ]
    assert package.instruments == [
        {
            "name": "resource RES-INSTR",
            "resource_id": "RES-INSTR",
            "availability_state": "AVAILABLE",
        }
    ]
    for entry in package.reagents + package.instruments:
        assert entry["resource_id"] not in {"RES-SERVICE", "RES-UNREFERENCED"}


def test_required_return_derives_from_goal_outputs(tmp_path: Path) -> None:
    """Only the goal's declared string-named outputs become return tokens."""
    root = init_project(tmp_path)
    goal = frozen_record(
        make_goal(
            "GOAL-1",
            outputs=(
                {"name": "raw_isotherm_data"},
                {"name": "analysis_input_manifest"},
                {"name": "raw_isotherm_data"},  # duplicate: distinct tokens
                {"unit": "cm3/g"},  # no name: not a returnable artifact
                {"name": 42},  # non-str name: not a returnable artifact
            ),
        )
    )
    package = generate_lab_execution_package(root, goal, [], "RUN-2026-001")
    assert package.required_return == [
        "analysis_input_manifest",
        "raw_isotherm_data",
    ]


def test_authoring_lists_are_validated_and_order_preserved(
    tmp_path: Path,
) -> None:
    """The remaining authoring columns (critical control variables and
    operator records) have no typed goal source; their order is
    meaningful on the operator sheet and is preserved."""
    root = init_project(tmp_path)
    goal = frozen_record(make_goal("GOAL-1"))

    package = generate_lab_execution_package(
        root,
        goal,
        [],
        "RUN-2026-001",
        required_operator_records=("record 2", "record 1"),
        critical_control_variables=({"second": True}, {"first": True}),
    )
    assert package.required_operator_records == ["record 2", "record 1"]
    assert package.critical_control_variables == [
        {"second": True},
        {"first": True},
    ]

    with pytest.raises(TypeError):
        generate_lab_execution_package(
            root, goal, [], "RUN-2026-001",
            required_operator_records=cast(Any, ("ok", 1)),
        )
    with pytest.raises(LabPackageDataError):
        generate_lab_execution_package(
            root, goal, [], "RUN-2026-001", required_operator_records=("",)
        )
    with pytest.raises(TypeError):
        generate_lab_execution_package(
            root, goal, [], "RUN-2026-001",
            critical_control_variables=cast(Any, ("not a mapping",)),
        )


@pytest.mark.parametrize("run_id", ["", "  ", ".", "..", "a/b", r"a\b"])
def test_rejects_unsafe_run_ids(tmp_path: Path, run_id: str) -> None:
    """The run id maps to the handoff directory: it must stay a segment."""
    root = init_project(tmp_path)
    goal = frozen_record(make_goal("GOAL-1"))
    with pytest.raises(LabPackageDataError, match="run_id"):
        generate_lab_execution_package(root, goal, [], run_id)


def test_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public boundaries, stable messages."""
    root = init_project(tmp_path)
    goal = frozen_record(make_goal("GOAL-1"))
    with pytest.raises(TypeError, match="root"):
        generate_lab_execution_package(cast(Any, 42), goal, [], "RUN-2026-001")
    with pytest.raises(TypeError, match="goal"):
        generate_lab_execution_package(
            root, cast(Any, "GOAL-1"), [], "RUN-2026-001"
        )
    with pytest.raises(TypeError, match="run_id"):
        generate_lab_execution_package(root, goal, [], cast(Any, 42))
    with pytest.raises(TypeError, match="resources"):
        generate_lab_execution_package(
            root, goal, cast(Any, make_resource("RES-1")), "RUN-2026-001"
        )
    with pytest.raises(TypeError, match="entry 1"):
        generate_lab_execution_package(
            root, goal, cast(Any, [make_resource("RES-1"), object()]), "RUN-2026-001"
        )


def test_uninitialized_root_raises(tmp_path: Path) -> None:
    """The project state at ``root`` must exist (propagated registry error)."""
    goal = frozen_record(make_goal("GOAL-1"))
    with pytest.raises(ProjectNotInitializedError):
        generate_lab_execution_package(tmp_path, goal, [], "RUN-2026-001")


@pytest.mark.parametrize(
    "path",
    PACKAGE_FILES,
    ids=lambda p: f"{p.parent.name}/{p.name}",
)
def test_benchmark_execution_packages_pass_the_generators_gate(
    path: Path,
) -> None:
    """AC-03: the milestone fixtures validate through the same runtime
    gate the generator uses -- no forked validator."""
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    validate_and_reject("lab-execution-package", data)
    assert data["goal_version"] == "v1"
