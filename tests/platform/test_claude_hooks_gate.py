"""Claude Code quality-gate hook templates -- the deterministic gate (DEV-M10-G04).

AC-01 -- task completion can be blocked on failing deterministic
verification: :func:`evaluate_gate` runs the frozen goal contract's
verification list (passed as plain data in :class:`VerificationSpec`)
through the injectable :class:`Verifier` boundary and answers the
mechanical :class:`GateVerdict` -- PASS (completion allowed) or BLOCK
(completion prevented, failing verification recorded) -- with no wall
clock, no randomness and no I/O: the same durable task context always
yields the same gate record.

AC-02 -- a failing gate feeds actionable feedback rather than marking
the task complete: the wiring (:func:`handle_hook_event`) delivers the
typed, frozen :class:`FeedbackRecord` (which verification failed, what
the verifier reported, what the task must fix) into the durable session
outbox -- a pending command of the M10-G03 :class:`SessionRegistry`
semantics -- and never fabricates a completion.

AC-03 -- the hooks do not replace Supervisor review: the gate verdict
is mechanical (it only gates the task-completion signal); the hook
layer has no surface declaring PASS/FAIL acceptance of the overall goal
(the Supervisor alone accepts, the frozen ``VerdictAuthority``
vocabulary).

The suite is pure: scripted in-memory fakes at the boundary (precedent:
the M10-G03 ``ScriptedTeamStore`` / ``ScriptedProcessRunner`` fakes),
no wall clock, no randomness, no file I/O.
"""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError

import pytest

