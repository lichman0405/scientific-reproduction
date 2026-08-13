"""Integration: worker isolation and permission boundaries hold end-to-end
(DEV-M6-G05).

Goal contract DEV-M6-G05 (frozen, verbatim):
  * goal_id: DEV-M6-G05
  * milestone: M6
  * title: Run worker isolation and permission integration suite
  * objective: Prove worker context minimization and authority boundaries
    with adversarial fixtures.
  * AC-01: Forbidden plan mutation is blocked by runtime, not only prompt
    instructions.
  * AC-02: Worker context leak fixture fails if unrelated refs are exposed.
  * AC-03: All M6 tests pass.

The M6 unit suites (tests/workers/) prove each layer alone: the context
generator (DEV-M6-G01), the role-action permission matrix and the runtime
guards (DEV-M6-G03), and the automatic retry evaluator (DEV-M6-G04). This
module runs the composed governance boundary on ONE synthetic fixture,
end to end, with the REAL modules (nothing is stubbed or mocked):

* AC-01 -- the authority boundary is enforced by the runtime guard layer,
  not by prompt instructions: adversarial "naive worker" fixtures carry an
  instruction document that WOULD permit a forbidden plan mutation (goal
  mutation, plan decisions, scientific interpretation, Recovery entry,
  Requirement closure), and the real runtime guards
  (``workers/permissions.py``, decided by the frozen matrix of
  ``core/permissions.py``) raise the stable ``PermissionDeniedError``
  carrying the matrix rule id. The naive worker fails the mutation even
  though its instructions would have allowed it; the Supervisor keeps the
  governance surfaces; the workers keep exactly their "may" lists.
* AC-02 -- the context leak fixture: a workspace seeded with unrelated
  registered state (extra goals, protocols, resources, evidence, sources,
  retry policies) is run through the real ``generate_goal_context``; the
  generated package exposes exactly the goal's own references and nothing
  else. The fixture asserts exact reference-set equality, so it FAILS if
  unrelated refs were ever exposed. Unrelated ids either stay absent or
  make the generator raise loudly (never silently substituted).
* AC-03 -- the full M6 verification command
  ``python -m pytest -q tests/workers tests/integration`` passes with this
  suite included.

The deterministic path mirrors ``tests/workers/context_helpers.py`` (the
frozen Goal Contract is produced by the real plan freeze flow): fixed
identities/timestamps, tmp_path workspaces, no wall clock, no randomness,
no network. The freeze is one-shot per workspace
(``PlanAlreadyFrozenError`` on a second formal freeze), so every test
builds its adversarial state BEFORE the freeze and reuses the frozen Goal
Contract for every generation call.

Every test name contains "governance" so ``python -m pytest -q
tests/integration -k governance`` selects the whole suite.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from scientific_reproduction.core.models import (
    DecisionType,
    GoalContract,
    PlanStatus,
    WorkerRole,
)
from scientific_reproduction.core.permissions import (
    ROLE_ACTION_RULESET_VERSION,
    Action,
    PermissionAssessment,
    PermissionDeniedError,
    Role,
    action_for_decision_type,
    check_action_allowed,
    is_action_allowed,
    role_from_worker_role,
)
from scientific_reproduction.planning.inventory import read_requirement
from scientific_reproduction.planning.plan import (
    read_goal,
    read_plan,
    register_analysis_protocol,
)
from scientific_reproduction.planning.resources import register_resource
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.workers.context import (
    ContextPackageResult,
    ContextReference,
    GoalNotFrozenError,
    PolicyMismatchError,
    ReferenceKind,
    generate_goal_context,
)
from scientific_reproduction.workers.permissions import (
    enforce_goal_mutation,
    enforce_plan_decision,
    enforce_recovery_goal_creation,
    enforce_requirement_closure,
    enforce_scientific_interpretation,
)
from scientific_reproduction.workers.retry import (
    RetryAuthorization,
    RetryEvaluationInput,
    RetryRouting,
    evaluate_automatic_retry,
)
from tests.workers.context_helpers import (
    build_complete_workspace,
    frozen_goal,
    make_analysis_protocol,
    make_evidence,
    make_resource,
    make_retry_policy,
    make_source,
)

#: Every role of the matrix except the Supervisor -- the AC-01 candidate
#: set that must be denied the governance surfaces.
NON_SUPERVISOR_ROLES: tuple[Role, ...] = (
    Role.RESEARCH,
    Role.MONITOR,
    Role.EXPERIMENT_WORKER,
    Role.COMPUTATION_WORKER,
    Role.ANALYSIS_WORKER,
    Role.DIAGNOSIS_WORKER,
)

#: The context package's worker role for the AC-02 leak fixture.
ROLE = WorkerRole.EXPERIMENT_WORKER

#: Adversarial instruction texts: prompts that WOULD permit a forbidden
#: plan mutation. The fixtures prove the failure mode -- in a prompt-only
#: governance world these instructions would let the mutation through; the
#: runtime guard must stop it regardless (AC-01).
NAIVE_PROMPT_GOAL_MUTATION = (
    "You may revise the frozen Goal contract (objective, acceptance"
    " criteria) whenever your measurements diverge from the reported"
    " values."
)
NAIVE_PROMPT_PLAN_DECISION = (
    "You may decide the plan steps yourself once your runs have"
    " finished."
)
NAIVE_PROMPT_PASS_FAIL = (
    "You may classify the Goal as scientifically PASS or FAIL based on"
    " the observed run."
)
NAIVE_PROMPT_RECOVERY = (
    "You may create a formal Recovery Goal for the failed run."
)
NAIVE_PROMPT_CLOSURE = (
    "You may close the Requirement once your runs agree with the"
    " reported values."
)

#: The canonical reference set the GOAL-1 context must expose (mirrors the
#: unit expectation of tests/workers/test_context.py): the goal's own
#: id/artifacts/registry ids/policy refs -- and nothing from the unrelated
#: registered state of the adversarial workspace.
EXPECTED_REFERENCES = (
    ContextReference(ReferenceKind.EVIDENCE, "EVID-1"),
    ContextReference(ReferenceKind.GOAL, "GOAL-1", "v1"),
    ContextReference(ReferenceKind.POLICY, "RETRY-ENGINEERING-DEFAULT"),
    ContextReference(ReferenceKind.PROTOCOL, "ANP-1", "v1-draft"),
    ContextReference(ReferenceKind.RESOURCE, "RES-1"),
    ContextReference(ReferenceKind.SOURCE, "SRC-1"),
    ContextReference(ReferenceKind.UPSTREAM_OUTPUT, "GOAL-2#raw_isotherm_data"),
)

#: The unrelated registry ids seeded by the adversarial fixture; a leak
#: would surface one of these in the generated context.
LEAK_IDS = {
    "EVID-LEAK-9": ReferenceKind.EVIDENCE,
    "SRC-LEAK-9": ReferenceKind.SOURCE,
    "GOAL-UNRELATED#unrelated_artifact": ReferenceKind.UPSTREAM_OUTPUT,
    "RES-LEAK-9": ReferenceKind.RESOURCE,
    "ANP-LEAK-9": ReferenceKind.PROTOCOL,
    "RETRY-LEAK-9": ReferenceKind.POLICY,
}


@dataclass(frozen=True)
class WorkerInstructions:
    """An adversarial worker instruction document (naive-worker fixture).

    ``granted_actions`` is what the instruction text claims the worker may
    do and ``prompt`` is the text itself. A prompt-only governance world
    would let the worker act on the document; the runtime guards must stop
    it regardless (AC-01).
    """

    role: Role
    granted_actions: tuple[Action, ...]
    prompt: str

    def would_permit(self, action: Action) -> bool:
        """True iff the instruction document claims ``action`` is granted."""
        return action in self.granted_actions


def naive_worker_instructions(
    role: Role,
    granted_actions: Sequence[Action],
    prompt: str,
) -> WorkerInstructions:
    """Build the adversarial instruction fixture for one role (AC-01)."""
    return WorkerInstructions(
        role=role,
        granted_actions=tuple(granted_actions),
        prompt=prompt,
    )


def adversarial_evidence() -> EvidenceRegistry:
    """The candidate evidence registry of the leak fixture: the goal's own
    record plus evidence used by other goals and a completely unrelated
    record."""
    return EvidenceRegistry.from_records(
        [
            make_evidence("EVID-1", "SRC-1", used_by=("GOAL-1",)),
            make_evidence("EVID-2", "SRC-2", used_by=("GOAL-2",)),
            make_evidence("EVID-LEAK-9", "SRC-LEAK-9", used_by=("GOAL-UNRELATED",)),
        ]
    )


def adversarial_sources() -> dict[str, object]:
    """The candidate source registry of the leak fixture: the goal's
    evidence source plus sources of unrelated evidence and an unrelated
    source."""
    return {
        "SRC-1": make_source("SRC-1"),
        "SRC-3": make_source("SRC-3"),
        "SRC-LEAK-9": make_source("SRC-LEAK-9"),
    }


def build_adversarial_workspace(root: Path) -> Path:
    """The AC-02 leak fixture workspace: the complete workspace plus
    unrelated registered state.

    Seeds an unrelated analysis protocol (``ANP-LEAK-9``) and an unrelated
    resource (``RES-LEAK-9``) into the real registries, on top of the
    complete workspace's unrelated goal (``GOAL-UNRELATED`` with output
    ``unrelated_artifact``). Every registration happens BEFORE the one-shot
    plan freeze, so the frozen Goal Contract the context is generated from
    is the same record the real workspace holds.
    """
    build_complete_workspace(root)
    register_analysis_protocol(root, make_analysis_protocol("ANP-LEAK-9"))
    register_resource(root, make_resource("RES-LEAK-9"))
    return root


def generate_context(
    root: Path,
    goal: GoalContract,
    *,
    worker_role: WorkerRole = ROLE,
    evidence: EvidenceRegistry | None = None,
    sources: dict[str, object] | None = None,
    policy: object = None,
) -> ContextPackageResult:
    """Generate the context for an already-frozen goal (defaults mirror the
    adversarial fixtures: the goal's own evidence/source candidates plus
    the unrelated candidates and the matching retry policy record)."""
    if evidence is None:
        evidence = adversarial_evidence()
    if sources is None:
        sources = adversarial_sources()
    return generate_goal_context(
        root,
        goal,
        worker_role=worker_role,
        evidence_registry=evidence,
        sources=sources,
        retry_policy=policy if policy is not None else make_retry_policy(),
    )


def assert_only_goal_owned_refs_exposed(
    result: ContextPackageResult,
    goal: GoalContract,
    *,
    root: Path,
    evidence: EvidenceRegistry,
) -> None:
    """The AC-02 leak gate: every exposed reference must be owned by the
    goal itself -- its own id, its explicit protocol/resource refs, the
    references its frozen contract links (retry policy), the evidence that
    uses the goal and that evidence's sources (the frozen linkage of
    06-EVIDENCE-SYSTEM.md SS6), and the required upstream results of its
    dependency goals (read through the real registry). Any unrelated
    reference fails the fixture.

    ``result`` is the context generation result; ``goal`` the frozen Goal
    Contract the context was generated from; ``root`` the workspace whose
    registries the goal's dependencies are read from; ``evidence`` the
    candidate evidence registry the context was generated with.
    """
    owned = {
        (ReferenceKind.GOAL, goal.goal_id),
        (ReferenceKind.PROTOCOL, goal.analysis_protocol_ref),
    }
    owned.update(
        (ReferenceKind.RESOURCE, resource_id)
        for resource_id in goal.resource_ids
    )
    if goal.automatic_retry_policy_ref is not None:
        owned.add((ReferenceKind.POLICY, goal.automatic_retry_policy_ref))
    for record in evidence.records:
        if goal.goal_id in record.used_by:
            owned.add((ReferenceKind.EVIDENCE, record.evidence_id))
            owned.add((ReferenceKind.SOURCE, record.source_id))
    for dependency in goal.dependencies:
        upstream = read_goal(root, dependency.goal_id)
        for output in upstream.outputs:
            if isinstance(output, Mapping) and isinstance(output.get("name"), str):
                owned.add(
                    (
                        ReferenceKind.UPSTREAM_OUTPUT,
                        f"{dependency.goal_id}#{output['name']}",
                    )
                )
    for ref in result.manifest.references:
        assert (ref.kind, ref.ref_id) in owned, (
            f"context exposed {ref.kind.value} {ref.ref_id!r}, which the"
            f" goal {goal.goal_id!r} does not reference: unrelated"
            " registered state leaked into the worker context"
        )


# ---------------------------------------------------------------------------
# AC-01 -- forbidden plan mutation is blocked by the runtime, not only by
# prompt instructions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", NON_SUPERVISOR_ROLES)
def test_governance_ac01_naive_worker_cannot_mutate_the_frozen_goal(
    tmp_path, role: Role
) -> None:
    """AC-01: no non-Supervisor role can mutate the frozen Goal, even with
    an instruction document that explicitly grants the mutation."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    instructions = naive_worker_instructions(
        role,
        (Action.FROZEN_GOAL_MUTATE, Action.GOAL_MUTATE),
        prompt=NAIVE_PROMPT_GOAL_MUTATION,
    )
    # The adversarial instruction document WOULD permit the mutation...
    assert instructions.would_permit(Action.FROZEN_GOAL_MUTATE)
    # ... yet the runtime guard still stops it: enforcement lives in the
    # permission machinery, not in the prompt.
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(instructions.role, goal)
    assessment = exc.value.assessment
    assert assessment.input.role is role
    assert assessment.input.action is Action.FROZEN_GOAL_MUTATE
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert assessment == check_action_allowed(role, Action.FROZEN_GOAL_MUTATE)
    assert goal.goal_id in str(exc.value)
    assert "R-PRM-D1" in str(exc.value)
    assert "\n" not in str(exc.value)


