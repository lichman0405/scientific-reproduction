"""FDM-201 simulated scenario E -- engineering Slurm failure (DEV-M8-G06).

Scenario E is the acceptance fixture **S4** of
``examples/fdm-201/simulated-scenarios.md``: the GCMC job dies because
the allocated compute node fails -- a scheduler/node issue, not a
scientific failure. Expected behavior (frozen acceptance):

* AC-01: the Monitor performs **identical engineering recovery only**:
  the failure is classified through the real slurm-ssh adapter's durable
  job record (``failure_class == "transport"``), the identical batch is
  resubmitted at the scheduler level, and **no Supervisor scientific
  replan** ever happens (no decision record, no run mutation, no new Run
  record, no new adapter job record).
* AC-02: a genuine scientific compute failure (non-zero remote exit,
  ``failure_class == "job"``) is **never** auto-resubmitted: observed,
  recorded and refused.
* AC-03: the decision history is durable: a replacement Monitor session
  (fresh dispatcher over the same state) replays the recorded decision
  exactly -- nothing is performed twice.

The scenario is executed end to end through the real machinery: the
``SSHComputeAdapter`` (real prepare/submit/status, real durable job
records and failure classification) over a scripted Slurm cluster
transport double (the house mock for the external scheduler -- same
pattern as the fake SSH transport of ``tests/adapters/compute/``), the
``RetryDispatcher`` of the execution monitor with the failure classifier
and the scheduler-level resubmission plumbing wired through the real
adapter, the ``WatchedRunRegistry``, the real ``FilesystemStateBackend``
run store and the real append-only ``ProjectEventLog``.

Determinism contract: every timestamp is the injected fixed stamp of a
single-stamp ``FakeClock``; the retry backoff is a zero-delay recording
backoff (the tested path never sleeps); the cluster is a pure in-memory
scripted double (no network, no wall clock). The scenario executor is a
pure function of its workspace directory, so the full scenario is
executed twice in the determinism test and the durable artifacts must
match byte for byte.

Test map: ``test_E_ac01_*`` -> AC-01 (identical engineering recovery
only), ``test_E_ac02_*`` -> AC-02 (scientific failure never
auto-resubmitted), ``test_E_ac03_*`` -> AC-03 (recorded decision
history replays exactly once).
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from pathlib import Path

import pytest

import scientific_reproduction.monitoring.retry as retry_module
from scientific_reproduction.adapters.compute.local import (
    JOBS_STATE_DIR,
    JobState,
    RunContext,
)
from scientific_reproduction.adapters.compute.ssh import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    FAILURE_CLASS_JOB,
    FAILURE_CLASS_TRANSPORT,
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
from scientific_reproduction.monitoring.registry import (
    WatchedRunRecord,
    WatchedRunRegistry,
)
from scientific_reproduction.monitoring.retry import (
    ENGINEERING_RETRY_WHITELIST,
    RETRY_ACTOR,
    RETRY_AUTHORIZED_REASON,
    RETRY_DECISION_AUTHORIZED,
    RETRY_DECISION_EVENT_TYPE,
    RETRY_DECISION_REFUSED,
    RETRY_REFUSED_REASON,
    RetryDispatcher,
    RetryError,
    RetryOutcome,
    RetrySummary,
)

FIXED_STAMP = "2026-08-14T00:00:00+00:00"

MONITOR_ID = generate_id("monitor", "scenario-e", "session-1")
RUN_ID = generate_id("run", "goal-1", "seq-1")
GOAL_ID = generate_id("goal", "goal-1")
JOB_ID = generate_id("job", RUN_ID)

DEFAULT_CREDENTIALS = SSHCredentials(host="slurm-cluster.example.edu", username="alice")

COMMAND = ("gcmc-simulation", "--input", "config.json")
OUTPUTS = ("gcmc.out",)
WORK_DIR = "/home/alice/scratch/work-1"


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
    """Zero-delay recording backoff: records every attempt it is asked
    to compute the delay for and always returns 0.0 -- the retry policy
    of the adapter never sleeps."""

    def __init__(self) -> None:
        self.attempts: list[int] = []

    def __call__(self, attempt: int) -> float:
        self.attempts.append(attempt)
        return 0.0


class SlurmClusterMock(SSHTransport):
    """The scripted Slurm cluster -- the house mock for the external
    scheduler of scenario E.

    Implements the transport boundary the real ``SSHComputeAdapter``
    talks to (connect/disconnect/run_command/push_file/pull_file) with a
    deterministic in-memory job table, plus the scheduler-level
    resubmission surface of the Monitor's plumbing:

    * the launch wrapper of ``submit`` assigns a deterministic remote
      pid and records the batch;
    * ``kill -0 <pid>`` answers liveness -- a job whose allocated node
      failed raises ``SSHConnectionError`` (the node failure of S4), a
      terminal job answers exit 1, a running job answers exit 0;
    * ``cat <statusfile>`` answers the recorded exit code of a terminal
      job (the ``echo $? > .sr_<job_id>_job.status`` of the real launch
      wrapper);
    * ``resubmit_identical`` re-accepts the identical batch and returns
      the fresh external job id -- the receipt of the resubmission.

    Every mutation is scripted by the test (``complete_job``,
    ``fail_allocated_node``); nothing is time-dependent.
    """

    _JOB_ID_RE = re.compile(r"\.sr_(sr_job_[0-9a-f]{32})_job\.status")

    def __init__(self) -> None:
        self.connected = False
        self.jobs: dict[int, dict[str, object]] = {}
        self._next_pid = 1001
        self.launches: list[str] = []
        self.resubmissions: list[tuple[tuple[str, ...], str, str]] = []

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
            # the launch wrapper of the adapter's submit: the cluster
            # accepts the batch and starts the job on a compute node
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
                    "the allocated compute node of the job failed: node"
                    " 'compute-7' is unreachable (scheduler/node issue)"
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

    # -- the scripted failure/recovery surface -----------------------------

    def complete_job(self, pid: int, exit_code: int = 0) -> None:
        """Script the job as externally terminal with ``exit_code``."""
        self.jobs[pid]["status"] = "failed" if exit_code else "completed"
        self.jobs[pid]["exit_code"] = exit_code

    def fail_allocated_node(self, pid: int) -> None:
        """Script the allocated node of the running job as failed (S4:
        the node dies while the GCMC job runs)."""
        self.jobs[pid]["node_failed"] = True

    def resubmit_identical(
        self, command: tuple[str, ...], working_directory: str
    ) -> str:
        """The scheduler-level identical resubmission: re-accept the
        identical batch (the same command in the same working
        directory) and return the fresh external job id -- the receipt
        of the resubmission."""
        job_id = generate_id("job", f"resubmit-{len(self.resubmissions) + 1}")
        pid = self._next_pid
        self._next_pid += 1
        self.jobs[pid] = {
            "job_id": job_id,
            "status": "running",
            "exit_code": None,
            "node_failed": False,
            "command": " ".join(command),
        }
        self.resubmissions.append((command, working_directory, job_id))
        return job_id


class AdapterFailureClassifier:
    """The Monitor's failure classifier over the real slurm-ssh adapter:
    reads the ``failure_class`` the adapter durably recorded on the job
    record -- ``"transport"`` for the S4 node/scheduler failure, ``"job"``
    for a scientific compute failure (the classification is the
    adapter's own AC-01 decision record, never guessed)."""

    def __init__(self, adapter: SSHComputeAdapter) -> None:
        self._adapter = adapter
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str | None:
        self.calls.append(external)
        if external.job_id is None:
            return None
        return self._adapter.read_job(external.job_id).failure_class


class AdapterResubmitPlumbing:
    """The Monitor's scheduler-level resubmission plumbing: replays the
    identical batch -- the command and working directory of the failed
    job, read from the real adapter's durable job record, never
    re-prepared and never re-planned -- into the scheduler's
    resubmission queue and returns the fresh external identity (the
    receipt recorded in the decision event)."""

    def __init__(self, adapter: SSHComputeAdapter, cluster: SlurmClusterMock) -> None:
        self._adapter = adapter
        self._cluster = cluster
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> RunExternal:
        self.calls.append(external)
        if external.job_id is None:
            raise AssertionError("the resubmission hook requires a job id")
        record = self._adapter.read_job(external.job_id)
        resubmitted_job_id = self._cluster.resubmit_identical(
            record.command, record.working_directory
        )
        return RunExternal(
            backend=SSH_BACKEND_NAME,
            job_id=resubmitted_job_id,
            working_directory=record.working_directory,
        )


# ---------------------------------------------------------------------------
# Scenario helpers (same conventions as the house scenario tests)
# ---------------------------------------------------------------------------


def make_run(external: RunExternal) -> Run:
    return Run(
        run_id=RUN_ID,
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


def make_watch_record(external: RunExternal) -> WatchedRunRecord:
    return WatchedRunRecord(
        run_id=RUN_ID,
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


def event_records(events_dir: Path) -> tuple[dict[str, object], ...]:
    """The recorded events of the real append-only event log."""
    return tuple(
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(
            (events_dir / "event").glob("*.json"),
            key=lambda p: p.name,
        )
    )


def run_file_bytes(runs_dir: Path) -> bytes:
    return (runs_dir / "run" / f"{RUN_ID}.json").read_bytes()


def job_file_bytes(adapter_state: Path) -> bytes:
    return (adapter_state / JOBS_STATE_DIR / f"{JOB_ID}.json").read_bytes()


@dataclass(frozen=True)
class ScenarioEResult:
    """The frozen, auditable evidence trail of one executed scenario."""

    root: Path
    failure_kind: str
    cluster: SlurmClusterMock
    adapter: SSHComputeAdapter
    backoff: RecordingBackoff
    summary: RetrySummary
    outcome: RetryOutcome
    run: Run
    watch: WatchedRunRecord
    run_bytes_before: bytes


def execute_scenario_e(
    root: Path,
    *,
    failure_kind: str = "transport",
) -> ScenarioEResult:
    """Execute scenario E end to end and return the evidence trail.

    The run is dispatched through the real slurm-ssh adapter over the
    scripted cluster; the job then fails -- for
    ``failure_kind="transport"`` the allocated node dies (S4, the
    scheduler/node issue), for ``failure_kind="job"`` the job exits
    non-zero (a scientific compute failure). The Monitor's retry
    dispatcher classifies the failure through the adapter's durable job
    record and decides. The executor is a pure function of its
    workspace directory.
    """
    state_dir = root / "monitor"
    runs_dir = root / "runs"
    events_dir = root / "events"
    adapter_state = root / "adapter"

    clock = FakeClock()
    backoff = RecordingBackoff()
    cluster = SlurmClusterMock()
    adapter = SSHComputeAdapter(
        DEFAULT_CREDENTIALS,
        adapter_state,
        transport=cluster,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=backoff),
        now=clock,
    )

    context = RunContext(RUN_ID, COMMAND, WORK_DIR, outputs=OUTPUTS)
    prepared = adapter.prepare(context)
    submitted = adapter.submit(context)
    assert prepared.job_id == submitted.job_id == JOB_ID
    assert len(cluster.launches) == 1
    assert JOB_ID in cluster.launches[0]

    if failure_kind == "transport":
        # S4: the allocated compute node of the running GCMC job fails.
        cluster.fail_allocated_node(submitted.remote_pid)
        with pytest.raises(SSHConnectionError):
            adapter.status(JOB_ID)
    else:
        # a scientific compute failure: the job runs to a non-zero exit.
        cluster.complete_job(submitted.remote_pid, exit_code=2)
        adapter.status(JOB_ID)

    external = RunExternal(
        backend=SSH_BACKEND_NAME,
        job_id=JOB_ID,
        working_directory=WORK_DIR,
    )
    run = make_run(external)
    watch = make_watch_record(external)
    run_store = FilesystemStateBackend(runs_dir)
    write_run(run_store, run)
    WatchedRunRegistry(state_dir, now=clock, monitor_id=MONITOR_ID).watch(watch)
    run_bytes_before = run_file_bytes(runs_dir)

    dispatcher = RetryDispatcher(
        state_dir,
        now=clock,
        monitor_id=MONITOR_ID,
        classifier=AdapterFailureClassifier(adapter),
        resubmit=AdapterResubmitPlumbing(adapter, cluster),
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
    )
    summary = dispatcher.decide_all()
    assert len(summary.outcomes) == 1

    return ScenarioEResult(
        root=root,
        failure_kind=failure_kind,
        cluster=cluster,
        adapter=adapter,
        backoff=backoff,
        summary=summary,
        outcome=summary.outcomes[0],
        run=run,
        watch=watch,
        run_bytes_before=run_bytes_before,
    )


def make_dispatcher(
    root: Path, *, monitor_id: str = MONITOR_ID
) -> RetryDispatcher:
    """A fresh Monitor-session dispatcher over the durable state of the
    executed scenario (AC-03: the replacement session)."""
    return RetryDispatcher(
        root / "monitor",
        now=FakeClock(),
        monitor_id=monitor_id,
        classifier=AdapterFailureClassifier(
            SSHComputeAdapter(
                DEFAULT_CREDENTIALS,
                root / "adapter",
                transport=SlurmClusterMock(),
                retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
                now=FakeClock(),
            )
        ),
        resubmit=AdapterResubmitPlumbing(
            SSHComputeAdapter(
                DEFAULT_CREDENTIALS,
                root / "adapter",
                transport=SlurmClusterMock(),
                retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
                now=FakeClock(),
            ),
            SlurmClusterMock(),
        ),
        run_store=FilesystemStateBackend(root / "runs"),
        event_log=ProjectEventLog(root / "events"),
    )


# ---------------------------------------------------------------------------
# AC-01 -- identical engineering recovery only
# ---------------------------------------------------------------------------


def test_E_node_failure_is_a_scheduler_issue_scripted(tmp_path: Path) -> None:
    """The S4 fixture: the allocated node of the running GCMC job dies.
    The adapter's status probe hits the unreachable node -- a
    connection-level failure, bounded by the injected retry policy with
    zero delay (never a sleep)."""
    cluster = SlurmClusterMock()
    backoff = RecordingBackoff()
    adapter = SSHComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path / "adapter",
        transport=cluster,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=backoff),
        now=FakeClock(),
    )
    adapter.prepare(RunContext(RUN_ID, COMMAND, WORK_DIR, outputs=OUTPUTS))
    submitted = adapter.submit(RunContext(RUN_ID, COMMAND, WORK_DIR, outputs=OUTPUTS))
    cluster.fail_allocated_node(submitted.remote_pid)
    with pytest.raises(SSHConnectionError, match="unreachable"):
        adapter.status(submitted.job_id)
    record = adapter.read_job(submitted.job_id)
    assert record.failure_class == FAILURE_CLASS_TRANSPORT
    assert record.state is JobState.RUNNING
    assert "unreachable" in (record.error or "")
    assert backoff.attempts == [1, 2]  # bounded retries, zero delay
    assert len(cluster.launches) == 1
    assert submitted.job_id in cluster.launches[0]


