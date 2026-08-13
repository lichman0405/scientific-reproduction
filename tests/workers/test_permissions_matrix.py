"""Frozen role-action permission matrix (DEV-M6-G03, core).

Every test name contains "permission" so ``python -m pytest -q
tests/workers -k permission`` selects this suite (the pre-existing
workers suites carry no "permission" in their names and are the only
deselected items). Sections:

* ``vocabulary`` -- the matrix is grounded in the frozen
  ``core.models`` vocabulary: the four ``WorkerRole`` members map 1:1
  to the four worker ``Role`` members with verbatim values, and the
  eleven ``DecisionType`` members map 1:1 to the eleven
  Supervisor-decision ``Action`` members with verbatim values;
* ``frozen`` -- the matrix is frozen and complete: the module table is
  a tuple of frozen ``PermissionRule`` dataclasses, the per-role
  allowed-action sets are frozensets, the trailing default rule matches
  every (role, action) pair of the frozen product, and every pair
  evaluates to an assessment whose ``matched_rule_id`` is never None,
  with every rule recorded exactly once and the ruleset version
  stamped;
* ``determinism`` -- same inputs -> byte-identical assessments, over
  the whole product;
* ``errors`` -- the ``RolePermissionError`` hierarchy is ValueError-
  subclassed with stable one-line messages, and wrong types raise
  ``TypeError`` at every public boundary (never ``ValueError``).

The suite is pure: no registry, no file I/O, no wall clock -- only the
frozen matrix and the in-memory vocabulary.
"""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError

import pytest

from scientific_reproduction.core.models import DecisionType, WorkerRole
from scientific_reproduction.core.permissions import (
    ACTION_ORDER,
    ANALYSIS_WORKER_ACTIONS,
    COMPUTATION_WORKER_ACTIONS,
    DIAGNOSIS_WORKER_ACTIONS,
    EXPERIMENT_WORKER_ACTIONS,
    MONITOR_ACTIONS,
    PERMISSION_RULES,
    RESEARCH_ACTIONS,
    ROLE_ACTION_RULESET_VERSION,
    ROLE_ORDER,
    WORKER_COMMON_ACTIONS,
    Action,
    PermissionAssessment,
    PermissionDecision,
    PermissionInput,
    PermissionRule,
    PermissionRulesetError,
    Role,
    RolePermissionError,
    action_for_decision_type,
    check_action_allowed,
    is_action_allowed,
    role_from_worker_role,
    validate_permission_ruleset,
)

#: The frozen (role, action) product the matrix must decide totally.
ALL_ROLES: tuple[Role, ...] = ROLE_ORDER
ALL_ACTIONS: tuple[Action, ...] = ACTION_ORDER


def rule_by_id(rule_id: str) -> PermissionRule:
    """Return the matrix rule with the given id (test helper)."""
    for rule in PERMISSION_RULES:
        if rule.rule_id == rule_id:
            return rule
    raise AssertionError(f"no rule with id {rule_id!r} in PERMISSION_RULES")


# ---------------------------------------------------------------------------
# Vocabulary (grounded in core.models: WorkerRole / DecisionType)
# ---------------------------------------------------------------------------


def test_permission_matrix_worker_role_vocabulary_is_grounded_in_core_models():
    # The four core.models.WorkerRole members map 1:1 to the four worker
    # Role members with verbatim values (no vocabulary is invented).
    assert role_from_worker_role(WorkerRole.EXPERIMENT_WORKER) is Role.EXPERIMENT_WORKER
    assert role_from_worker_role(WorkerRole.COMPUTATION_WORKER) is Role.COMPUTATION_WORKER
    assert role_from_worker_role(WorkerRole.ANALYSIS_WORKER) is Role.ANALYSIS_WORKER
    assert role_from_worker_role(WorkerRole.DIAGNOSIS_WORKER) is Role.DIAGNOSIS_WORKER
    for worker_role in WorkerRole:
        assert role_from_worker_role(worker_role).value == worker_role.value
    assert len(ROLE_ORDER) == 7  # supervisor, research, monitor + 4 workers


