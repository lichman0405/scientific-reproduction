"""The Claude Code orchestration adapter (DEV-M10-G03).

A concrete :class:`PlatformAdapter` for Claude Code, implementing the
normalized orchestration contract of 15-ADAPTER-SPEC.md SS5 with
Claude Code's **currently available** mechanisms (v0.1 scope):

=============================  ==================================================
Interface operation            Claude Code mechanism
=============================  ==================================================
``spawn_persistent_role``      Agent Teams task (`worker_mode:
                               ``agent_team_task``): the persistent role
                               runs as an in-process teammate session of
                               the Agent Teams lead runtime; the task
                               record persists in the file-based task
                               list.
``spawn_worker``               Agent Teams task, same mechanism,
                               goal-scoped.
``resume_session``             In-process attach when a live teammate
                               session object exists; otherwise the known
                               Agent Teams limitation applies (see below)
                               and the answer is an explicit FALLBACK
                               carrying the durable identity.
``terminate_session``          Stop of the live in-process teammate
                               session (the SDK ``stop_task`` surface);
                               otherwise a durable termination record
                               (explicit FALLBACK).
``is_session_alive``           Observed liveness of the live in-process
                               teammate session; never fabricated
                               (explicit UNSUPPORTED with ``alive=None``
                               when the liveness is unobservable).
``expose_command``             Delivery into the live in-process
                               teammate session; otherwise the durable
                               outbox (explicit FALLBACK -- the
                               directive is preserved for the
                               replacement session).
``fallback_subagent``          Short-lived subagent/process invocation
                               (``claude -p`` headless single-shot run);
                               always an explicit FALLBACK answer.
=============================  ==================================================

The known Agent Teams resume limitation (AC-03)
-----------------------------------------------
Agent Teams sessions are **not resumable across process restarts** the
way a persistent runtime session would be: the Agent Teams task list is
file-based and survives, but the live in-process teammate session
object -- the only thing a native resume could attach to -- is gone
(Claude Code's own documentation: "/resume ... do not restore
in-process teammates"). This adapter reconciles the limitation
explicitly:

* ``resume_session`` answers ``FallbackMode.FALLBACK`` with a
  ``fallback_reason`` naming the limitation when the task store cannot
  resume (the durable identity is still returned -- the Core's
  ``session_ref`` is never nullified);
* ``is_session_alive`` never fabricates liveness: it answers NATIVE only
  with an observed boolean, and ``UNSUPPORTED`` with ``alive=None`` when
  the liveness is unobservable;
* ``replace_session`` and ``fallback_subagent`` create a replacement
  session that carries the **same canonical ``session_ref``** and
  reconstructs from the shared workspace (13-EXECUTION-MONITOR.md SS4).

Durable identity (AC-02)
------------------------
The durable session identity is the Core's
``WorkerSessionHandle.session_ref`` (deterministic
``generate_id("session", ...)`` from DEV-M10-G02), reflected
workspace-side in :class:`SessionRegistry`. The Agent Teams task store
is a **transport detail**: task ids derive from the ``session_ref``
(never the reverse), a missing task-store record never rewrites the
identity, and every resume/replacement resolves against the registry --
rehydratable from a durable snapshot
(``SessionRegistry.from_records``), so a replacement adapter over the
same workspace answers the same identity without chat-memory access.

Boundaries
----------
All real Claude-specific interaction is behind two hermetically mockable
boundaries the tests fake deterministically (precedent: the M8-G06
``SlurmClusterMock`` fakes the ``SSHTransport`` boundary of the real
``SSHComputeAdapter``): :class:`TeamStoreClient` (the Agent Teams
task-store surface) and :class:`ProcessRunner` (the subagent/process
surface). Claude-specific SDK imports live only in this package and are
dynamic/guarded, so the module imports and the adapter works without the
Agent SDK.

Determinism: pure descriptor layer in the house style -- no wall clock,
no randomness, no hidden I/O; every answer is a pure function of the
inputs and the injected fakes.
"""

