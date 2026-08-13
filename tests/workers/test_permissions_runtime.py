"""Runtime role authorization guards (DEV-M6-G03, workers).

Every test name contains "permission" so ``python -m pytest -q
tests/workers -k permission`` selects this suite. The ``ac01``..
``ac04`` sections map one-to-one to the acceptance criteria of
DEV-M6-G03:

* ``ac01`` -- an Experiment/Computation worker cannot mutate a frozen
  Goal: :func:`enforce_goal_mutation` rejects worker-role callers at
  the goal-contract mutation surface (the frozen Goal Contract the plan
  freeze produced; a draft is denied as well), and the Supervisor is
  allowed;
* ``ac02`` -- a Worker cannot create a formal Recovery Goal nor close a
  Requirement: :func:`enforce_recovery_goal_creation` /
  :func:`enforce_requirement_closure` reject every worker role (and
  Research/Monitor); closure stays Supervisor-only;
* ``ac03`` -- Research cannot make scientific Plan decisions:
  :func:`enforce_plan_decision` rejects research-role callers for every
  ``core.models.DecisionType``; research's propose/prepare actions stay
  allowed;
* ``ac04`` -- Monitor cannot make scientific interpretation decisions:
  :func:`enforce_scientific_interpretation` rejects monitor-role
  callers; monitor observation actions stay allowed.

The guards are pure deterministic functions of (role, target): the
fixtures build the frozen/draft Goal Contracts, Requirement records and
decision types in memory only -- no registry, no file I/O, no wall
clock. Denied calls raise the stable ``PermissionDeniedError`` carrying
the full assessment; allowed calls return the assessment.
"""

from __future__ import annotations

import inspect

import pytest

from scientific_reproduction.core.models import (
    Criticality,
    DecisionType,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    ReproductionRequirement,
    RequirementOutcome,
    WorkerRole,
)
from scientific_reproduction.core.permissions import (
    RESEARCH_ACTIONS,
    ROLE_ACTION_RULESET_VERSION,
    Action,
    PermissionAssessment,
    PermissionDeniedError,
    PermissionInput,
    Role,
    RolePermissionError,
    action_for_decision_type,
    check_action_allowed,
    is_action_allowed,
    role_from_worker_role,
)
from scientific_reproduction.workers.permissions import (
    enforce_goal_mutation,
    enforce_plan_decision,
    enforce_recovery_goal_creation,
    enforce_requirement_closure,
    enforce_scientific_interpretation,
)

#: Every worker role of the matrix (the AC-01/AC-02 worker surfaces).
WORKER_ROLES: tuple[Role, ...] = (
    Role.EXPERIMENT_WORKER,
    Role.COMPUTATION_WORKER,
    Role.ANALYSIS_WORKER,
    Role.DIAGNOSIS_WORKER,
)


def make_goal(
    *,
    frozen: bool,
    version: str = "v1",
    track: GoalTrack = GoalTrack.STRICT_REPRODUCTION,
) -> GoalContract:
    """Build a Goal Contract in memory (no file access)."""
    return GoalContract(
        goal_id="GOAL-1",
        title="Reproduce the reported tensile strength",
        unit_process_type="tensile_test",
        track=track,
        objective="Measure the reported tensile strength",
        requirement_ids=["REQ-1"],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=frozen),
        analysis_protocol_ref="PROTO-1",
        replication=GoalReplication(
            independent_required=True, planned_n_policy="n=3"
        ),
        version=version,
        frozen=frozen,
    )


def make_requirement(
    outcome: RequirementOutcome = RequirementOutcome.OPEN,
) -> ReproductionRequirement:
    """Build a Requirement record in memory (no file access)."""
    return ReproductionRequirement(
        requirement_id="REQ-1",
        statement="Reproduce the reported value",
        inventory_items=["INV-1"],
        criticality=Criticality.CRITICAL,
        goal_ids=["GOAL-1"],
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# AC-01 -- Experiment/Computation worker cannot mutate a frozen Goal
# ---------------------------------------------------------------------------


def test_permission_ac01_experiment_worker_guard_rejects_frozen_goal_mutation():
    goal = make_goal(frozen=True)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.EXPERIMENT_WORKER, goal)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.FROZEN_GOAL_MUTATE
    assert assessment.input.role is Role.EXPERIMENT_WORKER
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert assessment.ruleset_version == ROLE_ACTION_RULESET_VERSION


