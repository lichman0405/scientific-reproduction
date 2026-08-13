"""The Agent Teams task-store boundary of the Claude Code adapter (DEV-M10-G03).

Claude Code's currently available mechanism for persistent roles and
goal-scoped workers is the **Agent Teams** feature (``worker_mode:
agent_team_task``; CLI-level, experimental, env-gated): a lead runtime
spawns in-process teammate sessions whose task list persists locally
(file-based, survives restarts) while the teammate sessions themselves
are in-process objects of the lead. The **known Agent Teams resume
limitation** (AC-03) is documented by Claude Code itself -- "No session
resumption with in-process teammates: ``/resume`` and ``/rewind`` do not
restore in-process teammates" -- i.e. an Agent Teams task-store session
is not resumable across process restarts the way a persistent runtime
session would be: the task record survives, but the live in-process
teammate session object is gone.

This module defines the hermetically mockable boundary the adapter talks
to (the tests fake this client deterministically -- precedent:
``tests/scenarios/`` M8-G06 fake the ``SSHTransport`` boundary):

* :class:`TeamStoreClient` -- the typed operations the adapter needs;
* :class:`TaskStoreProbe` -- the client's frozen, typed answer about one
  task id: whether the task record exists and whether a live in-process
  session is attached (the only state that can be resumed natively);
* :class:`TeamStoreUnavailableError` -- the typed refusal when the Agent
  SDK is not importable in this runtime; the adapter translates it into
  an explicit typed answer (AC-03), never a fabricated session;
* :class:`AgentTeamTaskStore` -- the thin real wrapper over the Agent
  SDK surface (``query``/``stop_task``). Its Claude-specific imports are
  dynamic and guarded, so the module imports and the adapter works when
  the SDK is absent -- the tests run without a live SDK. There is no
  programmatic agent-team task-store API in the current Agent SDK; the
  wrapper models the task-store surface it would build on.

Task identity is a *transport detail* (AC-02): :func:`derive_task_id` is
a deterministic pure function of the durable ``session_ref``
(``generate_id("team_task", session_ref)``), so the task record is
addressable across restarts, while identity never flows from the task
store to the Core -- the durable identity is the Core's
``WorkerSessionHandle.session_ref``, and a missing task record never
rewrites it.

Determinism: pure descriptor layer in the house style -- no wall clock,
no randomness, no I/O outside the injected client, frozen records.
"""

from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    PlatformAdapterError,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id

#: ``generate_id`` kind of the Agent Teams task id (a transport detail).
_TASK_ID_KIND = "team_task"

#: Root module of the Claude Agent SDK package (Claude-specific; this
#: module may import it inside the claude_code package only, and the
#: imports below are dynamic so the module imports without the SDK).
_AGENT_SDK_MODULE = "claude_agent_sdk"

__all__ = [
    "AgentTeamTaskStore",
    "TeamStoreClient",
    "TeamStoreUnavailableError",
    "TaskStoreProbe",
    "derive_task_id",
]


class TeamStoreUnavailableError(PlatformAdapterError):
    """The Agent Teams task store cannot be reached in this runtime.

    Raised by :class:`AgentTeamTaskStore` when the Agent SDK is not
    importable. The adapter translates this into an explicit typed
    answer (AC-03) -- never a fabricated session.
    """


def derive_task_id(session_ref: str) -> str:
    """The deterministic Agent Teams task id of one durable session.

    ``generate_id("team_task", session_ref)`` -- a pure function of the
    canonical ``session_ref``, so the task record is addressable across
    process restarts. The direction of the dependency matters (AC-02):
    the task id derives from the durable session identity, never the
    other way around -- the task store is a transport detail.

    Raises:
        TypeError: ``session_ref`` is not a str.
        PlatformAdapterDataError: ``session_ref`` is not a valid
            ``sr_session_<32 hex>`` reference.
    """
    if not isinstance(session_ref, str):
        raise TypeError(
            f"derive_task_id expects a session_ref str, got"
            f" {type(session_ref).__name__}"
        )
    if not is_valid_id(session_ref, kind="session"):
        raise PlatformAdapterDataError(
            f"invalid session_ref {session_ref!r}: expected sr_session_<32 hex>"
        )
    return generate_id(_TASK_ID_KIND, session_ref)