def test_E_ac01_transport_failure_authorizes_identical_resubmission(
    tmp_path: Path,
) -> None:
    """AC-01: the S4 node failure is classified ``transport`` through
    the real adapter's durable job record and the identical batch is
    resubmitted at the scheduler level: same command, same working
    directory, fresh external job id."""
    result = execute_scenario_e(tmp_path / "scenario-e")
    outcome = result.outcome

    assert outcome.failure_class == FAILURE_CLASS_TRANSPORT
    assert outcome.decision == RETRY_DECISION_AUTHORIZED
    assert outcome.decided_at == FIXED_STAMP
    assert not outcome.replayed
    assert result.summary.authorized_count == 1
    assert result.summary.refused_count == 0

    resubmitted = outcome.resubmitted_external
    assert resubmitted is not None
    assert resubmitted.backend == SSH_BACKEND_NAME
    assert resubmitted.job_id != JOB_ID
    assert is_valid_id(resubmitted.job_id, "job")
    assert resubmitted.working_directory == WORK_DIR

    # the scheduler accepted the identical batch exactly once
    assert result.cluster.resubmissions == [
        (COMMAND, WORK_DIR, resubmitted.job_id)
    ]


def test_E_ac01_resubmission_is_a_scheduler_receipt_not_a_new_job_record(
    tmp_path: Path,
) -> None:
    """AC-01: the identical resubmission is a scheduler-level receipt --
    no new Run record and no new adapter job record are ever created
    (the run keeps its identity, the adapter keeps exactly one durable
    job record)."""
    execute_scenario_e(tmp_path / "scenario-e")

    jobs_dir = tmp_path / "scenario-e" / "adapter" / JOBS_STATE_DIR
    assert sorted(p.name for p in jobs_dir.glob("*.json")) == [f"{JOB_ID}.json"]
    run_records = sorted(
        p.name for p in (tmp_path / "scenario-e" / "runs" / "run").glob("*.json")
    )
    assert run_records == [f"{RUN_ID}.json"]
    watched = sorted(
        p.name for p in (tmp_path / "scenario-e" / "monitor" / "watched").glob("*.json")
    )
    assert watched == [f"{RUN_ID}.json"]


