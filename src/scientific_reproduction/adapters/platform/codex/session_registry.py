"""The durable session registry of the Codex adapter (DEV-M10-G05).

The reconstruction seam of 13-EXECUTION-MONITOR.md SS3-SS4: when a
session cannot be resumed the replacement "reconstructs state from shared
workspace, latest checkpoint, append-only event log and external truth",
and "a brand-new Monitor must be able to take over without chat-memory
access". For the adapter, the shared-workspace slice it owns is the
**durable session identity map**: ``session_ref -> SessionRecord`` (the
canonical :class:`WorkerSessionHandle` plus the durable pending-command
outbox of one session).

This registry is deliberately *not* the Codex session store (AC-02): the
Codex CLI's local session transcripts (``~/.codex/sessions/`` JSONL
rollout files) are a transport detail and never the source of truth for
identity. The durable identity is the Core's
``WorkerSessionHandle.session_ref``; the registry is the adapter's
workspace-side reflection of it, rehydratable from a crash-state snapshot
via :meth:`SessionRegistry.from_records` -- the tests hydrate it from a
durable snapshot exactly like scenario G replays the crash-time state.

The registry is transport-level orchestration state (mirroring the
DEV-M10-G03 house pattern, deliberately claude-independent): it carries
no scientific/domain logic and imports no scientific-core module.

The default registry is an in-memory deterministic store (no I/O), the
injectable default for the tested path; a production deployment would
back the same ``to_records``/``from_records`` surface with the shared
workspace / checkpoint files.

Determinism: pure descriptor layer -- no wall clock, no randomness, no
I/O, canonical sorted records.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable, Mapping

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    WorkerSessionHandle,
)

__all__ = [
    "SessionRecord",
    "SessionRegistry",
    "SessionState",
]


class SessionState(StrEnum):
    """The durable lifecycle state of one session record.

    ``ACTIVE`` -- the durable identity is live/current (a replacement
    may be created for it); ``TERMINATED`` -- the session was
    intentionally terminated through
    :meth:`PlatformAdapter.terminate_session` and must not be resumed or
    replaced (the caller spawns a fresh session for a new logical
    context instead).
    """

    ACTIVE = "active"
    TERMINATED = "terminated"


@dataclass(frozen=True)
class SessionRecord:
    """One durable session record: the canonical handle plus its state.

    ``handle`` -- the frozen :class:`WorkerSessionHandle` (the durable
    identity, AC-02); ``state`` -- the durable lifecycle state;
    ``pending_commands`` -- the durable outbox of directives that could
    not be delivered to a live session and are preserved for the
    replacement session (a reconstruction source, 13-EXECUTION-MONITOR.md
    SS4).
    """

    handle: WorkerSessionHandle
    state: SessionState = SessionState.ACTIVE
    pending_commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.handle, WorkerSessionHandle):
            raise TypeError(
                "SessionRecord.handle must be a WorkerSessionHandle, got"
                f" {type(self.handle).__name__}"
            )
        if not isinstance(self.state, SessionState):
            raise TypeError(
                f"SessionRecord.state must be a SessionState, got"
                f" {type(self.state).__name__}"
            )
        if not isinstance(self.pending_commands, tuple):
            raise TypeError(
                "SessionRecord.pending_commands must be a tuple of str, got"
                f" {type(self.pending_commands).__name__}"
            )
        for directive in self.pending_commands:
            if not isinstance(directive, str) or not directive.strip():
                raise PlatformAdapterDataError(
                    "SessionRecord.pending_commands entries must be non-empty"
                    f" strings, got {directive!r}"
                )

    @property
    def session_ref(self) -> str:
        return self.handle.session_ref

    def with_pending_command(self, directive: str) -> SessionRecord:
        """The record with one more directive in the durable outbox."""
        if not isinstance(directive, str) or not directive.strip():
            raise PlatformAdapterDataError(
                "pending directives must be non-empty strings"
            )
        return SessionRecord(
            self.handle, self.state, (*self.pending_commands, directive)
        )

    def as_terminated(self) -> SessionRecord:
        """The record with its durable state set to TERMINATED."""
        return SessionRecord(self.handle, SessionState.TERMINATED, self.pending_commands)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the record in canonical field order."""
        data: dict[str, Any] = {
            "handle": self.handle.to_dict(),
            "state": self.state.value,
        }
        if self.pending_commands:
            data["pending_commands"] = list(self.pending_commands)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SessionRecord:
        """Build a record from a plain dict (corrupt state is a stable
        PlatformAdapterDataError -- records are workspace-transported
        state)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "SessionRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("handle", "state") if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt session record: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                handle=WorkerSessionHandle.from_dict(data["handle"]),
                state=SessionState(data["state"]),
                pending_commands=tuple(data.get("pending_commands", ())),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt session record: {exc}"
            ) from exc


class SessionRegistry:
    """The durable session identity map (workspace-side, AC-02).

    ``session_ref -> SessionRecord``. The registry is the adapter's
    reconstruction source for identity: resume/terminate/liveness/
    delivery resolve the Core's ``session_ref`` against it, never
    against the Codex session store. It rehydrates from a durable
    snapshot (``to_records``/``from_records``), so a replacement adapter
    over the same workspace answers the same identity
    (13-EXECUTION-MONITOR.md SS4).
    """

    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}

    def put(self, handle: WorkerSessionHandle) -> None:
        """Register (or re-register) the durable identity of one session.

        Re-registering an existing identity preserves its durable outbox
        (a replacement session of the same identity must not lose the
        pending commands preserved for it, SS4).
        """
        if not isinstance(handle, WorkerSessionHandle):
            raise TypeError(
                f"SessionRegistry.put expects a WorkerSessionHandle, got"
                f" {type(handle).__name__}"
            )
        existing = self._records.get(handle.session_ref)
        if existing is not None:
            self._records[handle.session_ref] = SessionRecord(
                handle, SessionState.ACTIVE, existing.pending_commands
            )
            return
        self._records[handle.session_ref] = SessionRecord(handle)

    def get(self, session_ref: str) -> SessionRecord | None:
        """The durable record of one ref, or None when unknown."""
        return self._records.get(session_ref)

    def is_terminated(self, session_ref: str) -> bool:
        """True iff the durable record exists and is TERMINATED."""
        record = self._records.get(session_ref)
        return record is not None and record.state is SessionState.TERMINATED

    def mark_terminated(self, session_ref: str) -> None:
        """Set the durable record's state to TERMINATED.

        Raises:
            PlatformAdapterDataError: no durable record for the ref
                (callers resolve against the registry first).
        """
        record = self._records.get(session_ref)
        if record is None:
            raise PlatformAdapterDataError(
                f"cannot terminate unknown session {session_ref!r}"
            )
        self._records[session_ref] = record.as_terminated()

    def add_pending_command(self, session_ref: str, directive: str) -> None:
        """Append one directive to the session's durable outbox.

        Raises:
            PlatformAdapterDataError: no durable record for the ref.
        """
        record = self._records.get(session_ref)
        if record is None:
            raise PlatformAdapterDataError(
                f"cannot queue a command for unknown session {session_ref!r}"
            )
        self._records[session_ref] = record.with_pending_command(directive)

    def pending_commands(self, session_ref: str) -> tuple[str, ...]:
        """The durable outbox of one session (reconstruction source, SS4)."""
        record = self._records.get(session_ref)
        return () if record is None else record.pending_commands

    def clear_pending_commands(self, session_ref: str) -> None:
        """Consume the durable outbox of one session."""
        record = self._records.get(session_ref)
        if record is None:
            return
        self._records[session_ref] = SessionRecord(record.handle, record.state)

    def to_records(self) -> tuple[dict[str, Any], ...]:
        """Canonical snapshot of the durable registry (sorted by ref)."""
        return tuple(record.to_dict() for _, record in sorted(self._records.items()))

    @classmethod
    def from_records(cls, records: Iterable[Mapping[str, Any]]) -> SessionRegistry:
        """Rehydrate a registry from a durable snapshot.

        Reconstructs exactly the workspace-side identity of a
        crash-state snapshot (corrupt records are a stable
        PlatformAdapterDataError).
        """
        registry = cls()
        for record_data in records:
            record = SessionRecord.from_dict(record_data)
            registry._records[record.session_ref] = record
        return registry

    def __len__(self) -> int:
        return len(self._records)
