"""Generic deterministic transition API (DEV-M2-G01).

A thin, stateless facade over the normative lifecycle rule tables in
``scientific_reproduction.core.rules.lifecycle``. Any registered enum type
(``ProjectPhase``, ``LifecycleState``) can be driven through
``can_transition`` / ``transition``; the rule tables themselves are the
normative data and are never consulted in any environment-dependent way, so
every call is deterministic (AC-03 of DEV-M2-G01).

Design:

* ``can_transition(state, new_state)`` -- pure membership check against the
  registered rule table for ``type(state)``.
* ``transition(state, new_state)`` -- same check, but raises
  ``IllegalTransitionError`` (a documented ``ValueError`` subclass) for
  illegal pairs, and returns ``new_state`` for legal ones.
* ``allowed_transitions_for(state)`` -- the outgoing arcs of ``state`` as a
  ``frozenset`` of target states.
* ``is_terminal(state)`` -- True when ``state`` has no outgoing arcs.

Unregistered enum types and mixed enum types are rejected with
``TypeError``; plain strings are rejected the same way (the rule tables key
on enum members, never on free text).
"""

from __future__ import annotations

from enum import Enum
from typing import cast

from scientific_reproduction.core.models import LifecycleState, ProjectPhase
from scientific_reproduction.core.rules.lifecycle import (
    PROJECT_PHASE_TRANSITIONS,
    RUN_LIFECYCLE_TRANSITIONS,
    IllegalTransitionError,
)

__all__ = [
    "can_transition",
    "transition",
    "allowed_transitions_for",
    "is_terminal",
]

_RULE_TABLES: dict[type[Enum], frozenset[tuple[Enum, Enum]]] = {
    ProjectPhase: cast(frozenset[tuple[Enum, Enum]], PROJECT_PHASE_TRANSITIONS),
    LifecycleState: cast(frozenset[tuple[Enum, Enum]], RUN_LIFECYCLE_TRANSITIONS),
}

_SUBJECTS: dict[type[Enum], str] = {
    ProjectPhase: "project-phase",
    LifecycleState: "run-lifecycle",
}


def _table_for(state: Enum) -> frozenset[tuple[Enum, Enum]]:
    """Return the rule table registered for ``type(state)``."""
    table = _RULE_TABLES.get(type(state))
    if table is None:
        registered = ", ".join(sorted(t.__name__ for t in _RULE_TABLES))
        raise TypeError(
            f"no transition rules registered for state type "
            f"{type(state).__name__}; registered types: {registered}"
        )
    return table


def _require_same_type(state: Enum, new_state: Enum) -> None:
    if type(new_state) is not type(state):
        raise TypeError(
            f"cannot transition between different enum types: "
            f"{type(state).__name__} -> {type(new_state).__name__}"
        )


def can_transition(state: Enum, new_state: Enum) -> bool:
    """Return whether ``state -> new_state`` is legal for ``state``'s type.

    Raises:
        TypeError: unregistered state type, or ``state`` and ``new_state``
            are different enum types.
    """
    _require_same_type(state, new_state)
    return (state, new_state) in _table_for(state)


def transition(state: Enum, new_state: Enum) -> Enum:
    """Validate ``state -> new_state`` and return ``new_state``.

    Raises:
        TypeError: unregistered state type, or mixed enum types.
        IllegalTransitionError: the pair is not in the normative rule table.
    """
    _require_same_type(state, new_state)
    if not can_transition(state, new_state):
        raise IllegalTransitionError(_SUBJECTS[type(state)], state, new_state)
    return new_state


def allowed_transitions_for(state: Enum) -> frozenset[Enum]:
    """Return the outgoing target states of ``state`` (never ``state``)."""
    table = _table_for(state)
    return frozenset(new for (old, new) in table if old == state)


def is_terminal(state: Enum) -> bool:
    """Return True when ``state`` has no outgoing normative transitions."""
    table = _table_for(state)
    return not any(old == state for old, _ in table)
