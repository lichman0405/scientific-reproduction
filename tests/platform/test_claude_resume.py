"""Claude Code adapter -- resume/replacement semantics (DEV-M10-G03 AC-02/AC-03).

AC-02 -- the Agent Team task store is never authoritative for identity:
the durable session identity is the Core's ``WorkerSessionHandle.session_ref``,
reflected workspace-side in the :class:`SessionRegistry` and rehydratable
from a durable snapshot (the shared-workspace reconstruction of
13-EXECUTION-MONITOR.md SS3-SS4). These tests pin that a replaced
session carries the same ``session_ref``, that a missing task-store
record never nullifies or rewrites the Core's identity, and that a
stray task record never creates an identity.

AC-03 -- the known Agent Teams resume limitation is explicitly
reconciled: ``resume_session`` answers the typed FALLBACK with a
``fallback_reason`` naming the limitation when the task store cannot
resume; ``is_session_alive`` never fabricates liveness (UNSUPPORTED with
``alive=None`` when unobservable, NATIVE only with an observed boolean);
the explicit replacement path (``replace_session``) and
``fallback_subagent`` create a replacement carrying the same canonical
``session_ref`` and reconstructing from the shared workspace.

The suite is pure: scripted in-memory fakes at the boundary (the
``ScriptedTeamStore``/``ScriptedProcessRunner`` of test_claude_adapter),
no wall clock, no randomness, no file I/O -- the durable snapshot is a
plain records tuple replayed into a fresh registry.
"""

from __future__ import annotations

import pytest

from scientific_reproduction.adapters.platform.base import (
    CommandSpec,
    FallbackMode,
    PlatformAdapterDataError,
    SessionNotFoundError,
    WorkerSessionHandle,
)
from scientific_reproduction.adapters.platform.claude_code import (
    ALIVE_STORE_UNAVAILABLE_REASON,
    ALIVE_UNOBSERVABLE_REASON,
    COMMAND_OUTBOX_REASON,
    REPLACE_STORE_UNAVAILABLE_REASON,
    RESUME_LIMITATION_REASON,
    RESUME_LIMITATION_RECORD_MISSING_REASON,
    RESUME_STORE_UNAVAILABLE_REASON,
    TERMINATE_FALLBACK_REASON,
    TERMINATE_STORE_UNAVAILABLE_REASON,
    TERMINATED_ALREADY_REASON,
    TERMINATED_COMMAND_REASON,
    TERMINATED_REPLACE_REASON,
    TERMINATED_RESUME_REASON,
    ClaudeCodePlatformAdapter,
    ProcessRunner,
    SessionRegistry,
    SessionState,
    SubprocessSpawn,
    TaskStoreProbe,
    TeamStoreUnavailableError,
    derive_spawn_id,
    derive_task_id,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    GoalExecutionContextPackage,
    WorkerRole,
)

PROJECT_ID = generate_id("project", "g03")
GOAL_ID = generate_id("goal", PROJECT_ID, "g1")


def make_context(run: str = "r1") -> GoalExecutionContextPackage:
    return GoalExecutionContextPackage(
        context_id=generate_id("worker-context", PROJECT_ID, GOAL_ID, run),
        worker_role=WorkerRole.EXPERIMENT_WORKER,
        goal_id=GOAL_ID,
        goal_version="1.0",
        allowed_actions=["prepare"],
        forbidden_actions=["mutate"],
    )


class ScriptedTeamStore:
    """The scripted agent-team task-store fake (same as test_claude_adapter)."""

    def __init__(self) -> None:
        self.submissions: list[str] = []
        self.deliveries: list[tuple[str, str]] = []
        self.records: set[str] = set()
        self.live: set[str] = set()
        self.unavailable: bool = False

    def _refusal(self) -> TeamStoreUnavailableError:
        return TeamStoreUnavailableError(
            "the claude agent sdk is not importable in this runtime; agent"
            " teams task-store operations are unavailable"
        )

    def submit_task(self, task_id: str, bundle: dict[str, object]) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.submissions.append(task_id)
        self.records.add(task_id)
        return True

    def probe(self, task_id: str) -> TaskStoreProbe:
        if self.unavailable:
            raise self._refusal()
        return TaskStoreProbe(
            task_record_present=task_id in self.records,
            live_session_attached=task_id in self.live,
        )

    def stop_task(self, task_id: str) -> bool:
        if self.unavailable:
            raise self._refusal()
        if task_id in self.live:
            self.live.discard(task_id)
            return True
        return False

    def deliver(self, task_id: str, directive: str) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.deliveries.append((task_id, directive))
        return task_id in self.live


