"""Tests for the retry-aftermath persistence (issue #150).

An authorized engineering retry decision was recorded only in the
event-log payload -- the Run record's ``engineering_retries`` history
had no writer (the report always rendered zero retries) and the watch
entry kept the dead job's external identity, so reconciliation kept
polling the dead job forever. The decision's aftermath is now
persisted through the same injected stores, after the event append
(the exactly-once decision fact), under the bounded per-run authoring
lease (the issue #145 discipline):

* the Run record gains exactly one ``engineering_retries`` entry per
  recorded authorized decision (the decision payload keyed by the
  event id) and its ``external`` identity advances to the resubmitted
  receipt -- a history update, never a parameter mutation;
* the watch entry's external identity advances to the resubmitted
  receipt, so the shipped reconciliation probes the resubmitted job
  instead of the dead one;
* the report renders the persisted retry history;
* a replay of a recorded decision heals a lost aftermath idempotently
  (the crash window between the event append and the aftermath), and a
  held lease fails the decision loudly -- the recorded event replays
  and heals on the next pass.

Determinism: every test injects a fixed-stamp clock, deterministic
``generate_id`` ids, ``tmp_path`` state directories and the real
filesystem state backend, watch registry and event log. No randomness,
no network, no sleeps anywhere.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.leases import LeaseHeldError, LeaseStore
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
from scientific_reproduction.monitoring.reconcile import (
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATE_RUNNING,
    EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
    ReconcileEngine,
)
from scientific_reproduction.monitoring.registry import WatchedRunRecord
from scientific_reproduction.monitoring.retry import (
    FAILURE_CLASS_TRANSPORT,
    FAILURE_KIND_SSH_CONNECTION_LOST,
    RETRY_DECISION_AUTHORIZED,
    RetryDispatcher,
)
from scientific_reproduction.planning.init import initialize_project
from scientific_reproduction.reporting.report import build_report
from scientific_reproduction.research.evidence import EvidenceRegistry

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"


class FakeClock:
    """Injectable clock: a single fixed stamp repeats forever and every
    read is recorded (mirrors the retry-dispatcher tests' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


class ConstantClassifier:
    """Failure classifier that always reports one fixed class (every
    call records the external identity it was given)."""

    def __init__(self, failure_class: str) -> None:
        self._failure_class = failure_class
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._failure_class


class MappingProbe:
    """External-status probe that reports a state per external job id
    (mirrors the reconcile tests' probe; every call records the
    external identity it was given)."""

    def __init__(self, states: dict[str, str]) -> None:
        self._states = dict(states)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._states.get(external.job_id, EXTERNAL_STATE_RUNNING)


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
    working_directory: str | None = None,
) -> RunExternal:
    """An external identity; by default a slurm-ssh-shaped one with a
    job id and a working directory."""
    return RunExternal(
        backend=backend,
        job_id=job_id,
        working_directory=working_directory,
    )


def make_watch_record(
    index: int = 1,
    *,
    external: RunExternal | None = None,
) -> WatchedRunRecord:
    """A deterministic watch entry for run ``index`` (the identity the
    dispatcher decides under)."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return WatchedRunRecord(
        run_id=run_id,
        external=external,
        watched_at=FIXED_STAMP,
        adapter_id="adapter:compute/slurm_ssh",
        adapter_version="1.0",
    )


def make_run(
    index: int = 1,
    *,
    external: RunExternal | None = None,
) -> Run:
    """A deterministic durable Run record (``RUNNING_EXTERNAL``, the
    fixed created/updated stamps)."""
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
        lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
        goal_version="v1",
        external=external,
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def write_run(run_store: FilesystemStateBackend, run: Run) -> None:
    """Persist a run through the real schema-validating backend."""
    run_store.write("run", run.run_id, run.to_dict())


def make_policy(
    index: int = 1,
    *,
    allowed: tuple[str, ...] = (),
) -> AutomaticRetryPolicy:
    """A deterministic frozen automatic retry policy record."""
    return AutomaticRetryPolicy(
        policy_id=generate_id("policy", f"p{index}"),
        allowed_engineering_failures=list(allowed),
        supervisor_required_changes=[],
    )


def write_goal_and_policy(
    dispatcher: RetryDispatcher, run: Run, policy: AutomaticRetryPolicy
) -> None:
    """The standard policy fixture: a Goal referencing ``policy`` and
    the policy record itself (both through the real backend)."""
    goal = GoalContract(
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
        automatic_retry_policy_ref=policy.policy_id,
        frozen_at=FIXED_STAMP,
        frozen_commit="abcdef0",
    )
    dispatcher.run_store.write("goal", run.goal_id, goal.to_dict())
    dispatcher.run_store.write(
        "retry-policy", policy.policy_id, policy.to_dict()
    )


def make_dispatcher(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    classifier: ConstantClassifier | None = None,
    resubmit: RecordingResubmit | None = None,
    clock: FakeClock | None = None,
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
    )


def watch_all(
    dispatcher: RetryDispatcher, records: tuple[WatchedRunRecord, ...]
) -> None:
    """Watch every record through the dispatcher's registry (durable)."""
    for record in records:
        dispatcher.registry.watch(record)


