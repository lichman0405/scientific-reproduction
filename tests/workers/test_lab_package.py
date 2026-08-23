"""Tests for the Lab Execution Package generator (issue #158).

The frozen Goal Contract comes from the real plan freeze flow
(``context_helpers.frozen_goal``) for the end-to-end case and from
``dataclasses.replace`` for the in-memory record-level cases. Every
happy-path result is checked by the generator's own persistence gate
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
    make_goal,
    make_resource,
)

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AvailabilityState,
    GoalContract,
    LabExecutionPackage,
    ResourceType,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.plan import InvalidPlanVersionError
from scientific_reproduction.planning.resources import read_resource
from scientific_reproduction.workers.context import GoalNotFrozenError
from scientific_reproduction.workers.lab_package import (
    LabPackageBuildError,
    LabPackageDataError,
    LabPackageResourceError,
    generate_lab_execution_package,
)

#: The authored procedure used across the tests, following the shared
#: step vocabulary (``action`` + ``inputs``/``outputs``/``trace_refs``),
#: with a benchmark-style ``step`` key passing through verbatim.
PROCEDURE = (
    {
        "step": "S1",
        "action": "Weigh 0.25 g of activated FDM-201 into the sample cell.",
        "inputs": ["FDM-201"],
        "outputs": ["loaded_cell"],
        "trace_refs": ["GOAL-EXE-20"],
    },
    {
        "step": "S2",
        "action": "Record the N2 adsorption isotherm at 77 K.",
        "inputs": ["loaded_cell"],
        "outputs": ["raw_isotherm"],
        "trace_refs": [],
    },
)


def frozen_record(goal: GoalContract) -> GoalContract:
    """The frozen record a plan freeze produces: frozen, formal version."""
    return replace(goal, frozen=True, version="v1")


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
    """AC-01: the frozen goal deterministically yields a schema-valid package."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root, "GOAL-1")
    resource = read_resource(root, "RES-1")

    package = generate_lab_execution_package(
        root,
        goal,
        [resource],
        "RUN-2026-001",
        procedure=PROCEDURE,
        critical_control_variables=(
            {"name": "activation_temperature", "value": "423 K"},
        ),
        prohibited_changes=("Do not change the activation temperature.",),
        required_operator_records=("record-01-synthesis-lab-notebook",),
        safety_notes=("Liquid N2 handling requires cryogenic gloves.",),
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
    assert package.procedure == [dict(step) for step in PROCEDURE]
    assert package.critical_control_variables == [
        {"name": "activation_temperature", "value": "423 K"}
    ]
    assert package.prohibited_changes == [
        "Do not change the activation temperature."
    ]
    assert package.required_operator_records == [
        "record-01-synthesis-lab-notebook"
    ]
    assert package.safety_notes == [
        "Liquid N2 handling requires cryogenic gloves."
    ]
    # The result passes the generator's own persistence gate.
    validate_and_reject("lab-execution-package", package.to_dict())


def test_package_is_deterministic(tmp_path: Path) -> None:
    """Same inputs in, same package out -- no wall clock, no randomness."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root, "GOAL-1")
    resource = read_resource(root, "RES-1")
    kwargs = {"procedure": PROCEDURE, "prohibited_changes": ("keep", "order")}

    first = generate_lab_execution_package(
        root, goal, [resource], "RUN-2026-001", **kwargs
    )
    second = generate_lab_execution_package(
        root, goal, [resource], "RUN-2026-001", **kwargs
    )
    assert first.to_dict() == second.to_dict()
    # The resource input order does not matter: tables are id-sorted.
    reordered = generate_lab_execution_package(
        root, goal, list(reversed([resource])), "RUN-2026-001", **kwargs
    )
    assert reordered.to_dict() == first.to_dict()


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


def test_procedure_steps_follow_the_shared_step_vocabulary(
    tmp_path: Path,
) -> None:
    """One step vocabulary: action + string lists; extra keys pass through."""
    root = init_project(tmp_path)
    goal = frozen_record(make_goal("GOAL-1"))

    package = generate_lab_execution_package(
        root, goal, [], "RUN-2026-001", procedure=PROCEDURE
    )
    assert package.procedure == [dict(step) for step in PROCEDURE]

    bad_steps = (
        {"inputs": ["x"]},  # missing action
        {"action": "  "},  # blank action
        {"action": "mix", "inputs": [1]},  # non-str input
        {"action": "mix", "outputs": "isotherm"},  # not a list
        {"action": "mix", "trace_refs": ("REF",)},  # not a list
    )
    for bad in bad_steps:
        with pytest.raises(LabPackageBuildError):
            generate_lab_execution_package(
                root, goal, [], "RUN-2026-001", procedure=(bad,)
            )
    with pytest.raises(TypeError, match="mapping"):
        generate_lab_execution_package(
            root, goal, [], "RUN-2026-001",
            procedure=cast(Any, ("not a step",)),
        )


def test_authoring_lists_are_validated_and_order_preserved(
    tmp_path: Path,
) -> None:
    """Authoring list order is meaningful on the operator sheet."""
    root = init_project(tmp_path)
    goal = frozen_record(make_goal("GOAL-1"))

    package = generate_lab_execution_package(
        root,
        goal,
        [],
        "RUN-2026-001",
        prohibited_changes=("change B", "change A"),
        required_operator_records=("record 2", "record 1"),
        safety_notes=("note 2", "note 1"),
        critical_control_variables=({"second": True}, {"first": True}),
    )
    assert package.prohibited_changes == ["change B", "change A"]
    assert package.required_operator_records == ["record 2", "record 1"]
    assert package.safety_notes == ["note 2", "note 1"]
    assert package.critical_control_variables == [
        {"second": True},
        {"first": True},
    ]

    with pytest.raises(TypeError):
        generate_lab_execution_package(
            root, goal, [], "RUN-2026-001",
            prohibited_changes=cast(Any, ("ok", 1)),
        )
    with pytest.raises(LabPackageDataError):
        generate_lab_execution_package(
            root, goal, [], "RUN-2026-001", safety_notes=("",)
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
