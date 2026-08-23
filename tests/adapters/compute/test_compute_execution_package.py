"""Tests for the runtime compute execution package (issue #161).

The computation side of the runtime execution handoff: the
``ComputeExecutionPackage`` model/schema, the frozen-Goal generator
(``adapters.compute.package.build_compute_execution_package``) and the
adapter handoff gate (``LocalComputeAdapter.prepare(..., package=...)``).

Covered acceptance:
  * the package is a registered normative object: model registry, schema
    file, schema validation and canonical state tree (``compute/``);
  * the generator derives from a frozen Goal and records the scientific
    parameters verbatim (force field, k-point mesh, cutoffs, convergence
    criteria), the input-file creation instructions, the declared
    outputs, the software/environment declarations and the resource
    requirements, and carries the frozen Goal version;
  * the generator is schema-gated and deterministic, and rejects an
    unfrozen Goal loudly;
  * the adapter handoff consumes the validated package, persists it at
    ``<state_dir>/packages/<package_id>.json``, and refuses malformed
    packages loudly before anything is written.

Determinism: fixed goal fixtures, an injected single-stamp clock, and
``tmp_path`` state directories; no wall clock, no randomness, no real
processes (``prepare`` never touches the launcher).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scientific_reproduction.adapters.compute.local import (
    JOBS_STATE_DIR,
    PACKAGES_STATE_DIR,
    ComputePackageConflictError,
    ComputePackageRecordError,
    LocalComputeAdapter,
    RunContext,
)
from scientific_reproduction.adapters.compute.package import (
    COMPUTE_EXECUTION_PACKAGE_SCHEMA,
    ComputeExecutionPackageDataError,
    GoalNotFrozenError,
    build_compute_execution_package,
    validate_compute_execution_package,
)
from scientific_reproduction.core import models as m
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    load_schema,
    validate_and_reject,
    validate_object,
)
from scientific_reproduction.core.state_backend import (
    SCHEMA_TO_STATE_DIR,
    FilesystemStateBackend,
)

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: The frozen computation design the generator must carry verbatim:
#: force field, k-point mesh, cutoff and convergence criteria.
SCIENTIFIC_INPUTS: list[dict[str, object]] = [
    {"name": "force_field", "value": "UFF"},
    {"name": "k_point_mesh", "value": "2x2x2"},
    {"name": "cutoff", "value": "12.5", "unit": "A"},
    {"name": "convergence", "value": "1e-6", "criterion": "energy_tolerance"},
]

#: Declared outputs of the frozen computation goal.
SCIENTIFIC_OUTPUTS: list[dict[str, object]] = [
    {"name": "uptake.csv", "description": "simulated adsorption uptake"},
]


class FakeClock:
    """Injectable clock: the single fixed stamp repeats forever."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp

    def __call__(self) -> str:
        return self._stamp


def make_frozen_goal(
    goal_id: str = "GOAL-1",
    *,
    frozen: bool = True,
    inputs: list[dict[str, object]] | None = None,
    outputs: list[dict[str, object]] | None = None,
    resource_ids: tuple[str, ...] = (),
    version: str = "v1",
) -> m.GoalContract:
    """Build a schema-valid (frozen) computation Goal."""
    return m.GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported simulation ({goal_id}).",
        unit_process_type="simulation",
        track=m.GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the reported GCMC adsorption isotherms.",
        requirement_ids=["REQ-1"],
        dependencies=[],
        acceptance=m.GoalAcceptance(criteria_ref="ACC-1", frozen=frozen),
        analysis_protocol_ref="ANL-1",
        replication=m.GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=version,
        frozen=frozen,
        inputs=list(inputs) if inputs is not None else [],
        outputs=list(outputs) if outputs is not None else [],
        resource_ids=list(resource_ids),
    )


