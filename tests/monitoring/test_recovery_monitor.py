"""Tests for the Monitor session recovery / replacement contract
(DEV-M8-G04, deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac01_*`` -- AC-01: a **fresh recovery object** over the same
  state directory reconstructs the full watched set, the per-run
  checkpoint progress and the completion facts (from the Run records and
  the event log) from the durable state alone, with no
  original-conversation artifacts, and yields the resumable reconcile
  configuration (monitor identity, probe seam, run store, event log) via
  ``resume_engine()``.
* ``test_ac02_*`` -- AC-02: reconstruction is observation-only: an
  injected dispatch hook is never invoked (zero external job creation),
  reconstruction writes nothing (the durable state stays byte-identical),
  and resuming reconciliation through the DEV-M8-G02 engine never
  re-emits anything already durably recorded.
* ``test_ac03_*`` -- AC-03: a completion that occurred while the original
  Monitor was down (the probe reports ``RESULT_AVAILABLE`` on the first
  replacement pass, with no event/checkpoint yet from the original
  Monitor) is reconciled by the replacement's first reconciliation pass:
  the Run moves to ``RESULT_AVAILABLE``, the single completion event is
  appended under the event log's idempotency key, and the checkpoint
  records the progress -- exactly once.
* ``test_recovery_*`` -- the durable contracts: stable ``MonitoringError``
  subclasses (``CorruptRecoveryStateError`` for corrupt reconstruction
  state, ``RecoveryContractError`` for identity contract violations),
  ``TypeError`` at the public type boundaries, the injected clock, the
  no-secrets discipline (walked over every persisted byte), deterministic
  reconstruction for identical inputs, and the no-adapters architectural
  boundary.

Determinism: every test injects a :class:`FakeClock` producing the
fixed ``FIXED_STAMP`` timestamp (no wall clock), ``tmp_path`` state
directories and ``generate_id`` ids. No randomness, no network, no
sleeps anywhere.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    LifecycleState,
    ProjectEvent,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring import MonitoringError
from scientific_reproduction.monitoring.checkpoint import (
    MonitorCheckpointStore,
    MonitorRunCheckpoint,
)
from scientific_reproduction.monitoring.reconcile import (
    EXTERNAL_COMPLETION_REASON,
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATE_RUNNING,
    EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
    RECONCILE_ACTOR,
    RECONCILE_COMPLETION_KEY_PREFIX,
    ReconcileEngine,
)
from scientific_reproduction.monitoring.recovery import (
    CorruptRecoveryStateError,
    MonitorRecovery,
    RecoveredCompletion,
    RecoveryContractError,
    RecoveryError,
    RecoveryPlan,
)
from scientific_reproduction.monitoring.registry import (
    WatchedRunRecord,
    WatchedRunRegistry,
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


class ConstantProbe:
    """External-status probe that always reports one fixed state."""

    def __init__(self, state: str) -> None:
        self._state = state
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._state


class MappingProbe:
    """External-status probe that reports a state per external job id
    (order-independent external truth)."""

    def __init__(self, states: dict[str, str]) -> None:
        self._states = dict(states)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._states.get(external.job_id, EXTERNAL_STATE_RUNNING)


class CountingDispatch:
    """Injected external-job dispatch hook that counts every call: the
    AC-02 negative-proof seam -- reconstruction must never invoke it."""

    def __init__(self) -> None:
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return EXTERNAL_STATE_RUNNING


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
    """A deterministic watch entry for run ``index``."""
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


def make_recovery(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    probe: Callable[[RunExternal], str] | None = None,
    clock: FakeClock | None = None,
    monitor_id: str | None = None,
    dispatch: Callable[[RunExternal], str] | None = None,
) -> MonitorRecovery:
    """A recovery object over ``state_dir`` with an injected run store
    over ``runs_dir``, an event log over ``events_dir``, the fixed clock
    and (optionally) a probe and a dispatch hook."""
    return MonitorRecovery(
        state_dir,
        now=clock or FakeClock(),
        probe=probe,
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        monitor_id=monitor_id,
        dispatch=dispatch,
    )


def make_engine(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    probe: Callable[[RunExternal], str] | None = None,
    clock: FakeClock | None = None,
    monitor_id: str | None = None,
) -> ReconcileEngine:
    """The original Monitor's engine over the same injected stores (used
    only to build the durable state a replacement recovers from)."""
    return ReconcileEngine(
        state_dir,
        now=clock or FakeClock(),
        probe=probe,
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        monitor_id=monitor_id,
    )


def load_checkpoint(state_dir: Path) -> tuple[MonitorRunCheckpoint, ...] | None:
    """The persisted checkpoint entry set of ``state_dir`` (None when no
    checkpoint was ever written)."""
    checkpoint = MonitorCheckpointStore(state_dir).load()
    if checkpoint is None:
        return None
    return checkpoint.entries


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


def watch_all(
    engine: ReconcileEngine, records: tuple[WatchedRunRecord, ...]
) -> None:
    """Watch every record through the engine's registry (durable)."""
    for record in records:
        engine.registry.watch(record)