from __future__ import annotations

import re
from typing import Any

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
from scientific_reproduction.adapters.platform.claude_code.process_runner import (
    ClaudeSubprocessRunner,
    ProcessRunner,
)
from scientific_reproduction.adapters.platform.claude_code.session_registry import (
    SessionRecord,
    SessionRegistry,
    SessionState,
)
from scientific_reproduction.adapters.platform.claude_code.task_bundle import (
    build_task_bundle,
    render_subagent_prompt,
)
from scientific_reproduction.adapters.platform.claude_code.team_store import (
    AgentTeamTaskStore,
    TaskStoreProbe,
    TeamStoreClient,
    TeamStoreUnavailableError,
    derive_task_id,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    CONTRACT_ROLE_IDS,
    get_role_contract,
)
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import GoalExecutionContextPackage

__all__ = [
    "ALIVE_STORE_UNAVAILABLE_REASON",
    "ALIVE_UNOBSERVABLE_REASON",
    "CLAUDE_CODE_ADAPTER_VERSION",
    "CLAUDE_CODE_PLATFORM_ID",
    "ClaudeCodePlatformAdapter",
    "COMMAND_OUTBOX_REASON",
    "REPLACE_STORE_UNAVAILABLE_REASON",
    "RESUME_LIMITATION_RECORD_MISSING_REASON",
    "RESUME_LIMITATION_REASON",
    "RESUME_STORE_UNAVAILABLE_REASON",
    "SPAWN_STORE_UNAVAILABLE_REASON",
    "SUBAGENT_FALLBACK_REASON",
    "SUBAGENT_UNAVAILABLE_REASON",
    "TERMINATED_ALREADY_REASON",
    "TERMINATED_COMMAND_REASON",
    "TERMINATED_REPLACE_REASON",
    "TERMINATED_RESUME_REASON",
    "TERMINATE_FALLBACK_REASON",
    "TERMINATE_STORE_UNAVAILABLE_REASON",
]

#: The platform backend id of this adapter (safe lowercase identifier).
CLAUDE_CODE_PLATFORM_ID: str = "claude_code"

#: Version of this adapter's capability record.
CLAUDE_CODE_ADAPTER_VERSION: str = "1.0"

#: Valid worker id/name shape (same contract as the interface).
_WORKER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

# ---------------------------------------------------------------------------
# Stable fallback reasons (AC-03: explicit, non-empty, named)
# ---------------------------------------------------------------------------

#: ``fallback_subagent`` always names the explicit subagent channel.
SUBAGENT_FALLBACK_REASON: str = (
    "spawned through the explicit subagent fallback channel"
)

#: The subagent channel refused the spawn (no live invocation in the
#: deterministic runtime); the durable identity is preserved.
SUBAGENT_UNAVAILABLE_REASON: str = (
    "the subagent fallback channel is unavailable in this runtime; the"
    " durable session identity is preserved"
)

#: A spawn could not be created in the task store (SDK/runtime absent).
SPAWN_STORE_UNAVAILABLE_REASON: str = (
    "the agent teams task store is unavailable in this runtime; no session"
    " was spawned"
)

#: The known Agent Teams resume limitation (AC-03): the task record
#: exists but the live in-process session object is gone.
RESUME_LIMITATION_REASON: str = (
    "agent teams task-store sessions are not resumable across process"
    " restarts; the task record exists but no live in-process session is"
    " attached in this runtime"
)

#: The known Agent Teams resume limitation with the task record missing:
#: the durable session identity is preserved and unchanged (AC-02).
RESUME_LIMITATION_RECORD_MISSING_REASON: str = (
    "agent teams task-store sessions are not resumable across process"
    " restarts and the task record is missing from the task store; the"
    " durable session identity is preserved unchanged"
)

#: The task store could not even be probed (SDK/runtime absent); the
#: durable session identity is preserved and unchanged.
RESUME_STORE_UNAVAILABLE_REASON: str = (
    "the agent teams task store is unavailable in this runtime; the"
    " session cannot be resumed natively and the durable session identity"
    " is preserved unchanged"
)

