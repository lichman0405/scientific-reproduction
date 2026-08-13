"""Role contract authority boundaries match the locked role spec (DEV-M10-G01
AC-01).

The frozen authority vocabulary is the role-action permission matrix
(``core.permissions``, DEV-M6-G03), itself grounded in
``03-ROLE-AND-PERMISSION-SPEC.md`` SS2-SS8. Every test here asserts that a
role contract's boundary -- allowed actions, forbidden actions, and the
typed decision/verdict/retry authorities -- matches the locked spec
exactly, on both sides:

* the "may" side: the contract's ``allowed_actions`` are granted by the
  matrix (and the spec's "may" lists appear in them);
* the "may not" side: the spec's per-role prohibitions appear in
  ``forbidden_actions`` / ``forbidden_practices`` and are denied by the
  matrix (``R-PRM-D1`` least privilege), so no role can cross its
  boundary;
* typed authorities: scientific decisions (``DecisionAuthority``),
  scientific verdicts (``VerdictAuthority``) and the resubmission/retry
  boundary (``RetryAuthority``) match the spec section of each role.

The suite is pure: no file I/O, no wall clock, no randomness -- only the
frozen contract records and the frozen matrix.
"""

from __future__ import annotations

from scientific_reproduction.adapters.platform.contracts import (
    CONTRACT_ROLE_IDS,
    DECISION_ACTIONS,
    ROLE_CONTRACTS,
    VERDICT_ACTIONS,
    DecisionAuthority,
    RetryAuthority,
    VerdictAuthority,
    contract_to_matrix_roles,
    get_role_contract,
)
from scientific_reproduction.core.permissions import (
    ACTION_ORDER,
    MONITOR_ACTIONS,
    RESEARCH_ACTIONS,
    WORKER_COMMON_ACTIONS,
    Action,
    Role,
    is_action_allowed,
)

#: The exclusive Supervisor surfaces: plan/decision, verdict and worker
#: dispatch actions no non-Supervisor role may hold (SS2 vs SS3-SS8).
SUPERVISOR_EXCLUSIVE_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.PLAN_FREEZE,
        Action.GOAL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.ANALYSIS_PROTOCOL_REVISION,
        Action.RECOVERY_ENTRY,
        Action.METHOD_REDESIGN_ENTRY,
        Action.GOAL_REVIEW,
        Action.REQUIREMENT_CLOSURE,
        Action.HUMAN_GATE_OPEN,
        Action.PROJECT_OUTCOME,
        Action.SCIENTIFIC_INTERPRETATION,
        Action.GOAL_CREATE,
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.RECOVERY_GOAL_CREATE,
        Action.REQUIREMENT_CLOSE,
        Action.WORKER_DISPATCH,
        Action.RESEARCH_REQUEST,
    }
)

#: The frozen matrix role-action grants, per contract role (the "may"
#: side the contracts must not exceed).
MATRIX_ALLOWED_BY_CONTRACT: dict[str, frozenset[Action]] = {
    "supervisor": frozenset(ACTION_ORDER),
    "research": RESEARCH_ACTIONS,
    "execution_monitor": MONITOR_ACTIONS,
    "worker": (
        WORKER_COMMON_ACTIONS
        | frozenset(
            {
                Action.EXECUTION_PACKAGE_PREPARE,
                Action.RUN_PREPARE,
                Action.FACT_REPORT,
                Action.ANALYSIS_EXECUTE,
                Action.DIAGNOSIS_REPORT,
                Action.ENGINEERING_RETRY,
            }
        )
    ),
}


def test_contracts_ac01_supervisor_boundary_matches_locked_spec_ss2():
    # SS2: the Supervisor alone may create/freeze Plans, Goals, acceptance
    # criteria and protocols, decide transitions, dispatch workers and
    # assign final outcomes -- the contract holds every matrix action, and
    # the SS2 "must not" practices (SS1 governance principle) are encoded.
    contract = get_role_contract("supervisor")
    assert contract.spec_section == "03-ROLE-AND-PERMISSION-SPEC.md SS2"
    assert contract.allowed_actions == frozenset(ACTION_ORDER)
    assert contract.decision_authority is DecisionAuthority.SUPERVISOR_ONLY
    assert contract.verdict_authority is VerdictAuthority.SUPERVISOR_ONLY
    assert contract.retry_authority is RetryAuthority.SUPERVISOR_ONLY
    # Every plan-mutation/verdict surface is on the Supervisor's allowed
    # side -- the boundary is positive for the Supervisor.
    for action in SUPERVISOR_EXCLUSIVE_ACTIONS:
        assert action in contract.allowed_actions
        assert is_action_allowed(Role.SUPERVISOR, action) is True
    # The SS2/SS1 prohibitions are practices, not action grants.
    for practice in (
        "silent_change_of_frozen_criteria",
        "significance_equivocated_as_equivalence",
        "failed_attempt_erasure",
        "premature_non_reproduction_claim",
        "scientific_authority_delegation",
    ):
        assert practice in contract.forbidden_practices