def test_permission_ac01_computation_worker_guard_rejects_frozen_goal_mutation():
    goal = make_goal(frozen=True)
    role = role_from_worker_role(WorkerRole.COMPUTATION_WORKER)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(role, goal)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.FROZEN_GOAL_MUTATE
    assert assessment.input.role is Role.COMPUTATION_WORKER
    assert "frozen goal contract 'GOAL-1'" in str(exc.value)


def test_permission_ac01_analysis_and_diagnosis_workers_guard_reject_frozen_goal_mutation():
    goal = make_goal(frozen=True)
    for role in (Role.ANALYSIS_WORKER, Role.DIAGNOSIS_WORKER):
        with pytest.raises(PermissionDeniedError) as exc:
            enforce_goal_mutation(role, goal)
        assert exc.value.assessment.input.action is Action.FROZEN_GOAL_MUTATE


def test_permission_ac01_worker_guard_rejects_draft_goal_mutation_too():
    # A draft goal is decided as GOAL_MUTATE and denied to workers as
    # well (03-ROLE-AND-PERMISSION-SPEC.md SS5: workers may not create
    # or change Goals in any state).
    goal = make_goal(frozen=False)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.EXPERIMENT_WORKER, goal)
    assert exc.value.assessment.input.action is Action.GOAL_MUTATE
    assert "goal contract 'GOAL-1'" in str(exc.value)


def test_permission_ac01_supervisor_guard_allows_frozen_goal_mutation():
    frozen_goal = make_goal(frozen=True)
    assessment = enforce_goal_mutation(Role.SUPERVISOR, frozen_goal)
    assert assessment.allowed is True
    assert assessment.matched_rule_id == "R-PRM-SUP1"
    assert assessment.input.action is Action.FROZEN_GOAL_MUTATE
    draft_assessment = enforce_goal_mutation(
        Role.SUPERVISOR, make_goal(frozen=False)
    )
    assert draft_assessment.allowed is True
    assert draft_assessment.input.action is Action.GOAL_MUTATE


def test_permission_ac01_guard_rejects_non_role_and_non_goal_inputs():
    goal = make_goal(frozen=True)
    with pytest.raises(TypeError):
        enforce_goal_mutation("supervisor", goal)
    with pytest.raises(TypeError):
        enforce_goal_mutation(WorkerRole.EXPERIMENT_WORKER, goal)
    with pytest.raises(TypeError):
        enforce_goal_mutation(Role.SUPERVISOR, "GOAL-1")
    with pytest.raises(TypeError):
        enforce_goal_mutation(Role.SUPERVISOR, None)
    with pytest.raises(TypeError):
        enforce_goal_mutation(None, goal)


def test_permission_ac01_denied_message_is_stable_and_single_line():
    goal = make_goal(frozen=True)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.COMPUTATION_WORKER, goal)
    assert str(exc.value) == (
        "role 'computation_worker' is not permitted to FROZEN_GOAL_MUTATE"
        " on frozen goal contract 'GOAL-1': rule R-PRM-D1 of the"
        " role-action matrix (ruleset 1.0) denies the action"
    )
    assert "\n" not in str(exc.value)


def test_permission_ac01_workers_cannot_mutate_frozen_goals_by_the_matrix():
    # Matrix-level evidence for AC-01: goal mutation actions are denied
    # to every worker role regardless of the target's state.
    for role in WORKER_ROLES:
        for action in (
            Action.GOAL_CREATE,
            Action.GOAL_MUTATE,
            Action.FROZEN_GOAL_MUTATE,
        ):
            assert is_action_allowed(role, action) is False


# ---------------------------------------------------------------------------
# AC-02 -- Worker cannot create a formal Recovery Goal or close Requirement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", WORKER_ROLES)
def test_permission_ac02_worker_guard_rejects_recovery_goal_creation(role):
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_recovery_goal_creation(role)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.RECOVERY_GOAL_CREATE
    assert assessment.input.role is role
    assert assessment.allowed is False
    assert "formal Recovery Goal" in str(exc.value)


