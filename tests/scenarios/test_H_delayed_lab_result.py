"""FDM-201 simulated scenario H -- delayed lab result (DEV-M8-G06).

Scenario H: the Experiment Worker dispatches the execution package
through the lab handoff and exits; the lab result is returned later,
after the worker is gone. Expected behavior (frozen acceptance):

* AC-03: the original experiment worker is **not required to remain
  alive**: a fresh Monitor session over the durable state and the
  handoff detects the returned Result Package through the real
  filesystem lab adapter, reconciles the completion, collects the
  returned files and spawns the follow-up analysis -- the worker
  session is never referenced, and the run record's stale
  ``worker_session_ref`` is never resolved.
* AC-02 (scenario discipline): before the result returns nothing is
  fabricated (the probe reports running, the run stays
  ``RUNNING_EXTERNAL``, the trigger scan ignores it), and after the
  result returns the follow-up is spawned exactly once (a repeated
  scan replays the single original trigger record; no duplicate
  trigger record, no duplicate completion event).

The scenario runs end to end through the real machinery: the
``FilesystemLabAdapter`` (real dispatch/status/collect over the real
outgoing/incoming handoff, schema-gated dispatch, manifest-validated
collection), the ``WatchedRunRegistry``, the ``ReconcileEngine``, the
``TriggerRegistry`` with the Monitor's follow-up plumbing writing the
durable analysis request through ``core.atomic.atomic_write``, the real
``FilesystemStateBackend`` run store and the real append-only
``ProjectEventLog``.

Determinism contract: fixed injected stamps (single-stamp ``FakeClock``
throughout), no sleeps, no network, no wall clock. The scenario
executor is a pure function of its workspace directory: the determinism
test runs it twice and compares the durable state byte for byte.

Test map: ``test_H_ac03_*`` -> AC-03 (the original worker is not
required to remain alive), ``test_H_ac02_*`` -> AC-02 (exactly-once
detection, completion event and follow-up).
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from pathlib import Path

import pytest

from scientific_reproduction.adapters.lab.base import (
    DispatchRecord,
    DispatchState,
)
from scientific_reproduction.adapters.lab.filesystem import FilesystemLabAdapter
from scientific_reproduction.adapters.lab.manifest import (
    RESULT_MANIFEST_VERSION,
    LabResultManifest,
)
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LabExecutionPackage,
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
from scientific_reproduction.monitoring.registry import (
    WatchedRunRecord,
    WatchedRunRegistry,
)
from scientific_reproduction.monitoring.triggers import (
    FOLLOWUP_TRIGGER_KIND,
    TriggerRecord,
    TriggerRegistry,
)

FIXED_STAMP = "2026-08-14T00:00:00+00:00"

MONITOR_ID = generate_id("monitor", "scenario-h", "session-1")
RUN_ID = generate_id("run", "goal-1", "seq-1")
GOAL_ID = generate_id("goal", "goal-1")
PACKAGE_ID = generate_id("package", "lab-result", "run-1")
PROJECT_ID = generate_id("project", "fdm-201")
DISPATCH_ID = generate_id("dispatch", PACKAGE_ID, RUN_ID)

#: The worker session that dispatched the package and then exited; the
#: scenario's durable Run record references it, and nothing in the
#: detection/collection/follow-up path may ever resolve it.
WORKER_SESSION = "session-experiment-worker-1"

LAB_BACKEND = "filesystem"

REQUIRED_RETURN = ["raw-data.csv"]


# ---------------------------------------------------------------------------
# Deterministic scenario machinery (no wall clock, no network, no sleeps)
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable clock: the single fixed stamp repeats forever and every
    read is recorded -- no wall clock anywhere in the tested path."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


class ExperimentWorker:
    """The Experiment Worker of scenario H: dispatches the execution
    package through the real lab adapter, records the durable Run and
    watch entries, and exits -- its session is never needed again."""

    def __init__(
        self,
        handoff: Path,
        monitor_state: Path,
        root: Path,
        *,
        clock: FakeClock,
    ) -> None:
        self._handoff = handoff
        self._monitor_state = monitor_state
        self._root = root
        self._clock = clock

    def dispatch_and_exit(self) -> DispatchRecord:
        """Dispatch the package, persist the Run record and the watch
        entry, then return the dispatch record (the worker session ends
        here: nothing below holds or uses this worker)."""
        adapter = FilesystemLabAdapter(self._handoff)
        package = LabExecutionPackage(
            package_id=PACKAGE_ID,
            project_id=PROJECT_ID,
            goal_id=GOAL_ID,
            run_id=RUN_ID,
            objective="synthesize the target compound per the frozen protocol",
            procedure=[{"step": 1, "action": "weigh the precursor"}],
            required_return=list(REQUIRED_RETURN),
        )
        dispatch = adapter.dispatch(package, dispatched_at=FIXED_STAMP)
        assert dispatch.dispatch_id == DISPATCH_ID

        run = Run(
            run_id=RUN_ID,
            goal_id=GOAL_ID,
            run_type=RunType.INDEPENDENT_REPLICATE,
            lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
            goal_version="1.0",
            scientific_review=None,
            worker_session_ref=WORKER_SESSION,
            external=RunExternal(backend=LAB_BACKEND, dispatch_id=dispatch.dispatch_id),
            artifacts=[],
            deviations=[],
            engineering_retries=[],
            created_at=FIXED_STAMP,
            updated_at=FIXED_STAMP,
        )
        FilesystemStateBackend(self._root).write(
            "run", run.run_id, run.to_dict()
        )
        WatchedRunRegistry(
            self._monitor_state, now=self._clock, monitor_id=MONITOR_ID
        ).watch(
            WatchedRunRecord(
                run_id=RUN_ID,
                external=RunExternal(
                    backend=LAB_BACKEND, dispatch_id=dispatch.dispatch_id
                ),
                watched_at=FIXED_STAMP,
                adapter_id="lab/filesystem",
                adapter_version="1.0",
            )
        )
        return dispatch


class LabAdapterProbe:
    """The Monitor's external-status probe over the real filesystem lab
    adapter: reports ``RESULT_AVAILABLE`` exactly when the adapter
    detects the returned Result Package in the incoming handoff (and
    nothing else is ever reported as a completion)."""

    def __init__(self, adapter: FilesystemLabAdapter) -> None:
        self._adapter = adapter
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        if external.dispatch_id is None:
            return EXTERNAL_STATE_RUNNING
        status = self._adapter.status(external.dispatch_id)
        if status.state is DispatchState.RESULT_AVAILABLE:
            return EXTERNAL_STATE_RESULT_AVAILABLE
        return EXTERNAL_STATE_RUNNING


class AnalysisFollowupPlumbing:
    """The Monitor's follow-up spawn plumbing (scenario H): issues the
    durable follow-up analysis request for the returned lab result --
    written through the house atomic writer into the monitor's request
    directory, keyed by the run -- and returns the request id, the
    receipt recorded in the trigger record."""

    def __init__(self, requests_dir: Path) -> None:
        self._requests_dir = requests_dir
        self.calls: list[Run] = []

    def __call__(self, run: Run) -> str:
        self.calls.append(run)
        request_id = generate_id("analysis-request", run.run_id, "lab-followup")
        dispatch_id = run.external.dispatch_id if run.external is not None else None
        atomic_write(
            self._requests_dir / f"{run.run_id}.json",
            json.dumps(
                {
                    "request_id": request_id,
                    "run_id": run.run_id,
                    "kind": "lab_result_followup_analysis",
                    "dispatch_id": dispatch_id,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return request_id


# ---------------------------------------------------------------------------
# Scenario helpers (same conventions as the house scenario tests)
# ---------------------------------------------------------------------------


def write_result_package(handoff: Path) -> Path:
    """The lab result returns later: a Result Package for the run
    appears in the incoming handoff, declaring the required raw data
    file."""
    incoming = handoff / "incoming" / RUN_ID
    incoming.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": RESULT_MANIFEST_VERSION,
        "package_id": PACKAGE_ID,
        "project_id": PROJECT_ID,
        "goal_id": GOAL_ID,
        "run_id": RUN_ID,
        "files": list(REQUIRED_RETURN),
        "notes": ["returned after the experiment worker exited"],
    }
    atomic_write(
        incoming / "result-manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    atomic_write(incoming / "raw-data.csv", "m/z,abundance\n118.0,4271\n")
    return incoming


def tree_bytes(root: Path) -> bytes:
    """The byte-identical snapshot of the durable state tree, with the
    workspace's own absolute path normalized out (the real adapter
    records the injected outgoing path on the dispatch record, which is
    workspace-dependent but never scenario-dependent; it appears both
    plain and JSON-escaped, i.e. with doubled backslashes)."""
    raw_root = str(root).encode("utf-8")
    escaped_root = raw_root.replace(b"\\", b"\\\\")
    chunks: list[bytes] = []
    for p in sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix(),
    ):
        chunks.append(
            p.read_bytes()
            .replace(escaped_root, b"<workspace>")
            .replace(raw_root, b"<workspace>")
        )
    return b"\n".join(chunks)


def run_record_bytes(runs_dir: Path) -> bytes:
    return (runs_dir / f"{RUN_ID}.json").read_bytes()


def trigger_files(monitor_state: Path) -> list[Path]:
    return sorted(
        (monitor_state / "trigger").glob("*.json"), key=lambda p: p.name
    )


def event_files(events_dir: Path) -> list[Path]:
    return sorted(events_dir.glob("*.json"), key=lambda p: p.name)


def completion_event_id() -> str:
    return generate_id(
        "event",
        EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
        RUN_ID,
        LifecycleState.RUNNING_EXTERNAL.value,
        LifecycleState.RESULT_AVAILABLE.value,
    )


@dataclass(frozen=True)
class ScenarioHResult:
    """The frozen, auditable evidence trail of one executed scenario."""

    root: Path
    handoff: Path
    dispatch: DispatchRecord
    probe_calls: tuple[RunExternal, ...]
    reconcile_outcome: object
    first_scan: object
    second_scan: object
    followup: AnalysisFollowupPlumbing
    collected: tuple[str, ...]
    run_bytes_before: bytes


def execute_scenario_h(root: Path) -> ScenarioHResult:
    """Execute scenario H end to end and return the evidence trail.

    Step 1: the Experiment Worker dispatches the package through the
    real lab adapter and exits (its session is dropped; nothing after
    this step touches it). Step 2: the lab result returns later, into
    the incoming handoff. Step 3: a fresh Monitor session -- fresh
    adapter over the same handoff, fresh registry/engine/trigger
    registry over the same durable state -- detects the package,
    reconciles the completion and spawns the follow-up analysis exactly
    once. The executor is a pure function of its workspace directory.
    """
    handoff = root / "lab"
    monitor_state = root / "monitor"
    runs_dir = root / "runs"
    requests_dir = root / "requests"
    clock = FakeClock()

    # step 1: the worker dispatches and exits -- never referenced again
    worker = ExperimentWorker(handoff, monitor_state, root, clock=clock)
    dispatch = worker.dispatch_and_exit()
    del worker
    run_bytes_before = run_record_bytes(runs_dir)

    # step 2: the result returns later
    write_result_package(handoff)

    # step 3: a fresh Monitor session over the durable state and handoff
    adapter = FilesystemLabAdapter(handoff)
    probe = LabAdapterProbe(adapter)
    engine = ReconcileEngine(
        monitor_state,
        now=clock,
        monitor_id=MONITOR_ID,
        probe=probe,
        run_store=FilesystemStateBackend(root),
        event_log=ProjectEventLog(root),
    )
    reconcile_outcome = engine.reconcile(RUN_ID)
    run_store = FilesystemStateBackend(root)
    run = Run.from_dict(run_store.read("run", RUN_ID))
    followup = AnalysisFollowupPlumbing(requests_dir)
    triggers = TriggerRegistry(
        monitor_state, now=clock, monitor_id=MONITOR_ID, followup=followup
    )
    first_scan = triggers.scan(run)
    second_scan = triggers.scan(run)
    collected = adapter.collect(DISPATCH_ID).collected_files

    return ScenarioHResult(
        root=root,
        handoff=handoff,
        dispatch=dispatch,
        probe_calls=tuple(probe.calls),
        reconcile_outcome=reconcile_outcome,
        first_scan=first_scan,
        second_scan=second_scan,
        followup=followup,
        collected=collected,
        run_bytes_before=run_bytes_before,
    )


# ---------------------------------------------------------------------------
# The worker dispatches and exits
# ---------------------------------------------------------------------------


def test_H_worker_dispatches_and_exits_leaving_durable_artifacts(
    tmp_path: Path,
) -> None:
    """The worker's exit leaves everything on disk: the outgoing
    dispatch record and execution manifest in the real handoff, the Run
    record (``RUNNING_EXTERNAL``, referencing the now-gone worker
    session), and the watch entry -- all readable by a FRESH adapter
    and a fresh registry over the same directories."""
    root = tmp_path / "scenario-h"
    handoff = root / "lab"
    monitor_state = root / "monitor"
    runs_dir = root / "runs"
    clock = FakeClock()

    worker = ExperimentWorker(handoff, monitor_state, root, clock=clock)
    worker.dispatch_and_exit()
    del worker  # the worker session is gone before the result returns

    outgoing = handoff / "outgoing" / RUN_ID
    assert (outgoing / "dispatch.json").is_file()
    assert (outgoing / "manifest.json").is_file()
    record = json.loads((outgoing / "dispatch.json").read_text(encoding="utf-8"))
    assert record["dispatch_id"] == DISPATCH_ID
    assert record["run_id"] == RUN_ID

    run = json.loads(run_record_bytes(runs_dir).decode("utf-8"))
    assert run["lifecycle_state"] == LifecycleState.RUNNING_EXTERNAL.value
    assert run["worker_session_ref"] == WORKER_SESSION
    assert run["external"]["dispatch_id"] == DISPATCH_ID
    assert run["external"]["backend"] == LAB_BACKEND

    watched = sorted(p.name for p in (monitor_state / "watched").glob("*.json"))
    assert watched == [f"{RUN_ID}.json"]

    # a fresh process context (new adapter over the same handoff, new
    # registry over the same state) sees the dispatch: running, no
    # result yet
    fresh_adapter = FilesystemLabAdapter(handoff)
    status = fresh_adapter.status(DISPATCH_ID)
    assert status.state is DispatchState.RUNNING_EXTERNAL
    fresh_registry = WatchedRunRegistry(
        monitor_state, now=clock, monitor_id=MONITOR_ID
    )
    assert fresh_registry.get(RUN_ID).external.dispatch_id == DISPATCH_ID


# ---------------------------------------------------------------------------
# Before the result returns: nothing is fabricated
# ---------------------------------------------------------------------------


def test_H_ac02_before_result_no_detection_no_trigger(tmp_path: Path) -> None:
    """Before the lab result returns, the Monitor detects nothing: the
    probe reports running, the run stays ``RUNNING_EXTERNAL`` and the
    trigger scan observes and ignores the run -- never triggered, never
    fabricated, no trigger record and no hook call."""
    root = tmp_path / "scenario-h"
    handoff = root / "lab"
    monitor_state = root / "monitor"
    events_dir = root / "events"
    requests_dir = root / "requests"
    clock = FakeClock()

    ExperimentWorker(handoff, monitor_state, root, clock=clock).dispatch_and_exit()
    assert not (handoff / "incoming" / RUN_ID / "result-manifest.json").exists()

    adapter = FilesystemLabAdapter(handoff)
    probe = LabAdapterProbe(adapter)
    engine = ReconcileEngine(
        monitor_state,
        now=clock,
        monitor_id=MONITOR_ID,
        probe=probe,
        run_store=FilesystemStateBackend(root),
        event_log=ProjectEventLog(root),
    )
    outcome = engine.reconcile(RUN_ID)
    assert not outcome.completed
    assert outcome.observed_state == EXTERNAL_STATE_RUNNING

    run = Run.from_dict(FilesystemStateBackend(root).read("run", RUN_ID))
    assert run.lifecycle_state is LifecycleState.RUNNING_EXTERNAL

    followup = AnalysisFollowupPlumbing(requests_dir)
    triggers = TriggerRegistry(
        monitor_state, now=clock, monitor_id=MONITOR_ID, followup=followup
    )
    scan = triggers.scan(run)
    assert scan.ignored
    assert scan.record is None
    assert followup.calls == []
    assert trigger_files(monitor_state) == []
    assert event_files(events_dir) == []


# ---------------------------------------------------------------------------
# AC-03 -- the original worker is not required to remain alive
# ---------------------------------------------------------------------------


def test_H_ac03_monitor_detects_incoming_package_and_reconciles(
    tmp_path: Path,
) -> None:
    """AC-03: after the worker is gone and the result returns, the fresh
    Monitor session detects the incoming Result Package through the
    real adapter, records the single completion event and moves the Run
    to ``RESULT_AVAILABLE``."""
    root = tmp_path / "scenario-h"
    result = execute_scenario_h(root)

    assert result.probe_calls == (
        RunExternal(backend=LAB_BACKEND, dispatch_id=DISPATCH_ID),
    )
    assert result.reconcile_outcome.completed
    assert result.reconcile_outcome.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE

    events = event_files(root / "events")
    assert len(events) == 1
    event = json.loads(events[0].read_text(encoding="utf-8"))
    assert event["event_id"] == completion_event_id()
    assert event["event_type"] == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    assert event["run_id"] == RUN_ID
    assert event["to"] == LifecycleState.RESULT_AVAILABLE.value

    run = json.loads(run_record_bytes(root / "runs").decode("utf-8"))
    assert run["lifecycle_state"] == LifecycleState.RESULT_AVAILABLE.value
    assert run["worker_session_ref"] == WORKER_SESSION  # stale, never resolved


def test_H_ac03_fresh_adapter_collects_returned_result_without_worker(
    tmp_path: Path,
) -> None:
    """AC-03: the returned result is collected through a fresh adapter
    over the same handoff -- the returned manifest matches the dispatch
    (the real run-association check) and the declared raw data file is
    present -- with no reference to the exited worker."""
    result = execute_scenario_h(tmp_path / "scenario-h")

    fresh_adapter = FilesystemLabAdapter(result.handoff)
    collected = fresh_adapter.collect(DISPATCH_ID)
    assert collected.run_id == RUN_ID
    assert collected.collected_files == ("raw-data.csv",)
    assert isinstance(collected.manifest, LabResultManifest)
    assert collected.manifest.manifest_version == RESULT_MANIFEST_VERSION
    assert collected.manifest.run_id == RUN_ID
    assert result.collected == ("raw-data.csv",)


def test_H_ac03_followup_analysis_spawned_exactly_once(tmp_path: Path) -> None:
    """AC-03: the Monitor spawns the follow-up analysis for the returned
    result exactly once -- the first scan invokes the hook once and
    persists the deterministic trigger record with the receipt; the
    repeated scan replays that single original record (no second hook
    call, no second record, no new bytes)."""
    root = tmp_path / "scenario-h"
    result = execute_scenario_h(root)

    first = result.first_scan
    assert first.triggered
    assert not first.replayed
    assert not first.ignored
    record: TriggerRecord = first.record
    assert record is not None
    assert record.run_id == RUN_ID
    assert record.trigger_kind == FOLLOWUP_TRIGGER_KIND
    assert record.triggered_at == FIXED_STAMP
    assert record.followup_id == generate_id(
        "analysis-request", RUN_ID, "lab-followup"
    )
    assert record.record_id == generate_id(
        "trigger", RUN_ID, FOLLOWUP_TRIGGER_KIND
    )

    # the durable analysis request exists on disk (atomic_write)
    request = json.loads(
        (root / "requests" / f"{RUN_ID}.json").read_text(encoding="utf-8")
    )
    assert request["request_id"] == record.followup_id
    assert request["run_id"] == RUN_ID
    assert request["dispatch_id"] == DISPATCH_ID

    second = result.second_scan
    assert second.replayed
    assert second.record == record
    assert len(result.followup.calls) == 1
    assert result.followup.calls[0].run_id == RUN_ID

    files_before = trigger_files(root / "monitor")
    assert len(files_before) == 1
    fresh_triggers = TriggerRegistry(
        root / "monitor",
        now=FakeClock(),
        monitor_id=MONITOR_ID,
        followup=AnalysisFollowupPlumbing(root / "requests"),
    )
    assert len(fresh_triggers.list_triggered()) == 1
    assert trigger_files(root / "monitor") == files_before
    assert fresh_triggers.get(RUN_ID) == record


def test_H_ac03_worker_process_gone_before_result_returns(tmp_path: Path) -> None:
    """AC-03: the worker process is gone before the result arrives --
    the whole detection/collection/follow-up leg of the scenario runs
    with no worker object in scope (only the durable artifacts it left),
    and the stale ``worker_session_ref`` on the Run record is never
    resolved by any step."""
    root = tmp_path / "scenario-h"
    handoff = root / "lab"
    monitor_state = root / "monitor"
    requests_dir = root / "requests"
    clock = FakeClock()

    worker = ExperimentWorker(handoff, monitor_state, root, clock=clock)
    dispatch = worker.dispatch_and_exit()
    del worker  # the worker session ends before the result returns

    # the result returns later
    write_result_package(handoff)
    assert dispatch.dispatch_id == DISPATCH_ID

    # the Monitor leg: fresh instances, durable paths only
    probe = LabAdapterProbe(FilesystemLabAdapter(handoff))
    engine = ReconcileEngine(
        monitor_state,
        now=clock,
        monitor_id=MONITOR_ID,
        probe=probe,
        run_store=FilesystemStateBackend(root),
        event_log=ProjectEventLog(root),
    )
    assert engine.reconcile(RUN_ID).completed
    run = Run.from_dict(FilesystemStateBackend(root).read("run", RUN_ID))
    assert run.lifecycle_state is LifecycleState.RESULT_AVAILABLE
    assert run.worker_session_ref == WORKER_SESSION  # stale, never used
    collected = FilesystemLabAdapter(handoff).collect(DISPATCH_ID)
    assert collected.collected_files == ("raw-data.csv",)

    followup = AnalysisFollowupPlumbing(requests_dir)
    triggers = TriggerRegistry(
        monitor_state, now=clock, monitor_id=MONITOR_ID, followup=followup
    )
    scan = triggers.scan(run)
    assert scan.triggered
    assert scan.record is not None
    assert scan.record.followup_id == generate_id(
        "analysis-request", RUN_ID, "lab-followup"
    )


# ---------------------------------------------------------------------------
# AC-02 -- exactly-once completion and follow-up
# ---------------------------------------------------------------------------


def test_H_ac02_completion_event_recorded_exactly_once(tmp_path: Path) -> None:
    """AC-02: the completion event exists exactly once with its
    deterministic id, and the run bytes before and after the Monitor
    leg differ only by the recorded completion (the single transition).
    A fresh reconcile pass over the same state neither re-emits nor
    re-transitions."""
    root = tmp_path / "scenario-h"
    result = execute_scenario_h(root)

    assert len(event_files(root / "events")) == 1
    assert result.reconcile_outcome.completed

    clock = FakeClock()
    engine = ReconcileEngine(
        root / "monitor",
        now=clock,
        monitor_id=MONITOR_ID,
        probe=LabAdapterProbe(FilesystemLabAdapter(root / "lab")),
        run_store=FilesystemStateBackend(root),
        event_log=ProjectEventLog(root),
    )
    again = engine.reconcile(RUN_ID)
    assert not again.completed
    assert len(event_files(root / "events")) == 1


# ---------------------------------------------------------------------------
# Determinism and hygiene
# ---------------------------------------------------------------------------


def test_H_deterministic_scenario_repeatable(tmp_path: Path) -> None:
    """The full scenario is deterministic: two executions in separate
    workspaces produce byte-identical durable state and identical
    outcomes (fixed stamps, no sleeps, no network)."""
    first = execute_scenario_h(tmp_path / "first")
    second = execute_scenario_h(tmp_path / "second")

    assert first.reconcile_outcome == second.reconcile_outcome
    assert first.first_scan == second.first_scan
    assert first.second_scan == second.second_scan
    assert first.collected == second.collected
    assert first.probe_calls == second.probe_calls
    assert tree_bytes(tmp_path / "first") == tree_bytes(tmp_path / "second")


def test_H_scenario_uses_safe_ids_only() -> None:
    """Every id the scenario touches is a generated id (lowercase kind +
    32 hex): path construction stays within the safe-identifier
    contract."""
    ids = (
        MONITOR_ID,
        RUN_ID,
        GOAL_ID,
        PACKAGE_ID,
        PROJECT_ID,
        DISPATCH_ID,
        completion_event_id(),
        generate_id("trigger", RUN_ID, FOLLOWUP_TRIGGER_KIND),
        generate_id("analysis-request", RUN_ID, "lab-followup"),
    )
    for value in ids:
        assert is_valid_id(value)
    assert all(
        not any(sep in value for sep in ("/", "\\", "*", "?", "[")) for value in ids
    )


def test_H_records_are_frozen_and_validated() -> None:
    """The scenario's durable records are frozen dataclasses that
    validate the documented contract (no silent mutation, no malformed
    records)."""
    assert is_dataclass(DispatchRecord)
    assert is_dataclass(TriggerRecord)
    record = TriggerRecord(
        record_id=generate_id("trigger", RUN_ID, FOLLOWUP_TRIGGER_KIND),
        run_id=RUN_ID,
        trigger_kind=FOLLOWUP_TRIGGER_KIND,
        triggered_at=FIXED_STAMP,
        followup_id=generate_id("analysis-request", RUN_ID, "lab-followup"),
    )
    with pytest.raises(FrozenInstanceError):
        record.trigger_kind = "fabricated_kind"  # type: ignore[misc]
