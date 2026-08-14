"""Cross-platform orchestration contract -- shared battery (DEV-M10-G06,
AC-01: Claude and Codex adapters pass the same core contract tests where
capabilities overlap).

One parameterized battery drives BOTH concrete
:class:`PlatformAdapter` implementations -- the Claude Code adapter
(DEV-M10-G03) and the Codex adapter (DEV-M10-G05) -- through the same
assertions, each with its own hermetic scripted fake at the transport
boundary (no network, no live SDK/CLI, no wall clock, no randomness).
The fakes mirror the unit-suite pattern of ``tests/platform``
(``ScriptedTeamStore`` / ``ScriptedSessionClient`` / scripted process
runners), so the two adapters answer from pure scripted state.

The battery pins the frozen shared vocabulary of
``adapters/platform/base.py``:

* capability declarations -- the same six native
  :class:`PlatformOperation` values, ``PlatformCapability.supports``
  consistency, canonical serialization, and independence from runtime
  availability;
* session identity discipline -- session references are pure functions
  of the canonical worker/role context
  (``derive_worker_session_ref`` / ``WorkerSessionHandle.session_ref``),
  transport ids derive from the ``session_ref`` via ``core.ids.generate_id``
  and never the reverse, and rehydration recomputes the reference;
* typed results -- every operation answers NATIVE results vs typed
  FALLBACK/UNSUPPORTED with stable, non-empty ``fallback_reason`` values
  for the same scripted probe states (AC-03 of the interface);
* the boundary error taxonomy -- ``SessionNotFoundError`` for unknown
  sessions, ``PlatformAdapterDataError`` for corrupt data, ``TypeError``
  at the public boundaries.

Where the two adapters legitimately differ (the ``fallback_subagent``
channel vocabulary, the resume-limitation reasons) the battery asserts
the differences are exactly the DOCUMENTED ones: same contract shape,
platform-specific answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from scientific_reproduction.adapters.platform.base import (
    CommandSpec,
    FallbackMode,
    PlatformAdapter,
    PlatformAdapterDataError,
    PlatformAdapterError,
    PlatformCapability,
    PlatformOperation,
    SessionNotFoundError,
    WorkerSessionHandle,
    derive_worker_session_ref,
)
from scientific_reproduction.adapters.platform.claude_code import (
    ALIVE_STORE_UNAVAILABLE_REASON,
    CLAUDE_CODE_ADAPTER_VERSION,
    CLAUDE_CODE_PLATFORM_ID,
    RESUME_STORE_UNAVAILABLE_REASON,
    SPAWN_STORE_UNAVAILABLE_REASON,
    TERMINATE_STORE_UNAVAILABLE_REASON,
    TERMINATED_ALREADY_REASON,
    TERMINATED_COMMAND_REASON,
    TERMINATED_REPLACE_REASON,
    TERMINATED_RESUME_REASON,
    ClaudeCodePlatformAdapter,
    ProcessRunner,
    SubprocessSpawn,
    TaskStoreProbe,
    TeamStoreClient,
    TeamStoreUnavailableError,
    derive_spawn_id,
    derive_task_id,
)
from scientific_reproduction.adapters.platform.claude_code import (
    ALIVE_UNOBSERVABLE_REASON as CLAUDE_ALIVE_UNOBSERVABLE_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    COMMAND_OUTBOX_REASON as CLAUDE_COMMAND_OUTBOX_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    RESUME_LIMITATION_REASON as CLAUDE_RESUME_LIMITATION_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    RESUME_LIMITATION_RECORD_MISSING_REASON as CLAUDE_RESUME_LIMITATION_RECORD_MISSING_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    SUBAGENT_FALLBACK_REASON as CLAUDE_SUBAGENT_FALLBACK_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    SUBAGENT_UNAVAILABLE_REASON as CLAUDE_SUBAGENT_UNAVAILABLE_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    TERMINATE_FALLBACK_REASON as CLAUDE_TERMINATE_FALLBACK_REASON,
)
from scientific_reproduction.adapters.platform.claude_code import (
    SessionRegistry as ClaudeSessionRegistry,
)
from scientific_reproduction.adapters.platform.codex import (
    ALIVE_CLIENT_UNAVAILABLE_REASON,
    CODEX_ADAPTER_VERSION,
    CODEX_PLATFORM_ID,
    RESUME_CLIENT_UNAVAILABLE_REASON,
    SPAWN_CLIENT_UNAVAILABLE_REASON,
    TERMINATE_CLIENT_UNAVAILABLE_REASON,
    CodexPlatformAdapter,
    CodexProcessRunner,
    CodexRunResult,
    CodexSessionClient,
    CodexSessionUnavailableError,
    SessionProbe,
    derive_run_id,
    derive_session_id,
)
from scientific_reproduction.adapters.platform.codex import (
    ALIVE_UNOBSERVABLE_REASON as CODEX_ALIVE_UNOBSERVABLE_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    COMMAND_OUTBOX_REASON as CODEX_COMMAND_OUTBOX_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    RESUME_LIMITATION_REASON as CODEX_RESUME_LIMITATION_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    RESUME_LIMITATION_RECORD_MISSING_REASON as CODEX_RESUME_LIMITATION_RECORD_MISSING_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    SUBAGENT_FALLBACK_REASON as CODEX_SUBAGENT_FALLBACK_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    SUBAGENT_UNAVAILABLE_REASON as CODEX_SUBAGENT_UNAVAILABLE_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    TERMINATE_FALLBACK_REASON as CODEX_TERMINATE_FALLBACK_REASON,
)
from scientific_reproduction.adapters.platform.codex import (
    SessionRegistry as CodexSessionRegistry,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    GoalExecutionContextPackage,
    WorkerRole,
)

#: The two platform keys of the parameterized battery.
CLAUDE = "claude"
CODEX = "codex"

#: Both platforms, in a stable order (parameterized battery ids).
BOTH_PLATFORMS = (CLAUDE, CODEX)

#: The deterministic project/goal of the battery (the identity of every
#: spawned session is then a pure function of these -- AC-02 discipline).
PROJECT_ID = generate_id("project", "dev-m10-g06")
GOAL_ID = generate_id("goal", "dev-m10-g06")


def make_context(
    *,
    project_id: str = PROJECT_ID,
    goal_id: str = GOAL_ID,
    run: str = "r1",
    evidence_refs: tuple[str, ...] = (),
) -> GoalExecutionContextPackage:
    """A deterministic worker-context package for the battery."""
    return GoalExecutionContextPackage(
        context_id=generate_id("worker-context", project_id, goal_id, run),
        worker_role=WorkerRole.EXPERIMENT_WORKER,
        goal_id=goal_id,
        goal_version="1.0",
        allowed_actions=["prepare"],
        forbidden_actions=["mutate"],
        evidence_refs=list(evidence_refs),
    )


# ---------------------------------------------------------------------------
# The hermetic boundary fakes (deterministic, scripted -- one per platform)
# ---------------------------------------------------------------------------


class ScriptedTeamStore(TeamStoreClient):
    """The Claude adapter's scripted Agent Teams task-store fake.

    ``records`` -- task ids whose persisted task record exists;
    ``live`` -- task ids with an attached live in-process session;
    ``submissions``/``stops``/``deliveries`` -- the recorded boundary
    calls, so tests can pin exactly what the adapter asked the store;
    ``accept=False`` -- the store refuses task submissions;
    ``unavailable=True`` -- the store raises the typed SDK-absent
    refusal (:class:`TeamStoreUnavailableError`).
    """

    def __init__(self, *, accept: bool = True, unavailable: bool = False) -> None:
        self.accept = accept
        self.unavailable = unavailable
        self.submissions: list[str] = []
        self.bundles: dict[str, dict[str, object]] = {}
        self.records: set[str] = set()
        self.live: set[str] = set()
        self.stops: list[str] = []
        self.deliveries: list[tuple[str, str]] = []

    def _refusal(self) -> TeamStoreUnavailableError:
        return TeamStoreUnavailableError(
            "the claude agent sdk is not importable in this runtime; agent"
            " teams task-store operations are unavailable"
        )

    def submit_task(self, task_id: str, bundle: dict[str, object]) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.submissions.append(task_id)
        if not self.accept:
            return False
        self.bundles[task_id] = bundle
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
        self.stops.append(task_id)
        if task_id in self.live:
            self.live.discard(task_id)
            return True
        return False

    def deliver(self, task_id: str, directive: str) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.deliveries.append((task_id, directive))
        return task_id in self.live


class ScriptedSessionClient(CodexSessionClient):
    """The Codex adapter's scripted session-client fake (same scripted
    shape as :class:`ScriptedTeamStore`: ``records``/``live``/
    ``submissions``/``stops``/``deliveries``/``accept``/``unavailable``)."""

    def __init__(self, *, accept: bool = True, unavailable: bool = False) -> None:
        self.accept = accept
        self.unavailable = unavailable
        self.submissions: list[str] = []
        self.bundles: dict[str, dict[str, object]] = {}
        self.records: set[str] = set()
        self.live: set[str] = set()
        self.stops: list[str] = []
        self.deliveries: list[tuple[str, str]] = []

    def _refusal(self) -> CodexSessionUnavailableError:
        return CodexSessionUnavailableError(
            "the codex cli is not available in this runtime; codex session"
            " operations are unavailable"
        )

    def start_session(self, session_id: str, bundle: dict[str, object]) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.submissions.append(session_id)
        if not self.accept:
            return False
        self.bundles[session_id] = bundle
        self.records.add(session_id)
        return True

    def probe(self, session_id: str) -> SessionProbe:
        if self.unavailable:
            raise self._refusal()
        return SessionProbe(
            record_present=session_id in self.records,
            live_session_attached=session_id in self.live,
        )

    def stop_session(self, session_id: str) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.stops.append(session_id)
        if session_id in self.live:
            self.live.discard(session_id)
            return True
        return False

    def deliver(self, session_id: str, directive: str) -> bool:
        if self.unavailable:
            raise self._refusal()
        self.deliveries.append((session_id, directive))
        return session_id in self.live


class ScriptedClaudeRunner(ProcessRunner):
    """The Claude subagent runner fake (``claude -p`` single-shot)."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.spawns: list[tuple[str, str]] = []

    def spawn_subagent(self, session_ref: str, prompt: str) -> SubprocessSpawn:
        self.spawns.append((session_ref, prompt))
        return SubprocessSpawn(
            spawn_id=derive_spawn_id(session_ref), accepted=self.accept
        )


