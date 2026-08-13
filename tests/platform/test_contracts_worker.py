"""Worker role contract forbids plan mutation and self-acceptance
(DEV-M10-G01 AC-03).

The Worker contract is the descriptor of the four worker roles
(03-ROLE-AND-PERMISSION-SPEC.md SS5-SS8). AC-03 requires it to forbid,
explicitly and in its own vocabulary:

* plan mutation -- mutating the frozen Plan, Goals, acceptance criteria
  or analysis protocol: the matrix actions (``PLAN_FREEZE``,
  ``GOAL_REVISION``, ``ACCEPTANCE_REVISION``,
  ``ANALYSIS_PROTOCOL_REVISION``, the Goal-family mutations) and the
  practice token ``plan_mutation``;
* self-acceptance -- a Worker may not accept, review or merge its own
  output: the practice tokens ``self_acceptance`` / ``self_review`` /
  ``self_merge`` and the prompt prohibition "never ... accept your own
  output".

The tests also cross-check the contract against the frozen runtime
matrix (DEV-M6-G03/G05): the same mutations are denied to every worker
role at runtime, so the contract agrees with the enforced boundary.

The suite is pure: no file I/O, no wall clock, no randomness.
"""

from __future__ import annotations

from scientific_reproduction.adapters.platform.contracts import (
    ROLE_CONTRACTS,
    DecisionAuthority,
    VerdictAuthority,
    get_role_contract,
)
from scientific_reproduction.core.permissions import (
    Action,
    Role,
    is_action_allowed,
)

#: The plan-mutation actions of the frozen matrix vocabulary: freezing or
#: revising Plans, Goals, acceptance criteria and analysis protocols, and
#: the Goal-family mutation surfaces (SS2 vs SS5-SS8).
PLAN_MUTATION_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.PLAN_FREEZE,
        Action.GOAL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.ANALYSIS_PROTOCOL_REVISION,
        Action.GOAL_CREATE,
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.RECOVERY_GOAL_CREATE,
    }
)

#: The four worker roles of the frozen matrix (SS5-SS8).
WORKER_MATRIX_ROLES: tuple[Role, ...] = (
    Role.EXPERIMENT_WORKER,
    Role.COMPUTATION_WORKER,
    Role.ANALYSIS_WORKER,
    Role.DIAGNOSIS_WORKER,
)


def test_contracts_ac03_worker_contract_forbids_plan_mutation_actions():
    # AC-03: every plan-mutation surface of the matrix vocabulary is in
    # the Worker contract's forbidden actions and out of its allowed
    # actions.
    worker = get_role_contract("worker")
    assert PLAN_MUTATION_ACTIONS <= worker.forbidden_actions
    assert PLAN_MUTATION_ACTIONS.isdisjoint(worker.allowed_actions)


def test_contracts_ac03_worker_contract_forbids_plan_mutation_practice():
    # AC-03: the plan-mutation prohibition is part of the contract's
    # forbidden-practices vocabulary in its own words, not only as matrix
    # actions.
    worker = get_role_contract("worker")
    assert "plan_mutation" in worker.forbidden_practices
    assert any(
        "never mutate the frozen Plan" in prohibition
        for prohibition in worker.prompt_prohibitions
    )


def test_contracts_ac03_worker_contract_forbids_self_acceptance_practices():
    # AC-03: a Worker may not accept, review or merge its own output --
    # the self-acceptance vocabulary is explicit in the contract.
    worker = get_role_contract("worker")
    for token in ("self_acceptance", "self_review", "self_merge"):
        assert token in worker.forbidden_practices
    assert any(
        "never declare PASS/FAIL or accept your own output" in prohibition
        for prohibition in worker.prompt_prohibitions
    )


def test_contracts_ac03_worker_contract_has_no_acceptance_or_verdict_authority():
    # AC-03: acceptance is a Supervisor decision; the Worker contract
    # carries no acceptance/verdict authority and no such action in its
    # allowed list (05-GOAL-RUN-SCHEMA.md SS7: PASS/FAIL is a review
    # decision stored separately).
    worker = get_role_contract("worker")
    assert worker.verdict_authority is VerdictAuthority.REPORT_FACTS
    assert worker.decision_authority is DecisionAuthority.NONE
    for action in (
        Action.SCIENTIFIC_INTERPRETATION,
        Action.GOAL_REVIEW,
        Action.REQUIREMENT_CLOSURE,
        Action.REQUIREMENT_CLOSE,
        Action.PROJECT_OUTCOME,
    ):
        assert action in worker.forbidden_actions
        assert action not in worker.allowed_actions


def test_contracts_ac03_worker_plan_mutation_is_runtime_denied_for_every_worker_role():
    # AC-03 cross-check with the frozen runtime matrix (DEV-M6-G03/G05):
    # the same mutations the contract forbids are denied to every worker
    # role at runtime -- the contract describes the enforced boundary.
    for role in WORKER_MATRIX_ROLES:
        for action in PLAN_MUTATION_ACTIONS:
            assert is_action_allowed(role, action) is False, (
                f"runtime matrix grants {action.value} to {role.value},"
                " contradicting the Worker contract"
            )


def test_contracts_ac03_only_worker_contract_carries_the_self_acceptance_prohibition():
    # AC-03 boundary hygiene: self-acceptance is the Worker contract's
    # prohibition; no other contract may silently grant it either (every
    # non-Supervisor contract forbids acceptance/verdict actions; the
    # Supervisor is the acceptor).
    worker = get_role_contract("worker")
    assert "self_acceptance" in worker.forbidden_practices
    for contract in ROLE_CONTRACTS:
        for action in (
            Action.SCIENTIFIC_INTERPRETATION,
            Action.PROJECT_OUTCOME,
        ):
            if contract.role_id == "supervisor":
                assert action in contract.allowed_actions
            else:
                assert action in contract.forbidden_actions
                assert action not in contract.allowed_actions