def test_permission_ac02_research_and_monitor_guard_reject_recovery_goal_creation():
    # 03-ROLE-AND-PERMISSION-SPEC.md SS3/SS4: research may not "decide
    # Recovery actions"; the monitor may not "enter Recovery
    # autonomously".
    for role in (Role.RESEARCH, Role.MONITOR):
        with pytest.raises(PermissionDeniedError) as exc:
            enforce_recovery_goal_creation(role)
        assert exc.value.assessment.input.action is Action.RECOVERY_GOAL_CREATE
        assert exc.value.assessment.allowed is False


def test_permission_ac02_supervisor_guard_allows_recovery_goal_creation():
    assessment = enforce_recovery_goal_creation(Role.SUPERVISOR)
    assert assessment.allowed is True
    assert assessment.matched_rule_id == "R-PRM-SUP1"
    assert assessment.input.action is Action.RECOVERY_GOAL_CREATE
    # The optional target label is allowed but only names the message.
    targeted = enforce_recovery_goal_creation(Role.SUPERVISOR, target="GOAL-9")
    assert targeted.allowed is True


@pytest.mark.parametrize("role", WORKER_ROLES)
def test_permission_ac02_worker_guard_rejects_requirement_closure(role):
    requirement = make_requirement()
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_requirement_closure(role, requirement)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.REQUIREMENT_CLOSE
    assert assessment.input.role is role
    assert assessment.allowed is False
    assert "REQ-1" in str(exc.value)


def test_permission_ac02_research_guard_rejects_requirement_closure():
    # Closure stays Supervisor-only (03 SS2 "close Goals/Requirements").
    with pytest.raises(PermissionDeniedError):
        enforce_requirement_closure(Role.RESEARCH, make_requirement())
    with pytest.raises(PermissionDeniedError):
        enforce_requirement_closure(Role.MONITOR, make_requirement())


def test_permission_ac02_supervisor_guard_allows_requirement_closure():
    assessment = enforce_requirement_closure(
        Role.SUPERVISOR, make_requirement()
    )
    assert assessment.allowed is True
    assert assessment.matched_rule_id == "R-PRM-SUP1"
    assert assessment.input.action is Action.REQUIREMENT_CLOSE


def test_permission_ac02_guard_type_error_boundaries():
    with pytest.raises(TypeError):
        enforce_requirement_closure(Role.SUPERVISOR, "REQ-1")
    with pytest.raises(TypeError):
        enforce_requirement_closure(Role.SUPERVISOR, None)
    with pytest.raises(TypeError):
        enforce_requirement_closure("supervisor", make_requirement())
    with pytest.raises(TypeError):
        enforce_recovery_goal_creation(7)
    with pytest.raises(TypeError):
        enforce_recovery_goal_creation(Role.SUPERVISOR, target=7)
    with pytest.raises(TypeError):
        enforce_recovery_goal_creation(None)


def test_permission_ac02_workers_denied_closure_and_recovery_actions_by_the_matrix():
    for role in WORKER_ROLES:
        for action in (Action.RECOVERY_GOAL_CREATE, Action.REQUIREMENT_CLOSE):
            assert is_action_allowed(role, action) is False
            assert check_action_allowed(role, action).matched_rule_id == "R-PRM-D1"


# ---------------------------------------------------------------------------
# AC-03 -- Research cannot make scientific Plan decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision_type", list(DecisionType))
def test_permission_ac03_research_guard_rejects_every_plan_decision(decision_type):
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_plan_decision(Role.RESEARCH, decision_type)
    assessment = exc.value.assessment
    assert assessment.input.action is action_for_decision_type(decision_type)
    assert assessment.input.role is Role.RESEARCH
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert f"plan decision {decision_type.value!r}" in str(exc.value)


@pytest.mark.parametrize("decision_type", list(DecisionType))
def test_permission_ac03_supervisor_guard_allows_every_plan_decision(decision_type):
    assessment = enforce_plan_decision(Role.SUPERVISOR, decision_type)
    assert assessment.allowed is True
    assert assessment.matched_rule_id == "R-PRM-SUP1"
    assert assessment.input.action is action_for_decision_type(decision_type)