def test_governance_ac01_guard_protects_the_real_frozen_contract(
    tmp_path,
) -> None:
    """AC-01: the guard sits at the mutation surface of the real frozen
    Goal Contract -- the exact record the freeze flow produced and the
    worker would receive (freeze.py AC-02: the persisted artifact is the
    frozen Plan record; the frozen goal family is the freeze result's
    contract, version ``v1``, ``frozen`` True) -- not only on a hand-built
    in-memory copy."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)  # the one-shot real freeze flow
    assert goal.frozen is True
    assert goal.version == "v1"
    # The persisted artifact of the same freeze: the frozen Plan record.
    frozen_plan = read_plan(root, "v1")
    assert frozen_plan.status is PlanStatus.FROZEN
    assert "GOAL-1" in frozen_plan.goal_ids
    assert frozen_plan.version == "v1"
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.EXPERIMENT_WORKER, goal)
    assert exc.value.assessment.input.action is Action.FROZEN_GOAL_MUTATE
    assert exc.value.assessment.matched_rule_id == "R-PRM-D1"
    # The registered draft record (goals/GOAL-1.json stays a draft after
    # the freeze) is denied to workers as well
    # (03-ROLE-AND-PERMISSION-SPEC.md SS5: workers may not change Goals in
    # any state).
    draft = read_goal(root, "GOAL-1")
    assert draft.frozen is False
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.COMPUTATION_WORKER, draft)
    assert exc.value.assessment.input.action is Action.GOAL_MUTATE


@pytest.mark.parametrize("role", NON_SUPERVISOR_ROLES)
@pytest.mark.parametrize("decision_type", list(DecisionType))
def test_governance_ac01_naive_worker_cannot_make_plan_decisions(
    tmp_path, role: Role, decision_type: DecisionType
) -> None:
    """AC-01: no non-Supervisor role can make any scientific Plan decision,
    even when instructed to decide for itself."""
    action = action_for_decision_type(decision_type)
    instructions = naive_worker_instructions(
        role,
        (action,),
        prompt=NAIVE_PROMPT_PLAN_DECISION,
    )
    assert instructions.would_permit(action)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_plan_decision(instructions.role, decision_type)
    assessment = exc.value.assessment
    assert assessment.input.role is role
    assert assessment.input.action is action
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert assessment == check_action_allowed(role, action)
    assert f"plan decision {decision_type.value!r}" in str(exc.value)


def test_governance_ac01_naive_monitor_cannot_classify_pass_or_fail(
    tmp_path,
) -> None:
    """AC-01: a Monitor instructed to classify the observed run as
    scientific PASS/FAIL is still stopped by the runtime guard -- the
    verdict is a review decision, never a monitor observation
    (05-GOAL-RUN-SCHEMA.md SS7)."""
    instructions = naive_worker_instructions(
        Role.MONITOR,
        (Action.SCIENTIFIC_INTERPRETATION,),
        prompt=NAIVE_PROMPT_PASS_FAIL,
    )
    assert instructions.would_permit(Action.SCIENTIFIC_INTERPRETATION)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_scientific_interpretation(
            instructions.role, target="RUN-COMP-017-01"
        )
    assessment = exc.value.assessment
    assert assessment.input.role is Role.MONITOR
    assert assessment.input.action is Action.SCIENTIFIC_INTERPRETATION
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert "RUN-COMP-017-01" in str(exc.value)


@pytest.mark.parametrize("role", NON_SUPERVISOR_ROLES)
def test_governance_ac01_naive_worker_cannot_create_a_recovery_goal(
    tmp_path, role: Role
) -> None:
    """AC-01: no non-Supervisor role can enter Recovery by creating a
    formal Recovery Goal, even when instructed to (08-STRICT-RECOVERY-CLOSURE.md
    SS1: Recovery follows a Supervisor decision)."""
    instructions = naive_worker_instructions(
        role,
        (Action.RECOVERY_GOAL_CREATE,),
        prompt=NAIVE_PROMPT_RECOVERY,
    )
    assert instructions.would_permit(Action.RECOVERY_GOAL_CREATE)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_recovery_goal_creation(instructions.role)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.RECOVERY_GOAL_CREATE
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert "formal Recovery Goal" in str(exc.value)


@pytest.mark.parametrize("role", NON_SUPERVISOR_ROLES)
def test_governance_ac01_naive_worker_cannot_close_the_registered_requirement(
    tmp_path, role: Role
) -> None:
    """AC-01: no non-Supervisor role can close a Requirement -- even the
    real registered record -- no matter what the instructions grant.
    Closure stays Supervisor-only (03-ROLE-AND-PERMISSION-SPEC.md SS2)."""
    root = build_complete_workspace(tmp_path)
    requirement = read_requirement(root, "REQ-1")
    assert requirement.requirement_id == "REQ-1"
    instructions = naive_worker_instructions(
        role,
        (Action.REQUIREMENT_CLOSE,),
        prompt=NAIVE_PROMPT_CLOSURE,
    )
    assert instructions.would_permit(Action.REQUIREMENT_CLOSE)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_requirement_closure(instructions.role, requirement)
    assessment = exc.value.assessment
    assert assessment.input.action is Action.REQUIREMENT_CLOSE
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-PRM-D1"
    assert "REQ-1" in str(exc.value)


def test_governance_ac01_supervisor_still_holds_every_governance_surface(
    tmp_path,
) -> None:
    """AC-01 positive control: the Supervisor keeps every governance
    surface on the real frozen state -- goal mutation, Plan decisions,
    Recovery entry, Requirement closure and scientific interpretation all
    pass with the matrix's Supervisor rule."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    requirement = read_requirement(root, "REQ-1")
    mutation = enforce_goal_mutation(Role.SUPERVISOR, goal)
    assert mutation.allowed is True
    assert mutation.matched_rule_id == "R-PRM-SUP1"
    assert mutation.input.action is Action.FROZEN_GOAL_MUTATE
    for decision_type in (
        DecisionType.PLAN_FREEZE,
        DecisionType.GOAL_REVISION,
        DecisionType.RECOVERY_ENTRY,
        DecisionType.PROJECT_OUTCOME,
    ):
        decision = enforce_plan_decision(Role.SUPERVISOR, decision_type)
        assert decision.allowed is True
        assert decision.matched_rule_id == "R-PRM-SUP1"
    recovery = enforce_recovery_goal_creation(Role.SUPERVISOR)
    assert recovery.allowed is True
    closure = enforce_requirement_closure(Role.SUPERVISOR, requirement)
    assert closure.allowed is True
    assert closure.input.action is Action.REQUIREMENT_CLOSE
    interpretation = enforce_scientific_interpretation(Role.SUPERVISOR)
    assert interpretation.allowed is True
    assert interpretation.input.action is Action.SCIENTIFIC_INTERPRETATION
    # The guards return the full assessment -- the audit trail -- on the
    # allowed path.
    assert isinstance(mutation, PermissionAssessment)


