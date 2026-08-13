"""FDM-201 simulated scenario G -- Monitor crash and recovery (DEV-M8-G06).

Scenario G: the execution Monitor session disappears while jobs run on
the cluster; the watchdog tries to resume the session and the resume
fails (simulated); a replacement Monitor session reconstructs its view
from the durable state alone -- the watch entries, the checkpoint, the
event log and the external slurm-ssh adapter -- and resumes the
reconciliation loop. Expected behavior (frozen acceptance):

* AC-02: the replacement loses **no Run, no job and no completion
  event**: exactly one Run record per run, exactly one adapter job
  record per run, exactly one completion event per run (sequences
  never reused), including the completion that occurred while the
  original session was down.
* AC-03: reconstruction is observation-only (nothing is written, the
  dispatch hook is never invoked) and the resumed first pass performs
  the missing completion exactly once; every later pass is a
  byte-identical steady-state no-op.

The scenario runs end to end through the real machinery: the
``SSHComputeAdapter`` over the scripted Slurm cluster transport double,
the ``WatchedRunRegistry``, the ``ReconcileEngine`` of the original
session, the ``MonitorRecovery`` replacement procedure and the resumed
engine, the real ``FilesystemStateBackend`` run store and the real
append-only ``ProjectEventLog`` -- all over one deterministic workspace.

Determinism contract: fixed injected stamps (single-stamp ``FakeClock``
throughout), zero-delay retry backoff (never a sleep), and a scripted
in-memory cluster (no network). The scenario executor is a pure function
of its workspace directory: the determinism test runs it twice and
compares the durable state byte for byte.

Test map: ``test_G_ac02_*`` -> AC-02 (no Run/job/completion event lost
across the replacement), ``test_G_ac03_*`` -> AC-03 (observation-only
reconstruction, exactly-once resumed completion).
"""

from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from pathlib import Path

import pytest

from scientific_reproduction.adapters.compute.local import (
    JOBS_STATE_DIR,
    JobState,
    RunContext,
)
from scientific_reproduction.adapters.compute.ssh import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    SSH_BACKEND_NAME,
    RemoteCommand,
    RemotePath,
    RemoteResult,
    SSHComputeAdapter,
    SSHConnectionError,
    SSHCredentials,
    SSHRetryPolicy,
    SSHTransport,
)
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring.reconcile import (
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATE_RUNNING,
    EXTERNAL_STATE_UNKNOWN,
    EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
    ReconcileEngine,
    ReconcileOutcome,
    ReconcileSummary,
)
from scientific_reproduction.monitoring.recovery import (
    MonitorRecovery,
    RecoveredCompletion,
    RecoveryError,
    RecoveryPlan,
)
from scientific_reproduction.monitoring.registry import (
    WatchedRunRecord,
    WatchedRunRegistry,
)

FIXED_STAMP = "2026-08-14T00:00:00+00:00"

MONITOR_ID = generate_id("monitor", "scenario-g", "session-1")
RUN_1 = generate_id("run", "goal-1", "seq-1")
RUN_2 = generate_id("run", "goal-1", "seq-2")
GOAL_ID = generate_id("goal", "goal-1")
JOB_1 = generate_id("job", RUN_1)
JOB_2 = generate_id("job", RUN_2)

DEFAULT_CREDENTIALS = SSHCredentials(host="slurm-cluster.example.edu", username="alice")

COMMANDS = {
    RUN_1: ("gcmc-simulation", "--input", "config-1.json"),
    RUN_2: ("gcmc-simulation", "--input", "config-2.json"),
}
OUTPUTS = ("gcmc.out",)
WORK_DIRS = {
    RUN_1: "/home/alice/scratch/work-1",
    RUN_2: "/home/alice/scratch/work-2",
}


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


class RecordingBackoff:
    """Zero-delay recording backoff: the adapter's retry policy never
    sleeps."""

    def __init__(self) -> None:
        self.attempts: list[int] = []

    def __call__(self, attempt: int) -> float:
        self.attempts.append(attempt)
        return 0.0