def test_E_ac01_engineering_recovery_only_no_supervisor_replan(
    tmp_path: Path,
) -> None:
    """AC-01: engineering recovery only -- the run record is never
    mutated (byte-identical, still ``RUNNING_EXTERNAL``, no engineering
    retry entry, no parameter change), no Supervisor decision exists
    anywhere, and the event log records only the engineering retry
    decision (never a replan/decision event)."""
    result = execute_scenario_e(tmp_path / "scenario-e")

    assert result.run_bytes_before == run_file_bytes(tmp_path / "scenario-e" / "runs")
    run_record = json.loads(
        run_file_bytes(tmp_path / "scenario-e" / "runs").decode("utf-8")
    )
    assert run_record["run_id"] == RUN_ID
    assert run_record["lifecycle_state"] == LifecycleState.RUNNING_EXTERNAL.value
    assert run_record["engineering_retries"] == []
    assert run_record["external"]["job_id"] == JOB_ID
    assert result.run.lifecycle_state is LifecycleState.RUNNING_EXTERNAL

    # no Supervisor decision artifact anywhere in the durable state
    decision_files = [
        p for p in result.root.rglob("*") if p.is_file() and "decision" in p.name
    ]
    assert decision_files == []

    events = event_records(tmp_path / "scenario-e" / "events")
    assert len(events) == 1
    assert events[0]["event_type"] == RETRY_DECISION_EVENT_TYPE
    assert events[0]["actor"] == RETRY_ACTOR
    assert events[0]["run_id"] == RUN_ID
    assert events[0]["reason"] == RETRY_AUTHORIZED_REASON
    assert events[0]["payload"]["decision"] == RETRY_DECISION_AUTHORIZED
    assert events[0]["payload"]["failure_class"] == FAILURE_CLASS_TRANSPORT
    assert events[0]["payload"]["resubmitted_external"]["backend"] == SSH_BACKEND_NAME


