"""Tests for the deterministic external-status reconciliation and
transition-event emission (DEV-M8-G02, deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac01_*`` -- AC-01: an external completion signal moves the Run
  to ``RESULT_AVAILABLE`` through the real transition machinery and
  appends **exactly one** ``external_status_change`` event (the event
  vocabulary of ``13-EXECUTION-MONITOR.md`` section 5). Re-reconciling
  the same progress never re-transitions and never re-emits; a crash
  between the Run write and the event/checkpoint bookkeeping converges
  on the single completion through the event log's idempotency key.
* ``test_ac02_*`` -- AC-02: unknown / temporarily unavailable /
  transient probe failures / unrecognized external states are observed
  and recorded in the checkpoint but never treated as completion: the
  Run stays in its state and no transition event is emitted.
* ``test_ac03_*`` -- AC-03: a **fresh engine** over the same state
  directory, run store and event log reconstructs progress from the
  durable state alone; re-reconciling the same external runs yields
  identical outcomes and identical durable bytes -- no duplicate
  transitions, no duplicate events.
* ``test_reconcile_*`` -- the durable contracts: stable
  ``MonitoringError`` subclasses (``ReconcileContractError`` for
  lifecycle/identity contract violations, ``CorruptProgressError`` for
  corrupt progress), ``TypeError`` at the public type boundaries, the
  injected clock, the no-secrets discipline (walked over every persisted
  byte), the default no-probe configuration that can never fabricate
  completion, and the no-adapters architectural boundary.

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
from scientific_reproduction.monitoring.checkpoint import (
    MonitorCheckpoint,
    MonitorCheckpointStore,
    MonitorRunCheckpoint,
)
from scientific_reproduction.monitoring.reconcile import (
    COMPLETION_SIGNALS,
    EXTERNAL_COMPLETION_REASON,
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATE_RUNNING,
    EXTERNAL_STATE_UNAVAILABLE,
    EXTERNAL_STATE_UNKNOWN,
    EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
    RECONCILE_ACTOR,
    CorruptProgressError,
    ReconcileContractError,
    ReconcileEngine,
    ReconcileError,
    ReconcileOutcome,
    ReconcileSummary,
)
from scientific_reproduction.monitoring.registry import WatchedRunRecord

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


class ScriptedProbe:
    """Scripted external-status probe: returns queued outcomes in
    order; an exhausted probe reports ``RUNNING_EXTERNAL``; a queued
    exception is raised (a transient probe failure). Every call records
    the external identity it was given."""

    def __init__(self, *outcomes: str | Exception) -> None:
        self._outcomes = list(outcomes)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        if not self._outcomes:
            return EXTERNAL_STATE_RUNNING
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class ConstantProbe:
    """External-status probe that always reports one fixed state (an
    order-independent script)."""

    def __init__(self, state: str) -> None:
        self._state = state
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._state


class MappingProbe:
    """External-status probe that reports a state per external job id
    (order-independent: the same external truth for both engines of an
    AC-03 restart test, whatever the reconcile traversal order)."""

    def __init__(self, states: dict[str, str]) -> None:
        self._states = dict(states)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._states.get(external.job_id, EXTERNAL_STATE_RUNNING)


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
    engine polls under)."""
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


def make_engine(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    probe: ScriptedProbe | None = None,
    clock: FakeClock | None = None,
    monitor_id: str | None = None,
) -> ReconcileEngine:
    """An engine over ``state_dir`` with an injected run store over
    ``runs_dir``, an event log over ``events_dir`` and the fixed clock."""
    return ReconcileEngine(
        state_dir,
        now=clock or FakeClock(),
        probe=probe,
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        monitor_id=monitor_id,
    )


def load_checkpoint(state_dir: Path) -> MonitorRunCheckpoint | None:
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


# ---------------------------------------------------------------------------
# AC-01: external completion moves the Run to RESULT_AVAILABLE exactly once
# ---------------------------------------------------------------------------


