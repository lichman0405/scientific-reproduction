"""The one-shot exec boundary of the Codex adapter (DEV-M10-G05).

The explicit fallback channel of 15-ADAPTER-SPEC.md SS5
(:meth:`PlatformAdapter.fallback_subagent`) maps onto the Codex CLI's
short-lived headless run mechanism: ``codex exec`` in non-interactive
mode ("codex exec --full-auto ..." is the documented CI pattern; the
``--json`` flag emits the machine-readable JSONL event stream whose
``thread.started`` event carries the session ``thread_id``). A one-shot
exec run executes one bounded context and returns -- the opposite of a
persistent session.

This module defines the hermetically mockable boundary the adapter talks
to (the tests fake this runner deterministically):

* :class:`CodexProcessRunner` -- the typed spawn operation the adapter
  needs;
* :class:`CodexRunResult` -- the frozen, typed outcome of one exec run;
* :class:`CodexExecRunner` -- the thin real wrapper; its CLI invocation
  is kept behind the boundary and not exercised in the deterministic
  test suite (no live process I/O).

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

#: ``generate_id`` kind of one one-shot exec run record (a transport
#: detail).
_RUN_KIND = "codex_run"

__all__ = [
    "CodexExecRunner",
    "CodexProcessRunner",
    "CodexRunResult",
    "derive_run_id",
]


def derive_run_id(session_ref: str) -> str:
    """The deterministic run record id of one one-shot exec fallback.

    ``generate_id("codex_run", session_ref)`` -- a pure function of the
    canonical ``session_ref`` (AC-02: the durable identity is the single
    source of every transport-level record).

    Raises:
        TypeError: ``session_ref`` is not a str.
        ValueError: ``session_ref`` is not a valid
            ``sr_session_<32 hex>`` reference.
    """
    if not isinstance(session_ref, str):
        raise TypeError(
            f"derive_run_id expects a session_ref str, got"
            f" {type(session_ref).__name__}"
        )
    if not is_valid_id(session_ref, kind="session"):
        raise ValueError(
            f"invalid session_ref {session_ref!r}: expected sr_session_<32 hex>"
        )
    return generate_id(_RUN_KIND, session_ref)


@dataclass(frozen=True)
class CodexRunResult:
    """The typed outcome of one one-shot exec run.

    ``run_id`` -- deterministic record id (see :func:`derive_run_id`);
    ``accepted`` -- whether the runner accepted the run (a refused run
    is an explicit typed answer, never a silent degradation).
    """

    run_id: str
    accepted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                "CodexRunResult.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not is_valid_id(self.run_id, kind=_RUN_KIND):
            raise ValueError(
                f"invalid run_id {self.run_id!r}: expected sr_codex_run_<32 hex>"
            )
        if not isinstance(self.accepted, bool):
            raise TypeError(
                "CodexRunResult.accepted must be a bool, got"
                f" {type(self.accepted).__name__}"
            )


class CodexProcessRunner(ABC):
    """The hermetically mockable one-shot exec boundary.

    ``spawn_run`` launches one short-lived headless ``codex exec`` run
    executing the given prompt text (rendered by the adapter from the
    canonical run bundle). The tests inject a scripted fake; the real
    runner keeps the process invocation behind the boundary.
    """

    @abstractmethod
    def spawn_run(self, session_ref: str, prompt: str) -> CodexRunResult:
        """Spawn one short-lived one-shot run for ``session_ref``/``prompt``.

        ``session_ref`` is the durable canonical reference (the run
        record id derives from it, AC-02); ``prompt`` is the rendered
        run bundle. Returns the frozen typed outcome; a refused run
        carries ``accepted=False`` (the caller still gets the explicit
        FALLBACK answer with the durable handle -- the identity is
        preserved).
        """


class CodexExecRunner(CodexProcessRunner):
    """The real one-shot exec runner (thin wrapper).

    Maps the boundary operation onto the Codex CLI's non-interactive
    headless invocation (``codex exec`` with the machine-readable
    ``--json`` output mode; the ``--full-auto`` approval bypass is
    deliberately NOT assumed -- approval policy belongs to project/user
    configuration, 15-ADAPTER-SPEC.md SS6). In the deterministic runtime
    no live process invocation is run: the wrapper answers a typed
    refusal (``accepted=False``), and the actual process invocation
    stays behind the boundary, not exercised in the test suite; the
    boundary contract is pinned by the scripted fake.
    """

    #: The headless exec subcommand of the fallback channel. The prompt
    #: is rendered from the canonical run bundle by the adapter; the
    #: Core never embeds platform-specific command syntax
    #: (15-ADAPTER-SPEC.md SS5).
    EXEC_SUBCOMMAND = "exec"

    #: The structured-output flag of the headless run (JSONL event
    #: stream; version-dependent).
    JSON_FLAG = "--json"

    def spawn_run(self, session_ref: str, prompt: str) -> CodexRunResult:
        if not isinstance(session_ref, str) or not is_valid_id(
            session_ref, kind="session"
        ):
            raise ValueError(
                f"spawn_run expects a valid session_ref, got {session_ref!r}"
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                f"spawn_run expects a non-empty prompt str, got {prompt!r}"
            )
        return CodexRunResult(run_id=derive_run_id(session_ref), accepted=False)
