"""Tests for the normative Run lifecycle transition rules (DEV-M2-G01).

AC mapping (goal contract DEV-M2-G01):
  * AC-01 (every normative legal transition is accepted): the full
    mainline ``CREATED -> ... -> CLOSED``, the pre-result cancellation
    arcs, and the result-bearing invalidation arcs are all accepted by
    ``is_legal_run_transition`` / ``apply_run_lifecycle_transition`` and by
    the generic ``can_transition`` / ``transition`` API.
  * AC-02 (representative illegal shortcut transitions rejected): the named
    shortcut ``RUNNING_EXTERNAL -> CREATED`` plus stage skips, backwards
    moves, terminal escapes, wrong abort kinds (``RESULT_AVAILABLE ->
    CANCELLED``, ``CREATED -> INVALIDATED``) and no-op same-state arcs are
    all rejected; ``apply``/``transition`` raise ``IllegalTransitionError``
    with a documented message.
  * AC-03 (deterministic, not prompt-dependent): the rule table is an
    immutable ``frozenset`` of exact locked pairs, every decision is stable
    across repeated evaluation, and the table covers exactly the frozen
    ``LifecycleState`` enum (see also test_lifecycle_shared.py).
"""

from __future__ import annotations

import pytest

from scientific_reproduction.core.models import LifecycleState
from scientific_reproduction.core.rules.lifecycle import (
    RUN_LIFECYCLE_TRANSITIONS,
    RUN_MAINLINE,
    TERMINAL_RUN_STATES,
    IllegalTransitionError,
    apply_run_lifecycle_transition,
    is_legal_run_transition,
    is_terminal_run_state,
)
from scientific_reproduction.core.transitions import can_transition, transition

# The complete normative Run rule set, written out in full. Any normative
# change must update this lock deliberately (auditability per AC-03).
LOCKED_RUN_TRANSITIONS: frozenset[tuple[LifecycleState, LifecycleState]] = frozenset(
    {
        # Mainline (05-GOAL-RUN-SCHEMA.md section 7, in order).
        (LifecycleState.CREATED, LifecycleState.READY),
        (LifecycleState.READY, LifecycleState.DISPATCHED),
        (LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL),
        (LifecycleState.RUNNING_EXTERNAL, LifecycleState.RESULT_AVAILABLE),
        (LifecycleState.RESULT_AVAILABLE, LifecycleState.ANALYZING),
        (LifecycleState.ANALYZING, LifecycleState.SUBMITTED_FOR_REVIEW),
        (LifecycleState.SUBMITTED_FOR_REVIEW, LifecycleState.CLOSED),
        # Cancellation: run abandoned before any result exists.
        (LifecycleState.CREATED, LifecycleState.CANCELLED),
        (LifecycleState.READY, LifecycleState.CANCELLED),
        (LifecycleState.DISPATCHED, LifecycleState.CANCELLED),
        (LifecycleState.RUNNING_EXTERNAL, LifecycleState.CANCELLED),
        # Invalidation: results produced but not trustworthy
        # (cf. retry-policy schema "invalidate_run_on").
        (LifecycleState.RESULT_AVAILABLE, LifecycleState.INVALIDATED),
        (LifecycleState.ANALYZING, LifecycleState.INVALIDATED),
        (LifecycleState.SUBMITTED_FOR_REVIEW, LifecycleState.INVALIDATED),
    }
)


# ---------------------------------------------------------------------------
# AC-01: every normative legal transition is accepted
# ---------------------------------------------------------------------------


def test_run_mainline_transitions_accepted() -> None:
    for old, new in zip(RUN_MAINLINE, RUN_MAINLINE[1:]):
        assert is_legal_run_transition(old, new), f"{old} -> {new} must be legal"
        assert apply_run_lifecycle_transition(old, new) == new
        assert can_transition(old, new) is True
        assert transition(old, new) == new


def test_run_cancellation_arcs_accepted() -> None:
    pre_result = {
        LifecycleState.CREATED,
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
    }
    for state in pre_result:
        assert is_legal_run_transition(state, LifecycleState.CANCELLED), (
            f"{state} -> CANCELLED must be legal"
        )
        assert (
            apply_run_lifecycle_transition(state, LifecycleState.CANCELLED)
            == LifecycleState.CANCELLED
        )
        assert transition(state, LifecycleState.CANCELLED) == LifecycleState.CANCELLED


def test_run_invalidation_arcs_accepted() -> None:
    result_bearing = {
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
    }
    for state in result_bearing:
        assert is_legal_run_transition(state, LifecycleState.INVALIDATED), (
            f"{state} -> INVALIDATED must be legal"
        )
        assert (
            apply_run_lifecycle_transition(state, LifecycleState.INVALIDATED)
            == LifecycleState.INVALIDATED
        )
        assert transition(state, LifecycleState.INVALIDATED) == LifecycleState.INVALIDATED


