"""Codex orchestration adapter -- AC-01 spawn mapping (DEV-M10-G05).

The concrete :class:`CodexPlatformAdapter` maps the normalized
orchestration contract (15-ADAPTER-SPEC.md SS5) onto Codex's currently
available mechanisms. These tests pin the AC-01 spawn paths and the
hermetic mock boundary:

* ``spawn_persistent_role`` / ``spawn_worker`` are NATIVE Codex session
  spawns: the session id derives deterministically from the canonical
  ``session_ref`` (the session store is a transport detail, AC-02), the
  run bundle carries the frozen role contract directives, and the
  durable identity is registered workspace-side in the
  :class:`SessionRegistry`;
* ``fallback_subagent`` is the explicit one-shot exec fallback channel
  (``codex exec`` -- always an explicit FALLBACK answer naming the
  channel), and the spawned worker carries the same canonical
  ``session_ref`` as a native spawn of the same logical context (AC-02);
* capability answers are distinct from data errors
  (``SessionNotFoundError`` / ``PlatformAdapterDataError``), exactly as
  in DEV-M10-G02;
* AC-02 -- the adapter is orchestration transport only: a structural AST
  scan proves the codex package imports no scientific-core domain
  module (no ``research``/``rules``/``state``/``analysis``/
  ``monitor``/``domain``/``execution`` internals) and no third-party
  package at all -- only the platform-neutral seams (platform base,
  shared role contracts, ``core.ids``, ``core.models``);
* the codex package is claude-independent: no Claude/OpenAI-specific
  import roots appear anywhere in the package.

The suite is pure: scripted in-memory fakes at the boundary (precedent:
the M8-G06 ``SlurmClusterMock`` fakes the ``SSHTransport`` boundary),
no wall clock, no randomness, no file I/O.
"""

from __future__ import annotations