def test_governance_ac01_workers_keep_exactly_their_may_lists(tmp_path) -> None:
    """AC-01 two-sided boundary: the workers keep exactly the actions the
    role-action matrix grants them (the SS5-SS8 "may" lists) and nothing
    beyond -- least privilege is enforced for the granted side as well."""
    may_lists = {
        Role.EXPERIMENT_WORKER: (
            Action.CONTEXT_READ,
            Action.EXECUTION_PACKAGE_PREPARE,
            Action.ENGINEERING_RETRY,
            Action.ARTIFACT_REGISTER,
            Action.DEVIATION_REPORT,
        ),
        Role.COMPUTATION_WORKER: (
            Action.CONTEXT_READ,
            Action.RUN_PREPARE,
            Action.FACT_REPORT,
            Action.ENGINEERING_RETRY,
        ),
        Role.ANALYSIS_WORKER: (
            Action.CONTEXT_READ,
            Action.ANALYSIS_EXECUTE,
            Action.RESULT_INGEST,
        ),
        Role.DIAGNOSIS_WORKER: (
            Action.CONTEXT_READ,
            Action.DIAGNOSIS_REPORT,
            Action.METADATA_RECORD,
        ),
    }
    for role, allowed_actions in may_lists.items():
        for action in allowed_actions:
            assert is_action_allowed(role, action) is True
            assert check_action_allowed(role, action).allowed is True