def build_completed_state(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    monitor_id: str | None = None,
) -> tuple[ReconcileEngine, Run, WatchedRunRecord]:
    """Build the durable state of one run completed by the original
    Monitor before it disappeared: watch entry, Run record at
    RESULT_AVAILABLE, the single completion event and the checkpoint
    progress. Returns ``(engine, run, watch_record)``."""
    engine = make_engine(
        state_dir, runs_dir, events_dir, monitor_id=monitor_id
    )
    run = make_run(1)
    watch = make_watch_record(1, external=run.external)
    watch_all(engine, (watch,))
    write_run(engine.run_store, run)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(
        state_dir,
        runs_dir,
        events_dir,
        probe=probe,
        monitor_id=monitor_id,
    )
    outcome = engine.reconcile(run.run_id)
    assert outcome.completed is True
    return engine, run, watch


# ---------------------------------------------------------------------------
# AC-01: replacement reconstructs the watched Runs without the original
# conversation
# ---------------------------------------------------------------------------


def test_ac01_fresh_recovery_reconstructs_watch_set_and_progress(
    tmp_path: Path,
) -> None:
    """AC-01: a fresh recovery object over the same state directory
    reconstructs the full watch set, the per-run checkpoint progress and
    the completion facts from the durable state alone -- no
    original-conversation artifact is involved."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    engine = make_engine(state, runs_dir, events_dir)
    completed_run = make_run(1)
    running_run = make_run(2)
    states = {
        completed_run.external.job_id: EXTERNAL_STATE_RESULT_AVAILABLE,
        running_run.external.job_id: EXTERNAL_STATE_RUNNING,
    }
    engine = make_engine(state, runs_dir, events_dir, probe=MappingProbe(states))
    completed_watch = make_watch_record(1, external=completed_run.external)
    running_watch = make_watch_record(2, external=running_run.external)
    watch_all(engine, (completed_watch, running_watch))
    for run in (completed_run, running_run):
        write_run(engine.run_store, run)
    first = engine.reconcile_all()
    assert first.completed_count == 1
    expected_watched = tuple(
        sorted((completed_watch, running_watch), key=lambda w: w.run_id)
    )
    expected_progress = load_checkpoint(state)
    assert expected_progress is not None and len(expected_progress) == 2
    expected_event_id = engine.event_log.list_events()[0].event.event_id

    # A FRESH recovery object over the same durable state -- no session
    # state, no reference to the original engine.
    fresh = make_recovery(state, runs_dir, events_dir)

    plan = fresh.reconstruct()

    assert plan.monitor_id == engine.monitor_id
    assert plan.watched == expected_watched
    assert plan.progress == expected_progress
    assert len(plan.completions) == 2
    by_run = {completion.run_id: completion for completion in plan.completions}
    assert by_run[completed_run.run_id] == RecoveredCompletion(
        run_id=completed_run.run_id,
        run_state=LifecycleState.RESULT_AVAILABLE,
        event_id=expected_event_id,
        event_timestamp=FIXED_STAMP,
        event_logged=True,
        checkpoint_records_completion=True,
    )
    assert by_run[completed_run.run_id].completed is True
    assert by_run[running_run.run_id] == RecoveredCompletion(
        run_id=running_run.run_id,
        run_state=LifecycleState.RUNNING_EXTERNAL,
        event_logged=False,
        checkpoint_records_completion=False,
    )
    assert by_run[running_run.run_id].completed is False


def test_ac01_fresh_recovery_yields_resumable_reconcile_configuration(
    tmp_path: Path,
) -> None:
    """AC-01: the reconstructed resumable reconcile configuration -- the
    monitor identity, the probe seam, the run store and the event log --
    is carried by the fresh recovery object: ``resume_engine()`` returns
    an engine over the same state directory wired to the same injected
    dependencies, and its reconciliation matches the original engine's
    durable record (idempotent, no re-emission)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    engine.reconcile(run.run_id)

    fresh = make_recovery(state, runs_dir, events_dir, probe=probe)
    assert fresh.monitor_id == engine.monitor_id
    assert fresh.probe is probe
    resumed = fresh.resume_engine()
    assert resumed.monitor_id == fresh.monitor_id
    assert resumed.probe is probe
    assert resumed.run_store is fresh.run_store
    assert resumed.event_log is fresh.event_log

    event_bytes = tree_bytes(events_dir)
    state_bytes = tree_bytes(state)
    second = resumed.reconcile_all()
    # Already durably recorded: the resumed pass re-emits nothing.
    assert second.completed_count == 0
    assert tree_bytes(events_dir) == event_bytes
    assert tree_bytes(state) == state_bytes
    assert len(fresh.event_log.list_events()) == 1


