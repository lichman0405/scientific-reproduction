"""Compute execution package support (issue #161).

The computation side of the runtime execution handoff. Until now the
translation from a frozen computation design (force fields, k-point
meshes, cutoffs, convergence criteria) to the input files a worker
creates was unrecorded, and ``RunContext`` carried no scientific
parameters. ``ComputeExecutionPackage``
(``schemas/compute-execution-package.schema.json``,
``core.models.ComputeExecutionPackage``) closes that gap: the
worker-facing artifact that carries the frozen Goal's scientific
parameters, the input-file creation instructions derived from them, the
declared outputs, the software/environment declarations, and the
resource requirements (11-COMPUTATION-SUBSYSTEM.md SS4).

``build_compute_execution_package`` is the generator: pure,
deterministic, schema-gated, and it only ever builds from a **frozen**
Goal -- an unfrozen Goal is refused loudly (``GoalNotFrozenError``),
because a runtime execution package exists only for a frozen design.
``validate_compute_execution_package`` is the handoff gate the compute
adapter's ``prepare`` calls before anything is written: malformed
packages are refused loudly (``SchemaValidationError``) and nothing is
persisted.
"""

from __future__ import annotations

from typing import Any, Mapping

from scientific_reproduction.core.models import ComputeExecutionPackage, GoalContract
from scientific_reproduction.core.schema_validation import validate_and_reject

#: The schema name of the runtime compute execution package
#: (``schemas/compute-execution-package.schema.json``).
COMPUTE_EXECUTION_PACKAGE_SCHEMA: str = "compute-execution-package"

#: Fixed input-file creation instruction, identical for every frozen
#: input: the worker materializes the frozen entry into an input file
#: and records the software/environment used to produce it (SS4). The
#: concrete file name is the worker's decision at materialization time.
_INPUT_FILE_INSTRUCTION: str = (
    "materialize the frozen input recorded under ``materializes`` into"
    " the computation input file and record the software name, version"
    " and environment used to produce it"
)

__all__ = [
    "COMPUTE_EXECUTION_PACKAGE_SCHEMA",
    "ComputeExecutionPackageError",
    "GoalNotFrozenError",
    "ComputeExecutionPackageDataError",
    "validate_compute_execution_package",
    "build_compute_execution_package",
]


class ComputeExecutionPackageError(ValueError):
    """Base error for compute execution package generation and handoff."""


class GoalNotFrozenError(ComputeExecutionPackageError):
    """Raised when a package is requested for a Goal that is not frozen.

    A runtime execution package only exists for a frozen Goal: the
    package executes a design, and an unfrozen design is still mutable
    (10-EXPERIMENT-SUBSYSTEM / 11-COMPUTATION-SUBSYSTEM SS4: the frozen
    Goal version links the package to the exact contract it executes).
    """


class ComputeExecutionPackageDataError(ComputeExecutionPackageError):
    """Raised when handoff data passes the schema but is unusable.

    The schema only constrains types; the handoff gate additionally
    requires a non-blank ``package_id`` (the state-tree stem).
    """


def validate_compute_execution_package(
    package: ComputeExecutionPackage | Mapping[str, Any],
) -> dict[str, Any]:
    """Schema-gate a runtime compute execution package; return its dict.

    The single handoff gate (``LocalComputeAdapter.prepare``): validates
    the package against ``schemas/compute-execution-package.schema.json``
    before anything is persisted and returns the canonical plain dict
    for the adapter to write.

    Args:
        package: a ``ComputeExecutionPackage`` model or a plain mapping
            of schema-keyed data.

    Returns:
        The package as a plain dict (canonical serialization input).

    Raises:
        TypeError: if ``package`` is neither a model nor a mapping.
        SchemaValidationError: if the package fails its schema -- loud,
            before anything is written.
        ComputeExecutionPackageDataError: if the package passes the
            schema but ``package_id`` is blank.
    """
    if isinstance(package, ComputeExecutionPackage):
        data = package.to_dict()
    elif isinstance(package, Mapping):
        data = dict(package)
    else:
        raise TypeError(
            "compute execution package must be a ComputeExecutionPackage"
            f" or a mapping of schema-keyed data, got {type(package).__name__}"
        )
    validate_and_reject(COMPUTE_EXECUTION_PACKAGE_SCHEMA, data)
    package_id = data.get("package_id")
    if not isinstance(package_id, str) or not package_id.strip():
        raise ComputeExecutionPackageDataError(
            "compute execution package requires a non-blank package_id"
        )
    return data