# ---------------------------------------------------------------------------
# AC-02: representative illegal shortcut transitions are rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Named shortcut: backwards move without the legal path.
        (LifecycleState.RUNNING_EXTERNAL, LifecycleState.CREATED),
        # Skipping stages of the mainline.
        (LifecycleState.CREATED, LifecycleState.CLOSED),
        (LifecycleState.CREATED, LifecycleState.SUBMITTED_FOR_REVIEW),
        (LifecycleState.READY, LifecycleState.RESULT_AVAILABLE),
        (LifecycleState.DISPATCHED, LifecycleState.RESULT_AVAILABLE),
        (LifecycleState.ANALYZING, LifecycleState.CLOSED),
        # Backwards moves.
        (LifecycleState.RESULT_AVAILABLE, LifecycleState.READY),
        (LifecycleState.SUBMITTED_FOR_REVIEW, LifecycleState.RUNNING_EXTERNAL),
        # Terminal states cannot be left.
        (LifecycleState.CLOSED, LifecycleState.READY),
        (LifecycleState.CLOSED, LifecycleState.CANCELLED),
        (LifecycleState.CANCELLED, LifecycleState.READY),
        (LifecycleState.CANCELLED, LifecycleState.INVALIDATED),
        (LifecycleState.INVALIDATED, LifecycleState.ANALYZING),
        (LifecycleState.INVALIDATED, LifecycleState.CLOSED),
        # Wrong abort kind: a result-bearing run is invalidated, not cancelled.
        (LifecycleState.RESULT_AVAILABLE, LifecycleState.CANCELLED),
        # Wrong abort kind: a pre-result run is cancelled, not invalidated.
        (LifecycleState.CREATED, LifecycleState.INVALIDATED),
        # No-op same-state arcs are not transitions.
        (LifecycleState.CREATED, LifecycleState.CREATED),
        (LifecycleState.SUBMITTED_FOR_REVIEW, LifecycleState.SUBMITTED_FOR_REVIEW),
    ],
)
def test_run_shortcut_transitions_rejected(
    old: LifecycleState, new: LifecycleState
) -> None:
    assert is_legal_run_transition(old, new) is False
    assert can_transition(old, new) is False
    with pytest.raises(IllegalTransitionError):
        apply_run_lifecycle_transition(old, new)
    with pytest.raises(IllegalTransitionError):
        transition(old, new)


def test_apply_run_rejects_illegal_with_documented_error() -> None:
    with pytest.raises(IllegalTransitionError) as excinfo:
        apply_run_lifecycle_transition(
            LifecycleState.RUNNING_EXTERNAL, LifecycleState.CREATED
        )
    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.subject == "run-lifecycle"
    assert excinfo.value.from_state == LifecycleState.RUNNING_EXTERNAL
    assert excinfo.value.to_state == LifecycleState.CREATED
    message = str(excinfo.value)
    assert "run-lifecycle" in message
    assert "RUNNING_EXTERNAL" in message
    assert "CREATED" in message


# ---------------------------------------------------------------------------
# Structural properties of the locked rule table
# ---------------------------------------------------------------------------


def test_run_rule_table_is_locked_exact_data() -> None:
    # The module table must equal the complete literal rule set above.
    assert RUN_LIFECYCLE_TRANSITIONS == LOCKED_RUN_TRANSITIONS


def test_run_table_has_no_same_state_arcs() -> None:
    for old, new in RUN_LIFECYCLE_TRANSITIONS:
        assert old != new, "a transition must record a change, never a no-op"


def test_run_table_covers_every_lifecycle_state() -> None:
    covered = {old for old, _ in RUN_LIFECYCLE_TRANSITIONS} | {
        new for _, new in RUN_LIFECYCLE_TRANSITIONS
    }
    assert covered == set(LifecycleState)


def test_closed_reachable_only_from_submitted_for_review() -> None:
    sources = {
        old for old, new in RUN_LIFECYCLE_TRANSITIONS if new == LifecycleState.CLOSED
    }
    assert sources == {LifecycleState.SUBMITTED_FOR_REVIEW}


def test_run_terminal_states_match_locked_set() -> None:
    assert TERMINAL_RUN_STATES == frozenset(
        {
            LifecycleState.CLOSED,
            LifecycleState.CANCELLED,
            LifecycleState.INVALIDATED,
        }
    )
    for state in LifecycleState:
        assert is_terminal_run_state(state) is (state in TERMINAL_RUN_STATES), (
            f"terminal predicate mismatch for {state}"
        )


# ---------------------------------------------------------------------------
# AC-03: deterministic, not prompt-dependent
# ---------------------------------------------------------------------------


def test_run_transition_decisions_stable_across_repeated_evaluation() -> None:
    states = list(LifecycleState)
    first = {
        (old, new): is_legal_run_transition(old, new)
        for old in states
        for new in states
    }
    for _ in range(20):
        snapshot = {
            (old, new): is_legal_run_transition(old, new)
            for old in states
            for new in states
        }
        assert snapshot == first
        for (old, new), expected in first.items():
            assert can_transition(old, new) is expected