def test_ac01_external_completion_moves_run_to_result_available_exactly_once(
    tmp_path: Path,
) -> None:
    """AC-01: a completion signal from the probe moves the Run to
    RESULT_AVAILABLE through the real transition machinery, appends
    exactly one transition event (the 13-EXECUTION-MONITOR.md section 5
    vocabulary) and records the progress in the checkpoint -- and
    re-reconciling the same progress never re-transitions and never
    re-emits."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    run_file_before = (runs_dir / "runs" / f"{run.run_id}.json").read_bytes()

    outcome = engine.reconcile(run.run_id)

    # The completion happened this pass and was stamped from the clock.
    assert outcome == ReconcileOutcome(
        run_id=run.run_id,
        observed_state=EXTERNAL_STATE_RESULT_AVAILABLE,
        observed_at=FIXED_STAMP,
        completed=True,
        transitioned_at=FIXED_STAMP,
    )
    # The Run moved to RESULT_AVAILABLE through the durable run store.
    persisted = Run.from_dict(
        engine.run_store.read("run", run.run_id)
    )
    assert persisted.lifecycle_state is LifecycleState.RESULT_AVAILABLE
    assert persisted.updated_at == FIXED_STAMP
    assert (runs_dir / "runs" / f"{run.run_id}.json").read_bytes() != run_file_before

    # Exactly one transition event with the normative vocabulary.
    log = engine.event_log
    records = log.list_events()
    assert len(records) == 1
    event = records[0].event
    assert event.event_type == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    assert event.actor == RECONCILE_ACTOR
    assert event.run_id == run.run_id
    assert event.object_id == run.run_id
    assert event.from_ == LifecycleState.RUNNING_EXTERNAL.value
    assert event.to == LifecycleState.RESULT_AVAILABLE.value
    assert event.reason == EXTERNAL_COMPLETION_REASON
    assert event.timestamp == FIXED_STAMP
    assert records[0].sequence == 1

    # The checkpoint records the completion observation durably.
    entries = load_checkpoint(state)
    assert entries == (
        MonitorRunCheckpoint(
            run_id=run.run_id,
            external=run.external,
            observed_state=EXTERNAL_STATE_RESULT_AVAILABLE,
            observed_at=FIXED_STAMP,
            reconciled_at=FIXED_STAMP,
        ),
    )

    # Re-reconciling the same progress: no second transition, no second
    # event, identical run-record bytes (AC-01 exactly-once).
    run_file_after = (runs_dir / "runs" / f"{run.run_id}.json").read_bytes()
    again = engine.reconcile(run.run_id)
    assert again.completed is False
    assert again.transitioned_at is None
    assert log.list_events() == records  # same single record, same sequence
    assert (runs_dir / "runs" / f"{run.run_id}.json").read_bytes() == run_file_after


def test_ac01_completion_after_crash_window_emits_single_event(
    tmp_path: Path,
) -> None:
    """AC-01: when the Run was already moved to RESULT_AVAILABLE but a
    crash cut the event/checkpoint bookkeeping short (no completion
    recorded in the checkpoint, no event in the log), the next reconcile
    appends exactly the one missing event -- the deterministic
    idempotency key converges on a single completion record -- and
    records the progress."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    # The crash window: no event appended, no completion recorded in the
    # checkpoint (a stale non-completion observation only).
    engine.checkpoint_store.save(
        MonitorCheckpoint(
            monitor_id=engine.monitor_id,
            created_at=FIXED_STAMP,
            entries=(
                MonitorRunCheckpoint(
                    run_id=run.run_id,
                    external=run.external,
                    observed_state=EXTERNAL_STATE_RUNNING,
                    observed_at=FIXED_STAMP,
                    reconciled_at=FIXED_STAMP,
                ),
            ),
        )
    )
    assert engine.event_log.list_events() == []

    outcome = engine.reconcile(run.run_id)

    # No transition this pass (the Run is already at RESULT_AVAILABLE),
    # but exactly the one missing completion event is appended and the
    # checkpoint now records the completion.
    assert outcome.completed is False
    assert outcome.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    records = engine.event_log.list_events()
    assert len(records) == 1
    assert records[0].event.event_type == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    assert records[0].event.from_ == LifecycleState.RUNNING_EXTERNAL.value
    assert records[0].event.to == LifecycleState.RESULT_AVAILABLE.value
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert entries[0].reconciled_at == FIXED_STAMP

    # Re-reconciling again never appends a second record (replayed
    # submission returns the single original record).
    engine.reconcile(run.run_id)
    assert engine.event_log.list_events() == records


