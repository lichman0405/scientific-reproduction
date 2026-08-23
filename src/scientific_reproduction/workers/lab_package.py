"""Lab Execution Package generator (issue #158).

Implements the runtime constructor behind the
``10-EXPERIMENT-SUBSYSTEM.md`` SS1 arrow "Worker -> create Experiment
Execution Package": a pure, deterministic, schema-gated generator that
derives the frozen ``core.models.LabExecutionPackage``
(``schemas/lab-execution-package.schema.yaml``) from the **frozen Goal
Contract** plus the goal's registered resources. The worker no longer
hand-authors the package JSON; the generator assembles the exact schema
shape, refuses non-frozen goals, and the result is always checked by
the real persistence gate (``validate_and_reject``) before it is
returned.

Normative grounding (locked readings)
-------------------------------------
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS1/SS3: the dispatching Worker creates
  the Experiment Execution Package; the package carries project/Goal/Run
  ids, track and frozen Goal version, the objective, the exact protocol
  steps, reagents/materials, equipment, critical control variables,
  prohibited modifications, operator records, safety notes and the
  required-return manifest;
* ``05-GOAL-RUN-SCHEMA.md`` SS4: the Goal Contract declares its inputs,
  outputs and resource ids -- the derivation sources of the package;
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS4: the dispatched package's
  ``required_return`` tokens are the required raw-data exports the
  operator must return -- derived here from the goal's declared outputs
  (the ``name`` key of an output object, the same locked reading the
  worker-context generator applies to ``required_outputs`` in
  ``workers/context.py``);
* ``schemas/lab-execution-package.schema.yaml``: the package shape the
  result must pass; the generator calls the real gate
  (``core.schema_validation.validate_and_reject``) on every output.

Derivation rules (deterministic, pure)
--------------------------------------
The package is a pure function of the registered state and the
caller-provided inputs:

* ``package_id`` -- ``core.ids.generate_id("package", project_id,
  goal.goal_id, run_id)``: deterministic and identity-bearing;
* ``project_id`` -- the registered project state record at ``root``
  (``planning.init.read_project_state``);
* ``goal_id`` / ``track`` / ``objective`` -- the frozen contract's own
  fields;
* ``goal_version`` -- the frozen record's exact version (a formal
  ``v<N>``; a draft or a frozen record without a formal version is
  rejected, so a generated package can never reference a drifting
  authoring state);
* ``run_id`` -- caller-injected (the Run the package dispatches; a Run
  is created from the context before dispatch, ``workers/context.py``);
* ``required_return`` -- the sorted distinct ``name`` keys of the goal's
  declared outputs; output objects without a string ``name`` are not
  returnable artifacts and are skipped (documented locked reading);
* ``reagents`` / ``instruments`` -- the goal's referenced resources,
  resolved from the provided ``Resource`` records: REAGENT and
  CONSUMABLE resources render into ``reagents``, INSTRUMENT resources
  into ``instruments`` (sorted by resource id, so the tables are
  deterministic regardless of the input order). Each entry carries the
  resource's ``name``, ``resource_id`` and ``availability_state`` plus
  ``notes`` / ``human_gate_required`` when set. Resource types the lab
  package has no table for (external services, compute/database access,
  safety capabilities, other) are not rendered -- they belong to the
  compute and worker-context packages, not the operator-facing sheet.
  A goal resource id with no provided record raises loudly (the frozen
  plan guarantees registration; nothing is silently dropped).

Stage-1 scaffolding (the #156 extension point)
----------------------------------------------
Until issue #156 (the typed Goal Contract ``procedure`` /
``execution_constraints``) is merged, the frozen goal carries no typed
procedure steps or execution constraints, so the generator cannot
derive them: the scientific step content (actions, per-step
inputs/outputs, critical control variables, prohibited changes,
operator records, safety notes) is **caller-authored** through the
keyword arguments and passed through verbatim. The authored ``procedure``
steps are validated against the shared step vocabulary of issue #156's
``GoalProcedureStep`` (``action`` plus ``inputs`` / ``outputs`` /
``trace_refs`` lists of strings): one step vocabulary, not two, so the
post-#156 projection (``goal.procedure`` -> package ``procedure``,
``execution_constraints.forbidden_changes`` -> ``prohibited_changes``,
``execution_constraints.safety_notes`` -> ``safety_notes``) slots into
this generator without a translator. Authoring lists are preserved in
caller order (their order is meaningful on the operator sheet); entries
must be non-empty strings.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the
injectable inputs: no randomness, no wall clock, no network.
``TypeError`` at the public boundaries; ``ValueError`` subclasses with
stable messages otherwise. The result is schema-gated on the way out:
a package that fails the real ``lab-execution-package`` schema raises
``SchemaValidationError`` (nothing is returned).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    GoalContract,
    LabExecutionPackage,
    Resource,
    ResourceType,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import read_project_state
from scientific_reproduction.planning.plan import is_formal_version
from scientific_reproduction.workers.context import GoalNotFrozenError

__all__ = [
    "INSTRUMENT_RESOURCE_TYPES",
    "REAGENT_RESOURCE_TYPES",
    "LabPackageBuildError",
    "LabPackageDataError",
    "LabPackageError",
    "LabPackageResourceError",
    "generate_lab_execution_package",
]

#: Resource types rendered into the package's ``reagents`` table
#: (the materials the operator procures/handles: reagents and
#: consumables).
REAGENT_RESOURCE_TYPES: frozenset[ResourceType] = frozenset(
    (ResourceType.REAGENT, ResourceType.CONSUMABLE)
)

#: Resource types rendered into the package's ``instruments`` table
#: (the equipment the operator runs).
INSTRUMENT_RESOURCE_TYPES: frozenset[ResourceType] = frozenset(
    (ResourceType.INSTRUMENT,)
)


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class LabPackageError(ValueError):
    """Base class for all lab-execution-package generation errors."""


class LabPackageBuildError(LabPackageError):
    """Raised when the package cannot be derived from the given state.

    Stable messages name the offending goal or field and the reason;
    nothing is silently dropped or fabricated.
    """


class LabPackageDataError(LabPackageError):
    """Raised when a caller-supplied value is malformed.

    E.g. a ``run_id`` that is not a safe handoff path segment or an
    empty authoring-list entry. Stable messages.
    """


class LabPackageResourceError(LabPackageError):
    """Raised when a resource the goal references has no provided record.

    The frozen plan guarantees registration, so a missing record is
    surfaced loudly instead of silently dropping the reference.
    """


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def generate_lab_execution_package(
    root: str | Path,
    goal: GoalContract,
    resources: Sequence[Resource],
    run_id: str,
    *,
    procedure: Sequence[Mapping[str, Any]] = (),
    critical_control_variables: Sequence[Mapping[str, Any]] = (),
    prohibited_changes: Sequence[str] = (),
    required_operator_records: Sequence[str] = (),
    safety_notes: Sequence[str] = (),
) -> LabExecutionPackage:
    """Generate the Lab Execution Package for one frozen goal.

    Pure and deterministic: the package is a pure function of the
    registered project state at ``root``, the frozen Goal Contract, the
    provided resource records and the injectable authoring inputs. The
    package identifies the frozen contract exactly (``goal_id`` and
    ``goal_version`` = the frozen record's id and version); the
    required-return tokens derive from the goal's declared outputs; the
    reagent/instrument tables derive from the goal's referenced
    resources. The procedure steps and the constraint-like fields are
    caller-authored stage-1 scaffolding (the typed goal procedure of
    issue #156 does not exist yet) and are passed through verbatim
    after shape validation against the shared step vocabulary.

    The result is schema-gated on the way out
    (``validate_and_reject("lab-execution-package", ...)``): a package
    that fails the real schema raises ``SchemaValidationError`` and
    nothing is returned.

    Args:
        root: the initialized workspace root.
        goal: the **frozen** Goal Contract (the record the plan freeze
            produced: ``frozen`` True, formal version ``v<N>``).
        resources: the registered ``Resource`` records the goal
            references (``goal.resource_ids``); records whose id the
            goal does not reference are ignored, a referenced id with no
            provided record raises ``LabPackageResourceError``.
        run_id: the Run the package dispatches (caller-injected; the Run
            is registered before dispatch).
        procedure: the authored procedure steps, each a mapping of the
            shared step vocabulary (non-empty string ``action`` plus
            optional ``inputs`` / ``outputs`` / ``trace_refs`` lists of
            strings); extra keys (e.g. ``step`` / ``title``) pass
            through verbatim. Default empty.
        critical_control_variables: the authored critical control
            variables (mappings, passed through verbatim). Default
            empty.
        prohibited_changes: the authored prohibited scientific
            modifications (non-empty strings, caller order preserved).
            Default empty.
        required_operator_records: the authored operator record
            requirements (non-empty strings, caller order preserved).
            Default empty.
        safety_notes: the authored safety/resource notes (non-empty
            strings, caller order preserved). Default empty.

    Returns:
        The schema-gated :class:`LabExecutionPackage`.

    Raises:
        TypeError: ``root`` is not a str/Path, ``goal`` is not a
            ``GoalContract``, ``resources`` is not a sequence of
            ``Resource`` records, ``run_id`` is not a str, or one of the
            authoring inputs violates its declared shape (a non-mapping
            procedure/control-variable entry, a non-str list entry).
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        GoalNotFrozenError: ``goal`` is not the frozen Goal Contract
            (``frozen`` False); stable message.
        LabPackageBuildError: the frozen goal carries no formal version
            (``v<N>``), or a procedure step violates the shared step
            vocabulary (missing/empty ``action``, non-string
            ``inputs`` / ``outputs`` / ``trace_refs`` entries); stable
            messages.
        LabPackageDataError: ``run_id`` is empty or not a safe handoff
            path segment, or an authoring-list entry is empty; stable
            messages.
        LabPackageResourceError: a goal resource id has no provided
            ``Resource`` record; stable message.
        InvalidPlanVersionError: ``goal.version`` is malformed
            (propagated from the formal-version check).
        SchemaValidationError: the generated package fails the real
            ``lab-execution-package`` schema (the models and the schema
            disagree -- a bug, never a caller error).
        ValueError: the stored project state record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(goal, GoalContract):
        raise TypeError(f"goal must be a GoalContract, got {type(goal).__name__}")
    if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes)):
        raise TypeError(
            "resources must be a sequence of Resource records, got"
            f" {type(resources).__name__}"
        )
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    for index, resource in enumerate(resources):
        if not isinstance(resource, Resource):
            raise TypeError(
                f"resources entry {index} must be a Resource, got"
                f" {type(resource).__name__}"
            )

    project = read_project_state(root)
    _require_frozen_goal(goal)
    _validate_run_id(run_id)
    reagents, instruments = _resource_tables(goal, resources)

    package = LabExecutionPackage(
        package_id=generate_id(
            "package", project.project_id, goal.goal_id, run_id
        ),
        project_id=project.project_id,
        goal_id=goal.goal_id,
        run_id=run_id,
        objective=goal.objective,
        procedure=_coerce_procedure(procedure),
        required_return=_required_returns(goal),
        track=goal.track,
        goal_version=goal.version,
        reagents=reagents,
        instruments=instruments,
        critical_control_variables=_coerce_object_list(
            "critical_control_variables", critical_control_variables
        ),
        prohibited_changes=_coerce_string_list(
            "prohibited_changes", prohibited_changes
        ),
        required_operator_records=_coerce_string_list(
            "required_operator_records", required_operator_records
        ),
        safety_notes=_coerce_string_list("safety_notes", safety_notes),
    )
    # The real persistence gate on the way out: a schema-invalid package
    # is refused loudly (the models and the schema must agree).
    validate_and_reject("lab-execution-package", package.to_dict())
    return package


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_frozen_goal(goal: GoalContract) -> None:
    """Reject generation from anything but the frozen Goal Contract.

    The package derives from the record the plan freeze produced
    (``frozen`` True, formal version ``v<N>``); a draft would make the
    dispatched package drift with the authoring state. The generated
    package carries the frozen record's exact version.
    """
    if not goal.frozen:
        raise GoalNotFrozenError(
            f"lab execution package generation requires the frozen goal"
            f" contract, got frozen=False for goal {goal.goal_id!r};"
            " re-read the frozen contract from the plan freeze result"
            " (planning.freeze.freeze_plan)"
        )
    if not is_formal_version(goal.version):
        raise LabPackageBuildError(
            f"frozen goal contract {goal.goal_id!r} must carry a formal"
            f" version 'v<N>', got {goal.version!r}"
        )