def test_E_ac01_no_supervisor_decision_surface_in_the_monitor_path() -> None:
    """AC-01: the Monitor's engineering-recovery machinery is
    self-contained -- the retry dispatcher source never references the
    Supervisor decision surface (no ``SupervisorDecision``, no
    ``DecisionType``) and nothing in the executed path consults it."""
    tree = ast.parse(inspect.getsource(retry_module))
    identifiers = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) or isinstance(node, ast.Attribute)
        for node in [node]
        if isinstance(node, ast.Name)
    }
    assert "SupervisorDecision" not in identifiers
    assert "DecisionType" not in identifiers


def test_E_ac01_resubmission_decided_exactly_once_per_failure(
    tmp_path: Path,
) -> None:
    """AC-01: the resubmission happens exactly once per failure -- a
    second decision pass over the same durable state replays the
    recorded decision (same receipt, hook not invoked again, no new
    event)."""
    result = execute_scenario_e(tmp_path / "scenario-e")
    first = result.outcome
    assert first.resubmitted_external is not None

    dispatcher = make_dispatcher(result.root)
    replay = dispatcher.decide_all()
    assert len(replay.outcomes) == 1
    second = replay.outcomes[0]
    assert second.decision == RETRY_DECISION_AUTHORIZED
    assert second.replayed
    assert second.resubmitted_external == first.resubmitted_external
    assert second.event_id == first.event_id
    assert len(result.cluster.resubmissions) == 1
    assert len(event_records(result.root / "events")) == 1


