"""Tests for the deterministic engineering retry dispatcher
(DEV-M8-G03, deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac01_*`` -- AC-01: a failure class on the engineering retry
  whitelist (``FAILURE_CLASS_TRANSPORT``) triggers an IDENTICAL
  resubmission through the injected hook -- same run identity, same
  external identity semantics, no parameter change (the dispatcher
  never writes the run store) -- and records exactly one
  ``engineering_retry_decision`` event. Re-deciding the same failure
  never re-invokes the hook (exactly-once per recorded decision).
* ``test_ac02_*`` -- AC-02: a scientific compute failure -- the
  ``"job"`` class, an unclassified failure, any unrecognized failure
  class -- never triggers a resubmission and never mutates run
  parameters: the run's parameters and state bytes are untouched, the
  hook is never invoked, and the refusal is recorded. The default
  configuration (no classifier / no hook) can never authorize a retry.
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
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring import MonitoringError, WatchNotFoundError
from scientific_reproduction.monitoring.registry import WatchedRunRecord
from scientific_reproduction.monitoring.retry import (
    ENGINEERING_RETRY_WHITELIST,
    FAILURE_CLASS_JOB,
    FAILURE_CLASS_TRANSPORT,
    RETRY_ACTOR,
    RETRY_AUTHORIZED_REASON,
    RETRY_DECISION_AUTHORIZED,
    RETRY_DECISION_EVENT_TYPE,
    RETRY_DECISION_REFUSED,
    RETRY_FAILURE_CLASS_UNCLASSIFIED,
    RETRY_REFUSED_REASON,
    CorruptRetryStateError,
    RetryContractError,
    RetryDispatcher,
    RetryError,
    RetryOutcome,
    RetrySummary,
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


def decision_event_id(run_id: str, failure_class: str | None) -> str:
    """The deterministic event id of a decision (a pure function of the
    decision inputs)."""
    normalized = (
        failure_class
        if failure_class is not None
        else RETRY_FAILURE_CLASS_UNCLASSIFIED
    )
    return generate_id("event", RETRY_DECISION_EVENT_TYPE, run_id, normalized)


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
    """AC-01: a failure class on the engineering retry whitelist
    (``FAILURE_CLASS_TRANSPORT`` -- scheduler/node unreachable) triggers
    an identical resubmission through the injected hook: the hook
    receives the watch's external identity, the resubmission receipt is
    recorded, exactly one deterministic decision event is appended, and
    the run record -- parameters and lifecycle -- is byte-identical
    after the decision (an identical resubmission never mutates the
    run)."""
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
    )
    # The hook performed exactly one identical resubmission of the
    # watch's external identity (same backend, same working directory).
    assert resubmit.calls == [run.external]
    assert resubmit.calls[0].backend == run.external.backend

    # The whitelist is exactly the engineering classes (the mirrored
    # transport vocabulary).
    assert ENGINEERING_RETRY_WHITELIST == frozenset({FAILURE_CLASS_TRANSPORT})

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
        "decision": RETRY_DECISION_AUTHORIZED,
        "resubmitted_external": {
            "backend": "slurm_ssh",
            "job_id": generate_id("job", "resubmit-1"),
            "working_directory": "/home/alice/scratch/work-1",
        },
    }
    assert records[0].sequence == 1

    # The identical resubmission: the run record -- parameters and
    # lifecycle -- is byte-identical (no parameter mutation, ever).
    assert (runs_dir / "runs" / f"{run.run_id}.json").read_bytes() == (
        run_file_before
    )
    persisted = Run.from_dict(dispatcher.run_store.read("run", run.run_id))
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert persisted == run


def test_ac01_resubmission_is_exactly_once_per_retry_decision(
    tmp_path: Path,
) -> None:
    """AC-01: re-deciding the same failure never re-invokes the
    resubmission hook: the recorded decision resolves the deterministic
    event id / idempotency key to the single original record and the
    second pass replays it (``replayed=True``, original receipt and
    stamp) -- the resubmission happens exactly once per recorded
    decision and the log sequence never advances twice."""
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

    first = dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert first.decision == RETRY_DECISION_AUTHORIZED
    assert resubmit.calls == [run.external]
    records_after_first = dispatcher.event_log.list_events()

    # The same decision again (e.g. a Monitor loop re-observing the same
    # failed run): no second hook call, no second record, no sequence
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


# ---------------------------------------------------------------------------
# AC-02: scientific compute failure never triggers parameter mutation
# ---------------------------------------------------------------------------


def test_ac02_scientific_compute_failure_never_resubmits_or_mutates(
    tmp_path: Path,
) -> None:
    """AC-02: a scientific compute failure (``FAILURE_CLASS_JOB`` --
    the job's own failure) never triggers a resubmission and never
    mutates anything: the resubmission hook is never invoked, the run's
    parameters and persisted state bytes are untouched, and the refusal
    is observed and recorded (auditable) as one refused decision."""
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
        "decision": RETRY_DECISION_REFUSED,
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

    outcome = dispatcher.decide(run.run_id, None)

    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.failure_class is None
    assert outcome.resubmitted_external is None
    assert resubmit.calls == []
    records = dispatcher.event_log.list_events()
    assert len(records) == 1
    assert records[0].event.payload == {
        "failure_class": None,
        "decision": RETRY_DECISION_REFUSED,
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

    outcome = dispatcher.decide(run.run_id, "custom_backend_error")

    assert outcome.decision == RETRY_DECISION_REFUSED
    assert resubmit.calls == []
    assert dispatcher.event_log.list_events()[0].event.payload == {
        "failure_class": "custom_backend_error",
        "decision": RETRY_DECISION_REFUSED,
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

    # An unclassified failure is refused and recorded by default.
    outcome = dispatcher.decide(run.run_id, None)
    assert outcome.decision == RETRY_DECISION_REFUSED
    assert len(dispatcher.event_log.list_events()) == 1

    # An authorized class cannot be performed without a hook: the
    # default hook raises loudly and nothing is recorded.
    with pytest.raises(RetryError):
        dispatcher.decide(run.run_id, FAILURE_CLASS_TRANSPORT)
    assert len(dispatcher.event_log.list_events()) == 1
    assert Run.from_dict(
        dispatcher.run_store.read("run", run.run_id)
    ) == run

    # decide_all with the default classifier: every watched run is
    # classified as unclassified and refused.
    summary = dispatcher.decide_all()
    assert summary.authorized_count == 0
    assert summary.refused_count == 1


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
    resubmission, identical event-log bytes."""
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

    first.decide(transport_run.run_id, FAILURE_CLASS_TRANSPORT)
    first.decide(job_run.run_id, FAILURE_CLASS_JOB)
    assert first_resubmit.calls == [transport_run.external]
    events_after_first = tree_bytes(events_dir)
    runs_after_first = tree_bytes(runs_dir)

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
    assert tree_bytes(events_dir) == events_after_first
    assert len(fresh.event_log.list_events()) == 2
    # The run store was never touched by any decision or replay.
    assert tree_bytes(runs_dir) == runs_after_first

    # The persisted run records were never touched by any decision.
    for run in runs:
        persisted = Run.from_dict(fresh.run_store.read("run", run.run_id))
        assert persisted == run
        assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL


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
