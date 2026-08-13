"""Runtime role authorization guards for the worker/runtime APIs (DEV-M6-G03).

Implements the **runtime authorization checks** deliverable of
DEV-M6-G03: guard functions that enforce the authority boundaries of
``03-ROLE-AND-PERMISSION-SPEC.md`` at the real mutation surfaces of the
deterministic runtime APIs, decided entirely by the frozen role-action
matrix of ``core/permissions.py``. The four acceptance criteria map to
four guards:

* AC-01 -- :func:`enforce_goal_mutation`: an Experiment/Computation
  worker cannot mutate a frozen Goal. The guard sits at the
  goal-contract mutation boundary (the ``planning.plan`` goal-family
  draft authoring and the ``planning.freeze`` frozen Goal variants):
  it selects ``FROZEN_GOAL_MUTATE`` for a frozen Goal Contract
  (``goal.frozen`` True -- the record the plan freeze produced) and
  ``GOAL_MUTATE`` for a draft, and the matrix denies both to every
  worker role (SS5/SS6: workers may not "create Goals" or change
  track; SS2: the Supervisor alone creates/modifies/versions formal
  Goals);
* AC-02 -- :func:`enforce_recovery_goal_creation` and
  :func:`enforce_requirement_closure`: a Worker cannot create a formal
  Recovery Goal nor close a Requirement. No recovery-goal creation or
  requirement-closure API exists in v0.1 (the checks are the
  deliverable; ``08-STRICT-RECOVERY-CLOSURE.md`` SS1: Recovery is
  entered "only after a formal strict failure/inconclusive state and
  Supervisor decision"); closure stays Supervisor-only (SS2: "close
  Goals/Requirements");
* AC-03 -- :func:`enforce_plan_decision`: Research cannot make
  scientific Plan decisions. The guard maps the
  ``core.models.DecisionType`` the plan-decision APIs accept to its
  matrix action and denies research-role callers -- research can
  propose/prepare (its allowed matrix actions are the SS3 "may" list)
  but never decide;
* AC-04 -- :func:`enforce_scientific_interpretation`: Monitor cannot
  make scientific interpretation decisions. The guard denies
  monitor-role callers -- monitors observe/report only (SS4: may not
  "classify a Goal as scientifically PASS/FAIL", "enter Recovery
  autonomously", "alter statistical design";
  ``05-GOAL-RUN-SCHEMA.md`` SS7: PASS/FAIL is a review decision stored
  separately).

Guard contract
--------------
Every guard is a pure deterministic function of (role, target): the
frozen matrix decides on ``(role, action)`` -- with the action selected
from the target's frozen state (e.g. the Goal Contract's ``frozen``
flag) -- and no state is read anywhere else: no I/O, no wall clock, no
network, no registry access (state enters only through the injected
arguments, so the guards are unit-testable without touching any
registry). Allowed calls return the full ``PermissionAssessment``
(role, action, matched rule, ruleset version) so the caller can persist
the audit trail; denied calls raise the stable
``PermissionDeniedError`` of ``core/permissions.py``. ``TypeError`` at
the public type boundaries (never ``ValueError`` for wrong types);
``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

from scientific_reproduction.core.models import (
    DecisionType,
    GoalContract,
    ReproductionRequirement,
)
from scientific_reproduction.core.permissions import (
    Action,
    PermissionAssessment,
    PermissionDeniedError,
    Role,
    action_for_decision_type,
    check_action_allowed,
)

__all__ = [
    "enforce_goal_mutation",
    "enforce_plan_decision",
    "enforce_recovery_goal_creation",
    "enforce_requirement_closure",
    "enforce_scientific_interpretation",
]


def enforce_goal_mutation(role: Role, goal: GoalContract) -> PermissionAssessment:
    """Guard the goal-contract mutation surface (AC-01).

    An Experiment/Computation worker cannot mutate a frozen Goal: the
    frozen variant (``goal.frozen`` True -- the record the plan freeze
    produced) is decided as the ``FROZEN_GOAL_MUTATE`` action, a draft
    as ``GOAL_MUTATE``, and the role-action matrix grants both to the
    Supervisor alone (``03-ROLE-AND-PERMISSION-SPEC.md`` SS2/SS5/SS6).
    Denied calls raise ``PermissionDeniedError`` with the full decision
    record attached (``exc.assessment``); allowed calls return the
    assessment (the audit trail).

    Args:
        role: the caller's matrix role.
        goal: the Goal Contract the caller attempts to mutate (the
            exact frozen or draft record; its ``frozen`` flag selects
            the action).

    Returns:
        The ``PermissionAssessment`` of the decision (allowed calls).

    Raises:
        TypeError: ``role`` is not a ``Role`` member, or ``goal`` is not
            a ``GoalContract``.
        PermissionDeniedError: the matrix denies the action (stable
            one-line message; ``exc.assessment`` carries the decision
            record).
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    if not isinstance(goal, GoalContract):
        raise TypeError(
            f"goal must be a GoalContract, got {type(goal).__name__}"
        )
    action = Action.FROZEN_GOAL_MUTATE if goal.frozen else Action.GOAL_MUTATE
    label = (
        f"frozen goal contract {goal.goal_id!r}"
        if goal.frozen
        else f"goal contract {goal.goal_id!r}"
    )
    return _guard(role, action, label)