def test_permission_matrix_decision_type_vocabulary_is_grounded_in_core_models():
    # Every core.models.DecisionType member maps 1:1 to an Action member
    # with the verbatim value, and the mapping is injective.
    mapped = {action_for_decision_type(decision_type) for decision_type in DecisionType}
    assert len(mapped) == len(DecisionType)
    for decision_type in DecisionType:
        action = action_for_decision_type(decision_type)
        assert action.value == decision_type.value
        assert action in ALL_ACTIONS


# ---------------------------------------------------------------------------
# Frozen and complete
# ---------------------------------------------------------------------------


def test_permission_matrix_is_frozen_table_and_rule_entries():
    # The matrix table is an immutable tuple of frozen PermissionRule
    # dataclasses: replacing a rule or mutating a rule raises.
    assert isinstance(PERMISSION_RULES, tuple)
    assert len(PERMISSION_RULES) >= 2  # role rules + the total default
    for rule in PERMISSION_RULES:
        assert dataclasses.is_dataclass(rule)
        assert isinstance(rule, PermissionRule)
        with pytest.raises(FrozenInstanceError):
            rule.rule_id = "mutated"
    # The per-role allowed-action sets are immutable frozensets.
    for actions in (
        RESEARCH_ACTIONS,
        MONITOR_ACTIONS,
        WORKER_COMMON_ACTIONS,
        EXPERIMENT_WORKER_ACTIONS,
        COMPUTATION_WORKER_ACTIONS,
        ANALYSIS_WORKER_ACTIONS,
        DIAGNOSIS_WORKER_ACTIONS,
    ):
        assert isinstance(actions, frozenset)


def test_permission_matrix_is_complete_over_role_action_product():
    # Every (role, action) pair of the frozen product gets a total
    # decision: allowed is a bool, matched_rule_id is never None, every
    # rule is recorded exactly once in order, and the ruleset version is
    # stamped.
    expected_ids = [rule.rule_id for rule in PERMISSION_RULES]
    for role in ALL_ROLES:
        for action in ALL_ACTIONS:
            assessment = check_action_allowed(role, action)
            assert isinstance(assessment, PermissionAssessment)
            assert isinstance(assessment.allowed, bool)
            assert assessment.matched_rule_id is not None
            assert assessment.matched_rule_id in expected_ids
            assert [d.rule_id for d in assessment.decisions] == expected_ids
            assert len(assessment.decisions) == len(PERMISSION_RULES)
            assert all(
                isinstance(decision, PermissionDecision)
                for decision in assessment.decisions
            )
            assert assessment.ruleset_version == ROLE_ACTION_RULESET_VERSION
            assert assessment.input == PermissionInput(role=role, action=action)


def test_permission_matrix_deny_default_rule_is_total_and_last():
    # R-PRM-D1 is the trailing total default: it matches every (role,
    # action) pair (so first-match evaluation is total) and it is the
    # only rule that denies -- every earlier rule grants.
    default = PERMISSION_RULES[-1]
    assert default.rule_id == "R-PRM-D1"
    assert default.allowed is False
    for role in ALL_ROLES:
        for action in ALL_ACTIONS:
            assert default.predicate(PermissionInput(role=role, action=action)) is True
    for role in ALL_ROLES:
        for action in ALL_ACTIONS:
            assessment = check_action_allowed(role, action)
            if assessment.matched_rule_id == "R-PRM-D1":
                assert assessment.allowed is False
            else:
                assert assessment.allowed is True


def test_permission_matrix_rule_ids_are_unique_and_ruleset_is_versioned():
    rule_ids = [rule.rule_id for rule in PERMISSION_RULES]
    assert len(rule_ids) == len(set(rule_ids))
    assert ROLE_ACTION_RULESET_VERSION == "1.0"