import ast
import shutil
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
from scientific_reproduction.adapters.platform.codex import (
    CODEX_ADAPTER_VERSION,
    CODEX_PLATFORM_ID,
    SPAWN_CLIENT_UNAVAILABLE_REASON,
    SUBAGENT_FALLBACK_REASON,
    SUBAGENT_UNAVAILABLE_REASON,
    CodexPlatformAdapter,
    CodexProcessRunner,
    CodexRunResult,
    CodexSessionClient,
    CodexSessionClientStore,
    CodexSessionUnavailableError,
    SessionProbe,
    SessionRecord,
    SessionRegistry,
    SessionState,
    build_run_bundle,
    derive_run_id,
    derive_session_id,
    render_exec_prompt,
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

#: The codex adapter package (scanned for AC-02 neutrality).
CODEX_DIR = (
    REPO_ROOT / "src" / "scientific_reproduction" / "adapters" / "platform" / "codex"
)

#: Scientific-core domain packages the adapter must never import
#: (AC-02): the frozen vocabulary of the goal ("no scientific-core
#: rule/state/analysis modules") plus every scientific-core domain
#: package present in the repository. The neutral seams the platform
#: adapters legitimately share are ``core`` and the platform base /
#: shared role contracts.
SCIENTIFIC_DOMAIN_PACKAGES: tuple[str, ...] = (
    "analysis",
    "research",
    "monitoring",
    "planning",
    "reporting",
    "workers",
    "artifacts",
    "audit",
    "domain_packs",
    "rules",
    "state",
    "execution",
    "monitor",
    "domain",
    "cli",
)

#: Claude/OpenAI-specific import roots that must not appear anywhere in
#: the codex package (the codex adapter is claude-independent).
FOREIGN_PLATFORM_IMPORT_ROOTS: tuple[str, ...] = (
    "anthropic",
    "claude_code",
    "claude",
    "us.anthropic",
    "claude_agent_sdk",
    "openai",
)

PROJECT_ID = generate_id("project", "g05")
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


class ScriptedSessionClient(CodexSessionClient):
    """The deterministic in-suite Codex session client fake.

    ``records`` -- session ids whose recorded transcript exists;
    ``live`` -- session ids with an attached live session process;
    ``submissions``/``stops``/``deliveries`` -- the recorded boundary
    calls, so tests can pin exactly what the adapter asked the client;
    ``accept=False`` -- the client refuses session starts;
    ``unavailable=True`` -- the client raises the typed CLI-absent
    refusal (:class:`CodexSessionUnavailableError`).
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


class ScriptedProcessRunner(CodexProcessRunner):
    """The deterministic in-suite one-shot exec runner fake."""

    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.spawns: list[tuple[str, str]] = []

    def spawn_run(self, session_ref: str, prompt: str) -> CodexRunResult:
        self.spawns.append((session_ref, prompt))
        return CodexRunResult(run_id=derive_run_id(session_ref), accepted=self.accept)


def make_adapter(
    *,
    client: ScriptedSessionClient | None = None,
    runner: ScriptedProcessRunner | None = None,
    registry: SessionRegistry | None = None,
) -> CodexPlatformAdapter:
    return CodexPlatformAdapter(
        session_client=client or ScriptedSessionClient(),
        process_runner=runner or ScriptedProcessRunner(),
        registry=registry or SessionRegistry(),
    )


# ---------------------------------------------------------------------------
# AC-01 -- the capability record
# ---------------------------------------------------------------------------


def test_codex_ac01_capability_declares_the_native_operations():
    # The typed capability record declares the six orchestration
    # operations as natively supported; fallback_subagent is the
    # explicit fallback channel itself and is deliberately not a native
    # capability (its answer is always FALLBACK, AC-03).
    adapter = make_adapter()
    capability = adapter.capabilities()
    assert isinstance(capability, PlatformCapability)
    assert capability.platform_id == CODEX_PLATFORM_ID
    assert capability.version == CODEX_ADAPTER_VERSION
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
    # runtime availability: the same record with an unavailable client
    # (capability answers stay distinct from data errors and from
    # runtime availability, AC-03).
    assert make_adapter(client=ScriptedSessionClient(unavailable=True)).capabilities() == (
        capability
    )
    assert PlatformCapability.from_dict(capability.to_dict()) == capability


def test_codex_ac01_adapter_implements_the_locked_interface_surface():
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
    assert adapter.platform_id == CODEX_PLATFORM_ID
    assert adapter.version == CODEX_ADAPTER_VERSION


# ---------------------------------------------------------------------------
# AC-01 -- the spawn paths (codex session mapping)
# ---------------------------------------------------------------------------


def test_codex_ac01_spawn_persistent_role_is_a_native_codex_session():
    # The Monitor use of 15-ADAPTER-SPEC.md: spawn_persistent_role is a
    # NATIVE Codex session start. The session id derives from the
    # canonical session_ref (the session store is a transport detail,
    # AC-02), and the durable identity is registered workspace-side.
    client = ScriptedSessionClient()
    adapter = make_adapter(client=client)
    result = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)

    assert result.mode is FallbackMode.NATIVE
    assert result.fallback_reason is None
    handle = result.handle
    assert isinstance(handle, WorkerSessionHandle)
    assert handle.platform_id == CODEX_PLATFORM_ID
    assert handle.worker_id == "execution_monitor"  # deterministic per role
    assert handle.role_id == "execution_monitor"
    assert handle.project_id == PROJECT_ID
    assert handle.goal_id is None and handle.context_id is None
    assert is_valid_id(handle.session_ref, kind="session")

    # one session start, keyed by the derived session id
    assert client.submissions == [derive_session_id(handle.session_ref)]
    assert is_valid_id(client.submissions[0], kind="codex_session")
    # the run bundle carries the durable identity plus the frozen role
    # contract (the run mapping deliverable)
    bundle = client.bundles[client.submissions[0]]
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


def test_codex_ac01_spawn_worker_is_a_native_codex_session():
    # A goal-scoped worker is a NATIVE Codex session start; the worker
    # id defaults to the deterministic worker_role value of the frozen
    # goal context, or the caller's explicit worker_id.
    client = ScriptedSessionClient()
    adapter = make_adapter(client=client)
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

    bundle = client.bundles[derive_session_id(handle.session_ref)]
    assert bundle["goal_id"] == GOAL_ID
    assert bundle["context_id"] == context.context_id
    assert bundle["contract_id"] == get_role_contract("worker").contract_id


def test_codex_ac01_session_id_is_a_pure_function_of_session_ref():
    # The transport session id is a deterministic pure function of the
    # durable session_ref: the same identity always yields the same
    # session id, and the session id never influences the identity
    # (AC-02).
    handle = WorkerSessionHandle(
        platform_id=CODEX_PLATFORM_ID,
        worker_id="experiment_worker",
        role_id="worker",
        project_id=PROJECT_ID,
        goal_id=GOAL_ID,
        context_id=make_context().context_id,
    )
    assert derive_session_id(handle.session_ref) == derive_session_id(handle.session_ref)
    assert derive_session_id(handle.session_ref) == generate_id(
        "codex_session", handle.session_ref
    )
    assert is_valid_id(derive_session_id(handle.session_ref), kind="codex_session")
    with pytest.raises(TypeError):
        derive_session_id(123)  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        derive_session_id("not-a-session-ref")


def test_codex_ac01_fallback_subagent_is_the_explicit_fallback_path():
    # 15-ADAPTER-SPEC.md SS5 fallback_subagent(...): always an explicit
    # FALLBACK answer naming the channel -- never a native session --
    # and the spawned worker carries the same canonical session_ref as a
    # native spawn of the same logical context (AC-02).
    client = ScriptedSessionClient()
    runner = ScriptedProcessRunner()
    adapter = make_adapter(client=client, runner=runner)
    context = make_context()

    native = adapter.spawn_worker("worker", context, project_id=PROJECT_ID)
    assert isinstance(native.handle, WorkerSessionHandle)
    fallback = adapter.fallback_subagent("worker", context, project_id=PROJECT_ID)

    assert fallback.mode is FallbackMode.FALLBACK
    assert fallback.fallback_reason == SUBAGENT_FALLBACK_REASON
    assert isinstance(fallback.handle, WorkerSessionHandle)
    assert fallback.handle == native.handle  # identical durable identity
    assert fallback.handle.session_ref == native.handle.session_ref

    # the one-shot exec channel received the durable identity and the
    # rendered prompt (identity + role contract directives)
    assert len(runner.spawns) == 1
    (session_ref, prompt) = runner.spawns[0]
    assert session_ref == native.handle.session_ref
    assert prompt == render_exec_prompt(
        build_run_bundle(fallback.handle, get_role_contract("worker"))
    )
    assert STATE_TRUTH_RULE in prompt
    assert "never declare PASS/FAIL or accept your own output" in prompt
    # the durable identity is registered for the subagent worker too
    assert adapter._registry.get(fallback.handle.session_ref) is not None


def test_codex_ac01_refused_subagent_spawn_keeps_the_durable_identity():
    # A refused one-shot exec spawn is an explicit typed answer with a
    # reason naming the unavailable channel; the durable identity is
    # still registered and preserved (AC-03: never silent, never lost).
    runner = ScriptedProcessRunner(accept=False)
    adapter = make_adapter(runner=runner)
    result = adapter.fallback_subagent("worker", make_context(), project_id=PROJECT_ID)

    assert result.mode is FallbackMode.FALLBACK
    assert result.fallback_reason == SUBAGENT_UNAVAILABLE_REASON
    assert isinstance(result.handle, WorkerSessionHandle)
    assert adapter._registry.get(result.handle.session_ref) is not None


def test_codex_ac01_unavailable_client_answers_explicit_unsupported():
    # When the session client is unavailable (no live Codex CLI), a
    # spawn is an explicit UNSUPPORTED refusal with a reason -- never a
    # fabricated session (AC-03), and never a data error (capability
    # answers are distinct from data errors).
    adapter = make_adapter(client=ScriptedSessionClient(unavailable=True))
    persistent = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert persistent.mode is FallbackMode.UNSUPPORTED
    assert persistent.handle is None
    assert persistent.fallback_reason == SPAWN_CLIENT_UNAVAILABLE_REASON

    worker = adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    assert worker.mode is FallbackMode.UNSUPPORTED
    assert worker.handle is None
    assert worker.fallback_reason == SPAWN_CLIENT_UNAVAILABLE_REASON

    refused = make_adapter(client=ScriptedSessionClient(accept=False))
    result = refused.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert result.mode is FallbackMode.UNSUPPORTED
    assert result.handle is None
    assert result.fallback_reason == SPAWN_CLIENT_UNAVAILABLE_REASON


def test_codex_ac01_default_adapter_without_live_runtime_answers_explicitly():
    # The default adapter (real thin client wrappers, no live Codex CLI
    # in the test runtime) still honors the contract: a spawn answers
    # the explicit UNSUPPORTED (never a fabricated session) and the
    # explicit one-shot fallback answers FALLBACK naming the unavailable
    # channel.
    adapter = CodexPlatformAdapter()
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


def _codex_cli_available() -> bool:
    return shutil.which("codex") is not None


@pytest.mark.skipif(
    _codex_cli_available(), reason="a codex CLI is on the PATH in this environment"
)
def test_codex_cli_guard_raises_typed_refusal_when_cli_absent():
    # The real client wrapper refuses with the typed
    # CodexSessionUnavailableError when no Codex CLI is available (the
    # guard the adapter translates into the explicit UNSUPPORTED
    # answer).
    client = CodexSessionClientStore()
    ref = generate_id("session", CODEX_PLATFORM_ID, "w", "worker", PROJECT_ID)
    with pytest.raises(CodexSessionUnavailableError):
        client.start_session(derive_session_id(ref), {"session_ref": ref})
    with pytest.raises(CodexSessionUnavailableError):
        client.probe(derive_session_id(ref))
    assert issubclass(CodexSessionUnavailableError, ValueError)


# ---------------------------------------------------------------------------
# Data errors and capability-vs-error separation (DEV-M10-G02 discipline)
# ---------------------------------------------------------------------------


def test_codex_unknown_session_is_session_not_found():
    # A session the adapter's durable registry knows nothing about is a
    # SessionNotFoundError -- a broken reference the caller must
    # resolve, distinct from a capability answer.
    adapter = make_adapter()
    ref = generate_id("session", CODEX_PLATFORM_ID, "w", "worker", PROJECT_ID)
    with pytest.raises(SessionNotFoundError):
        adapter.resume_session(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.terminate_session(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.is_session_alive(ref)
    with pytest.raises(SessionNotFoundError):
        adapter.expose_command(CommandSpec(session_ref=ref, directive="go"))
    assert issubclass(SessionNotFoundError, ValueError)


def test_codex_invalid_inputs_raise_type_and_data_errors():
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
            generate_id("session", CODEX_PLATFORM_ID, "w", "worker", PROJECT_ID)
        )


# ---------------------------------------------------------------------------
# The run bundle (run mapping deliverable)
# ---------------------------------------------------------------------------


def test_codex_run_bundle_is_canonical_and_carries_the_contract():
    # The bundle is a canonical, deterministic dict: durable identity
    # plus the frozen role contract directives -- the payload the
    # session client starts and the prompt the one-shot exec executes.
    # Equal inputs produce byte-identical bundles; corrupt bundles are a
    # stable PlatformAdapterDataError.
    adapter = make_adapter()
    handle = adapter.spawn_worker(
        "worker", make_context(), project_id=PROJECT_ID
    ).handle
    assert isinstance(handle, WorkerSessionHandle)
    contract = get_role_contract("worker")
    bundle = build_run_bundle(handle, contract)
    assert bundle["session_ref"] == handle.session_ref
    assert bundle["contract_id"] == contract.contract_id
    assert bundle["contract_version"] == ROLE_CONTRACTS_VERSION
    assert bundle["state_truth_rule"] == STATE_TRUTH_RULE
    assert list(bundle["prompt_obligations"]) == list(contract.prompt_obligations)
    assert build_run_bundle(handle, contract) == bundle

    prompt = render_exec_prompt(bundle)
    assert STATE_TRUTH_RULE in prompt
    assert "never declare PASS/FAIL or accept your own output" in prompt
    assert prompt == render_exec_prompt(bundle)

    with pytest.raises(TypeError):
        build_run_bundle(handle, "not a contract")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        render_exec_prompt("not a bundle")  # type: ignore[arg-type]
    with pytest.raises(PlatformAdapterDataError):
        render_exec_prompt({"session_ref": handle.session_ref})


# ---------------------------------------------------------------------------
# AC-02 -- no scientific-core domain imports (structural pin)
# ---------------------------------------------------------------------------


def _iter_imported_modules(source: str) -> list[str]:
    """All module names this source imports (import/from-import)."""
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return sorted(set(modules))


def test_codex_ac02_adapter_imports_no_scientific_core_domain_module():
    # AC-02 structural pin: the codex adapter package is orchestration
    # transport only. No module of the package may import a scientific-
    # core domain package (the frozen goal vocabulary plus every domain
    # package present in the repository); the only scientific_reproduction
    # imports allowed are the platform-neutral seams the DEV-M10-G02/G03
    # adapters legitimately share (platform base, shared role contracts,
    # core.ids / core.models) and the codex package itself.
    allowed_platform_seams = {
        "scientific_reproduction.adapters.platform.base",
        "scientific_reproduction.adapters.platform.contracts",
        "scientific_reproduction.adapters.platform.contracts.base",
        "scientific_reproduction.adapters.platform.codex",
    }
    offenders: list[str] = []
    for path in sorted(CODEX_DIR.rglob("*.py")):
        for module in _iter_imported_modules(path.read_text(encoding="utf-8")):
            relative = path.relative_to(REPO_ROOT)
            root = module.split(".")[0]
            if root not in (sys.stdlib_module_names or set()) and root not in {
                "scientific_reproduction"
            }:
                offenders.append(
                    f"{relative} imports third-party module {module!r}"
                )
            if not module.startswith("scientific_reproduction."):
                continue
            second = module.split(".")[1]
            if second in SCIENTIFIC_DOMAIN_PACKAGES:
                offenders.append(
                    f"{relative} imports scientific-core domain module {module!r}"
                )
            if module.startswith("scientific_reproduction.adapters"):
                if not any(
                    module == allowed or module.startswith(allowed + ".")
                    for allowed in allowed_platform_seams
                ):
                    offenders.append(
                        f"{relative} imports non-neutral adapter module {module!r}"
                    )
    assert offenders == []


def test_codex_ac02_adapter_imports_no_foreign_platform_api():
    # The codex package is claude-independent: no Claude/OpenAI-specific
    # import root appears anywhere in the package (not even guarded or
    # lazy imports -- the package has no dynamic imports at all).
    for path in sorted(CODEX_DIR.rglob("*.py")):
        modules = _iter_imported_modules(path.read_text(encoding="utf-8"))
        for module in modules:
            root = module.split(".")[0]
            assert root not in FOREIGN_PLATFORM_IMPORT_ROOTS, (
                f"{path.relative_to(REPO_ROOT)} imports foreign platform"
                f" module {module!r}"
            )


def test_codex_hygiene_no_wall_clock_and_no_randomness():
    # Determinism hygiene of the adapter package: no randomness or
    # wall-clock facilities are imported anywhere.
    for path in sorted(CODEX_DIR.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        imports = {module.split(".")[0] for module in _iter_imported_modules(source)}
        for forbidden in ("random", "uuid", "time", "datetime"):
            assert forbidden not in imports, (
                f"{path.relative_to(REPO_ROOT)} imports forbidden {forbidden!r}"
            )


def test_codex_boundary_records_are_frozen_and_typed():
    # The boundary records are frozen, validating dataclasses: no
    # silent mutation, no malformed probes/runs.
    probe = SessionProbe(record_present=True, live_session_attached=False)
    assert probe.record_present and not probe.live_session_attached
    with pytest.raises(FrozenInstanceError):
        probe.live_session_attached = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        SessionProbe(record_present="yes", live_session_attached=False)  # type: ignore[arg-type]

    run = CodexRunResult(
        run_id=derive_run_id(
            generate_id("session", CODEX_PLATFORM_ID, "w", "worker", PROJECT_ID)
        ),
        accepted=True,
    )
    assert run.accepted
    with pytest.raises(FrozenInstanceError):
        run.accepted = False  # type: ignore[misc]
    with pytest.raises(ValueError):
        CodexRunResult(run_id="nope", accepted=True)
    with pytest.raises(TypeError):
        CodexRunResult(run_id=run.run_id, accepted=1)  # type: ignore[arg-type]


def test_codex_session_records_are_frozen_and_round_trip():
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