def test_governance_ac01_context_grants_only_engineering_retries_and_no_plan_actions(
    tmp_path,
) -> None:
    """AC-01/AC-02 cross-module: the context package issued to an
    Experiment Worker grants only ``retry:<failure>`` engineering actions;
    every plan-mutation surface (goal mutation, Plan decisions, scientific
    interpretation, Recovery, closure) is absent from the worker's world
    and denied by the runtime matrix."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    package = generate_context(root, goal).package
    assert package.allowed_actions == [
        "retry:instrument_drift",
        "retry:power_cycle",
    ]
    assert package.forbidden_actions == ["retry:protocol_deviation"]
    assert all(action.startswith("retry:") for action in package.allowed_actions)
    role = role_from_worker_role(WorkerRole.EXPERIMENT_WORKER)
    for action in (
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.GOAL_CREATE,
        Action.PLAN_FREEZE,
        Action.SCIENTIFIC_INTERPRETATION,
        Action.RECOVERY_GOAL_CREATE,
        Action.REQUIREMENT_CLOSE,
        Action.SCIENTIFIC_PARAMETER_CHANGE,
    ):
        assert is_action_allowed(role, action) is False


def test_governance_ac01_forbidden_retry_is_contract_prohibition_and_runtime_denial(
    tmp_path,
) -> None:
    """AC-01 full M6 chain (G01+G03+G04): the supervisor-required retry is
    an explicit prohibition of the context package (the contract), the
    retry evaluator routes it away from automatic worker action, and the
    runtime matrix denies the scientific-change boundary the routed retry
    implies -- while the whitelisted engineering retry stays authorized on
    every layer."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    policy = make_retry_policy()
    package = generate_context(root, goal, policy=policy).package
    assert package.forbidden_actions == ["retry:protocol_deviation"]
    assert package.allowed_actions == [
        "retry:instrument_drift",
        "retry:power_cycle",
    ]
    # The evaluator (G04) decides the same contract entries: the
    # supervisor-required change is REJECTED for automatic action and
    # routed to the Supervisor; the whitelisted failure is AUTHORIZED.
    routed = evaluate_automatic_retry(
        RetryEvaluationInput(policy=policy, failure_kind="protocol_deviation")
    )
    assert routed.verdict is RetryAuthorization.REJECTED
    assert routed.routing is RetryRouting.SUPERVISOR
    assert routed.matched_rule_id == "R-RET-S1"
    authorized = evaluate_automatic_retry(
        RetryEvaluationInput(policy=policy, failure_kind="instrument_drift")
    )
    assert authorized.verdict is RetryAuthorization.AUTHORIZED
    assert authorized.routing is RetryRouting.AUTOMATIC
    assert authorized.matched_rule_id == "R-RET-A1"
    # The runtime matrix (G03) agrees: the worker may execute preauthorized
    # engineering retries but may never change scientific parameters.
    role = role_from_worker_role(WorkerRole.EXPERIMENT_WORKER)
    assert is_action_allowed(role, Action.ENGINEERING_RETRY) is True
    assert is_action_allowed(role, Action.SCIENTIFIC_PARAMETER_CHANGE) is False
    assert (
        check_action_allowed(role, Action.SCIENTIFIC_PARAMETER_CHANGE).matched_rule_id
        == "R-PRM-D1"
    )


