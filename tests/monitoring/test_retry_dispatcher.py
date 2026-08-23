"""Tests for the deterministic engineering retry dispatcher
(DEV-M8-G03, deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac01_*`` -- AC-01: a failure kind the Goal's frozen automatic
  retry policy authorizes (``allowed_engineering_failures`` -- the
  bridged ``"transport"`` kind or a policy kind reported directly)
  triggers an IDENTICAL resubmission through the injected hook -- same
  run identity, same external identity semantics, no parameter change
  of any kind -- and records exactly one
  ``engineering_retry_decision`` event per attempt. The decision's
  aftermath (issue #150) is persisted: the Run record gains the
  retry-history entry and the resubmitted external identity (a history
  update, never a parameter mutation), and the watch entry names the
  resubmitted identity. Re-deciding the same failure generation never
  re-invokes the hook (exactly-once per recorded decision) and heals a
  missing aftermath; a policy that does not authorize the kind refuses
  it.
* ``test_ac02_*`` -- AC-02: a scientific compute failure -- the
  ``"job"`` class, an unclassified failure, any unrecognized failure
  class, a Goal with no retry policy -- never triggers a resubmission
  and never mutates run parameters; ``max_identical_retries`` caps the
  identical resubmissions of one failure; ``invalidate_run_on`` kinds
  decide an invalidation, ``supervisor_required_changes`` kinds a
  Supervisor-required change -- both recorded, never resubmitted. The
  default configuration (no classifier / no hook) can never authorize
  a retry.
* ``test_ac03_*`` -- AC-03: every decision (authorized AND refused) is
  auditable in the real event log under deterministic ids; a **fresh
  dispatcher** over the same state directory, run store and event log
  replays recorded decisions from the durable state alone without
  re-executing; identical inputs produce byte-identical durable state.
* ``test_retry_*`` -- the durable contracts: stable ``MonitoringError``
  subclasses (``RetryContractError`` for lifecycle/identity contract
  violations, ``CorruptRetryStateError`` for corrupt retry state),
  ``TypeError`` at the public type boundaries, the injected clock, the
  no-secrets discipline (walked over every persisted byte), and the
  no-adapters architectural boundary.

Determinism: every test injects a :class:`FakeClock` producing the
fixed ``FIXED_STAMP`` timestamp (no wall clock), ``tmp_path`` state
directories and ``generate_id`` ids. No randomness, no network, no
sleeps anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AutomaticRetryPolicy,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring import MonitoringError, WatchNotFoundError
from scientific_reproduction.monitoring.registry import WatchedRunRecord
from scientific_reproduction.monitoring.retry import (
    FAILURE_CLASS_JOB,
    FAILURE_CLASS_TO_FAILURE_KIND,
    FAILURE_CLASS_TRANSPORT,
    FAILURE_KIND_SSH_CONNECTION_LOST,
    RETRY_ACTOR,
    RETRY_AUTHORIZED_REASON,
    RETRY_DECISION_AUTHORIZED,
    RETRY_DECISION_EVENT_TYPE,
    RETRY_DECISION_INVALIDATED,
    RETRY_DECISION_REFUSED,
    RETRY_DECISION_SUPERVISOR_REQUIRED,
    RETRY_FAILURE_CLASS_UNCLASSIFIED,
    RETRY_INVALIDATED_REASON,
    RETRY_REFUSED_REASON,
    RETRY_SUPERVISOR_REASON,
    CorruptRetryStateError,
    RetryContractError,
    RetryDispatcher,
    RetryError,
    RetryFailure,
    RetryOutcome,
    RetrySkipped,
    RetrySummary,
    failure_class_to_failure_kind,
)
from scientific_reproduction.workers.retry import (
    CHECKPOINT_CONTINUATION_KIND,
    REASON_ALLOWED_ENGINEERING_FAILURE,
    REASON_CEILING_NOT_REACHED,
    REASON_CEILING_REACHED,
    REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
    REASON_INVALIDATE_RUN,
    REASON_NO_POLICY_ENTRY,
    REASON_SUPERVISOR_REQUIRED_CHANGE,
)

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: Credential-shaped strings that must never appear in persisted bytes.
FORBIDDEN_SECRETS = ("password", "passphrase", "secret", "credential",
                     "token", "api_key")


class FakeClock:
    """Injectable clock: a single fixed stamp repeats forever and every
    read is recorded (mirrors the compute-adapter tests' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


class ScriptedClassifier:
    """Scripted failure classifier: returns queued classes in order;
    an exhausted classifier reports unclassified (None); a queued
    exception is raised (a transient classification failure). Every
    call records the external identity it was given."""

    def __init__(self, *outcomes: str | None | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str | None:
        self.calls.append(external)
        if not self._outcomes:
            return None
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class MappingClassifier:
    """Failure classifier that reports a class per external job id
    (order-independent: the same external truth for both dispatchers of
    an AC-03 restart test, whatever the traversal order)."""

    def __init__(self, classes: dict[str, str | None]) -> None:
        self._classes = dict(classes)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str | None:
        self.calls.append(external)
        return self._classes.get(external.job_id)


class RecordingResubmit:
    """Fake resubmission hook: performs an IDENTICAL resubmission and
    returns the new external identity (a deterministic fresh job id);
    every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[RunExternal] = []
        self._next_index = 0

    def __call__(self, external: RunExternal) -> RunExternal:
        self.calls.append(external)
        self._next_index += 1
        return RunExternal(
            backend=external.backend,
            job_id=generate_id("job", f"resubmit-{self._next_index}"),
            working_directory=external.working_directory,
        )


def make_run_id(index: int = 1) -> str:
    """A deterministic run id (``sr_run_<32 hex>``)."""
    return generate_id("run", f"goal-{index}", f"seq-{index}")


def make_external(
    *,
    backend: str = "slurm_ssh",
    job_id: str | None = None,
    dispatch_id: str | None = None,
    working_directory: str | None = None,
) -> RunExternal:
    """An external identity; by default a slurm-ssh-shaped one with a
    job id and a working directory."""
    return RunExternal(
        backend=backend,
        job_id=job_id,
        dispatch_id=dispatch_id,
        working_directory=working_directory,
    )


def make_watch_record(
    index: int = 1,
    *,
    external: RunExternal | None = None,
    watched_at: str = FIXED_STAMP,
) -> WatchedRunRecord:
    """A deterministic watch entry for run ``index`` (the identity the
    dispatcher resubmits under)."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return WatchedRunRecord(
        run_id=run_id,
        external=external,
        watched_at=watched_at,
        adapter_id="adapter:compute/slurm_ssh",
        adapter_version="1.0",
    )


def make_run(
    index: int = 1,
    *,
    lifecycle_state: LifecycleState = LifecycleState.RUNNING_EXTERNAL,
    external: RunExternal | None = None,
) -> Run:
    """A deterministic durable Run record (``RUNNING_EXTERNAL`` by
    default, with the fixed created/updated stamps)."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return Run(
        run_id=run_id,
        goal_id=generate_id("goal", f"g{index}"),
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=lifecycle_state,
        goal_version="v1",
        external=external,
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def write_run(run_store: FilesystemStateBackend, run: Run) -> None:
    """Persist a run through the real schema-validating backend."""
    run_store.write("run", run.run_id, run.to_dict())


def make_policy_id(index: int = 1) -> str:
    """A deterministic retry-policy id (``sr_policy_<32 hex>``)."""
    return generate_id("policy", f"p{index}")


def make_policy(
    index: int = 1,
    *,
    allowed: tuple[str, ...] = (),
    supervisor_required: tuple[str, ...] = (),
    max_identical_retries: int | None = None,
    invalidate_run_on: tuple[str, ...] = (),
) -> AutomaticRetryPolicy:
    """A deterministic frozen automatic retry policy record."""
    return AutomaticRetryPolicy(
        policy_id=make_policy_id(index),
        allowed_engineering_failures=list(allowed),
        supervisor_required_changes=list(supervisor_required),
        max_identical_retries=max_identical_retries,
        invalidate_run_on=list(invalidate_run_on),
    )


def make_goal(run: Run, *, policy_ref: str | None = None) -> GoalContract:
    """A deterministic durable Goal record for ``run`` (matching the
    run's goal_id), optionally referencing a retry policy."""
    return GoalContract(
        goal_id=run.goal_id,
        title=f"reproduce {run.goal_id}",
        unit_process_type="gcmc",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="reproduce the reported result",
        requirement_ids=[generate_id("requirement", run.goal_id)],
        dependencies=[],
        acceptance=GoalAcceptance(
            criteria_ref=generate_id("acceptance", run.goal_id),
            frozen=True,
        ),
        analysis_protocol_ref=generate_id("analysis", run.goal_id),
        replication=GoalReplication(
            independent_required=True, planned_n_policy="fixed"
        ),
        version="1.0",
        frozen=True,
        automatic_retry_policy_ref=policy_ref,
        frozen_at=FIXED_STAMP,
        frozen_commit="abcdef0",
    )


def write_goal(
    dispatcher: RetryDispatcher,
    run: Run,
    *,
    policy_ref: str | None = None,
) -> None:
    """Persist the run's Goal record through the real backend."""
    dispatcher.run_store.write(
        "goal", run.goal_id, make_goal(run, policy_ref=policy_ref).to_dict()
    )


def write_policy(
    dispatcher: RetryDispatcher, policy: AutomaticRetryPolicy
) -> None:
    """Persist a retry-policy record through the real backend."""
    dispatcher.run_store.write(
        "retry-policy", policy.policy_id, policy.to_dict()
    )


def write_goal_and_policy(
    dispatcher: RetryDispatcher, run: Run, policy: AutomaticRetryPolicy
) -> None:
    """The standard policy fixture: a Goal referencing ``policy`` and
    the policy record itself (both through the real backend)."""
    write_goal(dispatcher, run, policy_ref=policy.policy_id)
    write_policy(dispatcher, policy)



def make_dispatcher(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    classifier: ScriptedClassifier | MappingClassifier | None = None,
    resubmit: RecordingResubmit | None = None,
    clock: FakeClock | None = None,
    monitor_id: str | None = None,
) -> RetryDispatcher:
    """A dispatcher over ``state_dir`` with an injected run store over
    ``runs_dir``, an event log over ``events_dir`` and the fixed
    clock."""
    return RetryDispatcher(
        state_dir,
        now=clock or FakeClock(),
        classifier=classifier,
        resubmit=resubmit,
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        monitor_id=monitor_id,
    )


def event_records(events_dir: Path) -> list[dict[str, object]]:
    """The raw persisted event records (sorted, read from disk)."""
    records: list[dict[str, object]] = []
    for path in sorted((events_dir / "events").glob("*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def tree_bytes(root: Path) -> list[tuple[str, bytes]]:
    """(relative path, bytes) of every file under ``root``, sorted."""
    if not root.is_dir():
        return []
    return sorted(
        (p.relative_to(root).as_posix(), p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    )


def decision_event_id(
    run_id: str, failure_class: str | None, attempt: int = 0
) -> str:
    """The deterministic event id of a decision (a pure function of the
    decision inputs: the failure class and the attempt index)."""
    normalized = (
        failure_class
        if failure_class is not None
        else RETRY_FAILURE_CLASS_UNCLASSIFIED
    )
    return generate_id(
        "event",
        RETRY_DECISION_EVENT_TYPE,
        run_id,
        normalized,
        f"attempt-{attempt}",
    )


def watch_all(
    dispatcher: RetryDispatcher, records: tuple[WatchedRunRecord, ...]
) -> None:
    """Watch every record through the dispatcher's registry (durable)."""
    for record in records:
        dispatcher.registry.watch(record)


# ---------------------------------------------------------------------------
# AC-01: whitelisted scheduler/node failure triggers identical resubmission
# ---------------------------------------------------------------------------


def test_ac01_whitelisted_transport_failure_triggers_identical_resubmission(
    tmp_path: Path,
) -> None:
    """AC-01: a failure kind the Goal's frozen automatic retry policy
    authorizes (the policy whitelists the kind the ``"transport"``
    class bridges to -- scheduler/node unreachable) triggers an
    identical resubmission through the injected hook: the hook
    receives the watch's external identity, the resubmission receipt
    is recorded, exactly one deterministic decision event is appended
    carrying the consulted policy and the evaluator's reasoning, and
    the decision's aftermath (issue #150) is persisted -- the run
    record gains the retry-history entry and the resubmitted identity
    (parameters and lifecycle untouched: a history update, never a
    parameter mutation), and the watch entry names the resubmitted
    identity."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)
    run_file_before = (runs_dir / "runs" / f"{run.run_id}.json").read_bytes()

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    # The decision: authorized, stamped from the clock, with the
    # resubmission receipt (the new external identity).
    assert outcome == RetryOutcome(
        run_id=run.run_id,
        failure_class=FAILURE_CLASS_TRANSPORT,
        decision=RETRY_DECISION_AUTHORIZED,
        decided_at=FIXED_STAMP,
        resubmitted_external=RunExternal(
            backend="slurm_ssh",
            job_id=generate_id("job", "resubmit-1"),
            working_directory="/home/alice/scratch/work-1",
        ),
        replayed=False,
        event_id=decision_event_id(run.run_id, FAILURE_CLASS_TRANSPORT),
        failure_kind=FAILURE_KIND_SSH_CONNECTION_LOST,
        policy_id=policy.policy_id,
        attempt=0,
        routing="AUTOMATIC",
        matched_rule_id="R-RET-A1",
        reasoning_ids=(REASON_ALLOWED_ENGINEERING_FAILURE,),
    )
    # The hook performed exactly one identical resubmission of the
    # watch's external identity (same backend, same working directory).
    assert resubmit.calls == [run.external]
    assert resubmit.calls[0].backend == run.external.backend

    # The bridge maps the transport class to exactly one policy kind
    # (the policy's whitelist entry that authorized this decision).
    assert FAILURE_CLASS_TO_FAILURE_KIND == {
        FAILURE_CLASS_TRANSPORT: FAILURE_KIND_SSH_CONNECTION_LOST
    }

    # Exactly one decision record with the documented vocabulary.
    log = dispatcher.event_log
    records = log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.event_id == decision_event_id(
        run.run_id, FAILURE_CLASS_TRANSPORT
    )
    assert event.event_type == RETRY_DECISION_EVENT_TYPE
    assert event.actor == RETRY_ACTOR
    assert event.run_id == run.run_id
    assert event.object_id == run.run_id
    assert event.reason == RETRY_AUTHORIZED_REASON
    assert event.timestamp == FIXED_STAMP
    assert event.payload == {
        "failure_class": FAILURE_CLASS_TRANSPORT,
        "failure_kind": FAILURE_KIND_SSH_CONNECTION_LOST,
        "policy_id": policy.policy_id,
        "attempt": 0,
        "decision": RETRY_DECISION_AUTHORIZED,
        "routing": "AUTOMATIC",
        "matched_rule_id": "R-RET-A1",
        "reasoning_ids": [REASON_ALLOWED_ENGINEERING_FAILURE],
        "external": {
            "backend": "slurm_ssh",
            "job_id": run.external.job_id,
            "working_directory": "/home/alice/scratch/work-1",
        },
        "resubmitted_external": {
            "backend": "slurm_ssh",
            "job_id": generate_id("job", "resubmit-1"),
            "working_directory": "/home/alice/scratch/work-1",
        },
    }
    assert records[0].sequence == 1

    # The identical resubmission aftermath (issue #150): the run record
    # carries the retry-history entry -- the decision payload plus the
    # decision's event id -- and the resubmitted identity, while the
    # parameters and the lifecycle stay untouched (a history update,
    # never a parameter mutation).
    assert (runs_dir / "runs" / f"{run.run_id}.json").read_bytes() != (
        run_file_before
    )
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert persisted.engineering_retries == [
        {**event.payload, "event_id": event.event_id}
    ]
    assert persisted.external == outcome.resubmitted_external
    assert persisted.updated_at == FIXED_STAMP
    assert persisted.created_at == run.created_at
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert persisted.goal_id == run.goal_id
    assert persisted.run_type is run.run_type
    assert persisted.goal_version == run.goal_version
    assert persisted.artifacts == run.artifacts
    assert persisted.deviations == run.deviations

    # The watch entry names the resubmitted identity: the shipped
    # reconciliation probes the resubmitted job, not the dead one.
    watch_after = dispatcher.registry.get(run.run_id)
    assert watch_after.external == outcome.resubmitted_external


def test_ac01_resubmission_is_exactly_once_per_retry_decision(
    tmp_path: Path,
) -> None:
    """AC-01: re-deciding the same failure generation never re-invokes
    the resubmission hook: the recorded decision resolves the
    deterministic event id / idempotency key to the single original
    record and the second pass replays it (``replayed=True``, original
    receipt and stamp) -- the resubmission happens exactly once per
    recorded decision and the log sequence never advances twice. The
    crash window (the event appended, the aftermath not yet persisted --
    the watch entry still names the dead job) is healed by the replay:
    the watch entry converges back to the resubmitted identity and the
    retry-history entry stays unique."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    first = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert first.decision == RETRY_DECISION_AUTHORIZED
    assert resubmit.calls == [run.external]
    records_after_first = dispatcher.event_log.list_events()

    # Rewind the watch entry to the dead job -- the crash window: the
    # decision is recorded but the aftermath never advanced the watch.
    dispatcher.registry.unwatch(run.run_id)
    dispatcher.registry.watch(make_watch_record(1, external=run.external))

    # The same generation again (e.g. a Monitor restart in the crash
    # window): no second hook call, no second record, no sequence
    # advance -- the recorded history replays.
    second = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert second.replayed is True
    assert second.decided_at == FIXED_STAMP
    assert second.resubmitted_external == first.resubmitted_external
    assert second.decision == RETRY_DECISION_AUTHORIZED
    assert resubmit.calls == [run.external]  # exactly one hook call total
    assert dispatcher.event_log.list_events() == records_after_first
    assert len(dispatcher.event_log.list_events()) == 1
    assert dispatcher.event_log.list_events()[0].sequence == 1

    # The replay healed the aftermath to convergence: the watch entry
    # names the resubmitted identity again and the run record carries
    # exactly one retry-history entry.
    assert dispatcher.registry.get(run.run_id).external == (
        first.resubmitted_external
    )
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert len(persisted.engineering_retries) == 1
    assert persisted.engineering_retries[0]["event_id"] == first.event_id
    assert persisted.external == first.resubmitted_external


def test_ac01_policy_whitelist_authorizes_a_policy_kind_failure(
    tmp_path: Path,
) -> None:
    """AC-01: a Goal whose policy whitelists
    ``scheduler_node_failure`` authorizes that failure class through
    the dispatcher -- a classifier may report a policy failure-kind
    string directly (the bridge passes it through verbatim) and the
    frozen evaluator's whitelist rule decides."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=("scheduler_node_failure",))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, "scheduler_node_failure")

    assert outcome.decision == RETRY_DECISION_AUTHORIZED
    assert outcome.failure_class == "scheduler_node_failure"
    assert outcome.failure_kind == "scheduler_node_failure"
    assert outcome.policy_id == policy.policy_id
    assert outcome.attempt == 0
    assert outcome.routing == "AUTOMATIC"
    assert outcome.matched_rule_id == "R-RET-A1"
    assert outcome.reasoning_ids == (REASON_ALLOWED_ENGINEERING_FAILURE,)
    assert outcome.resubmitted_external is not None
    assert resubmit.calls == [run.external]

    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.reason == RETRY_AUTHORIZED_REASON
    assert event.payload["failure_kind"] == "scheduler_node_failure"
    assert event.payload["policy_id"] == policy.policy_id
    assert event.payload["matched_rule_id"] == "R-RET-A1"
    assert event.payload["reasoning_ids"] == [
        REASON_ALLOWED_ENGINEERING_FAILURE
    ]


def test_ac01_transport_failure_not_whitelisted_is_refused(
    tmp_path: Path,
) -> None:
    """AC-01: a Goal whose policy does not whitelist the kind the
    ``"transport"`` class bridges to refuses the transport failure --
    the whitelist is the contract and the refusal is the default for
    every class the policy does not authorize."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=("scheduler_node_failure",))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.failure_kind == FAILURE_KIND_SSH_CONNECTION_LOST
    assert outcome.policy_id == policy.policy_id
    assert outcome.matched_rule_id == "R-RET-D1"
    assert outcome.reasoning_ids == (REASON_NO_POLICY_ENTRY,)
    assert outcome.resubmitted_external is None
    assert resubmit.calls == []

    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.reason == RETRY_REFUSED_REASON
    assert event.payload["failure_class"] == FAILURE_CLASS_TRANSPORT
    assert event.payload["failure_kind"] == FAILURE_KIND_SSH_CONNECTION_LOST
    assert event.payload["decision"] == RETRY_DECISION_REFUSED
    assert "resubmitted_external" not in event.payload


# ---------------------------------------------------------------------------
# AC-02: scientific compute failure never triggers parameter mutation
# ---------------------------------------------------------------------------


def test_ac02_scientific_compute_failure_never_resubmits_or_mutates(
    tmp_path: Path,
) -> None:
    """AC-02: a scientific compute failure (``FAILURE_CLASS_JOB`` --
    the job's own failure) never triggers a resubmission and never
    mutates anything: under a policy that whitelists only the transport
    kind, the frozen evaluator's default rule refuses the job class, the
    resubmission hook is never invoked, the run's parameters and
    persisted state bytes are untouched, and the refusal is observed and
    recorded (auditable) as one refused decision."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)
    run_file_before = (runs_dir / "runs" / f"{run.run_id}.json").read_bytes()

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_JOB)

    assert outcome == RetryOutcome(
        run_id=run.run_id,
        failure_class=FAILURE_CLASS_JOB,
        decision=RETRY_DECISION_REFUSED,
        decided_at=FIXED_STAMP,
        resubmitted_external=None,
        replayed=False,
        event_id=decision_event_id(run.run_id, FAILURE_CLASS_JOB),
        failure_kind="job",
        policy_id=policy.policy_id,
        attempt=0,
        routing="AUTOMATIC",
        matched_rule_id="R-RET-D1",
        reasoning_ids=(REASON_NO_POLICY_ENTRY,),
    )
    # No resubmission happened.
    assert resubmit.calls == []
    # The run's parameters are untouched and its state bytes identical.
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert persisted == run
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert persisted.goal_id == run.goal_id
    assert persisted.run_type is run.run_type
    assert persisted.external == run.external
    assert (runs_dir / "runs" / f"{run.run_id}.json").read_bytes() == (
        run_file_before
    )

    # The refusal is observed and recorded: exactly one refused decision
    # event, no receipt in the payload.
    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.event_type == RETRY_DECISION_EVENT_TYPE
    assert event.reason == RETRY_REFUSED_REASON
    assert event.payload == {
        "failure_class": FAILURE_CLASS_JOB,
        "failure_kind": "job",
        "policy_id": policy.policy_id,
        "attempt": 0,
        "decision": RETRY_DECISION_REFUSED,
        "routing": "AUTOMATIC",
        "matched_rule_id": "R-RET-D1",
        "reasoning_ids": [REASON_NO_POLICY_ENTRY],
        "external": {
            "backend": "slurm_ssh",
            "job_id": run.external.job_id,
            "working_directory": "/home/alice/scratch/work-1",
        },
    }
    assert "resubmitted_external" not in event.payload

    # Re-deciding the same scientific failure: replay, still no hook
    # call, still the single refusal record.
    again = dispatcher.decide(run.run_id, FAILURE_CLASS_JOB)
    assert again.replayed is True
    assert resubmit.calls == []
    assert dispatcher.event_log.list_events() == records


def test_ac02_unclassified_failure_is_refused_and_recorded(
    tmp_path: Path,
) -> None:
    """AC-02: an unclassified failure (no ``failure_class`` recorded,
    i.e. None) is refused: observed and recorded (payload failure_class
    null), never resubmitted."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    # A Goal with no automatic retry policy ref: the dispatcher still
    # consults the (absent) policy and refuses by the no-policy default.
    write_goal(dispatcher, run)

    outcome = dispatcher.decide(run.run_id, None)

    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.failure_class is None
    assert outcome.resubmitted_external is None
    assert resubmit.calls == []
    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    assert records[0].event.payload == {
        "failure_class": None,
        "failure_kind": RETRY_FAILURE_CLASS_UNCLASSIFIED,
        "policy_id": None,
        "attempt": 0,
        "decision": RETRY_DECISION_REFUSED,
        "routing": "AUTOMATIC",
        "matched_rule_id": None,
        "reasoning_ids": [REASON_NO_POLICY_ENTRY],
        "external": {
            "backend": "slurm_ssh",
            "job_id": run.external.job_id,
            "working_directory": "/home/alice/scratch/work-1",
        },
    }
    assert records[0].event.reason == RETRY_REFUSED_REASON
    assert Run.from_dict(
        dispatcher.run_store.read("run", run.run_id)
    ) == run


def test_ac02_unrecognized_failure_class_is_refused_safe_by_construction(
    tmp_path: Path,
) -> None:
    """AC-02: anything not on the whitelist is a scientific compute
    failure -- an unrecognized failure class string is refused (the
    safe-by-construction default), never resubmitted."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, "custom_backend_error")

    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.failure_kind == "custom_backend_error"
    assert outcome.matched_rule_id == "R-RET-D1"
    assert outcome.reasoning_ids == (REASON_NO_POLICY_ENTRY,)
    assert resubmit.calls == []
    assert dispatcher.event_log.list_events()[0].event.payload == {
        "failure_class": "custom_backend_error",
        "failure_kind": "custom_backend_error",
        "policy_id": policy.policy_id,
        "attempt": 0,
        "decision": RETRY_DECISION_REFUSED,
        "routing": "AUTOMATIC",
        "matched_rule_id": "R-RET-D1",
        "reasoning_ids": [REASON_NO_POLICY_ENTRY],
        "external": {
            "backend": "slurm_ssh",
            "job_id": run.external.job_id,
            "working_directory": "/home/alice/scratch/work-1",
        },
    }


def test_ac02_default_configuration_never_authorizes_a_retry(
    tmp_path: Path,
) -> None:
    """AC-02: with no classifier and no resubmission hook injected, the
    default configuration records refused decisions only -- the
    dispatcher can never authorize a retry. An authorized class with no
    hook fails loudly through the default hook (never a silent no-op)
    and records nothing."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    # An unclassified failure is refused and recorded by default.
    outcome = dispatcher.decide(run.run_id, None)
    assert outcome.decision == RETRY_DECISION_REFUSED
    assert len(dispatcher.event_log.list_events()) == 1

    # A policy-authorized class cannot be performed without a hook: the
    # default hook raises loudly and nothing is recorded.
    with pytest.raises(RetryError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert len(dispatcher.event_log.list_events()) == 1
    assert Run.from_dict(
        dispatcher.run_store.read("run", run.run_id)
    ) == run

    # decide_all with the default classifier (every watched run
    # unclassified): refused, even under a policy that would authorize
    # the transport class -- the classifier never reports it.
    summary = dispatcher.decide_all()
    assert summary.authorized_count == 0
    assert summary.refused_count == 1


def test_ac02_max_identical_retries_gates_identical_checkpoint_continuation(
    tmp_path: Path,
) -> None:
    """AC-02: ``max_identical_retries: 3`` permits up to three
    identical resubmissions of one failure (the ``checkpoint_continuation``
    kind -- identity-bridged, authorized without a whitelist entry) and
    refuses the fourth. Each resubmission receipt becomes the next
    failing generation (the aftermath itself advances the watch external
    identity and the Run record's external identity for the same run --
    issue #150), so every decision gets its own attempt-indexed event
    and idempotency key; a ceiling refusal never advances the attempt."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, max_identical_retries=3)
    write_goal_and_policy(dispatcher, run, policy)

    for attempt in range(3):
        outcome = dispatcher.decide(run.run_id, CHECKPOINT_CONTINUATION_KIND)
        assert outcome.decision == RETRY_DECISION_AUTHORIZED
        assert outcome.attempt == attempt
        assert outcome.matched_rule_id == "R-RET-C1"
        assert outcome.reasoning_ids == (
            REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
            REASON_CEILING_NOT_REACHED,
        )
        assert outcome.resubmitted_external is not None
        # The resubmission receipt becomes the next failing generation:
        # the aftermath advanced the watch entry and the Run record's
        # external identity to the fresh receipt -- no external advance
        # is needed.
        assert dispatcher.registry.get(run.run_id).external == (
            outcome.resubmitted_external
        )

    # Each authorized decision appended its retry-history entry (a
    # history update -- the run's parameters stay untouched).
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert len(persisted.engineering_retries) == 3
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert persisted.goal_id == run.goal_id

    # The fourth identical failure: the ceiling is reached, the frozen
    # evaluator refuses -- never a resubmission.
    fourth = dispatcher.decide(run.run_id, CHECKPOINT_CONTINUATION_KIND)
    assert fourth.decision == RETRY_DECISION_REFUSED
    assert fourth.attempt == 3
    assert fourth.matched_rule_id == "R-RET-C2"
    assert fourth.reasoning_ids == (
        REASON_IDENTICAL_CHECKPOINT_CONTINUATION,
        REASON_CEILING_REACHED,
    )
    assert fourth.resubmitted_external is None

    # Exactly three hook calls (one per authorized attempt), four
    # attempt-indexed decision records.
    assert len(resubmit.calls) == 3
    records = dispatcher.event_log.list_events()
    assert len(records) == 4
    for index, record in enumerate(records):
        assert record.event.event_id == decision_event_id(
            run.run_id, CHECKPOINT_CONTINUATION_KIND, attempt=index
        )
        assert record.event.payload["attempt"] == index

    # Re-deciding the refused generation replays the refusal (the
    # ceiling refusal never advances the attempt index).
    again = dispatcher.decide(run.run_id, CHECKPOINT_CONTINUATION_KIND)
    assert again.replayed is True
    assert again.decision == RETRY_DECISION_REFUSED
    assert len(dispatcher.event_log.list_events()) == 4
    assert len(resubmit.calls) == 3


def test_ac02_invalidate_run_on_decides_invalidation_never_resubmission(
    tmp_path: Path,
) -> None:
    """AC-02: a failure kind the policy marks with
    ``invalidate_run_on`` decides an invalidation -- recorded, never a
    resubmission -- and re-deciding the same generation replays the
    invalidation (still no resubmission)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, invalidate_run_on=("scheduler_node_failure",))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, "scheduler_node_failure")

    assert outcome.decision == RETRY_DECISION_INVALIDATED
    assert outcome.failure_kind == "scheduler_node_failure"
    assert outcome.matched_rule_id == "R-RET-I1"
    assert outcome.reasoning_ids == (REASON_INVALIDATE_RUN,)
    assert outcome.resubmitted_external is None
    assert resubmit.calls == []

    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.reason == RETRY_INVALIDATED_REASON
    assert event.payload["decision"] == RETRY_DECISION_INVALIDATED
    assert "resubmitted_external" not in event.payload

    # Re-deciding the same generation replays the invalidation.
    again = dispatcher.decide(run.run_id, "scheduler_node_failure")
    assert again.replayed is True
    assert again.decision == RETRY_DECISION_INVALIDATED
    assert resubmit.calls == []
    assert len(dispatcher.event_log.list_events()) == 1


def test_ac02_supervisor_required_changes_decide_a_supervisor_required_change(
    tmp_path: Path,
) -> None:
    """AC-02: a failure kind the policy lists under
    ``supervisor_required_changes`` decides a Supervisor-required change
    (routed to the Supervisor, never an automatic resubmission) and the
    decision is recorded with the Supervisor routing."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, supervisor_required=("parameter_choice_error",))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, "parameter_choice_error")

    assert outcome.decision == RETRY_DECISION_SUPERVISOR_REQUIRED
    assert outcome.failure_kind == "parameter_choice_error"
    assert outcome.routing == "SUPERVISOR"
    assert outcome.matched_rule_id == "R-RET-S1"
    assert outcome.reasoning_ids == (REASON_SUPERVISOR_REQUIRED_CHANGE,)
    assert outcome.resubmitted_external is None
    assert resubmit.calls == []

    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.reason == RETRY_SUPERVISOR_REASON
    assert event.payload["decision"] == RETRY_DECISION_SUPERVISOR_REQUIRED
    assert event.payload["routing"] == "SUPERVISOR"
    assert "resubmitted_external" not in event.payload


def test_ac02_goal_without_retry_policy_ref_never_authorizes(
    tmp_path: Path,
) -> None:
    """AC-02: a Goal with no ``automatic_retry_policy_ref`` resolves no
    policy and never authorizes a retry: the no-policy default refuses
    (with the ``no_policy_entry`` reasoning), even for a kind another
    Goal's policy would authorize."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=resubmit
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    write_goal(dispatcher, run)

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.failure_kind == FAILURE_KIND_SSH_CONNECTION_LOST
    assert outcome.policy_id is None
    assert outcome.routing == "AUTOMATIC"
    assert outcome.matched_rule_id is None
    assert outcome.reasoning_ids == (REASON_NO_POLICY_ENTRY,)
    assert outcome.resubmitted_external is None
    assert resubmit.calls == []
    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    assert records[0].event.payload["policy_id"] is None
    assert records[0].event.payload["matched_rule_id"] is None


# ---------------------------------------------------------------------------
# AC-03: retry history remains auditable
# ---------------------------------------------------------------------------


def test_ac03_fresh_dispatcher_replays_recorded_decisions_without_reexecution(
    tmp_path: Path,
) -> None:
    """AC-03: a fresh dispatcher over the same state directory, run
    store and event log reconstructs the retry history from the
    recorded events alone: re-deciding the same failures returns the
    recorded outcomes (original stamp and receipt, ``replayed=True``)
    and never re-invokes the hook -- no duplicate records, no second
    resubmission, identical durable bytes. A watch entry rewound to the
    dead external identity (the crash window between the event append
    and the aftermath, issue #150) is healed back to the resubmitted
    identity by the replay -- without touching the already-converged run
    record."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    transport_run = make_run(1)
    job_run = make_run(2)
    runs = (transport_run, job_run)
    first_resubmit = RecordingResubmit()
    first = make_dispatcher(state, runs_dir, events_dir, resubmit=first_resubmit)
    watch_all(
        first,
        tuple(make_watch_record(i, external=run.external) for i, run in enumerate(runs, start=1)),
    )
    for run in runs:
        write_run(first.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(first, transport_run, policy)
    write_goal(first, job_run)

    first.decide(transport_run.run_id, FAILURE_CLASS_TRANSPORT)
    first.decide(job_run.run_id, FAILURE_CLASS_JOB)
    assert first_resubmit.calls == [transport_run.external]
    events_after_first = tree_bytes(events_dir)
    runs_after_first = tree_bytes(runs_dir)
    state_after_first = tree_bytes(state)

    # Rewind the transport run's watch entry to the dead external
    # identity: the crash window between the event append and the
    # aftermath leaves a legacy watch that still names the dead job.
    first.registry.unwatch(transport_run.run_id)
    first.registry.watch(
        make_watch_record(1, external=transport_run.external)
    )

    # A FRESH dispatcher over the same durable state -- no session state
    # -- with a fresh hook instance.
    fresh_resubmit = RecordingResubmit()
    fresh = make_dispatcher(
        state, runs_dir, events_dir, resubmit=fresh_resubmit
    )

    transport_replay = fresh.decide(transport_run.run_id, FAILURE_CLASS_TRANSPORT)
    job_replay = fresh.decide(job_run.run_id, FAILURE_CLASS_JOB)

    # The recorded history replays: original decision facts, and the
    # fresh hook was never invoked.
    assert transport_replay.replayed is True
    assert transport_replay.decision == RETRY_DECISION_AUTHORIZED
    assert transport_replay.decided_at == FIXED_STAMP
    assert transport_replay.resubmitted_external is not None
    assert job_replay.replayed is True
    assert job_replay.decision == RETRY_DECISION_REFUSED
    assert fresh_resubmit.calls == []
    # Identical durable bytes: no duplicate records, no re-execution.
    # The replay healed the rewound watch entry back to the resubmitted
    # identity and left the converged run record untouched.
    assert tree_bytes(events_dir) == events_after_first
    assert len(fresh.event_log.list_events()) == 2
    assert tree_bytes(runs_dir) == runs_after_first
    assert tree_bytes(state) == state_after_first

    # The transport run record carries the aftermath of the replayed
    # authorized decision: exactly one retry-history entry keyed by the
    # recorded event id and the resubmitted external identity.
    transport_persisted = Run.from_dict(
        fresh.run_store.read("run", transport_run.run_id)
    )
    assert len(transport_persisted.engineering_retries) == 1
    assert (
        transport_persisted.engineering_retries[0]["event_id"]
        == transport_replay.event_id
    )
    assert (
        transport_persisted.external
        == transport_replay.resubmitted_external
    )
    assert (
        transport_persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    )
    # A refused decision never touches its run record.
    job_persisted = Run.from_dict(fresh.run_store.read("run", job_run.run_id))
    assert job_persisted == job_run


def test_ac03_byte_identical_durable_state_for_identical_inputs(
    tmp_path: Path,
) -> None:
    """AC-03: identical injected inputs produce byte-identical durable
    state -- watch entries, run records and decision events (canonical
    sorted JSON, fixed clock, deterministic ids) -- no randomness, no
    wall clock."""
    payloads: list[dict[str, list[tuple[str, bytes]]]] = []
    # The same injected monitor identity for both variants: the durable
    # bytes are then byte-comparable.
    monitor_id = generate_id("monitor", "identical")
    for variant in ("a", "b"):
        state, runs_dir, events_dir = (
            tmp_path / variant / "state",
            tmp_path / variant / "runs",
            tmp_path / variant / "events",
        )
        run = make_run(1)
        dispatcher = make_dispatcher(
            state,
            runs_dir,
            events_dir,
            resubmit=RecordingResubmit(),
            monitor_id=monitor_id,
        )
        watch_all(dispatcher, (make_watch_record(1, external=run.external),))
        write_run(dispatcher.run_store, run)
        policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
        write_goal_and_policy(dispatcher, run, policy)
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
        dispatcher.decide(run.run_id, FAILURE_CLASS_JOB)  # replay-safe
        payloads.append(
            {
                "runs": tree_bytes(runs_dir),
                "events": tree_bytes(events_dir),
                "state": tree_bytes(state),
            }
        )
    assert payloads[0] == payloads[1]


def test_ac03_every_decision_is_auditable_in_the_event_log(
    tmp_path: Path,
) -> None:
    """AC-03: every retry decision -- authorized AND refused -- is
    appended through the real event log under a deterministic id
    carrying the failure class, the decision and the stamp; the log
    order is deterministic; the event bytes are canonical."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    runs = (make_run(1), make_run(2), make_run(3))
    classes = {
        runs[0].external.job_id: FAILURE_CLASS_TRANSPORT,
        runs[1].external.job_id: FAILURE_CLASS_JOB,
        runs[2].external.job_id: None,
    }
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        classifier=MappingClassifier(classes),
        resubmit=RecordingResubmit(),
    )
    watch_all(
        dispatcher,
        tuple(make_watch_record(i, external=run.external) for i, run in enumerate(runs, start=1)),
    )
    for run in runs:
        write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    for run in runs:
        write_goal_and_policy(dispatcher, run, policy)

    summary = dispatcher.decide_all()

    expected_by_run = {
        run.run_id: RetryOutcome(
            run_id=run.run_id,
            failure_class=classes[run.external.job_id],
            decision=(
                RETRY_DECISION_AUTHORIZED
                if classes[run.external.job_id] == FAILURE_CLASS_TRANSPORT
                else RETRY_DECISION_REFUSED
            ),
            decided_at=FIXED_STAMP,
            resubmitted_external=(
                None
                if classes[run.external.job_id] != FAILURE_CLASS_TRANSPORT
                else RunExternal(
                    backend="slurm_ssh",
                    job_id=generate_id("job", "resubmit-1"),
                    working_directory=run.external.working_directory,
                )
            ),
            replayed=False,
            event_id=decision_event_id(
                run.run_id, classes[run.external.job_id]
            ),
            failure_kind=failure_class_to_failure_kind(
                classes[run.external.job_id]
            ),
            policy_id=policy.policy_id,
            attempt=0,
            routing="AUTOMATIC",
            matched_rule_id=(
                "R-RET-A1"
                if classes[run.external.job_id] == FAILURE_CLASS_TRANSPORT
                else "R-RET-D1"
            ),
            reasoning_ids=(
                (REASON_ALLOWED_ENGINEERING_FAILURE,)
                if classes[run.external.job_id] == FAILURE_CLASS_TRANSPORT
                else (REASON_NO_POLICY_ENTRY,)
            ),
        )
        for run in runs
    }
    assert summary == RetrySummary(
        monitor_id=dispatcher.monitor_id,
        decided_at=FIXED_STAMP,
        outcomes=tuple(
            expected_by_run[run_id]
            for run_id in sorted(expected_by_run)
        ),
        authorized_count=1,
        refused_count=2,
    )
    assert summary.authorized_count == 1
    assert summary.refused_count == 2

    # Three decision records, deterministic sorted order (by sequence,
    # tie-broken by event id), each carrying the full audit vocabulary.
    records = dispatcher.event_log.list_events()
    assert len(records) == 3
    assert [records[0].sequence, records[1].sequence, records[2].sequence] == [
        1, 2, 3,
    ]
    by_run = {event.event.run_id: event.event for event in records}
    for run in runs:
        event = by_run[run.run_id]
        assert event.event_id == decision_event_id(
            run.run_id, classes[run.external.job_id]
        )
        assert event.event_type == RETRY_DECISION_EVENT_TYPE
        assert event.actor == RETRY_ACTOR
        assert event.timestamp == FIXED_STAMP
        assert event.payload["failure_class"] == classes[run.external.job_id]
        assert event.payload["decision"] in (
            RETRY_DECISION_AUTHORIZED,
            RETRY_DECISION_REFUSED,
        )
        assert event.payload["failure_kind"] == failure_class_to_failure_kind(
            classes[run.external.job_id]
        )
        assert event.payload["policy_id"] == policy.policy_id
        assert event.payload["attempt"] == 0
    assert by_run[runs[0].run_id].payload["matched_rule_id"] == "R-RET-A1"
    assert by_run[runs[1].run_id].payload["matched_rule_id"] == "R-RET-D1"
    assert by_run[runs[2].run_id].payload["matched_rule_id"] == "R-RET-D1"
    assert by_run[runs[0].run_id].reason == RETRY_AUTHORIZED_REASON
    assert by_run[runs[1].run_id].reason == RETRY_REFUSED_REASON
    assert by_run[runs[2].run_id].reason == RETRY_REFUSED_REASON
    assert "resubmitted_external" in by_run[runs[0].run_id].payload
    assert "resubmitted_external" not in by_run[runs[1].run_id].payload
    assert "resubmitted_external" not in by_run[runs[2].run_id].payload

    # The persisted event bytes are canonical sorted JSON (byte-stable
    # for identical inputs).
    raw = event_records(events_dir)
    assert len(raw) == 3
    for path in sorted((events_dir / "events").glob("*.json")):
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert path.read_text(encoding="utf-8") == json.dumps(
            parsed, indent=2, sort_keys=True, ensure_ascii=False
        )


def test_ac03_empty_watch_set_yields_empty_summary(tmp_path: Path) -> None:
    """AC-03: deciding an empty watch set (fresh or empty state
    directory) is a deterministic empty pass with no events."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    summary = dispatcher.decide_all()
    assert summary == RetrySummary(
        monitor_id=dispatcher.monitor_id,
        decided_at=FIXED_STAMP,
        outcomes=(),
        authorized_count=0,
        refused_count=0,
    )
    assert dispatcher.event_log.list_events() == []


# ---------------------------------------------------------------------------
# The retry contracts
# ---------------------------------------------------------------------------


def test_retry_unwatched_run_raises_watch_not_found(tmp_path: Path) -> None:
    """Deciding a run that is not watched raises the stable
    WatchNotFoundError (no record is appended)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    with pytest.raises(WatchNotFoundError):
        dispatcher.decide(make_run_id(1), FAILURE_CLASS_TRANSPORT)
    assert dispatcher.event_log.list_events() == []


def test_retry_missing_run_record_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A watch entry referencing a run with no run record in the run
    store is corrupt retry state: it fails loudly with the stable
    CorruptRetryStateError, never a silent skip."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    # No write_run: the run record is missing.
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)


def test_retry_corrupt_run_record_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A corrupt run record on disk fails the decision loudly with the
    stable CorruptRetryStateError."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    path = runs_dir / "runs" / f"{run.run_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)


def test_retry_decision_for_pre_external_run_raises_contract_error(
    tmp_path: Path,
) -> None:
    """A retry decision for a run whose lifecycle is still pre-external
    (never handed off -- no external failure exists) is a contract
    violation: stable RetryContractError, run unchanged, no event."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.DISPATCHED)
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=RecordingResubmit()
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)

    with pytest.raises(RetryContractError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert Run.from_dict(
        dispatcher.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.DISPATCHED
    assert dispatcher.event_log.list_events() == []


def test_retry_decision_for_result_recorded_run_raises_contract_error(
    tmp_path: Path,
) -> None:
    """A retry decision for a run that already recorded its result is a
    contract violation: no failure can be retried onto a finished run
    (stable RetryContractError, run unchanged, no event)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=RecordingResubmit()
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)

    with pytest.raises(RetryContractError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert Run.from_dict(
        dispatcher.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.RESULT_AVAILABLE
    assert dispatcher.event_log.list_events() == []


def test_retry_decision_for_cancelled_run_raises_contract_error(
    tmp_path: Path,
) -> None:
    """A retry decision for a cancelled run (terminal, no results) is a
    contract violation: stable RetryContractError, run unchanged, no
    event."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.CANCELLED)
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=RecordingResubmit()
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)

    with pytest.raises(RetryContractError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert Run.from_dict(
        dispatcher.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.CANCELLED
    assert dispatcher.event_log.list_events() == []


def test_retry_external_identity_mismatch_raises_contract_error(
    tmp_path: Path,
) -> None:
    """A Run record whose external identity disagrees with its watch
    entry is a contract violation (the dispatcher would resubmit under
    a mismatched identity): stable RetryContractError before any
    resubmission or event."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)  # watch entry matches this identity
    mismatched = make_run(1, external=make_external(
        backend="slurm_ssh",
        job_id=generate_id("job", "some-other-run"),
        working_directory="/home/alice/scratch/other",
    ))
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(state, runs_dir, events_dir, resubmit=resubmit)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, mismatched)

    with pytest.raises(RetryContractError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert resubmit.calls == []
    assert dispatcher.event_log.list_events() == []


def test_retry_corrupt_recorded_decision_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A recorded decision in the event log whose payload is malformed
    (an unknown decision) fails the replay loudly with the stable
    CorruptRetryStateError -- corrupt persisted state never replays
    silently."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    # A hand-crafted decision record with a bogus decision payload.
    event_id = decision_event_id(run.run_id, FAILURE_CLASS_TRANSPORT)
    bad_record = {
        "event_id": event_id,
        "timestamp": FIXED_STAMP,
        "actor": RETRY_ACTOR,
        "event_type": RETRY_DECISION_EVENT_TYPE,
        "object_id": run.run_id,
        "run_id": run.run_id,
        "reason": RETRY_AUTHORIZED_REASON,
        "payload": {
            "failure_class": FAILURE_CLASS_TRANSPORT,
            "decision": "bogus_decision",
        },
        "sequence": 1,
    }
    path = events_dir / "events" / f"{event_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bad_record, sort_keys=True), encoding="utf-8")

    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)


def test_retry_missing_goal_record_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A run record whose Goal has no goal record in the state store is
    corrupt retry state (the dispatcher cannot consult the Goal's
    policy): stable CorruptRetryStateError, never a silent no-policy
    refusal."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    # No write_goal: the Goal record is missing.
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher.event_log.list_events() == []


def test_retry_corrupt_goal_record_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A corrupt Goal record on disk fails the decision loudly with the
    stable CorruptRetryStateError."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    path = runs_dir / "goals" / f"{run.goal_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)


def test_retry_missing_policy_record_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A Goal referencing a retry policy with no policy record in the
    state store is corrupt retry state: stable CorruptRetryStateError,
    never a silent refusal."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal(dispatcher, run, policy_ref=policy.policy_id)
    # No write_policy: the policy record is missing.
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher.event_log.list_events() == []


def test_retry_policy_id_mismatch_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A Goal whose ``automatic_retry_policy_ref`` names a different
    policy than the record's own ``policy_id`` is corrupt retry state:
    stable CorruptRetryStateError, never a silent decision."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    # The record's own policy_id disagrees with the Goal's ref.
    write_goal(dispatcher, run, policy_ref=make_policy_id(2))
    write_policy(dispatcher, policy)
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher.event_log.list_events() == []


def test_retry_malformed_policy_record_raises_corrupt_state_error(
    tmp_path: Path,
) -> None:
    """A policy record violating its contract (``max_identical_retries``
    below its schema minimum) fails the decision loudly with the stable
    CorruptRetryStateError -- the frozen evaluator's RetryPolicyError
    surfaces as corrupt retry state."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(state, runs_dir, events_dir)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal(dispatcher, run, policy_ref=policy.policy_id)
    # Plant the policy record directly: the backend's schema validation
    # would reject max_identical_retries: -1 (as it should).
    policy_dict = policy.to_dict()
    policy_dict["max_identical_retries"] = -1
    path = runs_dir / "retry-policies" / f"{policy.policy_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(policy_dict, sort_keys=True), encoding="utf-8")
    with pytest.raises(CorruptRetryStateError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher.event_log.list_events() == []


def test_retry_resubmit_hook_receives_the_watch_external_identity(
    tmp_path: Path,
) -> None:
    """The resubmission hook is invoked with the watch entry's external
    identity -- the durable identity the monitor resubmits under."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(state, runs_dir, events_dir, resubmit=resubmit)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)
    dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert resubmit.calls == [run.external]


def test_retry_resubmit_hook_must_return_an_identical_external_identity(
    tmp_path: Path,
) -> None:
    """A resubmission hook returning a non-RunExternal value, an
    identity on a different backend, or an identity without any
    external id violates the identical-resubmission contract: TypeError
    / stable RetryContractError, nothing recorded."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    watch = make_watch_record(1, external=run.external)

    def bad_return(_external: RunExternal) -> str:
        return "not an identity"

    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=bad_return
    )
    watch_all(dispatcher, (watch,))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)
    with pytest.raises(TypeError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher.event_log.list_events() == []

    # A different backend is not an identical resubmission.
    other_state, other_runs, other_events = (
        tmp_path / "other-state", tmp_path / "other-runs", tmp_path / "other-events"
    )

    def other_backend(_external: RunExternal) -> RunExternal:
        return RunExternal(
            backend="local",
            job_id=generate_id("job", "resubmit-other"),
            working_directory=run.external.working_directory,
        )

    dispatcher2 = make_dispatcher(
        other_state,
        other_runs,
        other_events,
        resubmit=other_backend,
    )
    watch_all(dispatcher2, (watch,))
    write_run(dispatcher2.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher2, run, policy)
    with pytest.raises(RetryContractError):
        dispatcher2.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher2.event_log.list_events() == []

    # A resubmission without any addressable external id is refused.
    final_state, final_runs, final_events = (
        tmp_path / "final-state", tmp_path / "final-runs", tmp_path / "final-events"
    )

    def unaddressable(_external: RunExternal) -> RunExternal:
        return RunExternal(backend="slurm_ssh")

    dispatcher3 = make_dispatcher(
        final_state, final_runs, final_events, resubmit=unaddressable
    )
    watch_all(dispatcher3, (watch,))
    write_run(dispatcher3.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher3, run, policy)
    with pytest.raises(RetryContractError):
        dispatcher3.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert dispatcher3.event_log.list_events() == []


def test_retry_classifier_transient_failure_is_recorded_refused(
    tmp_path: Path,
) -> None:
    """A transient classifier failure (an exception from the injected
    classifier) is treated as unclassified: the run is refused and the
    refusal is recorded; the exception message is never persisted (no
    secrets in durable state)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    transient = RuntimeError("slurm ssh api_key expired while classifying")
    classifier = ScriptedClassifier(transient)
    dispatcher = make_dispatcher(state, runs_dir, events_dir, classifier=classifier)
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    write_goal(dispatcher, run)

    summary = dispatcher.decide_all()

    assert summary.refused_count == 1
    assert summary.authorized_count == 0
    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    assert records[0].event.payload["decision"] == RETRY_DECISION_REFUSED
    assert records[0].event.payload["failure_class"] is None
    persisted = b"".join(
        p.read_bytes()
        for root in (state, runs_dir, events_dir)
        for p in root.rglob("*")
        if p.is_file()
    )
    lowered = persisted.decode("utf-8", errors="replace").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered
    assert "expired while classifying" not in lowered


def test_retry_decide_all_classifies_each_watched_run(tmp_path: Path) -> None:
    """decide_all classifies every watched run through the injected
    classifier (in sorted run-id order) and decides each: transport
    classes are authorized, job classes refused."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    transport_run = make_run(1)
    job_run = make_run(2)
    classes = {
        transport_run.external.job_id: FAILURE_CLASS_TRANSPORT,
        job_run.external.job_id: FAILURE_CLASS_JOB,
    }
    classifier = MappingClassifier(classes)
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        classifier=classifier,
        resubmit=RecordingResubmit(),
    )
    watch_all(
        dispatcher,
        (
            make_watch_record(1, external=transport_run.external),
            make_watch_record(2, external=job_run.external),
        ),
    )
    for run in (transport_run, job_run):
        write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, transport_run, policy)
    write_goal(dispatcher, job_run)

    summary = dispatcher.decide_all()

    assert summary.authorized_count == 1
    assert summary.refused_count == 1
    assert {o.run_id: o.decision for o in summary.outcomes} == {
        transport_run.run_id: RETRY_DECISION_AUTHORIZED,
        job_run.run_id: RETRY_DECISION_REFUSED,
    }
    assert [o.run_id for o in summary.outcomes] == sorted(
        o.run_id for o in summary.outcomes
    )
    # Every watched run was classified through the injected classifier.
    assert set(classifier.calls) == {transport_run.external, job_run.external}


# ---------------------------------------------------------------------------
# Pass-level per-run isolation (issue #152)
# ---------------------------------------------------------------------------


def test_decide_all_skips_ineligible_runs_and_decides_the_eligible(
    tmp_path: Path,
) -> None:
    """Issue #152: a mixed watch set -- a ``RESULT_AVAILABLE`` run that
    completed normally plus a ``RUNNING_EXTERNAL`` run with an
    engineering failure -- does not abort the pass: the ineligible run
    is recorded as skipped, the eligible run is decided (and
    resubmitted), and the classifier is only invoked for the eligible
    run."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    completed_run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    failed_run = make_run(2)
    classifier = MappingClassifier(
        {failed_run.external.job_id: FAILURE_CLASS_TRANSPORT}
    )
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        classifier=classifier,
        resubmit=resubmit,
    )
    watch_all(
        dispatcher,
        (
            make_watch_record(1, external=completed_run.external),
            make_watch_record(2, external=failed_run.external),
        ),
    )
    for run in (completed_run, failed_run):
        write_run(dispatcher.run_store, run)
    # The eligible run's goal references a frozen policy whitelisting
    # the transport failure kind (issue #149: authorization consults
    # the policy; the ineligible run is skipped before any goal read).
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, failed_run, policy)

    summary = dispatcher.decide_all()

    # The pass did not abort: exactly the eligible run was decided.
    assert summary.authorized_count == 1
    assert summary.refused_count == 0
    assert [o.run_id for o in summary.outcomes] == [failed_run.run_id]
    assert summary.outcomes[0].decision == RETRY_DECISION_AUTHORIZED
    # The ineligible run is a skipped outcome with the stable reason.
    assert summary.skipped == (
        RetrySkipped(
            run_id=completed_run.run_id,
            lifecycle_state=LifecycleState.RESULT_AVAILABLE.value,
            skipped_at=FIXED_STAMP,
        ),
    )
    assert summary.failures == ()
    # Only the eligible run was classified and resubmitted.
    assert classifier.calls == [failed_run.external]
    assert resubmit.calls == [failed_run.external]
    assert len(dispatcher.event_log.list_events()) == 1
    assert Run.from_dict(
        dispatcher.run_store.read("run", completed_run.run_id)
    ) == completed_run