def _validate_run_id(run_id: str) -> None:
    """Reject run ids that would escape the handoff directory.

    The Run id maps to the handoff directory name
    (``lab/outgoing/<RUN_ID>/``); the same safe-path-segment discipline
    the lab adapter applies at dispatch.
    """
    if not run_id.strip():
        raise LabPackageDataError("run_id must be a non-empty string")
    if run_id in (".", "..") or "/" in run_id or "\\" in run_id:
        raise LabPackageDataError(
            f"run_id {run_id!r} is not a safe handoff path segment (no"
            " '/', no '\\\\', not '.' or '..')"
        )


def _resource_tables(
    goal: GoalContract, resources: Sequence[Resource]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map the goal's referenced resources onto the package tables.

    REAGENT / CONSUMABLE resources render into ``reagents``, INSTRUMENT
    resources into ``instruments``, sorted by resource id (deterministic
    regardless of the input order). Resource types the lab package has
    no table for are documented as not rendered. A referenced id with
    no provided record raises ``LabPackageResourceError``.
    """
    resources_by_id = {resource.resource_id: resource for resource in resources}
    missing = sorted(set(goal.resource_ids) - set(resources_by_id))
    if missing:
        raise LabPackageResourceError(
            f"goal {goal.goal_id!r} references resource(s)"
            f" {', '.join(repr(r) for r in missing)} but no matching"
            " Resource record was provided; the frozen plan guarantees"
            " registration -- pass the goal's registered resources"
        )
    reagents: list[dict[str, Any]] = []
    instruments: list[dict[str, Any]] = []
    for resource_id in sorted(goal.resource_ids):
        resource = resources_by_id[resource_id]
        entry: dict[str, Any] = {
            "name": resource.name,
            "resource_id": resource.resource_id,
            "availability_state": resource.availability_state.value,
        }
        if resource.notes is not None:
            entry["notes"] = resource.notes
        if resource.human_gate_required:
            entry["human_gate_required"] = True
        if resource.resource_type in REAGENT_RESOURCE_TYPES:
            reagents.append(entry)
        elif resource.resource_type in INSTRUMENT_RESOURCE_TYPES:
            instruments.append(entry)
        # Every other resource type has no lab-package table (documented
        # in the module docstring).
    return reagents, instruments


def _required_returns(goal: GoalContract) -> list[str]:
    """The required-return tokens derived from the goal's declared outputs.

    ``GoalContract.outputs`` is a list of objects
    (``schemas/goal.schema.yaml``); an output object contributes its
    ``name`` key when that key is a string -- the same locked reading
    the worker-context generator applies to ``required_outputs``
    (``workers/context.py``). Output objects without a string ``name``
    are not returnable artifacts and are skipped. Sorted, distinct.
    """
    names: set[str] = set()
    for output in goal.outputs:
        name = output.get("name")
        if isinstance(name, str):
            names.add(name)
    return sorted(names)


def _coerce_procedure(
    procedure: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and copy the authored procedure steps.

    The shared step vocabulary of issue #156 (one vocabulary for Goal
    Contract steps and package steps): every step is a mapping with a
    non-empty string ``action`` and, when present, ``inputs`` /
    ``outputs`` / ``trace_refs`` lists of strings. Extra keys (e.g.
    ``step`` / ``title``) pass through verbatim; step order is
    preserved.
    """
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(procedure):
        if not isinstance(step, Mapping):
            raise TypeError(
                f"procedure entry {index} must be a mapping, got"
                f" {type(step).__name__}"
            )
        data = dict(step)
        action = data.get("action")
        if not isinstance(action, str) or not action.strip():
            raise LabPackageBuildError(
                f"procedure entry {index} must carry a non-empty string"
                f" 'action' (the shared step vocabulary of issue #156),"
                f" got {action!r}"
            )
        for key in ("inputs", "outputs", "trace_refs"):
            value = data.get(key)
            if value is not None and (
                not isinstance(value, list)
                or not all(isinstance(item, str) for item in value)
            ):
                raise LabPackageBuildError(
                    f"procedure entry {index} {key!r} must be a list of"
                    f" strings, got {value!r}"
                )
        steps.append(data)
    return steps


def _coerce_object_list(
    name: str, values: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Copy one authoring list of mappings (caller order preserved)."""
    entries: list[dict[str, Any]] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise TypeError(
                f"{name} entry {index} must be a mapping, got"
                f" {type(value).__name__}"
            )
        entries.append(dict(value))
    return entries


def _coerce_string_list(name: str, values: Sequence[str]) -> list[str]:
    """Copy one authoring list of non-empty strings (order preserved)."""
    entries: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise TypeError(
                f"{name} entry {index} must be a str, got"
                f" {type(value).__name__}"
            )
        if not value.strip():
            raise LabPackageDataError(
                f"{name} entries must be non-empty strings"
            )
        entries.append(value)
    return entries