def test_ac01_already_recorded_completion_is_a_pure_noop(tmp_path: Path) -> None:
    """AC-01: when the Run record, the event log and the checkpoint all
    record the completion, re-reconciling is a pure no-op: the event log
    is never touched again (no re-emission, no sequence advance) and the
    durable bytes stay identical."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    # First reconcile performs the full completion bookkeeping.
    first = engine.reconcile(run.run_id)
    assert first.completed is False
    event_bytes = tree_bytes(events_dir)
    checkpoint_bytes = (state / "checkpoint.json").read_bytes()

    # The steady-state re-poll (probe keeps reporting the completion).
    again = engine.reconcile(run.run_id)
    assert again.completed is False
    assert tree_bytes(events_dir) == event_bytes  # event log untouched
    assert (state / "checkpoint.json").read_bytes() == checkpoint_bytes
    assert len(engine.event_log.list_events()) == 1
    assert engine.event_log.list_events()[0].sequence == 1


# ---------------------------------------------------------------------------
# AC-02: unknown/temporary adapter state never fabricates completion
# ---------------------------------------------------------------------------


def test_ac02_unknown_state_does_not_fabricate_completion(tmp_path: Path) -> None:
    """AC-02: an unknown probe outcome is observed and recorded in the
    checkpoint, never treated as completion: the Run stays
    RUNNING_EXTERNAL and no transition event is emitted."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ScriptedProbe(EXTERNAL_STATE_UNKNOWN)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    outcome = engine.reconcile(run.run_id)

    assert outcome == ReconcileOutcome(
        run_id=run.run_id,
        observed_state=EXTERNAL_STATE_UNKNOWN,
        observed_at=FIXED_STAMP,
        completed=False,
        transitioned_at=None,
    )
    assert probe.calls == [run.external]
    persisted = Run.from_dict(engine.run_store.read("run", run.run_id))
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert engine.event_log.list_events() == []
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == EXTERNAL_STATE_UNKNOWN
    assert entries[0].observed_at == FIXED_STAMP