def make_package(
    *,
    package_id: str = "CMP-PKG-1",
    goal: m.GoalContract | None = None,
) -> m.ComputeExecutionPackage:
    """Build a runtime execution package for the default frozen goal."""
    return build_compute_execution_package(
        goal if goal is not None else make_frozen_goal(
            inputs=SCIENTIFIC_INPUTS,
            outputs=SCIENTIFIC_OUTPUTS,
            resource_ids=("RES-011", "RES-021"),
        ),
        package_id=package_id,
        project_id="RP-001",
        run_id=generate_id("run", "compute-1"),
    )


def make_context(run_id: str | None = None) -> RunContext:
    """A default run context; ``prepare`` never touches the launcher."""
    return RunContext(
        run_id=run_id if run_id is not None else generate_id("run", "compute-1"),
        command=(sys.executable, "-c", "pass"),
        working_directory=".",
        outputs=("result.txt",),
    )


def make_adapter(state_dir: Path) -> LocalComputeAdapter:
    """An adapter over ``state_dir`` with a deterministic clock."""
    return LocalComputeAdapter(state_dir, now=FakeClock())


def read_package_file(state_dir: Path, package_id: str) -> dict[str, object]:
    """The on-disk staged package record as parsed JSON."""
    return json.loads(
        (state_dir / PACKAGES_STATE_DIR / f"{package_id}.json").read_text(
            encoding="utf-8"
        )
    )


# ---------------------------------------------------------------------------
# Model / schema / state-tree registration
# ---------------------------------------------------------------------------


def test_package_is_a_registered_normative_object() -> None:
    """The package model is registered exactly like the lab package."""
    assert "compute-execution-package" in m.MODEL_REGISTRY
    assert "compute-execution-package" in m.SCHEMA_NAMES
    assert m.MODEL_REGISTRY["compute-execution-package"] is m.ComputeExecutionPackage
    assert m.ComputeExecutionPackage.schema_name == COMPUTE_EXECUTION_PACKAGE_SCHEMA
    schema = load_schema(COMPUTE_EXECUTION_PACKAGE_SCHEMA)
    assert set(schema["required"]) == {
        "package_id",
        "project_id",
        "goal_id",
        "goal_version",
        "run_id",
        "objective",
        "scientific_parameters",
        "input_files",
        "declared_outputs",
        "software_environment",
        "resource_requirements",
    }
    assert SCHEMA_TO_STATE_DIR[COMPUTE_EXECUTION_PACKAGE_SCHEMA] == "compute"


def test_package_model_round_trips_to_dict_from_dict() -> None:
    package = make_package()
    doc = package.to_dict()
    assert m.ComputeExecutionPackage.from_dict(doc) == package
    assert m.ComputeExecutionPackage.from_dict(doc).to_dict() == doc


# ---------------------------------------------------------------------------
# The generator: frozen Goal in, schema-gated deterministic package out
# ---------------------------------------------------------------------------


def test_generator_records_scientific_parameters_verbatim() -> None:
    """The frozen design -- force field, k-point mesh, cutoff,
    convergence criteria -- is recorded exactly as frozen."""
    package = make_package()
    assert package.scientific_parameters == SCIENTIFIC_INPUTS
    assert package.declared_outputs == SCIENTIFIC_OUTPUTS
    assert package.resource_requirements == {
        "resource_ids": ["RES-011", "RES-021"]
    }
    assert package.goal_version == "v1"
    assert package.goal_id == "GOAL-1"
    assert package.project_id == "RP-001"
    assert package.track is m.GoalTrack.STRICT_REPRODUCTION
    assert package.objective == "Reproduce the reported GCMC adsorption isotherms."


def test_generator_records_one_input_file_instruction_per_frozen_input() -> None:
    package = make_package()
    assert [entry["parameter"] for entry in package.input_files] == [
        "force_field",
        "k_point_mesh",
        "cutoff",
        "convergence",
    ]
    # Every instruction carries the frozen entry verbatim and the SS4
    # materialization obligation.
    for entry, frozen in zip(package.input_files, SCIENTIFIC_INPUTS):
        assert entry["materializes"] == frozen
        assert entry["instructions"].strip()


