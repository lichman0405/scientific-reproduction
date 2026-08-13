"""The subagent/process spawn boundary of the Claude Code adapter (DEV-M10-G03).

The explicit fallback channel of 15-ADAPTER-SPEC.md SS5
(:meth:`PlatformAdapter.fallback_subagent`) maps onto Claude Code's
short-lived subagent/process invocation mechanism: a headless single-shot
run (``claude -p "<prompt>"`` style) that executes one bounded context
and returns -- the opposite of a persistent task-store session.

This module defines the hermetically mockable boundary the adapter talks
to (the tests fake this runner deterministically):

* :class:`ProcessRunner` -- the typed spawn operation the adapter needs;
* :class:`SubprocessSpawn` -- the frozen, typed outcome of one spawn;
* :class:`ClaudeSubprocessRunner` -- the thin real wrapper; its
  Claude-specific invocation is kept behind the boundary and not
  exercised in the deterministic test suite (no live process I/O).

A subagent fallback is always reported as ``FallbackMode.FALLBACK`` with
a reason naming the fallback channel (AC-03) -- never as a native
persistent session -- and the spawned worker carries the *same* canonical
``session_ref`` as a native spawn of the same logical context (AC-02:
replacement sessions reconstruct identity from the durable workspace).

Determinism: pure descriptor layer -- no wall clock, no randomness, no
process I/O outside the injected runner.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from scientific_reproduction.core.ids import generate_id, is_valid_id

#: ``generate_id`` kind of one subagent spawn record (a transport detail).
_SPAWN_KIND = "subagent_spawn"

__all__ = [
    "ClaudeSubprocessRunner",
    "ProcessRunner",
    "SubprocessSpawn",
    "derive_spawn_id",
]


def derive_spawn_id(session_ref: str) -> str:
    """The deterministic spawn record id of one subagent fallback.

    ``generate_id("subagent_spawn", session_ref)`` -- a pure function of
    the canonical ``session_ref`` (AC-02: the durable identity is the
    single source of every transport-level record).

    Raises:
        TypeError: ``session_ref`` is not a str.
        ValueError: ``session_ref`` is not a valid
            ``sr_session_<32 hex>`` reference.
    """
    if not isinstance(session_ref, str):
        raise TypeError(
            f"derive_spawn_id expects a session_ref str, got"
            f" {type(session_ref).__name__}"
        )
    if not is_valid_id(session_ref, kind="session"):
        raise ValueError(
            f"invalid session_ref {session_ref!r}: expected sr_session_<32 hex>"
        )
    return generate_id(_SPAWN_KIND, session_ref)


@dataclass(frozen=True)
class SubprocessSpawn:
    """The typed outcome of one subagent/process spawn.

    ``spawn_id`` -- deterministic record id (see :func:`derive_spawn_id`);
    ``accepted`` -- whether the runner accepted the spawn (a refused
    spawn is an explicit typed answer, never a silent degradation).
    """

    spawn_id: str
    accepted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.spawn_id, str):
            raise TypeError(
                "SubprocessSpawn.spawn_id must be a str, got"
                f" {type(self.spawn_id).__name__}"
            )
        if not is_valid_id(self.spawn_id, kind=_SPAWN_KIND):
            raise ValueError(
                f"invalid spawn_id {self.spawn_id!r}: expected"
                " sr_subagent_spawn_<32 hex>"
            )
        if not isinstance(self.accepted, bool):
            raise TypeError(
                "SubprocessSpawn.accepted must be a bool, got"
                f" {type(self.accepted).__name__}"
            )


class ProcessRunner(ABC):
    """The hermetically mockable subagent/process spawn boundary.

    ``spawn_subagent`` launches one short-lived headless run executing
    the given prompt text (rendered by the adapter from the canonical
    task bundle). The tests inject a scripted fake; the real runner
    keeps the process invocation behind the boundary.
    """

    @abstractmethod
    def spawn_subagent(self, session_ref: str, prompt: str) -> SubprocessSpawn:
        """Spawn one short-lived subagent run for ``session_ref``/``prompt``.

        ``session_ref`` is the durable canonical reference (the spawn
        record id derives from it, AC-02); ``prompt`` is the rendered
        task bundle. Returns the frozen typed outcome; a refused spawn
        carries ``accepted=False`` (the caller still gets the explicit
        FALLBACK answer with the durable handle -- the identity is
        preserved).
        """


class ClaudeSubprocessRunner(ProcessRunner):
    """The real short-lived subagent runner (thin wrapper).

    Maps the boundary operation onto Claude Code's headless single-shot
    invocation (``claude -p "<prompt>"`` style). In the deterministic
    runtime no live process invocation is run: the wrapper answers a
    typed refusal (``accepted=False``), and the actual process
    invocation stays behind the boundary, not exercised in the test
    suite; the boundary contract is pinned by the scripted fake.
    """

    #: The headless CLI prompt flag of the fallback channel. The prompt
    #: is rendered from the canonical task bundle by the adapter; the
    #: Core never embeds platform-specific command syntax
    #: (15-ADAPTER-SPEC.md SS5).
    PROMPT_FLAG = "-p"

    def spawn_subagent(self, session_ref: str, prompt: str) -> SubprocessSpawn:
        if not isinstance(session_ref, str) or not is_valid_id(
            session_ref, kind="session"
        ):
            raise ValueError(
                f"spawn_subagent expects a valid session_ref, got {session_ref!r}"
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                f"spawn_subagent expects a non-empty prompt str, got {prompt!r}"
            )
        return SubprocessSpawn(
            spawn_id=derive_spawn_id(session_ref), accepted=False
        )