def test_permission_matrix_first_match_wins_supervisor_rule_heads_the_table():
    # Order is normative: a Supervisor is decided by R-PRM-SUP1 even for
    # actions other roles also hold (first match wins).
    for action in (Action.SOURCE_SEARCH, Action.RUN_STATUS_INSPECT, Action.ENGINEERING_RETRY):
        assessment = check_action_allowed(Role.SUPERVISOR, action)
        assert assessment.allowed is True
        assert assessment.matched_rule_id == "R-PRM-SUP1"


def test_permission_matrix_supervisor_holds_every_action():
    # 03-ROLE-AND-PERMISSION-SPEC.md SS2: the Supervisor alone may
    # create/modify/version Goals, freeze Plans, decide transitions,
    # close Goals/Requirements and assign final outcomes -- the matrix
    # grants every action of the vocabulary.
    for action in ALL_ACTIONS:
        assessment = check_action_allowed(Role.SUPERVISOR, action)
        assert assessment.allowed is True
        assert assessment.matched_rule_id == "R-PRM-SUP1"


def test_permission_matrix_every_worker_role_shares_the_common_worker_actions():
    for worker_actions in (
        EXPERIMENT_WORKER_ACTIONS,
        COMPUTATION_WORKER_ACTIONS,
        ANALYSIS_WORKER_ACTIONS,
        DIAGNOSIS_WORKER_ACTIONS,
    ):
        assert WORKER_COMMON_ACTIONS <= worker_actions


def test_permission_matrix_research_denied_governance_actions_grants_research_actions():
    # 03-ROLE-AND-PERMISSION-SPEC.md SS3: research may not change Goals,
    # change acceptance criteria, decide Recovery actions or directly
    # dispatch Workers; its "may" list is granted.
    for action in (
        Action.PLAN_FREEZE,
        Action.GOAL_REVISION,
        Action.GOAL_CREATE,
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.RECOVERY_GOAL_CREATE,
        Action.REQUIREMENT_CLOSE,
        Action.ACCEPTANCE_REVISION,
        Action.RECOVERY_ENTRY,
        Action.WORKER_DISPATCH,
    ):
        assert is_action_allowed(Role.RESEARCH, action) is False
    for action in tuple(RESEARCH_ACTIONS):
        assert is_action_allowed(Role.RESEARCH, action) is True
        assert check_action_allowed(Role.RESEARCH, action).matched_rule_id == "R-PRM-RES1"


def test_permission_matrix_monitor_denied_scientific_actions_grants_observation_actions():
    # 03-ROLE-AND-PERMISSION-SPEC.md SS4: the monitor may not change
    # scientific parameters, classify a Goal as scientifically PASS/FAIL,
    # enter Recovery autonomously or alter statistical design; its
    # observation/operation "may" list is granted.
    for action in (
        Action.SCIENTIFIC_INTERPRETATION,
        Action.SCIENTIFIC_PARAMETER_CHANGE,
        Action.STATISTICAL_DESIGN_ALTER,
        Action.RECOVERY_ENTRY,
        Action.GOAL_REVIEW,
        Action.ACCEPTANCE_REVISION,
        Action.ANALYSIS_PROTOCOL_REVISION,
    ):
        assert is_action_allowed(Role.MONITOR, action) is False
    for action in tuple(MONITOR_ACTIONS):
        assert is_action_allowed(Role.MONITOR, action) is True
        assert check_action_allowed(Role.MONITOR, action).matched_rule_id == "R-PRM-MON1"


