"""Project phase advancement along the normative mainline (v0.3.1 local).

``project.yaml`` phase transitions are validated by
``core.rules.lifecycle`` and audited by the append-only event log, but
the runtime shipped no registration-level API to perform a sanctioned
transition: sessions had to hand-roll the *project.yaml* rewrite plus
the event write, and the deterministic-id / idempotency-key conventions
were only discoverable from ``core/events.py`` (reported by the first
acceptance run as the top breakage: the "phase advance" hole).

This module is that surface. One call advances the phase, records the
``project.phase.<from>.<to>`` event exactly once, and is resumable:
re-running after a crash window never duplicates the event and never
blocks.

Semantics
---------
* **Validated.** The target must be a normative transition
  (``is_legal_project_phase_transition``); an illegal transition raises
  ``IllegalTransitionError`` *before* anything is written.
* **Exactly-once.** ``project.yaml`` is rewritten atomically only when
  the phase actually moves; the event carries a deterministic id
  (``generate_id("event", "project.phase.<from>.<to>", project_id)``)
  and is appended with the same key, so a repeated submission replays
  instead of duplicating.
* **Resumable (crash-window recovery).** The *project.yaml* write and
  the event append are two separate atomic operations: a crash between
  them leaves the phase advanced with its event missing (the T5
  crash-window class of ``docs/user/bootstrap-state-authoring.md``
  §3). ``advance_project_phase`` detects "phase already at target" and
  reconciles the missing event through the idempotency key -- the only
  user-level repair ever needed is to call the API again.
* **No wall clock.** ``at`` is injected (determinism), like the rest of
  the planning layer.

Integration: ``planning.freeze`` gates on the phase threshold;
``planning.finalize`` owns the COMPLETED transition (its own
``project.finalized`` event type and audit package) -- this helper
covers the mainline phases a Supervisor drives, up to and including
REPORTING; COMPLETED stays with the finalization gate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import ProjectEvent
from scientific_reproduction.core.rules.lifecycle import (
    ProjectPhase,
    apply_project_phase_transition,
)
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)

__all__ = ["advance_project_phase"]


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _record_phase_event(
    project_root: Path,
    project: dict[str, Any],
    current: ProjectPhase,
    to_phase: ProjectPhase,
    *,
    actor: str,
    at: str,
    reason: str | None,
    key: str | None = None,
) -> None:
    """Append the phase-transition event (deterministic id, idempotent).

    The idempotency key IS the event type string, so a repeated append
    replays the recorded event; the key is scoped per project (the
    event id includes the project id, the key what a re-run matches
    on). ``key`` may be overridden by the crash-recovery path, which
    must re-append the *original* transition key (``current`` holds the
    already-won phase there, not the original source phase).
    """
    event_key = key or f"project.phase.{current.value}.{to_phase.value}"
    event_id = generate_id("event", event_key, str(project["project_id"]))
    event = ProjectEvent(
        event_id=event_id,
        timestamp=at,
        actor=actor,
        event_type=event_key,
        object_id=project["project_id"],
        from_=current.value,
        to=to_phase.value,
        reason=reason or None,
        payload={},
    )
    ProjectEventLog(project_root).append(event, idempotency_key=event_key)


def _entering_event_key(project_root: Path, to_phase: ProjectPhase) -> str | None:
    """The idempotency key of the documented transition entering ``to_phase``.

    The event log stores idempotency keys in plaintext claim files
    (``_event_log/idempotency/*.json``). A crash between the
    *project.yaml* write and the event append leaves the phase advanced
    with its event missing; the recovery path re-appends the original
    transition by resolving it from the claims (key shape
    ``project.phase.<from>.<to>``) -- never by inventing a synthetic
    ``from == to`` event. Deterministic: sorted, first match.
    """
    idem_dir = project_root / "_event_log" / "idempotency"
    if not idem_dir.is_dir():
        return None
    suffix = "." + to_phase.value
    candidates: list[str] = []
    for claim_path in sorted(idem_dir.glob("*.json")):
        try:
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        key = claim.get("idempotency_key")
        if not isinstance(key, str):
            continue
        parts = key.split(".")
        if (
            len(parts) == 4
            and parts[0] == "project"
            and parts[1] == "phase"
            and key.endswith(suffix)
        ):
            candidates.append(key)
    return sorted(candidates)[0] if candidates else None


def advance_project_phase(
    root: str | Path,
    to_phase: ProjectPhase | str,
    *,
    actor: str,
    at: str,
    reason: str = "",
) -> ProjectPhase:
    """Advance the registered project phase along the normative mainline.

    The transition is rule-gated (``apply_project_phase_transition``):
    an illegal pair raises ``IllegalTransitionError`` without writing
    anything. The event ``project.phase.<from>.<to>`` is appended
    exactly once (deterministic id + idempotency key); calling the API
    again with the next phase (or re-calling with the same phase after a
    crash) is safe and converges.

    Args:
        root: the initialized workspace root.
        to_phase: the target phase (``ProjectPhase`` or its value
            string).
        actor: recording actor stamped on the event.
        at: injected timestamp (determinism).
        reason: optional reason recorded in the event.

    Returns:
        The phase the project is now in (== ``to_phase``).

    Raises:
        TypeError: a parameter has the wrong type.
        ProjectNotInitializedError: no ``project.yaml`` exists at root.
        IllegalTransitionError: the current phase cannot transition to
            ``to_phase`` (no write happened).
        ValueError: the project record is corrupt (unparseable).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if isinstance(to_phase, str):
        try:
            phase_value = ProjectPhase(to_phase)
        except ValueError as exc:
            raise ValueError(
                f"unknown project phase {to_phase!r};"
                f" valid: {[p.value for p in ProjectPhase]}"
            ) from exc
    elif isinstance(to_phase, ProjectPhase):
        phase_value = to_phase
    else:
        raise TypeError(
            f"to_phase must be a ProjectPhase or str,"
            f" got {type(to_phase).__name__}"
        )
    if not isinstance(actor, str):
        raise TypeError(f"actor must be a str, got {type(actor).__name__}")
    if not isinstance(at, str):
        raise TypeError(f"at must be a str, got {type(at).__name__}")
    if reason is not None and not isinstance(reason, str):
        raise TypeError(f"reason must be a str, got {type(reason).__name__}")

    project_root = Path(root).resolve()
    _require_initialized(project_root)
    project_path = project_root / PROJECT_STATE_FILENAME
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"project state {project_path.name} is unreadable/corrupt: {exc}"
        ) from exc
    current = ProjectPhase(str(project["project_phase"]))

    if current is phase_value:
        # already at the target phase -- including the crash-window case
        # where the event append never happened. Reconcile through the
        # idempotency claims: resolve the *original* "entered <target>"
        # transition key, never a synthetic from==to event, never a
        # duplicate (same key + id replays), never a phase write.
        original_key = _entering_event_key(project_root, phase_value)
        if original_key is not None:
            original_from, original_to = original_key.split(".")[2:4]
            _record_phase_event(
                project_root, project,
                ProjectPhase(original_from), ProjectPhase(original_to),
                actor=actor, at=at, reason=reason or None, key=original_key,
            )
        return phase_value

    # rule-gated, raises before any write
    apply_project_phase_transition(current, phase_value)

    project["project_phase"] = phase_value.value
    project["updated_at"] = at
    atomic_write(
        project_path, json.dumps(project, indent=2, ensure_ascii=False) + "\n"
    )
    _record_phase_event(
        project_root, project, current, phase_value,
        actor=actor, at=at, reason=reason or None,
    )
    return phase_value
