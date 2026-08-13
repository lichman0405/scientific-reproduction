"""Automatic retry policy evaluator tests (DEV-M6-G04, workers).

Every test name contains "retry" so ``python -m pytest -q
tests/workers -k retry`` selects this suite (together with the
pre-existing ``test_context_ac01_goal_without_policy_exposes_no_retry_actions``
-- that test is untouched). The ``ac01``/``ac02``/``ac03`` sections map
one-to-one to the acceptance criteria of DEV-M6-G04:

* ``ac01`` -- transient SSH/network/scheduler node failures are
  authorized for automatic worker action when the frozen contract
  (the policy whitelist ``allowed_engineering_failures``) allows; a
  failure not whitelisted is not authorized, even if it looks
  transient -- the whitelist is the contract;
* ``ac02`` -- identical checkpoint continuation (rerun from the same
  checkpoint with zero scientific change) is authorized without
  scientific change, subject to the policy's ``max_identical_retries``
  gate (``None`` = unlimited, an int = hard ceiling), and only when
  checkpoint continuation is not an ``invalidate_run_on`` situation
  (those failure kinds invalidate the run instead of retrying);
* ``ac03`` -- scientific parameter modifications are never authorized
  for automatic worker action: the evaluator recognizes them (the
  policy's ``supervisor_required_changes`` -- the authoritative list --
  plus the frozen detection vocabulary) and routes them to the
  Supervisor as a decision record -- never an error and never an
  execution, because the module exposes pure evaluation only (proven
  by module-dict and source inspection).

The deterministic path mirrors ``context_helpers``: every fixture is a
fixed in-memory ``AutomaticRetryPolicy`` / ``RetryEvaluationInput``
record -- no registry, no file I/O, no wall clock, no network.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, is_dataclass, replace
from typing import Sequence

import pytest

from scientific_reproduction.core.models import AutomaticRetryPolicy
from scientific_reproduction.core.permissions import (
    Action,
    Role,
    is_action_allowed,
)
from scientific_reproduction.workers import retry as retry_module
from scientific_reproduction.workers.retry import (
    CHECKPOINT_CONTINUATION_KIND,
    REASON_ALLOWED_ENGINEERING_FAILURE,
    REASON_CEILING_NOT_REACHED,
    REASON_CEILING_REACHED,
    REASON_CEILING_UNLIMITED,
    REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
    REASON_IDS,
    REASON_INVALIDATE_RUN,
    REASON_NO_POLICY_ENTRY,
    REASON_SCIENTIFIC_CHANGE_VOCABULARY,
    REASON_SUPERVISOR_REQUIRED_CHANGE,
    RETRY_AUTHORIZATION_RULESET_VERSION,
    RETRY_DECISION_RULES,
    SCIENTIFIC_CHANGE_FAILURES,
    MalformedRetryPolicyError,
    RetryAssessment,
    RetryAuthorization,
    RetryEvaluationError,
    RetryEvaluationInput,
    RetryPolicyError,
    RetryRouting,
    RetryRule,
    RetryRuleDecision,
    RetryRulesetError,
    evaluate_automatic_retry,
    validate_retry_ruleset,
)

# ---------------------------------------------------------------------------
# Fixtures (deterministic, in-memory only)
# ---------------------------------------------------------------------------


def make_policy(
    policy_id: str = "RETRY-ENGINEERING-DEFAULT",
    *,
    allowed_engineering_failures: Sequence[str] = (
        "ssh_connection_lost",
        "network_timeout",
        "scheduler_node_failure",
    ),
    supervisor_required_changes: Sequence[str] = ("smearing_parameter_change",),
    max_identical_retries: int | None = None,
    invalidate_run_on: Sequence[str] = ("sample_loss",),
) -> AutomaticRetryPolicy:
    """Build a frozen AutomaticRetryPolicy in memory (no file access)."""
    return AutomaticRetryPolicy(
        policy_id=policy_id,
        allowed_engineering_failures=list(allowed_engineering_failures),
        supervisor_required_changes=list(supervisor_required_changes),
        max_identical_retries=max_identical_retries,
        invalidate_run_on=list(invalidate_run_on),
    )


def make_input(
    policy: AutomaticRetryPolicy | None = None,
    *,
    failure_kind: str = "ssh_connection_lost",
    identical_retry_count: int = 0,
    checkpoint_continuation: bool = False,
) -> RetryEvaluationInput:
    """Build a deterministic evaluation input for the default policy."""
    return RetryEvaluationInput(
        policy=policy if policy is not None else make_policy(),
        failure_kind=failure_kind,
        identical_retry_count=identical_retry_count,
        checkpoint_continuation=checkpoint_continuation,
    )


def make_checkpoint(
    policy: AutomaticRetryPolicy | None = None,
    *,
    identical_retry_count: int = 0,
) -> RetryEvaluationInput:
    """Build an identical checkpoint continuation input (AC-02)."""
    return make_input(
        policy,
        failure_kind=CHECKPOINT_CONTINUATION_KIND,
        identical_retry_count=identical_retry_count,
        checkpoint_continuation=True,
    )


# ---------------------------------------------------------------------------
# AC-01 -- transient engineering failures authorized when the contract allows
# ---------------------------------------------------------------------------


def test_retry_ac01_whitelisted_ssh_failure_authorized_for_automatic_action():
    # AC-01: a transient SSH failure whitelisted by the frozen policy is
    # pre-authorized for automatic worker action (11-COMPUTATION-SUBSYSTEM.md
    # SS5 "SSH transient failure").
    assessment = evaluate_automatic_retry(
        make_input(failure_kind="ssh_connection_lost")
    )
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert assessment.matched_rule_id == "R-RET-A1"
    assert assessment.matched_policy_entries == ("ssh_connection_lost",)
    assert assessment.reasoning_ids == (REASON_ALLOWED_ENGINEERING_FAILURE,)
    assert assessment.ceiling_reached is False


def test_retry_ac01_whitelisted_network_timeout_authorized():
    # AC-01: a whitelisted network timeout is authorized the same way.
    assessment = evaluate_automatic_retry(
        make_input(failure_kind="network_timeout")
    )
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.matched_rule_id == "R-RET-A1"
    assert assessment.matched_policy_entries == ("network_timeout",)


def test_retry_ac01_whitelisted_scheduler_node_failure_authorized_when_contract_allows():
    # The exact AC-01 wording: a scheduler node failure is authorized
    # WHEN the contract -- the frozen policy whitelist -- allows it.
    policy = make_policy(allowed_engineering_failures=("scheduler_node_failure",))
    assessment = evaluate_automatic_retry(
        make_input(policy, failure_kind="scheduler_node_failure")
    )
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert assessment.matched_rule_id == "R-RET-A1"


def test_retry_ac01_transient_failure_not_whitelisted_is_not_authorized():
    # Even a textbook-transient SSH failure is not authorized when the
    # contract does not whitelist it: the whitelist is the contract, not
    # a classification of how the failure "looks" (AC-01).
    policy = make_policy(allowed_engineering_failures=("network_timeout",))
    assessment = evaluate_automatic_retry(
        make_input(policy, failure_kind="ssh_connection_lost")
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.matched_rule_id == "R-RET-D1"
    assert assessment.matched_policy_entries == ()
    assert assessment.reasoning_ids == (REASON_NO_POLICY_ENTRY,)
    assert assessment.routing is RetryRouting.AUTOMATIC


def test_retry_ac01_whitelist_is_the_contract_not_the_failure_shape():
    # The same failure kind is authorized under one frozen policy and
    # rejected under another: the authorization is a pure function of the
    # contract, never of any property of the failure itself.
    allowed = evaluate_automatic_retry(make_input(failure_kind="network_timeout"))
    denied = evaluate_automatic_retry(
        make_input(
            make_policy(allowed_engineering_failures=("scheduler_node_failure",)),
            failure_kind="network_timeout",
        )
    )
    assert allowed.verdict is RetryAuthorization.AUTHORIZED
    assert denied.verdict is RetryAuthorization.REJECTED
    assert denied.matched_rule_id == "R-RET-D1"


def test_retry_ac01_authorized_assessment_records_full_decision_record():
    # Every assessment records the full decision record: every rule
    # evaluation, the matched rule id, the matched whitelist entry, the
    # reasoning id and the ruleset version.
    assessment = evaluate_automatic_retry(
        make_input(failure_kind="ssh_connection_lost")
    )
    assert assessment.ruleset_version == RETRY_AUTHORIZATION_RULESET_VERSION
    assert len(assessment.decisions) == len(RETRY_DECISION_RULES)
    matched = next(
        decision for decision in assessment.decisions if decision.rule_id == "R-RET-A1"
    )
    assert matched.matched is True
    assert matched.verdict is RetryAuthorization.AUTHORIZED
    assert matched.routing is RetryRouting.AUTOMATIC
    # The supervisor rule did not match this engineering failure.
    supervisor = next(
        decision
        for decision in assessment.decisions
        if decision.rule_id == "R-RET-S1"
    )
    assert supervisor.matched is False
    # The trailing total default always matches (a decision always
    # exists) but is not the deciding rule here.
    default = next(
        decision for decision in assessment.decisions if decision.rule_id == "R-RET-D1"
    )
    assert default.matched is True
    assert assessment.matched_rule_id == "R-RET-A1"


# ---------------------------------------------------------------------------
# AC-02 -- identical checkpoint continuation authorized without scientific change
# ---------------------------------------------------------------------------


def test_retry_ac02_identical_checkpoint_continuation_authorized_without_ceiling():
    # AC-02: identical checkpoint continuation (no scientific change) is
    # authorized; max_identical_retries None = unlimited per contract.
    assessment = evaluate_automatic_retry(
        make_checkpoint(identical_retry_count=5)
    )
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert assessment.matched_rule_id == "R-RET-C1"
    assert assessment.matched_policy_entries == ()
    assert assessment.reasoning_ids == (
        REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
        REASON_CEILING_UNLIMITED,
    )
    assert assessment.ceiling is None
    assert assessment.ceiling_reached is False


def test_retry_ac02_identical_checkpoint_continuation_within_ceiling_authorized():
    # Identical checkpoint continuation stays authorized while the count
    # is below the policy's hard ceiling.
    policy = make_policy(max_identical_retries=2)
    for count in (0, 1):
        assessment = evaluate_automatic_retry(
            make_checkpoint(policy, identical_retry_count=count)
        )
        assert assessment.verdict is RetryAuthorization.AUTHORIZED
        assert assessment.matched_rule_id == "R-RET-C1"
        assert assessment.reasoning_ids == (
            REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
            REASON_CEILING_NOT_REACHED,
        )
        assert assessment.ceiling == 2
        assert assessment.ceiling_reached is False


def test_retry_ac02_identical_checkpoint_continuation_at_ceiling_rejected():
    # At the hard ceiling the identical continuation is rejected -- the
    # ceiling state and trigger are recorded in the assessment.
    policy = make_policy(max_identical_retries=2)
    assessment = evaluate_automatic_retry(
        make_checkpoint(policy, identical_retry_count=2)
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert assessment.matched_rule_id == "R-RET-C2"
    assert assessment.reasoning_ids == (
        REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
        REASON_CEILING_REACHED,
    )
    assert assessment.ceiling == 2
    assert assessment.ceiling_reached is True


def test_retry_ac02_max_identical_retries_is_a_hard_ceiling():
    # The int ceiling is hard: counts below it are authorized, the
    # ceiling itself and everything above are rejected, and a zero
    # ceiling authorizes nothing.
    policy = make_policy(max_identical_retries=2)
    assert (
        evaluate_automatic_retry(
            make_checkpoint(policy, identical_retry_count=1)
        ).verdict
        is RetryAuthorization.AUTHORIZED
    )
    for count in (2, 3, 10):
        assessment = evaluate_automatic_retry(
            make_checkpoint(policy, identical_retry_count=count)
        )
        assert assessment.verdict is RetryAuthorization.REJECTED
        assert assessment.matched_rule_id == "R-RET-C2"
    zero = make_policy(max_identical_retries=0)
    assert (
        evaluate_automatic_retry(
            make_checkpoint(zero, identical_retry_count=0)
        ).verdict
        is RetryAuthorization.REJECTED
    )


def test_retry_ac02_ceiling_state_records_count_against_ceiling():
    # The assessment records the identical-retry count against the
    # ceiling verbatim (AC-02).
    policy = make_policy(max_identical_retries=3)
    assessment = evaluate_automatic_retry(
        make_checkpoint(policy, identical_retry_count=2)
    )
    assert assessment.input.identical_retry_count == 2
    assert assessment.ceiling == policy.max_identical_retries == 3
    assert assessment.ceiling_reached is False
    at_limit = evaluate_automatic_retry(
        make_checkpoint(policy, identical_retry_count=3)
    )
    assert at_limit.ceiling_reached is True


def test_retry_ac02_checkpoint_continuation_authorized_without_whitelist_entry():
    # AC-02 authorizes the identical checkpoint continuation from the
    # policy contract (the ceiling gate), not from a whitelist entry: a
    # policy that lists no checkpoint kind at all still authorizes it.
    policy = make_policy(
        allowed_engineering_failures=("network_timeout",),
        supervisor_required_changes=(),
        max_identical_retries=1,
    )
    assert "checkpoint_continuation" not in policy.allowed_engineering_failures
    assessment = evaluate_automatic_retry(
        make_checkpoint(policy, identical_retry_count=0)
    )
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.matched_rule_id == "R-RET-C1"


def test_retry_ac02_checkpoint_continuation_in_invalidate_run_on_situation_rejected():
    # AC-02: checkpoint continuation is authorized "only when ... not an
    # invalidate_run_on situation" -- those failure kinds invalidate the
    # run instead of retrying, and the trigger is recorded.
    policy = make_policy(invalidate_run_on=("checkpoint_continuation",))
    assessment = evaluate_automatic_retry(make_checkpoint(policy))
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert assessment.matched_rule_id == "R-RET-I1"
    assert assessment.matched_policy_entries == ("checkpoint_continuation",)
    assert assessment.reasoning_ids == (REASON_INVALIDATE_RUN,)


def test_retry_ac02_checkpoint_continuation_not_in_invalidate_run_on_authorized():
    # A policy that invalidates a different kind (e.g. sample_loss) does
    # not block the identical checkpoint continuation.
    policy = make_policy(invalidate_run_on=("sample_loss",))
    assessment = evaluate_automatic_retry(make_checkpoint(policy))
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.matched_rule_id == "R-RET-C1"


def test_retry_ac02_checkpoint_continuation_requires_identical_declaration():
    # The evaluator cannot observe scientific change; the declaration is
    # the input. A checkpoint continuation without the identical flag --
    # or the identical flag on any other kind -- is a contradictory input
    # rejected with a stable one-line message.
    with pytest.raises(RetryEvaluationError) as exc:
        make_input(
            failure_kind="checkpoint_continuation", checkpoint_continuation=False
        )
    assert "checkpoint_continuation" in str(exc.value)
    assert "\n" not in str(exc.value)
    with pytest.raises(RetryEvaluationError) as exc:
        make_input(failure_kind="ssh_connection_lost", checkpoint_continuation=True)
    assert "checkpoint_continuation" in str(exc.value)
    with pytest.raises(RetryEvaluationError):
        make_input(
            failure_kind="smearing_parameter_change", checkpoint_continuation=True
        )


def test_retry_ac02_identical_checkpoint_continuation_is_never_scientific():
    # AC-02's authorization is engineering-only: the reasoning never
    # names a supervisor route and the routing stays AUTOMATIC.
    assessment = evaluate_automatic_retry(make_checkpoint())
    assert assessment.verdict is RetryAuthorization.AUTHORIZED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert REASON_SUPERVISOR_REQUIRED_CHANGE not in assessment.reasoning_ids
    assert REASON_SCIENTIFIC_CHANGE_VOCABULARY not in assessment.reasoning_ids


# ---------------------------------------------------------------------------
# AC-03 -- scientific parameter modifications rejected for automatic action
# ---------------------------------------------------------------------------


def test_retry_ac03_supervisor_required_change_routed_to_supervisor():
    # AC-03: a scientific parameter modification in the policy's
    # supervisor_required_changes is rejected for automatic worker action
    # and routed to the Supervisor -- as a decision record, never an
    # error and never an execution.
    assessment = evaluate_automatic_retry(
        make_input(failure_kind="smearing_parameter_change")
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.SUPERVISOR
    assert assessment.matched_rule_id == "R-RET-S1"
    assert assessment.matched_policy_entries == ("smearing_parameter_change",)
    assert assessment.reasoning_ids == (REASON_SUPERVISOR_REQUIRED_CHANGE,)


def test_retry_ac03_scientific_change_never_authorized_even_when_whitelisted():
    # The strongest AC-03 reading: a known scientific modification that a
    # (malformed) policy also whitelists is STILL never authorized -- the
    # frozen vocabulary catches it before the whitelist rule fires.
    policy = make_policy(
        allowed_engineering_failures=("functional_change", "ssh_connection_lost"),
        supervisor_required_changes=(),
    )
    assessment = evaluate_automatic_retry(
        make_input(policy, failure_kind="functional_change")
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.SUPERVISOR
    assert assessment.matched_rule_id == "R-RET-V1"
    assert assessment.reasoning_ids == (REASON_SCIENTIFIC_CHANGE_VOCABULARY,)
    # The same policy's whitelisted engineering failure stays authorized.
    engineering = evaluate_automatic_retry(
        make_input(policy, failure_kind="ssh_connection_lost")
    )
    assert engineering.verdict is RetryAuthorization.AUTHORIZED


@pytest.mark.parametrize("failure_kind", sorted(SCIENTIFIC_CHANGE_FAILURES))
def test_retry_ac03_every_vocabulary_scientific_change_routed_to_supervisor(
    failure_kind,
):
    # The frozen detection vocabulary: every known scientific parameter
    # modification is rejected for automatic action and routed to the
    # Supervisor even when the policy lists nothing (the vocabulary aids
    # detection; the policy's supervisor_required_changes stay the
    # authoritative list).
    policy = make_policy(
        allowed_engineering_failures=("network_timeout",),
        supervisor_required_changes=(),
    )
    assessment = evaluate_automatic_retry(
        make_input(policy, failure_kind=failure_kind)
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.SUPERVISOR
    assert assessment.matched_rule_id == "R-RET-V1"
    assert assessment.matched_policy_entries == (failure_kind,)
    assert assessment.reasoning_ids == (REASON_SCIENTIFIC_CHANGE_VOCABULARY,)


def test_retry_ac03_fdm201_counter_case_never_auto_authorized():
    # The FDM-201 counter-case (examples/fdm-201/simulated-scenarios.md
    # S5): changing smearing/mixing/convergence policy would alter the
    # method, so each of those modifications is never auto-authorized.
    for failure_kind in (
        "smearing_parameter_change",
        "mixing_parameter_change",
        "convergence_policy_change",
    ):
        assessment = evaluate_automatic_retry(
            make_input(failure_kind=failure_kind)
        )
        assert assessment.verdict is RetryAuthorization.REJECTED
        assert assessment.routing is RetryRouting.SUPERVISOR


def test_retry_ac03_supervisor_routing_is_a_decision_record_not_an_error():
    # The SUPERVISOR routing is a normal, frozen assessment -- no
    # exception is raised, and the assessment carries the full decision
    # record with no reference to any execution path.
    assessment = evaluate_automatic_retry(
        make_input(failure_kind="smearing_parameter_change")
    )
    assert isinstance(assessment, RetryAssessment)
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.SUPERVISOR
    assert assessment.matched_rule_id in {
        rule.rule_id for rule in RETRY_DECISION_RULES
    }
    assert assessment.reasoning_ids
    for name in ("execute", "apply", "perform", "dispatch", "handler"):
        assert not hasattr(retry_module, name), name


def test_retry_ac03_supervisor_required_changes_are_authoritative_list():
    # The AUTHORITATIVE list is the policy's supervisor_required_changes:
    # a modification the policy names is routed by the policy rule
    # (R-RET-S1); one the policy does not name but the vocabulary knows
    # is caught by detection (R-RET-V1); one nobody knows (neither the
    # policy nor the frozen vocabulary) is rejected by the default -- the
    # evaluator never invents a supervisor route.
    policy = make_policy(supervisor_required_changes=("functional_change",))
    named = evaluate_automatic_retry(
        make_input(policy, failure_kind="functional_change")
    )
    assert named.matched_rule_id == "R-RET-S1"
    detected = evaluate_automatic_retry(
        make_input(policy, failure_kind="mixing_rule_change")
    )
    assert detected.matched_rule_id == "R-RET-V1"
    assert detected.routing is RetryRouting.SUPERVISOR
    unknown = evaluate_automatic_retry(
        make_input(policy, failure_kind="basis_set_change")
    )
    assert unknown.verdict is RetryAuthorization.REJECTED
    assert unknown.matched_rule_id == "R-RET-D1"
    assert unknown.routing is RetryRouting.AUTOMATIC
    assert unknown.reasoning_ids == (REASON_NO_POLICY_ENTRY,)


def test_retry_ac03_supervisor_required_change_wins_over_whitelist_and_invalidation():
    # Precedence: scientific classification is decided first -- a kind
    # listed in every policy list is routed to the Supervisor, never
    # auto-authorized and never merely invalidated.
    policy = make_policy(
        allowed_engineering_failures=("smearing_parameter_change",),
        supervisor_required_changes=("smearing_parameter_change",),
        invalidate_run_on=("smearing_parameter_change",),
    )
    assessment = evaluate_automatic_retry(
        make_input(policy, failure_kind="smearing_parameter_change")
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.SUPERVISOR
    assert assessment.matched_rule_id == "R-RET-S1"


def test_retry_ac03_invalidate_run_on_wins_over_whitelist():
    # Invalidation beats the whitelist: a kind listed in both
    # invalidate_run_on and allowed_engineering_failures invalidates the
    # run instead of retrying -- the whitelist entry is not consulted.
    policy = make_policy(
        allowed_engineering_failures=("sample_loss",),
        invalidate_run_on=("sample_loss",),
    )
    assessment = evaluate_automatic_retry(
        make_input(policy, failure_kind="sample_loss")
    )
    assert assessment.verdict is RetryAuthorization.REJECTED
    assert assessment.routing is RetryRouting.AUTOMATIC
    assert assessment.matched_rule_id == "R-RET-I1"
    assert assessment.reasoning_ids == (REASON_INVALIDATE_RUN,)


def test_retry_ac03_vocabulary_is_frozen_and_disjoint_from_automatic_kinds():
    # The vocabulary is a frozen set and never overlaps the automatic
    # retry kinds: no scientific kind is the checkpoint continuation, and
    # none of the engineering kinds of the spec are scientific.
    assert isinstance(SCIENTIFIC_CHANGE_FAILURES, frozenset)
    assert CHECKPOINT_CONTINUATION_KIND not in SCIENTIFIC_CHANGE_FAILURES
    for kind in (
        "ssh_connection_lost",
        "network_timeout",
        "scheduler_node_failure",
        CHECKPOINT_CONTINUATION_KIND,
    ):
        assert kind not in SCIENTIFIC_CHANGE_FAILURES


# ---------------------------------------------------------------------------
# Boundary -- the evaluator sits on the context package and the matrix
# ---------------------------------------------------------------------------


def test_retry_boundary_authorization_aligns_with_role_action_matrix():
    # The evaluator's boundary is the role-action matrix of DEV-M6-G03:
    # what it authorizes is the pre-authorized ENGINEERING_RETRY (granted
    # to the Monitor and the Experiment/Computation workers), and what it
    # routes to the Supervisor is exactly the Supervisor-only
    # SCIENTIFIC_PARAMETER_CHANGE boundary.
    for role in (Role.MONITOR, Role.EXPERIMENT_WORKER, Role.COMPUTATION_WORKER):
        assert is_action_allowed(role, Action.ENGINEERING_RETRY) is True
    assert (
        is_action_allowed(Role.EXPERIMENT_WORKER, Action.SCIENTIFIC_PARAMETER_CHANGE)
        is False
    )
    assert is_action_allowed(Role.SUPERVISOR, Action.SCIENTIFIC_PARAMETER_CHANGE) is True
    automatic = evaluate_automatic_retry(
        make_input(failure_kind="ssh_connection_lost")
    )
    assert automatic.verdict is RetryAuthorization.AUTHORIZED
    assert automatic.routing is RetryRouting.AUTOMATIC
    scientific = evaluate_automatic_retry(
        make_input(failure_kind="smearing_parameter_change")
    )
    assert scientific.verdict is RetryAuthorization.REJECTED
    assert scientific.routing is RetryRouting.SUPERVISOR


def test_retry_boundary_failure_kinds_are_the_context_retry_action_suffixes():
    # The evaluator is the decision layer the context package's
    # "retry:<failure>" actions point at: failure kinds are the exact
    # suffixes of those actions (workers/context.py), never prefixed or
    # invented.
    for kind in (
        "ssh_connection_lost",
        "network_timeout",
        "scheduler_node_failure",
        CHECKPOINT_CONTINUATION_KIND,
        "smearing_parameter_change",
    ):
        assert "retry:" not in kind
        assert " " not in kind
    assessment = evaluate_automatic_retry(
        make_input(failure_kind="ssh_connection_lost")
    )
    assert f"retry:{assessment.input.failure_kind}" == "retry:ssh_connection_lost"


def test_retry_boundary_module_exposes_pure_evaluation_only_no_execution_api():
    # AC-03 boundary proof: the module is a pure decision layer -- no
    # execution API exists in it, so the Supervisor-routed changes have
    # no path to be performed by this worker-facing module (module-dict
    # and source inspection).
    source = inspect.getsource(retry_module)
    for name in ("execute", "apply", "perform", "dispatch"):
        assert f"def {name}" not in source, name
        assert f".{name}(" not in source, name
    for export in retry_module.__all__:
        assert not export.startswith(("execute", "apply", "perform", "dispatch"))
    for name in ("execute", "apply", "perform", "dispatch", "run_retry", "execute_retry"):
        assert name not in retry_module.__dict__, name


# ---------------------------------------------------------------------------
# Paradigm boundaries
# ---------------------------------------------------------------------------


def test_retry_paradigm_rule_table_first_match_wins_total_default():
    inputs = (
        make_input(),  # whitelist (AC-01)
        make_checkpoint(),  # checkpoint (AC-02)
        make_checkpoint(
            make_policy(max_identical_retries=2), identical_retry_count=5
        ),  # ceiling exhausted
        make_input(failure_kind="smearing_parameter_change"),  # supervisor (AC-03)
        make_input(
            make_policy(allowed_engineering_failures=("network_timeout",)),
            failure_kind="ssh_connection_lost",
        ),  # default reject
        make_input(
            make_policy(invalidate_run_on=("sample_loss",)),
            failure_kind="sample_loss",
        ),  # invalidation
    )
    for input_ in inputs:
        assessment = evaluate_automatic_retry(input_)
        assert assessment.matched_rule_id is not None
        assert len(assessment.decisions) == len(RETRY_DECISION_RULES)
        assert [decision.rule_id for decision in assessment.decisions] == [
            rule.rule_id for rule in RETRY_DECISION_RULES
        ]
        assert sum(1 for decision in assessment.decisions if decision.matched) >= 1
        matched = next(
            decision
            for decision in assessment.decisions
            if decision.rule_id == assessment.matched_rule_id
        )
        assert matched.matched
        assert matched.verdict is assessment.verdict
        assert matched.routing is assessment.routing
        first_matched = next(decision for decision in assessment.decisions if decision.matched)
        assert first_matched.rule_id == assessment.matched_rule_id
    # The default rule is the total default.
    assert RETRY_DECISION_RULES[-1].rule_id == "R-RET-D1"
    assert RETRY_DECISION_RULES[-1].verdict is RetryAuthorization.REJECTED
    assert RETRY_DECISION_RULES[-1].routing is RetryRouting.AUTOMATIC


def test_retry_paradigm_ruleset_version_recorded():
    assert RETRY_AUTHORIZATION_RULESET_VERSION == "1.0"
    assert (
        evaluate_automatic_retry(make_input()).ruleset_version
        == RETRY_AUTHORIZATION_RULESET_VERSION
    )


def test_retry_paradigm_determinism_across_repeated_calls():
    # Pure functions: same inputs, same outputs, on every call.
    inputs = (
        make_input(),
        make_checkpoint(
            make_policy(max_identical_retries=2), identical_retry_count=1
        ),
        make_input(failure_kind="smearing_parameter_change"),
    )
    for input_ in inputs:
        first = evaluate_automatic_retry(input_)
        second = evaluate_automatic_retry(input_)
        assert first == second
        assert repr(first) == repr(second)
        assert first.decisions == second.decisions
        assert first.matched_policy_entries == second.matched_policy_entries
        assert first.reasoning_ids == second.reasoning_ids


def test_retry_paradigm_frozen_records_reject_mutation():
    records = (
        make_input(),
        evaluate_automatic_retry(make_input()),
        evaluate_automatic_retry(make_input()).decisions[0],
        RetryRuleDecision(
            rule_id="R-RET-D1",
            description="default",
            verdict=RetryAuthorization.REJECTED,
            routing=RetryRouting.AUTOMATIC,
            matched=False,
        ),
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)


def test_retry_paradigm_typeerror_at_boundaries():
    # Wrong types are rejected with TypeError at the public boundaries,
    # never ValueError.
    with pytest.raises(TypeError):
        evaluate_automatic_retry(None)
    with pytest.raises(TypeError):
        evaluate_automatic_retry("ssh_connection_lost")
    with pytest.raises(TypeError):
        evaluate_automatic_retry(make_input().__dict__)
    with pytest.raises(TypeError):
        RetryEvaluationInput(policy={}, failure_kind="x")
    with pytest.raises(TypeError):
        RetryEvaluationInput(policy=make_policy(), failure_kind=7)
    with pytest.raises(TypeError):
        RetryEvaluationInput(
            policy=make_policy(), failure_kind="x", identical_retry_count=True
        )
    with pytest.raises(TypeError):
        RetryEvaluationInput(
            policy=make_policy(), failure_kind="x", identical_retry_count=1.5
        )
    with pytest.raises(TypeError):
        RetryEvaluationInput(
            policy=make_policy(), failure_kind="x", identical_retry_count="1"
        )
    with pytest.raises(TypeError):
        RetryEvaluationInput(
            policy=make_policy(), failure_kind="x", checkpoint_continuation=1
        )
    with pytest.raises(TypeError):
        RetryRule(
            rule_id="R-RET-X",
            description="x",
            verdict="AUTHORIZED",
            routing=RetryRouting.AUTOMATIC,
            predicate=lambda i: True,
        )
    with pytest.raises(TypeError):
        RetryRule(
            rule_id="R-RET-X",
            description="x",
            verdict=RetryAuthorization.REJECTED,
            routing="AUTOMATIC",
            predicate=lambda i: True,
        )
    with pytest.raises(TypeError):
        RetryRule(
            rule_id="R-RET-X",
            description="x",
            verdict=RetryAuthorization.REJECTED,
            routing=RetryRouting.AUTOMATIC,
            predicate=None,
        )
    assessment = evaluate_automatic_retry(make_input())
    with pytest.raises(TypeError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=[assessment.decisions[0]],
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(TypeError):
        RetryAssessment(
            input=assessment.input,
            verdict="AUTHORIZED",
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(TypeError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
            ceiling=True,
        )
    with pytest.raises(TypeError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
            ceiling_reached=1,
        )


def test_retry_paradigm_value_errors_stable_and_one_line():
    # Value/rule violations raise stable one-line messages: the same
    # degenerate input always raises the same message text.
    with pytest.raises(RetryEvaluationError) as first:
        make_input(failure_kind="  ")
    with pytest.raises(RetryEvaluationError) as second:
        make_input(failure_kind="  ")
    assert str(first.value) == str(second.value)
    assert "non-empty" in str(first.value)
    assert "\n" not in str(first.value)
    with pytest.raises(RetryEvaluationError) as exc:
        make_input(failure_kind="ssh_connection_lost", identical_retry_count=-1)
    assert ">= 0" in str(exc.value)
    assert "\n" not in str(exc.value)
    # Direct construction of an inconsistent assessment is rejected.
    assessment = evaluate_automatic_retry(make_input())
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=(),
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-NOPE",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.SUPERVISOR,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("invented_reason",),
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("",),
            reasoning_ids=("allowed_engineering_failures",),
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=assessment.input,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-A1",
            matched_policy_entries=("ssh_connection_lost",),
            reasoning_ids=("allowed_engineering_failures",),
            ceiling_reached=True,
        )


def test_retry_paradigm_errors_are_valueerror_subclasses_with_stable_messages():
    assert issubclass(RetryPolicyError, ValueError)
    assert issubclass(RetryEvaluationError, RetryPolicyError)
    assert issubclass(MalformedRetryPolicyError, RetryPolicyError)
    assert issubclass(RetryRulesetError, RetryPolicyError)


def test_retry_paradigm_malformed_policy_rejected():
    # The evaluator consumes the policy as the contract: a malformed
    # record (wrong entry types, blank entries, negative, boolean or
    # float ceiling) is rejected with a stable one-line message before
    # any decision.
    for field_name, bad_value in (
        ("allowed_engineering_failures", [7]),
        ("allowed_engineering_failures", ["ssh_connection_lost", 7]),
        ("supervisor_required_changes", [""]),
        ("invalidate_run_on", [None]),
        ("max_identical_retries", -1),
        ("max_identical_retries", True),
        ("max_identical_retries", 2.5),
    ):
        policy = replace(make_policy(), **{field_name: bad_value})
        with pytest.raises(MalformedRetryPolicyError) as exc:
            make_input(policy=policy)
        assert "retry policy" in str(exc.value)
        assert "\n" not in str(exc.value)
    # The well-formed default policy still evaluates.
    assert (
        evaluate_automatic_retry(make_input()).verdict
        is RetryAuthorization.AUTHORIZED
    )


def test_retry_paradigm_policy_record_never_mutated():
    # The evaluator never mutates the frozen policy record: evaluating
    # several failures against it leaves it byte-identical.
    policy = make_policy()
    snapshot = AutomaticRetryPolicy(
        policy_id=policy.policy_id,
        allowed_engineering_failures=list(policy.allowed_engineering_failures),
        supervisor_required_changes=list(policy.supervisor_required_changes),
        max_identical_retries=policy.max_identical_retries,
        invalidate_run_on=list(policy.invalidate_run_on),
    )
    evaluate_automatic_retry(make_input(policy, failure_kind="network_timeout"))
    evaluate_automatic_retry(make_input(policy, failure_kind="smearing_parameter_change"))
    evaluate_automatic_retry(make_checkpoint(policy))
    assert policy == snapshot
    with pytest.raises(FrozenInstanceError):
        setattr(policy, "max_identical_retries", 5)


def test_retry_paradigm_assessment_ceiling_integrity_checks():
    # Direct construction: the recorded ceiling must equal the policy's
    # verbatim, and ceiling_reached must match the identical-retry count
    # of a checkpoint continuation.
    input_ = make_checkpoint(
        make_policy(max_identical_retries=2), identical_retry_count=0
    )
    assessment = evaluate_automatic_retry(input_)
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=input_,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-C1",
            matched_policy_entries=(),
            reasoning_ids=(
                REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
                REASON_CEILING_NOT_REACHED,
            ),
            ceiling=1,
            ceiling_reached=False,
        )
    with pytest.raises(RetryEvaluationError):
        RetryAssessment(
            input=input_,
            verdict=RetryAuthorization.AUTHORIZED,
            routing=RetryRouting.AUTOMATIC,
            decisions=assessment.decisions,
            matched_rule_id="R-RET-C1",
            matched_policy_entries=(),
            reasoning_ids=(
                REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
                REASON_CEILING_NOT_REACHED,
            ),
            ceiling=2,
            ceiling_reached=True,
        )
    # The honest record is accepted and equals the evaluated assessment.
    honest = RetryAssessment(
        input=input_,
        verdict=RetryAuthorization.AUTHORIZED,
        routing=RetryRouting.AUTOMATIC,
        decisions=assessment.decisions,
        matched_rule_id="R-RET-C1",
        matched_policy_entries=(),
        reasoning_ids=(
            REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
            REASON_CEILING_NOT_REACHED,
        ),
        ceiling=2,
        ceiling_reached=False,
    )
    assert honest == assessment


def test_retry_paradigm_ruleset_validation():
    ids = validate_retry_ruleset()
    assert ids == tuple(rule.rule_id for rule in RETRY_DECISION_RULES)
    assert len(ids) == len(set(ids))
    assert ids[-1] == "R-RET-D1"
    with pytest.raises(RetryRulesetError) as exc:
        validate_retry_ruleset(())
    assert "must not be empty" in str(exc.value)
    duplicated = RETRY_DECISION_RULES[:2] + RETRY_DECISION_RULES[:2]
    with pytest.raises(RetryRulesetError) as exc:
        validate_retry_ruleset(duplicated)
    assert "duplicate rule id" in str(exc.value)
    not_total = RETRY_DECISION_RULES[:-1] + (
        RetryRule(
            rule_id="R-RET-BAD",
            description="not a total default",
            verdict=RetryAuthorization.REJECTED,
            routing=RetryRouting.AUTOMATIC,
            predicate=lambda i: False,
        ),
    )
    with pytest.raises(RetryRulesetError) as exc:
        validate_retry_ruleset(not_total)
    assert "total default" in str(exc.value)
    with pytest.raises(TypeError):
        validate_retry_ruleset("rules")
    with pytest.raises(TypeError):
        validate_retry_ruleset((1, 2))


def test_retry_paradigm_module_all_exports_resolve():
    for name in retry_module.__all__:
        assert hasattr(retry_module, name), name
    # Declared exactly once (no duplicate export).
    assert len(retry_module.__all__) == len(set(retry_module.__all__))
    source = inspect.getsource(retry_module)
    assert "type: ignore" not in source
    assert "# noqa" not in source


def test_retry_paradigm_verdict_and_routing_vocabulary_frozen():
    # The verdict/routing vocabularies are exactly the frozen enum
    # members -- no invented strings.
    assert [member.value for member in RetryAuthorization] == [
        "AUTHORIZED",
        "REJECTED",
    ]
    assert [member.value for member in RetryRouting] == [
        "AUTOMATIC",
        "SUPERVISOR",
    ]
    assert isinstance(REASON_IDS, frozenset)
    assert REASON_NO_POLICY_ENTRY in REASON_IDS
    assert REASON_ALLOWED_ENGINEERING_FAILURE in REASON_IDS


def test_retry_paradigm_module_is_pure_no_io_no_randomness():
    source = inspect.getsource(retry_module)
    for forbidden in (
        "import random",
        "random.",
        "time.time",
        "datetime.now",
        "timezone",
        "urllib",
        "requests",
        "socket",
        "open(",
        "os.",
        "sys.",
        "pathlib",
        "atomic_write",
        "import json",
        "hashlib",
    ):
        assert forbidden not in source, forbidden
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
        assert forbidden not in retry_module.__dict__, forbidden
