"""Claude Code orchestration adapter -- AC-01 spawn mapping (DEV-M10-G03).

The concrete :class:`ClaudeCodePlatformAdapter` maps the normalized
orchestration contract (15-ADAPTER-SPEC.md SS5) onto Claude Code's
currently available mechanisms. These tests pin the AC-01 spawn paths
and the hermetic mock boundary:

* ``spawn_persistent_role`` / ``spawn_worker`` are NATIVE Agent Teams
  task spawns: the task id derives deterministically from the canonical
  ``session_ref`` (the task store is a transport detail, AC-02), the
  task bundle carries the frozen role contract directives (the
  agent-team task mapping deliverable), and the durable identity is
  registered workspace-side in the :class:`SessionRegistry`;
* ``fallback_subagent`` is the explicit subagent/process fallback
  channel -- always an explicit FALLBACK answer naming the channel, and
  the spawned worker carries the same canonical ``session_ref`` as a
  native spawn of the same logical context (AC-02);
* capability answers are distinct from data errors
  (``SessionNotFoundError`` / ``PlatformAdapterDataError``), exactly as
  in DEV-M10-G02;
* Claude-specific imports are contained in the claude_code package and
  localized (module-level imports are stdlib + scientific_reproduction
  only -- pinned by an AST scan mirroring test_base_neutrality), so the
  module imports and the adapter works without the Agent SDK.

The suite is pure: scripted in-memory fakes at the boundary (precedent:
the M8-G06 ``SlurmClusterMock`` fakes the ``SSHTransport`` boundary), no
wall clock, no randomness, no file I/O.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from scientific_reproduction.adapters.platform.base import (
    CommandSpec,
    FallbackMode,
    PlatformAdapter,
    PlatformAdapterDataError,
    PlatformCapability,
    PlatformOperation,
    SessionNotFoundError,
    WorkerSessionHandle,
)
from scientific_reproduction.adapters.platform.claude_code import (
    CLAUDE_CODE_ADAPTER_VERSION,
    CLAUDE_CODE_PLATFORM_ID,
    SPAWN_STORE_UNAVAILABLE_REASON,
    SUBAGENT_FALLBACK_REASON,
    SUBAGENT_UNAVAILABLE_REASON,
    AgentTeamTaskStore,
    ClaudeCodePlatformAdapter,
    ProcessRunner,
    SessionRecord,
    SessionRegistry,
    SessionState,
    SubprocessSpawn,
    TaskStoreProbe,
    TeamStoreClient,
    TeamStoreUnavailableError,
    build_task_bundle,
    derive_spawn_id,
    derive_task_id,
    render_subagent_prompt,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    ROLE_CONTRACTS_VERSION,
    STATE_TRUTH_RULE,
    get_role_contract,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    GoalExecutionContextPackage,
    WorkerRole,
)

#: Repository root: tests/platform/ -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The claude_code adapter package (the only Claude-specific package of
#: the repository; its module-level imports are scanned for neutrality).
CLAUDE_CODE_DIR = (
    REPO_ROOT / "src" / "scientific_reproduction" / "adapters" / "platform"
    / "claude_code"
)

#: Claude-specific import roots (same vocabulary as test_base_neutrality
#: plus the Agent SDK package name).
CLAUDE_SPECIFIC_IMPORT_ROOTS: tuple[str, ...] = (
    "anthropic",
    "claude_code",
    "claude",
    "us.anthropic",
    "claude_agent_sdk",
)

PROJECT_ID = generate_id("project", "g03")
GOAL_ID = generate_id("goal", PROJECT_ID, "g1")


def make_context(
    *, project_id: str = PROJECT_ID, goal_id: str = GOAL_ID, run: str = "r1"
) -> GoalExecutionContextPackage:
    return GoalExecutionContextPackage(
        context_id=generate_id("worker-context", project_id, goal_id, run),
        worker_role=WorkerRole.EXPERIMENT_WORKER,
        goal_id=goal_id,
        goal_version="1.0",
        allowed_actions=["prepare"],
        forbidden_actions=["mutate"],
    )


# ---------------------------------------------------------------------------
# The scripted boundary fakes (deterministic, in-suite)
# ---------------------------------------------------------------------------


class ScriptedTeamStore(TeamStoreClient):
    """The deterministic in-suite agent-team task-store fake.

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