def test_permission_matrix_workers_denied_goal_and_decision_actions():
    # 03-ROLE-AND-PERMISSION-SPEC.md SS5-SS8: no worker may create or
    # change Goals, create Recovery Goals, close Requirements, make plan
    # decisions or declare scientific verdicts.
    for role in (
        Role.EXPERIMENT_WORKER,
        Role.COMPUTATION_WORKER,
        Role.ANALYSIS_WORKER,
        Role.DIAGNOSIS_WORKER,
    ):
        for action in (
            Action.GOAL_CREATE,
            Action.GOAL_MUTATE,
            Action.FROZEN_GOAL_MUTATE,
            Action.RECOVERY_GOAL_CREATE,
            Action.REQUIREMENT_CLOSE,
            Action.REQUIREMENT_CLOSURE,
            Action.PLAN_FREEZE,
            Action.GOAL_REVISION,
            Action.SCIENTIFIC_INTERPRETATION,
            Action.RECOVERY_ENTRY,
            Action.METHOD_REDESIGN_ENTRY,
        ):
            assessment = check_action_allowed(role, action)
            assert assessment.allowed is False
            assert assessment.matched_rule_id == "R-PRM-D1"


# ---------------------------------------------------------------------------
# Ruleset validation
# ---------------------------------------------------------------------------


def test_permission_matrix_ruleset_validation_passes_on_the_frozen_table():
    rule_ids = validate_permission_ruleset()
    assert rule_ids == tuple(rule.rule_id for rule in PERMISSION_RULES)
    assert rule_ids[-1] == "R-PRM-D1"


def test_permission_matrix_ruleset_validation_detects_duplicate_rule_ids():
    table = [
        rule_by_id("R-PRM-SUP1"),
        rule_by_id("R-PRM-SUP1"),
        rule_by_id("R-PRM-D1"),
    ]
    with pytest.raises(PermissionRulesetError) as exc:
        validate_permission_ruleset(table)
    message = str(exc.value)
    assert "duplicate rule id" in message
    assert "R-PRM-SUP1" in message


def test_permission_matrix_ruleset_validation_detects_empty_table():
    with pytest.raises(PermissionRulesetError) as exc:
        validate_permission_ruleset([])
    assert "must not be empty" in str(exc.value)


def test_permission_matrix_ruleset_validation_detects_non_total_default():
    # A trailing rule that does not match every (role, action) pair is
    # not a total default: first-match evaluation would not be total.
    table = [
        rule_by_id("R-PRM-SUP1"),
        PermissionRule(
            rule_id="R-PRM-BAD1",
            description=(
                "a trailing rule that only matches a supervisor (not a"
                " total default)"
            ),
            allowed=True,
            predicate=lambda i: i.role is Role.SUPERVISOR,
        ),
    ]
    with pytest.raises(PermissionRulesetError) as exc:
        validate_permission_ruleset(table)
    message = str(exc.value)
    assert "total default" in message
    assert "R-PRM-BAD1" in message


def test_permission_matrix_ruleset_validation_rejects_wrong_entry_types():
    with pytest.raises(TypeError):
        validate_permission_ruleset(["not a rule"])
    with pytest.raises(TypeError):
        validate_permission_ruleset("not a sequence")
    with pytest.raises(TypeError):
        validate_permission_ruleset(42)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_permission_deterministic_assessments_are_byte_identical():
    first = check_action_allowed(Role.COMPUTATION_WORKER, Action.FROZEN_GOAL_MUTATE)
    second = check_action_allowed(Role.COMPUTATION_WORKER, Action.FROZEN_GOAL_MUTATE)
    assert first == second
    assert repr(first) == repr(second)


def test_permission_deterministic_whole_matrix_reproduces_identically():
    def snapshot() -> tuple[object, ...]:
        return tuple(
            (role, action, check_action_allowed(role, action))
            for role in ALL_ROLES
            for action in ALL_ACTIONS
        )

    assert snapshot() == snapshot()
    assert repr(snapshot()) == repr(snapshot())


