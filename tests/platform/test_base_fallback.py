"""Fallback mode is explicit (DEV-M10-G02 AC-03).

When a platform capability is unavailable the caller always gets an
explicit, typed answer, never a silent degradation. These tests pin the
fallback contract of the orchestration interface:

* :class:`FallbackMode` is a typed StrEnum vocabulary (NATIVE /
  FALLBACK / UNSUPPORTED) carried by every operation result;
* every interface capability has a defined explicit fallback answer: an
  operation the adapter's :class:`PlatformCapability` does not list as
  natively supported MUST be answered FALLBACK or UNSUPPORTED with a
  ``fallback_reason`` -- never NATIVE, never a fabricated handle
  (M10-G05 AC-03: "Unavailable live capability is reported as
  capability/fallback, not silently faked");
* callers can distinguish a real result from a fallback by ``mode``,
  and capability answers are distinct from data errors
  (:class:`SessionNotFoundError` etc.);
* :meth:`PlatformAdapter.fallback_subagent` is the explicit fallback
  path (15-ADAPTER-SPEC.md SS5) and always reports FALLBACK.

The suite is pure: no file I/O, no wall clock, no randomness.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import FrozenInstanceError

import pytest

from scientific_reproduction.adapters.platform.base import (
    AliveResult,
    CommandSpec,
    FallbackMode,
    MessageResult,
    PlatformAdapter,
    PlatformAdapterDataError,
    PlatformCapability,
    PlatformOperation,
    ResumeResult,
    SessionNotFoundError,
    SpawnResult,
    TerminateResult,
    WorkerSessionHandle,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    CONTRACT_ROLE_IDS,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    GoalExecutionContextPackage,
    WorkerRole,
)

ALL_OPERATIONS: tuple[PlatformOperation, ...] = tuple(PlatformOperation)

PLATFORM_ID = "mock"
PROJECT_ID = generate_id("project", "ac03")
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


class MockPlatformAdapter(PlatformAdapter):
    """Deterministic in-suite adapter honoring the fallback contract.

    ``operations`` declares which operations are answered natively;
    every other operation is answered as an explicit UNSUPPORTED result
    with a reason (the contract of AC-03). ``fallback_subagent`` is the
    explicit fallback path and always reports FALLBACK.
    """

    platform_id = PLATFORM_ID
    version = "1.0"
    description = "deterministic in-suite mock"

    def __init__(
        self, operations: tuple[PlatformOperation, ...] = ALL_OPERATIONS
    ) -> None:
        self._capability = PlatformCapability(
            platform_id=self.platform_id,
            version=self.version,
            description=self.description,
            operations=operations,
        )
        self._sessions: dict[str, WorkerSessionHandle] = {}

    def capabilities(self) -> PlatformCapability:
        return self._capability

    def _spawn_handle(
        self,
        role_id: str,
        project_id: str,
        goal_context: GoalExecutionContextPackage | None,
        worker_id: str | None,
    ) -> WorkerSessionHandle:
        worker = worker_id or (
            goal_context.worker_role.value if goal_context is not None else "persistent"
        )
        return WorkerSessionHandle(
            platform_id=self.platform_id,
            worker_id=worker,
            role_id=role_id,
            project_id=project_id,
            goal_id=goal_context.goal_id if goal_context is not None else None,
            context_id=goal_context.context_id if goal_context is not None else None,
        )

    def spawn_persistent_role(self, role_id: str, project_id: str) -> SpawnResult:
        if not self._capability.supports(PlatformOperation.SPAWN_PERSISTENT_ROLE):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason="persistent sessions are not supported",
            )
        handle = self._spawn_handle(role_id, project_id, None, None)
        self._sessions[handle.session_ref] = handle
        return SpawnResult(handle=handle, mode=FallbackMode.NATIVE)

    def spawn_worker(
        self,
        role_id: str,
        goal_context: GoalExecutionContextPackage,
        *,
        project_id: str,
        worker_id: str | None = None,
    ) -> SpawnResult:
        if not isinstance(goal_context, GoalExecutionContextPackage):
            raise TypeError(
                "spawn_worker expects a GoalExecutionContextPackage, got"
                f" {type(goal_context).__name__}"
            )
        if not self._capability.supports(PlatformOperation.SPAWN_WORKER):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason="worker sessions are not supported",
            )
        handle = self._spawn_handle(role_id, project_id, goal_context, worker_id)
        self._sessions[handle.session_ref] = handle
        return SpawnResult(handle=handle, mode=FallbackMode.NATIVE)

    def resume_session(self, session_ref: str) -> ResumeResult:
        if not self._capability.supports(PlatformOperation.RESUME_SESSION):
            return ResumeResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason="session resume is not supported",
            )
        if session_ref not in self._sessions:
            raise SessionNotFoundError(f"no session found for {session_ref!r}")
        return ResumeResult(
            handle=self._sessions[session_ref], mode=FallbackMode.NATIVE
        )

    def terminate_session(self, session_ref: str) -> TerminateResult:
        if not self._capability.supports(PlatformOperation.TERMINATE_SESSION):
            return TerminateResult(
                session_ref=session_ref,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason="session termination is not supported",
            )
        if session_ref not in self._sessions:
            raise SessionNotFoundError(f"no session found for {session_ref!r}")
        del self._sessions[session_ref]
        return TerminateResult(session_ref=session_ref, mode=FallbackMode.NATIVE)

    def is_session_alive(self, session_ref: str) -> AliveResult:
        if not self._capability.supports(PlatformOperation.IS_SESSION_ALIVE):
            return AliveResult(
                session_ref=session_ref,
                alive=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason="liveness checks are not supported",
            )
        if session_ref not in self._sessions:
            raise SessionNotFoundError(f"no session found for {session_ref!r}")
        return AliveResult(
            session_ref=session_ref, alive=True, mode=FallbackMode.NATIVE
        )

    def expose_command(self, command: CommandSpec) -> MessageResult:
        if not isinstance(command, CommandSpec):
            raise TypeError(
                f"expose_command expects a CommandSpec, got {type(command).__name__}"
            )
        if not self._capability.supports(PlatformOperation.EXPOSE_COMMAND):
            return MessageResult(
                command=command,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason="messaging is not supported",
            )
        if command.session_ref not in self._sessions:
            raise SessionNotFoundError(
                f"no session found for {command.session_ref!r}"
            )
        return MessageResult(command=command, mode=FallbackMode.NATIVE)

    def fallback_subagent(
        self,
        role_id: str,
        goal_context: GoalExecutionContextPackage,
        *,
        project_id: str,
        worker_id: str | None = None,
    ) -> SpawnResult:
        handle = self._spawn_handle(role_id, project_id, goal_context, worker_id)
        return SpawnResult(
            handle=handle,
            mode=FallbackMode.FALLBACK,
            fallback_reason="spawned through the explicit subagent fallback channel",
        )


def test_ac03_fallback_mode_is_a_typed_explicit_vocabulary():
    # FallbackMode is a typed StrEnum vocabulary; every result carries
    # exactly one member, so the fallback is part of the interface
    # contract and typed.
    assert issubclass(FallbackMode, str)
    assert {member.value for member in FallbackMode} == {
        "native",
        "fallback",
        "unsupported",
    }
    assert FallbackMode.NATIVE is FallbackMode("native")
    assert FallbackMode.FALLBACK is FallbackMode("fallback")
    assert FallbackMode.UNSUPPORTED is FallbackMode("unsupported")


def test_ac03_result_validation_enforces_the_explicit_answer_shapes():
    # The typed answer shapes are enforced by the records themselves:
    # a NATIVE result never carries a reason, a FALLBACK/UNSUPPORTED
    # answer always does, and an UNSUPPORTED answer never carries a
    # fabricated handle (AC-03: no silent degradation, no fake session).
    handle = MockPlatformAdapter().spawn_worker(
        "worker", make_context(), project_id=PROJECT_ID
    ).handle
    assert isinstance(handle, WorkerSessionHandle)
    with pytest.raises(PlatformAdapterDataError):
        SpawnResult(handle=handle, mode=FallbackMode.NATIVE, fallback_reason="x")
    with pytest.raises(PlatformAdapterDataError):
        SpawnResult(handle=None, mode=FallbackMode.FALLBACK)
    with pytest.raises(PlatformAdapterDataError):
        SpawnResult(handle=handle, mode=FallbackMode.UNSUPPORTED, fallback_reason="x")
    with pytest.raises(TypeError):
        SpawnResult(handle=None, mode=FallbackMode.NATIVE)
    ok = SpawnResult(
        handle=None,
        mode=FallbackMode.UNSUPPORTED,
        fallback_reason="persistent sessions are not supported",
    )
    assert ok.handle is None
    assert ok.mode is FallbackMode.UNSUPPORTED


def test_ac03_every_capability_has_a_defined_explicit_fallback_answer():
    # An adapter that declares NO native operations must answer every
    # interface capability with an explicit typed fallback answer: mode
    # in {FALLBACK, UNSUPPORTED} plus a fallback_reason -- never NATIVE,
    # never a fabricated session.
    adapter = MockPlatformAdapter(operations=())
    assert adapter.capabilities().operations == ()
    for operation in PlatformOperation:
        if operation is PlatformOperation.FALLBACK_SUBAGENT:
            continue  # the fallback path itself; covered below
        if operation is PlatformOperation.SPAWN_PERSISTENT_ROLE:
            result = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
        elif operation is PlatformOperation.SPAWN_WORKER:
            result = adapter.spawn_worker(
                "worker", make_context(), project_id=PROJECT_ID
            )
        elif operation is PlatformOperation.RESUME_SESSION:
            result = adapter.resume_session(
                generate_id("session", "mock", "w", "worker", PROJECT_ID)
            )
        elif operation is PlatformOperation.TERMINATE_SESSION:
            result = adapter.terminate_session(
                generate_id("session", "mock", "w", "worker", PROJECT_ID)
            )
        elif operation is PlatformOperation.IS_SESSION_ALIVE:
            result = adapter.is_session_alive(
                generate_id("session", "mock", "w", "worker", PROJECT_ID)
            )
        else:
            result = adapter.expose_command(
                CommandSpec(
                    session_ref=generate_id(
                        "session", "mock", "w", "worker", PROJECT_ID
                    ),
                    directive="report status",
                )
            )
        assert result.mode in (FallbackMode.FALLBACK, FallbackMode.UNSUPPORTED), (
            f"{operation.value} must be answered explicitly, got"
            f" {result.mode.value}"
        )
        assert isinstance(result.fallback_reason, str) and result.fallback_reason
        if result.mode is FallbackMode.UNSUPPORTED:
            assert result.to_dict().get("handle") is None
            assert result.to_dict().get("alive") is None


def test_ac03_callers_can_distinguish_real_results_from_fallback():
    # A caller can always tell a real platform result from a fallback by
    # the typed mode: NATIVE on the native path, FALLBACK with a reason
    # on the explicit fallback path, and the answers differ in their
    # payloads accordingly.
    adapter = MockPlatformAdapter()
    native = adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    assert native.mode is FallbackMode.NATIVE
    assert native.fallback_reason is None
    assert isinstance(native.handle, WorkerSessionHandle)
    fallback = adapter.fallback_subagent(
        "worker", make_context(run="r2"), project_id=PROJECT_ID
    )
    assert fallback.mode is FallbackMode.FALLBACK
    assert fallback.fallback_reason
    assert isinstance(fallback.handle, WorkerSessionHandle)
    assert fallback.handle.session_ref != native.handle.session_ref
    # Liveness: a real observation is a bool; an unsupported answer is
    # explicitly None (never a guess).
    alive = adapter.is_session_alive(native.handle.session_ref)
    assert alive.mode is FallbackMode.NATIVE
    assert alive.alive is True
    unsupported = MockPlatformAdapter(operations=()).is_session_alive(
        native.handle.session_ref
    )
    assert unsupported.mode is FallbackMode.UNSUPPORTED
    assert unsupported.alive is None
    assert unsupported.fallback_reason


def test_ac03_fallback_subagent_is_the_explicit_documented_fallback_path():
    # 15-ADAPTER-SPEC.md SS5 ``fallback_subagent(...)``: the explicit
    # fallback spawn path. It always reports FALLBACK with a reason --
    # a subagent fallback is never claimed as a native session.
    adapter = MockPlatformAdapter(operations=())
    result = adapter.fallback_subagent(
        "worker", make_context(), project_id=PROJECT_ID
    )
    assert result.mode is FallbackMode.FALLBACK
    assert result.fallback_reason
    assert isinstance(result.handle, WorkerSessionHandle)
    assert is_valid_id(result.handle.session_ref, kind="session")
    assert result.handle.role_id in CONTRACT_ROLE_IDS
    assert result.handle.goal_id == GOAL_ID


def test_ac03_capability_answers_are_distinct_from_data_errors():
    # A capability answer (typed result) and a data/state error
    # (ValueError-subclassed exception) are distinct: resume of a
    # session the platform never created raises SessionNotFoundError,
    # while an unsupported resume returns the typed UNSUPPORTED answer.
    adapter = MockPlatformAdapter()
    with pytest.raises(SessionNotFoundError):
        adapter.resume_session(
            generate_id("session", "mock", "w", "worker", PROJECT_ID, "nope")
        )
    with pytest.raises(SessionNotFoundError):
        adapter.is_session_alive(
            generate_id("session", "mock", "w", "worker", PROJECT_ID, "nope")
        )
    unsupported = MockPlatformAdapter(operations=()).resume_session(
        generate_id("session", "mock", "w", "worker", PROJECT_ID, "nope")
    )
    assert unsupported.mode is FallbackMode.UNSUPPORTED
    assert issubclass(SessionNotFoundError, ValueError)
    # Wrong input types raise TypeError at the public boundary.
    with pytest.raises(TypeError):
        adapter.expose_command("not a CommandSpec")
    with pytest.raises(TypeError):
        adapter.spawn_worker("worker", "not a context", project_id=PROJECT_ID)


def test_ac03_results_are_frozen_and_round_trip_typed():
    # Every operation result is a frozen validating record whose
    # serialization round-trips and preserves the typed mode; corrupt
    # state raises the stable PlatformAdapterDataError.
    adapter = MockPlatformAdapter()
    native = adapter.spawn_persistent_role("execution_monitor", PROJECT_ID)
    assert isinstance(native.handle, WorkerSessionHandle)
    assert dataclasses.is_dataclass(native)
    with pytest.raises(FrozenInstanceError):
        native.mode = FallbackMode.FALLBACK  # type: ignore[misc]
    restored = SpawnResult.from_dict(native.to_dict())
    assert restored == native
    assert restored.mode is FallbackMode.NATIVE
    assert restored.to_json() == native.to_json()
    worker = adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    assert isinstance(worker.handle, WorkerSessionHandle)
    # The 5 result records round-trip with their typed mode.
    assert ResumeResult.from_dict(
        adapter.resume_session(worker.handle.session_ref).to_dict()
    ).mode is FallbackMode.NATIVE
    assert AliveResult.from_dict(
        adapter.is_session_alive(worker.handle.session_ref).to_dict()
    ).alive is True
    message = CommandSpec(session_ref=worker.handle.session_ref, directive="go")
    assert MessageResult.from_dict(adapter.expose_command(message).to_dict()).mode is (
        FallbackMode.NATIVE
    )
    assert TerminateResult.from_dict(
        adapter.terminate_session(worker.handle.session_ref).to_dict()
    ).mode is FallbackMode.NATIVE
    with pytest.raises(PlatformAdapterDataError):
        SpawnResult.from_dict({})
    with pytest.raises(PlatformAdapterDataError):
        MessageResult.from_dict({"mode": "native"})
    with pytest.raises(TypeError):
        TerminateResult.from_dict("not a mapping")


def test_ac03_results_carry_no_wall_clock_fields():
    # Fallback answers carry no timestamp-like fields: the answer is a
    # pure typed record, repeatable and byte-identical.
    adapter = MockPlatformAdapter()
    result = adapter.spawn_worker("worker", make_context(), project_id=PROJECT_ID)
    keys = set(result.to_dict())
    assert not any(
        key in keys for key in ("timestamp", "created_at", "updated_at")
    )
    assert result.to_json() == result.to_json()
    assert json.loads(result.to_json()) == result.to_dict()
    # PlatformCapability is likewise a frozen, typed, round-trippable record.
    capability = adapter.capabilities()
    assert capability.supports(PlatformOperation.SPAWN_WORKER)
    partial = MockPlatformAdapter(
        operations=(PlatformOperation.SPAWN_WORKER,)
    ).capabilities()
    assert partial.supports(PlatformOperation.SPAWN_WORKER)
    assert not partial.supports(PlatformOperation.RESUME_SESSION)
    assert PlatformCapability.from_dict(capability.to_dict()) == capability
    assert dataclasses.is_dataclass(capability)
    with pytest.raises(FrozenInstanceError):
        capability.operations = ()  # type: ignore[misc]