class ScriptedProcessRunner(ProcessRunner):
    """The deterministic in-suite subagent runner fake."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.spawns: list[tuple[str, str]] = []

    def spawn_subagent(self, session_ref: str, prompt: str) -> SubprocessSpawn:
        self.spawns.append((session_ref, prompt))
        return SubprocessSpawn(
            spawn_id=derive_spawn_id(session_ref), accepted=self.accept
        )


def make_adapter(
    *,
    store: ScriptedTeamStore | None = None,
    runner: ScriptedProcessRunner | None = None,
    registry: SessionRegistry | None = None,
) -> ClaudeCodePlatformAdapter:
    return ClaudeCodePlatformAdapter(
        team_store=store or ScriptedTeamStore(),
        process_runner=runner or ScriptedProcessRunner(),
        registry=registry or SessionRegistry(),
    )


# ---------------------------------------------------------------------------
# AC-01 -- the capability record
# ---------------------------------------------------------------------------


def test_claude_ac01_capability_declares_the_native_operations():
    # The typed capability record declares the six orchestration
    # operations as natively supported; fallback_subagent is the
    # explicit fallback channel itself and is deliberately not a native
    # capability (its answer is always FALLBACK, AC-03).
    adapter = make_adapter()
    capability = adapter.capabilities()
    assert isinstance(capability, PlatformCapability)
    assert capability.platform_id == CLAUDE_CODE_PLATFORM_ID
    assert capability.version == CLAUDE_CODE_ADAPTER_VERSION
    assert capability.description
    assert capability.operations == (
        PlatformOperation.SPAWN_PERSISTENT_ROLE,
        PlatformOperation.SPAWN_WORKER,
        PlatformOperation.RESUME_SESSION,
        PlatformOperation.TERMINATE_SESSION,
        PlatformOperation.IS_SESSION_ALIVE,
        PlatformOperation.EXPOSE_COMMAND,
    )
    assert not capability.supports(PlatformOperation.FALLBACK_SUBAGENT)
    assert capability.supports(PlatformOperation.SPAWN_WORKER)
    # The capability record is platform-identity, independent of the
    # runtime availability: the same record with an unavailable store
    # (capability answers stay distinct from data errors and from
    # runtime availability, AC-03).
    assert make_adapter(store=ScriptedTeamStore(unavailable=True)).capabilities() == (
        capability
    )
    assert PlatformCapability.from_dict(capability.to_dict()) == capability


def test_claude_ac01_adapter_implements_the_locked_interface_surface():
    # The concrete adapter implements the frozen PlatformAdapter surface
    # (15-ADAPTER-SPEC.md SS5) -- every abstract operation plus the
    # explicit replacement path (13-EXECUTION-MONITOR.md SS4 / AC-03).
    adapter = make_adapter()
    assert isinstance(adapter, PlatformAdapter)
    missing = set(PlatformAdapter.__abstractmethods__) - {
        name
        for name in dir(adapter)
        if not name.startswith("_") and callable(getattr(adapter, name))
    }
    assert missing == set()
    assert adapter.platform_id == CLAUDE_CODE_PLATFORM_ID
    assert adapter.version == CLAUDE_CODE_ADAPTER_VERSION


# ---------------------------------------------------------------------------
# AC-01 -- the spawn paths (Agent Teams task mapping)
# ---------------------------------------------------------------------------


def test_claude_ac01_spawn_persistent_role_is_a_native_team_task():
    # The Monitor use of 15-ADAPTER-SPEC.md: spawn_persistent_role is a
    # NATIVE Agent Teams task spawn. The task id derives from the
    # canonical session_ref (the task store is a transport detail,
    # AC-02), and the durable identity is registered workspace-side.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    result = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)

    assert result.mode is FallbackMode.NATIVE
    assert result.fallback_reason is None
    handle = result.handle
    assert isinstance(handle, WorkerSessionHandle)
    assert handle.platform_id == CLAUDE_CODE_PLATFORM_ID
    assert handle.worker_id == "execution_monitor"  # deterministic per role
    assert handle.role_id == "execution_monitor"
    assert handle.project_id == PROJECT_ID
    assert handle.goal_id is None and handle.context_id is None
    assert is_valid_id(handle.session_ref, kind="session")

    # one task submission, keyed by the derived task id
    assert store.submissions == [derive_task_id(handle.session_ref)]
    assert is_valid_id(store.submissions[0], kind="team_task")
    # the task bundle carries the durable identity plus the frozen role
    # contract (the agent-team task mapping deliverable)
    bundle = store.bundles[store.submissions[0]]
    assert bundle["session_ref"] == handle.session_ref
    assert bundle["role_id"] == "execution_monitor"
    contract = get_role_contract("execution_monitor")
    assert bundle["contract_id"] == contract.contract_id
    assert bundle["state_truth_rule"] == STATE_TRUTH_RULE
    assert bundle["prompt_obligations"] == list(contract.prompt_obligations)
    assert bundle["prompt_prohibitions"] == list(contract.prompt_prohibitions)
    # the durable identity is registered
    record = adapter._registry.get(handle.session_ref)
    assert record is not None and record.handle == handle
    assert record.state is SessionState.ACTIVE


def test_claude_ac01_spawn_worker_is_a_native_team_task():
    # A goal-scoped worker is a NATIVE Agent Teams task spawn; the
    # worker id defaults to the deterministic worker_role value of the
    # frozen goal context, or the caller's explicit worker_id.
    store = ScriptedTeamStore()
    adapter = make_adapter(store=store)
    context = make_context()
    result = adapter.spawn_worker("worker", context, project_id=PROJECT_ID)

    assert result.mode is FallbackMode.NATIVE
    handle = result.handle
    assert isinstance(handle, WorkerSessionHandle)
    assert handle.role_id == "worker"
    assert handle.worker_id == WorkerRole.EXPERIMENT_WORKER.value
    assert handle.goal_id == GOAL_ID
    assert handle.context_id == context.context_id
    assert is_valid_id(handle.session_ref, kind="session")

    explicit = adapter.spawn_worker(
        "worker", make_context(run="r2"), project_id=PROJECT_ID, worker_id="alice"
    )
    assert isinstance(explicit.handle, WorkerSessionHandle)
    assert explicit.handle.worker_id == "alice"
    assert explicit.handle.session_ref != handle.session_ref

    bundle = store.bundles[derive_task_id(handle.session_ref)]
    assert bundle["goal_id"] == GOAL_ID
    assert bundle["context_id"] == context.context_id
    assert bundle["contract_id"] == get_role_contract("worker").contract_id


def test_claude_ac01_spawn_task_id_is_a_pure_function_of_session_ref():
    # The transport task id is a deterministic pure function of the
    # durable session_ref: the same identity always yields the same task
    # id, and the task id never influences the identity (AC-02).
    handle = WorkerSessionHandle(
        platform_id=CLAUDE_CODE_PLATFORM_ID,
        worker_id="experiment_worker",
        role_id="worker",
        project_id=PROJECT_ID,
        goal_id=GOAL_ID,
        context_id=make_context().context_id,
    )
    assert derive_task_id(handle.session_ref) == derive_task_id(handle.session_ref)
    assert derive_task_id(handle.session_ref) == generate_id(
        "team_task", handle.session_ref
    )
    assert is_valid_id(derive_task_id(handle.session_ref), kind="team_task")
    with pytest.raises(TypeError):
        derive_task_id(123)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        derive_task_id("not-a-session-ref")


def test_claude_ac01_fallback_subagent_is_the_explicit_fallback_path():
    # 15-ADAPTER-SPEC.md SS5 fallback_subagent(...): always an explicit
    # FALLBACK answer naming the channel -- never a native session --
    # and the spawned worker carries the same canonical session_ref as a
    # native spawn of the same logical context (AC-02).
    store = ScriptedTeamStore()
    runner = ScriptedProcessRunner()
    adapter = make_adapter(store=store, runner=runner)
    context = make_context()

    native = adapter.spawn_worker("worker", context, project_id=PROJECT_ID)
    assert isinstance(native.handle, WorkerSessionHandle)
    fallback = adapter.fallback_subagent("worker", context, project_id=PROJECT_ID)

    assert fallback.mode is FallbackMode.FALLBACK
    assert fallback.fallback_reason == SUBAGENT_FALLBACK_REASON
    assert isinstance(fallback.handle, WorkerSessionHandle)
    assert fallback.handle == native.handle  # identical durable identity
    assert fallback.handle.session_ref == native.handle.session_ref

    # the subagent channel received the durable identity and the
    # rendered prompt (identity + role contract directives)
    assert len(runner.spawns) == 1
    (session_ref, prompt) = runner.spawns[0]
    assert session_ref == native.handle.session_ref
    assert prompt == render_subagent_prompt(
        build_task_bundle(fallback.handle, get_role_contract("worker"))
    )
    assert STATE_TRUTH_RULE in prompt
    assert "never declare PASS/FAIL or accept your own output" in prompt
    # the durable identity is registered for the subagent worker too
    assert adapter._registry.get(fallback.handle.session_ref) is not None


def test_claude_ac01_refused_subagent_spawn_keeps_the_durable_identity():
    # A refused subagent spawn is an explicit typed answer with a reason
    # naming the unavailable channel; the durable identity is still
    # registered and preserved (AC-03: never silent, never lost).
    runner = ScriptedProcessRunner(accept=False)
    adapter = make_adapter(runner=runner)
    result = adapter.fallback_subagent("worker", make_context(), project_id=PROJECT_ID)

    assert result.mode is FallbackMode.FALLBACK
    assert result.fallback_reason == SUBAGENT_UNAVAILABLE_REASON
    assert isinstance(result.handle, WorkerSessionHandle)
    assert adapter._registry.get(result.handle.session_ref) is not None


def test_claude_ac01_unavailable_store_answers_explicit_unsupported():
    # When the Agent Teams task store is unavailable (the SDK absent),
    # a spawn is an explicit UNSUPPORTED refusal with a reason -- never
    # a fabricated session (AC-03), and never a data error (capability
    # answers are distinct from data errors).
    adapter = make_adapter(store=ScriptedTeamStore(unavailable=True))
    persistent = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert persistent.mode is FallbackMode.UNSUPPORTED
    assert persistent.handle is None
    assert persistent.fallback_reason == SPAWN_STORE_UNAVAILABLE_REASON

    worker = adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    assert worker.mode is FallbackMode.UNSUPPORTED
    assert worker.handle is None
    assert worker.fallback_reason == SPAWN_STORE_UNAVAILABLE_REASON

    refused = make_adapter(store=ScriptedTeamStore(accept=False))
    result = refused.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert result.mode is FallbackMode.UNSUPPORTED
    assert result.handle is None
    assert result.fallback_reason == SPAWN_STORE_UNAVAILABLE_REASON


def test_claude_ac01_default_adapter_without_sdk_answers_explicitly():
    # The default adapter (real thin client wrappers, no live SDK in the
    # test runtime) still honors the contract: a spawn answers the
    # explicit UNSUPPORTED (never a fabricated session) and the explicit
    # subagent fallback answers FALLBACK naming the unavailable channel.
    adapter = ClaudeCodePlatformAdapter()
    result = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert result.mode is FallbackMode.UNSUPPORTED
    assert result.handle is None
    assert result.fallback_reason
    fallback = adapter.fallback_subagent(
        "worker", make_context(), project_id=PROJECT_ID
    )
    assert fallback.mode is FallbackMode.FALLBACK
    assert isinstance(fallback.handle, WorkerSessionHandle)
    assert fallback.fallback_reason == SUBAGENT_UNAVAILABLE_REASON


def _sdk_importable() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


@pytest.mark.skipif(
    _sdk_importable(), reason="claude_agent_sdk is installed in this environment"
)
def test_claude_sdk_guard_raises_typed_refusal_when_sdk_absent():
    # The real client wrapper refuses with the typed TeamStoreUnavailableError
    # when the Agent SDK is absent (the guard the adapter translates into
    # the explicit UNSUPPORTED answer).
    store = AgentTeamTaskStore()
    ref = generate_id("session", CLAUDE_CODE_PLATFORM_ID, "w", "worker", PROJECT_ID)
    with pytest.raises(TeamStoreUnavailableError):
        store.submit_task(derive_task_id(ref), {"session_ref": ref})
    with pytest.raises(TeamStoreUnavailableError):
        store.probe(derive_task_id(ref))
    assert issubclass(TeamStoreUnavailableError, ValueError)


# ---------------------------------------------------------------------------
# Data errors and capability-vs-error separation (DEV-M10-G02 discipline)
# ---------------------------------------------------------------------------


def test_claude_unknown_session_is_session_not_found():
    # A session the adapter's durable registry knows nothing about is a
    # SessionNotFoundError -- a broken reference the caller must
    # resolve, distinct from a capability answer.
    adapter = make_adapter()
    ref = generate_id("session", CLAUDE_CODE_PLATFORM_ID, "w", "worker", PROJECT_ID)
    with pytest.raises(SessionNotFoundError):
        adapter.resume_session(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.terminate_session(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.is_session_alive(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.expose_command(CommandSpec(session_ref=ref, directive="go"))
    assert issubclass(SessionNotFoundError, ValueError)


def test_claude_invalid_inputs_raise_type_and_data_errors():
    # Wrong input types raise TypeError at the public boundary; malformed
    # references and out-of-vocabulary role ids raise the stable
    # PlatformAdapterDataError (same discipline as DEV-M10-G02).
    adapter = make_adapter()
    with pytest.raises(TypeError):
        adapter.spawn_persistent_role(123, PROJECT_ID)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        adapter.spawn_persistent_role("admin", PROJECT_ID)
    with pytest.raises(PlatformAdapterDataError):
        adapter.spawn_persistent_role("execution_monitor", "not-a-project")
    with pytest.raises(TypeError):
        adapter.spawn_worker("worker", "not a context", project_id=PROJECT_ID)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        adapter.spawn_worker(
            "worker", make_context(), project_id=PROJECT_ID, worker_id="Bad Name!"
        )
    with pytest.raises(TypeError):
        adapter.resume_session(123)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        adapter.resume_session("not-a-session-ref")
    with pytest.raises(PlatformAdapterDataError):
        adapter.terminate_session("not-a-session-ref")
    with pytest.raises(PlatformAdapterDataError):
        adapter.is_session_alive("not-a-session-ref")
    with pytest.raises(TypeError):
        adapter.expose_command("not a CommandSpec")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        adapter.replace_session(123)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        adapter.replace_session("not-a-session-ref")
    with pytest.raises(SessionNotFoundError):
        adapter.replace_session(
            generate_id("session", CLAUDE_CODE_PLATFORM_ID, "w", "worker", PROJECT_ID)
        )


# ---------------------------------------------------------------------------
# The task bundle (agent-team task mapping deliverable)
# ---------------------------------------------------------------------------


def test_claude_task_bundle_is_canonical_and_carries_the_contract():
    # The bundle is a canonical, deterministic dict: durable identity
    # plus the frozen role contract directives -- the payload the task
    # store runs and the prompt the subagent executes. Equal inputs
    # produce byte-identical bundles; corrupt bundles are a stable
    # PlatformAdapterDataError.
    adapter = make_adapter()
    handle = adapter.spawn_worker(
        "worker", make_context(), project_id=PROJECT_ID
    ).handle
    assert isinstance(handle, WorkerSessionHandle)
    contract = get_role_contract("worker")
    bundle = build_task_bundle(handle, contract)
    assert bundle["session_ref"] == handle.session_ref
    assert bundle["contract_id"] == contract.contract_id
    assert bundle["contract_version"] == ROLE_CONTRACTS_VERSION
    assert bundle["state_truth_rule"] == STATE_TRUTH_RULE
    assert list(bundle["prompt_obligations"]) == list(contract.prompt_obligations)
    assert build_task_bundle(handle, contract) == bundle

    prompt = render_subagent_prompt(bundle)
    assert STATE_TRUTH_RULE in prompt
    assert "never declare PASS/FAIL or accept your own output" in prompt
    assert prompt == render_subagent_prompt(bundle)

    with pytest.raises(TypeError):
        build_task_bundle(handle, "not a contract")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        render_subagent_prompt("not a bundle")  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        render_subagent_prompt({"session_ref": handle.session_ref})


# ---------------------------------------------------------------------------
# Boundaries and hygiene
# ---------------------------------------------------------------------------


def _iter_imports_with_scope(source: str) -> list[tuple[bool, str]]:
    """(inside_function_body, module) pairs of every import in the source."""
    tree = ast.parse(source)
    found: list[tuple[bool, str]] = []

    def visit(node: ast.AST, in_function: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Import):
                        found.extend((True, alias.name) for alias in sub.names)
                    elif isinstance(sub, ast.ImportFrom) and sub.module is not None:
                        found.append((True, sub.module))
            elif isinstance(child, ast.Import):
                found.extend((in_function, alias.name) for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module is not None:
                found.append((in_function, child.module))
            else:
                visit(child, in_function)

    visit(tree, False)
    return found


def test_claude_hygiene_no_claude_specific_imports_at_module_level():
    # The neutrality discipline of test_base_neutrality, extended to the
    # claude_code package: module-level imports are stdlib +
    # scientific_reproduction only; Claude-specific SDK roots may appear
    # only inside function bodies (the guarded lazy imports), so the
    # package imports and the adapter works without the Agent SDK.
    for path in sorted(CLAUDE_CODE_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for in_function, module in _iter_imports_with_scope(source):
            root = module.split(".")[0]
            if root in CLAUDE_SPECIFIC_IMPORT_ROOTS:
                assert in_function, (
                    f"{path.relative_to(REPO_ROOT)}: claude-specific import"
                    f" {module!r} must be localized inside a function body"
                )
            elif not in_function:
                assert (
                    root in sys.stdlib_module_names or root == "scientific_reproduction"
                ), (
                    f"{path.relative_to(REPO_ROOT)}: module-level import"
                    f" {module!r} must be stdlib or scientific_reproduction"
                )


def test_claude_hygiene_no_wall_clock_and_no_randomness():
    # Determinism hygiene of the adapter package: no randomness or
    # wall-clock facilities are imported anywhere.
    for path in sorted(CLAUDE_CODE_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        imports = {
            module.split(".")[0]
            for _, module in _iter_imports_with_scope(source)
        }
        for forbidden in ("random", "uuid", "time", "datetime"):
            assert forbidden not in imports, (
                f"{path.relative_to(REPO_ROOT)} imports forbidden {forbidden!r}"
            )


def test_claude_boundary_records_are_frozen_and_typed():
    # The boundary records are frozen, validating dataclasses: no
    # silent mutation, no malformed probes/spawns.
    probe = TaskStoreProbe(task_record_present=True, live_session_attached=False)
    assert probe.task_record_present and not probe.live_session_attached
    with pytest.raises(FrozenInstanceError):
        probe.live_session_attached = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        TaskStoreProbe(task_record_present="yes", live_session_attached=False)  # type: ignore[arg-type]

    spawn = SubprocessSpawn(
        spawn_id=derive_spawn_id(
            generate_id("session", CLAUDE_CODE_PLATFORM_ID, "w", "worker", PROJECT_ID)
        ),
        accepted=True,
    )
    assert spawn.accepted
    with pytest.raises(FrozenInstanceError):
        spawn.accepted = False  # type: ignore[misc]
    with pytest.raises(ValueError):
        SubprocessSpawn(spawn_id="nope", accepted=True)
    with pytest.raises(TypeError):
        SubprocessSpawn(spawn_id=spawn.spawn_id, accepted=1)  # type: ignore[arg-type]


def test_claude_session_records_are_frozen_and_round_trip():
    # SessionRecord is a frozen typed record whose serialization
    # round-trips; corrupt workspace state is a stable
    # PlatformAdapterDataError.
    adapter = make_adapter()
    handle = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID).handle
    assert isinstance(handle, WorkerSessionHandle)
    record = SessionRecord(handle)
    assert record.session_ref == handle.session_ref
    assert record.state is SessionState.ACTIVE
    with pytest.raises(FrozenInstanceError):
        record.state = SessionState.TERMINATED  # type: ignore[misc]
    restored = SessionRecord.from_dict(record.to_dict())
    assert restored == record
    terminated = record.as_terminated()
    assert terminated.state is SessionState.TERMINATED
    assert SessionRecord.from_dict(terminated.to_dict()) == terminated
    with pytest.raises(PlatformAdapterDataError):
        SessionRecord.from_dict({"state": "active"})
    with pytest.raises(PlatformAdapterDataError):
        SessionRecord(handle, pending_commands=("",))