def test_decide_all_records_per_run_failures_and_continues(
    tmp_path: Path,
) -> None:
    """Issue #152: per-run errors during the pass -- a watched run
    whose Run record is missing (``CorruptRetryStateError``) and a run
    whose external identity disagrees with its watch entry
    (``RetryContractError``) -- are recorded as per-run failed outcomes
    with the stable error, and the pass still decides the healthy runs."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    missing_run = make_run(1)
    mismatch_run = make_run(2)
    healthy_run = make_run(3)
    classifier = MappingClassifier(
        {healthy_run.external.job_id: FAILURE_CLASS_TRANSPORT}
    )
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        classifier=classifier,
        resubmit=resubmit,
    )
    mismatch_external = make_external(
        job_id=generate_id("job", "mismatch"),
        working_directory=mismatch_run.external.working_directory,
    )
    watch_all(
        dispatcher,
        (
            make_watch_record(1, external=missing_run.external),
            make_watch_record(2, external=mismatch_external),
            make_watch_record(3, external=healthy_run.external),
        ),
    )
    write_run(dispatcher.run_store, mismatch_run)
    write_run(dispatcher.run_store, healthy_run)
    # The healthy run's goal references a frozen policy whitelisting
    # the transport failure kind (issue #149); the mismatched run
    # fails the identity contract before any goal read.
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, healthy_run, policy)

    summary = dispatcher.decide_all()

    assert summary.authorized_count == 1
    assert summary.refused_count == 0
    assert summary.skipped == ()
    assert [o.run_id for o in summary.outcomes] == [healthy_run.run_id]
    assert all(
        isinstance(failure, RetryFailure) for failure in summary.failures
    )
    failures_by_run = {failure.run_id: failure for failure in summary.failures}
    assert set(failures_by_run) == {missing_run.run_id, mismatch_run.run_id}
    assert failures_by_run[missing_run.run_id].error == "CorruptRetryStateError"
    assert failures_by_run[missing_run.run_id].message.startswith(
        f"corrupt retry state for run {missing_run.run_id!r}"
    )
    assert failures_by_run[mismatch_run.run_id].error == "RetryContractError"
    assert "external identity disagrees" in failures_by_run[
        mismatch_run.run_id
    ].message
    # Only the healthy run was decided, resubmitted and recorded; the
    # mismatched run is classified (it is lifecycle-eligible) but its
    # decision fails the identity contract before the hook is reached.
    assert set(classifier.calls) == {mismatch_external, healthy_run.external}
    assert resubmit.calls == [healthy_run.external]
    assert len(dispatcher.event_log.list_events()) == 1


def test_decide_all_loud_default_hook_still_fails_the_pass(
    tmp_path: Path,
) -> None:
    """The per-run isolation covers per-run durable state errors only:
    an authorized decision with no resubmission hook injected raises
    the loud default ``RetryError`` -- a Monitor configuration problem
    -- and still fails the whole pass loudly."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    classifier = MappingClassifier(
        {run.external.job_id: FAILURE_CLASS_TRANSPORT}
    )
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, classifier=classifier
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    # The run's goal references a frozen policy whitelisting the
    # transport failure kind (issue #149), so the decision is
    # authorized and the loud default hook raises.
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    with pytest.raises(RetryError):
        dispatcher.decide_all()
    assert dispatcher.event_log.list_events() == []