# ---------------------------------------------------------------------------
# AC-02 -- scientific failures are never auto-resubmitted
# ---------------------------------------------------------------------------


def test_E_ac02_scientific_failure_refused_no_resubmission(tmp_path: Path) -> None:
    """AC-02: a scientific compute failure (the job exits non-zero,
    ``failure_class == "job"`` on the adapter's durable record) is
    refused: observed and recorded, never resubmitted, never mutating
    any run parameter."""
    result = execute_scenario_e(tmp_path / "scenario-e", failure_kind="job")
    outcome = result.outcome

    assert outcome.failure_class == FAILURE_CLASS_JOB
    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.resubmitted_external is None
    assert outcome.decided_at == FIXED_STAMP
    assert result.summary.authorized_count == 0
    assert result.summary.refused_count == 1

    # the run record is byte-identical and the resubmission hook was
    # never invoked (no scheduler resubmission, no new job)
    assert result.run_bytes_before == run_file_bytes(tmp_path / "scenario-e" / "runs")
    assert result.cluster.resubmissions == []

    events = event_records(tmp_path / "scenario-e" / "events")
    assert len(events) == 1
    assert events[0]["event_type"] == RETRY_DECISION_EVENT_TYPE
    assert events[0]["reason"] == RETRY_REFUSED_REASON
    assert events[0]["payload"]["decision"] == RETRY_DECISION_REFUSED
    assert events[0]["payload"]["failure_class"] == FAILURE_CLASS_JOB
    assert "resubmitted_external" not in events[0]["payload"]

    # the adapter recorded the job-level failure itself (non-zero exit)
    record = result.adapter.read_job(JOB_ID)
    assert record.state is JobState.FAILED
    assert record.failure_class == FAILURE_CLASS_JOB
    assert record.error == "remote command exited with status 2"