def test_ac01_empty_state_directory_yields_empty_plan(tmp_path: Path) -> None:
    """AC-01: reconstructing a fresh state directory (no durable state)
    yields an empty plan, deterministically."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    recovery = make_recovery(state, runs_dir, events_dir)
    plan = recovery.reconstruct()
    assert plan == RecoveryPlan(
        monitor_id=recovery.monitor_id,
        watched=(),
        progress=(),
        completions=(),
    )
    assert plan.watched == ()
    assert plan.progress == ()
    assert plan.completions == ()


# ---------------------------------------------------------------------------
# AC-02: no duplicate external job is created during reconstruction
# ---------------------------------------------------------------------------


def test_ac02_reconstruction_performs_zero_external_job_creation(
    tmp_path: Path,
) -> None:
    """AC-02: reconstruction is observation-only -- an injected dispatch
    hook (the seam through which a replacement session would create
    external jobs) is never invoked: zero external creation calls across
    reconstruction and the resumed reconciliation pass."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    build_completed_state(state, runs_dir, events_dir)
    dispatch = CountingDispatch()
    recovery = make_recovery(state, runs_dir, events_dir, dispatch=dispatch)
    assert recovery.dispatch is dispatch

    plan = recovery.reconstruct()
    assert len(plan.watched) == 1
    assert dispatch.calls == []

    # The resumed first pass reconciles through the DEV-M8-G02 engine
    # (observation + transition + event), never through the dispatch
    # seam: the external job is already running; nothing is re-created.
    summary = recovery.resume_engine().reconcile_all()
    assert summary.completed_count == 0
    assert dispatch.calls == []

    recovery.reconstruct()
    assert dispatch.calls == []


def test_ac02_reconstruction_writes_nothing_and_state_stays_byte_identical(
    tmp_path: Path,
) -> None:
    """AC-02: reconstruction never writes: the durable state (watch
    entries, checkpoint, Run records, event log) is byte-identical
    before and after, and the reconstructed plan is exactly the durable
    state -- no hidden re-dispatch, no hidden mutation."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    build_completed_state(state, runs_dir, events_dir)
    before = {
        "state": tree_bytes(state),
        "runs": tree_bytes(runs_dir),
        "events": tree_bytes(events_dir),
    }

    recovery = make_recovery(state, runs_dir, events_dir)
    plan = recovery.reconstruct()

    assert tree_bytes(state) == before["state"]
    assert tree_bytes(runs_dir) == before["runs"]
    assert tree_bytes(events_dir) == before["events"]
    # The plan is exactly the durable state, read back independently.
    fresh_registry = WatchedRunRegistry(state)
    assert plan.watched == fresh_registry.list_watched()
    assert plan.progress == MonitorCheckpointStore(state).load().entries


def test_ac02_replacement_resume_never_reemits_durably_recorded_state(
    tmp_path: Path,
) -> None:
    """AC-02 interaction with DEV-M8-G02 exactly-once: the replacement
    resumes reconciliation (a ReconcileEngine over the same dirs)
    without re-emitting anything already durably recorded -- the single
    completion event stays single, the event log bytes stay identical,
    no dispatch happens."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    build_completed_state(state, runs_dir, events_dir)
    event_bytes = tree_bytes(events_dir)
    state_bytes = tree_bytes(state)
    dispatch = CountingDispatch()

    # The external truth still reports the completion (the steady-state
    # re-poll refreshes the observation with the same state).
    recovery = make_recovery(
        state,
        runs_dir,
        events_dir,
        probe=ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE),
        dispatch=dispatch,
    )
    recovery.reconstruct()
    summary = recovery.resume_engine().reconcile_all()

    assert summary.completed_count == 0
    records = recovery.event_log.list_events()
    assert len(records) == 1
    assert records[0].sequence == 1
    assert tree_bytes(events_dir) == event_bytes
    assert tree_bytes(state) == state_bytes
    assert dispatch.calls == []