class AcceptingRunner(ProcessRunner):
    """A subagent channel that accepts every spawn (the SS5 fallback)."""

    def spawn_subagent(self, session_ref: str, prompt: str) -> SubprocessSpawn:
        return SubprocessSpawn(spawn_id=derive_spawn_id(session_ref), accepted=True)


def make_adapter(
    *, store: ScriptedTeamStore | None = None, registry: SessionRegistry | None = None
) -> ClaudeCodePlatformAdapter:
    return ClaudeCodePlatformAdapter(
        team_store=store or ScriptedTeamStore(),
        process_runner=None,
        registry=registry or SessionRegistry(),
    )


def spawn_monitor(adapter: ClaudeCodePlatformAdapter) -> WorkerSessionHandle:
    result = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert isinstance(result.handle, WorkerSessionHandle)
    return result.handle


def spawn_worker(adapter: ClaudeCodePlatformAdapter) -> WorkerSessionHandle:
    result = adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    assert isinstance(result.handle, WorkerSessionHandle)
    return result.handle


# ---------------------------------------------------------------------------
# AC-03 -- the resume limitation is explicitly reconciled
# ---------------------------------------------------------------------------


def test_claude_ac03_resume_returns_fallback_naming_the_limitation():
    # The task record exists (the task list is file-based and survives)
    # but no live in-process session is attached: resume must answer the
    # typed FALLBACK with a reason naming the known Agent Teams
    # limitation -- the durable identity is still returned, never
    # nullified (AC-02).
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_monitor(adapter)
    assert store.records == {derive_task_id(handle.session_ref)}

    result = adapter.resume_session(handle.session_ref)
    assert result.mode is FallbackMode.FALLBACK
    assert result.fallback_reason == RESUME_LIMITATION_REASON
    assert "not resumable" in RESUME_LIMITATION_REASON
    assert result.handle == handle  # the same durable identity


def test_claude_ac03_resume_is_native_when_a_live_session_is_attached():
    # Within the same runtime a live in-process session can be resumed
    # natively (the task store CAN resume it); the handle is the same
    # durable identity.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    store.live.add(derive_task_id(handle.session_ref))

    result = adapter.resume_session(handle.session_ref)
    assert result.mode is FallbackMode.NATIVE
    assert result.fallback_reason is None
    assert result.handle == handle


def test_claude_ac03_resume_with_unavailable_store_preserves_identity():
    # The task store cannot even be probed: the resume cannot be
    # performed natively, answered explicitly as FALLBACK with the
    # durable identity preserved (AC-03: never silent, never lost).
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    store.unavailable = True
    store.live.clear()

    result = adapter.resume_session(handle.session_ref)
    assert result.mode is FallbackMode.FALLBACK
    assert result.fallback_reason == RESUME_STORE_UNAVAILABLE_REASON
    assert result.handle == handle


def test_claude_ac03_is_session_alive_never_fabricates_liveness():
    # Liveness is only ever NATIVE with an observed boolean: True when a
    # live in-process session is attached, False exactly for a durably
    # terminated session. Every unobservable state is the explicit
    # UNSUPPORTED with alive=None -- never a guess (AC-03).
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)

    store.live.add(derive_task_id(handle.session_ref))
    observed = adapter.is_session_alive(handle.session_ref)
    assert observed.mode is FallbackMode.NATIVE
    assert observed.alive is True
    assert observed.fallback_reason is None

    store.live.clear()
    unobservable = adapter.is_session_alive(handle.session_ref)
    assert unobservable.mode is FallbackMode.UNSUPPORTED
    assert unobservable.alive is None
    assert unobservable.fallback_reason == ALIVE_UNOBSERVABLE_REASON

    store.unavailable = True
    refused = adapter.is_session_alive(handle.session_ref)
    assert refused.mode is FallbackMode.UNSUPPORTED
    assert refused.alive is None
    assert refused.fallback_reason == ALIVE_STORE_UNAVAILABLE_REASON

    # the durable terminated state is the one case where alive=False is
    # a durable fact, not a guess
    store.unavailable = False
    adapter.terminate_session(handle.session_ref)
    terminated = adapter.is_session_alive(handle.session_ref)
    assert terminated.mode is FallbackMode.NATIVE
    assert terminated.alive is False


