"""The deterministic quality-gate core of the Claude Code hooks (DEV-M10-G04).

The goal deliverable "quality gate hook templates": a hook handler and
typed gate records that run a **deterministic verification step** when
an Agent Teams task signals completion (or a teammate goes idle) and
**block the completion unless the verification passes** (AC-01). The
deterministic verification is a pure function of the durable task
context: the frozen goal contract's verification list (the
``verification:`` commands of a DEV-GOAL YAML, passed as plain data in
:class:`VerificationSpec`) checked through the injectable
:class:`Verifier` boundary -- evaluated with no wall clock, no
randomness and no I/O, so the same inputs always produce the same
:class:`GateRecord`.

A failing gate produces an actionable, typed :class:`FeedbackRecord`
(which verification failed, what the verifier reported, what the task
must fix, and the canonical durable-outbox directive rendered from
them). The hook wiring (:mod:`hooks.hook_events`) delivers that
directive into the durable session outbox -- a pending command of the
M10-G03 :class:`SessionRegistry` semantics -- never an automatic
completion (AC-02).

The gate verdict is **mechanical, not scientific** (AC-03): it only
decides whether the task-completion signal may proceed (``PASS``) or is
blocked (``BLOCK``). There is no acceptance/verdict authority in the
hook layer -- no surface declares PASS/FAIL acceptance of the overall
goal; the Supervisor alone accepts (03-ROLE-AND-PERMISSION-SPEC.md
SS4/SS5/SS7, the frozen ``VerdictAuthority`` vocabulary). The hook
produces feedback and a typed recommendation; the caller (the lead
runtime owning the Supervisor review) acts on it.

Boundaries: :class:`Verifier` is the hermetically mockable verification
boundary (precedent: the M10-G03 ``TeamStoreClient``/``ProcessRunner``
boundaries and their scripted fakes). The real wrapper
:class:`UnavailableVerifier` answers a typed refusal -- the
verification could not be executed -- so the gate blocks rather than
fabricating a pass: a verification that cannot be executed never
completes a task.

Determinism: pure descriptor layer in the house style -- frozen
records, StrEnum vocabulary, ``TypeError`` at the public boundaries,
stable one-line error messages, deterministic ids via
``core.ids.generate_id``, canonical serialization with lossless
round-trip, and no wall clock, no randomness, no I/O anywhere in this
module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapterDataError,
)
from scientific_reproduction.core.ids import generate_id, is_valid_id

#: ``generate_id`` kind of one gate feedback record (a transport detail).
_GATE_FEEDBACK_KIND = "gate_feedback"

__all__ = [
    "REQUIRED_FIX",
    "VERIFIER_UNAVAILABLE_REPORT",
    "FeedbackRecord",
    "GateRecord",
    "GateVerdict",
    "UnavailableVerifier",
    "VerificationOutcome",
    "VerificationSpec",
    "Verifier",
    "evaluate_gate",
]


#: The typed refusal report of the unavailable verifier: the check was
#: not executed, and no verification result is fabricated from it.
VERIFIER_UNAVAILABLE_REPORT: str = (
    "verification could not be executed in this runtime; no verification"
    " result is fabricated"
)

#: The stable "what the task must fix" text of a failing gate's
#: feedback record (AC-02: actionable feedback, not a silent block).
REQUIRED_FIX: str = (
    "re-run the goal verification and fix the failing verification before"
    " reporting task completion"
)


class GateVerdict(StrEnum):
    """The mechanical verdict on the task-completion signal (AC-01/AC-03).

    ``PASS`` -- every verification command passed; the task-completion
    signal is allowed to proceed mechanically. ``BLOCK`` -- at least one
    verification command failed (or could not be executed); the
    completion signal is blocked and a :class:`FeedbackRecord` is
    produced.

    This is deliberately NOT a scientific PASS/FAIL of the overall goal
    (AC-03): the gate only gates the mechanical task-completion signal;
    accepting work remains the Supervisor's authority alone.
    """

    PASS = "pass"
    BLOCK = "block"


@dataclass(frozen=True)
class VerificationOutcome:
    """The typed outcome of one verification command check.

    ``command`` -- one verification command of the frozen goal contract
    (the ``verification:`` list of the DEV-GOAL YAML, as data);
    ``passed`` -- whether the check passed; ``report`` -- what the
    verifier reported (its evidence, or its typed refusal).
    """

    command: str
    passed: bool
    report: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise PlatformAdapterDataError(
                "VerificationOutcome.command must be a non-empty string, got"
                f" {self.command!r}"
            )
        if not isinstance(self.passed, bool):
            raise TypeError(
                "VerificationOutcome.passed must be a bool, got"
                f" {type(self.passed).__name__}"
            )
        if not isinstance(self.report, str) or not self.report.strip():
            raise PlatformAdapterDataError(
                "VerificationOutcome.report must be a non-empty string, got"
                f" {self.report!r}"
            )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the outcome in canonical field order."""
        return {
            "command": self.command,
            "passed": self.passed,
            "report": self.report,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationOutcome:
        """Build an outcome from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "VerificationOutcome.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in ("command", "passed", "report") if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt verification outcome: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                command=data["command"],
                passed=data["passed"],
                report=data["report"],
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt verification outcome: {exc}"
            ) from exc


@dataclass(frozen=True)
class FeedbackRecord:
    """The typed, actionable feedback of one blocked task (AC-02).

    ``session_ref`` -- the durable session identity whose completion was
    blocked; ``goal_id`` -- the frozen goal context (None for
    project-persistent roles); ``failed_command`` -- which verification
    failed; ``verifier_report`` -- what the verifier reported;
    ``required_fix`` -- what the task must fix; ``feedback_id`` -- the
    deterministic record id (computed, immutable); ``directive`` -- the
    canonical durable-outbox directive rendered from the record (the
    pending command the wiring delivers into the session record of the
    M10-G03 :class:`SessionRegistry` semantics).

    A failing gate produces exactly one feedback record, naming the
    first failing verification in command order (deterministic); every
    command's outcome is recorded on the :class:`GateRecord`.
    """

    session_ref: str
    goal_id: str | None
    failed_command: str
    verifier_report: str
    required_fix: str
    feedback_id: str = ""
    directive: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.session_ref, str):
            raise TypeError(
                "FeedbackRecord.session_ref must be a str, got"
                f" {type(self.session_ref).__name__}"
            )
        if not is_valid_id(self.session_ref, kind="session"):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        if self.goal_id is not None and not isinstance(self.goal_id, str):
            raise TypeError(
                "FeedbackRecord.goal_id must be a str or None, got"
                f" {type(self.goal_id).__name__}"
            )
        if self.goal_id is not None and not is_valid_id(self.goal_id, kind="goal"):
            raise PlatformAdapterDataError(
                f"invalid goal_id {self.goal_id!r}: expected sr_goal_<32 hex>"
            )
        for name in ("failed_command", "verifier_report", "required_fix"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(
                    f"FeedbackRecord.{name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise PlatformAdapterDataError(
                    f"FeedbackRecord.{name} must be a non-empty string, got"
                    f" {value!r}"
                )
        if self.feedback_id:
            raise PlatformAdapterDataError(
                "FeedbackRecord.feedback_id is computed from session_ref and"
                " failed_command; pass none"
            )
        if self.directive:
            raise PlatformAdapterDataError(
                "FeedbackRecord.directive is rendered from the record; pass"
                " none"
            )
        # Frozen dataclass: the computed id and rendered directive are set
        # once at construction.
        object.__setattr__(
            self,
            "feedback_id",
            generate_id(_GATE_FEEDBACK_KIND, self.session_ref, self.failed_command),
        )
        object.__setattr__(self, "directive", _render_directive(self))

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the feedback in canonical field order."""
        data: dict[str, Any] = {
            "feedback_id": self.feedback_id,
            "session_ref": self.session_ref,
            "failed_command": self.failed_command,
            "verifier_report": self.verifier_report,
            "required_fix": self.required_fix,
            "directive": self.directive,
        }
        if self.goal_id is not None:
            data["goal_id"] = self.goal_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FeedbackRecord:
        """Build a feedback record from a plain dict (corrupt state is a
        stable PlatformAdapterDataError).

        The carried ``feedback_id``/``directive`` (if any) are ignored and
        recomputed deterministically from the identity and report fields,
        so a stale or corrupt serialized pair self-corrects on
        rehydration (the house self-correction rule of the adapter layer).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "FeedbackRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name
            for name in ("session_ref", "failed_command", "verifier_report",
                         "required_fix")
            if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt feedback record: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                session_ref=data["session_ref"],
                goal_id=data.get("goal_id"),
                failed_command=data["failed_command"],
                verifier_report=data["verifier_report"],
                required_fix=data["required_fix"],
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt feedback record: {exc}"
            ) from exc


def _render_directive(feedback: FeedbackRecord) -> str:
    """The canonical durable-outbox directive of one feedback record.

    One stable line naming the blocked session, the failing
    verification, what the verifier reported and the required fix -- the
    pending command delivered into the durable session outbox (AC-02).
    """
    return (
        "verification gate blocked task completion for"
        f" {feedback.session_ref}: verification {feedback.failed_command!r}"
        f" did not pass: {feedback.verifier_report}; {feedback.required_fix}"
    )


@dataclass(frozen=True)
class GateRecord:
    """The typed result of one gate run (AC-01: BLOCK vs PASS).

    ``session_ref`` -- the gated durable session identity; ``goal_id`` --
    the frozen goal context; ``verdict`` -- the mechanical
    :class:`GateVerdict` on the completion signal; ``outcomes`` -- every
    verification command's outcome, in command order (the failing
    verification is recorded); ``feedback`` -- the actionable
    :class:`FeedbackRecord` of the first failing verification (None on
    PASS).

    The record never declares a scientific outcome of the goal (AC-03):
    its verdict gates only the mechanical task-completion signal.
    """

    session_ref: str
    goal_id: str | None
    verdict: GateVerdict
    outcomes: tuple[VerificationOutcome, ...]
    feedback: FeedbackRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_ref, str):
            raise TypeError(
                "GateRecord.session_ref must be a str, got"
                f" {type(self.session_ref).__name__}"
            )
        if not is_valid_id(self.session_ref, kind="session"):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        if self.goal_id is not None and not isinstance(self.goal_id, str):
            raise TypeError(
                "GateRecord.goal_id must be a str or None, got"
                f" {type(self.goal_id).__name__}"
            )
        if self.goal_id is not None and not is_valid_id(self.goal_id, kind="goal"):
            raise PlatformAdapterDataError(
                f"invalid goal_id {self.goal_id!r}: expected sr_goal_<32 hex>"
            )
        if not isinstance(self.verdict, GateVerdict):
            raise TypeError(
                "GateRecord.verdict must be a GateVerdict member, got"
                f" {type(self.verdict).__name__}"
            )
        if not isinstance(self.outcomes, tuple):
            raise TypeError(
                "GateRecord.outcomes must be a tuple of VerificationOutcome,"
                f" got {type(self.outcomes).__name__}"
            )
        for outcome in self.outcomes:
            if not isinstance(outcome, VerificationOutcome):
                raise TypeError(
                    "GateRecord.outcomes entries must be VerificationOutcome"
                    f" instances, got {type(outcome).__name__}"
                )
        if self.feedback is not None and not isinstance(
            self.feedback, FeedbackRecord
        ):
            raise TypeError(
                "GateRecord.feedback must be a FeedbackRecord or None, got"
                f" {type(self.feedback).__name__}"
            )
        if self.verdict is GateVerdict.PASS:
            if self.feedback is not None:
                raise PlatformAdapterDataError(
                    "a PASS gate record must not carry a feedback record"
                )
        elif self.feedback is None:
            raise PlatformAdapterDataError(
                "a BLOCK gate record must carry a feedback record"
            )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the gate record in canonical field order."""
        data: dict[str, Any] = {
            "session_ref": self.session_ref,
            "verdict": self.verdict.value,
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }
        if self.goal_id is not None:
            data["goal_id"] = self.goal_id
        if self.feedback is not None:
            data["feedback"] = self.feedback.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GateRecord:
        """Build a gate record from a plain dict (corrupt state is a
        stable PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "GateRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in ("session_ref", "verdict", "outcomes") if name not in data
        ]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt gate record: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                session_ref=data["session_ref"],
                goal_id=data.get("goal_id"),
                verdict=GateVerdict(data["verdict"]),
                outcomes=tuple(
                    VerificationOutcome.from_dict(item)
                    for item in data["outcomes"]
                ),
                feedback=(
                    FeedbackRecord.from_dict(data["feedback"])
                    if data.get("feedback") is not None
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt gate record: {exc}"
            ) from exc


@dataclass(frozen=True)
class VerificationSpec:
    """The frozen verification context of one gated task (durable task context).

    ``session_ref`` -- the durable session identity being gated;
    ``goal_id`` -- the frozen goal the task executes (None for
    project-persistent roles); ``commands`` -- the goal contract's
    verification list (the ``verification:`` commands of the frozen
    DEV-GOAL YAML) as plain data: the deterministic verification step
    checks exactly these, in order. An empty list passes vacuously --
    the frozen goal contract governs what must be verified; the gate is
    mechanical and invents no requirement.
    """

    session_ref: str
    goal_id: str | None = None
    commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_ref, str):
            raise TypeError(
                "VerificationSpec.session_ref must be a str, got"
                f" {type(self.session_ref).__name__}"
            )
        if not is_valid_id(self.session_ref, kind="session"):
            raise PlatformAdapterDataError(
                f"invalid session_ref {self.session_ref!r}: expected"
                " sr_session_<32 hex>"
            )
        if self.goal_id is not None and not isinstance(self.goal_id, str):
            raise TypeError(
                "VerificationSpec.goal_id must be a str or None, got"
                f" {type(self.goal_id).__name__}"
            )
        if self.goal_id is not None and not is_valid_id(self.goal_id, kind="goal"):
            raise PlatformAdapterDataError(
                f"invalid goal_id {self.goal_id!r}: expected sr_goal_<32 hex>"
            )
        if not isinstance(self.commands, tuple):
            raise TypeError(
                "VerificationSpec.commands must be a tuple of str, got"
                f" {type(self.commands).__name__}"
            )
        for command in self.commands:
            if not isinstance(command, str) or not command.strip():
                raise PlatformAdapterDataError(
                    "VerificationSpec.commands entries must be non-empty"
                    f" strings, got {command!r}"
                )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the spec in canonical field order."""
        data: dict[str, Any] = {"session_ref": self.session_ref}
        if self.goal_id is not None:
            data["goal_id"] = self.goal_id
        if self.commands:
            data["commands"] = list(self.commands)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VerificationSpec:
        """Build a spec from a plain dict (corrupt state is a stable
        PlatformAdapterDataError)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "VerificationSpec.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("session_ref",) if name not in data]
        if missing:
            raise PlatformAdapterDataError(
                "corrupt verification spec: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                session_ref=data["session_ref"],
                goal_id=data.get("goal_id"),
                commands=tuple(data.get("commands", ())),
            )
        except (TypeError, ValueError) as exc:
            raise PlatformAdapterDataError(
                f"corrupt verification spec: {exc}"
            ) from exc


class Verifier(ABC):
    """The hermetically mockable verification boundary (AC-01).

    ``check`` answers one verification command with a frozen typed
    outcome, deterministically. The tests inject a scripted fake
    (precedent: the M10-G03 ``ScriptedTeamStore`` / ``ScriptedProcessRunner``
    fakes of the adapter boundaries). A real deployment substitutes a
    :class:`Verifier` that executes the goal verification commands
    behind this boundary; the deterministic suite never does.
    """

    @abstractmethod
    def check(self, command: str) -> VerificationOutcome:
        """Check one verification command of the frozen goal contract.

        ``command`` is one entry of the goal's ``verification:`` list.
        Returns the frozen typed outcome; a check that could not be
        executed answers ``passed=False`` with the typed refusal report
        -- never a fabricated pass.
        """


class UnavailableVerifier(Verifier):
    """The real thin wrapper: verification is not executable in this runtime.

    Every check answers the typed refusal
    (:data:`VERIFIER_UNAVAILABLE_REPORT`) with ``passed=False`` -- the
    gate then BLOCKS the completion signal: a verification that could
    not be executed is never a pass (AC-01: no fabricated completion).
    A real deployment substitutes a :class:`Verifier` that executes the
    goal verification commands.
    """

    def check(self, command: str) -> VerificationOutcome:
        if not isinstance(command, str) or not command.strip():
            raise PlatformAdapterDataError(
                "Verifier.check expects a non-empty command str, got"
                f" {command!r}"
            )
        return VerificationOutcome(
            command=command, passed=False, report=VERIFIER_UNAVAILABLE_REPORT
        )


def evaluate_gate(spec: VerificationSpec, verifier: Verifier) -> GateRecord:
    """Run the deterministic verification step of one gated task (AC-01).

    A pure function of the durable task context and the injected
    verifier: every command of the spec is checked in order; when all
    pass the gate is PASS and the task-completion signal may proceed;
    when any fails (or could not be executed) the gate is BLOCK with the
    failing verification recorded and the actionable feedback produced
    (AC-02). No wall clock, no randomness, no I/O.

    Raises:
        TypeError: ``spec`` is not a :class:`VerificationSpec` or
            ``verifier`` is not a :class:`Verifier`.
    """
    if not isinstance(spec, VerificationSpec):
        raise TypeError(
            f"evaluate_gate expects a VerificationSpec, got"
            f" {type(spec).__name__}"
        )
    if not isinstance(verifier, Verifier):
        raise TypeError(
            f"evaluate_gate expects a Verifier, got {type(verifier).__name__}"
        )
    outcomes = tuple(verifier.check(command) for command in spec.commands)
    failed = [outcome for outcome in outcomes if not outcome.passed]
    if not failed:
        return GateRecord(
            session_ref=spec.session_ref,
            goal_id=spec.goal_id,
            verdict=GateVerdict.PASS,
            outcomes=outcomes,
        )
    first_failed = failed[0]
    feedback = FeedbackRecord(
        session_ref=spec.session_ref,
        goal_id=spec.goal_id,
        failed_command=first_failed.command,
        verifier_report=first_failed.report,
        required_fix=REQUIRED_FIX,
    )
    return GateRecord(
        session_ref=spec.session_ref,
        goal_id=spec.goal_id,
        verdict=GateVerdict.BLOCK,
        outcomes=outcomes,
        feedback=feedback,
    )
