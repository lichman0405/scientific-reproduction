"""Human Gate registration and resolution (v0.3.1 local).

The frozen ``HumanGate`` model (``core/models.py``) and the ``human-gate``
schema shipped since v0.2.0 as the designed record of a gate that needs a
human decision, but no registration surface existed: gates were only
constructed inline (e.g. the author-contact gate of the scenario tests).
This module adds the registry surface, so a Supervisor can open an
evidence-interpretation gate for an ambiguous digitized reading, keep
executing under the recorded ``default_safe_action``, and resolve the gate
once at project close-out.

Semantics:

* **Exactly-once registration.** Gate ids are deterministic
  (``core.ids.generate_id`` with kind ``gate``): the same ambiguity
  re-encountered reproduces the same id, and a duplicate registration is
  rejected with a stable ``DuplicateHumanGateError``, like every other
  registry id. A caller-supplied id must be a valid ``sr_gate_<hex>`` id.
* **Explicit transitions only.** ``resolve_human_gate`` permits exactly
  ``OPEN -> {APPROVED, REJECTED, CANCELLED}``; every other status is
  terminal, so resolving an already-resolved gate is an error
  (``InvalidHumanGateTransitionError``), never a silent no-op.
* **No wall clock.** ``HumanGate`` carries no timestamp fields; a gate's
  audit trail is its status progression and the ``resolution_note``,
  matching the no-wall-clock state-authoring rule of the inventory
  registry.

Integration points: ``planning.finalize`` blocks ``COMPLETED`` while any
gate is OPEN (the single close-out confirmation); ``reporting.human_summary``
lists every recorded gate and its resolution under the "人工确认项" section.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import GateStatus, HumanGate
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    PlanningError,
    ProjectNotInitializedError,
)

#: Workspace directory holding the gate records
#: (``templates/PROJECT-TREE.template.txt``).
HUMAN_GATES_STATE_DIR: str = "human-gates"

#: ID kind of gate records (``<gate_id> = sr_gate_<32 hex>``).
_GATE_KIND: str = "gate"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: A user-supplied gate: the typed model or a schema-shaped mapping
#: (``gate_id`` optional in the mapping form, derived deterministically).
GateInput: TypeAlias = HumanGate | Mapping[str, Any]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HumanGateError(PlanningError, ValueError):
    """Base class for all human gate registry errors."""


class DuplicateHumanGateError(HumanGateError):
    """Raised when a ``gate_id`` is registered a second time."""


class HumanGateNotFoundError(HumanGateError):
    """Raised when operating on a gate id that is not registered."""


class InvalidHumanGateTransitionError(HumanGateError):
    """Raised when ``resolve_human_gate`` is asked for a status change the
    gate's current status does not permit (only OPEN gates can move)."""


class InvalidRegistryIdError(HumanGateError):
    """Raised when an id is not a safe single registry path segment."""


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The only status changes a gate record may legally make. Every other
#: status (APPROVED/REJECTED/RESOLVED/CANCELLED) is terminal.
GATE_TRANSITIONS: dict[GateStatus, frozenset[GateStatus]] = {
    GateStatus.OPEN: frozenset(
        {GateStatus.APPROVED, GateStatus.REJECTED, GateStatus.CANCELLED}
    ),
}


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


def _validate_gate_id(kind: str, value: str) -> None:
    """Reject ids that would escape the registry directory as file names.

    Gate ids follow the canonical ``sr_gate_<hex>`` pattern; ids that
    violate it (or simply contain path separators) are rejected with a
    stable error.
    """
    if not is_valid_id(value, kind=_GATE_KIND):
        raise InvalidRegistryIdError(
            f"invalid {kind} id {value!r}: gate ids must match the canonical"
            " sr_gate_<32hex> pattern (non-empty single path segment, no"
            " '/', no '\\', not '.' or '..')"
        )


def _canonical_json(data: dict[str, object]) -> str:
    """The registry canonical serialization (same convention as
    ``planning/inventory.py``)."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _gate_path(root: Path, gate_id: str) -> Path:
    return root / HUMAN_GATES_STATE_DIR / f"{gate_id}.json"


def _coerce_gate(gate: GateInput) -> HumanGate:
    """Coerce the input to ``HumanGate``; mapping form may omit ``gate_id``."""
    if isinstance(gate, HumanGate):
        return gate
    if isinstance(gate, Mapping):
        data = dict(gate)
        if not str(data.get("gate_id") or "").strip():
            gate_type = data.get("gate_type")
            trigger = str(data.get("trigger") or "")
            if not gate_type or not trigger:
                raise ValueError(
                    "a gate input without a gate_id needs at least"
                    " gate_type and trigger to derive a deterministic one"
                )
            # deterministic id over the canonical fields: the id depends on
            # the ambiguity and what it affects, so two gates with identical
            # trigger text but different affected refs never collide
            parts = [str(gate_type), trigger]
            parts.extend(
                sorted(str(ref) for ref in (data.get("affected_refs") or []))
            )
            data["gate_id"] = generate_id(_GATE_KIND, *parts)
        return HumanGate.from_dict(data)
    raise TypeError(
        f"gate must be a HumanGate or a schema-shaped mapping,"
        f" got {type(gate).__name__}"
    )


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def register_human_gate(root: str | Path, gate: GateInput) -> HumanGate:
    """Register one human gate in the workspace ``human-gates/`` directory.

    The gate is schema-validated (``validate_and_reject``) and persisted
    as canonical JSON (``core.atomic.atomic_write``). When the caller does
    not supply a ``gate_id`` (mapping form only), one is derived
    deterministically from the gate type and trigger, so registering the
    same ambiguity twice reproduces the same id and is rejected the same
    way.

    Args:
        root: the initialized workspace root.
        gate: the gate as a typed ``HumanGate`` or a schema-shaped mapping.

    Returns:
        The registered gate record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``gate`` is neither a
            ``HumanGate`` nor a mapping.
        ValueError: the gate is schema-invalid (subclass
            ``SchemaValidationError``), its ``gate_type`` is not a frozen
            enum value, or a required field is missing.
        InvalidRegistryIdError: the ``gate_id`` is not a canonical
            ``sr_gate_<hex>`` id.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateHumanGateError: a gate with the same ``gate_id`` is
            already registered (stable message).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    gate_model = _coerce_gate(gate)
    _validate_gate_id("human gate", gate_model.gate_id)
    state_path = _gate_path(project_root, gate_model.gate_id)
    if state_path.is_file():
        raise DuplicateHumanGateError(
            f"human gate {gate_model.gate_id!r} is already registered; a"
            " gate_id is unique per gate and duplicate registration is"
            " rejected"
        )
    validate_and_reject("human-gate", gate_model.to_dict())
    atomic_write(state_path, _canonical_json(gate_model.to_dict()))
    return gate_model