def test_claude_ac03_terminated_session_is_never_resumed_or_replaced():
    # A terminated session is a durable fact: resume and the explicit
    # replacement path refuse with the typed UNSUPPORTED, liveness is
    # the durable False, commands are refused, and a second terminate is
    # the idempotent FALLBACK.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)

    stop = adapter.terminate_session(handle.session_ref)
    assert stop.mode is FallbackMode.FALLBACK
    assert stop.fallback_reason == TERMINATE_FALLBACK_REASON

    resume = adapter.resume_session(handle.session_ref)
    assert resume.mode is FallbackMode.UNSUPPORTED
    assert resume.handle is None
    assert resume.fallback_reason == TERMINATED_RESUME_REASON

    replace = adapter.replace_session(handle.session_ref)
    assert replace.mode is FallbackMode.UNSUPPORTED
    assert replace.handle is None
    assert replace.fallback_reason == TERMINATED_REPLACE_REASON

    message = adapter.expose_command(
        CommandSpec(session_ref=handle.session_ref, directive="go")
    )
    assert message.mode is FallbackMode.UNSUPPORTED
    assert message.fallback_reason == TERMINATED_COMMAND_REASON

    again = adapter.terminate_session(handle.session_ref)
    assert again.mode is FallbackMode.FALLBACK
    assert again.fallback_reason == TERMINATED_ALREADY_REASON

    # the durable record reflects the termination
    record = adapter._registry.get(handle.session_ref)
    assert record is not None and record.state is SessionState.TERMINATED


def test_claude_ac03_terminate_is_native_when_a_live_session_is_stopped():
    # terminate of a live in-process session is the NATIVE answer; the
    # stop is recorded at the boundary and the durable state flips to
    # terminated.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    store.live.add(derive_task_id(handle.session_ref))

    result = adapter.terminate_session(handle.session_ref)
    assert result.mode is FallbackMode.NATIVE
    assert result.fallback_reason is None
    assert derive_task_id(handle.session_ref) not in store.live
    assert adapter._registry.is_terminated(handle.session_ref)

    # store unavailable: the termination is still recorded durably
    other = ScriptedTeamStore()
    adapter2 = make_adapter(store=other)
    handle2 = spawn_worker(adapter2)
    other.unavailable = True
    fallback = adapter2.terminate_session(handle2.session_ref)
    assert fallback.mode is FallbackMode.FALLBACK
    assert fallback.fallback_reason == TERMINATE_STORE_UNAVAILABLE_REASON
    assert adapter2._registry.is_terminated(handle2.session_ref)


# ---------------------------------------------------------------------------
# AC-02 -- the task store is never authoritative for identity
# ---------------------------------------------------------------------------


def test_claude_ac02_replaced_session_carries_the_same_session_ref():
    # The explicit replacement path re-creates the transport task but
    # the durable identity is untouched: the replacement carries the
    # same canonical session_ref (the value the Core stores in
    # Run.worker_session_ref never changes, AC-02).
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    task_id = derive_task_id(handle.session_ref)

    result = adapter.replace_session(handle.session_ref)
    assert result.mode is FallbackMode.NATIVE
    assert isinstance(result.handle, WorkerSessionHandle)
    assert result.handle.session_ref == handle.session_ref
    assert result.handle == handle  # byte-identical identity record
    # the transport was re-created (second submission of the same
    # derived task id), the workspace identity was not
    assert store.submissions == [task_id, task_id]
    assert adapter._registry.get(handle.session_ref) is not None
    assert adapter._registry.get(handle.session_ref).handle == handle


def test_claude_ac02_missing_task_record_does_not_rewrite_identity():
    # A task-store record that went missing (e.g. the task list was
    # cleared) does not nullify or rewrite the Core's identity: resume
    # still answers the typed FALLBACK with the same durable handle and
    # a reason naming both the limitation and the missing record, and
    # the workspace identity is unchanged.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    snapshot_before = adapter._registry.to_records()

    store.records.clear()
    result = adapter.resume_session(handle.session_ref)
    assert result.mode is FallbackMode.FALLBACK
    assert result.fallback_reason == RESUME_LIMITATION_RECORD_MISSING_REASON
    assert result.handle == handle
    assert adapter._registry.to_records() == snapshot_before
    assert is_valid_id(handle.session_ref, kind="session")