def enforce_recovery_goal_creation(
    role: Role, target: str | None = None
) -> PermissionAssessment:
    """Guard the formal Recovery Goal creation surface (AC-02).

    A Worker cannot create a formal Recovery Goal: the action
    ``RECOVERY_GOAL_CREATE`` is granted to the Supervisor alone
    (``03-ROLE-AND-PERMISSION-SPEC.md`` SS2 -- the Supervisor decides
    strict/recovery/redesign transitions; SS3 research may not "decide
    Recovery actions"; SS8 diagnosis may not "create a Recovery
    protocol"; SS5/SS6 workers may not "create Goals").
    ``08-STRICT-RECOVERY-CLOSURE.md`` SS1: Recovery is entered only
    after a formal strict failure/inconclusive state and Supervisor
    decision. No recovery-goal creation API exists in v0.1 -- this
    check is the deliverable and guards the surface wherever the
    Supervisor flow lands it.

    Args:
        role: the caller's matrix role.
        target: optional label of the would-be Recovery Goal (used in
            the stable message only; ``None`` defaults to "a formal
            Recovery Goal").

    Returns:
        The ``PermissionAssessment`` of the decision (allowed calls).

    Raises:
        TypeError: ``role`` is not a ``Role`` member, or ``target`` is
            neither a str nor None.
        PermissionDeniedError: the matrix denies the action (stable
            one-line message; ``exc.assessment`` carries the decision
            record).
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    if target is not None and not isinstance(target, str):
        raise TypeError(
            f"target must be a str or None, got {type(target).__name__}"
        )
    return _guard(
        role,
        Action.RECOVERY_GOAL_CREATE,
        target if target is not None else "a formal Recovery Goal",
    )


def enforce_requirement_closure(
    role: Role, requirement: ReproductionRequirement
) -> PermissionAssessment:
    """Guard the requirement-closure surface (AC-02).

    A Worker cannot close a Requirement: the action
    ``REQUIREMENT_CLOSE`` is granted to the Supervisor alone
    (``03-ROLE-AND-PERMISSION-SPEC.md`` SS2: the Supervisor alone may
    "close Goals/Requirements"; the closure stays Supervisor-only --
    worker result packages may reference Requirements by id
    (``workers/results.py`` AC-02) but can never close them). No
    requirement-closure API exists in v0.1 -- this check is the
    deliverable and guards the surface wherever the closure flow lands
    it.

    Args:
        role: the caller's matrix role.
        requirement: the ``ReproductionRequirement`` record the caller
            attempts to close (its id names the target in the stable
            message; the decision itself is a pure function of
            (role, action)).

    Returns:
        The ``PermissionAssessment`` of the decision (allowed calls).

    Raises:
        TypeError: ``role`` is not a ``Role`` member, or ``requirement``
            is not a ``ReproductionRequirement``.
        PermissionDeniedError: the matrix denies the action (stable
            one-line message; ``exc.assessment`` carries the decision
            record).
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    if not isinstance(requirement, ReproductionRequirement):
        raise TypeError(
            "requirement must be a ReproductionRequirement, got"
            f" {type(requirement).__name__}"
        )
    return _guard(
        role,
        Action.REQUIREMENT_CLOSE,
        f"requirement {requirement.requirement_id!r}",
    )