class SlurmClusterMock(SSHTransport):
    """The scripted Slurm cluster -- the house mock for the external
    scheduler of scenario G. See scenario E for the full contract; G
    uses the liveness/exit-status surface (``kill -0``/``cat``) and the
    ``complete_job`` scripting of external completions, including the
    completion that arrives while the Monitor session is down."""

    _JOB_ID_RE = re.compile(r"\.sr_(sr_job_[0-9a-f]{32})_job\.status")

    def __init__(self) -> None:
        self.connected = False
        self.jobs: dict[int, dict[str, object]] = {}
        self._next_pid = 1001
        self.launches: list[str] = []

    # -- the SSH transport boundary ---------------------------------------

    def connect(self) -> None:
        if self.connected:
            raise SSHConnectionError("cluster session is already connected")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def _job_id_of(self, path: str) -> str:
        match = self._JOB_ID_RE.search(path)
        if match is None:
            raise AssertionError(f"unrecognized cluster path: {path!r}")
        return match.group(1)

    def run_command(self, command: RemoteCommand) -> RemoteResult:
        if not self.connected:
            raise SSHConnectionError("cluster session is not connected")
        argv = command.argv
        if argv[0] == "sh" and argv[1] == "-c":
            line = argv[2]
            job_id = self._job_id_of(line)
            pid = self._next_pid
            self._next_pid += 1
            self.jobs[pid] = {
                "job_id": job_id,
                "status": "running",
                "exit_code": None,
                "node_failed": False,
                "command": line,
            }
            self.launches.append(line)
            return RemoteResult(exit_code=0, stdout=str(pid))
        if argv[0] == "kill":
            pid = int(argv[2])
            job = self.jobs.get(pid)
            if job is None:
                return RemoteResult(exit_code=1)
            if job["node_failed"]:
                raise SSHConnectionError(
                    "the allocated compute node of the job failed (node"
                    " unreachable)"
                )
            if job["status"] == "running":
                return RemoteResult(exit_code=0)
            return RemoteResult(exit_code=1)
        if argv[0] == "cat":
            job_id = self._job_id_of(argv[1])
            for job in self.jobs.values():
                if job["job_id"] == job_id:
                    exit_code = job["exit_code"]
                    if exit_code is None:
                        return RemoteResult(exit_code=1)
                    return RemoteResult(exit_code=0, stdout=str(exit_code))
            return RemoteResult(exit_code=1)
        raise AssertionError(f"unexpected cluster command: {argv!r}")

    def push_file(self, local_path: Path, remote_path: RemotePath) -> None:
        return None

    def pull_file(self, remote_path: RemotePath, local_path: Path) -> None:
        return None

    # -- the scripted completion surface -----------------------------------

    def complete_job(self, pid: int, exit_code: int = 0) -> None:
        """Script the job as externally terminal (``exit_code`` 0 means
        the run completed; the status file reports the exit code)."""
        self.jobs[pid]["status"] = "failed" if exit_code else "completed"
        self.jobs[pid]["exit_code"] = exit_code


class SlurmAdapterProbe:
    """The Monitor's external-status probe over the real slurm-ssh
    adapter: reports the adapter's job status in the monitor's
    external-state vocabulary -- ``RESULT_AVAILABLE`` exactly when the
    adapter's durable record says the job completed (a terminal record
    is answered from the record alone; a running record is probed
    through the real adapter)."""

    def __init__(self, adapter: SSHComputeAdapter) -> None:
        self._adapter = adapter
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        if external.job_id is None:
            return EXTERNAL_STATE_UNKNOWN
        status = self._adapter.status(external.job_id)
        if status.state is JobState.COMPLETED:
            return EXTERNAL_STATE_RESULT_AVAILABLE
        if status.state is JobState.RUNNING:
            return EXTERNAL_STATE_RUNNING
        return EXTERNAL_STATE_UNKNOWN


class SessionResumeFailed(RuntimeError):
    """The simulated failure of the watchdog's attempt to resume the
    vanished original Monitor session (scenario G step 2)."""