# ---------------------------------------------------------------------------
# Determinism, secrets, default configuration, boundaries
# ---------------------------------------------------------------------------


def test_retry_uses_injected_clock(tmp_path: Path) -> None:
    """Every stamped value (decision, event record) comes from the
    injected clock -- no wall clock anywhere."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    clock = FakeClock(FIXED_STAMP)
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        resubmit=RecordingResubmit(),
        clock=clock,
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)
    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert outcome.decided_at == FIXED_STAMP
    assert dispatcher.event_log.list_events()[0].event.timestamp == FIXED_STAMP
    assert clock.calls, "the dispatcher must consult the injected clock"


def test_retry_persisted_state_never_carries_credentials(
    tmp_path: Path,
) -> None:
    """The no-secrets discipline: after a full retry scenario (including
    a transient classifier failure with a credential-shaped message),
    no persisted byte anywhere carries credential-shaped content."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    transport_run = make_run(1)
    job_run = make_run(2)
    classifier = MappingClassifier(
        {
            transport_run.external.job_id: FAILURE_CLASS_TRANSPORT,
            job_run.external.job_id: FAILURE_CLASS_JOB,
        }
    )
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        classifier=classifier,
        resubmit=RecordingResubmit(),
    )
    watch_all(
        dispatcher,
        (
            make_watch_record(1, external=transport_run.external),
            make_watch_record(2, external=job_run.external),
        ),
    )
    for run in (transport_run, job_run):
        write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, transport_run, policy)
    write_goal(dispatcher, job_run)
    dispatcher.decide_all()
    # A transient classifier failure with a credential-shaped message.
    transient_dispatcher = make_dispatcher(
        tmp_path / "t-state",
        tmp_path / "t-runs",
        tmp_path / "t-events",
        classifier=ScriptedClassifier(
            RuntimeError("slurm ssh token authentication failed")
        ),
        resubmit=RecordingResubmit(),
    )
    transient_run = make_run(3)
    transient_dispatcher.registry.watch(
        make_watch_record(3, external=transient_run.external)
    )
    write_run(transient_dispatcher.run_store, transient_run)
    write_goal(transient_dispatcher, transient_run)
    transient_dispatcher.decide_all()

    bytes_ = b"".join(
        p.read_bytes()
        for root in (state, runs_dir, events_dir,
                     tmp_path / "t-state", tmp_path / "t-runs",
                     tmp_path / "t-events")
        for p in root.rglob("*")
        if p.is_file()
    )
    lowered = bytes_.decode("utf-8", errors="replace").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered, (
            f"persisted state must never carry {forbidden!r}"
        )
    assert "authentication failed" not in lowered


