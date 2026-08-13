"""Tests for the normative project-phase transition rules (DEV-M2-G01).

AC mapping (goal contract DEV-M2-G01):
  * AC-01 (every normative legal transition is accepted): the mainline
    ``INITIALIZING -> ... -> COMPLETED`` (04-PROJECT-LIFECYCLE.md section 2
    order), the ``EXECUTING -> REPLANNING -> PLAN_AUDIT`` replanning loop,
    and every suspend/resume arc between active and suspension phases are
    accepted by ``is_legal_project_phase_transition`` /
    ``apply_project_phase_transition`` and the generic API.
  * AC-02 (representative illegal shortcut transitions rejected): backwards
    moves, stage skips (e.g. ``PLANNING -> PLAN_FROZEN`` without
    ``PLAN_AUDIT``), ``REPLANNING -> EXECUTING`` without re-audit, terminal
    escapes, ``PAUSED -> COMPLETED`` without resume + mainline work,
    suspension-to-suspension and no-op same-state arcs are all rejected;
    ``apply``/``transition`` raise ``IllegalTransitionError``.
  * AC-03 (deterministic, not prompt-dependent): the rule table is an
    immutable ``frozenset`` of exact locked pairs, every decision is stable
    across repeated evaluation, and the table covers exactly the frozen
    ``ProjectPhase`` enum (see also test_lifecycle_shared.py).
"""

from __future__ import annotations

import pytest

from scientific_reproduction.core.models import ProjectPhase
from scientific_reproduction.core.rules.lifecycle import (
    ACTIVE_PROJECT_PHASES,
    PROJECT_PHASE_MAINLINE,
    PROJECT_PHASE_TRANSITIONS,
    SUSPENSION_PROJECT_PHASES,
    IllegalTransitionError,
    apply_project_phase_transition,
    is_legal_project_phase_transition,
    is_suspension_project_phase,
    is_terminal_project_phase,
)
from scientific_reproduction.core.transitions import can_transition, transition

# The frozen mainline order, written out in full (04-PROJECT-LIFECYCLE.md
# section 2). Any normative change must update this lock deliberately.
LOCKED_PROJECT_MAINLINE: tuple[ProjectPhase, ...] = (
    ProjectPhase.INITIALIZING,
    ProjectPhase.SOURCE_ACQUISITION,
    ProjectPhase.REPRODUCTION_INVENTORY,
    ProjectPhase.PLANNING,
    ProjectPhase.PLAN_AUDIT,
    ProjectPhase.PLAN_FROZEN,
    ProjectPhase.EXECUTING,
    ProjectPhase.FINAL_VALIDATION,
    ProjectPhase.REPORTING,
    ProjectPhase.COMPLETED,
)

LOCKED_SUSPENSION_PHASES: frozenset[ProjectPhase] = frozenset(
    {
        ProjectPhase.PAUSED,
        ProjectPhase.WAITING_HUMAN,
        ProjectPhase.WAITING_RESOURCE,
    }
)


# ---------------------------------------------------------------------------
# AC-01: every normative legal transition is accepted
# ---------------------------------------------------------------------------


def test_project_mainline_transitions_accepted() -> None:
    for old, new in zip(PROJECT_PHASE_MAINLINE, PROJECT_PHASE_MAINLINE[1:]):
        assert is_legal_project_phase_transition(old, new), (
            f"{old} -> {new} must be legal"
        )
        assert apply_project_phase_transition(old, new) == new
        assert can_transition(old, new) is True
        assert transition(old, new) == new


def test_project_full_mainline_journey_via_apply() -> None:
    phase = PROJECT_PHASE_MAINLINE[0]
    for _, new in zip(PROJECT_PHASE_MAINLINE, PROJECT_PHASE_MAINLINE[1:]):
        phase = apply_project_phase_transition(phase, new)
    assert phase == ProjectPhase.COMPLETED


def test_replanning_loop_accepted() -> None:
    assert is_legal_project_phase_transition(
        ProjectPhase.EXECUTING, ProjectPhase.REPLANNING
    )
    assert is_legal_project_phase_transition(
        ProjectPhase.REPLANNING, ProjectPhase.PLAN_AUDIT
    )
    # The full legal recovery path back to execution: the revised plan must
    # pass audit and re-freeze before execution resumes.
    phase = apply_project_phase_transition(ProjectPhase.EXECUTING, ProjectPhase.REPLANNING)
    phase = apply_project_phase_transition(phase, ProjectPhase.PLAN_AUDIT)
    phase = apply_project_phase_transition(phase, ProjectPhase.PLAN_FROZEN)
    assert apply_project_phase_transition(phase, ProjectPhase.EXECUTING) == (
        ProjectPhase.EXECUTING
    )


@pytest.mark.parametrize("active", sorted(ACTIVE_PROJECT_PHASES, key=str))
def test_suspend_and_resume_arcs_accepted(active: ProjectPhase) -> None:
    assert active != ProjectPhase.COMPLETED
    for suspended in SUSPENSION_PROJECT_PHASES:
        assert is_legal_project_phase_transition(active, suspended), (
            f"{active} -> {suspended} must be legal"
        )
        assert is_legal_project_phase_transition(suspended, active), (
            f"{suspended} -> {active} must be legal"
        )
        assert apply_project_phase_transition(active, suspended) == suspended
        assert apply_project_phase_transition(suspended, active) == active