def test_permission_deterministic_allowed_sets_are_stable():
    # The per-role allowed-action sets are frozen constants: same role
    # vocabulary -> same grants, in every call.
    assert tuple(sorted(RESEARCH_ACTIONS)) == tuple(sorted(RESEARCH_ACTIONS))
    assert tuple(sorted(MONITOR_ACTIONS)) == tuple(sorted(MONITOR_ACTIONS))
    assert WORKER_COMMON_ACTIONS == WORKER_COMMON_ACTIONS


# ---------------------------------------------------------------------------
# Errors and boundaries
# ---------------------------------------------------------------------------


def test_permission_error_hierarchy_are_valueerror_subclasses():
    assert issubclass(RolePermissionError, ValueError)
    assert issubclass(PermissionRulesetError, RolePermissionError)
    assert issubclass(PermissionRulesetError, ValueError)


@pytest.mark.parametrize(
    "bad_role",
    [None, "supervisor", 7, WorkerRole.EXPERIMENT_WORKER],
)
def test_permission_check_action_allowed_rejects_non_role_inputs(bad_role):
    with pytest.raises(TypeError):
        check_action_allowed(bad_role, Action.PLAN_FREEZE)


@pytest.mark.parametrize(
    "bad_action",
    [None, "PLAN_FREEZE", 7, DecisionType.PLAN_FREEZE],
)
def test_permission_check_action_allowed_rejects_non_action_inputs(bad_action):
    with pytest.raises(TypeError):
        check_action_allowed(Role.SUPERVISOR, bad_action)


def test_permission_is_action_allowed_matches_check_and_rejects_bad_types():
    assert is_action_allowed(Role.SUPERVISOR, Action.PLAN_FREEZE) is True
    assert is_action_allowed(Role.RESEARCH, Action.PLAN_FREEZE) is False
    with pytest.raises(TypeError):
        is_action_allowed("supervisor", Action.PLAN_FREEZE)
    with pytest.raises(TypeError):
        is_action_allowed(Role.SUPERVISOR, "PLAN_FREEZE")


def test_permission_action_for_decision_type_rejects_non_decision_type():
    with pytest.raises(TypeError):
        action_for_decision_type("PLAN_FREEZE")
    with pytest.raises(TypeError):
        action_for_decision_type(None)


def test_permission_role_from_worker_role_rejects_non_worker_role():
    with pytest.raises(TypeError):
        role_from_worker_role("experiment_worker")
    with pytest.raises(TypeError):
        role_from_worker_role(Role.EXPERIMENT_WORKER)


def test_permission_input_constructor_rejects_wrong_types():
    with pytest.raises(TypeError):
        PermissionInput(role="supervisor", action=Action.PLAN_FREEZE)
    with pytest.raises(TypeError):
        PermissionInput(role=Role.SUPERVISOR, action="PLAN_FREEZE")


def test_permission_rule_constructor_validates_strictly():
    with pytest.raises(TypeError):
        PermissionRule(rule_id=7, description="x", allowed=True, predicate=lambda i: True)
    with pytest.raises(PermissionRulesetError):
        PermissionRule(rule_id="", description="x", allowed=True, predicate=lambda i: True)
    with pytest.raises(PermissionRulesetError):
        PermissionRule(rule_id="R-X", description="  ", allowed=True, predicate=lambda i: True)
    with pytest.raises(TypeError):
        PermissionRule(rule_id="R-X", description="x", allowed=1, predicate=lambda i: True)
    with pytest.raises(TypeError):
        PermissionRule(rule_id="R-X", description="x", allowed=True, predicate="not callable")
    with pytest.raises(TypeError):
        PermissionRule(rule_id="R-X", description="x", allowed=True, predicate=None)


def test_permission_ruleset_error_message_is_stable_and_one_line():
    with pytest.raises(PermissionRulesetError) as exc:
        validate_permission_ruleset([])
    assert str(exc.value) == (
        "the role-action rule table must not be empty: at least the total"
        " default rule is required"
    )
    assert "\n" not in str(exc.value)