def test_generator_input_file_parameter_names_fallback_positionally() -> None:
    """A frozen input without a name still gets a stable instruction key."""
    goal = make_frozen_goal(inputs=[{"value": "UFF"}, {"value": "2x2x2"}])
    package = make_package(goal=goal)
    assert [entry["parameter"] for entry in package.input_files] == [
        "input-parameter-1",
        "input-parameter-2",
    ]


def test_generator_records_software_environment_declarations() -> None:
    goal = make_frozen_goal(
        inputs=[
            {"name": "force_field", "value": "UFF"},
            {"software": "GROMACS", "version": "2024.1", "environment": "module"},
        ]
    )
    package = make_package(goal=goal)
    declared = package.software_environment["declared"]
    assert declared == [
        {"software": "GROMACS", "version": "2024.1", "environment": "module"}
    ]


def test_generator_output_is_schema_valid() -> None:
    package = make_package()
    assert validate_object(COMPUTE_EXECUTION_PACKAGE_SCHEMA, package.to_dict()) == []
    validate_and_reject(COMPUTE_EXECUTION_PACKAGE_SCHEMA, package.to_dict())


def test_generator_is_deterministic() -> None:
    first = make_package()
    second = make_package()
    assert first.to_dict() == second.to_dict()
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_generator_rejects_unfrozen_goal_loudly() -> None:
    goal = make_frozen_goal(frozen=False, inputs=SCIENTIFIC_INPUTS)
    with pytest.raises(GoalNotFrozenError) as exc:
        build_compute_execution_package(
            goal, package_id="CMP-PKG-1", project_id="RP-001", run_id="RUN-1"
        )
    assert "GOAL-1" in str(exc.value)
    assert "not frozen" in str(exc.value)


def test_generator_rejects_non_goal_with_type_error() -> None:
    with pytest.raises(TypeError):
        build_compute_execution_package(  # type: ignore[arg-type]
            {"goal_id": "GOAL-1", "frozen": True},
            package_id="CMP-PKG-1",
            project_id="RP-001",
            run_id="RUN-1",
        )


# ---------------------------------------------------------------------------
# The handoff gate: malformed packages refused loudly, nothing written
# ---------------------------------------------------------------------------


def test_validate_rejects_missing_required_field_loudly() -> None:
    malformed = make_package().to_dict()
    del malformed["scientific_parameters"]
    with pytest.raises(SchemaValidationError) as exc:
        validate_compute_execution_package(malformed)
    assert exc.value.obj_type == COMPUTE_EXECUTION_PACKAGE_SCHEMA


def test_validate_rejects_wrong_typed_field_loudly() -> None:
    malformed = make_package().to_dict()
    malformed["scientific_parameters"] = "UFF"  # not an array
    with pytest.raises(SchemaValidationError):
        validate_compute_execution_package(malformed)


def test_validate_rejects_wrong_handoff_type_with_type_error() -> None:
    with pytest.raises(TypeError):
        validate_compute_execution_package(42)  # type: ignore[arg-type]


def test_validate_rejects_blank_package_id() -> None:
    malformed = make_package().to_dict()
    malformed["package_id"] = "   "
    with pytest.raises(ComputeExecutionPackageDataError):
        validate_compute_execution_package(malformed)


def test_backend_records_package_in_the_compute_state_tree(tmp_path: Path) -> None:
    """A backend write lands at ``<root>/compute/<package_id>.json``."""
    backend = FilesystemStateBackend(tmp_path)
    doc = make_package().to_dict()
    backend.write(COMPUTE_EXECUTION_PACKAGE_SCHEMA, "CMP-PKG-1", doc)
    stored = json.loads((tmp_path / "compute" / "CMP-PKG-1.json").read_text(
        encoding="utf-8"
    ))
    assert stored == doc

    malformed = dict(doc)
    del malformed["objective"]
    with pytest.raises(SchemaValidationError):
        backend.write(COMPUTE_EXECUTION_PACKAGE_SCHEMA, "CMP-PKG-2", malformed)
    assert not (tmp_path / "compute" / "CMP-PKG-2.json").exists()