def test_E_ac02_unclassified_failure_refused(tmp_path: Path) -> None:
    """AC-02: a failure the adapter never classified (None) can never
    authorize a resubmission -- refusal is the safe-by-construction
    default, and the decision is still recorded durably."""
    result = execute_scenario_e(tmp_path / "scenario-e", failure_kind="job")
    dispatcher = make_dispatcher(result.root)
    outcome = dispatcher.decide(RUN_ID, None)
    assert outcome.failure_class is None
    assert outcome.decision == RETRY_DECISION_REFUSED
    assert outcome.resubmitted_external is None
    assert not outcome.replayed
    # nothing was resubmitted and no new decision artifact appeared
    assert result.cluster.resubmissions == []
    assert len(event_records(result.root / "events")) == 2


def test_E_ac02_whitelist_is_engineering_only() -> None:
    """AC-02: the engineering whitelist admits exactly the transport
    class -- the job class (scientific compute failures) is never on
    it."""
    assert ENGINEERING_RETRY_WHITELIST == frozenset({FAILURE_CLASS_TRANSPORT})
    assert FAILURE_CLASS_TRANSPORT == "transport"
    assert FAILURE_CLASS_JOB == "job"
    assert FAILURE_CLASS_JOB not in ENGINEERING_RETRY_WHITELIST


# ---------------------------------------------------------------------------
# AC-03 -- recorded history replays exactly once (replacement Monitor)
# ---------------------------------------------------------------------------