#: A terminated session is never resumed.
TERMINATED_RESUME_REASON: str = "the session was terminated and cannot be resumed"

#: A terminated session is never replaced.
TERMINATED_REPLACE_REASON: str = (
    "the session was terminated; spawn a fresh session for a new logical"
    " context instead"
)

#: Liveness is unobservable without a live in-process session; no
#: liveness guess is fabricated (AC-03).
ALIVE_UNOBSERVABLE_REASON: str = (
    "the liveness of an agent teams task-store session cannot be observed"
    " without a live in-process session; no liveness is fabricated"
)

#: The task store could not even be probed; no liveness guess is
#: fabricated (AC-03).
ALIVE_STORE_UNAVAILABLE_REASON: str = (
    "the agent teams task store is unavailable in this runtime; liveness"
    " cannot be observed and no liveness is fabricated"
)

#: A command is never delivered to a terminated session.
TERMINATED_COMMAND_REASON: str = (
    "the session was terminated; the command was not delivered"
)

#: No live session to deliver to: the directive is preserved in the
#: durable outbox for the replacement session (the adapter-defined
#: fallback channel, 13-EXECUTION-MONITOR.md SS4).
COMMAND_OUTBOX_REASON: str = (
    "no live in-process session to deliver to; the command is recorded in"
    " the durable outbox for the replacement session"
)

#: No live session to stop: the termination is recorded durably.
TERMINATE_FALLBACK_REASON: str = (
    "no live in-process session to stop; the termination is recorded in"
    " the durable session registry"
)

#: The task store could not even be reached: the termination is recorded
#: durably.
TERMINATE_STORE_UNAVAILABLE_REASON: str = (
    "the agent teams task store is unavailable in this runtime; the"
    " termination is recorded in the durable session registry"
)

#: A second terminate of an already-terminated session (idempotent).
TERMINATED_ALREADY_REASON: str = "the session was already terminated"

#: A replacement could not be spawned; the durable identity is preserved.
REPLACE_STORE_UNAVAILABLE_REASON: str = (
    "the agent teams task store is unavailable in this runtime; a"
    " replacement could not be spawned and the durable session identity"
    " is preserved"
)