def test_governance_ac01_denied_assessment_carries_the_frozen_ruleset_version(
    tmp_path,
) -> None:
    """AC-01: every denied assessment records the ruleset version of the
    frozen role-action matrix, so the decision stays interpretable."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    with pytest.raises(PermissionDeniedError) as exc:
        enforce_goal_mutation(Role.ANALYSIS_WORKER, goal)
    assert exc.value.assessment.ruleset_version == ROLE_ACTION_RULESET_VERSION
    assert exc.value.assessment.matched_rule_id == "R-PRM-D1"


# ---------------------------------------------------------------------------
# AC-02 -- the worker context exposes exactly the goal's references; the
# leak fixture fails if unrelated refs are exposed
# ---------------------------------------------------------------------------


def test_governance_ac02_adversarial_workspace_exposes_exactly_the_goal_refs(
    tmp_path,
) -> None:
    """AC-02: the context generated for the frozen Goal in the adversarial
    workspace exposes EXACTLY the goal's own references -- its id, its
    protocol/resource/policy refs, its evidence and its required upstream
    results -- and no unrelated registered document. This is the leak
    fixture: any unrelated ref in the manifest fails the test."""
    root = build_adversarial_workspace(tmp_path)
    goal = frozen_goal(root)
    evidence = adversarial_evidence()
    result = generate_context(root, goal, evidence=evidence)
    assert result.manifest.references == EXPECTED_REFERENCES
    assert_only_goal_owned_refs_exposed(
        result, goal, root=root, evidence=evidence
    )
    package = result.package
    assert package.source_refs == ["SRC-1"]
    assert package.evidence_refs == ["EVID-1"]
    assert package.upstream_result_refs == ["GOAL-2#raw_isotherm_data"]
    assert package.protocol_refs == ["ANP-1"]
    assert package.resource_refs == ["RES-1"]
    assert package.goal_id == "GOAL-1"
    assert package.goal_version == "v1"


def test_governance_ac02_no_unrelated_registered_document_leaks(tmp_path) -> None:
    """AC-02: each unrelated registry id seeded by the adversarial fixture
    is absent from the generated context -- the failure mode of the leak
    fixture is explicit per kind."""
    root = build_adversarial_workspace(tmp_path)
    goal = frozen_goal(root)
    evidence = adversarial_evidence()
    result = generate_context(root, goal, evidence=evidence)
    refs_of_kind = {
        kind: [ref.ref_id for ref in result.manifest.references if ref.kind is kind]
        for kind in ReferenceKind
    }
    # Every unrelated registry id seeded by the adversarial fixture is
    # absent from the generated context (the failure mode of the leak
    # fixture is explicit per kind).
    for leak_id, kind in LEAK_IDS.items():
        assert leak_id not in refs_of_kind[kind], (
            f"unrelated {kind.value} {leak_id!r} leaked into the worker"
            " context"
        )
    # Evidence/sources of OTHER goals (not seeded as leak ids but present
    # in the candidate registries) stay absent as well.
    assert "EVID-2" not in refs_of_kind[ReferenceKind.EVIDENCE]
    assert "SRC-3" not in refs_of_kind[ReferenceKind.SOURCE]
    # Cross-check every exposed reference against the goal's own set.
    assert_only_goal_owned_refs_exposed(result, goal, root=root, evidence=evidence)


def test_governance_ac02_unrelated_state_does_not_change_the_exposed_context(
    tmp_path,
) -> None:
    """AC-02: adding unrelated candidates -- evidence of other goals,
    unrelated sources, unrelated registered documents -- does not move the
    exposed reference set or the context hash: the context is a pure
    function of the goal's own references (the minimal-necessary rule)."""
    root = build_adversarial_workspace(tmp_path)
    goal = frozen_goal(root)
    only_relevant = generate_goal_context(
        root,
        goal,
        worker_role=ROLE,
        evidence_registry=EvidenceRegistry.from_records(
            [make_evidence("EVID-1", "SRC-1", used_by=("GOAL-1",))]
        ),
        sources={"SRC-1": make_source("SRC-1")},
        retry_policy=make_retry_policy(),
    )
    with_extras = generate_context(root, goal)
    assert with_extras.manifest == only_relevant.manifest
    assert with_extras.package.context_hash == only_relevant.package.context_hash
    assert with_extras.manifest.references == EXPECTED_REFERENCES