def test_contracts_ac01_research_boundary_matches_locked_spec_ss3():
    # SS3: research may search/acquire/index sources and execute evidence
    # checklists, but may not change Goals, change acceptance criteria,
    # decide Recovery actions or directly dispatch Workers.
    contract = get_role_contract("research")
    assert contract.spec_section == "03-ROLE-AND-PERMISSION-SPEC.md SS3"
    assert contract.allowed_actions == RESEARCH_ACTIONS
    for action in (
        Action.GOAL_CREATE,
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.GOAL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.RECOVERY_ENTRY,
        Action.RECOVERY_GOAL_CREATE,
        Action.WORKER_DISPATCH,
    ):
        assert action in contract.forbidden_actions
        assert is_action_allowed(Role.RESEARCH, action) is False
    # Research holds no scientific decision, verdict or retry authority
    # and may not contact authors autonomously.
    assert contract.decision_authority is DecisionAuthority.NONE
    assert contract.verdict_authority is VerdictAuthority.REPORT_FACTS
    assert contract.retry_authority is RetryAuthority.NONE
    assert "author_contact_without_human_gate" in contract.forbidden_practices


def test_contracts_ac01_monitor_boundary_matches_locked_spec_ss4():
    # SS4: the Monitor may inspect Runs, transition operational lifecycle,
    # validate Result Packages, execute preauthorized engineering retries
    # and maintain event records -- but may not change scientific
    # parameters, classify a Goal as PASS/FAIL, enter Recovery
    # autonomously or alter statistical design.
    contract = get_role_contract("execution_monitor")
    assert contract.spec_section == "03-ROLE-AND-PERMISSION-SPEC.md SS4"
    assert contract.allowed_actions == MONITOR_ACTIONS
    for action in (
        Action.SCIENTIFIC_PARAMETER_CHANGE,
        Action.SCIENTIFIC_INTERPRETATION,
        Action.GOAL_REVIEW,
        Action.PROJECT_OUTCOME,
        Action.RECOVERY_ENTRY,
        Action.RECOVERY_GOAL_CREATE,
        Action.STATISTICAL_DESIGN_ALTER,
    ):
        assert action in contract.forbidden_actions
        assert is_action_allowed(Role.MONITOR, action) is False
    # The Monitor's "may" list is exactly the matrix grant, and its retry
    # boundary is the preauthorized engineering retry (never beyond).
    for action in MONITOR_ACTIONS:
        assert action in contract.allowed_actions
        assert is_action_allowed(Role.MONITOR, action) is True
    assert contract.retry_authority is RetryAuthority.PREAUTHORIZED_ENGINEERING
    assert (
        "retry_beyond_preauthorized_engineering"
        in contract.forbidden_practices
    )
    assert "autonomous_recovery_entry" in contract.forbidden_practices


def test_contracts_ac01_worker_boundary_matches_locked_spec_ss5_ss8():
    # SS5-SS8: workers may read one context, execute the frozen work,
    # record metadata, register artifacts and report deviations -- but may
    # not change protocols/goals, change track, decide retries beyond the
    # whitelist or declare PASS/FAIL.
    contract = get_role_contract("worker")
    assert contract.spec_section == "03-ROLE-AND-PERMISSION-SPEC.md SS5-SS8"
    assert contract.allowed_actions == MATRIX_ALLOWED_BY_CONTRACT["worker"]
    for action in (
        Action.ANALYSIS_PROTOCOL_REVISION,
        Action.SCIENTIFIC_PARAMETER_CHANGE,
        Action.SCIENTIFIC_INTERPRETATION,
        Action.GOAL_REVIEW,
        Action.PROJECT_OUTCOME,
        Action.GOAL_CREATE,
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.RECOVERY_GOAL_CREATE,
        Action.RECOVERY_ENTRY,
        Action.METHOD_REDESIGN_ENTRY,
        Action.REQUIREMENT_CLOSURE,
        Action.REQUIREMENT_CLOSE,
        Action.PLAN_FREEZE,
        Action.GOAL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.STATISTICAL_DESIGN_ALTER,
    ):
        assert action in contract.forbidden_actions
        # Every worker role of the matrix denies the action.
        for role in contract_to_matrix_roles("worker"):
            assert is_action_allowed(role, action) is False
    assert contract.decision_authority is DecisionAuthority.NONE
    assert contract.verdict_authority is VerdictAuthority.REPORT_FACTS
    assert contract.retry_authority is RetryAuthority.WHITELISTED_ENGINEERING
    assert (
        "retry_beyond_whitelisted_engineering"
        in contract.forbidden_practices
    )


