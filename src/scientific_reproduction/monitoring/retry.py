"""The Monitor's engineering retry dispatcher (DEV-M8-G03, deliverable).

The Execution Monitor applies pre-authorized engineering recovery to
watched external Runs that failed for engineering reasons (a scheduler
or node problem, a connection-level transport failure) -- without ever
replanning scientifically. This module implements that decision as a
deterministic primitive: given the durable monitor state directory (the
watched-Run registry and the durable Run store of DEV-M8-G01/G02), the
append-only event log and injected hooks, it decides for each failed
external Run whether an identical resubmission is authorized, performs
it through an injected resubmission hook, and records every decision
(authorized, refused, invalidated and supervisor-routed) as an
auditable event through the real event log with deterministic ids -- so
a Monitor restart reconstructs the full retry history from the durable
state alone.

Failure-class vocabulary and the policy-kind bridge
---------------------------------------------------
The engineering-vs-scientific classification of a failure lives in the
compute adapters (``adapters/compute/ssh.py`` and
``adapters/compute/slurm_ssh.py``): the persisted job records carry a
``failure_class`` field of ``"transport"`` (connection-level:
scheduler/node unreachable -- an engineering failure) or ``"job"``
(the job's own failure -- a scientific compute failure), or ``None``
(no failure). The monitoring subsystem never imports the adapters
package (locked by ``tests/monitoring/test_monitoring_surface.py``):
``retry.py`` mirrors that vocabulary as the plain documented constants
:data:`FAILURE_CLASS_TRANSPORT` and :data:`FAILURE_CLASS_JOB`, and the
classifier / resubmission hook are injected callables.

The frozen ``AutomaticRetryPolicy`` contract speaks a different,
policy-level vocabulary -- the failure kinds of ``workers/retry.py``
(``"ssh_connection_lost"``, ``"scheduler_node_failure"``,
``"checkpoint_continuation"``, ...). :func:`failure_class_to_failure_kind`
is the deterministic bridge between the two vocabularies: the mirrored
adapter class ``"transport"`` maps to the policy kind
``"ssh_connection_lost"``, every other failure class passes through
verbatim (so a classifier may already report a policy kind directly),
and a failure the adapter never classified maps to
:data:`RETRY_FAILURE_CLASS_UNCLASSIFIED`. The bridge is a pure function
of the failure class -- the dispatcher never imports the adapters.

Automatic retry policy semantics (AC-01/AC-02)
----------------------------------------------
Every retry decision consults the Goal's frozen automatic retry
policy: the dispatcher resolves ``goal.automatic_retry_policy_ref``
through the injected run store (the ``retry-policy`` record) and
routes the authorization decision through the frozen
``workers/retry.py`` evaluator (``evaluate_automatic_retry`` over the
ordered ``RETRY_DECISION_RULES`` table -- first match wins, the
whitelist is the contract). The dispatcher reimplements none of the
policy semantics; it only maps the evaluator's verdict and routing
onto the decision vocabulary:

* a whitelisted engineering failure (``R-RET-A1``) or an identical
  checkpoint continuation within ``max_identical_retries``
  (``R-RET-C1``) authorizes an IDENTICAL resubmission through the
  injected hook -- same run identity, same external identity semantics
  (the same backend; the hook returns the fresh external id of the
  resubmission), no parameter change of any kind -- the dispatcher
  never writes the Run record and under no circumstance mutates run
  parameters.
* a ``supervisor_required_changes`` entry or a scientific-change
  vocabulary match (``R-RET-S1``/``R-RET-V1``) decides a
  Supervisor-required change: observed and recorded, never
  resubmitted.
* an ``invalidate_run_on`` entry (``R-RET-I1``) decides an
  invalidation: observed and recorded, never resubmitted.
* anything else (``R-RET-D1``) -- the ``"job"`` class, an unclassified
  ``None``, an unrecognized string, or a Goal with no retry policy ref
  at all -- is a scientific compute failure: observed and recorded as
  a refused decision and never resubmitted (safe-by-construction: the
  refusal is the default for every failure no policy entry
  authorizes).

The identical-retry ceiling (``max_identical_retries``) is enforced
through an attempt-indexed decision identity: the dispatcher counts
the recorded authorized decisions of the same (run, failure class) as
the ``identical_retry_count`` the frozen evaluator gates checkpoint
continuation with, so the ceiling can actually reject the N+1th
identical resubmission instead of the idempotency key capping retries
at one per failure class.

Retry-decision event vocabulary (AC-03, auditable history)
----------------------------------------------------------
Every decision is appended through the real append-only
``ProjectEventLog`` as an ``engineering_retry_decision`` event (actor
``execution-monitor``, object/run the decided Run, stable reason per
decision), carrying the failure class, the bridged failure kind, the
consulted policy id, the attempt index, the decision, the evaluator
routing, the matched rule id and reasoning ids, the decided external
identity and -- when authorized -- the resubmitted external identity
in the payload. The event id is a pure function of the decision inputs
(``generate_id("event", "engineering_retry_decision", <run_id>,
<failure class>, "attempt-<n>")``) and the append uses the
deterministic idempotency key ``retry.decision:<run_id>:<failure
class>:attempt-<n>``: the attempt index is part of the decision
identity, so each of the ``max_identical_retries`` resubmissions is
recorded exactly once and the log's sequence never advances twice for
the same decision.

Exactly-once resubmission and restart replay
--------------------------------------------
The recorded decision is the durable "retry was performed" fact: the
dispatcher resolves the recorded history *before* touching the
resubmission hook. A recorded decision replays when it is the decision
of the current failure generation -- the recorded ``external``
identity matches the watch entry's (a record predating the field
matches any generation) -- so a re-decided (or restart-replayed)
decision returns the recorded history and never re-invokes the hook,
while a genuinely new failure generation (a fresh external identity --
e.g. the resubmission itself failing again) advances to the next
attempt and a fresh decision. A crash between the resubmission and the
append of the decision record re-invokes the hook when the same
decision is re-issued; once the record exists, re-deciding is a pure
idempotent replay.

Determinism and discipline
--------------------------
All timestamps come from the injected clock (``now``); ids are
generated deterministically with ``core.ids.generate_id``; the event
reason, the event type and the idempotency-key prefix are stable
documented constants; the payload is plain JSON-able data persisted as
canonical sorted JSON through the real event log. The dispatcher
persists nothing itself: every write goes through the injected
registry, run store and event log, and the dispatcher **never writes
the run store** (no parameter mutation, ever). Errors follow the house
paradigm: ``TypeError`` at public type boundaries, stable
``MonitoringError`` subclasses otherwise (``RetryContractError`` for
lifecycle/identity contract violations, ``CorruptRetryStateError`` for
corrupt retry state -- including an unreadable goal or retry-policy
record). No credentials are ever persisted: transient classifier
failures are recorded as unclassified refusals and their messages
never reach durable bytes.

The pass-level :meth:`RetryDispatcher.decide_all` isolates per-run
errors (issue #152): only ``RUNNING_EXTERNAL`` runs are eligible for a
decision, ineligible runs are recorded as skipped, a run whose decision
raises a stable per-run ``RetryError`` is recorded as a per-run failed
outcome, and the pass continues -- one permanently failing entry never
blocks the retry decisions of the healthy ones. Corrupt project-level
state (an unreadable watch set) still fails the pass loudly.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    AutomaticRetryPolicy,
    GoalContract,
    LifecycleState,
    ProjectEvent,
    Run,
    RunExternal,
)
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    StateBackend,
)
from scientific_reproduction.monitoring.registry import (
    MONITOR_ID_KIND,
    MonitoringClock,
    MonitoringError,
    WatchedRunRecord,
    WatchedRunRegistry,
    WatchNotFoundError,
    utc_now,
    validate_external_identity,
)
from scientific_reproduction.workers.retry import (
    CHECKPOINT_CONTINUATION_KIND,
    REASON_INVALIDATE_RUN,
    REASON_NO_POLICY_ENTRY,
    RetryAuthorization,
    RetryEvaluationInput,
    RetryPolicyError,
    RetryRouting,
    evaluate_automatic_retry,
)

__all__ = [
    "CorruptRetryStateError",
    "FAILURE_CLASS_JOB",
    "FAILURE_CLASS_TO_FAILURE_KIND",
    "FAILURE_CLASS_TRANSPORT",
    "FAILURE_KIND_SSH_CONNECTION_LOST",
    "FailureClassifier",
    "RETRY_ACTOR",
    "RETRY_AUTHORIZED_REASON",
    "RETRY_DECISION_AUTHORIZED",
    "RETRY_DECISION_EVENT_TYPE",
    "RETRY_DECISION_INVALIDATED",
    "RETRY_DECISION_KEY_PREFIX",
    "RETRY_DECISION_REFUSED",
    "RETRY_DECISION_SUPERVISOR_REQUIRED",
    "RETRY_DECISIONS",
    "RETRY_FAILURE_CLASS_UNCLASSIFIED",
    "RETRY_INVALIDATED_REASON",
    "RETRY_REFUSED_REASON",
    "RETRY_SUPERVISOR_REASON",
    "ResubmitHook",
    "RetryContractError",
    "RetryDispatcher",
    "RetryError",
    "RetryFailure",
    "RetryOutcome",
    "RetrySkipped",
    "RetrySummary",
    "failure_class_to_failure_kind",
]

# ---------------------------------------------------------------------------
# The failure-class vocabulary (mirrors the adapters' job-record
# vocabulary; the monitoring subsystem never imports the adapters)
# ---------------------------------------------------------------------------

#: The ``failure_class`` value of a TRANSPORT failure (connection-level:
#: the scheduler or node is unreachable -- an ENGINEERING failure that
#: a pre-authorized retry may address). Mirrors
#: ``adapters/compute/ssh.py`` ``FAILURE_CLASS_TRANSPORT``.
FAILURE_CLASS_TRANSPORT: str = "transport"

#: The ``failure_class`` value of the job's OWN failure (non-zero exit,
#: batch-script failure, OOM, timeout -- a SCIENTIFIC compute failure:
#: never retried, never mutated, observed and refused). Mirrors
#: ``adapters/compute/ssh.py`` ``FAILURE_CLASS_JOB``.
FAILURE_CLASS_JOB: str = "job"

#: The normalized failure class of an unclassified failure (the
#: adapter recorded no ``failure_class``, i.e. ``None``): used only as
#: the stable key/id segment of unclassified decisions (the payload
#: still carries ``null``).
RETRY_FAILURE_CLASS_UNCLASSIFIED: str = "unclassified"

#: The canonical failure kind the mirrored ``"transport"`` class
#: bridges to: a connection-level loss on the SSH transport boundary
#: (unreachable host, dropped connection, timeout -- an engineering
#: failure; the ``workers/retry.py`` SS5 failure-kind vocabulary).
FAILURE_KIND_SSH_CONNECTION_LOST: str = "ssh_connection_lost"

#: The deterministic adapter-class -> policy-kind bridge table: the
#: mirrored adapter failure-class vocabulary maps onto the frozen
#: failure-kind vocabulary of the automatic retry policy. A class not
#: in the table passes through verbatim (a classifier may already
#: report a policy kind directly).
FAILURE_CLASS_TO_FAILURE_KIND: dict[str, str] = {
    FAILURE_CLASS_TRANSPORT: FAILURE_KIND_SSH_CONNECTION_LOST,
}


def failure_class_to_failure_kind(failure_class: str | None) -> str:
    """Bridge an adapter-recorded failure class to a policy failure kind.

    The deterministic bridge between the two vocabularies: a mirrored
    adapter class maps through :data:`FAILURE_CLASS_TO_FAILURE_KIND`
    (``"transport"`` -> ``"ssh_connection_lost"``); any other
    non-blank string passes through verbatim (a classifier may already
    report a policy kind such as ``"scheduler_node_failure"``); an
    unclassified failure (``None`` or blank) maps to
    :data:`RETRY_FAILURE_CLASS_UNCLASSIFIED`.
    """
    if failure_class is None or not failure_class.strip():
        return RETRY_FAILURE_CLASS_UNCLASSIFIED
    return FAILURE_CLASS_TO_FAILURE_KIND.get(failure_class, failure_class)


# ---------------------------------------------------------------------------
# The retry-decision event vocabulary (AC-03, auditable history)
# ---------------------------------------------------------------------------

#: Actor of the retry-decision events appended by the dispatcher: the
#: Execution Monitor (the same actor as reconciliation).
RETRY_ACTOR: str = "execution-monitor"

#: Event type of a retry decision record: one event per decision,
#: carrying the failure class, the failure kind, the consulted policy,
#: the attempt index, the decision, the routing and reasoning of the
#: frozen evaluator and -- when authorized -- the resubmitted external
#: identity in the payload.
RETRY_DECISION_EVENT_TYPE: str = "engineering_retry_decision"

#: The decision vocabulary: the retry was authorized and performed.
RETRY_DECISION_AUTHORIZED: str = "retry_authorized"

#: The decision vocabulary: the retry was refused (no policy entry
#: authorizes the failure).
RETRY_DECISION_REFUSED: str = "retry_refused"

#: The decision vocabulary: the failure invalidates the run (the
#: policy's ``invalidate_run_on``) -- an invalidation decision, never
#: a resubmission.
RETRY_DECISION_INVALIDATED: str = "run_invalidated"

#: The decision vocabulary: the failure is a scientific change the
#: policy routes to the Supervisor (``supervisor_required_changes`` /
#: the scientific-change vocabulary) -- a Supervisor-required change
#: decision, never a resubmission.
RETRY_DECISION_SUPERVISOR_REQUIRED: str = "supervisor_required_change"

#: The complete decision vocabulary (every value a decision record may
#: carry).
RETRY_DECISIONS: frozenset[str] = frozenset(
    {
        RETRY_DECISION_AUTHORIZED,
        RETRY_DECISION_REFUSED,
        RETRY_DECISION_INVALIDATED,
        RETRY_DECISION_SUPERVISOR_REQUIRED,
    }
)

#: Stable reason of an authorized retry decision record.
RETRY_AUTHORIZED_REASON: str = "engineering_failure_retry_authorized"

#: Stable reason of a refused retry decision record.
RETRY_REFUSED_REASON: str = "scientific_failure_retry_refused"

#: Stable reason of an invalidation decision record.
RETRY_INVALIDATED_REASON: str = "engineering_failure_run_invalidated"

#: Stable reason of a Supervisor-required change decision record.
RETRY_SUPERVISOR_REASON: str = "scientific_change_supervisor_required"

#: The stable event reason per decision (the auditable
#: reason-vocabulary of the decision records).
_DECISION_REASONS: dict[str, str] = {
    RETRY_DECISION_AUTHORIZED: RETRY_AUTHORIZED_REASON,
    RETRY_DECISION_REFUSED: RETRY_REFUSED_REASON,
    RETRY_DECISION_INVALIDATED: RETRY_INVALIDATED_REASON,
    RETRY_DECISION_SUPERVISOR_REQUIRED: RETRY_SUPERVISOR_REASON,
}

#: Prefix of the deterministic idempotency key under which a decision
#: record is appended (``retry.decision:<run_id>:<failure
#: class>:attempt-<n>``): the attempt index is part of the decision
#: identity, so each identical retry is recorded exactly once (AC-03)
#: and the resubmission is performed at most once per recorded
#: decision (AC-01) while the ``max_identical_retries`` ceiling stays
#: enforceable.
RETRY_DECISION_KEY_PREFIX: str = "retry.decision"


# ---------------------------------------------------------------------------
# Errors (stable MonitoringError subclasses)
# ---------------------------------------------------------------------------


class RetryError(MonitoringError):
    """Base error of the engineering retry dispatcher."""


class RetryContractError(RetryError):
    """Raised when a retry decision hits a contract violation: a
    decision is requested for a run whose lifecycle cannot carry a
    retry (pre-external states ``CREATED``/``READY``/``DISPATCHED`` --
    no external failure exists -- or the result-bearing/terminal states
    ``RESULT_AVAILABLE``/``ANALYZING``/``SUBMITTED_FOR_REVIEW``/
    ``CLOSED``/``CANCELLED``/``INVALIDATED`` -- no failure can be
    retried onto a finished run), the Run record's external identity
    disagrees with the watch entry's (the monitor would resubmit under
    a mismatched identity), or the resubmission hook returns an
    identity that is not an identical resubmission (a different
    backend, or no addressable external id)."""


class CorruptRetryStateError(RetryError):
    """Raised when retry state is corrupt: the watch entry references a
    run with no run record in the run store, the stored run record
    cannot be read/parsed, the run's goal record or the referenced
    automatic retry policy record cannot be read/parsed (or the policy
    cannot serve as a contract), or a recorded retry decision in the
    event log is malformed. Corrupt persisted state fails loudly, never
    silently."""


# ---------------------------------------------------------------------------
# The injected hooks
# ---------------------------------------------------------------------------

#: The injected failure classifier: a callable taking the external
#: identity (a ``RunExternal``) of a watched run and returning the
#: adapter-recorded failure class of its failed job -- the mirrored
#: vocabulary ``"transport"`` | ``"job"`` | ``None``, or a policy
#: failure-kind string directly (any other string passes through the
#: bridge verbatim and is refused unless the policy whitelists it). A
#: classifier raising an exception is a transient classification
#: failure: it is treated as unclassified (refused and recorded); the
#: exception message itself is never persisted.
FailureClassifier: TypeAlias = Callable[[RunExternal], str | None]

#: The injected resubmission hook: a callable taking the external
#: identity of the failed run and performing the IDENTICAL
#: resubmission, returning the new external identity of the
#: resubmission (the receipt recorded in the decision event). Never an
#: adapters import -- the resubmission itself belongs to the caller's
#: adapter plumbing. The hook's exceptions propagate to the caller
#: (nothing is recorded, the decision stays re-issuable).
ResubmitHook: TypeAlias = Callable[[RunExternal], RunExternal]


def _unclassified_classifier(_external: RunExternal) -> None:
    """The default classifier: always reports unclassified (``None``).

    With no classifier injected the dispatcher can never authorize a
    retry (AC-02): the default configuration records refused decisions
    only.
    """
    return None


def _no_resubmit_hook(_external: RunExternal) -> RunExternal:
    """The default resubmission hook: raises loudly.

    The default configuration has no adapter plumbing, so an authorized
    retry decision cannot be performed. The default never silently
    drops a resubmission: it fails loudly instead (no silent no-op).
    """
    raise RetryError(
        "no resubmission hook is injected: an authorized engineering"
        " retry decision cannot be performed; inject a resubmit hook to"
        " enable resubmission (the default never silently drops a"
        " resubmission)"
    )


# ---------------------------------------------------------------------------
# The retry decision records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryOutcome:
    """The outcome of deciding one failed external Run.

    Attributes:
        run_id: the decided run.
        failure_class: the adapter-recorded failure class of the
            failed external job (``None`` when unclassified).
        decision: one of :data:`RETRY_DECISIONS`
            (``retry_authorized`` / ``retry_refused`` /
            ``run_invalidated`` / ``supervisor_required_change``).
        decided_at: the injected clock stamp of the decision.
        resubmitted_external: the resubmitted external identity (the
            receipt of the resubmission hook) for an authorized
            decision; always None for any other decision.
        replayed: True iff this pass resolved an already-recorded
            decision (the recorded history is authoritative; nothing
            was performed this pass and the hook was not invoked).
        event_id: the deterministic id of the decision record in the
            event log.
        failure_kind: the policy failure kind the failure class
            bridged to (``""`` for records predating the policy
            vocabulary).
        policy_id: the consulted automatic retry policy (``None`` when
            the goal references no policy).
        attempt: the identical-retry index of this decision (0-based;
            the count of recorded authorized decisions of the same
            (run, failure class) before this one).
        routing: the frozen evaluator's routing (``"AUTOMATIC"`` /
            ``"SUPERVISOR"``; ``""`` for records predating the policy
            vocabulary).
        matched_rule_id: the frozen rule-table rule that decided
            (``None`` when the goal references no policy).
        reasoning_ids: the frozen reasoning ids of the decision.
    """

    run_id: str
    failure_class: str | None
    decision: str
    decided_at: str
    resubmitted_external: RunExternal | None = None
    replayed: bool = False
    event_id: str = ""
    failure_kind: str = ""
    policy_id: str | None = None
    attempt: int = 0
    routing: str = ""
    matched_rule_id: str | None = None
    reasoning_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                f"RetryOutcome.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if self.failure_class is not None and not isinstance(
            self.failure_class, str
        ):
            raise TypeError(
                "RetryOutcome.failure_class must be a str or None, got"
                f" {type(self.failure_class).__name__}"
            )
        if self.decision not in RETRY_DECISIONS:
            raise RetryError(
                f"RetryOutcome.decision {self.decision!r} is not one of"
                f" {', '.join(sorted(RETRY_DECISIONS))!r}"
            )
        if not isinstance(self.decided_at, str) or not self.decided_at:
            raise RetryError(
                "RetryOutcome.decided_at must be a non-empty timestamp"
                f" string, got {self.decided_at!r}"
            )
        if self.resubmitted_external is not None and not isinstance(
            self.resubmitted_external, RunExternal
        ):
            raise TypeError(
                "RetryOutcome.resubmitted_external must be a RunExternal"
                " or None, got"
                f" {type(self.resubmitted_external).__name__}"
            )
        if not isinstance(self.replayed, bool):
            raise TypeError(
                "RetryOutcome.replayed must be a bool, got"
                f" {type(self.replayed).__name__}"
            )
        if not isinstance(self.event_id, str) or not is_valid_id(
            self.event_id, "event"
        ):
            raise RetryError(
                f"RetryOutcome.event_id {self.event_id!r} is not a valid"
                " event id (sr_event_<32 hex chars>)"
            )
        if not isinstance(self.failure_kind, str):
            raise TypeError(
                "RetryOutcome.failure_kind must be a str, got"
                f" {type(self.failure_kind).__name__}"
            )
        if self.policy_id is not None and not isinstance(self.policy_id, str):
            raise TypeError(
                "RetryOutcome.policy_id must be a str or None, got"
                f" {type(self.policy_id).__name__}"
            )
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError(
                "RetryOutcome.attempt must be an int, got"
                f" {type(self.attempt).__name__}"
            )
        if self.attempt < 0:
            raise RetryError(
                f"RetryOutcome.attempt must be >= 0, got {self.attempt}"
            )
        if not isinstance(self.routing, str):
            raise TypeError(
                "RetryOutcome.routing must be a str, got"
                f" {type(self.routing).__name__}"
            )
        if self.matched_rule_id is not None and not isinstance(
            self.matched_rule_id, str
        ):
            raise TypeError(
                "RetryOutcome.matched_rule_id must be a str or None, got"
                f" {type(self.matched_rule_id).__name__}"
            )
        if not isinstance(self.reasoning_ids, tuple) or not all(
            isinstance(reason, str) for reason in self.reasoning_ids
        ):
            raise TypeError(
                "RetryOutcome.reasoning_ids must be a tuple of str, got"
                f" {type(self.reasoning_ids).__name__}"
            )
        if (self.decision == RETRY_DECISION_AUTHORIZED) != (
            self.resubmitted_external is not None
        ):
            raise RetryError(
                "RetryOutcome invariant violation: an authorized decision"
                " always carries the resubmitted external identity, any"
                " other decision never does"
            )


@dataclass(frozen=True)
class RetrySkipped:
    """The skipped outcome of one watched run in a pass-level decision:
    the run's lifecycle cannot carry a retry decision (it is not
    ``RUNNING_EXTERNAL`` -- a completed, result-bearing or terminal
    state), so the pass records the skip and continues (issue #152: a
    mixed watch set no longer aborts the pass).

    Attributes:
        run_id: the skipped run.
        lifecycle_state: the stable lifecycle-state value of the run
            (the reason the decision was skipped).
        skipped_at: the injected clock stamp of the skip.
    """

    run_id: str
    lifecycle_state: str
    skipped_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                f"RetrySkipped.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        valid_states = {state.value for state in LifecycleState}
        if self.lifecycle_state not in valid_states:
            raise RetryError(
                f"RetrySkipped.lifecycle_state {self.lifecycle_state!r} is"
                " not a valid lifecycle-state value"
                f" {sorted(valid_states)!r}"
            )
        if self.lifecycle_state == LifecycleState.RUNNING_EXTERNAL.value:
            raise RetryError(
                "RetrySkipped invariant violation: a RUNNING_EXTERNAL run"
                " is eligible for a retry decision and can never be skipped"
            )
        if not isinstance(self.skipped_at, str) or not self.skipped_at:
            raise RetryError(
                "RetrySkipped.skipped_at must be a non-empty timestamp"
                f" string, got {self.skipped_at!r}"
            )


@dataclass(frozen=True)
class RetryFailure:
    """The failed outcome of deciding one watched run: the per-run
    error-isolation record of the pass-level API.

    ``decide_all`` records a run whose decision raises a stable per-run
    ``RetryError`` (``RetryContractError`` or ``CorruptRetryStateError``)
    here instead of aborting the pass (issue #152): the failing entry is
    skipped and the remaining watched runs still get decided. Corrupt
    project-level state (an unreadable watch set) still fails the pass
    loudly.

    Attributes:
        run_id: the run whose decision failed.
        error: the stable error class name (``RetryContractError`` or
            ``CorruptRetryStateError``).
        message: the error message (diagnostics for the operator; never
            persisted).
    """

    run_id: str
    error: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str):
            raise TypeError(
                f"RetryFailure.run_id must be a str, got"
                f" {type(self.run_id).__name__}"
            )
        if not isinstance(self.error, str) or not self.error:
            raise RetryError(
                "RetryFailure.error must be a non-empty error class name,"
                f" got {self.error!r}"
            )
        if not isinstance(self.message, str):
            raise TypeError(
                "RetryFailure.message must be a str, got"
                f" {type(self.message).__name__}"
            )


@dataclass(frozen=True)
class RetrySummary:
    """The outcome of deciding the full watch set.

    ``outcomes`` is the per-run outcomes in sorted run-id order
    (deterministic); ``authorized_count`` counts the authorized
    decisions of this pass, ``refused_count`` every non-authorized
    decision (refused, invalidated and supervisor-routed); ``skipped``
    is the per-run skipped outcomes (runs whose lifecycle cannot carry
    a retry) and ``failures`` the per-run failed outcomes (issue #152
    error isolation), both in sorted run-id order.
    """

    monitor_id: str
    decided_at: str
    outcomes: tuple[RetryOutcome, ...] = ()
    authorized_count: int = 0
    refused_count: int = 0
    skipped: tuple[RetrySkipped, ...] = ()
    failures: tuple[RetryFailure, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.monitor_id, str) or not is_valid_id(
            self.monitor_id, MONITOR_ID_KIND
        ):
            raise RetryError(
                f"RetrySummary.monitor_id {self.monitor_id!r} is not a"
                " valid monitor id (sr_monitor_<32 hex chars>)"
            )
        if not isinstance(self.decided_at, str) or not self.decided_at:
            raise RetryError(
                "RetrySummary.decided_at must be a non-empty timestamp"
                f" string, got {self.decided_at!r}"
            )
        if not isinstance(self.outcomes, tuple):
            raise TypeError(
                "RetrySummary.outcomes must be a tuple of RetryOutcome"
                f" entries, got {type(self.outcomes).__name__}"
            )
        for outcome in self.outcomes:
            if not isinstance(outcome, RetryOutcome):
                raise TypeError(
                    "RetrySummary.outcomes entries must be RetryOutcome,"
                    f" got {type(outcome).__name__}"
                )
        for name in ("authorized_count", "refused_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"RetrySummary.{name} must be an int, got"
                    f" {type(value).__name__}"
                )
        run_ids = [outcome.run_id for outcome in self.outcomes]
        if run_ids != sorted(run_ids):
            raise RetryError(
                "RetrySummary.outcomes must be sorted by run_id"
                " (deterministic order)"
            )
        if self.authorized_count != sum(
            1 for o in self.outcomes if o.decision == RETRY_DECISION_AUTHORIZED
        ):
            raise RetryError(
                "RetrySummary.authorized_count must equal the number of"
                " authorized outcomes"
            )
        if self.refused_count != sum(
            1 for o in self.outcomes if o.decision != RETRY_DECISION_AUTHORIZED
        ):
            raise RetryError(
                "RetrySummary.refused_count must equal the number of"
                " non-authorized outcomes"
            )
        for name, record_type in (
            ("skipped", RetrySkipped),
            ("failures", RetryFailure),
        ):
            records = getattr(self, name)
            if not isinstance(records, tuple):
                raise TypeError(
                    f"RetrySummary.{name} must be a tuple of"
                    f" {record_type.__name__} entries, got"
                    f" {type(records).__name__}"
                )
            for record in records:
                if not isinstance(record, record_type):
                    raise TypeError(
                        f"RetrySummary.{name} entries must be"
                        f" {record_type.__name__}, got {type(record).__name__}"
                    )
            record_ids = [record.run_id for record in records]
            if record_ids != sorted(record_ids):
                raise RetryError(
                    f"RetrySummary.{name} must be sorted by run_id"
                    " (deterministic order)"
                )
        outcome_ids = {outcome.run_id for outcome in self.outcomes}
        skipped_ids = {record.run_id for record in self.skipped}
        failure_ids = {record.run_id for record in self.failures}
        if outcome_ids & skipped_ids or outcome_ids & failure_ids or (
            skipped_ids & failure_ids
        ):
            raise RetryError(
                "a run cannot appear in more than one of the outcomes,"
                " skipped and failures groups of one decision pass"
            )


# ---------------------------------------------------------------------------
# The retry dispatcher
# ---------------------------------------------------------------------------


class RetryDispatcher:
    """Deterministic engineering retry decisions for watched Runs.

    Decides each failed external Run of the watched-Run registry
    (DEV-M8-G01) against the Goal's frozen automatic retry policy --
    resolved through ``goal.automatic_retry_policy_ref`` and the frozen
    ``workers/retry.py`` evaluator -- using the durable Run store and
    the append-only event log, and records every decision as an
    auditable event (DEV-M8-G03):

    * AC-01: a failure kind the policy whitelists triggers an
      IDENTICAL resubmission through the injected resubmission hook --
      same run identity, same external identity semantics (same
      backend), no parameter change -- and one
      ``engineering_retry_decision`` event is appended under a
      deterministic attempt-indexed idempotency key. Re-deciding the
      same failure generation resolves to the recorded decision and
      never re-invokes the hook (exactly-once per recorded decision).
    * AC-02: a scientific compute failure -- any failure kind no
      policy entry authorizes (``FAILURE_CLASS_JOB``, ``None``, an
      unrecognized string, a Goal with no policy) -- is observed and
      recorded as a refused decision: the hook is never invoked and no
      run parameter is ever mutated (the dispatcher never writes the
      run store). ``invalidate_run_on`` kinds decide an invalidation,
      ``supervisor_required_changes`` kinds a Supervisor-required
      change -- both recorded, never resubmitted. With no classifier
      injected, the default configuration can never authorize a retry.
    * AC-03: a fresh dispatcher over the same state directory, run
      store and event log reconstructs the retry history from the
      recorded events alone; re-deciding the same failures yields
      identical outcomes and identical durable bytes.

    The dispatcher persists nothing itself: every write goes through
    the registry, the injected run store and the injected event log.

    Args:
        state_dir: the monitor's durable state directory (watch entries
            at ``<state_dir>/watched/``).
        now: injectable clock producing a timestamp string (default
            ``utc_now``) -- no wall clock in the tested path.
        monitor_id: the Monitor identity (``sr_monitor_<32 hex>``).
            Defaults to the deterministic identity of the state
            directory.
        classifier: the injected failure classifier (default: always
            unclassified -- the default configuration never authorizes
            a retry).
        resubmit: the injected resubmission hook performing the
            identical resubmission (default: a hook that raises
            loudly -- the default never silently drops a
            resubmission).
        run_store: the durable Run store the dispatcher reads Run,
            Goal and retry-policy records through (default:
            ``FilesystemStateBackend(state_dir)`` -- the canonical tree
            directories).
        event_log: the append-only event log the dispatcher appends
            decision records to (default: ``ProjectEventLog(state_dir)``
            -- events at ``<state_dir>/events/``, the canonical tree
            directory).

    Raises:
        TypeError: ``state_dir`` is not a str/Path, ``now``,
            ``classifier`` or ``resubmit`` is not callable, ``run_store``
            is not a ``StateBackend``, or ``event_log`` is not a
            ``ProjectEventLog``.
        MonitoringError: an injected ``monitor_id`` is not a valid
            monitor id.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        now: MonitoringClock | None = None,
        monitor_id: str | None = None,
        classifier: FailureClassifier | None = None,
        resubmit: ResubmitHook | None = None,
        run_store: StateBackend | None = None,
        event_log: ProjectEventLog | None = None,
    ) -> None:
        if not isinstance(state_dir, (str, Path)):
            raise TypeError(
                "state_dir must be a str or Path, got"
                f" {type(state_dir).__name__}"
            )
        if now is not None and not callable(now):
            raise TypeError(
                f"now must be callable, got {type(now).__name__}"
            )
        if classifier is not None and not callable(classifier):
            raise TypeError(
                f"classifier must be callable, got {type(classifier).__name__}"
            )
        if resubmit is not None and not callable(resubmit):
            raise TypeError(
                f"resubmit must be callable, got {type(resubmit).__name__}"
            )
        if run_store is not None and not isinstance(run_store, StateBackend):
            raise TypeError(
                "run_store must be a StateBackend, got"
                f" {type(run_store).__name__}"
            )
        if event_log is not None and not isinstance(event_log, ProjectEventLog):
            raise TypeError(
                "event_log must be a ProjectEventLog, got"
                f" {type(event_log).__name__}"
            )
        self._state_dir = Path(state_dir)
        self._now_fn = now if now is not None else utc_now
        self._classifier = (
            classifier if classifier is not None else _unclassified_classifier
        )
        self._resubmit_fn = (
            resubmit if resubmit is not None else _no_resubmit_hook
        )
        # The registry validates the injected monitor_id (stable
        # MonitoringError).
        self._registry = WatchedRunRegistry(
            self._state_dir,
            now=self._now_fn,
            monitor_id=monitor_id,
        )
        self._monitor_id = self._registry.monitor_id
        self._run_store = (
            run_store
            if run_store is not None
            else FilesystemStateBackend(self._state_dir)
        )
        self._event_log = (
            event_log
            if event_log is not None
            else ProjectEventLog(self._state_dir)
        )

    # -- identity and injected dependencies ---------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected monitor state directory."""
        return self._state_dir

    @property
    def monitor_id(self) -> str:
        """The Monitor identity owning this dispatcher."""
        return self._monitor_id

    @property
    def registry(self) -> WatchedRunRegistry:
        """The watched-Run registry of this dispatcher."""
        return self._registry

    @property
    def classifier(self) -> FailureClassifier:
        """The injected failure classifier."""
        return self._classifier

    @property
    def resubmit(self) -> ResubmitHook:
        """The injected resubmission hook."""
        return self._resubmit_fn

    @property
    def run_store(self) -> StateBackend:
        """The durable Run store this dispatcher reads through."""
        return self._run_store

    @property
    def event_log(self) -> ProjectEventLog:
        """The event log this dispatcher appends decision records to."""
        return self._event_log

    # -- the retry decision -------------------------------------------------

    def decide(self, run_id: str, failure_class: str | None) -> RetryOutcome:
        """Decide one failed external Run and record the decision.

        The decision consults the Goal's frozen automatic retry policy
        (resolved through ``goal.automatic_retry_policy_ref``) and is
        routed through the frozen ``workers/retry.py`` evaluator: a
        failure kind the policy authorizes triggers an identical
        resubmission through the injected resubmission hook (AC-01);
        every other failure is recorded as a refused, invalidated or
        Supervisor-routed decision and never resubmitted (AC-02). The
        decision is recorded through the real event log under the
        deterministic attempt-indexed idempotency key; re-deciding a
        recorded decision of the current failure generation returns the
        recorded history (``replayed=True``) and never re-invokes the
        hook (AC-01 exactly-once / AC-03 replay).

        Args:
            run_id: the watched run to decide.
            failure_class: the adapter-recorded failure class of the
                failed external job -- the mirrored vocabulary
                ``"transport"`` | ``"job"`` | ``None``, or a policy
                failure-kind string directly.

        Returns:
            The :class:`RetryOutcome` of this decision.

        Raises:
            TypeError: ``run_id`` is not a str, or ``failure_class`` is
                neither a str nor None.
            WatchNotFoundError: the run is not watched.
            RetryContractError: the decision is requested for a run
                whose lifecycle cannot carry a retry, the Run record's
                external identity disagrees with the watch entry, or
                the resubmission hook returns an identity that is not
                an identical resubmission.
            CorruptRetryStateError: the run record is missing or
                corrupt, the run's goal record or the referenced
                automatic retry policy cannot be read or cannot serve
                as a contract, or the recorded decision in the event
                log is malformed.
            RetryError: no resubmission hook is injected and the
                decision is authorized (the loud default hook).
        """
        if not isinstance(run_id, str):
            raise TypeError(
                f"run_id must be a str, got {type(run_id).__name__}"
            )
        if failure_class is not None and not isinstance(failure_class, str):
            raise TypeError(
                "failure_class must be a str or None, got"
                f" {type(failure_class).__name__}"
            )
        normalized = (
            failure_class
            if failure_class is not None
            else RETRY_FAILURE_CLASS_UNCLASSIFIED
        )
        failure_kind = failure_class_to_failure_kind(failure_class)

        # Exactly-once: the recorded decision is the durable "retry was
        # performed" fact. Resolve the recorded history BEFORE touching
        # the resubmission hook or the run store, so re-deciding (or
        # restart-replaying) the same failure generation returns the
        # recorded decision and never re-executes (AC-01/AC-03). A
        # decision belongs to the generation it was decided for -- the
        # recorded ``external`` identity; a fresh external identity
        # (e.g. the resubmission itself failing again) advances to the
        # next attempt.
        records = self._recorded_decisions(run_id, failure_class)
        try:
            watch = self._registry.get(run_id)
        except WatchNotFoundError:
            if records:
                return self._replay_outcome(run_id, records[-1])
            raise
        matching = self._matching_record(run_id, records, watch.external)
        if matching is not None:
            return self._replay_outcome(run_id, matching)

        run = self._read_run(run_id)
        self._check_external_identity(watch, run)
        if run.lifecycle_state is not LifecycleState.RUNNING_EXTERNAL:
            raise RetryContractError(
                f"retry decision requested for run {run_id!r} whose"
                f" lifecycle state {run.lifecycle_state.value!r} cannot"
                " carry a retry (only RUNNING_EXTERNAL runs with a failed"
                " external job can be retried); the decision is refused"
                " as a contract violation"
            )
        attempt = _count_authorized_decisions(records)
        event_id = generate_id(
            "event",
            RETRY_DECISION_EVENT_TYPE,
            run_id,
            normalized,
            f"attempt-{attempt}",
        )

        # The automatic retry policy decision (AC-01/AC-02): the frozen
        # evaluator decides; the dispatcher maps its verdict/routing
        # onto the decision vocabulary (never reimplements the policy
        # semantics). A Goal with no retry policy ref authorizes
        # nothing (the refusal default).
        policy = self._resolve_policy(run_id, run)
        reasoning_ids: tuple[str, ...]
        if policy is None:
            decision = RETRY_DECISION_REFUSED
            routing = RetryRouting.AUTOMATIC.value
            matched_rule_id = None
            reasoning_ids = (REASON_NO_POLICY_ENTRY,)
        else:
            try:
                assessment = evaluate_automatic_retry(
                    RetryEvaluationInput(
                        policy=policy,
                        failure_kind=failure_kind,
                        identical_retry_count=attempt,
                        checkpoint_continuation=(
                            failure_kind == CHECKPOINT_CONTINUATION_KIND
                        ),
                    )
                )
            except RetryPolicyError as exc:
                raise CorruptRetryStateError(
                    f"corrupt retry state for run {run_id!r}: the frozen"
                    f" automatic retry policy {policy.policy_id!r} cannot"
                    f" be evaluated: {exc}"
                ) from exc
            routing = assessment.routing.value
            matched_rule_id = assessment.matched_rule_id
            reasoning_ids = assessment.reasoning_ids
            if assessment.verdict is RetryAuthorization.AUTHORIZED:
                decision = RETRY_DECISION_AUTHORIZED
            elif assessment.routing is RetryRouting.SUPERVISOR:
                decision = RETRY_DECISION_SUPERVISOR_REQUIRED
            elif REASON_INVALIDATE_RUN in assessment.reasoning_ids:
                decision = RETRY_DECISION_INVALIDATED
            else:
                decision = RETRY_DECISION_REFUSED

        stamp = self._now_fn()

        resubmitted: RunExternal | None = None
        if decision == RETRY_DECISION_AUTHORIZED:
            resubmitted = self._resubmit_fn(watch.external)
            if not isinstance(resubmitted, RunExternal):
                raise TypeError(
                    "the resubmission hook must return a RunExternal (the"
                    " resubmitted external identity), got"
                    f" {type(resubmitted).__name__}"
                )
            if resubmitted.backend != watch.external.backend:
                raise RetryContractError(
                    f"run {run_id!r} resubmission changed the backend from"
                    f" {watch.external.backend!r} to"
                    f" {resubmitted.backend!r}; an identical resubmission"
                    " never changes the compute backend"
                )
            try:
                validate_external_identity(
                    resubmitted, error=RetryContractError
                )
            except RetryContractError as exc:
                raise RetryContractError(
                    f"run {run_id!r} resubmission is not addressable: {exc}"
                ) from exc

        policy_id = policy.policy_id if policy is not None else None
        payload: dict[str, Any] = {
            "failure_class": failure_class,
            "failure_kind": failure_kind,
            "policy_id": policy_id,
            "attempt": attempt,
            "decision": decision,
            "routing": routing,
            "matched_rule_id": matched_rule_id,
            "reasoning_ids": list(reasoning_ids),
            "external": watch.external.to_dict(),
        }
        if resubmitted is not None:
            payload["resubmitted_external"] = resubmitted.to_dict()
        self._event_log.append(
            self._decision_event(event_id, run_id, stamp, decision, payload),
            idempotency_key=f"{RETRY_DECISION_KEY_PREFIX}:{run_id}:"
            f"{normalized}:attempt-{attempt}",
        )
        return RetryOutcome(
            run_id=run_id,
            failure_class=failure_class,
            decision=decision,
            decided_at=stamp,
            resubmitted_external=resubmitted,
            replayed=False,
            event_id=event_id,
            failure_kind=failure_kind,
            policy_id=policy_id,
            attempt=attempt,
            routing=routing,
            matched_rule_id=matched_rule_id,
            reasoning_ids=reasoning_ids,
        )

    def decide_all(self) -> RetrySummary:
        """Decide every watched run (sorted run-id order) and return
        the summary, classifying each eligible run through the injected
        failure classifier.

        Per-run isolation (issue #152): only runs whose lifecycle is
        ``RUNNING_EXTERNAL`` are eligible for a decision; a watched run
        at any other lifecycle (``RESULT_AVAILABLE``, terminal states)
        is recorded as skipped (``skipped``) -- never aborted -- and
        the classifier is not invoked for it. A run whose decision
        raises a stable per-run ``RetryError`` (``RetryContractError``
        or ``CorruptRetryStateError``) is recorded as a per-run failed
        outcome (``failures``) with the stable error, and the pass
        continues with the remaining runs. Corrupt project-level state
        (an unreadable watch set), a classifier/type contract violation
        or the loud default hook (no resubmission hook for an
        authorized decision) still fails the whole pass loudly
        (deterministic sorted order, deterministic error)."""
        outcomes: list[RetryOutcome] = []
        skipped: list[RetrySkipped] = []
        failures: list[RetryFailure] = []
        for record in self._registry.list_watched():
            run_id = record.run_id
            try:
                run = self._read_run(run_id)
            except CorruptRetryStateError as exc:
                failures.append(
                    RetryFailure(
                        run_id=run_id,
                        error=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue
            if run.lifecycle_state is not LifecycleState.RUNNING_EXTERNAL:
                skipped.append(
                    RetrySkipped(
                        run_id=run_id,
                        lifecycle_state=run.lifecycle_state.value,
                        skipped_at=self._now_fn(),
                    )
                )
                continue
            try:
                outcomes.append(
                    self.decide(run_id, self._classify(record.external))
                )
            except (RetryContractError, CorruptRetryStateError) as exc:
                failures.append(
                    RetryFailure(
                        run_id=run_id,
                        error=type(exc).__name__,
                        message=str(exc),
                    )
                )
        return RetrySummary(
            monitor_id=self._monitor_id,
            decided_at=self._now_fn(),
            outcomes=tuple(outcomes),
            authorized_count=sum(
                1
                for o in outcomes
                if o.decision == RETRY_DECISION_AUTHORIZED
            ),
            refused_count=sum(
                1 for o in outcomes if o.decision != RETRY_DECISION_AUTHORIZED
            ),
            skipped=tuple(skipped),
            failures=tuple(failures),
        )

    # -- internals ----------------------------------------------------------

    def _classify(self, external: RunExternal) -> str | None:
        """Classify the failure of ``external`` through the injected
        classifier.

        A classifier exception is a transient classification failure:
        it carries no class information and is treated as unclassified
        (``None`` -- refused and recorded, AC-02); its message is never
        recorded (no secrets in durable state). A non-str, non-None
        return is a classifier contract violation and fails loudly.
        """
        try:
            failure_class = self._classifier(external)
        except Exception:
            return None
        if failure_class is not None and not isinstance(failure_class, str):
            raise TypeError(
                "the failure classifier must return a str failure class"
                " or None, got"
                f" {type(failure_class).__name__}"
            )
        return failure_class

    def _read_run(self, run_id: str) -> Run:
        """Read the durable Run record through the injected run store.

        Raises:
            CorruptRetryStateError: the run record is missing or
                corrupt.
        """
        try:
            data = self._run_store.read("run", run_id)
            return Run.from_dict(data)
        except (FileNotFoundError, ValueError) as exc:
            raise CorruptRetryStateError(
                f"corrupt retry state for run {run_id!r}: the watch entry"
                f" references a run record that cannot be read: {exc}"
            ) from exc

    def _read_goal(self, run_id: str, run: Run) -> GoalContract:
        """Read the durable Goal record of the run through the injected
        run store (the goal the frozen retry policy is resolved from).

        Raises:
            CorruptRetryStateError: the goal record is missing or
                corrupt.
        """
        try:
            data = self._run_store.read("goal", run.goal_id)
            return GoalContract.from_dict(data)
        except (FileNotFoundError, ValueError) as exc:
            raise CorruptRetryStateError(
                f"corrupt retry state for run {run_id!r}: the run's goal"
                f" record {run.goal_id!r} cannot be read: {exc}"
            ) from exc

    def _resolve_policy(
        self, run_id: str, run: Run
    ) -> AutomaticRetryPolicy | None:
        """Resolve the run's frozen automatic retry policy record.

        The goal's ``automatic_retry_policy_ref`` names the policy in
        the retry-policy registry; a goal without a ref has no policy
        (the refusal default -- nothing is ever authorized without a
        contract).

        Raises:
            CorruptRetryStateError: the referenced retry-policy record
                is missing, corrupt, or does not carry the referenced
                policy id.
        """
        goal = self._read_goal(run_id, run)
        ref = goal.automatic_retry_policy_ref
        if ref is None:
            return None
        try:
            data = self._run_store.read("retry-policy", ref)
            policy = AutomaticRetryPolicy.from_dict(data)
        except (FileNotFoundError, ValueError) as exc:
            raise CorruptRetryStateError(
                f"corrupt retry state for run {run_id!r}: the goal's"
                f" automatic retry policy ref {ref!r} resolves to a"
                f" retry-policy record that cannot be read: {exc}"
            ) from exc
        if policy.policy_id != ref:
            raise CorruptRetryStateError(
                f"corrupt retry state for run {run_id!r}: the retry-policy"
                f" record {ref!r} carries policy_id {policy.policy_id!r}"
                " (a record's identity must match its ref)"
            )
        return policy

    def _recorded_decisions(
        self, run_id: str, failure_class: str | None
    ) -> list[ProjectEvent]:
        """The recorded decision history of this (run, failure class)
        in append order -- every recorded decision of the failure,
        whatever the attempt."""
        return [
            record.event
            for record in self._event_log.list_events()
            if record.event.event_type == RETRY_DECISION_EVENT_TYPE
            and record.event.run_id == run_id
            and record.event.payload.get("failure_class") == failure_class
        ]

    def _matching_record(
        self,
        run_id: str,
        records: list[ProjectEvent],
        external: RunExternal,
    ) -> ProjectEvent | None:
        """The latest recorded decision of the current failure
        generation: the decision whose recorded ``external`` identity
        equals the watch entry's (a record predating the field matches
        any generation -- recorded history stays authoritative).

        Raises:
            CorruptRetryStateError: a recorded decision carries a
                malformed external identity.
        """
        current = external.to_dict()
        for event in reversed(records):
            payload = event.payload
            if "external" not in payload:
                return event
            recorded = payload["external"]
            if not isinstance(recorded, Mapping):
                raise CorruptRetryStateError(
                    f"recorded retry decision {event.event_id!r} for run"
                    f" {run_id!r} carries a malformed external identity"
                )
            if recorded == current:
                return event
        return None

    def _check_external_identity(
        self, watch: WatchedRunRecord, run: Run
    ) -> None:
        """The Run record's external identity, when it names one, must
        agree with the watch entry's (the dispatcher must not resubmit
        a run under a different external identity than its durable
        record claims).

        Raises:
            RetryContractError: the identities disagree.
        """
        run_external = run.external
        if run_external is None:
            return
        watch_external = watch.external
        if run_external.backend != watch_external.backend:
            raise RetryContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record backend {run_external.backend!r}"
                f" vs watch backend {watch_external.backend!r}; retry"
                " refuses to resubmit under a mismatched identity"
            )
        if (
            run_external.job_id is not None
            and run_external.job_id != watch_external.job_id
        ):
            raise RetryContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record job_id {run_external.job_id!r}"
                f" vs watch job_id {watch_external.job_id!r}"
            )
        if (
            run_external.dispatch_id is not None
            and run_external.dispatch_id != watch_external.dispatch_id
        ):
            raise RetryContractError(
                f"run {run.run_id!r} external identity disagrees with its"
                f" watch entry: run record dispatch_id"
                f" {run_external.dispatch_id!r} vs watch dispatch_id"
                f" {watch_external.dispatch_id!r}"
            )

    def _replay_outcome(self, run_id: str, event: ProjectEvent) -> RetryOutcome:
        """Rebuild the :class:`RetryOutcome` of an already-recorded
        decision from the event log record alone (AC-03): the recorded
        history is authoritative -- the original stamp and the recorded
        resubmission receipt -- and nothing is performed this pass.
        Payload fields of the policy vocabulary are validated only when
        present (records predating them stay replayable).

        Raises:
            CorruptRetryStateError: the recorded decision is malformed
                (an unknown decision, a mistyped payload field or a
                malformed resubmitted identity).
        """
        event_id = event.event_id
        payload = event.payload
        decision = payload.get("decision")
        if decision not in RETRY_DECISIONS:
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries an unknown decision {decision!r}; expected one"
                f" of {', '.join(sorted(RETRY_DECISIONS))!r}"
            )
        failure_class = payload.get("failure_class")
        if failure_class is not None and not isinstance(failure_class, str):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped failure_class {failure_class!r}"
            )
        failure_kind = payload.get("failure_kind", "")
        if not isinstance(failure_kind, str):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped failure_kind {failure_kind!r}"
            )
        policy_id = payload.get("policy_id")
        if policy_id is not None and not isinstance(policy_id, str):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped policy_id {policy_id!r}"
            )
        attempt = payload.get("attempt", 0)
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped attempt {attempt!r}"
            )
        if attempt < 0:
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a negative attempt {attempt}"
            )
        routing = payload.get("routing", "")
        if not isinstance(routing, str):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped routing {routing!r}"
            )
        matched_rule_id = payload.get("matched_rule_id")
        if matched_rule_id is not None and not isinstance(
            matched_rule_id, str
        ):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped matched_rule_id {matched_rule_id!r}"
            )
        reasoning_ids = payload.get("reasoning_ids", [])
        if not isinstance(reasoning_ids, list) or not all(
            isinstance(reason, str) for reason in reasoning_ids
        ):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries mistyped reasoning_ids {reasoning_ids!r}"
            )
        resubmitted_raw = payload.get("resubmitted_external")
        resubmitted: RunExternal | None = None
        if resubmitted_raw is not None:
            if not isinstance(resubmitted_raw, Mapping):
                raise CorruptRetryStateError(
                    f"recorded retry decision {event_id!r} for run"
                    f" {run_id!r} carries a malformed resubmitted identity"
                )
            try:
                resubmitted = RunExternal.from_dict(resubmitted_raw)
            except (TypeError, ValueError) as exc:
                raise CorruptRetryStateError(
                    f"recorded retry decision {event_id!r} for run"
                    f" {run_id!r} carries a malformed resubmitted identity:"
                    f" {exc}"
                ) from exc
        return RetryOutcome(
            run_id=run_id,
            failure_class=failure_class,
            decision=decision,
            decided_at=event.timestamp,
            resubmitted_external=resubmitted,
            replayed=True,
            event_id=event_id,
            failure_kind=failure_kind,
            policy_id=policy_id,
            attempt=attempt,
            routing=routing,
            matched_rule_id=matched_rule_id,
            reasoning_ids=tuple(reasoning_ids),
        )

    def _decision_event(
        self,
        event_id: str,
        run_id: str,
        stamp: str,
        decision: str,
        payload: dict[str, Any],
    ) -> ProjectEvent:
        """The deterministic decision record (AC-03, auditable
        history): event type ``engineering_retry_decision``, actor
        ``execution-monitor``, the stable reason per decision, and the
        payload carrying the failure class and kind, the consulted
        policy, the attempt index, the decision, the routing and
        reasoning and the decided external identity (plus the
        resubmitted external identity when authorized)."""
        return ProjectEvent(
            event_id=event_id,
            timestamp=stamp,
            actor=RETRY_ACTOR,
            event_type=RETRY_DECISION_EVENT_TYPE,
            object_id=run_id,
            run_id=run_id,
            reason=_DECISION_REASONS[decision],
            payload=payload,
        )


def _count_authorized_decisions(records: list[ProjectEvent]) -> int:
    """The identical-retry index of a recorded decision history: how
    many authorized decisions this (run, failure class) already
    performed -- the ``identical_retry_count`` the frozen evaluator
    gates identical checkpoint continuation with (attempt 0 is the
    first retry). Refusals never advance the index."""
    return sum(
        1
        for event in records
        if event.payload.get("decision") == RETRY_DECISION_AUTHORIZED
    )