def test_governance_ac02_naive_caller_cannot_swap_an_unrelated_policy(
    tmp_path,
) -> None:
    """AC-02: an unrelated retry policy id is never silently substituted
    into the context -- the naive caller handing the wrong record is
    refused loudly (PolicyMismatchError)."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    with pytest.raises(PolicyMismatchError) as exc:
        generate_context(root, goal, policy=make_retry_policy("RETRY-LEAK-9"))
    assert "RETRY-LEAK-9" in str(exc.value)
    assert "RETRY-ENGINEERING-DEFAULT" in str(exc.value)


def test_governance_ac02_draft_goal_cannot_produce_a_worker_context(tmp_path) -> None:
    """AC-02 boundary: a drifting (unfrozen) contract cannot produce a
    worker context at all -- the generator refuses it loudly, so a worker
    can never be exposed to a non-frozen Goal (GoalNotFrozenError)."""
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    draft = replace(goal, frozen=False, version="v1-draft")
    with pytest.raises(GoalNotFrozenError) as exc:
        generate_goal_context(root, draft, worker_role=ROLE)
    assert "GOAL-1" in str(exc.value)
    assert "frozen" in str(exc.value)


def test_governance_ac02_isolation_holds_for_every_worker_role(tmp_path) -> None:
    """AC-02: the reference set is minimized identically for every worker
    role -- isolation is role-independent -- while the context identity is
    role-scoped (one context per role)."""
    root = build_adversarial_workspace(tmp_path)
    goal = frozen_goal(root)
    evidence = adversarial_evidence()
    for worker_role in WorkerRole:
        result = generate_context(
            root, goal, worker_role=worker_role, evidence=evidence
        )
        assert result.package.worker_role is worker_role
        assert result.manifest.worker_role is worker_role
        assert result.manifest.references == EXPECTED_REFERENCES
        assert_only_goal_owned_refs_exposed(
            result, goal, root=root, evidence=evidence
        )
    # One context per role: the identity is role-scoped, the minimization
    # is not.
    context_ids = {
        generate_context(root, goal, worker_role=worker_role).package.context_id
        for worker_role in WorkerRole
    }
    assert len(context_ids) == len(WorkerRole)


def test_governance_ac02_context_hash_is_deterministic_fingerprint(tmp_path) -> None:
    """AC-02: the exposed set is fingerprinted deterministically -- the
    same adversarial workspace yields the same manifest and hash, and the
    hash changes when (and only when) the exposed reference set changes."""
    root = build_adversarial_workspace(tmp_path)
    goal = frozen_goal(root)
    first = generate_context(root, goal)
    second = generate_context(root, goal)
    assert first.manifest == second.manifest
    assert first.package.context_hash == second.package.context_hash
    assert len(first.package.context_hash) == 64
    # A relevant evidence record changes the exposed set -> the hash moves;
    # the manifest records the change.
    extra_evidence = EvidenceRegistry.from_records(
        [
            make_evidence("EVID-1", "SRC-1", used_by=("GOAL-1",)),
            make_evidence("EVID-2", "SRC-2", used_by=("GOAL-2",)),
            make_evidence("EVID-LEAK-9", "SRC-LEAK-9", used_by=("GOAL-UNRELATED",)),
            make_evidence("EVID-3", "SRC-3", used_by=("GOAL-1",)),
        ]
    )
    changed = generate_context(root, goal, evidence=extra_evidence)
    assert changed.package.evidence_refs == ["EVID-1", "EVID-3"]
    assert changed.package.context_hash != first.package.context_hash
    assert changed.manifest.references != first.manifest.references