class ScriptedCodexRunner(CodexProcessRunner):
    """The Codex one-shot exec runner fake (``codex exec --json``)."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.spawns: list[tuple[str, str]] = []

    def spawn_run(self, session_ref: str, prompt: str) -> CodexRunResult:
        self.spawns.append((session_ref, prompt))
        return CodexRunResult(run_id=derive_run_id(session_ref), accepted=self.accept)


# ---------------------------------------------------------------------------
# The per-platform rig (adapter + scripted boundary fakes)
# ---------------------------------------------------------------------------


@dataclass
class PlatformRig:
    """One platform's adapter plus its scripted boundary fakes.

    ``store`` and ``runner`` expose the same scripted surface for both
    platforms (``records``/``live``/``submissions``/``stops``/
    ``deliveries``/``accept``/``unavailable``; ``spawns``/``accept``), so
    the shared battery drives both adapters identically.
    """

    platform_id: str
    adapter: PlatformAdapter
    store: Any
    runner: Any
    registry: Any

    def transport_id(self, session_ref: str) -> str:
        """The transport-side id the adapter derived from ``session_ref``
        (the store key; a pure function of the durable identity)."""
        if self.platform_id == CLAUDE_CODE_PLATFORM_ID:
            return derive_task_id(session_ref)
        return derive_session_id(session_ref)


def make_rig(
    platform: str,
    *,
    accept: bool = True,
    unavailable: bool = False,
    runner_accept: bool = True,
    registry: Any = None,
) -> PlatformRig:
    """Build one platform's rig with fresh scripted fakes.

    ``accept=False`` -- the transport refuses every submission;
    ``unavailable=True`` -- the transport raises the typed refusal (the
    runtime-absent answer); ``runner_accept=False`` -- the subagent
    channel refuses every spawn; ``registry`` -- an optional pre-built
    registry (e.g. rehydrated from a durable snapshot, the SS4
    reconstruction source).
    """
    if platform == CLAUDE:
        store = ScriptedTeamStore(accept=accept, unavailable=unavailable)
        runner = ScriptedClaudeRunner(accept=runner_accept)
        registry = registry if registry is not None else ClaudeSessionRegistry()
        adapter = ClaudeCodePlatformAdapter(
            team_store=store, process_runner=runner, registry=registry
        )
        return PlatformRig(
            CLAUDE_CODE_PLATFORM_ID, adapter, store, runner, registry
        )
    if platform == CODEX:
        store = ScriptedSessionClient(accept=accept, unavailable=unavailable)
        runner = ScriptedCodexRunner(accept=runner_accept)
        registry = registry if registry is not None else CodexSessionRegistry()
        adapter = CodexPlatformAdapter(
            session_client=store, process_runner=runner, registry=registry
        )
        return PlatformRig(CODEX_PLATFORM_ID, adapter, store, runner, registry)
    raise ValueError(f"unknown platform {platform!r}; expected one of {BOTH_PLATFORMS}")


def rehydrate_rig(platform: str, snapshot: tuple[dict[str, Any], ...]) -> PlatformRig:
    """A fresh rig over the same durable workspace snapshot (the SS4
    reconstruction of 13-EXECUTION-MONITOR.md): a fresh transport (its
    live sessions are all gone) plus a registry rehydrated from the
    crash-state snapshot -- a brand-new adapter over the same state
    directory answers the same identity without chat-memory access."""
    registry = (
        ClaudeSessionRegistry.from_records(snapshot)
        if platform == CLAUDE
        else CodexSessionRegistry.from_records(snapshot)
    )
    return make_rig(platform, registry=registry)


def spawn_monitor(rig: PlatformRig) -> WorkerSessionHandle:
    """The Monitor spawn of the rig (project-persistent role)."""
    result = rig.adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert isinstance(result.handle, WorkerSessionHandle)
    return result.handle


def spawn_worker(
    rig: PlatformRig, context: GoalExecutionContextPackage | None = None
) -> WorkerSessionHandle:
    """The goal-scoped worker spawn of the rig."""
    context = context if context is not None else make_context()
    result = rig.adapter.spawn_worker("worker", context, project_id=PROJECT_ID)
    assert isinstance(result.handle, WorkerSessionHandle)
    return result.handle


# ---------------------------------------------------------------------------
# Capability declarations (the AC-03 anchor of the shared contract)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_capability_declares_the_same_native_operations(platform: str) -> None:
    # Both adapters declare the same six native orchestration operations;
    # fallback_subagent is the explicit fallback channel itself and is
    # deliberately not a native capability (its answer is always FALLBACK).
    rig = make_rig(platform)
    capability = rig.adapter.capabilities()
    assert isinstance(capability, PlatformCapability)
    assert capability.platform_id == rig.platform_id
    assert capability.version == "1.0"
    assert capability.version == (
        CLAUDE_CODE_ADAPTER_VERSION if platform == CLAUDE else CODEX_ADAPTER_VERSION
    )
    assert capability.description
    assert capability.operations == (
        PlatformOperation.SPAWN_PERSISTENT_ROLE,
        PlatformOperation.SPAWN_WORKER,
        PlatformOperation.RESUME_SESSION,
        PlatformOperation.TERMINATE_SESSION,
        PlatformOperation.IS_SESSION_ALIVE,
        PlatformOperation.EXPOSE_COMMAND,
    )
    # supports() is consistent with the declared operations for every
    # member of the frozen operation vocabulary.
    for operation in PlatformOperation:
        assert capability.supports(operation) is (operation in capability.operations)
    assert capability.supports(PlatformOperation.SPAWN_WORKER)
    assert not capability.supports(PlatformOperation.FALLBACK_SUBAGENT)
    # The capability record is platform identity, independent of the
    # runtime availability: an unavailable transport answers the same
    # record (capability answers stay distinct from runtime availability).
    assert make_rig(platform, unavailable=True).adapter.capabilities() == capability
    # Canonical serialization round-trips losslessly; a different
    # record is a different capability.
    assert PlatformCapability.from_dict(capability.to_dict()) == capability
    assert PlatformCapability.from_dict(
        {
            "platform_id": capability.platform_id,
            "version": "2.0",
            "description": capability.description,
            "operations": [],
        }
    ) != capability


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_adapter_implements_the_locked_interface_surface(platform: str) -> None:
    # Both adapters implement the frozen PlatformAdapter surface (every
    # abstract operation) with stable class-level identity.
    rig = make_rig(platform)
    assert isinstance(rig.adapter, PlatformAdapter)
    missing = set(PlatformAdapter.__abstractmethods__) - {
        name
        for name in dir(rig.adapter)
        if not name.startswith("_") and callable(getattr(rig.adapter, name))
    }
    assert missing == set()
    assert rig.adapter.platform_id == rig.platform_id
    assert rig.adapter.version == "1.0"


# ---------------------------------------------------------------------------
# Session identity discipline (AC-02: pure functions of the canonical
# context; transport ids derive from the session_ref, never the reverse)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_session_identity_is_a_pure_function_of_the_context(
    platform: str,
) -> None:
    # The durable session reference is a pure deterministic function of
    # the logical worker/role context: same inputs, same reference; the
    # handle's session_ref is exactly derive_worker_session_ref(...); a
    # different context yields a different reference.
    rig = make_rig(platform)
    context = make_context()
    handle = spawn_worker(rig, context)
    assert handle.platform_id == rig.platform_id
    assert is_valid_id(handle.session_ref, kind="session")
    assert handle.session_ref == derive_worker_session_ref(
        rig.platform_id,
        handle.worker_id,
        handle.role_id,
        handle.project_id,
        handle.goal_id,
        handle.context_id,
    )
    assert handle.session_ref == derive_worker_session_ref(
        rig.platform_id,
        handle.worker_id,
        handle.role_id,
        handle.project_id,
        handle.goal_id,
        handle.context_id,
    )
    other = spawn_worker(rig, make_context(run="r2"))
    assert other.session_ref != handle.session_ref
    assert handle.session_ref == derive_worker_session_ref(
        rig.platform_id, "experiment_worker", "worker", PROJECT_ID, GOAL_ID,
        context.context_id,
    )
    # The transport id is a pure function of the session_ref via
    # core.ids.generate_id -- the store key never influences the identity.
    transport = rig.transport_id(handle.session_ref)
    assert transport == rig.transport_id(handle.session_ref)
    assert is_valid_id(transport)
    assert transport == generate_id(
        "team_task" if platform == CLAUDE else "codex_session", handle.session_ref
    )
    # Rehydration recomputes the reference: a stale or corrupt session_ref
    # in persisted state self-corrects (never authoritative).
    stale = WorkerSessionHandle.from_dict(
        {**handle.to_dict(), "session_ref": "sr_session_" + "0" * 32}
    )
    assert stale.session_ref == handle.session_ref
    assert WorkerSessionHandle.from_dict(handle.to_dict()) == handle


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_transport_record_alone_never_creates_identity(platform: str) -> None:
    # The transport store never creates identity: a stray task/session
    # record with no durable workspace record is not a session -- the
    # operations answer the data error SessionNotFoundError, and the
    # adapter never asks the store to name a session.
    rig = make_rig(platform)
    ref = generate_id("session", rig.platform_id, "ghost", "worker", PROJECT_ID)
    rig.store.records.add(rig.transport_id(ref))

    with pytest.raises(SessionNotFoundError):
        rig.adapter.resume_session(ref)
    with pytest.raises(SessionNotFoundError):
        rig.adapter.terminate_session(ref)
    with pytest.raises(SessionNotFoundError):
        rig.adapter.is_session_alive(ref)
    assert rig.registry.get(ref) is None


# ---------------------------------------------------------------------------
# Spawn results (typed NATIVE; explicit UNSUPPORTED when unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_spawn_persistent_role_is_native_and_registered(platform: str) -> None:
    # The Monitor use of 15-ADAPTER-SPEC.md: spawn_persistent_role is a
    # NATIVE spawn keyed by the derived transport id; the durable
    # identity is registered workspace-side as ACTIVE.
    rig = make_rig(platform)
    result = rig.adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)

    assert result.mode is FallbackMode.NATIVE
    assert result.fallback_reason is None
    handle = result.handle
    assert isinstance(handle, WorkerSessionHandle)
    assert handle.platform_id == rig.platform_id
    assert handle.worker_id == "execution_monitor"  # deterministic per role
    assert handle.role_id == "execution_monitor"
    assert handle.project_id == PROJECT_ID
    assert handle.goal_id is None and handle.context_id is None
    assert is_valid_id(handle.session_ref, kind="session")

    # one submission, keyed by the derived transport id; the bundle
    # carries the durable identity (the transport never names it)
    assert rig.store.submissions == [rig.transport_id(handle.session_ref)]
    bundle = rig.store.bundles[rig.store.submissions[0]]
    assert bundle["session_ref"] == handle.session_ref
    record = rig.registry.get(handle.session_ref)
    assert record is not None and record.handle == handle
    # the durable lifecycle state is the same vocabulary on both
    # platforms (each package owns its StrEnum; compare by value)
    assert record.state.value == "active"


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_spawn_worker_is_native_and_deterministic(platform: str) -> None:
    # A goal-scoped worker is a NATIVE spawn; the worker id defaults to
    # the deterministic worker_role value of the frozen context, or the
    # caller's explicit worker_id.
    rig = make_rig(platform)
    context = make_context()
    result = rig.adapter.spawn_worker("worker", context, project_id=PROJECT_ID)

    assert result.mode is FallbackMode.NATIVE
    handle = result.handle
    assert isinstance(handle, WorkerSessionHandle)
    assert handle.role_id == "worker"
    assert handle.worker_id == WorkerRole.EXPERIMENT_WORKER.value
    assert handle.goal_id == GOAL_ID
    assert handle.context_id == context.context_id
    assert is_valid_id(handle.session_ref, kind="session")

    explicit = rig.adapter.spawn_worker(
        "worker", make_context(run="r2"), project_id=PROJECT_ID, worker_id="alice"
    )
    assert isinstance(explicit.handle, WorkerSessionHandle)
    assert explicit.handle.worker_id == "alice"
    assert explicit.handle.session_ref != handle.session_ref

    bundle = rig.store.bundles[rig.transport_id(handle.session_ref)]
    assert bundle["goal_id"] == GOAL_ID
    assert bundle["context_id"] == context.context_id


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_unavailable_transport_answers_explicit_unsupported(
    platform: str,
) -> None:
    # When the transport is unavailable (no live SDK/CLI), a spawn is an
    # explicit UNSUPPORTED refusal with a stable reason -- never a
    # fabricated session, never a data error.
    rig = make_rig(platform, unavailable=True)
    persistent = rig.adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert persistent.mode is FallbackMode.UNSUPPORTED
    assert persistent.handle is None
    assert persistent.fallback_reason
    worker = rig.adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    assert worker.mode is FallbackMode.UNSUPPORTED
    assert worker.handle is None
    assert worker.fallback_reason

    refused = make_rig(platform, accept=False)
    result = refused.adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert result.mode is FallbackMode.UNSUPPORTED
    assert result.handle is None
    assert result.fallback_reason


# ---------------------------------------------------------------------------
# Resume / liveness / command delivery / termination (typed modes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_resume_answers_native_and_fallback_typed_modes(platform: str) -> None:
    # Resume answers NATIVE only when a live session is attached; every
    # un-resumable state is the typed FALLBACK carrying the SAME durable
    # handle and a stable reason naming the limitation -- the Core's
    # session_ref is never nullified by the transport (AC-02).
    rig = make_rig(platform)
    handle = spawn_worker(rig)
    transport = rig.transport_id(handle.session_ref)

    rig.store.live.add(transport)
    native = rig.adapter.resume_session(handle.session_ref)
    assert native.mode is FallbackMode.NATIVE
    assert native.fallback_reason is None
    assert native.handle == handle

    rig.store.live.clear()
    limited = rig.adapter.resume_session(handle.session_ref)
    assert limited.mode is FallbackMode.FALLBACK
    assert limited.handle == handle  # the same durable identity
    assert limited.fallback_reason and "not resumable" in limited.fallback_reason

    rig.store.records.clear()
    missing = rig.adapter.resume_session(handle.session_ref)
    assert missing.mode is FallbackMode.FALLBACK
    assert missing.handle == handle
    assert missing.fallback_reason

    rig.store.unavailable = True
    refused = rig.adapter.resume_session(handle.session_ref)
    assert refused.mode is FallbackMode.FALLBACK
    assert refused.handle == handle
    assert refused.fallback_reason

    # a terminated session is never resumed: the typed UNSUPPORTED
    rig2 = make_rig(platform)
    handle2 = spawn_worker(rig2)
    rig2.adapter.terminate_session(handle2.session_ref)
    terminated = rig2.adapter.resume_session(handle2.session_ref)
    assert terminated.mode is FallbackMode.UNSUPPORTED
    assert terminated.handle is None
    assert terminated.fallback_reason == TERMINATED_RESUME_REASON


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_alive_answers_observed_or_explicit_unsupported(platform: str) -> None:
    # Liveness is only ever NATIVE with an observed boolean; every
    # unobservable state is the explicit UNSUPPORTED with alive=None --
    # never a liveness guess (AC-03).
    rig = make_rig(platform)
    handle = spawn_worker(rig)
    transport = rig.transport_id(handle.session_ref)

    rig.store.live.add(transport)
    observed = rig.adapter.is_session_alive(handle.session_ref)
    assert observed.mode is FallbackMode.NATIVE
    assert observed.alive is True
    assert observed.fallback_reason is None

    rig.store.live.clear()
    unobservable = rig.adapter.is_session_alive(handle.session_ref)
    assert unobservable.mode is FallbackMode.UNSUPPORTED
    assert unobservable.alive is None
    assert unobservable.fallback_reason

    rig.store.unavailable = True
    refused = rig.adapter.is_session_alive(handle.session_ref)
    assert refused.mode is FallbackMode.UNSUPPORTED
    assert refused.alive is None
    assert refused.fallback_reason

    # the durably terminated state is the one case where alive=False is
    # a durable fact, not a guess
    rig2 = make_rig(platform)
    handle2 = spawn_worker(rig2)
    rig2.adapter.terminate_session(handle2.session_ref)
    terminated = rig2.adapter.is_session_alive(handle2.session_ref)
    assert terminated.mode is FallbackMode.NATIVE
    assert terminated.alive is False


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_command_delivery_is_native_or_durable_outbox(platform: str) -> None:
    # expose_command delivers into the live session (NATIVE) or preserves
    # the directive in the durable outbox for the replacement session
    # (FALLBACK naming the fallback channel) -- never a silent drop.
    rig = make_rig(platform)
    handle = spawn_worker(rig)
    transport = rig.transport_id(handle.session_ref)
    command = CommandSpec(session_ref=handle.session_ref, directive="poll slurm")

    rig.store.live.add(transport)
    delivered = rig.adapter.expose_command(command)
    assert delivered.mode is FallbackMode.NATIVE
    assert delivered.fallback_reason is None
    assert delivered.command == command
    assert rig.store.deliveries == [(transport, "poll slurm")]
    assert rig.registry.pending_commands(handle.session_ref) == ()

    rig.store.live.clear()
    outboxed = rig.adapter.expose_command(command)
    assert outboxed.mode is FallbackMode.FALLBACK
    assert outboxed.command == command
    assert outboxed.fallback_reason
    assert rig.registry.pending_commands(handle.session_ref) == ("poll slurm",)

    # a terminated session refuses delivery outright (UNSUPPORTED)
    rig2 = make_rig(platform)
    handle2 = spawn_worker(rig2)
    rig2.adapter.terminate_session(handle2.session_ref)
    refused = rig2.adapter.expose_command(
        CommandSpec(session_ref=handle2.session_ref, directive="go")
    )
    assert refused.mode is FallbackMode.UNSUPPORTED
    assert refused.fallback_reason == TERMINATED_COMMAND_REASON


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_terminate_answers_typed_modes_and_durable_state(platform: str) -> None:
    # terminate is NATIVE when a live session was stopped; FALLBACK when
    # there was no live session to stop -- the termination is recorded
    # durably, so the session is never resumed or replaced.
    rig = make_rig(platform)
    handle = spawn_worker(rig)
    transport = rig.transport_id(handle.session_ref)

    rig.store.live.add(transport)
    native = rig.adapter.terminate_session(handle.session_ref)
    assert native.mode is FallbackMode.NATIVE
    assert native.fallback_reason is None
    assert transport not in rig.store.live
    assert rig.registry.is_terminated(handle.session_ref)

    again = rig.adapter.terminate_session(handle.session_ref)
    assert again.mode is FallbackMode.FALLBACK  # idempotent
    assert again.fallback_reason == TERMINATED_ALREADY_REASON

    # no live session: the termination is recorded durably (FALLBACK)
    rig2 = make_rig(platform)
    handle2 = spawn_worker(rig2)
    recorded = rig2.adapter.terminate_session(handle2.session_ref)
    assert recorded.mode is FallbackMode.FALLBACK
    assert recorded.fallback_reason
    assert rig2.registry.is_terminated(handle2.session_ref)

    # terminated: never resumed, never replaced, liveness is the durable
    # False
    resume = rig2.adapter.resume_session(handle2.session_ref)
    assert resume.mode is FallbackMode.UNSUPPORTED
    assert resume.handle is None
    replace = rig2.adapter.replace_session(handle2.session_ref)
    assert replace.mode is FallbackMode.UNSUPPORTED
    assert replace.handle is None
    alive = rig2.adapter.is_session_alive(handle2.session_ref)
    assert alive.mode is FallbackMode.NATIVE and alive.alive is False


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_fallback_subagent_always_answers_explicit_fallback(
    platform: str,
) -> None:
    # 15-ADAPTER-SPEC.md SS5: fallback_subagent is the explicit fallback
    # channel -- always a FALLBACK answer naming the channel, never a
    # native session -- and the spawned worker carries the SAME canonical
    # session_ref as a native spawn of the same logical context (AC-02).
    rig = make_rig(platform)
    context = make_context()

    native = rig.adapter.spawn_worker("worker", context, project_id=PROJECT_ID)
    assert isinstance(native.handle, WorkerSessionHandle)
    fallback = rig.adapter.fallback_subagent("worker", context, project_id=PROJECT_ID)

    assert fallback.mode is FallbackMode.FALLBACK
    assert fallback.fallback_reason
    assert isinstance(fallback.handle, WorkerSessionHandle)
    assert fallback.handle == native.handle  # identical durable identity
    assert fallback.handle.session_ref == native.handle.session_ref
    assert rig.registry.get(fallback.handle.session_ref) is not None

    # a refused subagent channel still keeps the durable identity
    refusing = make_rig(platform, runner_accept=False)
    refused = refusing.adapter.fallback_subagent(
        "worker", context, project_id=PROJECT_ID
    )
    assert refused.mode is FallbackMode.FALLBACK
    assert refused.fallback_reason
    assert isinstance(refused.handle, WorkerSessionHandle)
    assert refusing.registry.get(refused.handle.session_ref) is not None


# ---------------------------------------------------------------------------
# The boundary error taxonomy (capability answers are distinct from data
# errors, DEV-M10-G02 discipline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_unknown_session_is_session_not_found(platform: str) -> None:
    # A session the durable registry knows nothing about is a
    # SessionNotFoundError -- a broken reference the caller must resolve,
    # distinct from a capability answer.
    rig = make_rig(platform)
    ref = generate_id("session", rig.platform_id, "w", "worker", PROJECT_ID)
    with pytest.raises(SessionNotFoundError):
        rig.adapter.resume_session(ref)
    with pytest.raises(SessionNotFoundError):
        rig.adapter.terminate_session(ref)
    with pytest.raises(SessionNotFoundError):
        rig.adapter.is_session_alive(ref)
    with pytest.raises(SessionNotFoundError):
        rig.adapter.expose_command(CommandSpec(session_ref=ref, directive="go"))
    with pytest.raises(SessionNotFoundError):
        rig.adapter.replace_session(ref)
    assert issubclass(SessionNotFoundError, ValueError)


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_corrupt_data_is_data_error_and_bad_types_are_type_error(
    platform: str,
) -> None:
    # Wrong input types raise TypeError at the public boundary; malformed
    # references and out-of-vocabulary role ids raise the stable
    # PlatformAdapterDataError (same discipline on both platforms).
    rig = make_rig(platform)
    with pytest.raises(TypeError):
        rig.adapter.spawn_persistent_role(123, PROJECT_ID)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.spawn_persistent_role("admin", PROJECT_ID)
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.spawn_persistent_role("execution_monitor", "not-a-project")
    with pytest.raises(TypeError):
        rig.adapter.spawn_worker(
            "worker", "not a context", project_id=PROJECT_ID  # type: ignore[arg-type]
        )
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.spawn_worker(
            "worker", make_context(), project_id=PROJECT_ID, worker_id="Bad Name!"
        )
    with pytest.raises(TypeError):
        rig.adapter.resume_session(123)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.resume_session("not-a-session-ref")
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.terminate_session("not-a-session-ref")
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.is_session_alive("not-a-session-ref")
    with pytest.raises(TypeError):
        rig.adapter.expose_command("not a CommandSpec")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        rig.adapter.replace_session(123)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        rig.adapter.replace_session("not-a-session-ref")
    assert issubclass(PlatformAdapterDataError, PlatformAdapterError)
    assert issubclass(PlatformAdapterError, ValueError)


# ---------------------------------------------------------------------------
# Result records: uniformly typed, stable reasons, canonical round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_result_records_serialize_canonically(platform: str) -> None:
    # Every operation result is a frozen record of the shared vocabulary
    # whose canonical serialization round-trips losslessly on both
    # platforms (the state-backend / event-log path).
    rig = make_rig(platform)
    handle = spawn_worker(rig)
    command = CommandSpec(session_ref=handle.session_ref, directive="report")

    spawn = rig.adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert spawn.to_dict()["mode"] == "native"
    assert type(spawn).from_dict(spawn.to_dict()) == spawn
    resume = rig.adapter.resume_session(handle.session_ref)
    assert type(resume).from_dict(resume.to_dict()) == resume
    message = rig.adapter.expose_command(command)
    assert type(message).from_dict(message.to_dict()) == message
    alive = rig.adapter.is_session_alive(handle.session_ref)
    assert type(alive).from_dict(alive.to_dict()) == alive
    terminated = rig.adapter.terminate_session(handle.session_ref)
    assert type(terminated).from_dict(terminated.to_dict()) == terminated

    assert WorkerSessionHandle.from_dict(handle.to_dict()) == handle
    assert CommandSpec.from_dict(command.to_dict()) == command
    # corrupt records are the stable data error
    with pytest.raises(PlatformAdapterDataError):
        type(spawn).from_dict({"mode": "native"})  # missing handle
    with pytest.raises(PlatformAdapterDataError):
        type(terminated).from_dict({"mode": "native"})  # missing session_ref


@pytest.mark.parametrize("platform", BOTH_PLATFORMS, ids=BOTH_PLATFORMS)
def test_contract_full_flow_is_deterministic_and_repeatable(platform: str) -> None:
    # The whole flow -- spawn, resume (limitation), liveness, command
    # outbox, replacement, terminate -- is a pure function of the inputs:
    # two fresh executions produce byte-identical results and snapshots.

    def run() -> tuple[object, ...]:
        rig = make_rig(platform)
        handle = spawn_worker(rig)
        transport = rig.transport_id(handle.session_ref)
        rig.store.live.add(transport)
        resume_native = rig.adapter.resume_session(handle.session_ref)
        rig.store.live.clear()
        resume_fallback = rig.adapter.resume_session(handle.session_ref)
        alive_unknown = rig.adapter.is_session_alive(handle.session_ref)
        outbox = rig.adapter.expose_command(
            CommandSpec(session_ref=handle.session_ref, directive="watch")
        )
        replaced = rig.adapter.replace_session(handle.session_ref)
        snapshot = rig.registry.to_records()
        stop = rig.adapter.terminate_session(handle.session_ref)
        return (resume_native, resume_fallback, alive_unknown, outbox,
                replaced, snapshot, stop)

    first = run()
    second = run()
    assert first == second
    assert first[0].mode is FallbackMode.NATIVE  # type: ignore[union-attr]
    assert first[1].mode is FallbackMode.FALLBACK  # type: ignore[union-attr]
    assert first[2].mode is FallbackMode.UNSUPPORTED  # type: ignore[union-attr]
    assert first[3].mode is FallbackMode.FALLBACK  # type: ignore[union-attr]
    assert first[4].mode is FallbackMode.NATIVE  # type: ignore[union-attr]
    assert first[6].mode is FallbackMode.FALLBACK  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# The DOCUMENTED differences: same contract shape, platform-specific
# answers (fallback channel vocabulary, resume-limitation reasons)
# ---------------------------------------------------------------------------


def test_contract_documented_differences_are_the_only_differences() -> None:
    # Both adapters answer the SAME contract shape for the same probe
    # state; where they legitimately differ (the fallback_subagent
    # channel vocabulary, the resume-limitation reasons) the differences
    # are exactly the documented platform-specific answers.
    claude = make_rig(CLAUDE)
    codex = make_rig(CODEX)
    context = make_context()

    native_claude = spawn_worker(claude, context)
    native_codex = spawn_worker(codex, context)
    assert native_claude.role_id == native_codex.role_id == "worker"
    assert native_claude.goal_id == native_codex.goal_id == GOAL_ID
    assert native_claude.context_id == native_codex.context_id
    # the durable refs differ ONLY by the platform-scoped identity
    assert native_claude.session_ref != native_codex.session_ref
    assert native_claude.session_ref == derive_worker_session_ref(
        CLAUDE_CODE_PLATFORM_ID, native_claude.worker_id, "worker", PROJECT_ID,
        GOAL_ID, native_claude.context_id,
    )
    assert native_codex.session_ref == derive_worker_session_ref(
        CODEX_PLATFORM_ID, native_codex.worker_id, "worker", PROJECT_ID,
        GOAL_ID, native_codex.context_id,
    )

    # -- resume limitation: same shape (FALLBACK + the durable handle),
    # platform-specific reason vocabulary naming each limitation
    resume_claude = claude.adapter.resume_session(native_claude.session_ref)
    resume_codex = codex.adapter.resume_session(native_codex.session_ref)
    assert resume_claude.mode is resume_codex.mode is FallbackMode.FALLBACK
    assert resume_claude.handle == native_claude
    assert resume_codex.handle == native_codex
    assert resume_claude.fallback_reason == CLAUDE_RESUME_LIMITATION_REASON
    assert resume_codex.fallback_reason == CODEX_RESUME_LIMITATION_REASON
    assert CLAUDE_RESUME_LIMITATION_REASON != CODEX_RESUME_LIMITATION_REASON
    assert "agent teams" in resume_claude.fallback_reason
    assert "codex sessions" in resume_codex.fallback_reason
    assert "not resumable" in resume_claude.fallback_reason
    assert "not resumable" in resume_codex.fallback_reason

    # -- missing-record resume: same shape, same documented vocabulary
    claude.store.records.clear()
    codex.store.records.clear()
    missing_claude = claude.adapter.resume_session(native_claude.session_ref)
    missing_codex = codex.adapter.resume_session(native_codex.session_ref)
    assert missing_claude.mode is missing_codex.mode is FallbackMode.FALLBACK
    assert missing_claude.handle == native_claude
    assert missing_codex.handle == native_codex
    assert missing_claude.fallback_reason == (
        CLAUDE_RESUME_LIMITATION_RECORD_MISSING_REASON
    )
    assert missing_codex.fallback_reason == (
        CODEX_RESUME_LIMITATION_RECORD_MISSING_REASON
    )

    # -- liveness unobservable: same shape (UNSUPPORTED, alive=None),
    # platform-specific reason
    alive_claude = claude.adapter.is_session_alive(native_claude.session_ref)
    alive_codex = codex.adapter.is_session_alive(native_codex.session_ref)
    assert alive_claude.mode is alive_codex.mode is FallbackMode.UNSUPPORTED
    assert alive_claude.alive is None and alive_codex.alive is None
    assert alive_claude.fallback_reason == CLAUDE_ALIVE_UNOBSERVABLE_REASON
    assert alive_codex.fallback_reason == CODEX_ALIVE_UNOBSERVABLE_REASON
    assert CLAUDE_ALIVE_UNOBSERVABLE_REASON != CODEX_ALIVE_UNOBSERVABLE_REASON
    assert "agent teams" in alive_claude.fallback_reason
    assert "codex" in alive_codex.fallback_reason

    # -- command outbox: same shape (FALLBACK preserving the directive in
    # the durable outbox), platform-specific wording
    outbox_claude = claude.adapter.expose_command(
        CommandSpec(session_ref=native_claude.session_ref, directive="watch")
    )
    outbox_codex = codex.adapter.expose_command(
        CommandSpec(session_ref=native_codex.session_ref, directive="watch")
    )
    assert outbox_claude.mode is outbox_codex.mode is FallbackMode.FALLBACK
    assert "durable outbox" in outbox_claude.fallback_reason
    assert "durable outbox" in outbox_codex.fallback_reason
    assert CLAUDE_COMMAND_OUTBOX_REASON != CODEX_COMMAND_OUTBOX_REASON
    assert "in-process" in outbox_claude.fallback_reason
    assert "in-process" not in outbox_codex.fallback_reason
    assert claude.registry.pending_commands(
        native_claude.session_ref
    ) == ("watch",)
    assert codex.registry.pending_commands(native_codex.session_ref) == (
        "watch",
    )

    # -- the accepted subagent channel: the SAME explicit reason on both
    # platforms (identical channel semantics), same durable identity
    fallback_claude = claude.adapter.fallback_subagent(
        "worker", context, project_id=PROJECT_ID
    )
    fallback_codex = codex.adapter.fallback_subagent(
        "worker", context, project_id=PROJECT_ID
    )
    assert fallback_claude.mode is fallback_codex.mode is FallbackMode.FALLBACK
    assert CLAUDE_SUBAGENT_FALLBACK_REASON == CODEX_SUBAGENT_FALLBACK_REASON
    assert fallback_claude.fallback_reason == CLAUDE_SUBAGENT_FALLBACK_REASON
    assert fallback_codex.fallback_reason == CODEX_SUBAGENT_FALLBACK_REASON
    assert isinstance(fallback_claude.handle, WorkerSessionHandle)
    assert isinstance(fallback_codex.handle, WorkerSessionHandle)
    assert fallback_claude.handle == native_claude
    assert fallback_codex.handle == native_codex

    # -- the refused subagent channel: same shape, platform-specific
    # channel vocabulary
    refused_claude = make_rig(CLAUDE, runner_accept=False)
    refused_codex = make_rig(CODEX, runner_accept=False)
    native_c = spawn_worker(refused_claude, context)
    native_x = spawn_worker(refused_codex, context)
    sub_claude = refused_claude.adapter.fallback_subagent(
        "worker", context, project_id=PROJECT_ID
    )
    sub_codex = refused_codex.adapter.fallback_subagent(
        "worker", context, project_id=PROJECT_ID
    )
    assert sub_claude.mode is sub_codex.mode is FallbackMode.FALLBACK
    assert CLAUDE_SUBAGENT_UNAVAILABLE_REASON != CODEX_SUBAGENT_UNAVAILABLE_REASON
    assert sub_claude.fallback_reason == CLAUDE_SUBAGENT_UNAVAILABLE_REASON
    assert sub_codex.fallback_reason == CODEX_SUBAGENT_UNAVAILABLE_REASON
    assert "subagent fallback channel" in sub_claude.fallback_reason
    assert "one-shot codex exec" in sub_codex.fallback_reason
    assert sub_claude.handle == native_c  # identity preserved
    assert sub_codex.handle == native_x

    # -- unavailable transport on spawn: same shape (UNSUPPORTED, no
    # handle), platform-specific reason
    store_claude = make_rig(CLAUDE, unavailable=True)
    store_codex = make_rig(CODEX, unavailable=True)
    spawn_claude = store_claude.adapter.spawn_persistent_role(
        "execution_monitor", PROJECT_ID
    )
    spawn_codex = store_codex.adapter.spawn_persistent_role(
        "execution_monitor", PROJECT_ID
    )
    assert spawn_claude.mode is spawn_codex.mode is FallbackMode.UNSUPPORTED
    assert spawn_claude.handle is None and spawn_codex.handle is None
    assert SPAWN_STORE_UNAVAILABLE_REASON != SPAWN_CLIENT_UNAVAILABLE_REASON
    assert spawn_claude.fallback_reason == SPAWN_STORE_UNAVAILABLE_REASON
    assert spawn_codex.fallback_reason == SPAWN_CLIENT_UNAVAILABLE_REASON
    assert "agent teams task store" in spawn_claude.fallback_reason
    assert "codex session client" in spawn_codex.fallback_reason

    # -- store-unavailable terminate: same shape, platform-specific
    # reason
    claude_store_off = make_rig(CLAUDE)
    codex_store_off = make_rig(CODEX)
    h_c = spawn_worker(claude_store_off, context)
    h_x = spawn_worker(codex_store_off, context)
    claude_store_off.store.unavailable = True
    codex_store_off.store.unavailable = True
    stop_claude = claude_store_off.adapter.terminate_session(h_c.session_ref)
    stop_codex = codex_store_off.adapter.terminate_session(h_x.session_ref)
    assert stop_claude.mode is stop_codex.mode is FallbackMode.FALLBACK
    assert stop_claude.fallback_reason == TERMINATE_STORE_UNAVAILABLE_REASON
    assert stop_codex.fallback_reason == TERMINATE_CLIENT_UNAVAILABLE_REASON

    # -- store-unavailable resume: same shape, platform-specific reason
    # (fresh rigs: the terminate above durably terminated its sessions)
    claude_resume_off = make_rig(CLAUDE)
    codex_resume_off = make_rig(CODEX)
    hr_c = spawn_worker(claude_resume_off, context)
    hr_x = spawn_worker(codex_resume_off, context)
    claude_resume_off.store.unavailable = True
    codex_resume_off.store.unavailable = True
    resume_claude_off = claude_resume_off.adapter.resume_session(hr_c.session_ref)
    resume_codex_off = codex_resume_off.adapter.resume_session(hr_x.session_ref)
    assert resume_claude_off.mode is resume_codex_off.mode is FallbackMode.FALLBACK
    assert resume_claude_off.handle == hr_c  # identity preserved
    assert resume_codex_off.handle == hr_x
    assert resume_claude_off.fallback_reason == RESUME_STORE_UNAVAILABLE_REASON
    assert resume_codex_off.fallback_reason == RESUME_CLIENT_UNAVAILABLE_REASON

    # -- terminated-session vocabulary is identical (the durable facts
    # are platform-neutral)
    claude_off = make_rig(CLAUDE)
    codex_off = make_rig(CODEX)
    hc = spawn_worker(claude_off, context)
    hx = spawn_worker(codex_off, context)
    for rig, ref in ((claude_off, hc.session_ref), (codex_off, hx.session_ref)):
        rig.adapter.terminate_session(ref)
        resume = rig.adapter.resume_session(ref)
        assert resume.mode is FallbackMode.UNSUPPORTED
        assert resume.fallback_reason == TERMINATED_RESUME_REASON
        replace = rig.adapter.replace_session(ref)
        assert replace.mode is FallbackMode.UNSUPPORTED
        assert replace.fallback_reason == TERMINATED_REPLACE_REASON
        command = rig.adapter.expose_command(
            CommandSpec(session_ref=ref, directive="go")
        )
        assert command.mode is FallbackMode.UNSUPPORTED
        assert command.fallback_reason == TERMINATED_COMMAND_REASON
        again = rig.adapter.terminate_session(ref)
        assert again.mode is FallbackMode.FALLBACK
        assert again.fallback_reason == TERMINATED_ALREADY_REASON
    assert CLAUDE_TERMINATE_FALLBACK_REASON != CODEX_TERMINATE_FALLBACK_REASON
    assert "durable session registry" in CLAUDE_TERMINATE_FALLBACK_REASON
    assert "durable session registry" in CODEX_TERMINATE_FALLBACK_REASON
    assert ALIVE_STORE_UNAVAILABLE_REASON != ALIVE_CLIENT_UNAVAILABLE_REASON