# ---------------------------------------------------------------------------
# AC-03: a completion during the Monitor outage is reconciled later
# ---------------------------------------------------------------------------


def test_ac03_completion_during_outage_reconciled_by_replacement_first_pass(
    tmp_path: Path,
) -> None:
    """AC-03: the external run completed while the original Monitor was
    down -- the durable state has the watch entry and the Run record at
    RUNNING_EXTERNAL, but NO event and NO checkpoint from the original
    Monitor. The replacement reconstructs the state (the run is not yet
    durably completed) and its first reconciliation pass reconciles the
    completion: the Run moves to RESULT_AVAILABLE, the single completion
    event is appended and the checkpoint records the progress."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)  # still RUNNING_EXTERNAL in the durable state
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    # The original Monitor never observed the completion: no event, no
    # checkpoint at all.
    assert load_checkpoint(state) is None
    assert engine.event_log.list_events() == []

    # The replacement reconstructs from the durable state alone: the run
    # is watched and running, completion not durably recorded.
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    recovery = make_recovery(state, runs_dir, events_dir, probe=probe)
    plan = recovery.reconstruct()
    assert plan.watched == (make_watch_record(1, external=run.external),)
    completion = plan.completions[0]
    assert completion.run_id == run.run_id
    assert completion.run_state is LifecycleState.RUNNING_EXTERNAL
    assert completion.completed is False
    assert completion.event_logged is False
    assert completion.checkpoint_records_completion is False

    # The replacement's first reconciliation pass reconciles the outage
    # completion.
    summary = recovery.resume_engine().reconcile_all()
    assert summary.completed_count == 1
    outcome = summary.outcomes[0]
    assert outcome.completed is True
    assert outcome.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert outcome.observed_at == FIXED_STAMP
    persisted = Run.from_dict(recovery.run_store.read("run", run.run_id))
    assert persisted.lifecycle_state is LifecycleState.RESULT_AVAILABLE
    records = recovery.event_log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.event_type == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    assert event.actor == RECONCILE_ACTOR
    assert event.from_ == LifecycleState.RUNNING_EXTERNAL.value
    assert event.to == LifecycleState.RESULT_AVAILABLE.value
    assert event.reason == EXTERNAL_COMPLETION_REASON
    entries = load_checkpoint(state)
    assert entries is not None and len(entries) == 1
    assert entries[0].observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert entries[0].reconciled_at == FIXED_STAMP

    # Exactly once: re-running the pass never duplicates the event.
    again = recovery.resume_engine().reconcile_all()
    assert again.completed_count == 0
    assert len(recovery.event_log.list_events()) == 1


def test_ac03_outage_completion_appends_single_event_with_idempotency_key(
    tmp_path: Path,
) -> None:
    """AC-03: the outage completion is appended exactly once, under the
    deterministic idempotency key of DEV-M8-G02 (``reconcile.completed:
    <run_id>``): the event record carries the key, the sequence stays at
    1 and repeated replacement passes resolve to the single original
    record."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    recovery = make_recovery(state, runs_dir, events_dir, probe=probe)
    first = recovery.resume_engine().reconcile_all()
    assert first.completed_count == 1

    records = event_records(events_dir)
    assert len(records) == 1
    assert records[0]["idempotency_key"] == (
        f"{RECONCILE_COMPLETION_KEY_PREFIX}:{run.run_id}"
    )
    assert records[0]["sequence"] == 1
    assert records[0]["run_id"] == run.run_id

    # A second replacement pass (and a third fresh pass) resolve the
    # idempotency claim to the single original record: never a
    # duplicate, never a sequence advance.
    second = recovery.resume_engine().reconcile_all()
    assert second.completed_count == 0
    assert len(event_records(events_dir)) == 1
    assert recovery.event_log.list_events()[0].sequence == 1
    third = make_recovery(state, runs_dir, events_dir, probe=probe)
    third.resume_engine().reconcile_all()
    assert len(event_records(events_dir)) == 1