@dataclass(frozen=True)
class TaskStoreProbe:
    """The task store's typed answer about one task id.

    ``task_record_present`` -- the persisted task record exists (the
    Agent Teams task list is file-based and survives restarts);
    ``live_session_attached`` -- a live in-process task session object
    is attached in this runtime (the only state that can be resumed
    natively; after a lead-process restart it is always False -- the
    known Agent Teams limitation, AC-03).
    """

    task_record_present: bool
    live_session_attached: bool

    def __post_init__(self) -> None:
        if not isinstance(self.task_record_present, bool):
            raise TypeError(
                "TaskStoreProbe.task_record_present must be a bool, got"
                f" {type(self.task_record_present).__name__}"
            )
        if not isinstance(self.live_session_attached, bool):
            raise TypeError(
                "TaskStoreProbe.live_session_attached must be a bool, got"
                f" {type(self.live_session_attached).__name__}"
            )


class TeamStoreClient(ABC):
    """The hermetically mockable Agent Teams task-store boundary.

    The adapter composes this client for every spawn/resume/terminate/
    liveness/delivery operation; the tests inject a scripted fake
    (precedent: the M8-G06 ``SlurmClusterMock`` fakes the
    ``SSHTransport`` boundary of the real ``SSHComputeAdapter``).

    The operations are the minimal typed surface the adapter needs:

    * ``submit_task`` -- create/refresh the persisted task record for
      one task id carrying the task bundle;
    * ``probe`` -- what the task store knows about one task id;
    * ``stop_task`` -- stop the live in-process session (True if a live
      session was stopped; the name matches the Agent SDK's
      ``stop_task`` surface);
    * ``deliver`` -- deliver one directive to the live in-process
      session (True if delivered).
    """

    @abstractmethod
    def submit_task(self, task_id: str, bundle: dict[str, Any]) -> bool:
        """Create/refresh the persisted task record for ``task_id``.

        Returns True when the task record was accepted. ``bundle`` is
        the canonical task bundle (:func:`task_bundle.build_task_bundle`):
        the durable session identity plus the frozen role contract
        directives the task executes.
        """

    @abstractmethod
    def probe(self, task_id: str) -> TaskStoreProbe:
        """What the task store knows about ``task_id`` (frozen answer)."""

    @abstractmethod
    def stop_task(self, task_id: str) -> bool:
        """Stop the live in-process session of ``task_id``.

        Returns True when a live session was stopped, False when there
        was nothing live to stop (a task record alone cannot be stopped
        after the lead process exited -- the known limitation).
        """

    @abstractmethod
    def deliver(self, task_id: str, directive: str) -> bool:
        """Deliver one directive to the live in-process session.

        Returns True when the directive was delivered to a live session,
        False when no live session is attached in this runtime.
        """


class AgentTeamTaskStore(TeamStoreClient):
    """The real Agent Teams task-store client (thin SDK wrapper).

    Every Claude-specific import is dynamic and guarded
    (``importlib.import_module`` inside the methods): the module and the
    adapter import and function without the Agent SDK installed, exactly
    as the tests do. When the SDK is absent every operation raises
    :class:`TeamStoreUnavailableError` -- the adapter translates that
    into an explicit typed answer (AC-03), never a fabricated session.

    There is no programmatic agent-team task-store API in the current
    Agent SDK: the Agent Teams feature is CLI-level (experimental,
    env-gated) and its task list is file-based. The wrapper therefore
    models the task-store surface the feature would expose, over the
    Agent SDK session machinery the lead runtime is built on
    (``query(prompt=..., options=ClaudeAgentOptions(...))`` for the
    teammate runs and the ``stop_task(task_id)`` stop surface). The
    SDK-dependent calls are not exercised in the deterministic test
    suite (no live agent-team runtime); the boundary contract they
    implement is pinned by the scripted fake.
    """

    def __init__(self) -> None:
        self._sdk: Any = None

    def _load_sdk(self) -> Any:
        """Import the Agent SDK lazily; typed refusal when absent."""
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module(_AGENT_SDK_MODULE)
            except ImportError as exc:
                raise TeamStoreUnavailableError(
                    "the claude agent sdk is not importable in this runtime;"
                    " agent teams task-store operations are unavailable"
                ) from exc
        return self._sdk

    def submit_task(self, task_id: str, bundle: dict[str, Any]) -> bool:
        self._load_sdk()
        # With a live agent-team runtime this would create the persisted
        # task record and start the in-process teammate session for
        # ``task_id``. The call is not exercised in the deterministic
        # test suite (no live runtime).
        return False

    def probe(self, task_id: str) -> TaskStoreProbe:
        self._load_sdk()
        # With a live agent-team runtime this would read the file-based
        # task list for ``task_id`` and report the attached in-process
        # teammate session.
        return TaskStoreProbe(
            task_record_present=False, live_session_attached=False
        )

    def stop_task(self, task_id: str) -> bool:
        self._load_sdk()
        return False

    def deliver(self, task_id: str, directive: str) -> bool:
        self._load_sdk()
        return False