def build_compute_execution_package(
    goal: GoalContract,
    *,
    package_id: str,
    project_id: str,
    run_id: str,
) -> ComputeExecutionPackage:
    """Build the runtime execution package for a frozen Goal.

    Pure and deterministic (same frozen Goal in, same package out -- no
    wall clock, no randomness): the scientific parameters are the frozen
    Goal's ``inputs`` verbatim (force field, k-point mesh, cutoffs,
    convergence criteria, ...), the declared outputs are the frozen
    Goal's ``outputs``, the resource requirements carry the frozen
    Goal's ``resource_ids``, and the frozen Goal version links the
    package to the exact contract it materializes. The returned package
    is validated against its schema before it is handed back.

    Args:
        goal: the Goal the package executes; must be frozen.
        package_id: the package record id (state-tree stem).
        project_id: the project the Goal belongs to.
        run_id: the Run the package backs (``RunContext.run_id``).

    Returns:
        The schema-validated ``ComputeExecutionPackage``.

    Raises:
        TypeError: if ``goal`` is not a ``GoalContract``.
        GoalNotFrozenError: if the Goal is not frozen -- loud, nothing
            is built.
        SchemaValidationError: if the derived package fails its schema
            (a generator bug, never a Goal problem).
    """
    if not isinstance(goal, GoalContract):
        raise TypeError(
            "build_compute_execution_package expects a GoalContract,"
            f" got {type(goal).__name__}"
        )
    if not goal.frozen:
        raise GoalNotFrozenError(
            f"Goal {goal.goal_id!r} is not frozen: a compute execution"
            " package only exists for a frozen Goal"
        )
    inputs = list(goal.inputs)
    package = ComputeExecutionPackage(
        package_id=package_id,
        project_id=project_id,
        goal_id=goal.goal_id,
        goal_version=goal.version,
        run_id=run_id,
        objective=goal.objective,
        scientific_parameters=inputs,
        input_files=_input_file_instructions(inputs),
        declared_outputs=list(goal.outputs),
        software_environment=_software_environment(inputs),
        resource_requirements=_resource_requirements(goal.resource_ids),
        track=goal.track,
    )
    # Schema-gated output: never hand back a package that does not
    # validate against its own schema.
    validate_and_reject(COMPUTE_EXECUTION_PACKAGE_SCHEMA, package.to_dict())
    return package


def _parameter_name(entry: Mapping[str, Any], index: int) -> str:
    """The parameter name of a frozen input entry.

    The frozen entry's own ``name`` when it carries a non-blank string,
    otherwise a deterministic positional fallback so every input gets a
    stable, distinct instruction key.
    """
    name = entry.get("name")
    if isinstance(name, str) and name.strip():
        return name
    return f"input-parameter-{index + 1}"


def _input_file_instructions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One input-file creation instruction per frozen Goal input.

    Each instruction records what the file materializes (the frozen
    entry verbatim) and the fixed SS4 obligation; the concrete file name
    is the worker's decision at materialization time.
    """
    return [
        {
            "parameter": _parameter_name(entry, index),
            "materializes": dict(entry),
            "instructions": _INPUT_FILE_INSTRUCTION,
        }
        for index, entry in enumerate(inputs)
    ]


def _software_environment(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """The software/environment declarations of the frozen Goal.

    Frozen inputs that already declare software or environment facts are
    recorded verbatim; the worker records the actual software name,
    version and environment at materialization time (SS4).
    """
    declared = [
        dict(entry) for entry in inputs if "software" in entry or "environment" in entry
    ]
    return {
        "declared": declared,
        "note": (
            "worker records the actual software name, version and"
            " environment at materialization time"
        ),
    }


def _resource_requirements(resource_ids: list[str]) -> dict[str, Any]:
    """The resource requirements of the frozen Goal (its ``resource_ids``)."""
    return {"resource_ids": list(resource_ids)}