def test_ac03_stale_running_observation_is_overridden_by_replacement_pass(
    tmp_path: Path,
) -> None:
    """AC-03: when the original Monitor recorded a RUNNING observation
    in the checkpoint before going down, and the external run completed
    during the outage, the replacement's first pass reconciles the
    completion and overrides the stale observation -- exactly one event,
    checkpoint progress updated."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(
        state,
        runs_dir,
        events_dir,
        probe=ConstantProbe(EXTERNAL_STATE_RUNNING),
    )
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    # The original Monitor observed RUNNING, then went down.
    engine.reconcile(run.run_id)
    assert engine.event_log.list_events() == []
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == EXTERNAL_STATE_RUNNING

    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    recovery = make_recovery(state, runs_dir, events_dir, probe=probe)
    plan = recovery.reconstruct()
    assert plan.completions[0].checkpoint_records_completion is False

    summary = recovery.resume_engine().reconcile_all()
    assert summary.completed_count == 1
    assert len(recovery.event_log.list_events()) == 1
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert entries[0].reconciled_at == FIXED_STAMP


def test_ac03_mixed_outage_and_pre_outage_runs_reconciled_exactly_once(
    tmp_path: Path,
) -> None:
    """AC-03: a watch set mixing a run completed before the outage
    (event + checkpoint already recorded) and a run completed during the
    outage (nothing recorded yet) is reconciled by the replacement pass
    without duplicating the pre-outage completion: only the outage run
    completes this pass, and the log holds exactly one event per run."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    pre_outage_run = make_run(1)
    outage_run = make_run(2)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(
        engine,
        (
            make_watch_record(1, external=pre_outage_run.external),
            make_watch_record(2, external=outage_run.external),
        ),
    )
    for run in (pre_outage_run, outage_run):
        write_run(engine.run_store, run)
    # Run 1 completed before the outage; run 2 still running when the
    # original Monitor went down, then completed during the outage.
    probe = MappingProbe(
        {
            pre_outage_run.external.job_id: EXTERNAL_STATE_RESULT_AVAILABLE,
            outage_run.external.job_id: EXTERNAL_STATE_RUNNING,
        }
    )
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    first = engine.reconcile_all()
    assert first.completed_count == 1

    # The replacement sees the external truth after the outage: both
    # completed.
    after_outage = MappingProbe(
        {
            pre_outage_run.external.job_id: EXTERNAL_STATE_RESULT_AVAILABLE,
            outage_run.external.job_id: EXTERNAL_STATE_RESULT_AVAILABLE,
        }
    )
    recovery = make_recovery(state, runs_dir, events_dir, probe=after_outage)
    plan = recovery.reconstruct()
    by_run = {c.run_id: c for c in plan.completions}
    assert by_run[pre_outage_run.run_id].event_logged is True
    assert by_run[pre_outage_run.run_id].checkpoint_records_completion is True
    assert by_run[outage_run.run_id].event_logged is False

    summary = recovery.resume_engine().reconcile_all()
    assert summary.completed_count == 1
    completed_ids = [
        o.run_id for o in summary.outcomes if o.completed
    ]
    assert completed_ids == [outage_run.run_id]
    assert Run.from_dict(
        recovery.run_store.read("run", outage_run.run_id)
    ).lifecycle_state is LifecycleState.RESULT_AVAILABLE
    # One event per run, both with the idempotency keys.
    records = recovery.event_log.list_events()
    assert len(records) == 2
    assert {r.event.run_id for r in records} == {
        pre_outage_run.run_id, outage_run.run_id
    }
    # Re-running the pass never duplicates.
    recovery.resume_engine().reconcile_all()
    assert len(recovery.event_log.list_events()) == 2


# ---------------------------------------------------------------------------
# The recovery contracts
# ---------------------------------------------------------------------------


