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
(authorized and refused) as an auditable event through the real event
log with deterministic ids -- so a Monitor restart reconstructs the
full retry history from the durable state alone.

Failure-class vocabulary (mirrored, never imported)
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

Whitelist semantics (AC-01/AC-02)
---------------------------------
:data:`ENGINEERING_RETRY_WHITELIST` is the authorized failure-class
set (the engineering classes: ``FAILURE_CLASS_TRANSPORT`` and its
adapter variants -- today exactly ``"transport"``). A failure whose
adapter-recorded class is on the whitelist may trigger an **identical
resubmission**: the same Run identity, the same external identity
semantics (the same backend; the hook returns the fresh external id of
the resubmission), no parameter change of any kind -- the dispatcher
never writes the Run record and under no circumstance mutates run
parameters. Anything not on the whitelist -- the ``"job"`` class, an
unclassified ``None``, or any unrecognized string -- is a SCIENTIFIC
compute failure: it is observed and recorded as a refused decision and
never resubmits (safe-by-construction: the refusal is the default for
every class outside the whitelist).

Retry-decision event vocabulary (AC-03, auditable history)
----------------------------------------------------------
Every decision -- an authorized retry AND a refused retry -- is
appended through the real append-only ``ProjectEventLog`` as an
``engineering_retry_decision`` event (actor ``execution-monitor``,
object/run the decided Run, stable reason per decision:
``engineering_failure_retry_authorized`` /
``scientific_failure_retry_refused``), carrying the failure class, the
decision and the resubmitted external identity (when authorized) in
the payload. The event id is a pure function of the decision inputs
(``generate_id("event", "engineering_retry_decision", <run_id>,
<failure class>)``) and the append uses the deterministic idempotency
key ``retry.decision:<run_id>:<failure class>``, so re-deciding the
same failure resolves to the single original record and the log's
sequence never advances twice for the same decision.

Exactly-once resubmission
-------------------------
The recorded decision is the durable "retry was performed" fact: the
dispatcher resolves the decision record *before* touching the
resubmission hook, so a re-decided (or restart-replayed) decision
returns the recorded history and never re-invokes the hook -- the
resubmission happens at most once per recorded decision. A crash
between the resubmission and the append of the decision record
re-invokes the hook when the same decision is re-issued; once the
record exists, re-deciding is a pure idempotent replay.

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
corrupt retry state). No credentials are ever persisted: transient
classifier failures are recorded as unclassified refusals and their
messages never reach durable bytes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
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
    utc_now,
    validate_external_identity,
)