def read_human_gate(root: str | Path, gate_id: str) -> HumanGate:
    """Read one registered human gate record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``gate_id`` is not a str.
        InvalidRegistryIdError: ``gate_id`` is not a canonical gate id.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        HumanGateNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt (unparseable or not an
            object).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(gate_id, str):
        raise TypeError(f"gate_id must be a str, got {type(gate_id).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_gate_id("human gate", gate_id)
    state_path = _gate_path(project_root, gate_id)
    if not state_path.is_file():
        raise HumanGateNotFoundError(
            f"no human gate with id {gate_id!r} is registered at"
            f" {project_root}"
        )
    return HumanGate.from_dict(json.loads(state_path.read_text(encoding="utf-8")))


def list_human_gates(root: str | Path) -> tuple[HumanGate, ...]:
    """List every registered human gate, sorted by id (deterministic).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    directory = project_root / HUMAN_GATES_STATE_DIR
    if not directory.is_dir():
        return ()
    records: list[HumanGate] = []
    for path in sorted(directory.glob("*.json")):
        records.append(
            HumanGate.from_dict(json.loads(path.read_text(encoding="utf-8")))
        )
    return tuple(records)


def resolve_human_gate(
    root: str | Path,
    gate_id: str,
    to_status: GateStatus,
    *,
    resolution_note: str | None = None,
) -> HumanGate:
    """Close one OPEN gate with the human's decision (status transition).

    The status transition is rule-gated (``GATE_TRANSITIONS``): only an
    OPEN gate can move, and only to APPROVED / REJECTED / CANCELLED --
    every other status is terminal. The human's answer is stored in the
    record's ``resolution_note`` so the close-out audit trail keeps the
    decision (which reading/value was picked, or the rejection reason).

    Args:
        root: the initialized workspace root.
        gate_id: the gate to resolve.
        to_status: the target status (APPROVED/REJECTED/CANCELLED).
        resolution_note: the human's answer; free text, optional.

    Returns:
        The updated (persisted) gate record.

    Raises:
        TypeError: ``root`` is not a str/Path, ``gate_id`` is not a str, or
            ``resolution_note`` is not a str/None.
        InvalidRegistryIdError: ``gate_id`` is not a canonical gate id.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        HumanGateNotFoundError: no record with that id is registered.
        InvalidHumanGateTransitionError: the current status cannot
            transition to ``to_status`` (stable message without newlines).
        ValueError: the updated record is schema-invalid (should not
            happen for canonical transitions).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(gate_id, str):
        raise TypeError(f"gate_id must be a str, got {type(gate_id).__name__}")
    if resolution_note is not None and not isinstance(resolution_note, str):
        raise TypeError(
            f"resolution_note must be a str or None,"
            f" got {type(resolution_note).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_gate_id("human gate", gate_id)
    current = read_human_gate(project_root, gate_id)
    allowed = GATE_TRANSITIONS.get(current.status)
    if allowed is None or to_status not in allowed:
        raise InvalidHumanGateTransitionError(
            f"human gate {gate_id} cannot transition from"
            f" {current.status.value} to {to_status.value}"
        )
    # keep every other field verbatim: resolving a gate only moves its
    # status and records the human's answer
    updated = replace(
        current, status=to_status, resolution_note=resolution_note
    )
    validate_and_reject("human-gate", updated.to_dict())
    atomic_write(
        _gate_path(project_root, gate_id), _canonical_json(updated.to_dict())
    )
    return updated


# ---------------------------------------------------------------------------
# Module exports (mirrors the planning module convention)
# ---------------------------------------------------------------------------

__all__ = [
    "GATE_TRANSITIONS",
    "HUMAN_GATES_STATE_DIR",
    "DuplicateHumanGateError",
    "GateInput",
    "HumanGateError",
    "HumanGateNotFoundError",
    "InvalidHumanGateTransitionError",
    "InvalidRegistryIdError",
    "list_human_gates",
    "read_human_gate",
    "register_human_gate",
    "resolve_human_gate",
]