def test_E_ac03_fresh_dispatcher_replays_recorded_history(tmp_path: Path) -> None:
    """AC-03: a replacement Monitor session (fresh dispatcher, fresh
    classifier, fresh plumbing over the same durable state) reconstructs
    the recorded decision history -- the original receipt and stamp are
    authoritative, nothing is performed twice and no durable bytes
    change."""
    result = execute_scenario_e(tmp_path / "scenario-e")
    before = tree_bytes(tmp_path / "scenario-e")

    dispatcher = make_dispatcher(result.root)
    replay = dispatcher.decide_all()
    assert len(replay.outcomes) == 1
    assert replay.outcomes[0].replayed
    assert replay.outcomes[0].resubmitted_external == result.outcome.resubmitted_external
    assert replay.outcomes[0].decided_at == FIXED_STAMP
    assert len(result.cluster.resubmissions) == 1

    assert tree_bytes(tmp_path / "scenario-e") == before
    assert run_file_bytes(tmp_path / "scenario-e" / "runs") == result.run_bytes_before


def test_E_ac03_fresh_dispatcher_replays_refused_history(tmp_path: Path) -> None:
    """AC-03: the replacement session replays refused decisions too --
    the recorded refusal is authoritative (nothing becomes authorized on
    replay)."""
    result = execute_scenario_e(tmp_path / "scenario-e", failure_kind="job")
    dispatcher = make_dispatcher(result.root)
    replay = dispatcher.decide_all()
    assert len(replay.outcomes) == 1
    assert replay.outcomes[0].decision == RETRY_DECISION_REFUSED
    assert replay.outcomes[0].replayed
    assert replay.outcomes[0].resubmitted_external is None
    assert len(event_records(result.root / "events")) == 1


# ---------------------------------------------------------------------------
# Determinism and hygiene
# ---------------------------------------------------------------------------


def test_E_deterministic_scenario_repeatable(tmp_path: Path) -> None:
    """The full scenario is deterministic: two executions in separate
    workspaces produce byte-identical durable state and identical
    outcomes (fixed stamps, zero-delay backoff, scripted cluster)."""
    first = execute_scenario_e(tmp_path / "first")
    second = execute_scenario_e(tmp_path / "second")

    assert first.outcome == second.outcome
    assert first.summary == second.summary
    assert first.cluster.launches == second.cluster.launches
    assert first.cluster.resubmissions == second.cluster.resubmissions
    assert tree_bytes(tmp_path / "first") == tree_bytes(tmp_path / "second")
    assert event_records(tmp_path / "first" / "events") == event_records(
        tmp_path / "second" / "events"
    )


def test_E_scenario_uses_safe_ids_only() -> None:
    """Every id the scenario touches is a generated id (lowercase kind +
    32 hex): path construction stays within the safe-identifier
    contract."""
    ids = (
        MONITOR_ID,
        RUN_ID,
        GOAL_ID,
        JOB_ID,
        generate_id("event", RETRY_DECISION_EVENT_TYPE, RUN_ID, "transport"),
        generate_id("job", "resubmit-1"),
    )
    for value in ids:
        assert is_valid_id(value)
    assert all(
        not any(sep in value for sep in ("/", "\\", "*", "?", "[")) for value in ids
    )


def test_E_outcomes_are_frozen_and_validate_their_contract() -> None:
    """The scenario's decision records are frozen dataclasses that
    validate the documented contract: an authorized decision always
    carries the resubmission receipt, a refused decision never does,
    and no outcome can be silently mutated."""
    assert is_dataclass(RetryOutcome)
    assert is_dataclass(RetrySummary)
    outcome = RetryOutcome(
        run_id=RUN_ID,
        failure_class=FAILURE_CLASS_TRANSPORT,
        decision=RETRY_DECISION_AUTHORIZED,
        decided_at=FIXED_STAMP,
        resubmitted_external=RunExternal(
            backend=SSH_BACKEND_NAME,
            job_id=generate_id("job", "resubmit-1"),
            working_directory=WORK_DIR,
        ),
        event_id=generate_id(
            "event", RETRY_DECISION_EVENT_TYPE, RUN_ID, FAILURE_CLASS_TRANSPORT
        ),
    )
    with pytest.raises(FrozenInstanceError):
        outcome.decision = RETRY_DECISION_REFUSED  # type: ignore[misc]
    with pytest.raises(RetryError):
        RetryOutcome(
            run_id=RUN_ID,
            failure_class=FAILURE_CLASS_TRANSPORT,
            decision=RETRY_DECISION_AUTHORIZED,
            decided_at=FIXED_STAMP,
        )