def test_recovery_corrupt_watch_entry_fails_reconstruction_loudly(
    tmp_path: Path,
) -> None:
    """A corrupt watch entry on disk fails reconstruction loudly with the
    stable CorruptRecoveryStateError (a ValueError subclass) -- corrupt
    reconstruction state is never silently skipped."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    (state / "watched" / f"{run.run_id}.json").write_text(
        "{not json", encoding="utf-8"
    )
    recovery = make_recovery(state, runs_dir, events_dir)
    with pytest.raises(CorruptRecoveryStateError):
        recovery.reconstruct()


def test_recovery_corrupt_checkpoint_fails_reconstruction_loudly(
    tmp_path: Path,
) -> None:
    """A corrupt checkpoint file on disk fails reconstruction loudly with
    the stable CorruptRecoveryStateError."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    (state / "checkpoint.json").write_text("[1, 2]", encoding="utf-8")
    recovery = make_recovery(state, runs_dir, events_dir)
    with pytest.raises(CorruptRecoveryStateError):
        recovery.reconstruct()


def test_recovery_missing_run_record_fails_reconstruction_loudly(
    tmp_path: Path,
) -> None:
    """A watch entry referencing a run with no Run record in the run
    store is corrupt reconstruction state: it fails loudly with the
    stable CorruptRecoveryStateError, never a silent skip."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1),))
    # No write_run: the run record is missing.
    recovery = make_recovery(state, runs_dir, events_dir)
    with pytest.raises(CorruptRecoveryStateError):
        recovery.reconstruct()


def test_recovery_external_identity_mismatch_raises_contract_error(
    tmp_path: Path,
) -> None:
    """A Run record whose external identity disagrees with its watch
    entry is a contract violation (the replacement would resume polling
    the wrong external run): the stable RecoveryContractError, before
    any reconciliation."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    engine = make_engine(state, runs_dir, events_dir)
    run = make_run(1)  # watch entry matches this identity
    mismatched = make_run(
        1,
        external=make_external(
            backend="slurm_ssh",
            job_id=generate_id("job", "some-other-run"),
            working_directory="/home/alice/scratch/other",
        ),
    )
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, mismatched)
    recovery = make_recovery(state, runs_dir, events_dir)
    with pytest.raises(RecoveryContractError):
        recovery.reconstruct()


def test_recovery_completion_event_without_run_completion_is_corrupt(
    tmp_path: Path,
) -> None:
    """A completion event in the event log for a run whose Run record
    does not record a completion is internally contradictory durable
    state: reconstruction fails loudly with the stable
    CorruptRecoveryStateError."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    engine = make_engine(state, runs_dir, events_dir)
    run = make_run(1)  # still RUNNING_EXTERNAL
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    # A completion event planted in the log although the Run record
    # never recorded the completion.
    event_log = engine.event_log
    event = ProjectEvent(
        event_id=generate_id(
            "event",
            EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
            run.run_id,
            LifecycleState.RUNNING_EXTERNAL.value,
            LifecycleState.RESULT_AVAILABLE.value,
        ),
        timestamp=FIXED_STAMP,
        actor=RECONCILE_ACTOR,
        event_type=EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
        object_id=run.run_id,
        run_id=run.run_id,
        from_=LifecycleState.RUNNING_EXTERNAL.value,
        to=LifecycleState.RESULT_AVAILABLE.value,
        reason=EXTERNAL_COMPLETION_REASON,
    )
    event_log.append(
        event, idempotency_key=f"{RECONCILE_COMPLETION_KEY_PREFIX}:{run.run_id}"
    )
    recovery = make_recovery(state, runs_dir, events_dir)
    with pytest.raises(CorruptRecoveryStateError):
        recovery.reconstruct()


def test_recovery_reconstruction_is_deterministic_for_identical_inputs(
    tmp_path: Path,
) -> None:
    """AC-01/AC-03 determinism: identical durable state (identical
    injected inputs, fixed clock, deterministic ids) reconstructs to
    identical plans -- no randomness, no wall clock."""
    monitor_id = generate_id("monitor", "identical")
    plans: list[RecoveryPlan] = []
    for variant in ("a", "b"):
        state, runs_dir, events_dir = (
            tmp_path / variant / "state",
            tmp_path / variant / "runs",
            tmp_path / variant / "events",
        )
        build_completed_state(
            state, runs_dir, events_dir, monitor_id=monitor_id
        )
        plans.append(
            make_recovery(
                state, runs_dir, events_dir, monitor_id=monitor_id
            ).reconstruct()
        )
    assert plans[0] == plans[1]
    assert plans[0].watched == plans[1].watched
    assert plans[0].progress == plans[1].progress
    assert plans[0].completions == plans[1].completions


def test_recovery_uses_injected_clock_for_resumed_reconciliation(
    tmp_path: Path,
) -> None:
    """Every stamped value of the resumed reconciliation pass comes from
    the injected clock -- no wall clock anywhere."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    clock = FakeClock(FIXED_STAMP)
    recovery = make_recovery(
        state,
        runs_dir,
        events_dir,
        probe=ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE),
        clock=clock,
    )
    summary = recovery.resume_engine().reconcile_all()
    assert summary.reconciled_at == FIXED_STAMP
    assert summary.outcomes[0].observed_at == FIXED_STAMP
    assert summary.outcomes[0].transitioned_at == FIXED_STAMP
    assert recovery.event_log.list_events()[0].event.timestamp == FIXED_STAMP
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_at == FIXED_STAMP
    assert clock.calls, "the resumed engine must consult the injected clock"


