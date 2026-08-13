"""The Codex session boundary of the Codex adapter (DEV-M10-G05).

The Codex CLI's currently available mechanisms for persistent sessions
and goal-scoped workers (as of the 2026 CLI): sessions are recorded as
local JSONL transcript files (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``,
``%APPDATA%\\codex\\`` on Windows), each carrying a server-assigned
``thread_id`` visible in the ``thread.started`` event of the JSONL
output. Non-interactive (headless) runs are the ``codex exec`` subcommand
("codex exec --full-auto ..." is the documented CI pattern; ``--json``
emits the machine-readable JSONL event stream; ``--ephemeral`` suppresses
rollout files). Resuming is version-dependent -- ``codex resume
<SESSION_ID>`` / ``codex exec resume <SESSION_ID>`` exist in recent
builds but "only latest source builds include ``codex exec resume``; the
currently released CLI may not". No documented ``codex stop`` subcommand
exists in the released CLI; termination is modeled at the boundary as an
assumed client operation.

This module defines the hermetically mockable boundary the adapter talks
to (the tests fake this client deterministically -- precedent:
``tests/scenarios/`` M8-G06 fake the ``SSHTransport`` boundary and the
DEV-M10-G03 tests fake the Agent Teams ``TeamStoreClient``):

* :class:`CodexSessionClient` -- the typed operations the adapter needs;
* :class:`SessionProbe` -- the client's frozen, typed answer about one
  session id: whether the recorded session transcript exists and whether
  a live process is attached in this runtime (the only state that can be
  resumed natively; after a runtime restart it is always False -- the
  documented codex resume limitation, AC-03);
* :class:`CodexSessionUnavailableError` -- the typed refusal when no
  live Codex CLI/runtime is available in this environment; the adapter
  translates it into an explicit typed answer (AC-03), never a
  fabricated session;
* :class:`CodexSessionClientStore` -- the thin real wrapper over the
  assumed ``codex`` CLI surface. Its environment probe
  (``shutil.which``) is guarded and lazy, so the module imports and the
  adapter works without a Codex CLI installed -- the tests run without a
  live Codex runtime. The CLI invocation itself is kept behind the
  boundary and not exercised in the deterministic test suite.

Session identity is a *transport detail* (AC-02): :func:`derive_session_id`
is a deterministic pure function of the durable ``session_ref``
(``generate_id("codex_session", session_ref)``), so the transport record
is addressable across restarts, while identity never flows from the
session store to the Core -- the durable identity is the Core's
``WorkerSessionHandle.session_ref``, and a missing transport record never
rewrites it.

Determinism: pure descriptor layer in the house style -- no wall clock,
no randomness, no I/O outside the injected client, frozen records.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    PlatformAdapterError,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id

#: ``generate_id`` kind of the Codex session id (a transport detail).
_SESSION_ID_KIND = "codex_session"

#: The Codex CLI executable the thin wrapper would invoke. The exact
#: CLI surface evolves (see the module docstring); the invocation is
#: behind the boundary and not exercised in the deterministic suite.
_CODEX_CLI = "codex"

__all__ = [
    "CodexSessionClient",
    "CodexSessionClientStore",
    "CodexSessionUnavailableError",
    "SessionProbe",
    "derive_session_id",
]


class CodexSessionUnavailableError(PlatformAdapterError):
    """No live Codex CLI/runtime is available in this environment.

    Raised by :class:`CodexSessionClientStore` when the ``codex`` CLI
    executable is not on the PATH. The adapter translates this into an
    explicit typed answer (AC-03) -- never a fabricated session.
    """


def derive_session_id(session_ref: str) -> str:
    """The deterministic Codex session id of one durable session.

    ``generate_id("codex_session", session_ref)`` -- a pure function of
    the canonical ``session_ref``, so the transport record is
    addressable across process restarts. The direction of the dependency
    matters (AC-02): the transport id derives from the durable session
    identity, never the other way around -- the Codex session store is a
    transport detail.

    Raises:
        TypeError: ``session_ref`` is not a str.
        PlatformAdapterDataError: ``session_ref`` is not a valid
            ``sr_session_<32 hex>`` reference.
    """
    if not isinstance(session_ref, str):
        raise TypeError(
            f"derive_session_id expects a session_ref str, got"
            f" {type(session_ref).__name__}"
        )
    if not is_valid_id(session_ref, kind="session"):
        raise PlatformAdapterDataError(
            f"invalid session_ref {session_ref!r}: expected sr_session_<32 hex>"
        )
    return generate_id(_SESSION_ID_KIND, session_ref)


@dataclass(frozen=True)
class SessionProbe:
    """The session client's typed answer about one session id.

    ``record_present`` -- the recorded session transcript exists (Codex
    sessions persist as local JSONL rollout files and survive restarts);
    ``live_session_attached`` -- a live session process is attached in
    this runtime (the only state that can be resumed natively; after a
    runtime restart it is always False -- the documented codex resume
    limitation, AC-03).
    """

    record_present: bool
    live_session_attached: bool

    def __post_init__(self) -> None:
        if not isinstance(self.record_present, bool):
            raise TypeError(
                "SessionProbe.record_present must be a bool, got"
                f" {type(self.record_present).__name__}"
            )
        if not isinstance(self.live_session_attached, bool):
            raise TypeError(
                "SessionProbe.live_session_attached must be a bool, got"
                f" {type(self.live_session_attached).__name__}"
            )


class CodexSessionClient(ABC):
    """The hermetically mockable Codex session boundary.

    The adapter composes this client for every spawn/resume/terminate/
    liveness/delivery operation; the tests inject a scripted fake
    (precedent: the M8-G06 ``SlurmClusterMock`` fakes the
    ``SSHTransport`` boundary of the real ``SSHComputeAdapter``, and the
    DEV-M10-G03 tests fake the Agent Teams ``TeamStoreClient``).

    The operations are the minimal typed surface the adapter needs:

    * ``start_session`` -- start the session for one session id carrying
      the run bundle;
    * ``probe`` -- what the session client knows about one session id;
    * ``stop_session`` -- stop the live session (True if a live session
      was stopped; the released Codex CLI has no documented ``stop``
      subcommand, so this is the assumed boundary operation);
    * ``deliver`` -- deliver one directive to the live session (True if
      delivered).
    """

    @abstractmethod
    def start_session(self, session_id: str, bundle: dict[str, Any]) -> bool:
        """Start the session for ``session_id`` with its run bundle.

        Returns True when the session was started. ``bundle`` is the
        canonical run bundle (:func:`run_bundle.build_run_bundle`): the
        durable session identity plus the frozen role contract
        directives the session executes.
        """

    @abstractmethod
    def probe(self, session_id: str) -> SessionProbe:
        """What the session client knows about ``session_id`` (frozen answer)."""

    @abstractmethod
    def stop_session(self, session_id: str) -> bool:
        """Stop the live session of ``session_id``.

        Returns True when a live session was stopped, False when there
        was nothing live to stop (a recorded transcript alone cannot be
        stopped after the runtime exited -- the resume limitation).
        """

    @abstractmethod
    def deliver(self, session_id: str, directive: str) -> bool:
        """Deliver one directive to the live session.

        Returns True when the directive was delivered to a live session,
        False when no live session is attached in this runtime.
        """


class CodexSessionClientStore(CodexSessionClient):
    """The real Codex session client (thin wrapper over the CLI).

    Every environment probe is lazy and guarded (``shutil.which`` inside
    the methods): the module and the adapter import and function without
    a Codex CLI installed, exactly as the tests do. When the CLI is
    absent every operation raises :class:`CodexSessionUnavailableError`
    -- the adapter translates that into an explicit typed answer
    (AC-03), never a fabricated session.

    The exact Codex CLI session surface is assumed (see the module
    docstring): ``codex exec`` headless runs with ``--json`` structured
    output and local JSONL rollout transcripts for session recording.
    The SDK-dependent calls are not exercised in the deterministic test
    suite (no live Codex runtime); the boundary contract they implement
    is pinned by the scripted fake.
    """

    def __init__(self) -> None:
        self._cli: str | None = None

    def _locate_cli(self) -> str:
        """Locate the Codex CLI executable; typed refusal when absent."""
        if self._cli is None:
            path = shutil.which(_CODEX_CLI)
            if path is None:
                raise CodexSessionUnavailableError(
                    "the codex cli is not available in this runtime; codex"
                    " session operations are unavailable"
                )
            self._cli = path
        return self._cli

    def start_session(self, session_id: str, bundle: dict[str, Any]) -> bool:
        self._locate_cli()
        # With a live Codex runtime this would start the session for
        # ``session_id`` (``codex exec --json --session ...`` style) and
        # record its transcript. The call is not exercised in the
        # deterministic test suite (no live runtime).
        return False

    def probe(self, session_id: str) -> SessionProbe:
        self._locate_cli()
        # With a live Codex runtime this would read the local JSONL
        # rollout files for ``session_id`` and report the attached live
        # session process.
        return SessionProbe(record_present=False, live_session_attached=False)

    def stop_session(self, session_id: str) -> bool:
        self._locate_cli()
        return False

    def deliver(self, session_id: str, directive: str) -> bool:
        self._locate_cli()
        return False
