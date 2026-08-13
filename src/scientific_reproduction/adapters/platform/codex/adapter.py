"""The Codex orchestration adapter (DEV-M10-G05).

A concrete :class:`PlatformAdapter` for Codex, implementing the
normalized orchestration contract of 15-ADAPTER-SPEC.md SS5 with
Codex's **currently available** mechanisms (v0.1 scope):

=============================  ==================================================
Interface operation            Codex mechanism
=============================  ==================================================
``spawn_persistent_role``      A persistent Codex session started through
                               the Codex session client; the session
                               transcript persists locally (JSONL rollout
                               files under ``~/.codex/sessions/`` /
                               ``%APPDATA%\\codex\\`` on Windows).
``spawn_worker``               A Codex session, same mechanism,
                               goal-scoped.
``resume_session``             Native attach when a live session process
                               is attached; otherwise the documented
                               Codex resume limitation applies (see
                               below) and the answer is an explicit
                               FALLBACK carrying the durable identity.
``terminate_session``          Stop of the live session through the
                               session client (the assumed boundary
                               operation; the released CLI has no
                               documented ``stop`` subcommand);
                               otherwise a durable termination record
                               (explicit FALLBACK).
``is_session_alive``           Observed liveness of the live session
                               process; never fabricated (explicit
                               UNSUPPORTED with ``alive=None`` when the
                               liveness is unobservable).
``expose_command``             Delivery into the live session; otherwise
                               the durable outbox (explicit FALLBACK --
                               the directive is preserved for the
                               replacement session).
``fallback_subagent``          Short-lived one-shot headless run
                               (``codex exec``, the documented
                               non-interactive mode with the
                               machine-readable ``--json`` output);
                               always an explicit FALLBACK answer.
=============================  ==================================================

The documented Codex resume limitation (AC-03)
----------------------------------------------
Codex sessions are not reliably resumable across process restarts:
sessions persist only as local JSONL transcripts, and headless
resumption (``codex resume <SESSION_ID>`` / ``codex exec resume``) is
version-dependent -- per the Codex CLI documentation "only latest source
builds include ``codex exec resume``; the currently released CLI may
not". This adapter reconciles the limitation explicitly:

* ``resume_session`` answers ``FallbackMode.FALLBACK`` with a
  ``fallback_reason`` naming the limitation when the session client
  cannot resume (the durable identity is still returned -- the Core's
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
workspace-side in :class:`SessionRegistry`. The Codex session store is a
**transport detail**: session ids derive from the ``session_ref`` (never
the reverse), a missing session-store record never rewrites the
identity, and every resume/replacement resolves against the registry --
rehydratable from a durable snapshot
(``SessionRegistry.from_records``), so a replacement adapter over the
same workspace answers the same identity without chat-memory access.

Orchestration transport only (AC-02)
-------------------------------------
This adapter is pure orchestration transport: it contains ZERO
scientific/domain logic and imports no scientific-core module
(no ``research``/``rules``/``state``/``analysis``/``monitor``/
``domain``/``execution`` internals) -- only the platform-neutral seams
the DEV-M10-G02/G03 adapters legitimately share (``core.ids``,
``core.models`` for the goal-context package type, the platform base and
the shared role contracts). The contract is delivered via
``WorkerSessionHandle``/``CommandSpec``/the five result records;
scientific semantics live in the Core. The structural boundary is pinned
by the AC-02 import scan in ``tests/platform/test_codex_adapter.py``.

Boundaries
----------
All real Codex interaction is behind two hermetically mockable
boundaries the tests fake deterministically (precedent: the M8-G06
``SlurmClusterMock`` fakes the ``SSHTransport`` boundary of the real
``SSHComputeAdapter``; the DEV-M10-G03 tests fake the Agent Teams
``TeamStoreClient``): :class:`CodexSessionClient` (the session surface)
and :class:`CodexProcessRunner` (the one-shot exec surface). Codex CLI
interaction lives only in this package, is lazy/guarded, and is not
exercised in the deterministic test suite (no live Codex runtime); the
real thin wrappers answer typed refusals (``CodexSessionUnavailableError``,
``accepted=False``) so the default adapter honors the full contract
explicitly -- never a fake success.

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
from scientific_reproduction.adapters.platform.codex.process_runner import (
    CodexExecRunner,
    CodexProcessRunner,
)
from scientific_reproduction.adapters.platform.codex.run_bundle import (
    build_run_bundle,
    render_exec_prompt,
)
from scientific_reproduction.adapters.platform.codex.session_client import (
    CodexSessionClient,
    CodexSessionClientStore,
    CodexSessionUnavailableError,
    SessionProbe,
    derive_session_id,
)
from scientific_reproduction.adapters.platform.codex.session_registry import (
    SessionRecord,
    SessionRegistry,
    SessionState,
)
from scientific_reproduction.adapters.platform.contracts.base import (
    CONTRACT_ROLE_IDS,
    get_role_contract,
)
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import GoalExecutionContextPackage

__all__ = [
    "ALIVE_CLIENT_UNAVAILABLE_REASON",
    "ALIVE_UNOBSERVABLE_REASON",
    "CODEX_ADAPTER_VERSION",
    "CODEX_PLATFORM_ID",
    "CodexPlatformAdapter",
    "COMMAND_OUTBOX_REASON",
    "REPLACE_CLIENT_UNAVAILABLE_REASON",
    "RESUME_CLIENT_UNAVAILABLE_REASON",
    "RESUME_LIMITATION_RECORD_MISSING_REASON",
    "RESUME_LIMITATION_REASON",
    "SPAWN_CLIENT_UNAVAILABLE_REASON",
    "SUBAGENT_FALLBACK_REASON",
    "SUBAGENT_UNAVAILABLE_REASON",
    "TERMINATED_ALREADY_REASON",
    "TERMINATED_COMMAND_REASON",
    "TERMINATED_REPLACE_REASON",
    "TERMINATED_RESUME_REASON",
    "TERMINATE_CLIENT_UNAVAILABLE_REASON",
    "TERMINATE_FALLBACK_REASON",
]

#: The platform backend id of this adapter (safe lowercase identifier).
CODEX_PLATFORM_ID: str = "codex"

#: Version of this adapter's capability record.
CODEX_ADAPTER_VERSION: str = "1.0"

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
    "the one-shot codex exec fallback channel is unavailable in this"
    " runtime; the durable session identity is preserved"
)

#: A spawn could not be created in the session client (CLI/runtime absent).
SPAWN_CLIENT_UNAVAILABLE_REASON: str = (
    "the codex session client is unavailable in this runtime; no session"
    " was spawned"
)

#: The documented Codex resume limitation (AC-03): the session record
#: exists but no live session process is attached.
RESUME_LIMITATION_REASON: str = (
    "codex sessions are not resumable across process restarts in this"
    " runtime; the session record exists but no live session is attached"
)

#: The documented Codex resume limitation with the session record
#: missing: the durable session identity is preserved and unchanged
#: (AC-02).
RESUME_LIMITATION_RECORD_MISSING_REASON: str = (
    "codex sessions are not resumable across process restarts and the"
    " session record is missing from the session client; the durable"
    " session identity is preserved unchanged"
)

#: The session client could not even be probed (CLI/runtime absent); the
#: durable session identity is preserved and unchanged.
RESUME_CLIENT_UNAVAILABLE_REASON: str = (
    "the codex session client is unavailable in this runtime; the session"
    " cannot be resumed natively and the durable session identity is"
    " preserved unchanged"
)

#: A terminated session is never resumed.
TERMINATED_RESUME_REASON: str = "the session was terminated and cannot be resumed"

#: A terminated session is never replaced.
TERMINATED_REPLACE_REASON: str = (
    "the session was terminated; spawn a fresh session for a new logical"
    " context instead"
)

#: Liveness is unobservable without a live session process; no liveness
#: guess is fabricated (AC-03).
ALIVE_UNOBSERVABLE_REASON: str = (
    "the liveness of a codex session cannot be observed without a live"
    " session process; no liveness is fabricated"
)

#: The session client could not even be probed; no liveness guess is
#: fabricated (AC-03).
ALIVE_CLIENT_UNAVAILABLE_REASON: str = (
    "the codex session client is unavailable in this runtime; liveness"
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
    "no live session to deliver to; the command is recorded in the"
    " durable outbox for the replacement session"
)

#: No live session to stop: the termination is recorded durably.
TERMINATE_FALLBACK_REASON: str = (
    "no live session to stop; the termination is recorded in the durable"
    " session registry"
)

#: The session client could not even be reached: the termination is
#: recorded durably.
TERMINATE_CLIENT_UNAVAILABLE_REASON: str = (
    "the codex session client is unavailable in this runtime; the"
    " termination is recorded in the durable session registry"
)

#: A second terminate of an already-terminated session (idempotent).
TERMINATED_ALREADY_REASON: str = "the session was already terminated"

#: A replacement could not be spawned; the durable identity is preserved.
REPLACE_CLIENT_UNAVAILABLE_REASON: str = (
    "the codex session client is unavailable in this runtime; a"
    " replacement could not be spawned and the durable session identity"
    " is preserved"
)


class CodexPlatformAdapter(PlatformAdapter):
    """The concrete Codex orchestration adapter (DEV-M10-G05).

    Implements the normalized orchestration contract of
    15-ADAPTER-SPEC.md SS5 over Codex's currently available mechanisms
    (session-client sessions for persistent roles and goal-scoped
    workers; the one-shot headless ``codex exec`` run for the explicit
    fallback channel), with the documented Codex resume limitation
    reconciled explicitly (AC-03) and the durable identity kept in the
    workspace-side :class:`SessionRegistry`, never in the session store
    (AC-02).

    The three collaborators are injectable -- the hermetically mockable
    boundaries the tests fake deterministically: :class:`CodexSessionClient`
    (the Codex session store), :class:`CodexProcessRunner` (the one-shot
    exec channel) and :class:`SessionRegistry` (the durable identity
    map, rehydratable from a workspace snapshot). With the defaults, in
    a runtime without a live Codex CLI, every session-backed operation
    answers an explicit typed FALLBACK/UNSUPPORTED (AC-03) -- never a
    fabricated session.
    """

    platform_id = CODEX_PLATFORM_ID
    version = CODEX_ADAPTER_VERSION
    description = (
        "Codex orchestration adapter: codex session-client sessions for"
        " persistent roles and goal-scoped workers, with the explicit"
        " one-shot headless exec fallback channel (DEV-M10-G05)"
    )

    def __init__(
        self,
        *,
        session_client: CodexSessionClient | None = None,
        process_runner: CodexProcessRunner | None = None,
        registry: SessionRegistry | None = None,
    ) -> None:
        self._session_client = (
            session_client
            if session_client is not None
            else CodexSessionClientStore()
        )
        self._process_runner = (
            process_runner if process_runner is not None else CodexExecRunner()
        )
        self._registry = registry if registry is not None else SessionRegistry()

    # -- capability (AC-03 anchor) -------------------------------------------

    def capabilities(self) -> PlatformCapability:
        """The typed capability record of the Codex adapter.

        All six orchestration operations are natively supported (the
        session-client machinery can perform them); ``fallback_subagent``
        is the explicit fallback channel itself and is deliberately not a
        native capability -- its answer is always FALLBACK. State
        dependent answers (e.g. resume of a session the session client
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

    def _start_session(self, handle: WorkerSessionHandle) -> bool:
        """Start the session-client record of one durable identity.

        True when the session client accepted the session; False when
        the client is unavailable or refused (the caller answers the
        explicit typed UNSUPPORTED). The session id derives from the
        canonical ``session_ref`` (AC-02: the session store is a
        transport detail).
        """
        contract = get_role_contract(handle.role_id)
        bundle = build_run_bundle(handle, contract)
        session_id = derive_session_id(handle.session_ref)
        try:
            return bool(self._session_client.start_session(session_id, bundle))
        except CodexSessionUnavailableError:
            return False

    def spawn_persistent_role(self, role_id: str, project_id: str) -> SpawnResult:
        """Spawn one project-persistent role as a Codex session (AC-01).
        The worker id is the role id itself (deterministic per role;
        e.g. ``execution_monitor``).
        """
        role = self._checked_role_id("spawn_persistent_role", role_id)
        project = self._checked_project_id("spawn_persistent_role", project_id)
        handle = WorkerSessionHandle(
            platform_id=self.platform_id,
            worker_id=role,
            role_id=role,
            project_id=project,
        )
        if not self._start_session(handle):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=SPAWN_CLIENT_UNAVAILABLE_REASON,
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
        """Spawn one goal-scoped worker as a Codex session (AC-01). The
        worker id is the caller's, or the deterministic ``worker_role``
        value of the goal context.
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
        if not self._start_session(handle):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=SPAWN_CLIENT_UNAVAILABLE_REASON,
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
        the goal-scoped worker as a short-lived one-shot exec run.

        Always answered ``FallbackMode.FALLBACK`` with a reason naming
        the channel -- never a native persistent session. The spawned
        worker carries the *same* canonical ``session_ref`` as a native
        spawn of the same logical context (AC-02): a replacement
        one-shot run reconstructs from the shared workspace with the
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
        prompt = render_exec_prompt(build_run_bundle(handle, contract))
        spawn = self._process_runner.spawn_run(handle.session_ref, prompt)
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

    def _probe(self, session_ref: str) -> SessionProbe | None:
        """The session-client probe of one ref; None when the client is
        unavailable (the caller answers the explicit typed fallback)."""
        try:
            return self._session_client.probe(derive_session_id(session_ref))
        except CodexSessionUnavailableError:
            return None

    def resume_session(self, session_ref: str) -> ResumeResult:
        """Resume a session by its durable reference (AC-03).

        NATIVE when a live session process is attached (the session
        client can resume it); otherwise FALLBACK with a
        ``fallback_reason`` naming the documented Codex resume
        limitation -- the durable identity is still returned, never
        nullified by the session store (AC-02).

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
                fallback_reason=RESUME_CLIENT_UNAVAILABLE_REASON,
            )
        if probe.live_session_attached:
            return ResumeResult(handle=record.handle, mode=FallbackMode.NATIVE)
        if probe.record_present:
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

        Creates a new session in the session client for the **same**
        durable identity: the replacement carries the same canonical
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
        if not self._start_session(record.handle):
            return SpawnResult(
                handle=None,
                mode=FallbackMode.UNSUPPORTED,
                fallback_reason=REPLACE_CLIENT_UNAVAILABLE_REASON,
            )
        # The registry record is intentionally untouched: the identity
        # and its durable outbox are preserved (AC-02).
        return SpawnResult(handle=record.handle, mode=FallbackMode.NATIVE)

    # -- termination / liveness / delivery -----------------------------------

    def terminate_session(self, session_ref: str) -> TerminateResult:
        """Stop a session by its durable reference (AC-03).

        NATIVE when a live session was stopped; FALLBACK with a reason
        when there was no live session to stop -- the termination is
        recorded in the durable session registry, so a terminated
        session is never resumed or replaced.

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
            stopped = self._session_client.stop_session(
                derive_session_id(record.session_ref)
            )
        except CodexSessionUnavailableError:
            self._registry.mark_terminated(record.session_ref)
            return TerminateResult(
                session_ref=record.session_ref,
                mode=FallbackMode.FALLBACK,
                fallback_reason=TERMINATE_CLIENT_UNAVAILABLE_REASON,
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
        session process is attached, ``alive=False`` exactly for a
        durably terminated session. When the liveness is unobservable
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
                fallback_reason=ALIVE_CLIENT_UNAVAILABLE_REASON,
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

        NATIVE when the directive was delivered into the live session;
        FALLBACK when there is no live session -- the directive is
        recorded in the durable outbox (the adapter-defined fallback
        channel) and preserved for the replacement session
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
            delivered = self._session_client.deliver(
                derive_session_id(record.session_ref), command.directive
            )
        except CodexSessionUnavailableError:
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