class Watchdog:
    """The watchdog of scenario G: it tries to resume the original
    Monitor session and, when the resume fails (simulated), the
    replacement Monitor takes over from the durable state."""

    def __init__(self) -> None:
        self.resume_attempts: list[str] = []

    def try_resume(self) -> None:
        self.resume_attempts.append("original-monitor-session")
        raise SessionResumeFailed(
            "the original Monitor session cannot be resumed: its process"
            " is gone and the session state is not recoverable (watchdog"
            " resume failure, simulated)"
        )


class CountingDispatch:
    """The injected external-job dispatch hook that counts every call:
    the negative-proof seam -- reconstruction and resumed reconciliation
    must never create or dispatch any external job."""

    def __init__(self) -> None:
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return EXTERNAL_STATE_RUNNING


# ---------------------------------------------------------------------------
# Scenario helpers (same conventions as the house scenario tests)
# ---------------------------------------------------------------------------


def make_run(run_id: str, external: RunExternal) -> Run:
    return Run(
        run_id=run_id,
        goal_id=GOAL_ID,
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
        goal_version="1.0",
        scientific_review=None,
        worker_session_ref=None,
        external=external,
        artifacts=[],
        deviations=[],
        engineering_retries=[],
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def make_watch_record(run_id: str, external: RunExternal) -> WatchedRunRecord:
    return WatchedRunRecord(
        run_id=run_id,
        external=external,
        watched_at=FIXED_STAMP,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
    )


def write_run(run_store: FilesystemStateBackend, run: Run) -> None:
    run_store.write("run", run.run_id, run.to_dict())


def tree_bytes(root: Path) -> bytes:
    """The byte-identical snapshot of the durable state tree."""
    return b"\n".join(
        p.read_bytes()
        for p in sorted(
            (p for p in root.rglob("*") if p.is_file()),
            key=lambda p: str(p.relative_to(root)),
        )
    )


def event_files(events_dir: Path) -> list[Path]:
    return sorted((events_dir / "event").glob("*.json"), key=lambda p: p.name)


def run_file_bytes(runs_dir: Path, run_id: str) -> bytes:
    return (runs_dir / "run" / f"{run_id}.json").read_bytes()


def completion_event_id(run_id: str) -> str:
    return generate_id(
        "event",
        EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
        run_id,
        LifecycleState.RUNNING_EXTERNAL.value,
        LifecycleState.RESULT_AVAILABLE.value,
    )


@dataclass(frozen=True)
class ScenarioGResult:
    """The frozen, auditable evidence trail of one executed scenario."""

    root: Path
    cluster: SlurmClusterMock
    adapter: SSHComputeAdapter
    dispatch: CountingDispatch
    watchdog: Watchdog
    resume_failed: bool
    original_summary: ReconcileSummary
    plan: RecoveryPlan
    resumed_summary: ReconcileSummary
    steady_summary: ReconcileSummary
    state_before_watchdog: bytes
    crash_state: dict[str, bytes]
    state_before_reconstruct: bytes
    state_after_reconstruct: bytes


def execute_scenario_g(root: Path) -> ScenarioGResult:
    """Execute scenario G end to end and return the evidence trail.

    Step 1: two runs are dispatched through the real slurm-ssh adapter
    and watched; the original Monitor session reconciles once (run 1
    completes, run 2 still running). Step 2: the session disappears
    (the watchdog's resume attempt fails, simulated). During the outage
    run 2 completes on the cluster. Step 3: the replacement Monitor
    reconstructs its view from the durable state (checkpoint, event
    log, watch entries, the external adapter) and resumes the
    reconciliation loop; a second steady-state pass follows. The
    executor is a pure function of its workspace directory.
    """
    state_dir = root / "monitor"
    runs_dir = root / "runs"
    events_dir = root / "events"
    adapter_state = root / "adapter"

    clock = FakeClock()
    cluster = SlurmClusterMock()
    adapter = SSHComputeAdapter(
        DEFAULT_CREDENTIALS,
        adapter_state,
        transport=cluster,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
        now=clock,
    )

    # step 1a: dispatch both runs through the real adapter and watch them
    pid_by_run: dict[str, int] = {}
    for run_id in (RUN_1, RUN_2):
        context = RunContext(run_id, COMMANDS[run_id], WORK_DIRS[run_id], outputs=OUTPUTS)
        adapter.prepare(context)
        submitted = adapter.submit(context)
        assert submitted.job_id == generate_id("job", run_id)
        pid_by_run[run_id] = submitted.remote_pid
        external = RunExternal(
            backend=SSH_BACKEND_NAME,
            job_id=submitted.job_id,
            working_directory=WORK_DIRS[run_id],
        )
        run_store = FilesystemStateBackend(runs_dir)
        write_run(run_store, make_run(run_id, external))
        WatchedRunRegistry(state_dir, now=clock, monitor_id=MONITOR_ID).watch(
            make_watch_record(run_id, external)
        )

    # step 1b: the original Monitor session reconciles (run 1 completes)
    cluster.complete_job(pid_by_run[RUN_1])
    original_engine = ReconcileEngine(
        state_dir,
        now=clock,
        monitor_id=MONITOR_ID,
        probe=SlurmAdapterProbe(adapter),
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
    )
    original_summary = original_engine.reconcile_all()
    # the session disappears here: nothing below touches its objects

    # step 2a: during the outage run 2 completes on the cluster
    cluster.complete_job(pid_by_run[RUN_2])

    # step 2b: the watchdog tries to resume the original session -- fails
    state_before_watchdog = tree_bytes(root)
    watchdog = Watchdog()
    resume_failed = False
    try:
        watchdog.try_resume()
    except SessionResumeFailed:
        resume_failed = True
    crash_state = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }
    state_before_reconstruct = tree_bytes(root)

    # step 3: the replacement Monitor reconstructs and resumes
    dispatch = CountingDispatch()
    recovery = MonitorRecovery(
        state_dir,
        now=clock,
        monitor_id=MONITOR_ID,
        probe=SlurmAdapterProbe(adapter),
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        dispatch=dispatch,
    )
    plan = recovery.reconstruct()
    state_after_reconstruct = tree_bytes(root)
    resumed_engine = recovery.resume_engine()
    resumed_summary = resumed_engine.reconcile_all()
    steady_summary = resumed_engine.reconcile_all()

    return ScenarioGResult(
        root=root,
        cluster=cluster,
        adapter=adapter,
        dispatch=dispatch,
        watchdog=watchdog,
        resume_failed=resume_failed,
        original_summary=original_summary,
        plan=plan,
        resumed_summary=resumed_summary,
        steady_summary=steady_summary,
        state_before_watchdog=state_before_watchdog,
        crash_state=crash_state,
        state_before_reconstruct=state_before_reconstruct,
        state_after_reconstruct=state_after_reconstruct,
    )


