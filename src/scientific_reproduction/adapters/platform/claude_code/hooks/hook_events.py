"""The hook wiring template of the Claude Code quality gate (DEV-M10-G04).

How the real Claude Code hook mechanism would invoke the quality gate:
Claude Code runs hook commands configured in ``settings.json`` under a
``hooks`` map keyed by hook event name (the rendered configuration of
:mod:`hooks.config` names :mod:`hooks.entry` for the events below). For
Agent Teams task lifecycles the task-completion and idle events --
``TaskCompleted`` (a teammate task signals completion) and
``TeammateIdle`` (a teammate task goes idle) -- carry the task record
(the transport task id plus the durable session identity the M10-G03
task bundle embeds). The hook command receives the event JSON on stdin
and emits the decision JSON on stdout; a ``block`` decision prevents
the event, so the task-completion signal is mechanically blocked until
the gate passes.

This module is the **wiring template**: :class:`HookEvent` (the typed
payload of one hook event), :class:`HookDecision` / :class:`HookAction`
(the typed mechanical answer of the hook boundary) and
:func:`handle_hook_event` (the boundary the real hook mechanism would
call, hermetically mockable). The real event delivery is NOT exercised:
the deterministic test suite drives ``handle_hook_event`` directly with
scripted fakes, exactly like the M10-G03 adapter boundaries.

AC-02 delivery: when the gate blocks, the wiring appends the feedback
record's canonical directive to the durable session outbox -- a pending
command of the M10-G03 :class:`SessionRegistry` semantics, preserved
for the replacement session (13-EXECUTION-MONITOR.md SS4). The wiring
never marks the task complete: the only durable effect of a failure is
the feedback in the outbox.

AC-03: the returned action is a typed recommendation on the mechanical
completion signal only; there is no acceptance/verdict authority in the
hook layer (the Supervisor alone accepts).

Determinism: pure descriptor layer -- frozen records, ``TypeError`` at
the public boundaries, stable one-line error messages, canonical
serialization with lossless round-trip, no wall clock, no randomness,
no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
    SessionNotFoundError,
)
from scientific_reproduction.adapters.platform.claude_code.hooks.gate import (
    GateRecord,
    GateVerdict,
    VerificationSpec,
    Verifier,
    evaluate_gate,
)
from scientific_reproduction.adapters.platform.claude_code.session_registry import (
    SessionRegistry,
)
from scientific_reproduction.core.ids import is_valid_id

__all__ = [
    "HookAction",
    "HookDecision",
    "HookEvent",
    "HookEventType",
    "handle_hook_event",
]


class HookEventType(StrEnum):
    """The Claude Code hook events the quality gate templates.

    The Agent Teams task-lifecycle events of the DEV-M10-G04 objective:
    ``TaskCompleted`` -- a teammate task signals completion;
    ``TeammateIdle`` -- a teammate task goes idle. Both run the same
    deterministic gate: a task whose verification fails must neither
    complete nor be allowed to idle as done (AC-01).
    """

    TASK_COMPLETED = "TaskCompleted"
    TEAMMATE_IDLE = "TeammateIdle"


@dataclass(frozen=True)
class HookEvent:
    """The typed payload of one hook event (task completion / idle).

    ``event_type`` -- the hook event (see :class:`HookEventType`);
    ``task_id`` -- the transport task id of the Agent Teams task record
    (a transport detail, exactly as in M10-G03); ``session_ref`` -- the
    durable session identity of the gated task (the value the task
    bundle embeds, AC-02: the durable identity is never the task
    store's); ``goal_id`` -- the frozen goal context of the task (None
    for project-persistent roles).
    """

    event_type: HookEventType
    task_id: str
    session_ref: str
    goal_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, HookEventType):
            raise TypeError(
                "HookEvent.event_type must be a HookEventType member, got"
                f" {type(self.event_type).__name__}"
            )
        if not isinstance(self.task_id, str):
            raise TypeError(
                "HookEvent.task_id must be a str, got"
                f" {type(self.task_id).__name__}"
            )
        if not is_valid_id(self.task_id, kind="team_task"):
            raise PlatformAdapterDataError(
                f"invalid task_id {self.task_id!r}: expected"
                " sr_team_task_<32 hex>"
            )
        if not isinstance(self.session_ref, str):
            raise TypeError(
                "HookEvent.session_ref must be a str, got"
                f" {type(self.session_ref).__name__}"
            )
        if not is_valid_id(self.session_ref, kind="session"):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        if self.goal_id is not None and not isinstance(self.goal_id, str):
            raise TypeError(
                "HookEvent.goal_id must be a str or None, got"
                f" {type(self.goal_id).__name__}"
            )
        if self.goal_id is not None and not is_valid_id(self.goal_id, kind="goal"):
            raise PlatformAdapterDataError(
                f"invalid goal_id {self.goal_id!r}: expected sr_goal_<32 hex>"
            )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the event in canonical field order."""
        data: dict[str, Any] = {
            "event_type": self.event_type.value,
            "task_id": self.task_id,
            "session_ref": self.session_ref,
        }
        if self.goal_id is not None:
            data["goal_id"] = self.goal_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HookEvent:
        """Build an event from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "HookEvent.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in ("event_type", "task_id", "session_ref")
            if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt hook event: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                event_type=HookEventType(data["event_type"]),
                task_id=data["task_id"],
                session_ref=data["session_ref"],
                goal_id=data.get("goal_id"),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt hook event: {exc}"
            ) from exc


class HookDecision(StrEnum):
    """The mechanical answer of the hook boundary on the completion signal.

    ``PASS`` -- the gate passed; the task-completion signal proceeds.
    ``BLOCK`` -- the gate blocked the completion; feedback was delivered
    into the durable session outbox. The answer is mechanical (AC-03):
    it never declares acceptance of the overall goal -- the Supervisor
    alone accepts.
    """

    PASS = "pass"
    BLOCK = "block"


@dataclass(frozen=True)
class HookAction:
    """The typed action of the hook wiring on one event (the recommendation).

    ``decision`` -- the mechanical :class:`HookDecision` on the
    task-completion signal; ``gate`` -- the full :class:`GateRecord`
    (every verification outcome plus the actionable feedback). A BLOCK
    decision means the completion signal must not proceed; the feedback
    has already been delivered into the durable session outbox by the
    wiring (AC-02).

    The action is a typed recommendation the caller (the lead runtime
    owning the Supervisor review) acts on -- it can never itself accept
    work (AC-03).
    """

    decision: HookDecision
    gate: GateRecord

    def __post_init__(self) -> None:
        if not isinstance(self.decision, HookDecision):
            raise TypeError(
                "HookAction.decision must be a HookDecision member, got"
                f" {type(self.decision).__name__}"
            )
        if not isinstance(self.gate, GateRecord):
            raise TypeError(
                "HookAction.gate must be a GateRecord, got"
                f" {type(self.gate).__name__}"
            )
        if self.decision is HookDecision.BLOCK and self.gate.verdict is not (
            GateVerdict.BLOCK
        ):
            raise PlatformAdapterDataError(
                "a BLOCK hook action must carry a BLOCK gate record"
            )
        if self.decision is HookDecision.PASS and self.gate.verdict is not (
            GateVerdict.PASS
        ):
            raise PlatformAdapterDataError(
                "a PASS hook action must carry a PASS gate record"
            )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the action in canonical field order (the decision
        JSON a real hook runtime would emit)."""
        return {"decision": self.decision.value, "gate": self.gate.to_dict()}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> HookAction:
        """Build an action from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "HookAction.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("decision", "gate") if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt hook action: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                decision=HookDecision(data["decision"]),
                gate=GateRecord.from_dict(data["gate"]),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt hook action: {exc}"
            ) from exc


def handle_hook_event(
    event: HookEvent,
    *,
    spec: VerificationSpec,
    verifier: Verifier,
    registry: SessionRegistry | None = None,
) -> HookAction:
    """The hook wiring template: one quality gate run on one hook event.

    The boundary the real ``TaskCompleted`` / ``TeammateIdle`` hook
    invocation would call. It runs the deterministic verification step
    (AC-01) and, when the gate blocks, delivers the actionable feedback
    into the durable session outbox -- a pending command of the session
    record, the M10-G03 :class:`SessionRegistry` semantics (AC-02) --
    NEVER an automatic completion. The returned action is the typed
    recommendation the caller acts on (AC-03).

    ``event`` -- the hook event payload; ``spec`` -- the durable
    verification context of the gated task (its session_ref must match
    the event's -- the durable identity is the same); ``verifier`` --
    the injectable verification boundary; ``registry`` -- the durable
    session records (a fresh registry by default).

    Raises:
        TypeError: ``event`` is not a :class:`HookEvent`.
        PlatformAdapterDataError: the event's durable identity does not
            match the verification spec's (corrupt hook payload).
        SessionNotFoundError: the durable registry holds no session
            record for the identity -- the hook cannot deliver feedback
            into an unknown session (a broken reference the caller must
            resolve, the DEV-M10-G02 discipline).
    """
    if not isinstance(event, HookEvent):
        raise TypeError(
            f"handle_hook_event expects a HookEvent, got {type(event).__name__}"
        )
    if not isinstance(spec, VerificationSpec):
        raise TypeError(
            f"handle_hook_event expects a VerificationSpec, got"
            f" {type(spec).__name__}"
        )
    if event.session_ref != spec.session_ref:
        raise PlatformAdapterDataError(
            "corrupt hook payload: event session_ref"
            f" {event.session_ref!r} does not match the verification spec"
            f" session_ref {spec.session_ref!r}"
        )
    if registry is None:
        registry = SessionRegistry()
    if registry.get(spec.session_ref) is None:
        raise SessionNotFoundError(f"no session found for {spec.session_ref!r}")
    gate = evaluate_gate(spec, verifier)
    feedback = gate.feedback
    if feedback is not None:
        # AC-02: the only durable effect of a blocked gate is the
        # feedback appended to the session's durable outbox -- never a
        # completion, never a fabricated pass.
        registry.add_pending_command(spec.session_ref, feedback.directive)
        return HookAction(decision=HookDecision.BLOCK, gate=gate)
    return HookAction(decision=HookDecision.PASS, gate=gate)