import scientific_reproduction.adapters.platform.claude_code.hooks as hooks_pkg
from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    SessionNotFoundError,
    WorkerSessionHandle,
)
from scientific_reproduction.adapters.platform.claude_code import (
    CLAUDE_CODE_PLATFORM_ID,
    SessionRegistry,
    SessionState,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.gate import (
    REQUIRED_FIX,
    VERIFIER_UNAVAILABLE_REPORT,
    FeedbackRecord,
    GateRecord,
    GateVerdict,
    UnavailableVerifier,
    VerificationOutcome,
    VerificationSpec,
    Verifier,
    evaluate_gate,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.hook_events import (
    HookAction,
    HookDecision,
    HookEvent,
    HookEventType,
    handle_hook_event,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id

PROJECT_ID = generate_id("project", "g04")
GOAL_ID = generate_id("goal", PROJECT_ID, "g1")

#: The goal contract verification list of this goal, as data (the
#: ``verification:`` commands of a DEV-GOAL YAML).
GOAL_VERIFICATION = ("python -m pytest -q tests/platform -k hook",)


def make_handle() -> WorkerSessionHandle:
    return WorkerSessionHandle(
        platform_id=CLAUDE_CODE_PLATFORM_ID,
        worker_id="experiment_worker",
        role_id="worker",
        project_id=PROJECT_ID,
        goal_id=GOAL_ID,
        context_id=generate_id("worker-context", PROJECT_ID, GOAL_ID, "r1"),
    )


def make_registry() -> SessionRegistry:
    registry = SessionRegistry()
    registry.put(make_handle())
    return registry


def make_spec(
    *,
    session_ref: str | None = None,
    goal_id: str | None = GOAL_ID,
    commands: tuple[str, ...] = GOAL_VERIFICATION,
) -> VerificationSpec:
    return VerificationSpec(
        session_ref=session_ref or make_handle().session_ref,
        goal_id=goal_id,
        commands=commands,
    )


def make_event(
    *,
    event_type: HookEventType = HookEventType.TASK_COMPLETED,
    session_ref: str | None = None,
) -> HookEvent:
    handle = make_handle()
    return HookEvent(
        event_type=event_type,
        task_id=generate_id("team_task", session_ref or handle.session_ref),
        session_ref=session_ref or handle.session_ref,
        goal_id=GOAL_ID,
    )


class ScriptedVerifier(Verifier):
    """The deterministic in-suite verification fake.

    ``results`` -- command -> passed; ``checks`` -- the recorded check
    calls in order, so tests can pin exactly what the gate asked.
    """

    def __init__(self, results: dict[str, bool] | None = None) -> None:
        self.results = dict(results or {})
        self.checks: list[str] = []

    def check(self, command: str) -> VerificationOutcome:
        self.checks.append(command)
        passed = self.results.get(command, True)
        report = "verification passed" if passed else "verification failed"
        return VerificationOutcome(command=command, passed=passed, report=report)


# ---------------------------------------------------------------------------
# The typed records (frozen, validated, canonical)
# ---------------------------------------------------------------------------


def test_hook_gate_records_are_frozen_typed_and_validated():
    # The gate records are frozen, validating dataclasses: no silent
    # mutation, no malformed verdicts/outcomes/specs/events.
    assert set(GateVerdict.__members__) == {"PASS", "BLOCK"}
    spec = make_spec()
    assert spec.session_ref == make_handle().session_ref
    assert spec.commands == GOAL_VERIFICATION
    with pytest.raises(FrozenInstanceError):
        spec.commands = ("other",)  # type: ignore[misc]
    with pytest.raises(TypeError):
        VerificationSpec(session_ref=123, commands=())  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        VerificationSpec(session_ref="not-a-session-ref", commands=())
    with pytest.raises(PlatformAdapterDataError):
        make_spec(commands=("",))
    with pytest.raises(TypeError):
        make_spec(commands="not-a-tuple")  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        VerificationOutcome(command="x", passed="yes", report="r")  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        VerificationOutcome(command="", passed=True, report="r")

    with pytest.raises(PlatformAdapterDataError):
        FeedbackRecord(
            session_ref="not-a-session-ref",
            goal_id=None,
            failed_command="x",
            verifier_report="r",
            required_fix=REQUIRED_FIX,
        )
    with pytest.raises(PlatformAdapterDataError):
        FeedbackRecord(
            session_ref=spec.session_ref,
            goal_id=None,
            failed_command="",
            verifier_report="r",
            required_fix=REQUIRED_FIX,
        )
    with pytest.raises(PlatformAdapterDataError):
        FeedbackRecord(
            session_ref=spec.session_ref,
            goal_id=None,
            failed_command="x",
            verifier_report="r",
            required_fix=REQUIRED_FIX,
            feedback_id="not-computed",  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError):
        GateRecord(
            session_ref=spec.session_ref,
            goal_id=GOAL_ID,
            verdict="pass",  # type: ignore[arg-type]
            outcomes=(),
        )
    with pytest.raises(PlatformAdapterDataError):
        # a BLOCK gate must carry its feedback
        GateRecord(
            session_ref=spec.session_ref,
            goal_id=GOAL_ID,
            verdict=GateVerdict.BLOCK,
            outcomes=(),
        )
    with pytest.raises(PlatformAdapterDataError):
        # a PASS gate never carries feedback
        GateRecord(
            session_ref=spec.session_ref,
            goal_id=GOAL_ID,
            verdict=GateVerdict.PASS,
            outcomes=(),
            feedback=FeedbackRecord(
                session_ref=spec.session_ref,
                goal_id=GOAL_ID,
                failed_command="x",
                verifier_report="r",
                required_fix=REQUIRED_FIX,
            ),
        )

    event = make_event()
    assert event.event_type is HookEventType.TASK_COMPLETED
    with pytest.raises(FrozenInstanceError):
        event.session_ref = "other"  # type: ignore[misc]
    with pytest.raises(PlatformAdapterDataError):
        HookEvent(
            event_type=HookEventType.TASK_COMPLETED,
            task_id="not-a-task-id",
            session_ref=spec.session_ref,
        )


def test_hook_gate_records_round_trip_losslessly():
    # The gate records serialize canonically and round-trip losslessly;
    # corrupt serialized state is a stable PlatformAdapterDataError.
    spec = make_spec()
    assert VerificationSpec.from_dict(spec.to_dict()) == spec
    empty_spec = VerificationSpec(session_ref=spec.session_ref)
    assert VerificationSpec.from_dict(empty_spec.to_dict()) == empty_spec

    verifier = ScriptedVerifier(
        {"python -m pytest -q tests/platform -k hook": False}
    )
    gate = evaluate_gate(spec, verifier)
    restored = GateRecord.from_dict(gate.to_dict())
    assert restored == gate
    assert restored.feedback is not None
    assert FeedbackRecord.from_dict(restored.feedback.to_dict()) == restored.feedback

    passing = evaluate_gate(make_spec(), ScriptedVerifier())
    assert GateRecord.from_dict(passing.to_dict()) == passing

    event = make_event()
    assert HookEvent.from_dict(event.to_dict()) == event
    action = HookAction(decision=HookDecision.BLOCK, gate=gate)
    assert HookAction.from_dict(action.to_dict()) == action

    with pytest.raises(PlatformAdapterDataError):
        GateRecord.from_dict({"session_ref": spec.session_ref})
    with pytest.raises(PlatformAdapterDataError):
        VerificationSpec.from_dict({"goal_id": GOAL_ID})
    with pytest.raises(PlatformAdapterDataError):
        HookEvent.from_dict({"event_type": "TaskCompleted"})
    with pytest.raises(PlatformAdapterDataError):
        FeedbackRecord.from_dict({"session_ref": spec.session_ref})


# ---------------------------------------------------------------------------
# AC-01 -- completion can be blocked on failing deterministic verification
# ---------------------------------------------------------------------------


def test_hook_ac01_passing_verification_allows_task_completion():
    # Every verification command passes: the gate is PASS, the
    # completion signal may proceed, no feedback is produced.
    verifier = ScriptedVerifier()
    spec = make_spec(commands=("pytest a", "pytest b"))
    gate = evaluate_gate(spec, verifier)

    assert gate.verdict is GateVerdict.PASS
    assert gate.session_ref == spec.session_ref
    assert gate.goal_id == GOAL_ID
    assert gate.feedback is None
    assert [outcome.command for outcome in gate.outcomes] == ["pytest a", "pytest b"]
    assert all(outcome.passed for outcome in gate.outcomes)
    assert verifier.checks == ["pytest a", "pytest b"]  # checked in order

    registry = make_registry()
    action = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    assert action.decision is HookDecision.PASS
    assert action.gate == gate
    assert registry.pending_commands(spec.session_ref) == ()


def test_hook_ac01_failing_verification_blocks_task_completion():
    # A failing verification command blocks the completion signal: the
    # gate is BLOCK, the failing verification is recorded, and the
    # wiring answers the mechanical block -- completion prevented.
    verifier = ScriptedVerifier({"pytest a": True, "pytest b": False})
    spec = make_spec(commands=("pytest a", "pytest b"))
    gate = evaluate_gate(spec, verifier)

    assert gate.verdict is GateVerdict.BLOCK
    assert gate.feedback is not None
    assert [outcome.command for outcome in gate.outcomes] == ["pytest a", "pytest b"]
    assert gate.outcomes[0].passed and not gate.outcomes[1].passed
    assert gate.feedback.failed_command == "pytest b"
    assert gate.feedback.verifier_report == "verification failed"

    registry = make_registry()
    action = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    assert action.decision is HookDecision.BLOCK
    assert action.gate == gate
    # the blocked completion delivered exactly the feedback directive
    # into the durable session outbox
    assert registry.pending_commands(spec.session_ref) == (
        gate.feedback.directive,
    )


def test_hook_ac01_first_failing_verification_names_the_feedback():
    # Multiple failures: every outcome is recorded; the single feedback
    # record names the first failing verification in command order
    # (deterministic).
    verifier = ScriptedVerifier({"pytest a": False, "pytest c": False})
    spec = make_spec(commands=("pytest a", "pytest b", "pytest c"))
    gate = evaluate_gate(spec, verifier)

    assert gate.verdict is GateVerdict.BLOCK
    assert not gate.outcomes[0].passed
    assert gate.outcomes[1].passed
    assert not gate.outcomes[2].passed
    assert gate.feedback is not None
    assert gate.feedback.failed_command == "pytest a"


def test_hook_ac01_gate_is_a_pure_function_of_durable_context():
    # The gate is a pure function of the durable task context and the
    # injected verifier: the same inputs always yield the same records,
    # and no wall clock/randomness/I/O enters the evaluation.
    spec = make_spec()
    verifier = ScriptedVerifier({"python -m pytest -q tests/platform -k hook": False})
    first = evaluate_gate(spec, verifier)
    second = evaluate_gate(spec, verifier)
    assert first == second
    assert verifier.checks == list(GOAL_VERIFICATION) * 2

    registry = make_registry()
    action_a = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    action_b = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    assert action_a == action_b


def test_hook_ac01_teammate_idle_event_is_gated_the_same():
    # The TeammateIdle event runs the same deterministic gate: a task
    # whose verification fails must neither complete nor idle as done.
    spec = make_spec()
    failing = ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    idle_action = handle_hook_event(
        make_event(event_type=HookEventType.TEAMMATE_IDLE),
        spec=spec,
        verifier=failing,
        registry=make_registry(),
    )
    assert idle_action.decision is HookDecision.BLOCK

    completed_action = handle_hook_event(
        make_event(event_type=HookEventType.TASK_COMPLETED),
        spec=spec,
        verifier=failing,
        registry=make_registry(),
    )
    assert completed_action.decision is HookDecision.BLOCK
    assert idle_action == completed_action


def test_hook_ac01_empty_verification_list_passes_vacuously():
    # A goal contract with an empty verification list passes vacuously:
    # the gate is mechanical and invents no requirement.
    spec = make_spec(commands=())
    gate = evaluate_gate(spec, ScriptedVerifier())
    assert gate.verdict is GateVerdict.PASS
    assert gate.outcomes == ()
    assert gate.feedback is None


def test_hook_ac01_unavailable_verifier_blocks_never_passes():
    # The real thin wrapper refuses every check with the typed report:
    # a verification that cannot be executed is never a pass -- the gate
    # blocks (no fabricated completion).
    verifier = UnavailableVerifier()
    outcome = verifier.check(GOAL_VERIFICATION[0])
    assert not outcome.passed
    assert outcome.report == VERIFIER_UNAVAILABLE_REPORT

    gate = evaluate_gate(make_spec(), verifier)
    assert gate.verdict is GateVerdict.BLOCK
    assert gate.feedback is not None
    assert gate.feedback.verifier_report == VERIFIER_UNAVAILABLE_REPORT

    registry = make_registry()
    action = handle_hook_event(
        make_event(), spec=make_spec(), verifier=verifier, registry=registry
    )
    assert action.decision is HookDecision.BLOCK
    assert len(registry.pending_commands(make_spec().session_ref)) == 1


def test_hook_ac01_mismatched_event_identity_is_corrupt_payload():
    # The event's durable identity must match the verification spec's:
    # a mismatch is a corrupt hook payload (stable data error), never a
    # silent gate on the wrong session.
    other = WorkerSessionHandle(
        platform_id=CLAUDE_CODE_PLATFORM_ID,
        worker_id="alice",
        role_id="worker",
        project_id=PROJECT_ID,
        goal_id=GOAL_ID,
        context_id=generate_id("worker-context", PROJECT_ID, GOAL_ID, "r2"),
    )
    registry = make_registry()
    registry.put(other)
    event = make_event(session_ref=other.session_ref)
    with pytest.raises(PlatformAdapterDataError):
        handle_hook_event(
            event, spec=make_spec(), verifier=ScriptedVerifier(), registry=registry
        )


def test_hook_ac01_unknown_session_is_a_data_error():
    # A completion signal for a session the durable registry does not
    # know is a broken reference (SessionNotFoundError, the DEV-M10-G02
    # discipline) -- feedback cannot be delivered into an unknown
    # session's outbox.
    unknown = generate_id(
        "session", CLAUDE_CODE_PLATFORM_ID, "ghost", "worker", PROJECT_ID
    )
    with pytest.raises(SessionNotFoundError):
        handle_hook_event(
            make_event(session_ref=unknown),
            spec=make_spec(session_ref=unknown),
            verifier=ScriptedVerifier(),
            registry=make_registry(),
        )


# ---------------------------------------------------------------------------
# AC-02 -- hook failure feeds actionable feedback, never a completion
# ---------------------------------------------------------------------------


def test_hook_ac02_feedback_record_is_actionable_and_typed():
    # The feedback record is a typed, frozen record carrying exactly the
    # actionable pieces: which verification failed, what the verifier
    # reported, what the task must fix -- plus the canonical durable
    # outbox directive rendered from them.
    spec = make_spec()
    verifier = ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    gate = evaluate_gate(spec, verifier)
    feedback = gate.feedback
    assert isinstance(feedback, FeedbackRecord)
    assert feedback is not None
    assert feedback.session_ref == spec.session_ref
    assert feedback.goal_id == GOAL_ID
    assert feedback.failed_command == GOAL_VERIFICATION[0]
    assert feedback.verifier_report == "verification failed"
    assert feedback.required_fix == REQUIRED_FIX
    assert is_valid_id(feedback.feedback_id, kind="gate_feedback")
    assert feedback.feedback_id == generate_id(
        "gate_feedback", spec.session_ref, GOAL_VERIFICATION[0]
    )
    # the durable-outbox directive names the session, the failing
    # verification, the verifier report and the required fix
    assert spec.session_ref in feedback.directive
    assert GOAL_VERIFICATION[0] in feedback.directive
    assert "verification failed" in feedback.directive
    assert REQUIRED_FIX in feedback.directive
    # frozen: no silent mutation of the feedback
    with pytest.raises(FrozenInstanceError):
        feedback.verifier_report = "changed"  # type: ignore[misc]


def test_hook_ac02_blocked_gate_delivers_feedback_to_the_durable_outbox():
    # The failing gate's feedback is delivered back into the durable
    # session context: a pending command of the session record, the
    # M10-G03 SessionRegistry outbox semantics -- preserved for the
    # replacement session, not lost in the hook runtime.
    spec = make_spec()
    registry = make_registry()
    verifier = ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    action = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )

    assert action.decision is HookDecision.BLOCK
    feedback = action.gate.feedback
    assert feedback is not None
    record = registry.get(spec.session_ref)
    assert record is not None
    assert record.pending_commands == (feedback.directive,)
    assert registry.pending_commands(spec.session_ref) == (feedback.directive,)


def test_hook_ac02_failure_never_fabricates_a_completion():
    # A failing gate never marks the task complete: the hook layer has
    # no completion side effect at all -- the session record's durable
    # state stays ACTIVE and the only changed field is the outbox.
    spec = make_spec()
    registry = make_registry()
    before = registry.get(spec.session_ref)
    assert before is not None
    assert before.state is SessionState.ACTIVE
    assert before.pending_commands == ()

    verifier = ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    action = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    assert action.decision is HookDecision.BLOCK

    after = registry.get(spec.session_ref)
    assert after is not None
    assert after.state is SessionState.ACTIVE  # never terminated/completed
    assert after.handle == before.handle
    assert after.pending_commands == (action.gate.feedback.directive,)
    # the only durable difference is the appended feedback directive
    assert after.to_dict() == {
        "handle": before.handle.to_dict(),
        "state": "active",
        "pending_commands": list(after.pending_commands),
    }
    # the session lifecycle has no completion state the hook could use
    assert {state.value for state in SessionState} == {"active", "terminated"}


def test_hook_ac02_feedback_delivery_appends_to_the_outbox():
    # Repeated blocked gates append to the durable outbox in order
    # (append-only, the SessionRegistry semantics) -- every failing
    # completion is answered with its feedback, never a completion.
    spec = make_spec()
    registry = make_registry()
    verifier = ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    first = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    second = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=registry
    )
    assert first.decision is HookDecision.BLOCK
    assert second.decision is HookDecision.BLOCK
    assert first.gate == second.gate
    directive = first.gate.feedback.directive
    assert registry.pending_commands(spec.session_ref) == (directive, directive)


def test_hook_ac02_passing_gate_delivers_no_feedback():
    # A passing gate delivers nothing into the durable outbox: no
    # pending command, no feedback record.
    spec = make_spec()
    registry = make_registry()
    action = handle_hook_event(
        make_event(), spec=spec, verifier=ScriptedVerifier(), registry=registry
    )
    assert action.decision is HookDecision.PASS
    assert action.gate.feedback is None
    assert registry.pending_commands(spec.session_ref) == ()


# ---------------------------------------------------------------------------
# AC-03 -- the hooks never replace Supervisor review
# ---------------------------------------------------------------------------


def test_hook_ac03_gate_verdict_is_mechanical_not_scientific():
    # The hook layer's only verdict vocabulary is the mechanical gate on
    # the task-completion signal: GateRecord carries no goal-outcome or
    # acceptance field, and HookAction carries only the mechanical
    # decision plus the gate record.
    assert {state.value for state in GateVerdict} == {"pass", "block"}
    assert {state.value for state in HookDecision} == {"pass", "block"}
    gate_fields = {field.name for field in dataclasses.fields(GateRecord)}
    assert gate_fields == {"session_ref", "goal_id", "verdict", "outcomes",
                           "feedback"}
    action_fields = {field.name for field in dataclasses.fields(HookAction)}
    assert action_fields == {"decision", "gate"}
    # no field of either record can carry an acceptance or a scientific
    # goal outcome ("outcomes" are the mechanical verification facts)
    forbidden_names = {
        "accepted",
        "acceptance",
        "outcome",
        "goal_outcome",
        "goal_review",
        "verdict_authority",
    }
    for fields in (gate_fields, action_fields):
        assert forbidden_names.isdisjoint(fields)


def test_hook_ac03_no_acceptance_surface_in_the_hook_layer():
    # AC-03 (mirror of the frozen VerdictAuthority discipline): the hook
    # layer can block or pass the mechanical completion signal and
    # produce feedback, but no export of the hooks package declares
    # acceptance or a scientific verdict of the overall goal -- the
    # Supervisor alone accepts work.
    forbidden = ("accept", "acceptance", "goal_review", "goal_outcome",
                 "verdict_authority")
    for name in hooks_pkg.__all__:
        assert not any(token in name.lower() for token in forbidden), (
            f"hook layer export {name!r} suggests an acceptance surface"
        )
    # the recommendation carries no acceptance channel: a BLOCK action
    # is a gate record plus the mechanical decision, nothing more
    spec = make_spec()
    verifier = ScriptedVerifier({GOAL_VERIFICATION[0]: False})
    action = handle_hook_event(
        make_event(), spec=spec, verifier=verifier, registry=make_registry()
    )
    assert action.decision is HookDecision.BLOCK
    assert isinstance(action.gate, GateRecord)
