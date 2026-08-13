"""Shared/generic transition-API and determinism tests (DEV-M2-G01, AC-03).

Covered behaviors:
  * the generic ``can_transition`` / ``transition`` /
    ``allowed_transitions_for`` / ``is_terminal`` API dispatches on the
    state type against the normative rule tables in
    ``core.rules.lifecycle``;
  * unregistered enum types, mixed enum types and plain strings are
    rejected with ``TypeError`` -- the rule tables key on enum members,
    never on free text or environment data;
  * AC-03: both rule tables are immutable ``frozenset`` data, are identical
    after module re-import, and every decision is a pure function of the
    two inputs.
"""

from __future__ import annotations

import importlib

import pytest

from scientific_reproduction.core import models as m
from scientific_reproduction.core.models import LifecycleState, ProjectPhase
from scientific_reproduction.core.rules import lifecycle
from scientific_reproduction.core.rules.lifecycle import (
    PROJECT_PHASE_TRANSITIONS,
    RUN_LIFECYCLE_TRANSITIONS,
    IllegalTransitionError,
)
from scientific_reproduction.core.transitions import (
    allowed_transitions_for,
    can_transition,
    is_terminal,
    transition,
)

# ---------------------------------------------------------------------------
# Generic dispatch on the state type
# ---------------------------------------------------------------------------


def test_generic_api_dispatch_on_state_type() -> None:
    assert can_transition(ProjectPhase.INITIALIZING, ProjectPhase.SOURCE_ACQUISITION)
    assert can_transition(LifecycleState.CREATED, LifecycleState.READY)
    assert not can_transition(ProjectPhase.EXECUTING, ProjectPhase.INITIALIZING)
    assert not can_transition(LifecycleState.RUNNING_EXTERNAL, LifecycleState.CREATED)


def test_generic_transition_returns_new_state() -> None:
    assert transition(ProjectPhase.INITIALIZING, ProjectPhase.SOURCE_ACQUISITION) is (
        ProjectPhase.SOURCE_ACQUISITION
    )
    assert transition(LifecycleState.CREATED, LifecycleState.READY) is (
        LifecycleState.READY
    )


def test_generic_transition_raises_illegal_transition_error() -> None:
    with pytest.raises(IllegalTransitionError):
        transition(LifecycleState.RUNNING_EXTERNAL, LifecycleState.CREATED)
    with pytest.raises(IllegalTransitionError):
        transition(ProjectPhase.COMPLETED, ProjectPhase.EXECUTING)


@pytest.mark.parametrize(
    "unregistered",
    [
        m.RunType.INDEPENDENT_REPLICATE,
        m.ScientificReview.PASS,
        m.PlanStatus.FROZEN,
        m.GateStatus.OPEN,
    ],
)
def test_generic_api_rejects_unregistered_enum_types(unregistered: m.Enum) -> None:
    with pytest.raises(TypeError):
        can_transition(unregistered, unregistered)
    with pytest.raises(TypeError):
        transition(unregistered, unregistered)
    with pytest.raises(TypeError):
        allowed_transitions_for(unregistered)
    with pytest.raises(TypeError):
        is_terminal(unregistered)


def test_generic_api_rejects_mixed_enum_types() -> None:
    with pytest.raises(TypeError):
        can_transition(ProjectPhase.INITIALIZING, LifecycleState.CREATED)
    with pytest.raises(TypeError):
        transition(LifecycleState.CREATED, ProjectPhase.INITIALIZING)
    # Mixed types are rejected before any table lookup: even a nominally
    # legal project pair whose target happens to be a run state must raise
    # TypeError.
    with pytest.raises(TypeError):
        can_transition(ProjectPhase.INITIALIZING, LifecycleState.READY)


def test_generic_api_rejects_plain_strings() -> None:
    with pytest.raises(TypeError):
        can_transition("CREATED", LifecycleState.READY)
    with pytest.raises(TypeError):
        can_transition(LifecycleState.CREATED, "READY")
    with pytest.raises(TypeError):
        transition("INITIALIZING", "SOURCE_ACQUISITION")
    with pytest.raises(TypeError):
        is_terminal("PAUSED")


def test_allowed_transitions_for_run() -> None:
    assert allowed_transitions_for(LifecycleState.CREATED) == frozenset(
        {LifecycleState.READY, LifecycleState.CANCELLED}
    )
    assert allowed_transitions_for(LifecycleState.RESULT_AVAILABLE) == frozenset(
        {LifecycleState.ANALYZING, LifecycleState.INVALIDATED}
    )
    assert allowed_transitions_for(LifecycleState.CLOSED) == frozenset()