def make_replacement(root: Path) -> MonitorRecovery:
    """A fresh replacement Monitor session over the durable state of the
    executed scenario (AC-03: fresh instances everywhere)."""
    state_dir = root / "monitor"
    runs_dir = root / "runs"
    events_dir = root / "events"
    adapter_state = root / "adapter"
    clock = FakeClock()
    adapter = SSHComputeAdapter(
        DEFAULT_CREDENTIALS,
        adapter_state,
        transport=SlurmClusterMock(),
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
        now=clock,
    )
    return MonitorRecovery(
        state_dir,
        now=clock,
        monitor_id=MONITOR_ID,
        probe=SlurmAdapterProbe(adapter),
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        dispatch=CountingDispatch(),
    )


# ---------------------------------------------------------------------------
# The crash and the failed resume
# ---------------------------------------------------------------------------


def test_G_crash_leaves_durable_state_for_the_replacement(tmp_path: Path) -> None:
    """The vanished session leaves everything the replacement needs on
    disk: both watch entries, both run records (run 1 already
    ``RESULT_AVAILABLE``, run 2 still ``RUNNING_EXTERNAL``), the
    checkpoint progress for both runs and run 1's completion event in
    the append-only log -- read from the crash-time snapshot (the state
    a replacement session finds)."""
    result = execute_scenario_g(tmp_path / "scenario-g")
    crash = result.crash_state

    watched = sorted(
        name
        for name in crash
        if name.startswith("monitor/watched/") and name.endswith(".json")
    )
    assert watched == sorted(
        [f"monitor/watched/{RUN_1}.json", f"monitor/watched/{RUN_2}.json"]
    )

    run_1 = json.loads(crash[f"runs/run/{RUN_1}.json"].decode("utf-8"))
    run_2 = json.loads(crash[f"runs/run/{RUN_2}.json"].decode("utf-8"))
    assert run_1["lifecycle_state"] == LifecycleState.RESULT_AVAILABLE.value
    assert run_2["lifecycle_state"] == LifecycleState.RUNNING_EXTERNAL.value

    checkpoint = json.loads(crash["monitor/checkpoint.json"].decode("utf-8"))
    by_run = {e["run_id"]: e for e in checkpoint["entries"]}
    assert set(by_run) == {RUN_1, RUN_2}
    assert by_run[RUN_1]["observed_state"] == EXTERNAL_STATE_RESULT_AVAILABLE
    assert by_run[RUN_2]["observed_state"] == EXTERNAL_STATE_RUNNING

    events = sorted(
        name for name in crash if name.startswith("events/event/") and name.endswith(".json")
    )
    assert len(events) == 1
    event = json.loads(crash[events[0]].decode("utf-8"))
    assert event["event_id"] == completion_event_id(RUN_1)
    assert event["event_type"] == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    assert event["run_id"] == RUN_1
    assert event["to"] == LifecycleState.RESULT_AVAILABLE.value