__all__ = [
    "CorruptRetryStateError",
    "ENGINEERING_RETRY_WHITELIST",
    "FAILURE_CLASS_JOB",
    "FAILURE_CLASS_TRANSPORT",
    "FailureClassifier",
    "RETRY_ACTOR",
    "RETRY_AUTHORIZED_REASON",
    "RETRY_DECISION_AUTHORIZED",
    "RETRY_DECISION_EVENT_TYPE",
    "RETRY_DECISION_KEY_PREFIX",
    "RETRY_DECISION_REFUSED",
    "RETRY_FAILURE_CLASS_UNCLASSIFIED",
    "RETRY_REFUSED_REASON",
    "ResubmitHook",
    "RetryContractError",
    "RetryDispatcher",
    "RetryError",
    "RetryOutcome",
    "RetrySummary",
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

#: The engineering retry whitelist (AC-01): the authorized failure
#: classes that may trigger an identical resubmission. Every failure
#: class outside this set -- ``FAILURE_CLASS_JOB``, ``None``
#: (unclassified) or any unrecognized string -- is a scientific
#: compute failure and is refused (AC-02): refusal is the
#: safe-by-construction default.
ENGINEERING_RETRY_WHITELIST: frozenset[str] = frozenset(
    {FAILURE_CLASS_TRANSPORT}
)

# ---------------------------------------------------------------------------
# The retry-decision event vocabulary (AC-03, auditable history)
# ---------------------------------------------------------------------------

#: Actor of the retry-decision events appended by the dispatcher: the
#: Execution Monitor (the same actor as reconciliation).
RETRY_ACTOR: str = "execution-monitor"

#: Event type of a retry decision record: one event per decision
#: (authorized retry AND refused retry), carrying the failure class,
#: the decision and -- when authorized -- the resubmitted external
#: identity in the payload.
RETRY_DECISION_EVENT_TYPE: str = "engineering_retry_decision"

#: The decision vocabulary: the retry was authorized and performed.
RETRY_DECISION_AUTHORIZED: str = "retry_authorized"

#: The decision vocabulary: the retry was refused (scientific failure).
RETRY_DECISION_REFUSED: str = "retry_refused"

#: Stable reason of an authorized retry decision record.
RETRY_AUTHORIZED_REASON: str = "engineering_failure_retry_authorized"

#: Stable reason of a refused retry decision record.
RETRY_REFUSED_REASON: str = "scientific_failure_retry_refused"

#: Prefix of the deterministic idempotency key under which a decision
#: record is appended (``retry.decision:<run_id>:<failure class>``):
#: the event log resolves re-submissions of the same decision to the
#: single original record, so a decision is recorded exactly once
#: (AC-03) and the resubmission is performed at most once per recorded
#: decision (AC-01).
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
    cannot be read/parsed, or a recorded retry decision in the event
    log is malformed. Corrupt persisted state fails loudly, never
    silently."""


# ---------------------------------------------------------------------------
# The injected hooks
# ---------------------------------------------------------------------------

#: The injected failure classifier: a callable taking the external
#: identity (a ``RunExternal``) of a watched run and returning the
#: adapter-recorded failure class of its failed job -- the mirrored
#: vocabulary ``"transport"`` | ``"job"`` | ``None`` (any other string
#: is treated as an unrecognized class and refused). A classifier
#: raising an exception is a transient classification failure: it is
#: treated as unclassified (refused and recorded); the exception
#: message itself is never persisted.
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
        decision: ``RETRY_DECISION_AUTHORIZED`` or
            ``RETRY_DECISION_REFUSED``.
        decided_at: the injected clock stamp of the decision.
        resubmitted_external: the resubmitted external identity (the
            receipt of the resubmission hook) for an authorized
            decision; always None for a refused decision.
        replayed: True iff this pass resolved an already-recorded
            decision (the recorded history is authoritative; nothing
            was performed this pass and the hook was not invoked).
        event_id: the deterministic id of the decision record in the
            event log.
    """

    run_id: str
    failure_class: str | None
    decision: str
    decided_at: str
    resubmitted_external: RunExternal | None = None
    replayed: bool = False
    event_id: str = ""

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
        if self.decision not in (
            RETRY_DECISION_AUTHORIZED,
            RETRY_DECISION_REFUSED,
        ):
            raise RetryError(
                f"RetryOutcome.decision {self.decision!r} is not one of"
                f" {RETRY_DECISION_AUTHORIZED!r},"
                f" {RETRY_DECISION_REFUSED!r}"
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
        if (self.decision == RETRY_DECISION_AUTHORIZED) != (
            self.resubmitted_external is not None
        ):
            raise RetryError(
                "RetryOutcome invariant violation: an authorized decision"
                " always carries the resubmitted external identity, a"
                " refused decision never does"
            )


@dataclass(frozen=True)
class RetrySummary:
    """The outcome of deciding the full watch set.

    ``outcomes`` is the per-run outcomes in sorted run-id order
    (deterministic); ``authorized_count`` / ``refused_count`` count the
    decisions of this pass.
    """

    monitor_id: str
    decided_at: str
    outcomes: tuple[RetryOutcome, ...] = ()
    authorized_count: int = 0
    refused_count: int = 0

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
            1 for o in self.outcomes if o.decision == RETRY_DECISION_REFUSED
        ):
            raise RetryError(
                "RetrySummary.refused_count must equal the number of"
                " refused outcomes"
            )


# ---------------------------------------------------------------------------
# The retry dispatcher
# ---------------------------------------------------------------------------


class RetryDispatcher:
    """Deterministic engineering retry decisions for watched Runs.

    Decides each failed external Run of the watched-Run registry
    (DEV-M8-G01) against the engineering retry whitelist, using the
    durable Run store and the append-only event log, and records every
    decision as an auditable event (DEV-M8-G03):

    * AC-01: a failure class on :data:`ENGINEERING_RETRY_WHITELIST`
      triggers an IDENTICAL resubmission through the injected
      resubmission hook -- same run identity, same external identity
      semantics (same backend), no parameter change -- and one
      ``engineering_retry_decision`` event is appended under a
      deterministic idempotency key. Re-deciding the same failure
      resolves to the recorded decision and never re-invokes the hook
      (exactly-once per recorded decision).
    * AC-02: a scientific compute failure -- any failure class outside
      the whitelist (``FAILURE_CLASS_JOB``, ``None``, an unrecognized
      string) -- is observed and recorded as a refused decision: the
      hook is never invoked and no run parameter is ever mutated (the
      dispatcher never writes the run store). With no classifier
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
        run_store: the durable Run store the dispatcher reads Run
            records through (default: ``FilesystemStateBackend(
            state_dir)`` -- runs at ``<state_dir>/run/``).
        event_log: the append-only event log the dispatcher appends
            decision records to (default: ``ProjectEventLog(state_dir)``
            -- events at ``<state_dir>/event/``).

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

        A failure class on :data:`ENGINEERING_RETRY_WHITELIST`
        (``FAILURE_CLASS_TRANSPORT``) authorizes an identical
        resubmission through the injected resubmission hook (AC-01);
        every other failure class is a scientific compute failure and
        is refused: observed and recorded, never resubmitted, never
        mutating any run parameter (AC-02). The decision is recorded
        through the real event log under the deterministic idempotency
        key; re-deciding a recorded decision returns the recorded
        history (``replayed=True``) and never re-invokes the hook
        (AC-01 exactly-once / AC-03 replay).

        Args:
            run_id: the watched run to decide.
            failure_class: the adapter-recorded failure class of the
                failed external job -- the mirrored vocabulary
                ``"transport"`` | ``"job"`` | ``None`` (any other
                string is unrecognized and refused).

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
                corrupt, or the recorded decision in the event log is
                malformed.
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
        authorized = failure_class in ENGINEERING_RETRY_WHITELIST
        decision = (
            RETRY_DECISION_AUTHORIZED
            if authorized
            else RETRY_DECISION_REFUSED
        )
        event_id = generate_id(
            "event", RETRY_DECISION_EVENT_TYPE, run_id, normalized
        )

        # Exactly-once: the recorded decision is the durable "retry was
        # performed" fact. Resolve it BEFORE touching the resubmission
        # hook, so re-deciding (or restart-replaying) the same failure
        # returns the recorded history and never re-executes (AC-01/
        # AC-03).
        recorded = self._event_log.get(event_id)
        if recorded is not None:
            return self._replay_outcome(run_id, event_id, recorded.event)

        watch = self._registry.get(run_id)
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
        stamp = self._now_fn()

        resubmitted: RunExternal | None = None
        if authorized:
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

        self._event_log.append(
            self._decision_event(
                event_id, run_id, stamp, failure_class, decision, resubmitted
            ),
            idempotency_key=f"{RETRY_DECISION_KEY_PREFIX}:{run_id}:"
            f"{normalized}",
        )
        return RetryOutcome(
            run_id=run_id,
            failure_class=failure_class,
            decision=decision,
            decided_at=stamp,
            resubmitted_external=resubmitted,
            replayed=False,
            event_id=event_id,
        )

    def decide_all(self) -> RetrySummary:
        """Decide every watched run (sorted run-id order) and return
        the summary, classifying each run through the injected failure
        classifier. A contract violation or corrupt state anywhere in
        the watch set fails the whole pass loudly (deterministic sorted
        order, deterministic error)."""
        outcomes = tuple(
            self.decide(record.run_id, self._classify(record.external))
            for record in self._registry.list_watched()
        )
        return RetrySummary(
            monitor_id=self._monitor_id,
            decided_at=self._now_fn(),
            outcomes=outcomes,
            authorized_count=sum(
                1
                for o in outcomes
                if o.decision == RETRY_DECISION_AUTHORIZED
            ),
            refused_count=sum(
                1 for o in outcomes if o.decision == RETRY_DECISION_REFUSED
            ),
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

    def _replay_outcome(
        self, run_id: str, event_id: str, event: ProjectEvent
    ) -> RetryOutcome:
        """Rebuild the :class:`RetryOutcome` of an already-recorded
        decision from the event log record alone (AC-03): the recorded
        history is authoritative -- the original stamp and the recorded
        resubmission receipt -- and nothing is performed this pass.

        Raises:
            CorruptRetryStateError: the recorded decision is malformed
                (an unknown decision, a mistyped failure class or a
                malformed resubmitted identity).
        """
        payload = event.payload
        decision = payload.get("decision")
        if decision not in (
            RETRY_DECISION_AUTHORIZED,
            RETRY_DECISION_REFUSED,
        ):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries an unknown decision {decision!r}; expected"
                f" {RETRY_DECISION_AUTHORIZED!r} or"
                f" {RETRY_DECISION_REFUSED!r}"
            )
        failure_class = payload.get("failure_class")
        if failure_class is not None and not isinstance(failure_class, str):
            raise CorruptRetryStateError(
                f"recorded retry decision {event_id!r} for run {run_id!r}"
                f" carries a mistyped failure_class {failure_class!r}"
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
        )

    def _decision_event(
        self,
        event_id: str,
        run_id: str,
        stamp: str,
        failure_class: str | None,
        decision: str,
        resubmitted: RunExternal | None,
    ) -> ProjectEvent:
        """The deterministic decision record (AC-03, auditable
        history): event type ``engineering_retry_decision``, actor
        ``execution-monitor``, the stable reason per decision, and the
        payload carrying the failure class, the decision and -- when
        authorized -- the resubmitted external identity."""
        payload: dict[str, Any] = {
            "failure_class": failure_class,
            "decision": decision,
        }
        if resubmitted is not None:
            payload["resubmitted_external"] = resubmitted.to_dict()
        return ProjectEvent(
            event_id=event_id,
            timestamp=stamp,
            actor=RETRY_ACTOR,
            event_type=RETRY_DECISION_EVENT_TYPE,
            object_id=run_id,
            run_id=run_id,
            reason=(
                RETRY_AUTHORIZED_REASON
                if decision == RETRY_DECISION_AUTHORIZED
                else RETRY_REFUSED_REASON
            ),
            payload=payload,
        )