def test_retry_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public type boundaries."""
    with pytest.raises(TypeError):
        RetryDispatcher(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RetryDispatcher(tmp_path / "s", now="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RetryDispatcher(
            tmp_path / "s", classifier="not callable"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        RetryDispatcher(
            tmp_path / "s", resubmit="not callable"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        RetryDispatcher(
            tmp_path / "s", run_store="not a backend"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        RetryDispatcher(
            tmp_path / "s", event_log="not a log"  # type: ignore[arg-type]
        )
    dispatcher = make_dispatcher(tmp_path / "s", tmp_path / "r", tmp_path / "e")
    with pytest.raises(TypeError):
        dispatcher.decide(42, FAILURE_CLASS_TRANSPORT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        dispatcher.decide(make_run_id(1), 42)  # type: ignore[arg-type]


def test_retry_error_hierarchy_is_value_error_based() -> None:
    """The retry error hierarchy is ValueError-based with stable
    subclasses (the house paradigm for durable-state errors)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(RetryError, MonitoringError)
    assert issubclass(RetryContractError, RetryError)
    assert issubclass(CorruptRetryStateError, RetryError)
    assert RetryContractError is not CorruptRetryStateError
    assert RetryError is not MonitoringError


def test_retry_module_does_not_couple_to_adapters() -> None:
    """Importing the retry dispatcher never pulls in the adapters
    package (proven in a fresh interpreter): the failure classes are
    plain mirrored constants and the resubmission is an injected hook."""
    code = (
        "import sys\n"
        "import scientific_reproduction.monitoring.retry\n"
        "assert 'scientific_reproduction.adapters' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The failure-class bridge
# ---------------------------------------------------------------------------


def test_failure_class_bridge_is_deterministic() -> None:
    """The adapter failure-class -> policy failure-kind bridge is a pure,
    deterministic function: the documented table maps the transport
    class to the policy kind ``ssh_connection_lost``, unclassified
    inputs (None, blank) collapse to the unclassified kind, and every
    other class passes through verbatim (policy kinds the classifier may
    report directly)."""
    assert failure_class_to_failure_kind(None) == (
        RETRY_FAILURE_CLASS_UNCLASSIFIED
    )
    assert failure_class_to_failure_kind("") == (
        RETRY_FAILURE_CLASS_UNCLASSIFIED
    )
    assert failure_class_to_failure_kind("   ") == (
        RETRY_FAILURE_CLASS_UNCLASSIFIED
    )
    assert failure_class_to_failure_kind(FAILURE_CLASS_TRANSPORT) == (
        FAILURE_KIND_SSH_CONNECTION_LOST
    )
    assert failure_class_to_failure_kind(FAILURE_CLASS_JOB) == (
        FAILURE_CLASS_JOB
    )
    assert failure_class_to_failure_kind("scheduler_node_failure") == (
        "scheduler_node_failure"
    )
    assert failure_class_to_failure_kind(CHECKPOINT_CONTINUATION_KIND) == (
        CHECKPOINT_CONTINUATION_KIND
    )


def test_failure_class_bridge_covers_the_adapter_vocabulary() -> None:
    """Every failure class the shipped compute adapters can report is
    covered by the bridge (the adapters are imported locally: the
    monitoring package itself must never import them)."""
    from scientific_reproduction.adapters.compute import (
        slurm_ssh as slurm_ssh_adapter,
    )
    from scientific_reproduction.adapters.compute import ssh as ssh_adapter

    assert ssh_adapter.FAILURE_CLASS_TRANSPORT == FAILURE_CLASS_TRANSPORT
    assert slurm_ssh_adapter.FAILURE_CLASS_TRANSPORT == FAILURE_CLASS_TRANSPORT
    assert ssh_adapter.FAILURE_CLASS_JOB == FAILURE_CLASS_JOB
    assert slurm_ssh_adapter.FAILURE_CLASS_JOB == FAILURE_CLASS_JOB
    for adapter_class in (
        ssh_adapter.FAILURE_CLASS_TRANSPORT,
        ssh_adapter.FAILURE_CLASS_JOB,
        slurm_ssh_adapter.FAILURE_CLASS_TRANSPORT,
        slurm_ssh_adapter.FAILURE_CLASS_JOB,
    ):
        assert failure_class_to_failure_kind(adapter_class) in {
            FAILURE_KIND_SSH_CONNECTION_LOST,
            FAILURE_CLASS_JOB,
        }