def test_permission_ac03_research_can_propose_and_prepare_but_not_decide():
    # Research keeps its propose/prepare actions (03 SS3 "may" list --
    # search, acquire, extract, assess, record saturation, respond to
    # Research Requests) but every scientific Plan decision is denied.
    for action in tuple(RESEARCH_ACTIONS):
        assert is_action_allowed(Role.RESEARCH, action) is True
    for decision_action in (
        Action.PLAN_FREEZE,
        Action.GOAL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.ANALYSIS_PROTOCOL_REVISION,
        Action.RECOVERY_ENTRY,
        Action.METHOD_REDESIGN_ENTRY,
        Action.GOAL_REVIEW,
        Action.PROJECT_OUTCOME,
        Action.RESEARCH_REQUEST,
        Action.HUMAN_GATE_OPEN,
        Action.REQUIREMENT_CLOSURE,
    ):
        assert is_action_allowed(Role.RESEARCH, decision_action) is False


def test_permission_ac03_workers_guard_rejects_plan_decisions():
    for role in WORKER_ROLES:
        with pytest.raises(PermissionDeniedError) as exc:
            enforce_plan_decision(role, DecisionType.PLAN_FREEZE)
        assert exc.value.assessment.input.action is Action.PLAN_FREEZE
        assert exc.value.assessment.allowed is False


def test_permission_ac03_plan_decision_guard_type_error_boundaries():
    with pytest.raises(TypeError):
        enforce_plan_decision(Role.RESEARCH, "PLAN_FREEZE")
    with pytest.raises(TypeError):
        enforce_plan_decision(Role.RESEARCH, None)
    with pytest.raises(TypeError):
        enforce_plan_decision(WorkerRole.ANALYSIS_WORKER, DecisionType.PLAN_FREEZE)
    with pytest.raises(TypeError):
        enforce_plan_decision("research", DecisionType.PLAN_FREEZE)


def test_permission_ac03_research_decision_denied_message_is_stable():
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_plan_decision(Role.RESEARCH, DecisionType.GOAL_REVISION)
    assert str(exc.value) == (
        "role 'research' is not permitted to GOAL_REVISION on plan"
        " decision 'GOAL_REVISION': rule R-PRM-D1 of the role-action"
        " matrix (ruleset 1.0) denies the action"
    )
    assert "\n" not in str(exc.value)


# ---------------------------------------------------------------------------
# AC-04 -- Monitor cannot make scientific interpretation decisions
# ---------------------------------------------------------------------------


def test_permission_ac04_monitor_guard_rejects_scientific_interpretation():
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_scientific_interpretation(Role.MONITOR)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.SCIENTIFIC_INTERPRETATION
    assert assessment.input.role is Role.MONITOR
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert "scientific interpretation" in str(exc.value)


def test_permission_ac04_monitor_guard_rejects_interpretation_of_observed_runs():
    # 13-EXECUTION-MONITOR.md SS6: a monitor may say "job exited with
    # code X" but cannot decide a scientific verdict; the guard rejects
    # the verdict action even when the target names the observed run.
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_scientific_interpretation(Role.MONITOR, target="RUN-COMP-017-01")
    assert "RUN-COMP-017-01" in str(exc.value)
    assert exc.value.assessment.input.action is Action.SCIENTIFIC_INTERPRETATION


def test_permission_ac04_monitor_observation_actions_stay_allowed():
    # The monitor keeps its observation/operation actions (03 SS4 "may"
    # list): it observes and reports, it never interprets.
    for action in (
        Action.RUN_STATUS_INSPECT,
        Action.RUN_LIFECYCLE_TRANSITION,
        Action.RESULT_PACKAGE_VALIDATE,
        Action.ENGINEERING_RETRY,
        Action.FOLLOWUP_WORKER_SPAWN,
        Action.EVENT_RECORD_MAINTAIN,
        Action.MONITOR_RESUME,
    ):
        assert is_action_allowed(Role.MONITOR, action) is True


def test_permission_ac04_monitor_cannot_interpret_or_alter_scientific_inputs():
    for action in (
        Action.SCIENTIFIC_INTERPRETATION,
        Action.SCIENTIFIC_PARAMETER_CHANGE,
        Action.STATISTICAL_DESIGN_ALTER,
        Action.RECOVERY_ENTRY,
        Action.ANALYSIS_PROTOCOL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.GOAL_REVIEW,
    ):
        assert is_action_allowed(Role.MONITOR, action) is False