class ClaudeCodePlatformAdapter(PlatformAdapter):
    """The concrete Claude Code orchestration adapter (DEV-M10-G03).

    Implements the normalized orchestration contract of
    15-ADAPTER-SPEC.md SS5 over Claude Code's currently available
    mechanisms (Agent Teams task-store sessions for persistent roles and
    goal-scoped workers; the subagent/process invocation for the
    explicit fallback channel), with the known Agent Teams resume
    limitation reconciled explicitly (AC-03) and the durable identity
    kept in the workspace-side :class:`SessionRegistry`, never in the
    task store (AC-02).

    The three collaborators are injectable -- the hermetically mockable
    boundaries the tests fake deterministically: :class:`TeamStoreClient`
    (the Agent Teams task store), :class:`ProcessRunner` (the subagent
    channel) and :class:`SessionRegistry` (the durable identity map,
    rehydratable from a workspace snapshot). With the defaults, in a
    runtime without the Agent SDK, every store-backed operation answers
    an explicit typed FALLBACK/UNSUPPORTED (AC-03) -- never a fabricated
    session.
    """

    platform_id = CLAUDE_CODE_PLATFORM_ID
    version = CLAUDE_CODE_ADAPTER_VERSION
    description = (
        "Claude Code orchestration adapter: Agent Teams task-store sessions"
        " for persistent roles and goal-scoped workers, with the explicit"
        " subagent/process fallback channel (DEV-M10-G03)"
    )

    def __init__(
        self,
        *,
        team_store: TeamStoreClient | None = None,
        process_runner: ProcessRunner | None = None,
        registry: SessionRegistry | None = None,
    ) -> None:
        self._team_store = team_store if team_store is not None else AgentTeamTaskStore()
        self._process_runner = (
            process_runner if process_runner is not None else ClaudeSubprocessRunner()
        )
        self._registry = registry if registry is not None else SessionRegistry()

    # -- capability (AC-03 anchor) -------------------------------------------

    def capabilities(self) -> PlatformCapability:
        """The typed capability record of the Claude Code adapter.

        All six orchestration operations are natively supported (the
        task-store machinery can perform them); ``fallback_subagent`` is
        the explicit fallback channel itself and is deliberately not a
        native capability -- its answer is always FALLBACK. State
        dependent answers (e.g. resume of a session the task store
        cannot resume) are reported per operation through the typed
        result modes (AC-03), never through the capability record.
        """
        return PlatformCapability(
            platform_id=self.platform_id,
            version=self.version,
            description=self.description,
            operations=(
                PlatformOperation.SPAWN_PERSISTENT_ROLE,
                PlatformOperation.SPAWN_WORKER,
                PlatformOperation.RESUME_SESSION,
                PlatformOperation.TERMINATE_SESSION,
                PlatformOperation.IS_SESSION_ALIVE,
                PlatformOperation.EXPOSE_COMMAND,
            ),
        )

    # -- validation helpers --------------------------------------------------

    def _checked_session_ref(self, method: str, session_ref: Any) -> str:
        if not isinstance(session_ref, str):
            raise TypeError(
                f"{method} expects a session_ref str, got"
                f" {type(session_ref).__name__}"
            )
        if not is_valid_id(session_ref, kind="session"):
            raise PlatformAdapterDataError(
                f"invalid session_ref {session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        return session_ref

    def _checked_role_id(self, method: str, role_id: Any) -> str:
        if not isinstance(role_id, str):
            raise TypeError(
                f"{method} expects a role_id str, got {type(role_id).__name__}"
            )
        if role_id not in CONTRACT_ROLE_IDS:
            raise PlatformAdapterDataError(
                f"unknown role_id {role_id!r}; expected one of:"
                f" {', '.join(CONTRACT_ROLE_IDS)}"
            )
        return role_id

    def _checked_project_id(self, method: str, project_id: Any) -> str:
        if not isinstance(project_id, str):
            raise TypeError(
                f"{method} expects a project_id str, got"
                f" {type(project_id).__name__}"
            )
        if not is_valid_id(project_id, kind="project"):
            raise PlatformAdapterDataError(
                f"invalid project_id {project_id!r}: expected"
                " sr_project_<32 hex>"
            )
        return project_id

    def _checked_worker_id(self, method: str, worker_id: Any) -> str | None:
        if worker_id is None:
            return None
        if not isinstance(worker_id, str):
            raise TypeError(
                f"{method} expects a worker_id str or None, got"
                f" {type(worker_id).__name__}"
            )
        if not _WORKER_ID_PATTERN.fullmatch(worker_id):
            raise PlatformAdapterDataError(
                f"invalid worker_id {worker_id!r}: expected"
                " ^[a-z][a-z0-9_-]*$"
            )
        return worker_id

    # -- spawn machinery (AC-01) ---------------------------------------------

    def _submit_task(self, handle: WorkerSessionHandle) -> bool:
        """Create/refresh the task-store record of one durable identity.

        True when the task store accepted the task; False when the store
        is unavailable or refused (the caller answers the explicit
        typed UNSUPPORTED). The task id derives from the canonical
        ``session_ref`` (AC-02: the task store is a transport detail).
        """
        contract = get_role_contract(handle.role_id)
        bundle = build_task_bundle(handle, contract)
        task_id = derive_task_id(handle.session_ref)
        try:
            return bool(self._team_store.submit_task(task_id, bundle))
        except TeamStoreUnavailableError:
            return False

    def spawn_persistent_role(self, role_id: str, project_id: str) -> SpawnResult:
        """Spawn one project-persistent role as an Agent Teams task-store
        session (AC-01). The worker id is the role id itself
        (deterministic per role; e.g. ``execution_monitor``).
        """
        role = self._checked_role_id("spawn_persistent_role", role_id)
        project = self._checked_project_id("spawn_persistent_role", project_id)
        handle = WorkerSessionHandle(
            platform_id=self.platform_id,
            worker_id=role,
            role_id=role,
            project_id=project,
        )
        if not self._submit_task(handle):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=SPAWN_STORE_UNAVAILABLE_REASON,
            )
        self._registry.put(handle)
        return SpawnResult(handle=handle, mode=FallbackMode.NATIVE)

    def spawn_worker(
        self,
        role_id: str,
        goal_context: GoalExecutionContextPackage,
        *,
        project_id: str,
        worker_id: str | None = None,
    ) -> SpawnResult:
        """Spawn one goal-scoped worker as an Agent Teams task-store
        session (AC-01). The worker id is the caller's, or the
        deterministic ``worker_role`` value of the goal context.
        """
        role = self._checked_role_id("spawn_worker", role_id)
        project = self._checked_project_id("spawn_worker", project_id)
        if not isinstance(goal_context, GoalExecutionContextPackage):
            raise TypeError(
                "spawn_worker expects a GoalExecutionContextPackage, got"
                f" {type(goal_context).__name__}"
            )
        worker = self._checked_worker_id("spawn_worker", worker_id)
        handle = WorkerSessionHandle(
            platform_id=self.platform_id,
            worker_id=worker or goal_context.worker_role.value,
            role_id=role,
            project_id=project,
            goal_id=goal_context.goal_id,
            context_id=goal_context.context_id,
        )
        if not self._submit_task(handle):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=SPAWN_STORE_UNAVAILABLE_REASON,
            )
        self._registry.put(handle)
        return SpawnResult(handle=handle, mode=FallbackMode.NATIVE)

    def fallback_subagent(
        self,
        role_id: str,
        goal_context: GoalExecutionContextPackage,
        *,
        project_id: str,
        worker_id: str | None = None,
    ) -> SpawnResult:
        """The explicit fallback spawn path (15-ADAPTER-SPEC.md SS5):
        the goal-scoped worker as a short-lived subagent run.

        Always answered ``FallbackMode.FALLBACK`` with a reason naming
        the channel -- never a native persistent session. The spawned
        worker carries the *same* canonical ``session_ref`` as a native
        spawn of the same logical context (AC-02): a replacement
        subagent reconstructs from the shared workspace with the
        identity unchanged.
        """
        role = self._checked_role_id("fallback_subagent", role_id)
        project = self._checked_project_id("fallback_subagent", project_id)
        if not isinstance(goal_context, GoalExecutionContextPackage):
            raise TypeError(
                "fallback_subagent expects a GoalExecutionContextPackage,"
                f" got {type(goal_context).__name__}"
            )
        worker = self._checked_worker_id("fallback_subagent", worker_id)
        handle = WorkerSessionHandle(
            platform_id=self.platform_id,
            worker_id=worker or goal_context.worker_role.value,
            role_id=role,
            project_id=project,
            goal_id=goal_context.goal_id,
            context_id=goal_context.context_id,
        )
        if self._registry.is_terminated(handle.session_ref):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=TERMINATED_REPLACE_REASON,
            )
        contract = get_role_contract(role)
        prompt = render_subagent_prompt(build_task_bundle(handle, contract))
        spawn = self._process_runner.spawn_subagent(handle.session_ref, prompt)
        self._registry.put(handle)
        if spawn.accepted:
            return SpawnResult(
                handle=handle,
                mode=FallbackMode.FALLBACK,
                fallback_reason=SUBAGENT_FALLBACK_REASON,
            )
        return SpawnResult(
            handle=handle,
            mode=FallbackMode.FALLBACK,
            fallback_reason=SUBAGENT_UNAVAILABLE_REASON,
        )

    # -- resume / replacement (AC-02, AC-03) ---------------------------------

    def _resolve_record(self, method: str, session_ref: Any) -> SessionRecord:
        checked = self._checked_session_ref(method, session_ref)
        record = self._registry.get(checked)
        if record is None:
            raise SessionNotFoundError(f"no session found for {checked!r}")
        return record

    def _probe(self, session_ref: str) -> TaskStoreProbe | None:
        """The task-store probe of one ref; None when the store is
        unavailable (the caller answers the explicit typed fallback)."""
        try:
            return self._team_store.probe(derive_task_id(session_ref))
        except TeamStoreUnavailableError:
            return None

    def resume_session(self, session_ref: str) -> ResumeResult:
        """Resume a session by its durable reference (AC-03).

        NATIVE when a live in-process task session is attached (the task
        store can resume it); otherwise FALLBACK with a
        ``fallback_reason`` naming the known Agent Teams limitation --
        the durable identity is still returned, never nullified by the
        task store (AC-02).

        Raises:
            TypeError: ``session_ref`` is not a str.
            PlatformAdapterDataError: ``session_ref`` is not a valid
                ``sr_session_<32 hex>`` reference.
            SessionNotFoundError: no session exists for that reference
                (the durable registry holds no identity for it; the
                caller must spawn a replacement, SS4).
        """
        record = self._resolve_record("resume_session", session_ref)
        if record.state is SessionState.TERMINATED:
            return ResumeResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=TERMINATED_RESUME_REASON,
            )
        probe = self._probe(record.session_ref)
        if probe is None:
            return ResumeResult(
                handle=record.handle,
                mode=FallbackMode.FALLBACK,
                fallback_reason=RESUME_STORE_UNAVAILABLE_REASON,
            )
        if probe.live_session_attached:
            return ResumeResult(handle=record.handle, mode=FallbackMode.NATIVE)
        if probe.task_record_present:
            return ResumeResult(
                handle=record.handle,
                mode=FallbackMode.FALLBACK,
                fallback_reason=RESUME_LIMITATION_REASON,
            )
        return ResumeResult(
            handle=record.handle,
            mode=FallbackMode.FALLBACK,
            fallback_reason=RESUME_LIMITATION_RECORD_MISSING_REASON,
        )

    def replace_session(self, session_ref: str) -> SpawnResult:
        """The explicit replacement path (13-EXECUTION-MONITOR.md SS4).

        Creates a new session in the task store for the **same** durable
        identity: the replacement carries the same canonical
        ``session_ref`` (the value the Core stores in
        ``Run.worker_session_ref`` never changes, AC-02) and
        reconstructs its context from the shared workspace -- including
        the durable outbox preserved for it.

        Raises:
            TypeError: ``session_ref`` is not a str.
            PlatformAdapterDataError: ``session_ref`` is not a valid
                ``sr_session_<32 hex>`` reference.
            SessionNotFoundError: no session exists for that reference.
        """
        record = self._resolve_record("replace_session", session_ref)
        if record.state is SessionState.TERMINATED:
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=TERMINATED_REPLACE_REASON,
            )
        if not self._submit_task(record.handle):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=REPLACE_STORE_UNAVAILABLE_REASON,
            )
        # The registry record is intentionally untouched: the identity
        # and its durable outbox are preserved (AC-02).
        return SpawnResult(handle=record.handle, mode=FallbackMode.NATIVE)

    # -- termination / liveness / delivery -----------------------------------

    def terminate_session(self, session_ref: str) -> TerminateResult:
        """Stop a session by its durable reference (AC-03).

        NATIVE when a live in-process task session was stopped; FALLBACK
        with a reason when there was no live session to stop -- the
        termination is recorded in the durable session registry, so a
        terminated session is never resumed or replaced.

        Raises:
            TypeError: ``session_ref`` is not a str.
            PlatformAdapterDataError: ``session_ref`` is not a valid
                ``sr_session_<32 hex>`` reference.
            SessionNotFoundError: no session exists for that reference.
        """
        record = self._resolve_record("terminate_session", session_ref)
        if record.state is SessionState.TERMINATED:
            return TerminateResult(
                session_ref=record.session_ref,
                mode=FallbackMode.FALLBACK,
                fallback_reason=TERMINATED_ALREADY_REASON,
            )
        try:
            stopped = self._team_store.stop_task(derive_task_id(record.session_ref))
        except TeamStoreUnavailableError:
            self._registry.mark_terminated(record.session_ref)
            return TerminateResult(
                session_ref=record.session_ref,
                mode=FallbackMode.FALLBACK,
                fallback_reason=TERMINATE_STORE_UNAVAILABLE_REASON,
            )
        self._registry.mark_terminated(record.session_ref)
        if stopped:
            return TerminateResult(
                session_ref=record.session_ref, mode=FallbackMode.NATIVE
            )
        return TerminateResult(
            session_ref=record.session_ref,
            mode=FallbackMode.FALLBACK,
            fallback_reason=TERMINATE_FALLBACK_REASON,
        )

    def is_session_alive(self, session_ref: str) -> AliveResult:
        """Check whether a session is alive (AC-03: never fabricated).

        NATIVE with an observed boolean only: ``alive=True`` when a live
        in-process task session is attached, ``alive=False`` exactly for
        a durably terminated session. When the liveness is unobservable
        the answer is the explicit UNSUPPORTED with ``alive=None`` --
        no liveness guess is ever fabricated.

        Raises:
            TypeError: ``session_ref`` is not a str.
            PlatformAdapterDataError: ``session_ref`` is not a valid
                ``sr_session_<32 hex>`` reference.
            SessionNotFoundError: no session exists for that reference.
        """
        record = self._resolve_record("is_session_alive", session_ref)
        if record.state is SessionState.TERMINATED:
            return AliveResult(
                session_ref=record.session_ref,
                alive=False,
                mode=FallbackMode.NATIVE,
            )
        probe = self._probe(record.session_ref)
        if probe is None:
            return AliveResult(
                session_ref=record.session_ref,
                alive=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=ALIVE_STORE_UNAVAILABLE_REASON,
            )
        if probe.live_session_attached:
            return AliveResult(
                session_ref=record.session_ref,
                alive=True,
                mode=FallbackMode.NATIVE,
            )
        return AliveResult(
            session_ref=record.session_ref,
            alive=None,
            mode=FallbackMode.UNSUPPORTED,
            fallback_reason=ALIVE_UNOBSERVABLE_REASON,
        )

    def expose_command(self, command: CommandSpec) -> MessageResult:
        """Expose one platform-neutral command to a session (AC-03).

        NATIVE when the directive was delivered into the live in-process
        task session; FALLBACK when there is no live session -- the
        directive is recorded in the durable outbox (the adapter-defined
        fallback channel) and preserved for the replacement session
        (13-EXECUTION-MONITOR.md SS4).

        Raises:
            TypeError: ``command`` is not a :class:`CommandSpec`.
            SessionNotFoundError: no session exists for the command's
                ``session_ref``.
        """
        if not isinstance(command, CommandSpec):
            raise TypeError(
                f"expose_command expects a CommandSpec, got"
                f" {type(command).__name__}"
            )
        record = self._resolve_record("expose_command", command.session_ref)
        if record.state is SessionState.TERMINATED:
            return MessageResult(
                command=command,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=TERMINATED_COMMAND_REASON,
            )
        try:
            delivered = self._team_store.deliver(
                derive_task_id(record.session_ref), command.directive
            )
        except TeamStoreUnavailableError:
            self._registry.add_pending_command(record.session_ref, command.directive)
            return MessageResult(
                command=command,
                mode=FallbackMode.FALLBACK,
                fallback_reason=COMMAND_OUTBOX_REASON,
            )
        if delivered:
            return MessageResult(command=command, mode=FallbackMode.NATIVE)
        self._registry.add_pending_command(record.session_ref, command.directive)
        return MessageResult(
            command=command,
            mode=FallbackMode.FALLBACK,
            fallback_reason=COMMAND_OUTBOX_REASON,
        )