def test_ac02_temporary_unavailable_state_does_not_fabricate_completion(
    tmp_path: Path,
) -> None:
    """AC-02: a temporarily-unavailable probe outcome (state unavailable)
    is observed and recorded, never treated as completion."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ScriptedProbe(EXTERNAL_STATE_UNAVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    outcome = engine.reconcile(run.run_id)

    assert outcome.observed_state == EXTERNAL_STATE_UNAVAILABLE
    assert outcome.completed is False
    assert Run.from_dict(
        engine.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert engine.event_log.list_events() == []
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == EXTERNAL_STATE_UNAVAILABLE


def test_ac02_probe_transient_failure_does_not_fabricate_completion(
    tmp_path: Path,
) -> None:
    """AC-02: a transient probe failure (an exception from the probe --
    e.g. a backend timeout) is observed as ``TEMPORARY_UNAVAILABLE`` and
    recorded, never treated as completion; the error message itself is
    never persisted (no secrets in durable state)."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    transient = RuntimeError("slurm ssh token expired while polling")
    probe = ScriptedProbe(transient)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    outcome = engine.reconcile(run.run_id)

    assert outcome.observed_state == EXTERNAL_STATE_UNAVAILABLE
    assert outcome.completed is False
    assert Run.from_dict(
        engine.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert engine.event_log.list_events() == []
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == EXTERNAL_STATE_UNAVAILABLE
    # The transient failure message (credential-shaped) never reaches
    # persisted bytes.
    persisted = b"".join(
        p.read_bytes()
        for root in (state, runs_dir, events_dir)
        for p in root.rglob("*")
        if p.is_file()
    )
    lowered = persisted.decode("utf-8", errors="replace").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered
    assert "token expired" not in lowered


def test_ac02_unrecognized_external_state_does_not_fabricate_completion(
    tmp_path: Path,
) -> None:
    """AC-02: only an exact match against the completion signal set can
    complete a run -- an unrecognized backend-specific state (anything
    but ``RESULT_AVAILABLE``) is observed and recorded, never treated as
    completion."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ScriptedProbe("SUBMITTING")
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    outcome = engine.reconcile(run.run_id)

    assert COMPLETION_SIGNALS == frozenset({EXTERNAL_STATE_RESULT_AVAILABLE})
    assert outcome.observed_state == "SUBMITTING"
    assert outcome.completed is False
    assert Run.from_dict(
        engine.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert engine.event_log.list_events() == []
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_state == "SUBMITTING"


# ---------------------------------------------------------------------------
# AC-03: reconciliation is idempotent across restart
# ---------------------------------------------------------------------------


def test_ac03_fresh_engine_reconstructs_progress_and_reconciling_is_idempotent(
    tmp_path: Path,
) -> None:
    """AC-03: a fresh engine over the same state directory, run store
    and event log reconstructs reconciliation progress from the durable
    state alone (watch set from the registry, completion from the Run
    records and the checkpoint, events from the log); re-reconciling the
    same external runs yields identical outcomes and identical durable
    bytes -- no duplicate transitions, no duplicate events."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    completed_run = make_run(1)
    running_run = make_run(2)
    unknown_run = make_run(3)
    runs = (completed_run, running_run, unknown_run)
    # Order-independent external truth: run 1 completes, run 2 keeps
    # running, run 3 is unknown -- whichever order reconciliation
    # traverses the watch set.
    states = {
        completed_run.external.job_id: EXTERNAL_STATE_RESULT_AVAILABLE,
        running_run.external.job_id: EXTERNAL_STATE_RUNNING,
        unknown_run.external.job_id: EXTERNAL_STATE_UNKNOWN,
    }
    engine = make_engine(state, runs_dir, events_dir, probe=MappingProbe(states))
    watch_all(
        engine,
        tuple(make_watch_record(i, external=run.external) for i, run in enumerate(runs, start=1)),
    )
    for run in runs:
        write_run(engine.run_store, run)

    first = engine.reconcile_all()
    assert first.completed_count == 1
    assert [
        o.completed for o in first.outcomes if o.run_id == completed_run.run_id
    ] == [True]
    assert [
        o.completed
        for o in first.outcomes
        if o.run_id in (running_run.run_id, unknown_run.run_id)
    ] == [False, False]
    assert first.monitor_id == engine.monitor_id
    assert first.reconciled_at == FIXED_STAMP

    # Durable state of the first pass.
    first_run_bytes = tree_bytes(runs_dir)
    first_event_bytes = tree_bytes(events_dir)
    first_state_bytes = tree_bytes(state)
    first_checkpoint = (state / "checkpoint.json").read_bytes()
    assert len(engine.event_log.list_events()) == 1

    # A FRESH engine over the same durable state -- no session state --
    # with a probe reporting the same external truth.
    fresh = make_engine(state, runs_dir, events_dir, probe=MappingProbe(states))

    second = fresh.reconcile_all()

    # Identical outcomes, no duplicate transitions or events: the
    # completion run was already recorded, the running/unknown runs stay
    # observed-not-completed.
    assert second.completed_count == 0
    assert [o.completed for o in second.outcomes] == [False, False, False]
    observed_by_run = {o.run_id: o.observed_state for o in second.outcomes}
    assert observed_by_run[completed_run.run_id] == EXTERNAL_STATE_RESULT_AVAILABLE
    assert observed_by_run[running_run.run_id] == EXTERNAL_STATE_RUNNING
    assert observed_by_run[unknown_run.run_id] == EXTERNAL_STATE_UNKNOWN
    # Durable bytes are identical -- reconciliation is idempotent.
    assert tree_bytes(runs_dir) == first_run_bytes
    assert tree_bytes(events_dir) == first_event_bytes
    assert tree_bytes(state) == first_state_bytes
    assert (state / "checkpoint.json").read_bytes() == first_checkpoint
    assert len(engine.event_log.list_events()) == 1
    # The reconstructed progress: the completion run is at
    # RESULT_AVAILABLE, the others still RUNNING_EXTERNAL.
    assert Run.from_dict(
        fresh.run_store.read("run", completed_run.run_id)
    ).lifecycle_state is LifecycleState.RESULT_AVAILABLE
    assert Run.from_dict(
        fresh.run_store.read("run", running_run.run_id)
    ).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert Run.from_dict(
        fresh.run_store.read("run", unknown_run.run_id)
    ).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    entries = load_checkpoint(state)
    assert entries is not None and len(entries) == 3
    observed = {e.run_id: e.observed_state for e in entries}
    assert observed[completed_run.run_id] == EXTERNAL_STATE_RESULT_AVAILABLE
    assert observed[running_run.run_id] == EXTERNAL_STATE_RUNNING
    assert observed[unknown_run.run_id] == EXTERNAL_STATE_UNKNOWN


def test_ac03_byte_identical_durable_state_for_identical_inputs(
    tmp_path: Path,
) -> None:
    """AC-03: identical injected inputs produce byte-identical durable
    state -- Run records, watch entries, checkpoint and event log
    (canonical sorted JSON, fixed clock, deterministic ids) -- no
    randomness, no wall clock."""
    payloads: list[dict[str, list[tuple[str, bytes]]]] = []
    # The same injected monitor identity for both variants: the durable
    # bytes of the checkpoint are then byte-comparable.
    monitor_id = generate_id("monitor", "identical")
    for variant in ("a", "b"):
        state, runs_dir, events_dir = (
            tmp_path / variant / "state",
            tmp_path / variant / "runs",
            tmp_path / variant / "events",
        )
        run = make_run(1)
        probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
        engine = make_engine(
            state, runs_dir, events_dir, probe=probe, monitor_id=monitor_id
        )
        watch_all(engine, (make_watch_record(1, external=run.external),))
        write_run(engine.run_store, run)
        first = engine.reconcile(run.run_id)
        assert first.completed is True
        engine.reconcile(run.run_id)  # the steady-state completion re-poll
        payloads.append(
            {
                "runs": tree_bytes(runs_dir),
                "events": tree_bytes(events_dir),
                "state": tree_bytes(state),
            }
        )
    assert payloads[0] == payloads[1]


def test_ac03_empty_watch_set_yields_empty_summary(tmp_path: Path) -> None:
    """AC-03: reconciling an empty watch set (fresh or empty state
    directory) is a deterministic empty pass."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    engine = make_engine(state, runs_dir, events_dir)
    summary = engine.reconcile_all()
    assert summary == ReconcileSummary(
        monitor_id=engine.monitor_id,
        reconciled_at=FIXED_STAMP,
        outcomes=(),
        completed_count=0,
    )
    assert load_checkpoint(state) is None


# ---------------------------------------------------------------------------
# The reconciliation contracts
# ---------------------------------------------------------------------------


def test_reconcile_unwatched_run_raises_watch_not_found(tmp_path: Path) -> None:
    """Reconciling a run that is not watched raises the stable
    WatchNotFoundError."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    engine = make_engine(state, runs_dir, events_dir)
    with pytest.raises(WatchNotFoundError):
        engine.reconcile(make_run_id(1))


def test_reconcile_missing_run_record_raises_corrupt_progress_error(
    tmp_path: Path,
) -> None:
    """A watch entry referencing a run with no run record in the run
    store is corrupt progress: it fails loudly with the stable
    CorruptProgressError, never a silent skip."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    # No write_run: the run record is missing.
    with pytest.raises(CorruptProgressError):
        engine.reconcile(run.run_id)


def test_reconcile_corrupt_run_record_raises_corrupt_progress_error(
    tmp_path: Path,
) -> None:
    """A corrupt run record on disk fails reconciliation loudly with the
    stable CorruptProgressError."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    path = runs_dir / "runs" / f"{run.run_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CorruptProgressError):
        engine.reconcile(run.run_id)


def test_reconcile_completion_for_pre_external_run_raises_contract_error(
    tmp_path: Path,
) -> None:
    """AC-02: a completion signal for a run whose lifecycle is still
    pre-external (never handed off) is a contract violation: it raises
    the stable ReconcileContractError and nothing is fabricated -- the
    run stays in its state and no event is emitted."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.DISPATCHED)
    probe = ScriptedProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    with pytest.raises(ReconcileContractError):
        engine.reconcile(run.run_id)

    assert Run.from_dict(
        engine.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.DISPATCHED
    assert engine.event_log.list_events() == []


def test_reconcile_completion_for_cancelled_run_raises_contract_error(
    tmp_path: Path,
) -> None:
    """AC-02: a completion signal for a cancelled run (terminal, no
    results) is a contract violation: stable ReconcileContractError, run
    unchanged, no event."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.CANCELLED)
    probe = ScriptedProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)

    with pytest.raises(ReconcileContractError):
        engine.reconcile(run.run_id)

    assert Run.from_dict(
        engine.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.CANCELLED
    assert engine.event_log.list_events() == []


def test_reconcile_external_identity_mismatch_raises_contract_error(
    tmp_path: Path,
) -> None:
    """A Run record whose external identity disagrees with its watch
    entry is a contract violation (the monitor would poll the wrong
    external run): stable ReconcileContractError before any transition
    or event."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)  # watch entry matches this identity
    mismatched = make_run(1, external=make_external(
        backend="slurm_ssh",
        job_id=generate_id("job", "some-other-run"),
        working_directory="/home/alice/scratch/other",
    ))
    probe = ScriptedProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, mismatched)

    with pytest.raises(ReconcileContractError):
        engine.reconcile(run.run_id)

    assert engine.event_log.list_events() == []


def test_reconcile_probe_receives_the_watch_external_identity(
    tmp_path: Path,
) -> None:
    """The probe is polled with the watch entry's external identity --
    the durable identity the monitor reconciles under."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ScriptedProbe(EXTERNAL_STATE_RUNNING)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    engine.reconcile(run.run_id)
    assert probe.calls == [run.external]


# ---------------------------------------------------------------------------
# Determinism, secrets, default configuration, boundaries
# ---------------------------------------------------------------------------


def test_reconcile_uses_injected_clock(tmp_path: Path) -> None:
    """Every stamped value (observation, transition, checkpoint) comes
    from the injected clock -- no wall clock anywhere."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    clock = FakeClock(FIXED_STAMP)
    probe = ScriptedProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe, clock=clock)
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    outcome = engine.reconcile(run.run_id)
    assert outcome.observed_at == FIXED_STAMP
    assert outcome.transitioned_at == FIXED_STAMP
    assert engine.event_log.list_events()[0].event.timestamp == FIXED_STAMP
    entries = load_checkpoint(state)
    assert entries and entries[0].observed_at == FIXED_STAMP
    assert entries[0].reconciled_at == FIXED_STAMP
    assert clock.calls, "the engine must consult the injected clock"


def test_reconcile_default_probe_never_fabricates_completion(
    tmp_path: Path,
) -> None:
    """AC-02: with no probe injected, the engine observes everything as
    unknown -- the default configuration can never fabricate a
    completion."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    engine = make_engine(state, runs_dir, events_dir)  # no probe
    watch_all(engine, (make_watch_record(1, external=run.external),))
    write_run(engine.run_store, run)
    outcome = engine.reconcile(run.run_id)
    assert outcome.observed_state == EXTERNAL_STATE_UNKNOWN
    assert outcome.completed is False
    assert Run.from_dict(
        engine.run_store.read("run", run.run_id)
    ).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert engine.event_log.list_events() == []


def test_reconcile_default_store_layout_over_state_dir(tmp_path: Path) -> None:
    """The default run store and event log derive from the state
    directory: runs at ``<state_dir>/run/``, events at
    ``<state_dir>/event/``."""
    state = tmp_path / "state"
    run = make_run(1)
    probe = ScriptedProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = ReconcileEngine(state, now=FakeClock(), probe=probe)
    FilesystemStateBackend(state).write("run", run.run_id, run.to_dict())
    engine.registry.watch(make_watch_record(1, external=run.external))
    outcome = engine.reconcile(run.run_id)
    assert outcome.completed is True
    assert (state / "runs" / f"{run.run_id}.json").is_file()
    assert event_records(state)  # events at <state_dir>/event/


def test_reconcile_persisted_state_never_carries_credentials(
    tmp_path: Path,
) -> None:
    """The no-secrets discipline: after a full reconciliation scenario
    (including a transient probe failure with a credential-shaped
    message), no persisted byte anywhere carries credential-shaped
    content."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    completed = make_run(1)
    transient = make_run(2)
    probe = ScriptedProbe(
        EXTERNAL_STATE_RESULT_AVAILABLE,
        RuntimeError("slurm ssh api_key authentication failed"),
        EXTERNAL_STATE_RUNNING,
    )
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    watch_all(
        engine,
        (
            make_watch_record(1, external=completed.external),
            make_watch_record(2, external=transient.external),
        ),
    )
    for run in (completed, transient):
        write_run(engine.run_store, run)
    engine.reconcile_all()

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
    assert "authentication failed" not in lowered


def test_reconcile_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public type boundaries."""
    with pytest.raises(TypeError):
        ReconcileEngine(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReconcileEngine(tmp_path / "s", now="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReconcileEngine(tmp_path / "s", probe="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReconcileEngine(
            tmp_path / "s", run_store="not a backend"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        ReconcileEngine(
            tmp_path / "s", event_log="not a log"  # type: ignore[arg-type]
        )
    engine = make_engine(tmp_path / "s", tmp_path / "r", tmp_path / "e")
    with pytest.raises(TypeError):
        engine.reconcile(42)  # type: ignore[arg-type]


def test_reconcile_error_hierarchy_is_value_error_based() -> None:
    """The reconcile error hierarchy is ValueError-based with stable
    subclasses (the house paradigm for durable-state errors)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(ReconcileError, MonitoringError)
    assert issubclass(ReconcileContractError, ReconcileError)
    assert issubclass(CorruptProgressError, ReconcileError)
    assert ReconcileContractError is not CorruptProgressError


def test_reconcile_module_does_not_couple_to_adapters() -> None:
    """Importing the reconciliation primitive never pulls in the
    adapters package (proven in a fresh interpreter): the external state
    is a plain injected string vocabulary."""
    code = (
        "import sys\n"
        "import scientific_reproduction.monitoring.reconcile\n"
        "assert 'scientific_reproduction.adapters' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