def test_G_watchdog_tries_resume_and_resume_fails(tmp_path: Path) -> None:
    """The watchdog attempts the resume of the vanished original session
    exactly once, the resume failure is simulated, and the failed
    attempt writes nothing (the durable state is byte-identical before
    and after the attempt)."""
    result = execute_scenario_g(tmp_path / "scenario-g")

    assert result.resume_failed
    assert result.watchdog.resume_attempts == ["original-monitor-session"]
    assert result.state_before_watchdog == result.state_before_reconstruct


# ---------------------------------------------------------------------------
# AC-02 -- no Run, no job, no completion event lost
# ---------------------------------------------------------------------------


def test_G_ac02_replacement_reconstructs_state_from_checkpoint_events_adapter(
    tmp_path: Path,
) -> None:
    """AC-02: the replacement's reconstruction view comes from the
    durable state alone -- the watch set from the registry files, the
    per-run progress from the checkpoint, the completion facts from the
    Run records and the event log, and the live view of the external
    adapter -- and reflects exactly the two runs, run 1 complete and
    run 2 still externally running (its completion happens later, during
    the outage)."""
    result = execute_scenario_g(tmp_path / "scenario-g")
    plan = result.plan

    assert plan.monitor_id == MONITOR_ID
    assert [w.run_id for w in plan.watched] == sorted([RUN_1, RUN_2])
    assert [w.external.job_id for w in plan.watched] == sorted([JOB_1, JOB_2])
    assert [p.run_id for p in plan.progress] == sorted([RUN_1, RUN_2])

    completions = {c.run_id: c for c in plan.completions}
    assert set(completions) == {RUN_1, RUN_2}

    run_1: RecoveredCompletion = completions[RUN_1]
    assert run_1.completed
    assert run_1.run_state is LifecycleState.RESULT_AVAILABLE
    assert run_1.event_id == completion_event_id(RUN_1)
    assert run_1.event_timestamp == FIXED_STAMP
    assert run_1.event_logged
    assert run_1.checkpoint_records_completion

    run_2: RecoveredCompletion = completions[RUN_2]
    assert not run_2.completed
    assert run_2.run_state is LifecycleState.RUNNING_EXTERNAL
    assert run_2.event_id is None
    assert not run_2.event_logged
    assert not run_2.checkpoint_records_completion