# ---------------------------------------------------------------------------
# AC-02: representative illegal shortcut transitions are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Backwards moves.
        (ProjectPhase.EXECUTING, ProjectPhase.INITIALIZING),
        (ProjectPhase.PLAN_AUDIT, ProjectPhase.PLANNING),
        (ProjectPhase.FINAL_VALIDATION, ProjectPhase.PLANNING),
        # Skipping stages of the mainline.
        (ProjectPhase.INITIALIZING, ProjectPhase.COMPLETED),
        (ProjectPhase.PLANNING, ProjectPhase.PLAN_FROZEN),
        (ProjectPhase.PLAN_AUDIT, ProjectPhase.EXECUTING),
        (ProjectPhase.REPRODUCTION_INVENTORY, ProjectPhase.EXECUTING),
        # Replanning shortcut: skipping re-audit and re-freeze.
        (ProjectPhase.REPLANNING, ProjectPhase.EXECUTING),
        # Terminal escapes.
        (ProjectPhase.COMPLETED, ProjectPhase.REPORTING),
        (ProjectPhase.COMPLETED, ProjectPhase.PAUSED),
        # Suspension shortcuts.
        (ProjectPhase.PAUSED, ProjectPhase.COMPLETED),
        (ProjectPhase.WAITING_HUMAN, ProjectPhase.WAITING_RESOURCE),
        # No-op same-state arcs are not transitions.
        (ProjectPhase.INITIALIZING, ProjectPhase.INITIALIZING),
        (ProjectPhase.WAITING_RESOURCE, ProjectPhase.WAITING_RESOURCE),
    ],
)
def test_project_shortcut_transitions_rejected(
    old: ProjectPhase, new: ProjectPhase
) -> None:
    assert is_legal_project_phase_transition(old, new) is False
    assert can_transition(old, new) is False
    with pytest.raises(IllegalTransitionError):
        apply_project_phase_transition(old, new)
    with pytest.raises(IllegalTransitionError):
        transition(old, new)


def test_apply_project_rejects_illegal_with_documented_error() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        apply_project_phase_transition(ProjectPhase.EXECUTING, ProjectPhase.INITIALIZING)
    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.subject == "project-phase"
    assert excinfo.value.from_state == ProjectPhase.EXECUTING
    assert excinfo.value.to_state == ProjectPhase.INITIALIZING
    message = str(excinfo.value)
    assert "project-phase" in message
    assert "EXECUTING" in message
    assert "INITIALIZING" in message


# ---------------------------------------------------------------------------
# Structural properties of the locked rule table
# ---------------------------------------------------------------------------


def test_project_phase_rule_table_is_locked_exact_data() -> None:
    assert PROJECT_PHASE_MAINLINE == LOCKED_PROJECT_MAINLINE
    assert SUSPENSION_PROJECT_PHASES == LOCKED_SUSPENSION_PHASES
    # Rebuild the normative table from the locked literals above.
    mainline = {
        (LOCKED_PROJECT_MAINLINE[i], LOCKED_PROJECT_MAINLINE[i + 1])
        for i in range(len(LOCKED_PROJECT_MAINLINE) - 1)
    }
    replanning = {
        (ProjectPhase.EXECUTING, ProjectPhase.REPLANNING),
        (ProjectPhase.REPLANNING, ProjectPhase.PLAN_AUDIT),
    }
    active = {
        phase
        for phase in LOCKED_PROJECT_MAINLINE
        if phase != ProjectPhase.COMPLETED
    } | {ProjectPhase.REPLANNING}
    suspend = {
        (a, s) for a in active for s in LOCKED_SUSPENSION_PHASES
    }
    resume = {
        (s, a) for s in LOCKED_SUSPENSION_PHASES for a in active
    }
    locked = mainline | replanning | suspend | resume
    assert PROJECT_PHASE_TRANSITIONS == frozenset(locked)
    # 9 mainline + 2 replanning + 10 active x 3 suspension x 2 directions.
    assert len(PROJECT_PHASE_TRANSITIONS) == 9 + 2 + 60


def test_project_table_has_no_same_state_arcs() -> None:
    for old, new in PROJECT_PHASE_TRANSITIONS:
        assert old != new, "a transition must record a change, never a no-op"


def test_project_table_covers_every_phase() -> None:
    covered = {old for old, _ in PROJECT_PHASE_TRANSITIONS} | {
        new for _, new in PROJECT_PHASE_TRANSITIONS
    }
    assert covered == set(ProjectPhase)


def test_project_terminal_and_suspension_predicates() -> None:
    for phase in ProjectPhase:
        assert is_terminal_project_phase(phase) is (phase == ProjectPhase.COMPLETED), (
            f"terminal predicate mismatch for {phase}"
        )
        assert is_suspension_project_phase(phase) is (phase in SUSPENSION_PROJECT_PHASES), (
            f"suspension predicate mismatch for {phase}"
        )


# ---------------------------------------------------------------------------
# AC-03: deterministic, not prompt-dependent
# ---------------------------------------------------------------------------


def test_project_transition_decisions_stable_across_repeated_evaluation() -> None:
    states = list(ProjectPhase)
    first = {
        (old, new): is_legal_project_phase_transition(old, new)
        for old in states
        for new in states
    }
    for _ in range(20):
        snapshot = {
            (old, new): is_legal_project_phase_transition(old, new)
            for old in states
            for new in states
        }
        assert snapshot == first
        for (old, new), expected in first.items():
            assert can_transition(old, new) is expected