def enforce_plan_decision(
    role: Role, decision_type: DecisionType
) -> PermissionAssessment:
    """Guard the scientific Plan decision surface (AC-03).

    Research cannot make scientific Plan decisions: the guard maps the
    ``core.models.DecisionType`` the plan-decision APIs accept
    (``PLAN_FREEZE`` through ``PROJECT_OUTCOME``) to its matrix action
    (``action_for_decision_type``) and the matrix denies every decision
    action to the Research role -- research can propose/prepare (its
    allowed matrix actions are the SS3 "may" list) but never decide
    (``03-ROLE-AND-PERMISSION-SPEC.md`` SS3: research may not change
    Goals, change acceptance criteria or decide Recovery actions;
    ``09-RESEARCH-SUBSYSTEM.md`` SS3: "Only Supervisor may issue formal
    Research Requests").

    Args:
        role: the caller's matrix role.
        decision_type: the decision type of the plan-decision surface
            the caller attempts (a ``core.models.DecisionType`` member).

    Returns:
        The ``PermissionAssessment`` of the decision (allowed calls).

    Raises:
        TypeError: ``role`` is not a ``Role`` member, or ``decision_type``
            is not a ``DecisionType`` member.
        PermissionDeniedError: the matrix denies the action (stable
            one-line message; ``exc.assessment`` carries the decision
            record).
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    action = action_for_decision_type(decision_type)
    return _guard(role, action, f"plan decision {decision_type.value!r}")


def enforce_scientific_interpretation(
    role: Role, target: str | None = None
) -> PermissionAssessment:
    """Guard the scientific interpretation surface (AC-04).

    Monitor cannot make scientific interpretation decisions: the action
    ``SCIENTIFIC_INTERPRETATION`` (a PASS/FAIL or other scientific
    verdict -- ``05-GOAL-RUN-SCHEMA.md`` SS7: "Scientific PASS/FAIL is
    not a Run lifecycle state; it is a review decision stored
    separately") is denied to the Monitor role -- monitors observe and
    report only (``03-ROLE-AND-PERMISSION-SPEC.md`` SS4: may not
    "classify a Goal as scientifically PASS/FAIL", "change scientific
    parameters", "alter statistical design" or "enter Recovery
    autonomously"; ``13-EXECUTION-MONITOR.md`` SS6: a monitor can say
    "job exited with code X" but cannot decide that a DFT model should
    use a new functional).

    Args:
        role: the caller's matrix role.
        target: optional label of the object an interpretation would
            decide (e.g. the observed run id; used in the stable
            message only; ``None`` defaults to "a scientific
            interpretation").

    Returns:
        The ``PermissionAssessment`` of the decision (allowed calls).

    Raises:
        TypeError: ``role`` is not a ``Role`` member, or ``target`` is
            neither a str nor None.
        PermissionDeniedError: the matrix denies the action (stable
            one-line message; ``exc.assessment`` carries the decision
            record).
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    if target is not None and not isinstance(target, str):
        raise TypeError(
            f"target must be a str or None, got {type(target).__name__}"
        )
    return _guard(
        role,
        Action.SCIENTIFIC_INTERPRETATION,
        target if target is not None else "a scientific interpretation",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _guard(role: Role, action: Action, target_label: str) -> PermissionAssessment:
    """Evaluate the matrix and raise ``PermissionDeniedError`` when denied.

    The internal enforcement gate shared by every guard: the decision
    is a pure function of (role, action) plus the injected target label
    (which only enters the stable message). No I/O, no wall clock, no
    network, no registry access.
    """
    assessment = check_action_allowed(role, action)
    if not assessment.allowed:
        raise PermissionDeniedError(
            f"role {role.value!r} is not permitted to {action.value} on"
            f" {target_label}: rule {assessment.matched_rule_id} of the"
            f" role-action matrix (ruleset {assessment.ruleset_version})"
            " denies the action",
            assessment=assessment,
        )
    return assessment