def test_recovery_default_store_layout_over_state_dir(tmp_path: Path) -> None:
    """The default run store and event log derive from the state
    directory: runs at ``<state_dir>/run/``, events at
    ``<state_dir>/event/``."""
    state = tmp_path / "state"
    run = make_run(1)
    engine = ReconcileEngine(state, now=FakeClock())
    FilesystemStateBackend(state).write("run", run.run_id, run.to_dict())
    engine.registry.watch(make_watch_record(1, external=run.external))
    recovery = MonitorRecovery(
        state,
        now=FakeClock(),
        probe=ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE),
    )
    plan = recovery.reconstruct()
    assert len(plan.watched) == 1
    assert plan.completions[0].run_state is LifecycleState.RUNNING_EXTERNAL
    summary = recovery.resume_engine().reconcile_all()
    assert summary.completed_count == 1
    assert (state / "runs" / f"{run.run_id}.json").is_file()
    assert event_records(state)  # events at <state_dir>/event/


def test_recovery_persisted_state_never_carries_credentials(
    tmp_path: Path,
) -> None:
    """The no-secrets discipline: after a full outage-reconcile scenario
    (reconstruction + the resumed first pass), no persisted byte
    anywhere carries credential-shaped content."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    recovery = make_recovery(
        state,
        runs_dir,
        events_dir,
        probe=ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE),
    )
    recovery.reconstruct()
    recovery.resume_engine().reconcile_all()

    bytes_ = b"".join(
        p.read_bytes()
        for root in (state, runs_dir, events_dir)
        for p in root.rglob("*")
        if p.is_file()
    )
    lowered = bytes_.decode("utf-8", errors="replace").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered, (
            f"persisted state must never carry {forbidden!r}"
        )


def test_recovery_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public type boundaries."""
    with pytest.raises(TypeError):
        MonitorRecovery(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MonitorRecovery(tmp_path / "s", now="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MonitorRecovery(tmp_path / "s", probe="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MonitorRecovery(
            tmp_path / "s", dispatch="not callable"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        MonitorRecovery(
            tmp_path / "s", run_store="not a backend"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        MonitorRecovery(
            tmp_path / "s", event_log="not a log"  # type: ignore[arg-type]
        )


def test_recovery_error_hierarchy_is_value_error_based() -> None:
    """The recovery error hierarchy is ValueError-based with stable
    subclasses (the house paradigm for durable-state errors)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(RecoveryError, MonitoringError)
    assert issubclass(CorruptRecoveryStateError, RecoveryError)
    assert issubclass(RecoveryContractError, RecoveryError)
    assert CorruptRecoveryStateError is not RecoveryContractError


def test_recovery_module_does_not_couple_to_adapters() -> None:
    """Importing the recovery module never pulls in the adapters package
    (proven in a fresh interpreter): the external ids and the dispatch
    hook are plain documented core ``RunExternal``-shaped vocabulary, not
    adapter types."""
    code = (
        "import sys\n"
        "import scientific_reproduction.monitoring.recovery\n"
        "assert 'scientific_reproduction.adapters' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