def test_G_ac02_reconstruction_is_observation_only(tmp_path: Path) -> None:
    """AC-02: reconstruction never writes (the durable state is
    byte-identical before and after ``reconstruct()``), never invokes
    the dispatch hook (no external job is created), and a fresh
    recovery session reconstructs the identical view without touching a
    byte."""
    root = tmp_path / "scenario-g"
    result = execute_scenario_g(root)

    assert result.dispatch.calls == []
    assert result.state_after_reconstruct == result.state_before_reconstruct

    # a fresh recovery session over a pristine replica of the crash-time
    # durable state reconstructs the identical plan without touching a byte
    replica = tmp_path / "replica"
    for rel, data in result.crash_state.items():
        target = replica / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    fresh = make_replacement(replica)
    before_fresh = tree_bytes(replica)
    assert fresh.reconstruct() == result.plan
    assert tree_bytes(replica) == before_fresh


def test_G_ac02_no_duplicate_run_job_or_completion_event(tmp_path: Path) -> None:
    """AC-02: after the replacement's resumed pass there is exactly one
    Run record per run, exactly one adapter job record per run, exactly
    two watch entries and exactly one completion event per run (the
    sequences 1 and 2 are never reused)."""
    root = tmp_path / "scenario-g"
    result = execute_scenario_g(root)

    run_records = sorted(p.name for p in (root / "runs" / "run").glob("*.json"))
    assert run_records == sorted([f"{RUN_1}.json", f"{RUN_2}.json"])
    job_records = sorted(p.name for p in (root / "adapter" / JOBS_STATE_DIR).glob("*.json"))
    assert job_records == sorted([f"{JOB_1}.json", f"{JOB_2}.json"])
    watched = sorted(p.name for p in (root / "monitor" / "watched").glob("*.json"))
    assert watched == sorted([f"{RUN_1}.json", f"{RUN_2}.json"])

    events = event_files(root / "events")
    assert len(events) == 2
    event_ids = sorted(
        json.loads(p.read_text(encoding="utf-8"))["event_id"] for p in events
    )
    assert event_ids == sorted(
        [completion_event_id(RUN_1), completion_event_id(RUN_2)]
    )
    for run_id in (RUN_1, RUN_2):
        record = json.loads(run_file_bytes(root / "runs", run_id).decode("utf-8"))
        assert record["lifecycle_state"] == LifecycleState.RESULT_AVAILABLE.value
        assert record["external"]["job_id"] == generate_id("job", run_id)

    # exactly one run performed the completion transition per pass
    assert result.original_summary.completed_count == 1
    assert result.resumed_summary.completed_count == 1
    assert result.steady_summary.completed_count == 0


def test_G_ac02_completion_during_outage_not_lost(tmp_path: Path) -> None:
    """AC-02: run 2's completion happened while the original session was
    down; the replacement's first resumed pass performs the transition
    and records the event exactly once -- the completion is not lost,
    and the run 1 completion is not duplicated."""
    root = tmp_path / "scenario-g"
    result = execute_scenario_g(root)

    resumed = result.resumed_summary
    outcomes = {o.run_id: o for o in resumed.outcomes}
    run_1: ReconcileOutcome = outcomes[RUN_1]
    run_2: ReconcileOutcome = outcomes[RUN_2]
    assert not run_1.completed  # steady-state re-poll: never re-transitioned
    assert run_1.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert run_2.completed  # the outage completion, exactly once
    assert run_2.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert run_2.transitioned_at == FIXED_STAMP
    assert run_1.transitioned_at is None

    # a fresh recovery session sees both completions recorded
    fresh = make_replacement(root)
    plan = fresh.reconstruct()
    completions = {c.run_id: c for c in plan.completions}
    assert completions[RUN_1].completed and completions[RUN_1].event_logged
    assert completions[RUN_2].completed and completions[RUN_2].event_logged
    assert completions[RUN_2].event_id == completion_event_id(RUN_2)
    assert len(event_files(root / "events")) == 2


def test_G_ac02_replacement_dispatch_never_invoked(tmp_path: Path) -> None:
    """AC-02: the replacement Monitor never creates or dispatches a new
    external job -- the resumed reconciliation reuses the existing job
    records (exactly two on the adapter) and the dispatch hook stays
    silent across reconstruction and both passes."""
    root = tmp_path / "scenario-g"
    result = execute_scenario_g(root)

    assert result.dispatch.calls == []
    job_records = sorted(p.name for p in (root / "adapter" / JOBS_STATE_DIR).glob("*.json"))
    assert job_records == sorted([f"{JOB_1}.json", f"{JOB_2}.json"])


