"""Resource registry and availability-state vocabulary (DEV-M4-G05).

Implements the **Resource state representation** deliverable of DEV-M4-G05
over the frozen ``Resource`` model (``schemas/resource.schema.yaml`` /
``core/models.py``), grounded in:

* ``01-PRODUCT-REQUIREMENTS.md`` SS5 step 7: the Supervisor "creates Work
  Packages, Requirements, Unit Process ``/goals``, dependencies, resources,
  acceptance criteria, replication plans, primary analysis protocols,
  assumption registry and closure contracts" -- resources are a plan input
  (the plan record's ``resource_ids`` and the goal contracts'
  ``resource_ids`` reference them);
* ``14-STATE-GIT-ARTIFACTS.md`` SS3 (per-object state files): the
  ``resources/`` directory of ``templates/PROJECT-TREE.template.txt`` is
  the resource state dir;
* ``core/models.py``: ``Resource`` (resource_id, name, resource_type,
  availability_state, blocks_goal_ids, estimated_cost, currency,
  human_gate_required, notes) and ``AvailabilityState`` -- the frozen enum
  with exactly the four availability states AVAILABLE / PROCURE /
  OUTSOURCE / CAPABILITY_GAP (``schemas/resource.schema.yaml``
  ``availability_state`` enum);
* ``schemas/resource.schema.yaml``: the resource record shape
  (``availability_state`` required, ``blocks_goal_ids`` optional array).

Registry (normative)
--------------------
The registry follows the M4-G02 inventory pattern: canonical JSON records
via ``core.atomic.atomic_write`` (atomic, parent dirs created), ids
validated as single path segments, schema-validated before persistence
(``validate_and_reject`` ``"resource"``), and **immutable-functional**: a
resource id is written exactly once and re-registration raises
``DuplicateResourceError`` with a stable message (no clobbering). Reads and
listings return typed ``Resource`` records; ``list_resources`` / the
``load_resource_registry`` snapshot are deterministically sorted by id, so
the DAG builder (``planning/dag.py``) computes a pure function of the
registered state.

AC-01 (normative reading)
-------------------------
"AVAILABLE/PROCURE/OUTSOURCE/CAPABILITY_GAP states are representable": the
four states are the frozen ``AvailabilityState`` enum of
``schemas/resource.schema.yaml`` -- nothing is redefined -- and
representability means the registry persists, reads back and lists a
``Resource`` in every state, round-tripping exactly.

AC-02 (normative reading -- the gap vocabulary)
-----------------------------------------------
A resource **gap** is any state other than AVAILABLE (PROCURE, OUTSOURCE
and CAPABILITY_GAP are all gaps; each calls for a different remediation but
each blocks), or a missing resource: a ``resource_id`` reference with no
registered record is a gap by definition -- a requirement that cannot be
satisfied. ``is_resource_gap`` is the frozen predicate: ``None`` (missing)
is a gap, AVAILABLE is not, every other state is. Whether a gap *blocks* a
goal is decided by the rule table in ``planning/dag.py``
(``BLOCKER_RULES``); this module owns only the availability vocabulary.

Pure deterministic functions, no randomness, no wall-clock, no LLM;
``TypeError`` at the public boundaries; errors of the registry path
(``ProjectNotInitializedError``, corrupt-record ``ValueError``) follow the
``planning/inventory.py`` conventions with stable messages.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import AvailabilityState, Resource
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    PlanningError,
    ProjectNotInitializedError,
)

__all__ = [
    "RESOURCES_STATE_DIR",
    "RESOURCE_GAP_STATES",
    "DuplicateResourceError",
    "InvalidResourceIdError",
    "ResourceError",
    "ResourceInput",
    "ResourceNotFoundError",
    "ResourceRegistry",
    "is_resource_gap",
    "list_resources",
    "load_resource_registry",
    "read_resource",
    "register_resource",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ResourceError(PlanningError):
    """Base class for all resource registry errors."""


class DuplicateResourceError(ResourceError, ValueError):
    """Raised when a resource id is registered a second time (no clobbering)."""


class ResourceNotFoundError(ResourceError, ValueError):
    """Raised when reading a resource id that is not registered."""


class InvalidResourceIdError(ResourceError, ValueError):
    """Raised when a resource id is not a safe single registry path segment."""


# ---------------------------------------------------------------------------
# Frozen constants and the availability-state vocabulary (AC-01/AC-02)
# ---------------------------------------------------------------------------

#: Workspace directory holding the resource records
#: (``templates/PROJECT-TREE.template.txt``).
RESOURCES_STATE_DIR: str = "resources"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: The frozen gap states: any availability state other than AVAILABLE is a
#: resource gap (AC-02 normative reading -- PROCURE, OUTSOURCE and
#: CAPABILITY_GAP each call for a different remediation but each blocks).
RESOURCE_GAP_STATES: frozenset[AvailabilityState] = frozenset(
    (
        AvailabilityState.PROCURE,
        AvailabilityState.OUTSOURCE,
        AvailabilityState.CAPABILITY_GAP,
    )
)

#: A user-supplied resource: the typed model or a schema-shaped dict.
ResourceInput: TypeAlias = Resource | Mapping[str, Any]


def is_resource_gap(resource: Resource | None) -> bool:
    """True iff ``resource`` is a gap (AC-02 vocabulary).

    A missing resource (``None`` -- a reference with no registered record)
    is a gap by definition: the requirement cannot be satisfied.
    AVAILABLE is not a gap; PROCURE / OUTSOURCE / CAPABILITY_GAP are
    (``RESOURCE_GAP_STATES``).

    Raises:
        TypeError: ``resource`` is neither a ``Resource`` nor ``None``.
    """
    if resource is None:
        return True
    if not isinstance(resource, Resource):
        raise TypeError(
            f"resource must be a Resource or None, got {type(resource).__name__}"
        )
    return resource.availability_state in RESOURCE_GAP_STATES


# ---------------------------------------------------------------------------
# Resource registry (immutable-functional, no clobbering)
# ---------------------------------------------------------------------------


def register_resource(root: str | Path, resource: ResourceInput) -> Resource:
    """Register one resource record at ``resources/<resource_id>.json``.

    The record is schema-validated (``validate_and_reject`` ``"resource"``)
    and persisted as canonical JSON (``core.atomic.atomic_write``). The
    registry is immutable-functional: a resource id is written exactly once
    and a second registration is rejected with a stable
    ``DuplicateResourceError`` (no clobbering). The resource id must be a
    safe single path segment.

    Args:
        root: the initialized workspace root.
        resource: the resource as a typed ``Resource`` or a schema-shaped
            mapping (missing optional fields default as in the model).

    Returns:
        The registered resource record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``resource`` is neither a
            ``Resource`` nor a mapping.
        ValueError: the resource is schema-invalid (subclass
            ``SchemaValidationError``), its ``availability_state`` is not a
            frozen enum value, or a required field is missing.
        InvalidResourceIdError: the ``resource_id`` is not a safe single
            path segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateResourceError: a resource with the same ``resource_id`` is
            already registered (stable message).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    resource_model = _coerce_resource(resource)
    _validate_registry_id(resource_model.resource_id)
    state_path = _resource_path(project_root, resource_model.resource_id)
    if state_path.is_file():
        raise DuplicateResourceError(
            f"resource {resource_model.resource_id!r} is already registered;"
            " resources are immutable and each resource_id is written exactly"
            " once"
        )
    validate_and_reject("resource", resource_model.to_dict())
    atomic_write(state_path, _canonical_json(resource_model.to_dict()))
    return resource_model