def event_records(events_dir: Path) -> list[dict[str, object]]:
    """The raw persisted event records (sorted, read from disk)."""
    records: list[dict[str, object]] = []
    for path in sorted((events_dir / "events").glob("*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


# ---------------------------------------------------------------------------
# The aftermath of an authorized decision
# ---------------------------------------------------------------------------


def test_aftermath_run_record_carries_retry_history_entry(
    tmp_path: Path,
) -> None:
    """The Run record of an authorized decision gains exactly one
    ``engineering_retries`` entry -- the recorded decision payload keyed
    by the event id -- and its ``external`` identity advances to the
    resubmitted receipt, stamped from the injected clock: a history
    update; every parameter and the lifecycle stay untouched."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    decision_stamp = "2026-08-14T01:00:00+00:00"
    run = make_run(1)
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        resubmit=RecordingResubmit(),
        clock=FakeClock(decision_stamp),
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    assert outcome.decision == RETRY_DECISION_AUTHORIZED
    resubmitted = outcome.resubmitted_external
    assert resubmitted is not None
    events = event_records(events_dir)
    assert len(events) == 1
    assert events[0]["event_id"] == outcome.event_id

    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    # Exactly one entry: the decision payload keyed by the event id.
    assert persisted.engineering_retries == [
        {**events[0]["payload"], "event_id": outcome.event_id}
    ]
    # The record's external identity advanced to the receipt and the
    # write was stamped from the injected clock.
    assert persisted.external == resubmitted
    assert persisted.updated_at == decision_stamp
    # A history update, never a parameter mutation.
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert persisted.goal_id == run.goal_id
    assert persisted.run_type is run.run_type
    assert persisted.goal_version == run.goal_version
    assert persisted.artifacts == run.artifacts
    assert persisted.deviations == run.deviations
    assert persisted.created_at == run.created_at


def test_aftermath_watch_entry_names_resubmitted_identity(
    tmp_path: Path,
) -> None:
    """The watch entry of an authorized decision advances to the
    resubmitted external identity: the shipped reconciliation probes
    the resubmitted job, never the dead one (issue #150)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=RecordingResubmit()
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    resubmitted = outcome.resubmitted_external
    assert resubmitted is not None
    watched = dispatcher.registry.get(run.run_id)
    assert watched.external == resubmitted
    # A fresh job identity, not the dead one.
    assert watched.external.job_id != run.external.job_id


def test_aftermath_report_renders_the_retry_history(tmp_path: Path) -> None:
    """The human-readable report renders the persisted retry history:
    the strict/recovery-history section counts the run with its
    engineering retries and the failures section renders the retry
    total -- from the Run record written by the aftermath (issue #150:
    ``Run.engineering_retries`` gains a real writer)."""
    root = tmp_path / "workspace"
    initialize_project(
        root,
        "10.1039/D5TA00771B",
        timestamp=datetime(2026, 8, 14, tzinfo=timezone.utc),
        identity=AuditIdentity(name="Audit Bot", email="audit@example.org"),
    )
    run = make_run(1)
    dispatcher = RetryDispatcher(
        tmp_path / "state",
        now=FakeClock(),
        resubmit=RecordingResubmit(),
        run_store=FilesystemStateBackend(root),
        event_log=ProjectEventLog(tmp_path / "events"),
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert outcome.decision == RETRY_DECISION_AUTHORIZED

    report = build_report(root, EvidenceRegistry(), [])
    sections = {section.title: section for section in report.sections}
    recovery = sections["Strict/recovery history"].body
    assert "Runs with engineering retries: 1" in recovery
    assert f"- {run.run_id}: 1 engineering retries" in recovery
    failures = sections["Failures and deviations"].body
    assert "engineering retries: 1 total" in failures


def test_aftermath_reconcile_observes_resubmitted_job_completion(
    tmp_path: Path,
) -> None:
    """The shipped reconciliation observes the RESUBMITTED job's
    completion after an authorized retry: the watch entry names the
    fresh job, so the probe receives the resubmitted external identity
    -- never the dead one -- and the completion moves the Run to
    ``RESULT_AVAILABLE`` through the real transition machinery."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    dispatcher = make_dispatcher(
        state, runs_dir, events_dir, resubmit=RecordingResubmit()
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    outcome = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    resubmitted = outcome.resubmitted_external
    assert resubmitted is not None

    # The resubmitted job completes; the dead job is never probed.
    probe = MappingProbe(
        {resubmitted.job_id: EXTERNAL_STATE_RESULT_AVAILABLE}
    )
    engine = ReconcileEngine(
        state,
        now=FakeClock(),
        probe=probe,
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
    )

    result = engine.reconcile(run.run_id)

    assert result.completed
    # The probe received the resubmitted identity -- exactly once, and
    # never the dead job's identity.
    assert probe.calls == [resubmitted]
    persisted = Run.from_dict(engine.run_store.read("run", run.run_id))
    assert persisted.lifecycle_state is LifecycleState.RESULT_AVAILABLE
    # Exactly one external-status-change event (the retry decision is
    # the only other event in the shared log).
    status_events = [
        event
        for event in event_records(events_dir)
        if event["event_type"] == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    ]
    assert len(status_events) == 1


def test_aftermath_replay_heals_legacy_state(tmp_path: Path) -> None:
    """A recorded authorized decision whose aftermath was lost (the
    crash window between the event append and the aftermath: the Run
    record still carries the dead identity and no retry entry, the
    watch entry still names the dead job) is healed by the replay of
    the recorded event: the entry is appended, both identities advance
    to the recorded receipt, the hook is never re-invoked and no second
    event is appended."""
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
    assert first.resubmitted_external is not None

    # Rewind BOTH the run record and the watch entry to the
    # pre-decision state -- the crash window before any aftermath write.
    write_run(dispatcher.run_store, run)
    dispatcher.registry.unwatch(run.run_id)
    dispatcher.registry.watch(make_watch_record(1, external=run.external))

    replay = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)

    # The recorded decision replays: same receipt and event, no second
    # resubmission, no second event.
    assert replay.replayed
    assert replay.resubmitted_external == first.resubmitted_external
    assert replay.event_id == first.event_id
    assert resubmit.calls == [run.external]
    assert len(event_records(events_dir)) == 1
    # The replay healed the aftermath: the entry and both identities
    # converge to the recorded receipt.
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert len(persisted.engineering_retries) == 1
    assert persisted.engineering_retries[0]["event_id"] == first.event_id
    assert persisted.external == first.resubmitted_external
    # The heal never re-stamps updated_at (replay stays clock-free).
    assert persisted.updated_at == FIXED_STAMP
    assert dispatcher.registry.get(run.run_id).external == (
        first.resubmitted_external
    )


def test_aftermath_lease_conflict_fails_loudly(tmp_path: Path) -> None:
    """A held per-run authoring lease makes the aftermath fail loudly
    (the issue #145 concurrency discipline): ``decide`` raises
    ``LeaseHeldError`` after the decision event is recorded (the
    exactly-once fact survives), ``decide_all`` isolates the failure
    per run, and once the lease is released the recorded event replays
    and heals the aftermath."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    resubmit = RecordingResubmit()
    dispatcher = make_dispatcher(
        state,
        runs_dir,
        events_dir,
        classifier=ConstantClassifier(FAILURE_CLASS_TRANSPORT),
        resubmit=resubmit,
    )
    watch_all(dispatcher, (make_watch_record(1, external=run.external),))
    write_run(dispatcher.run_store, run)
    policy = make_policy(1, allowed=(FAILURE_KIND_SSH_CONNECTION_LOST,))
    write_goal_and_policy(dispatcher, run, policy)

    leases = LeaseStore(runs_dir)
    held = leases.acquire("run", run.run_id, "other-principal", 60.0)

    # The decision event is the exactly-once fact: it is recorded, then
    # the aftermath fails loudly on the held lease.
    with pytest.raises(LeaseHeldError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert resubmit.calls == [run.external]
    assert len(event_records(events_dir)) == 1
    # Nothing of the aftermath happened.
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert persisted.engineering_retries == []
    assert persisted.external == run.external
    assert dispatcher.registry.get(run.run_id).external == run.external

    # The pass-level API isolates the held-lease failure per run (the
    # replay of the recorded decision heals the aftermath and hits the
    # same held lease).
    summary = dispatcher.decide_all()
    assert len(summary.outcomes) == 0
    assert len(summary.failures) == 1
    assert summary.failures[0].run_id == run.run_id
    assert summary.failures[0].error == "LeaseHeldError"
    assert summary.failures[0].message

    # Once the lease is released the recorded event replays and heals
    # the aftermath to convergence -- no second event, no second
    # resubmission.
    leases.release(held)
    healed = dispatcher.decide_all()
    assert len(healed.outcomes) == 1
    assert healed.outcomes[0].replayed
    receipt = healed.outcomes[0].resubmitted_external
    assert receipt is not None
    assert len(event_records(events_dir)) == 1
    assert resubmit.calls == [run.external]
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert len(persisted.engineering_retries) == 1
    assert persisted.engineering_retries[0]["event_id"] == (
        healed.outcomes[0].event_id
    )
    assert persisted.external == receipt
    assert dispatcher.registry.get(run.run_id).external == receipt
