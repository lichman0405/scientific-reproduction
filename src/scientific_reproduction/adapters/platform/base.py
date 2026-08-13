"""Platform-neutral orchestration interface (DEV-M10-G02).

Defines the **PlatformAdapter interface** deliverable: the only platform
surface the Core may touch for spawning, resuming, stopping and
messaging agent sessions, grounded in the frozen specs:

* ``15-ADAPTER-SPEC.md`` SS5 ("PlatformAdapter"): the normalized
  orchestration contract is ``spawn_persistent_role(role,
  project_context)``, ``spawn_worker(role, goal_context)``,
  ``resume_session(session_ref)``,
  ``terminate_session(session_ref)``, ``is_session_alive(session_ref)``,
  ``expose_command(command_spec)`` and ``fallback_subagent(...)``, and
  "The Core must not embed platform-specific command syntax".
* ``13-EXECUTION-MONITOR.md`` SS3-SS4: a Monitor session is resumed
  through a platform adapter capability; if resume fails a replacement
  session is created that "must be able to take over without chat-memory
  access", reconstructing from shared workspace, checkpoint, event log
  and external truth. The durable session identity this module defines
  makes that take-over mechanical: the canonical ``session_ref`` is a
  deterministic function of the logical worker/role context, so a
  replacement session of the same context carries the same ref and the
  project state (``Run.worker_session_ref``) never changes.
* ``03-ROLE-AND-PERMISSION-SPEC.md`` via the frozen role contract
  vocabulary (DEV-M10-G01): every spawn names a contract ``role_id``
  (``CONTRACT_ROLE_IDS``), never a platform-specific role.

Platform neutrality (AC-01)
---------------------------
This module is the ONLY platform surface the Core may import, and it is
deliberately free of any Claude-specific (anthropic/claude-code) import:
it imports only the stdlib and scientific-reproduction modules
(``core.ids``, ``core.models``, the platform-neutral role contracts).
The Core therefore never imports a Claude-specific task API directly --
spawn/resume/stop/message all flow through ``PlatformAdapter``.

Durable session/worker identity (AC-02)
---------------------------------------
Every spawn/resume returns a frozen :class:`WorkerSessionHandle`
carrying the durable identity the project state needs: the canonical
``session_ref`` (deterministic ``generate_id("session", ...)`` -- the
value a Run record stores in its ``worker_session_ref``), the platform
backend id, the worker id/name, the frozen contract role id and the
project/goal/context references. The handle serializes canonically and
round-trips losslessly through the state backend / event log.

Explicit fallback mode (AC-03)
------------------------------
When a platform capability is unavailable the caller always gets an
explicit, typed answer, never a silent degradation. Every operation
returns a frozen result record carrying a :class:`FallbackMode` member:
``NATIVE`` (the platform performed the operation itself),
``FALLBACK`` (the operation was performed through the explicit fallback
path -- :meth:`PlatformAdapter.fallback_subagent` or an adapter-defined
fallback channel) or ``UNSUPPORTED`` (the capability is unavailable; the
result carries no fabricated handle). A ``fallback_reason`` explains the
FALLBACK/UNSUPPORTED answer. The contract rule is enforced by tests:
an operation the adapter's :meth:`PlatformAdapter.capabilities` does not
list as natively supported MUST be answered FALLBACK or UNSUPPORTED --
never NATIVE, never a fake session, never an exception masquerading as
a capability answer (M10-G05 AC-03: "Unavailable live capability is
reported as capability/fallback, not silently faked").

Determinism
-----------
Pure descriptor layer in the house style: frozen records, StrEnum
vocabulary, ``TypeError`` at the public boundaries,
``ValueError``-subclassed stable errors, deterministic ids via
``core.ids.generate_id``, canonical sorted serialization with lossless
round-trip, and no wall clock, no randomness, no I/O anywhere in this
module.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Mapping

from scientific_reproduction.adapters.platform.contracts.base import (
    CONTRACT_ROLE_IDS,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import GoalExecutionContextPackage

__all__ = [
    "AliveResult",
    "CommandSpec",
    "FallbackMode",
    "MessageResult",
    "PLATFORM_ADAPTER_CONTRACT_VERSION",
    "PlatformAdapter",
    "PlatformAdapterDataError",
    "PlatformAdapterError",
    "PlatformCapability",
    "PlatformOperation",
    "ResumeResult",
    "SessionNotFoundError",
    "SpawnResult",
    "TerminateResult",
    "WorkerSessionHandle",
    "derive_worker_session_ref",
]

#: Version of the platform orchestration contract. Bumped whenever a
#: contract rule or vocabulary changes; every handle and capability
#: record is validated against the same version's rules.
PLATFORM_ADAPTER_CONTRACT_VERSION: str = "1.0"

#: ``generate_id`` kind of the canonical durable session reference.
_SESSION_REF_KIND = "session"

#: ``generate_id`` kind of the canonical command/message reference.
_COMMAND_REF_KIND = "command"

#: Valid platform backend id shape: a safe lowercase identifier usable
#: as a lookup key on every platform (e.g. ``claude_code``, ``codex``).
_PLATFORM_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Valid worker id/name shape: a safe identifier; hyphens are allowed
#: because platform-generated worker names (e.g. ``teammate-3``) use them.
_WORKER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Valid version shape (``major.minor``).
_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class PlatformAdapterError(ValueError):
    """Base error of the platform orchestration subsystem.

    Every message is stable: it names the failing operation and the
    reason, so callers and tests can rely on it.
    """


class PlatformAdapterDataError(PlatformAdapterError):
    """Raised when the adapter receives malformed orchestration data.

    Covers invalid role ids, invalid session/command/project/goal
    references and corrupt records rehydrated from the state backend or
    event log.
    """


class SessionNotFoundError(PlatformAdapterError):
    """Raised when an operation refers to an unknown session reference.

    This is a data/state error, distinct from the capability answer: a
    session the adapter never created (or already replaced) is not a
    fallback mode -- it is a broken reference the caller must resolve.
    """


# ---------------------------------------------------------------------------
# Fallback vocabulary (AC-03)
# ---------------------------------------------------------------------------


class FallbackMode(StrEnum):
    """How one platform operation was answered (AC-03).

    Every operation result carries exactly one member, so a caller can
    always distinguish a real platform result from a fallback:

    ``NATIVE`` -- the platform performed the operation itself (a real
    persistent session was spawned/resumed/stopped, liveness was
    observed, the command was delivered through the session);
    ``FALLBACK`` -- the operation was performed through the explicit
    fallback path (:meth:`PlatformAdapter.fallback_subagent` or an
    adapter-defined fallback channel) and is reported as such, with a
    ``fallback_reason``;
    ``UNSUPPORTED`` -- the capability is unavailable; the caller gets
    this typed refusal with a ``fallback_reason`` and never a fabricated
    session or a silent degradation.
    """

    NATIVE = "native"
    FALLBACK = "fallback"
    UNSUPPORTED = "unsupported"


class PlatformOperation(StrEnum):
    """One operation of the normalized orchestration contract.

    The frozen vocabulary of 15-ADAPTER-SPEC.md SS5. An adapter's
    :class:`PlatformCapability` lists which of these it supports
    natively; every other member must be answered explicitly as
    FALLBACK or UNSUPPORTED (AC-03).
    """

    SPAWN_PERSISTENT_ROLE = "spawn_persistent_role"
    SPAWN_WORKER = "spawn_worker"
    RESUME_SESSION = "resume_session"
    TERMINATE_SESSION = "terminate_session"
    IS_SESSION_ALIVE = "is_session_alive"
    EXPOSE_COMMAND = "expose_command"
    FALLBACK_SUBAGENT = "fallback_subagent"


def _check_fallback(mode: Any, fallback_reason: Any) -> None:
    """Validate the shared (mode, fallback_reason) pair of one result.

    ``NATIVE`` results carry no reason; ``FALLBACK``/``UNSUPPORTED``
    results must carry a non-empty reason so the caller always knows
    what degraded and why (AC-03: never silent).
    """
    if not isinstance(mode, FallbackMode):
        raise TypeError(
            f"mode must be a FallbackMode member, got {type(mode).__name__}"
        )
    if mode is FallbackMode.NATIVE:
        if fallback_reason is not None:
            raise PlatformAdapterDataError(
                "a NATIVE result must not carry a fallback_reason"
            )
    elif not isinstance(fallback_reason, str) or not fallback_reason.strip():
        raise PlatformAdapterDataError(
            f"a {mode.value} result must carry a non-empty fallback_reason"
        )


def derive_worker_session_ref(
    platform_id: str,
    worker_id: str,
    role_id: str,
    project_id: str,
    goal_id: str | None = None,
    context_id: str | None = None,
) -> str:
    """The canonical durable session reference of one logical worker.

    ``generate_id("session", platform_id, worker_id, role_id,
    project_id, goal_id, context_id)`` -- a pure deterministic function
    of the logical worker/role context, so the same worker on the same
    goal/context always derives the same ``sr_session_<32 hex>``
    reference. That is what makes the identity durable enough for
    project state (AC-02): the value stored in ``Run.worker_session_ref``
    is stable across process restarts and across Monitor replacement
    sessions (13-EXECUTION-MONITOR.md SS4 -- a brand-new session takes
    over without chat-memory access, and the state keeps one reference).
    """
    return generate_id(
        _SESSION_REF_KIND,
        platform_id,
        worker_id,
        role_id,
        project_id,
        goal_id or "",
        context_id or "",
    )


# ---------------------------------------------------------------------------
# Capability record (AC-03 anchor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformCapability:
    """The typed capability record of one platform adapter (AC-03).

    Declares which :class:`PlatformOperation` members the adapter
    supports natively. The interface contract is: any operation NOT
    listed here must be answered by the adapter as an explicit
    FALLBACK or UNSUPPORTED result with a ``fallback_reason`` -- never a
    NATIVE result, never a fabricated session, never a silent
    degradation (M10-G05 AC-03).
    """

    platform_id: str
    version: str
    description: str = ""
    operations: tuple[PlatformOperation, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.platform_id, str):
            raise TypeError(
                "PlatformCapability.platform_id must be a str, got"
                f" {type(self.platform_id).__name__}"
            )
        if not _PLATFORM_ID_PATTERN.fullmatch(self.platform_id):
            raise PlatformAdapterDataError(
                "PlatformCapability.platform_id must match"
                " ^[a-z][a-z0-9_]*$, got"
                f" {self.platform_id!r}"
            )
        if not isinstance(self.version, str):
            raise TypeError(
                "PlatformCapability.version must be a str, got"
                f" {type(self.version).__name__}"
            )
        if not _VERSION_PATTERN.fullmatch(self.version):
            raise PlatformAdapterDataError(
                f"PlatformCapability.version must match ^\\d+\\.\\d+$, got"
                f" {self.version!r}"
            )
        if not isinstance(self.description, str):
            raise TypeError(
                "PlatformCapability.description must be a str, got"
                f" {type(self.description).__name__}"
            )
        if not isinstance(self.operations, tuple):
            raise TypeError(
                "PlatformCapability.operations must be a tuple of"
                f" PlatformOperation, got {type(self.operations).__name__}"
            )
        for operation in self.operations:
            if not isinstance(operation, PlatformOperation):
                raise TypeError(
                    "PlatformCapability.operations entries must be"
                    " PlatformOperation members, got"
                    f" {type(operation).__name__}"
                )

    def supports(self, operation: PlatformOperation) -> bool:
        """True iff ``operation`` is declared as natively supported."""
        return operation in self.operations

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the capability in canonical field order."""
        return {
            "platform_id": self.platform_id,
            "version": self.version,
            "description": self.description,
            "operations": [op.value for op in self.operations],
        }

    def to_json(self) -> str:
        """Canonical deterministic JSON of the capability."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PlatformCapability:
        """Build a capability from a plain dict (corrupt state is a
        stable PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "PlatformCapability.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name
            for name in ("platform_id", "version", "description", "operations")
            if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt platform capability record: missing required"
                f" field(s): {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                platform_id=data["platform_id"],
                version=data["version"],
                description=data["description"],
                operations=tuple(
                    PlatformOperation(value) for value in data["operations"]
                ),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                "corrupt platform capability record for platform"
                f" {data.get('platform_id')!r}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The worker session handle (AC-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerSessionHandle:
    """The durable session/worker identity of one spawned session (AC-02).

    A frozen record carrying exactly the identity the project state
    needs: the canonical ``session_ref`` (deterministic
    ``sr_session_<32 hex>`` -- the value a Run record stores in its
    ``worker_session_ref``), the platform backend id, the worker id/name
    on the platform, the frozen contract role id (DEV-M10-G01) and the
    project/goal/context references that ground it.

    ``session_ref`` is computed, never caller-supplied: it is a pure
    function of ``(platform_id, worker_id, role_id, project_id,
    goal_id, context_id)`` (see :func:`derive_worker_session_ref`), so
    the same logical worker context always yields the same reference --
    resuming, restarting or replacing the session never changes the
    identity the project state holds.

    Attributes:
        platform_id: platform backend id (e.g. ``"claude_code"``,
            ``"codex"``) -- ``^[a-z][a-z0-9_]*$``.
        worker_id: the worker's id/name on the platform
            (``^[a-z][a-z0-9_-]*$``).
        role_id: frozen contract role id (one of ``CONTRACT_ROLE_IDS``).
        project_id: the project the session belongs to
            (``sr_project_<32 hex>``).
        goal_id: the goal context of a worker session; None for
            project-persistent roles (``sr_goal_<32 hex>`` when set).
        context_id: the worker-context package id of a goal-scoped
            worker session; distinguishes sessions of different runs of
            one goal (``sr_worker-context_<32 hex>`` when set).
        session_ref: computed canonical durable reference.
    """

    platform_id: str
    worker_id: str
    role_id: str
    project_id: str
    goal_id: str | None = None
    context_id: str | None = None
    session_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.platform_id, str):
            raise TypeError(
                "WorkerSessionHandle.platform_id must be a str, got"
                f" {type(self.platform_id).__name__}"
            )
        if not _PLATFORM_ID_PATTERN.fullmatch(self.platform_id):
            raise PlatformAdapterDataError(
                "WorkerSessionHandle.platform_id must match"
                " ^[a-z][a-z0-9_]*$, got"
                f" {self.platform_id!r}"
            )
        if not isinstance(self.worker_id, str):
            raise TypeError(
                "WorkerSessionHandle.worker_id must be a str, got"
                f" {type(self.worker_id).__name__}"
            )
        if not _WORKER_ID_PATTERN.fullmatch(self.worker_id):
            raise PlatformAdapterDataError(
                "WorkerSessionHandle.worker_id must match"
                " ^[a-z][a-z0-9_-]*$, got"
                f" {self.worker_id!r}"
            )
        if not isinstance(self.role_id, str):
            raise TypeError(
                "WorkerSessionHandle.role_id must be a str, got"
                f" {type(self.role_id).__name__}"
            )
        if self.role_id not in CONTRACT_ROLE_IDS:
            raise PlatformAdapterDataError(
                f"unknown role_id {self.role_id!r}; expected one of:"
                f" {', '.join(CONTRACT_ROLE_IDS)}"
            )
        if not isinstance(self.project_id, str):
            raise TypeError(
                "WorkerSessionHandle.project_id must be a str, got"
                f" {type(self.project_id).__name__}"
            )
        if not is_valid_id(self.project_id, kind="project"):
            raise PlatformAdapterDataError(
                f"invalid project_id {self.project_id!r}: expected"
                " sr_project_<32 hex>"
            )
        if self.goal_id is not None and not isinstance(self.goal_id, str):
            raise TypeError(
                "WorkerSessionHandle.goal_id must be a str or None, got"
                f" {type(self.goal_id).__name__}"
            )
        if self.goal_id is not None and not is_valid_id(self.goal_id, kind="goal"):
            raise PlatformAdapterDataError(
                f"invalid goal_id {self.goal_id!r}: expected sr_goal_<32 hex>"
            )
        if self.context_id is not None and not isinstance(self.context_id, str):
            raise TypeError(
                "WorkerSessionHandle.context_id must be a str or None, got"
                f" {type(self.context_id).__name__}"
            )
        if self.context_id is not None and not is_valid_id(
            self.context_id, kind="worker-context"
        ):
            raise PlatformAdapterDataError(
                f"invalid context_id {self.context_id!r}: expected"
                " sr_worker-context_<32 hex>"
            )
        if self.session_ref:
            raise PlatformAdapterDataError(
                "WorkerSessionHandle.session_ref is computed from the"
                " identity fields; pass none"
            )
        # Frozen dataclass: the computed reference is set once at
        # construction.
        object.__setattr__(
            self,
            "session_ref",
            derive_worker_session_ref(
                self.platform_id,
                self.worker_id,
                self.role_id,
                self.project_id,
                self.goal_id,
                self.context_id,
            ),
        )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the handle in canonical field order.

        ``session_ref`` is first (the value project state stores in
        ``Run.worker_session_ref``); unset optional context fields are
        omitted, exactly as the state backend expects.
        """
        data: dict[str, Any] = {
            "session_ref": self.session_ref,
            "platform_id": self.platform_id,
            "worker_id": self.worker_id,
            "role_id": self.role_id,
            "project_id": self.project_id,
        }
        if self.goal_id is not None:
            data["goal_id"] = self.goal_id
        if self.context_id is not None:
            data["context_id"] = self.context_id
        return data

    def to_json(self) -> str:
        """Canonical deterministic JSON of the handle (sorted keys)."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkerSessionHandle:
        """Build a handle from a plain dict (corrupt state is a stable
        PlatformAdapterDataError).

        The carried ``session_ref`` (if any) is ignored and recomputed
        deterministically from the identity fields, so a stale or
        corrupt reference in persisted state self-corrects on rehydration.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "WorkerSessionHandle.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name
            for name in ("platform_id", "worker_id", "role_id", "project_id")
            if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt worker session handle: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                platform_id=data["platform_id"],
                worker_id=data["worker_id"],
                role_id=data["role_id"],
                project_id=data["project_id"],
                goal_id=data.get("goal_id"),
                context_id=data.get("context_id"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                "corrupt worker session handle for platform"
                f" {data.get('platform_id')!r}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Message payload (expose_command)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """One platform-neutral command/message exposed to a session.

    15-ADAPTER-SPEC.md SS5: "The Core must not embed platform-specific
    command syntax" -- the Core expresses the command as a
    platform-neutral ``directive``; the adapter translates it to its own
    session machinery. ``command_ref`` is a deterministic
    ``sr_command_<32 hex>`` pure function of the session and the
    directive, so the same command to the same session is always the
    same reference (idempotent re-delivery is detectable).
    """

    session_ref: str
    directive: str
    command_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.session_ref, str):
            raise TypeError(
                "CommandSpec.session_ref must be a str, got"
                f" {type(self.session_ref).__name__}"
            )
        if not is_valid_id(self.session_ref, kind=_SESSION_REF_KIND):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        if not isinstance(self.directive, str):
            raise TypeError(
                "CommandSpec.directive must be a str, got"
                f" {type(self.directive).__name__}"
            )
        if not self.directive.strip():
            raise PlatformAdapterDataError(
                "CommandSpec.directive must be a non-empty string, got"
                f" {self.directive!r}"
            )
        if self.command_ref:
            raise PlatformAdapterDataError(
                "CommandSpec.command_ref is computed from session_ref and"
                " directive; pass none"
            )
        object.__setattr__(
            self,
            "command_ref",
            generate_id(
                _COMMAND_REF_KIND, self.session_ref, self.directive
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the command in canonical field order."""
        return {
            "command_ref": self.command_ref,
            "session_ref": self.session_ref,
            "directive": self.directive,
        }

    def to_json(self) -> str:
        """Canonical deterministic JSON of the command."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CommandSpec:
        """Build a command from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "CommandSpec.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in ("session_ref", "directive") if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt command record: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                session_ref=data["session_ref"],
                directive=data["directive"],
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt command record: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Operation results (each carries the explicit FallbackMode, AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnResult:
    """The typed outcome of a spawn operation (AC-03).

    ``mode`` NATIVE or FALLBACK carries a real, usable
    :class:`WorkerSessionHandle`; ``mode`` UNSUPPORTED carries ``None``
    and an explicit ``fallback_reason`` -- the caller is never handed a
    fabricated session.
    """

    handle: WorkerSessionHandle | None
    mode: FallbackMode
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        _check_fallback(self.mode, self.fallback_reason)
        if self.mode is FallbackMode.UNSUPPORTED:
            if self.handle is not None:
                raise PlatformAdapterDataError(
                    "an UNSUPPORTED spawn result must not carry a handle"
                )
        elif not isinstance(self.handle, WorkerSessionHandle):
            raise TypeError(
                "SpawnResult.handle must be a WorkerSessionHandle for a"
                f" {self.mode.value} result, got"
                f" {type(self.handle).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result in canonical field order."""
        data: dict[str, Any] = {"mode": self.mode.value}
        if self.handle is not None:
            data["handle"] = self.handle.to_dict()
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        return data

    def to_json(self) -> str:
        """Canonical deterministic JSON of the result."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SpawnResult:
        """Build a result from a plain dict (corrupt state is a stable
        PlatformAdapterDataError).

        ``handle`` is optional in the dict (absent for UNSUPPORTED
        results, exactly as ``to_dict`` omits it), so every result
        round-trips losslessly.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "SpawnResult.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("mode",) if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt spawn result: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                handle=(
                    WorkerSessionHandle.from_dict(data["handle"])
                    if data.get("handle") is not None
                    else None
                ),
                mode=FallbackMode(data["mode"]),
                fallback_reason=data.get("fallback_reason"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt spawn result: {exc}"
            ) from exc


@dataclass(frozen=True)
class ResumeResult:
    """The typed outcome of a resume operation (AC-03).

    Same shape as :class:`SpawnResult`: a resumed
    :class:`WorkerSessionHandle` under NATIVE/FALLBACK, an explicit
    UNSUPPORTED refusal without a handle otherwise.
    """

    handle: WorkerSessionHandle | None
    mode: FallbackMode
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        _check_fallback(self.mode, self.fallback_reason)
        if self.mode is FallbackMode.UNSUPPORTED:
            if self.handle is not None:
                raise PlatformAdapterDataError(
                    "an UNSUPPORTED resume result must not carry a handle"
                )
        elif not isinstance(self.handle, WorkerSessionHandle):
            raise TypeError(
                "ResumeResult.handle must be a WorkerSessionHandle for a"
                f" {self.mode.value} result, got"
                f" {type(self.handle).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result in canonical field order."""
        data: dict[str, Any] = {"mode": self.mode.value}
        if self.handle is not None:
            data["handle"] = self.handle.to_dict()
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        return data

    def to_json(self) -> str:
        """Canonical deterministic JSON of the result."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResumeResult:
        """Build a result from a plain dict (corrupt state is a stable
        PlatformAdapterDataError).

        ``handle`` is optional in the dict (absent for UNSUPPORTED
        results, exactly as ``to_dict`` omits it), so every result
        round-trips losslessly.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "ResumeResult.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("mode",) if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt resume result: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                handle=(
                    WorkerSessionHandle.from_dict(data["handle"])
                    if data.get("handle") is not None
                    else None
                ),
                mode=FallbackMode(data["mode"]),
                fallback_reason=data.get("fallback_reason"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt resume result: {exc}"
            ) from exc


@dataclass(frozen=True)
class TerminateResult:
    """The typed outcome of a stop operation (AC-03).

    ``mode`` NATIVE or FALLBACK reports that the session was
    terminated (through its own machinery or through the explicit
    fallback channel); ``mode`` UNSUPPORTED reports that termination
    could not be performed -- the explicit typed refusal with a
    ``fallback_reason``.
    """

    session_ref: str
    mode: FallbackMode
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_id(self.session_ref, kind=_SESSION_REF_KIND):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        _check_fallback(self.mode, self.fallback_reason)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result in canonical field order."""
        data: dict[str, Any] = {
            "session_ref": self.session_ref,
            "mode": self.mode.value,
        }
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        return data

    def to_json(self) -> str:
        """Canonical deterministic JSON of the result."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TerminateResult:
        """Build a result from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "TerminateResult.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in ("session_ref", "mode") if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt terminate result: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                session_ref=data["session_ref"],
                mode=FallbackMode(data["mode"]),
                fallback_reason=data.get("fallback_reason"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt terminate result: {exc}"
            ) from exc


@dataclass(frozen=True)
class AliveResult:
    """The typed outcome of a liveness check (AC-03).

    ``mode`` NATIVE or FALLBACK carries the observed ``alive`` boolean;
    ``mode`` UNSUPPORTED carries ``alive=None`` -- the platform cannot
    answer and says so explicitly instead of guessing.
    """

    session_ref: str
    alive: bool | None
    mode: FallbackMode
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_id(self.session_ref, kind=_SESSION_REF_KIND):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        _check_fallback(self.mode, self.fallback_reason)
        if self.mode is FallbackMode.UNSUPPORTED:
            if self.alive is not None:
                raise PlatformAdapterDataError(
                    "an UNSUPPORTED liveness result must carry alive=None"
                )
        elif not isinstance(self.alive, bool):
            raise TypeError(
                "AliveResult.alive must be a bool for a"
                f" {self.mode.value} result, got"
                f" {type(self.alive).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result in canonical field order."""
        data: dict[str, Any] = {
            "session_ref": self.session_ref,
            "mode": self.mode.value,
        }
        if self.alive is not None:
            data["alive"] = self.alive
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        return data

    def to_json(self) -> str:
        """Canonical deterministic JSON of the result."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AliveResult:
        """Build a result from a plain dict (corrupt state is a stable
        PlatformAdapterDataError).

        ``alive`` is optional in the dict (absent when the platform
        cannot answer, exactly as ``to_dict`` omits it), so every
        result round-trips losslessly.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "AliveResult.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("session_ref", "mode") if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt liveness result: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                session_ref=data["session_ref"],
                alive=data.get("alive"),
                mode=FallbackMode(data["mode"]),
                fallback_reason=data.get("fallback_reason"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt liveness result: {exc}"
            ) from exc


@dataclass(frozen=True)
class MessageResult:
    """The typed outcome of a message/command delivery (AC-03).

    ``mode`` NATIVE or FALLBACK reports that the
    :class:`CommandSpec` was delivered (through the session or through
    the explicit fallback channel); ``mode`` UNSUPPORTED reports that it
    was not -- the explicit typed refusal with a ``fallback_reason``.
    """

    command: CommandSpec
    mode: FallbackMode
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        _check_fallback(self.mode, self.fallback_reason)
        if not isinstance(self.command, CommandSpec):
            raise TypeError(
                "MessageResult.command must be a CommandSpec, got"
                f" {type(self.command).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result in canonical field order."""
        data: dict[str, Any] = {
            "command": self.command.to_dict(),
            "mode": self.mode.value,
        }
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        return data

    def to_json(self) -> str:
        """Canonical deterministic JSON of the result."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MessageResult:
        """Build a result from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "MessageResult.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("command", "mode") if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt message result: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                command=CommandSpec.from_dict(data["command"]),
                mode=FallbackMode(data["mode"]),
                fallback_reason=data.get("fallback_reason"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt message result: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# The orchestration interface (AC-01: the only platform surface)
# ---------------------------------------------------------------------------


class PlatformAdapter(ABC):
    """Contract for platform-neutral agent orchestration.

    Subclasses declare their identity as stable, versioned class
    constants (``platform_id``, ``version``, ``description``). The
    operations are exactly the normalized orchestration contract of
    15-ADAPTER-SPEC.md SS5 -- spawn persistent roles and goal-scoped
    workers, resume/stop sessions, check liveness, expose
    platform-neutral commands and the explicit subagent fallback path.

    Fallback contract (AC-03)
    -------------------------
    ``capabilities()`` declares which operations are natively
    supported. Any operation NOT listed there must be answered as an
    explicit ``FallbackMode.FALLBACK`` or ``FallbackMode.UNSUPPORTED``
    result carrying a ``fallback_reason`` -- never a NATIVE result,
    never a fabricated session, never a silent degradation.

    Determinism
    -----------
    Implementations must be deterministic in the tested path: no wall
    clock, no randomness, no hidden I/O. Session references are
    canonical functions of the logical worker context
    (:class:`WorkerSessionHandle`), so the project state keeps one
    stable ``session_ref`` across resumes and replacements
    (13-EXECUTION-MONITOR.md SS3-SS4).
    """

    platform_id: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str] = ""

    @abstractmethod
    def capabilities(self) -> PlatformCapability:
        """The typed capability record of this platform (AC-03).

        Declares which :class:`PlatformOperation` members are supported
        natively. Operations not listed must be answered explicitly as
        FALLBACK or UNSUPPORTED results, never silently faked.
        """

    @abstractmethod
    def spawn_persistent_role(self, role_id: str, project_id: str) -> SpawnResult:
        """Spawn one project-persistent role session.

        The Monitor (13-EXECUTION-MONITOR.md SS1: the only v0.1 role
        with a high-availability runtime policy) is the canonical use:
        a session that is expected to remain active in a durable
        runtime and to be resumed or replaced when it disappears.

        Args:
            role_id: frozen contract role id
                (``CONTRACT_ROLE_IDS``; ``"execution_monitor"`` for the
                Monitor, ``"supervisor"``/``"research"`` for the other
                persistent roles).
            project_id: the project the role session belongs to
                (``sr_project_<32 hex>``).

        Returns:
            A :class:`SpawnResult`: NATIVE/FALLBACK with a real
            :class:`WorkerSessionHandle`, or UNSUPPORTED with an
            explicit ``fallback_reason`` and no handle (AC-03).

        Raises:
            TypeError: ``role_id`` or ``project_id`` is not a str.
            PlatformAdapterDataError: ``role_id`` is outside the frozen
                contract role vocabulary or ``project_id`` is not a
                valid project id.
        """

    @abstractmethod
    def spawn_worker(
        self,
        role_id: str,
        goal_context: GoalExecutionContextPackage,
        *,
        project_id: str,
        worker_id: str | None = None,
    ) -> SpawnResult:
        """Spawn one goal-scoped worker session.

        Workers execute exactly one bounded frozen Goal/Run context
        (03-ROLE-AND-PERMISSION-SPEC.md SS5-SS8); the Monitor spawns
        follow-up collection/analysis workers according to frozen rules
        (13-EXECUTION-MONITOR.md SS2).

        Args:
            role_id: frozen contract role id -- ``"worker"`` for
                goal-scoped workers.
            goal_context: the frozen worker-context execution package
                the worker executes.
            project_id: the project the worker session belongs to.
            worker_id: optional worker id/name on the platform; when
                None the adapter derives a deterministic id from the
                goal context (e.g. the ``worker_role`` value).

        Returns:
            A :class:`SpawnResult` (see :meth:`spawn_persistent_role`).

        Raises:
            TypeError: ``role_id`` or ``project_id`` is not a str,
                ``goal_context`` is not a
                ``GoalExecutionContextPackage``, or ``worker_id`` is
                not a str/None.
            PlatformAdapterDataError: ``role_id`` is outside the frozen
                contract role vocabulary.
        """

    @abstractmethod
    def resume_session(self, session_ref: str) -> ResumeResult:
        """Resume an existing session by its durable reference.

        The session resume layer of 13-EXECUTION-MONITOR.md SS3:
        "Attempt to resume the original Monitor session through
        platform adapter capability."

        Args:
            session_ref: the canonical durable reference
                (``sr_session_<32 hex>``) as stored in project state
                (``Run.worker_session_ref``).

        Returns:
            A :class:`ResumeResult`: NATIVE/FALLBACK with the resumed
            :class:`WorkerSessionHandle`, or UNSUPPORTED with an
            explicit ``fallback_reason`` and no handle (AC-03).

        Raises:
            TypeError: ``session_ref`` is not a str.
            SessionNotFoundError: no session exists for that reference
                (the caller must spawn a replacement, SS4).
        """

    @abstractmethod
    def terminate_session(self, session_ref: str) -> TerminateResult:
        """Stop a session by its durable reference.

        Args:
            session_ref: the canonical durable reference.

        Returns:
            A :class:`TerminateResult` -- NATIVE/FALLBACK reports the
            session was terminated, UNSUPPORTED is the explicit typed
            refusal (AC-03).

        Raises:
            TypeError: ``session_ref`` is not a str.
            SessionNotFoundError: no session exists for that reference.
        """

    @abstractmethod
    def is_session_alive(self, session_ref: str) -> AliveResult:
        """Check whether a session is still alive.

        Args:
            session_ref: the canonical durable reference.

        Returns:
            An :class:`AliveResult` -- NATIVE/FALLBACK carries the
            observed ``alive`` boolean, UNSUPPORTED carries
            ``alive=None``: the platform cannot answer and says so
            explicitly (AC-03).

        Raises:
            TypeError: ``session_ref`` is not a str.
            SessionNotFoundError: no session exists for that reference.
        """

    @abstractmethod
    def expose_command(self, command: CommandSpec) -> MessageResult:
        """Expose one platform-neutral command to a session.

        15-ADAPTER-SPEC.md SS5: "The Core must not embed platform-
        specific command syntax" -- the Core passes a
        :class:`CommandSpec` with a platform-neutral directive; the
        adapter translates it to its own session machinery.

        Args:
            command: the platform-neutral command/message to deliver.

        Returns:
            A :class:`MessageResult` -- NATIVE/FALLBACK reports the
            command was delivered, UNSUPPORTED is the explicit typed
            refusal (AC-03).

        Raises:
            TypeError: ``command`` is not a :class:`CommandSpec`.
            SessionNotFoundError: no session exists for the command's
                ``session_ref``.
        """

    @abstractmethod
    def fallback_subagent(
        self,
        role_id: str,
        goal_context: GoalExecutionContextPackage,
        *,
        project_id: str,
        worker_id: str | None = None,
    ) -> SpawnResult:
        """The explicit fallback spawn path (15-ADAPTER-SPEC.md SS5).

        Spawns the goal-scoped worker as a short-lived subagent /
        subprocess instead of a persistent session. This operation is
        the documented, typed fallback (AC-03): its result is always
        ``FallbackMode.FALLBACK`` with a ``fallback_reason`` naming the
        fallback channel -- a subagent fallback is never reported as a
        native persistent session.

        Args and raises: identical to :meth:`spawn_worker`.
        """