def test_permission_ac04_supervisor_guard_allows_interpretation():
    assessment = enforce_scientific_interpretation(Role.SUPERVISOR)
    assert assessment.allowed is True
    assert assessment.matched_rule_id == "R-PRM-SUP1"


def test_permission_ac04_interpretation_guard_type_error_boundaries():
    with pytest.raises(TypeError):
        enforce_scientific_interpretation("monitor")
    with pytest.raises(TypeError):
        enforce_scientific_interpretation(None)
    with pytest.raises(TypeError):
        enforce_scientific_interpretation(Role.MONITOR, target=7)


def test_permission_ac04_monitor_interpretation_denied_message_is_stable():
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_scientific_interpretation(Role.MONITOR, target="RUN-001")
    assert str(exc.value) == (
        "role 'execution_monitor' is not permitted to"
        " SCIENTIFIC_INTERPRETATION on RUN-001: rule R-PRM-D1 of the"
        " role-action matrix (ruleset 1.0) denies the action"
    )
    assert "\n" not in str(exc.value)


# ---------------------------------------------------------------------------
# Guard contract: error carrying, determinism, purity
# ---------------------------------------------------------------------------


def test_permission_denied_error_carries_the_full_assessment_record():
    goal = make_goal(frozen=True)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.COMPUTATION_WORKER, goal)
    assessment = exc.value.assessment
    assert isinstance(assessment, PermissionAssessment)
    assert isinstance(exc.value, RolePermissionError)
    assert isinstance(exc.value, ValueError)
    assert assessment.allowed is False
    assert assessment.input == PermissionInput(
        Role.COMPUTATION_WORKER, Action.FROZEN_GOAL_MUTATE
    )
    assert assessment.ruleset_version == ROLE_ACTION_RULESET_VERSION


def test_permission_runtime_guards_are_deterministic():
    goal = make_goal(frozen=True)
    first = enforce_goal_mutation(Role.SUPERVISOR, goal)
    second = enforce_goal_mutation(Role.SUPERVISOR, goal)
    assert first == second
    assert repr(first) == repr(second)
    requirement = make_requirement()
    assert enforce_requirement_closure(
        Role.SUPERVISOR, requirement
    ) == enforce_requirement_closure(Role.SUPERVISOR, requirement)
    assert enforce_plan_decision(
        Role.SUPERVISOR, DecisionType.RECOVERY_ENTRY
    ) == enforce_plan_decision(Role.SUPERVISOR, DecisionType.RECOVERY_ENTRY)


def test_permission_runtime_guards_are_pure_no_io_or_clock_imports():
    import scientific_reproduction.core.permissions as core_permissions
    import scientific_reproduction.workers.permissions as workers_permissions

    # The permission modules import no I/O-capable or clock/random
    # modules: state enters only through the injected arguments.
    for module in (core_permissions, workers_permissions):
        for forbidden in (
            "os",
            "sys",
            "time",
            "datetime",
            "random",
            "socket",
            "pathlib",
            "json",
            "hashlib",
        ):
            assert forbidden not in module.__dict__, (
                f"{module.__name__} must not import {forbidden}"
            )


def test_permission_runtime_bridge_from_core_worker_role_vocabulary():
    # The runtime guards consume the matrix Role; the worker-facing
    # bridge maps the core WorkerRole vocabulary 1:1.
    assert role_from_worker_role(WorkerRole.ANALYSIS_WORKER) is Role.ANALYSIS_WORKER
    assert role_from_worker_role(WorkerRole.DIAGNOSIS_WORKER) is Role.DIAGNOSIS_WORKER


def test_permission_runtime_guard_module_exports_only_guards():
    import scientific_reproduction.workers.permissions as workers_permissions

    exports = workers_permissions.__all__
    assert exports == [
        "enforce_goal_mutation",
        "enforce_plan_decision",
        "enforce_recovery_goal_creation",
        "enforce_requirement_closure",
        "enforce_scientific_interpretation",
    ]
    assert len(exports) == len(set(exports))  # no duplicates
    source = inspect.getsource(workers_permissions)
    assert "type: ignore" not in source
    assert "# noqa" not in source