def read_resource(root: str | Path, resource_id: str) -> Resource:
    """Read one registered resource record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``resource_id`` is not a
            str.
        InvalidResourceIdError: ``resource_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ResourceNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt (unparseable or not an
            object).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(resource_id, str):
        raise TypeError(
            f"resource_id must be a str, got {type(resource_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_registry_id(resource_id)
    state_path = _resource_path(project_root, resource_id)
    if not state_path.is_file():
        raise ResourceNotFoundError(
            f"no resource with id {resource_id!r} is registered at"
            f" {project_root}"
        )
    return _read_resource_record(state_path)


def list_resources(root: str | Path) -> tuple[Resource, ...]:
    """List every registered resource, sorted by id (deterministic).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    directory = project_root / RESOURCES_STATE_DIR
    if not directory.is_dir():
        return ()
    records: list[Resource] = []
    for path in sorted(directory.glob("*.json")):
        records.append(_read_resource_record(path))
    return tuple(records)


@dataclass(frozen=True)
class ResourceRegistry:
    """A typed snapshot of the registered resource state.

    ``resources`` is sorted by resource id, so the snapshot is deterministic
    for a given workspace; it is the input of the pure DAG functions
    (``planning/dag.py``: ``resource_blockers_for_goal`` /
    ``resource_blocker_mapping`` / ``build_plan_dag``).
    """

    resources: tuple[Resource, ...]


def load_resource_registry(root: str | Path) -> ResourceRegistry:
    """Load a typed snapshot of the registered resource state.

    Convenience composition of :func:`list_resources`: the snapshot is the
    input of the pure blocker-mapping functions in ``planning/dag.py``.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    return ResourceRegistry(resources=list_resources(root))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _is_safe_registry_id(value: str) -> bool:
    """True iff ``value`` is a safe single registry path segment."""
    return (
        value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
    )


def _validate_registry_id(value: str) -> None:
    """Reject resource ids that would escape the resources directory.

    A non-str id (only reachable through a hand-built mapping, before the
    schema gate) is rejected with the stable id error rather than a raw
    ``TypeError`` from the path-segment checks.
    """
    if not isinstance(value, str) or not _is_safe_registry_id(value):
        raise InvalidResourceIdError(
            f"invalid resource id {value!r}: ids must be non-empty single"
            " path segments (no '/', no '\\\\', not '.' or '..')"
        )


def _coerce_resource(resource: ResourceInput) -> Resource:
    """Return a typed resource from either input form."""
    if isinstance(resource, Resource):
        return resource
    if isinstance(resource, Mapping):
        return Resource.from_dict(resource)
    raise TypeError(
        f"resource must be a Resource or a mapping, got {type(resource).__name__}"
    )


def _resource_path(root: Path, resource_id: str) -> Path:
    return root / RESOURCES_STATE_DIR / f"{resource_id}.json"


def _canonical_json(data: dict[str, object]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _read_resource_record(path: Path) -> Resource:
    """Load and type a resource record, rejecting corrupt state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt resource record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt resource record at {path}: expected a JSON object"
        )
    return Resource.from_dict(raw)