# ---------------------------------------------------------------------------
# The adapter handoff: prepare() consumes the validated package
# ---------------------------------------------------------------------------


def test_prepare_with_package_stages_job_and_persists_package(
    tmp_path: Path,
) -> None:
    adapter = make_adapter(tmp_path)
    ctx = make_context()
    package = make_package()
    prepared = adapter.prepare(ctx, package=package)
    assert prepared.run_id == ctx.run_id
    # Durable package record at <state_dir>/packages/<package_id>.json.
    assert (tmp_path / PACKAGES_STATE_DIR / "CMP-PKG-1.json").is_file()
    assert read_package_file(tmp_path, "CMP-PKG-1") == package.to_dict()
    # The job staging proceeded as usual.
    assert (tmp_path / JOBS_STATE_DIR / f"{prepared.job_id}.json").is_file()


def test_prepare_accepts_a_plain_mapping_package(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    prepared = adapter.prepare(make_context(), package=make_package().to_dict())
    assert prepared.state.value == "prepared"
    assert read_package_file(tmp_path, "CMP-PKG-1") == make_package().to_dict()


def test_prepare_with_malformed_package_refuses_loudly_and_writes_nothing(
    tmp_path: Path,
) -> None:
    adapter = make_adapter(tmp_path)
    malformed = make_package().to_dict()
    del malformed["scientific_parameters"]
    with pytest.raises(SchemaValidationError):
        adapter.prepare(make_context(), package=malformed)
    # The gate runs before anything is written: no package record, no job
    # record, not even the packages/ directory.
    assert not (tmp_path / PACKAGES_STATE_DIR).exists()
    assert not (tmp_path / JOBS_STATE_DIR).exists()


def test_prepare_package_wrong_type_raises_type_error(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    with pytest.raises(TypeError):
        adapter.prepare(make_context(), package=42)  # type: ignore[arg-type]
    assert not (tmp_path / PACKAGES_STATE_DIR).exists()
    assert not (tmp_path / JOBS_STATE_DIR).exists()


def test_prepare_unsafe_package_id_refused(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    package = make_package(package_id="../escape")
    with pytest.raises(ComputeExecutionPackageDataError):
        adapter.prepare(make_context(), package=package)
    assert not (tmp_path / PACKAGES_STATE_DIR).exists()


def test_prepare_package_is_idempotent_for_identical_content(
    tmp_path: Path,
) -> None:
    ctx = make_context()
    package = make_package()
    first = make_adapter(tmp_path).prepare(ctx, package=package)
    second = make_adapter(tmp_path).prepare(ctx, package=package)
    assert first.job_id == second.job_id
    assert len(list((tmp_path / PACKAGES_STATE_DIR).iterdir())) == 1


def test_prepare_package_conflict_with_different_content_refused(
    tmp_path: Path,
) -> None:
    ctx = make_context()
    original = make_package()
    make_adapter(tmp_path).prepare(ctx, package=original)
    conflicting = build_compute_execution_package(
        make_frozen_goal(inputs=[{"name": "force_field", "value": "DREIDING"}]),
        package_id="CMP-PKG-1",
        project_id="RP-001",
        run_id=generate_id("run", "compute-1"),
    )
    with pytest.raises(ComputePackageConflictError):
        make_adapter(tmp_path).prepare(ctx, package=conflicting)
    # The original record survives the refusal untouched.
    assert read_package_file(tmp_path, "CMP-PKG-1") == original.to_dict()


def test_prepare_corrupt_package_record_raises_record_error(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    adapter.prepare(make_context(), package=make_package())
    record = tmp_path / PACKAGES_STATE_DIR / "CMP-PKG-1.json"
    record.write_bytes(b"\xff\xfe{")  # not decodable as UTF-8
    with pytest.raises(ComputePackageRecordError):
        make_adapter(tmp_path).prepare(make_context(), package=make_package())


def test_prepare_without_package_still_stages_the_job(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path)
    prepared = adapter.prepare(make_context())
    assert prepared.state.value == "prepared"
    assert not (tmp_path / PACKAGES_STATE_DIR).exists()