def test_contracts_ac01_no_contract_crosses_another_roles_exclusive_authority():
    # SS2 vs SS3-SS8: the plan/verdict/dispatch surfaces are Supervisor-
    # exclusive; no non-Supervisor contract may hold any of them (no role
    # may cross its boundary).
    for role_id in ("research", "execution_monitor", "worker"):
        contract = get_role_contract(role_id)
        for action in SUPERVISOR_EXCLUSIVE_ACTIONS:
            assert action not in contract.allowed_actions, (
                f"role {role_id!r} may hold {action.value}, which is"
                " Supervisor-exclusive in the locked spec"
            )


def test_contracts_ac01_every_allowed_action_is_granted_by_the_matrix():
    # AC-01 grounding: a contract's "may" list never exceeds the frozen
    # role-action matrix grant for its role(s) -- the contract describes
    # the locked matrix, it does not invent authority. (The Worker
    # contract is the union of the four worker roles, so each allowed
    # action is granted to at least one of its mapped roles.)
    for contract in ROLE_CONTRACTS:
        granted = {
            action
            for action in ACTION_ORDER
            if any(
                is_action_allowed(role, action)
                for role in contract_to_matrix_roles(contract.role_id)
            )
        }
        assert contract.allowed_actions <= granted
        assert contract.allowed_actions <= MATRIX_ALLOWED_BY_CONTRACT[contract.role_id]


def test_contracts_ac01_every_forbidden_action_is_denied_by_the_matrix():
    # AC-01 grounding: a contract's "may not" list contains only actions
    # the frozen matrix denies for its role(s) -- least privilege holds on
    # both sides of the boundary.
    for contract in ROLE_CONTRACTS:
        for role in contract_to_matrix_roles(contract.role_id):
            for action in contract.forbidden_actions:
                assert is_action_allowed(role, action) is False, (
                    f"role contract {contract.role_id!r} forbids {action.value},"
                    f" which the matrix grants to {role.value}"
                )


def test_contracts_ac01_decision_verdict_retry_authorities_match_spec():
    # The typed authority fields mirror the spec sections exactly:
    # supervisor decides and assigns outcomes; research decides nothing;
    # the monitor executes preauthorized retries; workers use only
    # whitelisted engineering retries.
    authorities = {contract.role_id: contract for contract in ROLE_CONTRACTS}
    assert authorities["supervisor"].decision_authority is DecisionAuthority.SUPERVISOR_ONLY
    assert authorities["supervisor"].verdict_authority is VerdictAuthority.SUPERVISOR_ONLY
    assert authorities["supervisor"].retry_authority is RetryAuthority.SUPERVISOR_ONLY
    assert authorities["research"].decision_authority is DecisionAuthority.NONE
    assert authorities["research"].verdict_authority is VerdictAuthority.REPORT_FACTS
    assert authorities["research"].retry_authority is RetryAuthority.NONE
    assert (
        authorities["execution_monitor"].decision_authority
        is DecisionAuthority.NONE
    )
    assert (
        authorities["execution_monitor"].verdict_authority
        is VerdictAuthority.REPORT_FACTS
    )
    assert (
        authorities["execution_monitor"].retry_authority
        is RetryAuthority.PREAUTHORIZED_ENGINEERING
    )
    assert authorities["worker"].decision_authority is DecisionAuthority.NONE
    assert authorities["worker"].verdict_authority is VerdictAuthority.REPORT_FACTS
    assert (
        authorities["worker"].retry_authority
        is RetryAuthority.WHITELISTED_ENGINEERING
    )


def test_contracts_ac01_non_supervisor_contracts_forbid_decision_and_verdict_actions():
    # A role without Supervisor decision/verdict authority must explicitly
    # forbid the decision and verdict actions -- the boundary is encoded,
    # not implied.
    for role_id in ("research", "execution_monitor", "worker"):
        contract = get_role_contract(role_id)
        assert DECISION_ACTIONS <= contract.forbidden_actions
        assert VERDICT_ACTIONS <= contract.forbidden_actions
    assert CONTRACT_ROLE_IDS == (
        "supervisor",
        "research",
        "execution_monitor",
        "worker",
    )