# ---------------------------------------------------------------------------
# AC-03 -- observation-only reconstruction, exactly-once resume
# ---------------------------------------------------------------------------


def test_G_ac03_resumed_pass_and_fresh_session_are_idempotent(tmp_path: Path) -> None:
    """AC-03: after the resumed pass every further pass is a byte-identical
    no-op -- the same engine and a fresh engine over the same durable
    state both report zero completions, append nothing and change no
    byte."""
    root = tmp_path / "scenario-g"
    result = execute_scenario_g(root)
    assert result.steady_summary.completed_count == 0
    before = tree_bytes(root)
    events_before = event_files(root / "events")

    fresh_engine = make_replacement(root).resume_engine()
    summary = fresh_engine.reconcile_all()
    assert summary.completed_count == 0
    assert tree_bytes(root) == before
    assert event_files(root / "events") == events_before
    assert run_file_bytes(root / "runs", RUN_1) == run_file_bytes(root / "runs", RUN_1)


# ---------------------------------------------------------------------------
# Determinism and hygiene
# ---------------------------------------------------------------------------


def test_G_deterministic_scenario_repeatable(tmp_path: Path) -> None:
    """The full scenario is deterministic: two executions in separate
    workspaces produce byte-identical durable state and identical
    plans and summaries (fixed stamps, zero-delay backoff, scripted
    cluster)."""
    first = execute_scenario_g(tmp_path / "first")
    second = execute_scenario_g(tmp_path / "second")

    assert first.plan == second.plan
    assert first.original_summary == second.original_summary
    assert first.resumed_summary == second.resumed_summary
    assert first.steady_summary == second.steady_summary
    assert tree_bytes(tmp_path / "first") == tree_bytes(tmp_path / "second")


def test_G_scenario_uses_safe_ids_only() -> None:
    """Every id the scenario touches is a generated id (lowercase kind +
    32 hex): path construction stays within the safe-identifier
    contract."""
    ids = (
        MONITOR_ID,
        RUN_1,
        RUN_2,
        GOAL_ID,
        JOB_1,
        JOB_2,
        completion_event_id(RUN_1),
        completion_event_id(RUN_2),
    )
    for value in ids:
        assert is_valid_id(value)
    assert all(
        not any(sep in value for sep in ("/", "\\", "*", "?", "[")) for value in ids
    )


def test_G_records_are_frozen_and_validate_their_contract() -> None:
    """The scenario's recovery records are frozen dataclasses that
    validate the documented contract (no silent mutation, no malformed
    completion facts)."""
    assert is_dataclass(RecoveryPlan)
    assert is_dataclass(RecoveredCompletion)
    assert is_dataclass(ReconcileOutcome)
    assert is_dataclass(ReconcileSummary)

    outcome = ReconcileOutcome(
        run_id=RUN_2,
        observed_state=EXTERNAL_STATE_RESULT_AVAILABLE,
        observed_at=FIXED_STAMP,
        completed=True,
        transitioned_at=FIXED_STAMP,
    )
    with pytest.raises(FrozenInstanceError):
        outcome.completed = False  # type: ignore[misc]
    completion = RecoveredCompletion(
        run_id=RUN_2,
        run_state=LifecycleState.RESULT_AVAILABLE,
        event_id=completion_event_id(RUN_2),
        event_timestamp=FIXED_STAMP,
        event_logged=True,
        checkpoint_records_completion=True,
    )
    with pytest.raises(FrozenInstanceError):
        completion.run_state = LifecycleState.RUNNING_EXTERNAL  # type: ignore[misc]
    with pytest.raises(RecoveryError):
        # a completion fact that claims a logged event without its id is
        # malformed and rejected by the contract
        RecoveredCompletion(
            run_id=RUN_2,
            run_state=LifecycleState.RESULT_AVAILABLE,
            event_logged=True,
        )
