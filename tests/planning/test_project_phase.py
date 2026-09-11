"""Tests for ``planning.phase.advance_project_phase`` (v0.3.1 local).

The acceptance run's top breakage: the runtime had no registration-level
API to advance ``project.yaml`` phase -- sessions hand-rolled the write
plus the event and crashed on deterministic-id/idempotency-key
conventions. This module tests the surface that replaces the hand-roll:
rule-gated transitions, exactly-once events, and crash-window recovery.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.rules.lifecycle import ProjectPhase
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    initialize_project,
)
from scientific_reproduction.planning.phase import advance_project_phase

AT = "2026-09-05T08:00:00Z"
ACTOR = "supervisor"

#: Deterministic init inputs (mirrors tests/integration/test_planning_flow.py).
DOI = "10.1039/D5TA00771B"
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _init_project(tmp_path: Path, name: str = "project") -> Path:
    """A real initialized project at its INITIALIZING phase.

    ``initialize_project`` (the CLI-init backend) leaves the project at
    the first mainline phase; the phase-advance API is the only way to
    move it.
    """
    root = tmp_path / name
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def _phase(root: Path) -> str:
    return json.loads((root / "project.yaml").read_text(encoding="utf-8"))[
        "project_phase"
    ]


def _events_of(root: Path, event_type: str) -> list:
    return [
        e
        for e in (root / "events").glob("sr_event_*.json")
        if f'"event_type": "{event_type}"' in e.read_text(encoding="utf-8")
    ]


def test_advance_along_mainline_and_event_recorded(tmp_path: Path) -> None:
    root = _init_project(tmp_path)
    assert _phase(root) == ProjectPhase.INITIALIZING.value
    advance_project_phase(
        root, ProjectPhase.SOURCE_ACQUISITION, actor=ACTOR, at=AT,
        reason="paper acquired",
    )
    advance_project_phase(
        root, ProjectPhase.REPRODUCTION_INVENTORY, actor=ACTOR, at=AT
    )
    assert _phase(root) == ProjectPhase.REPRODUCTION_INVENTORY.value
    events = _events_of(root, "project.phase.INITIALIZING.SOURCE_ACQUISITION")
    assert len(events) == 1
    record = json.loads(events[0].read_text(encoding="utf-8"))
    assert record["from"] == "INITIALIZING"
    assert record["to"] == "SOURCE_ACQUISITION"
    assert record["actor"] == ACTOR
    assert record["reason"] == "paper acquired"


def test_advance_same_phase_is_idempotent_no_duplicate_event(tmp_path: Path) -> None:
    root = _init_project(tmp_path)
    advance_project_phase(root, ProjectPhase.SOURCE_ACQUISITION, actor=ACTOR, at=AT)
    advance_project_phase(root, ProjectPhase.SOURCE_ACQUISITION, actor=ACTOR, at=AT)
    # the phase record is untouched and the event was appended once
    assert _phase(root) == ProjectPhase.SOURCE_ACQUISITION.value
    assert len(_events_of(root, "project.phase.INITIALIZING.SOURCE_ACQUISITION")) == 1


def test_advance_reconciles_event_missing_after_crash_window(tmp_path: Path) -> None:
    root = _init_project(tmp_path)
    advance_project_phase(root, ProjectPhase.SOURCE_ACQUISITION, actor=ACTOR, at=AT)
    # simulate the crash between project.yaml write and event append:
    # the event record vanishes, the phase is already advanced
    for ev in _events_of(root, "project.phase.INITIALIZING.SOURCE_ACQUISITION"):
        ev.unlink()
    # re-invoking the API repairs the event (and never rewrites the phase)
    advance_project_phase(root, ProjectPhase.SOURCE_ACQUISITION, actor=ACTOR, at=AT)
    assert len(_events_of(root, "project.phase.INITIALIZING.SOURCE_ACQUISITION")) == 1


def test_advance_illegal_transition_rejected_without_writes(tmp_path: Path) -> None:
    from scientific_reproduction.core.transitions import IllegalTransitionError

    root = _init_project(tmp_path)
    with pytest.raises(IllegalTransitionError):
        advance_project_phase(root, ProjectPhase.PLAN_FROZEN, actor=ACTOR, at=AT)
    assert _phase(root) == ProjectPhase.INITIALIZING.value
    # no phase event was appended (the init event log may have records
    # of its own -- the phase event type is what must not exist)
    assert not _events_of(root, "project.phase.INITIALIZING.PLAN_FROZEN")


def test_advance_unknown_phase_string(tmp_path: Path) -> None:
    root = _init_project(tmp_path)
    with pytest.raises(ValueError, match="unknown project phase"):
        advance_project_phase(root, "NOT_A_PHASE", actor=ACTOR, at=AT)


def test_advance_requires_initialized_project(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotInitializedError):
        advance_project_phase(
            tmp_path / "nothing", ProjectPhase.COMPLETED, actor=ACTOR, at=AT
        )


def test_advance_type_error_boundaries(tmp_path: Path) -> None:
    root = _init_project(tmp_path)
    for bad in (
        dict(actor=None, at=AT),
        dict(actor=ACTOR, at=None),
        dict(actor=ACTOR, at=AT, reason=123),
    ):
        with pytest.raises(TypeError):
            advance_project_phase(
                root, ProjectPhase.SOURCE_ACQUISITION, **bad
            )
    with pytest.raises(TypeError):
        advance_project_phase(root, 42, actor=ACTOR, at=AT)