def test_claude_ac02_task_record_alone_never_creates_identity():
    # The task store never creates identity: a stray task record with no
    # durable workspace record is not a session -- resume is the data
    # error SessionNotFoundError, and the adapter never asks the store
    # to name a session (AC-02: identity flows from the Core/workspace,
    # never from the task store).
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    ref = generate_id("session", "claude_code", "ghost", "worker", PROJECT_ID)
    store.records.add(derive_task_id(ref))

    with pytest.raises(SessionNotFoundError):
        adapter.resume_session(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.terminate_session(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.is_session_alive(ref)


def test_claude_ac02_adapter_never_queries_the_store_by_identity_fields():
    # Every store interaction is keyed by the derived task id -- the
    # adapter never hands the store an identity field (the store could
    # not influence any identity even if it tried). Pinned by recording
    # every task id the fake is asked about.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    store.live.add(derive_task_id(handle.session_ref))
    adapter.resume_session(handle.session_ref)
    adapter.is_session_alive(handle.session_ref)
    adapter.terminate_session(handle.session_ref)

    task_id = derive_task_id(handle.session_ref)
    assert store.submissions == [task_id]


def test_claude_ac02_replacement_reconstructs_from_workspace_snapshot():
    # The scenario-G reconstruction flow: the lead process dies; a fresh
    # adapter over the same workspace (the durable snapshot replayed
    # into a fresh registry via from_records, with a fresh task store
    # whose live sessions are all gone) answers the same identity -- the
    # Core's session_ref is unchanged, the durable outbox survives, and
    # the replacement session takes over without chat-memory access
    # (13-EXECUTION-MONITOR.md SS3-SS4).
    original_store = ScriptedTeamStore()
    original = make_adapter(store=original_store)
    handle = spawn_worker(original)
    ref = handle.session_ref
    pending = original.expose_command(CommandSpec(session_ref=ref, directive="report"))
    assert pending.mode is FallbackMode.FALLBACK
    assert pending.fallback_reason == COMMAND_OUTBOX_REASON

    # the crash-time durable snapshot
    snapshot = original._registry.to_records()

    # a brand-new adapter over the same workspace: a fresh store (the
    # file-based task list survives the restart -- record present -- but
    # the in-process teammate session objects are gone) + registry
    # rehydrated from the durable snapshot -- the reconstruction source
    fresh_store = ScriptedTeamStore()
    fresh_store.records.add(derive_task_id(ref))
    fresh = make_adapter(store=fresh_store, registry=SessionRegistry.from_records(snapshot))

    resumed = fresh.resume_session(ref)
    assert resumed.mode is FallbackMode.FALLBACK
    assert resumed.fallback_reason == RESUME_LIMITATION_REASON
    assert resumed.handle.session_ref == ref
    assert resumed.handle == handle

    replaced = fresh.replace_session(ref)
    assert replaced.mode is FallbackMode.NATIVE
    assert replaced.handle.session_ref == ref  # the same canonical ref
    assert fresh._registry.to_records() == snapshot  # identity untouched
    # the durable outbox survived the replacement (SS4 reconstruction)
    assert fresh._registry.pending_commands(ref) == ("report",)

    # the resumed engine can observe the replacement session
    fresh_store.live.add(derive_task_id(ref))
    alive = fresh.is_session_alive(ref)
    assert alive.mode is FallbackMode.NATIVE and alive.alive is True


def test_claude_ac02_fallback_replacement_keeps_identity_and_outbox():
    # The fallback_subagent replacement path of the same logical context
    # also keeps the canonical session_ref AND the durable outbox (the
    # registry re-registration never wipes the pending commands).
    adapter = ClaudeCodePlatformAdapter(
        team_store=ScriptedTeamStore(),
        process_runner=AcceptingRunner(),
        registry=SessionRegistry(),
    )
    handle = spawn_worker(adapter)
    adapter.expose_command(
        CommandSpec(session_ref=handle.session_ref, directive="report")
    )
    assert adapter._registry.pending_commands(handle.session_ref) == ("report",)

    replacement = adapter.fallback_subagent(
        "worker", make_context(), project_id=PROJECT_ID
    )
    assert replacement.mode is FallbackMode.FALLBACK
    assert isinstance(replacement.handle, WorkerSessionHandle)
    assert replacement.handle.session_ref == handle.session_ref
    # the durable outbox is preserved for the replacement session
    assert adapter._registry.pending_commands(handle.session_ref) == ("report",)
    adapter._registry.clear_pending_commands(handle.session_ref)
    assert adapter._registry.pending_commands(handle.session_ref) == ()


def test_claude_ac02_snapshot_round_trips_and_corrupt_state_is_a_data_error():
    # The durable snapshot (to_records/from_records) round-trips
    # losslessly -- the reconstruction source of SS4 -- and corrupt
    # workspace state is the stable PlatformAdapterDataError.
    adapter = make_adapter()
    handle = spawn_worker(adapter)
    adapter.expose_command(
        CommandSpec(session_ref=handle.session_ref, directive="watch")
    )
    adapter.terminate_session(handle.session_ref)

    snapshot = adapter._registry.to_records()
    restored = SessionRegistry.from_records(snapshot)
    assert restored.to_records() == snapshot
    assert restored.get(handle.session_ref) == adapter._registry.get(handle.session_ref)
    assert restored.is_terminated(handle.session_ref)
    assert restored.pending_commands(handle.session_ref) == ("watch",)

    with pytest.raises(PlatformAdapterDataError):
        SessionRegistry.from_records([{"state": "active"}])
    with pytest.raises(PlatformAdapterDataError):
        SessionRegistry.from_records(
            [{"handle": {"platform_id": "claude_code"}, "state": "active"}]
        )


def test_claude_ac02_replace_unknown_or_unavailable():
    # replace_session on an unknown ref is SessionNotFoundError; with an
    # unavailable store the explicit UNSUPPORTED refusal preserves the
    # identity.
    adapter = make_adapter()
    with pytest.raises(SessionNotFoundError):
        adapter.replace_session(
            generate_id("session", "claude_code", "w", "worker", PROJECT_ID)
        )

    store = ScriptedTeamStore()
    adapter2 = make_adapter(store=store)
    handle = spawn_worker(adapter2)
    store.unavailable = True
    result = adapter2.replace_session(handle.session_ref)
    assert result.mode is FallbackMode.UNSUPPORTED
    assert result.handle is None
    assert result.fallback_reason == REPLACE_STORE_UNAVAILABLE_REASON
    assert adapter2._registry.get(handle.session_ref) is not None


# ---------------------------------------------------------------------------
# Delivery and determinism
# ---------------------------------------------------------------------------


def test_claude_expose_command_native_when_live_and_outboxed_when_not():
    # expose_command delivers into the live in-process session (NATIVE,
    # recorded at the boundary) or preserves the directive in the
    # durable outbox (FALLBACK naming the fallback channel, SS4).
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    handle = spawn_worker(adapter)
    command = CommandSpec(session_ref=handle.session_ref, directive="poll slurm")

    store.live.add(derive_task_id(handle.session_ref))
    delivered = adapter.expose_command(command)
    assert delivered.mode is FallbackMode.NATIVE
    assert delivered.fallback_reason is None
    assert store.deliveries == [(derive_task_id(handle.session_ref), "poll slurm")]
    assert adapter._registry.pending_commands(handle.session_ref) == ()

    store.live.clear()
    outboxed = adapter.expose_command(
        CommandSpec(session_ref=handle.session_ref, directive="recheck")
    )
    assert outboxed.mode is FallbackMode.FALLBACK
    assert outboxed.fallback_reason == COMMAND_OUTBOX_REASON
    assert adapter._registry.pending_commands(handle.session_ref) == ("recheck",)

    # store unavailable: the same explicit fallback channel
    store2 = ScriptedTeamStore()
    adapter2 = make_adapter(store=store2)
    handle2 = spawn_worker(adapter2)
    store2.unavailable = True
    refused = adapter2.expose_command(
        CommandSpec(session_ref=handle2.session_ref, directive="recheck")
    )
    assert refused.mode is FallbackMode.FALLBACK
    assert refused.fallback_reason == COMMAND_OUTBOX_REASON
    assert adapter2._registry.pending_commands(handle2.session_ref) == ("recheck",)


def test_claude_deterministic_flow_repeatable():
    # The whole flow -- spawn, resume (limitation), replace, liveness,
    # terminate -- is a pure function of the inputs: two fresh
    # executions produce byte-identical results and snapshots.
    def run() -> tuple[object, ...]:
        store = ScriptedTeamStore()
        adapter = make_adapter(store=store)
        handle = spawn_monitor(adapter)
        store.live.add(derive_task_id(handle.session_ref))
        resume_native = adapter.resume_session(handle.session_ref)
        store.live.clear()
        resume_fallback = adapter.resume_session(handle.session_ref)
        alive_unknown = adapter.is_session_alive(handle.session_ref)
        replaced = adapter.replace_session(handle.session_ref)
        snapshot = adapter._registry.to_records()
        stop = adapter.terminate_session(handle.session_ref)
        return (
            resume_native,
            resume_fallback,
            alive_unknown,
            replaced,
            snapshot,
            stop,
        )

    first = run()
    second = run()
    assert first == second
    assert first[0].mode is FallbackMode.NATIVE
    assert first[1].mode is FallbackMode.FALLBACK
    assert first[1].fallback_reason == RESUME_LIMITATION_REASON
    assert first[2].mode is FallbackMode.UNSUPPORTED
    assert first[3].mode is FallbackMode.NATIVE
    assert first[5].mode is FallbackMode.FALLBACK