def test_allowed_transitions_for_project() -> None:
    assert allowed_transitions_for(ProjectPhase.EXECUTING) == frozenset(
        {
            ProjectPhase.FINAL_VALIDATION,
            ProjectPhase.REPLANNING,
            ProjectPhase.PAUSED,
            ProjectPhase.WAITING_HUMAN,
            ProjectPhase.WAITING_RESOURCE,
        }
    )
    assert allowed_transitions_for(ProjectPhase.REPLANNING) == frozenset(
        {
            ProjectPhase.PLAN_AUDIT,
            ProjectPhase.PAUSED,
            ProjectPhase.WAITING_HUMAN,
            ProjectPhase.WAITING_RESOURCE,
        }
    )
    assert allowed_transitions_for(ProjectPhase.COMPLETED) == frozenset()


def test_generic_is_terminal() -> None:
    for state in (
        LifecycleState.CLOSED,
        LifecycleState.CANCELLED,
        LifecycleState.INVALIDATED,
        ProjectPhase.COMPLETED,
    ):
        assert is_terminal(state) is True, f"{state} must be terminal"
    for state in (
        LifecycleState.READY,
        LifecycleState.ANALYZING,
        ProjectPhase.PAUSED,
        ProjectPhase.EXECUTING,
        ProjectPhase.REPLANNING,
    ):
        assert is_terminal(state) is False, f"{state} must not be terminal"


# ---------------------------------------------------------------------------
# AC-03: rule tables are immutable data with deterministic content
# ---------------------------------------------------------------------------


def test_rule_tables_are_immutable_data() -> None:
    assert isinstance(PROJECT_PHASE_TRANSITIONS, frozenset)
    assert isinstance(RUN_LIFECYCLE_TRANSITIONS, frozenset)
    with pytest.raises(AttributeError):
        PROJECT_PHASE_TRANSITIONS.add(  # type: ignore[attr-defined]
            (ProjectPhase.COMPLETED, ProjectPhase.PAUSED)
        )
    with pytest.raises(AttributeError):
        RUN_LIFECYCLE_TRANSITIONS.add(  # type: ignore[attr-defined]
            (LifecycleState.CLOSED, LifecycleState.READY)
        )
    for old, new in PROJECT_PHASE_TRANSITIONS:
        assert isinstance(old, ProjectPhase)
        assert isinstance(new, ProjectPhase)
    for old, new in RUN_LIFECYCLE_TRANSITIONS:
        assert isinstance(old, LifecycleState)
        assert isinstance(new, LifecycleState)


def test_rule_tables_identical_after_module_reimport() -> None:
    reloaded = importlib.reload(lifecycle)
    assert reloaded.PROJECT_PHASE_TRANSITIONS == PROJECT_PHASE_TRANSITIONS
    assert reloaded.RUN_LIFECYCLE_TRANSITIONS == RUN_LIFECYCLE_TRANSITIONS
    assert len(reloaded.PROJECT_PHASE_TRANSITIONS) == len(PROJECT_PHASE_TRANSITIONS)
    assert len(reloaded.RUN_LIFECYCLE_TRANSITIONS) == len(RUN_LIFECYCLE_TRANSITIONS)
    # Predicates on the reloaded tables agree with the first import.
    assert (
        reloaded.is_legal_run_transition(
            LifecycleState.READY, LifecycleState.DISPATCHED
        )
        is True
    )
    assert (
        reloaded.is_legal_project_phase_transition(
            ProjectPhase.EXECUTING, ProjectPhase.INITIALIZING
        )
        is False
    )


def test_rule_tables_hold_only_normative_enum_members() -> None:
    # No outcome, review, plan-status or other enum value leaks into the
    # lifecycle tables (04-PROJECT-LIFECYCLE.md section 1 and
    # 05-GOAL-RUN-SCHEMA.md section 7 separation rules).
    project_states = {state for pair in PROJECT_PHASE_TRANSITIONS for state in pair}
    run_states = {state for pair in RUN_LIFECYCLE_TRANSITIONS for state in pair}
    assert project_states <= set(ProjectPhase)
    assert run_states <= set(LifecycleState)
    assert not (project_states & set(m.ReproductionOutcome))
    assert not (run_states & set(m.ScientificReview))
