"""Tests for the slurm-over-SSH ComputeAdapter (DEV-M7-G04,
deliverable).

Direct per-AC coverage, named after the acceptance criteria:

* ``test_slurm_ac01_*`` -- the external Slurm job id persists after the
  submitting Worker exits: ``submit`` parses ``Submitted batch job
  <id>`` into the record's first-class ``external_id`` field, and a
  **fresh adapter instance** over the same state directory can
  ``status``/``collect``/``cancel``/``resume`` the job from the record
  alone. Transport failures (dropped connection, unreachable host,
  dropped mid-transfer) raise/record the TRANSPORT failure class while
  clean remote answers (sbatch/mkdir refusal, unparseable submission
  line, missing remote output) are the JOB-level stable errors, never
  retried.
* ``test_slurm_ac02_*`` -- the full scheduler-state vocabulary
  (PENDING/PD/CONFIGURING/CF/SUSPENDED/S/RUNNING/R/COMPLETING/CG/
  REQUEUED/RQ through ``squeue``, COMPLETED/CD/CANCELLED/CA/FAILED/F/
  TIMEOUT/TO/NODE_FAIL/NF/OUT_OF_MEMORY/OOM/PREEMPTED/PR through
  ``sacct``) normalizes to the canonical JobState vocabulary through
  the ordered rule table ``R-SLURM-S1..S27`` (first match wins, trailing
  total default). Queued states are ``running`` (alive, not terminal)
  with the raw observed state carried on the status outcome; terminal
  decisions are persisted once and answered from the record alone;
  identical observations produce byte-identical decisions; totality is
  proven by ``validate_slurm_state_rules``.
* ``test_slurm_ac03_*`` -- scientific input files are never modified:
  input bytes are identical after scripted transport-failure/retry
  cycles, after failed job outcomes and after a full collect, and the
  input's content never appears in any state-directory file.
* ``test_slurm_*`` -- the full six-operation ComputeAdapter interface,
  the Modules/environment execution metadata capture (module-load
  statements and environment exports in the generated batch script,
  captured on the durable record, never credentials), the durable
  record contract (schema, from_dict validation, no credential fields),
  the state rule-table validator, cancel semantics, idempotent
  re-collection and the strict adapter boundaries.

Determinism: every test injects a scripted :class:`FakeSlurmTransport`
(no network), a :class:`FakeClock` producing the fixed ``FIXED_STAMP``
timestamp (no wall clock), a zero-delay backoff (no sleeps) and
``tmp_path`` state directories. No randomness, no network, no sleeps
anywhere; every artifact assertion exercises the real registry,
checksum and id machinery.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from scientific_reproduction.adapters.compute.local import (
    TERMINATE_PENDING_NOTE,
    ComputeCollectError,
    ComputeJobIdentityError,
    ComputeJobNotFoundError,
    ComputeJobRecordError,
    ComputeJobStateError,
    JobState,
    RunContext,
)
from scientific_reproduction.adapters.compute.slurm_ssh import (
    SLURM_BACKEND_NAME,
    SLURM_STATE_RULES,
    SLURM_STATE_UNAVAILABLE_NOTE,
    SlurmAdapterError,
    SlurmCancelledJob,
    SlurmCollectedJob,
    SlurmComputeAdapter,
    SlurmJobIdentityError,
    SlurmJobLaunchError,
    SlurmJobRecord,
    SlurmJobStatus,
    SlurmPreparedJob,
    SlurmResumedJob,
    SlurmStateRule,
    SlurmSubmittedJob,
    normalize_scheduler_state,
    validate_slurm_state_rules,
)
from scientific_reproduction.adapters.compute.ssh import (
    FAILURE_CLASS_JOB,
    FAILURE_CLASS_TRANSPORT,
    RemoteCommand,
    RemotePath,
    RemoteResult,
    SSHConnectionError,
    SSHCredentials,
    SSHJobIdentityError,
    SSHRemoteError,
    SSHRemoteFileNotFoundError,
    SSHRetryPolicy,
    SSHTransferError,
    SSHTransport,
    SSHTransportError,
)
from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.ids import generate_id

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: The default remote working directory of the fixtures.
REMOTE_WORKDIR = "/home/alice/scratch/work-1"

#: The default scripted external Slurm job id of a submitted job.
EXTERNAL_ID = 423554

#: The default scripted credentials (secrets deliberately distinctive so
#: the persistence walk can prove their absence).
DEFAULT_CREDENTIALS = SSHCredentials(
    host="cluster.example.edu",
    username="cred-user-77",
    password="s3cr3t-p@ssw0rd-9",
    private_key_path=r"C:\keys\id_ed25519",
    key_passphrase="pa55phrase-7-xyz",
)

#: The active (non-terminal) scheduler state vocabulary (AC-02).
ACTIVE_SLURM_STATES = [
    "PENDING",
    "PD",
    "CONFIGURING",
    "CF",
    "SUSPENDED",
    "S",
    "RUNNING",
    "R",
    "COMPLETING",
    "CG",
    "REQUEUED",
    "RQ",
]

#: The terminal scheduler state vocabulary (AC-02).
COMPLETED_SLURM_STATES = ["COMPLETED", "CD"]
CANCELLED_SLURM_STATES = ["CANCELLED", "CA"]
FAILED_SLURM_STATES = [
    "FAILED",
    "F",
    "TIMEOUT",
    "TO",
    "NODE_FAIL",
    "NF",
    "OUT_OF_MEMORY",
    "OOM",
    "PREEMPTED",
    "PR",
]


def make_run_id(label: str = "goal-4") -> str:
    """A deterministic generated run id for a fixture label."""
    return generate_id("run", label)


def make_context(
    run_id: str | None = None,
    *,
    command: tuple[str, ...] = ("python", "sim.py", "--param", "a b"),
    working_directory: str = REMOTE_WORKDIR,
    outputs: tuple[str, ...] = ("result.txt",),
) -> RunContext:
    """A default run context; pass explicit values to vary one axis."""
    return RunContext(
        run_id=run_id if run_id is not None else make_run_id(),
        command=command,
        working_directory=working_directory,
        outputs=outputs,
    )


class FakeClock:
    """Injectable clock.

    A single stamp repeats forever; a sequence pops stamps in order and
    raises if exhausted (tests that need distinct timestamps enumerate
    them explicitly).
    """

    def __init__(self, *stamps: str) -> None:
        assert stamps, "FakeClock requires at least one stamp"
        self._stamps = list(stamps)
        self.calls: list[str] = []

    def __call__(self) -> str:
        if len(self._stamps) == 1:
            stamp = self._stamps[0]
        else:
            if not self._stamps:
                raise AssertionError("FakeClock exhausted its stamp sequence")
            stamp = self._stamps.pop(0)
        self.calls.append(stamp)
        return stamp


class RecordingBackoff:
    """Zero-delay recording backoff: records the attempt numbers it is
    asked to compute the delay for, and always returns 0.0 (the tested
    path never sleeps)."""

    def __init__(self) -> None:
        self.attempts: list[int] = []
        self.delays: list[float] = []

    def __call__(self, attempt: int) -> float:
        self.attempts.append(attempt)
        self.delays.append(0.0)
        return 0.0


class FakeSlurmTransport(SSHTransport):
    """Scripted SSHTransport double for the Slurm adapter: no network,
    all behavior scripted.

    * ``connect_failures_left`` -- that many ``connect`` calls raise
      ``SSHConnectionError`` (scripted unreachable/refused host /
      authentication failure), then connect succeeds.
    * ``run_script`` -- a queue of ``RemoteResult`` (returned in order)
      or ``SSHTransportError`` (raised; an ``SSHTransferError`` also
      drops the session, ``connected`` -> False, simulating a
      mid-operation drop). The queue is shared by every remote command
      (mkdir, sbatch, squeue, sacct, scancel) in execution order.
    * ``pull_script`` -- a queue of ``None`` (write ``pull_payload`` to
      the local path, simulating a successful transfer) or exceptions
      (raised; an ``SSHTransferError`` also drops the session).
    * ``disconnect_errors_left`` -- that many ``disconnect`` calls raise
      (the adapter's ``_close_quietly`` must not let them mask a retry).
    * ``pushed`` -- the local content of every pushed batch script, so
      tests can assert the generated wrapper byte-for-byte.
    * ``log`` -- ordered call log of ``("connect"|"disconnect"|"run"|
      "push"|"pull", args)`` entries, making the reconnect observable.

    Strictness: operating while disconnected, connecting while already
    connected, or consuming an exhausted script raises (catches adapter
    logic errors).
    """

    def __init__(
        self,
        *,
        host: str = "cluster.example.edu",
        connect_failures_left: int = 0,
        connect_error: str | None = None,
        run_script: Sequence[RemoteResult | SSHTransportError] = (),
        pull_script: Sequence[BaseException | None] = (),
        pull_payload: str = "42",
        disconnect_errors_left: int = 0,
    ) -> None:
        self.host = host
        self.connected = False
        self.connect_failures_left = connect_failures_left
        self.connect_error = connect_error
        self.run_script = list(run_script)
        self.pull_script = list(pull_script)
        self.pull_payload = pull_payload
        self.disconnect_errors_left = disconnect_errors_left
        self.log: list[tuple[str, tuple[object, ...]]] = []
        self.pushed: list[tuple[Path, RemotePath, bytes]] = []

    # -- SSHTransport ------------------------------------------------------

    def connect(self) -> None:
        self.log.append(("connect", ()))
        if self.connected:
            raise SSHConnectionError("transport is already connected")
        if self.connect_failures_left > 0:
            self.connect_failures_left -= 1
            raise SSHConnectionError(
                self.connect_error
                if self.connect_error is not None
                else f"connection to host {self.host!r} refused"
            )
        self.connected = True

    def disconnect(self) -> None:
        self.log.append(("disconnect", ()))
        if self.disconnect_errors_left > 0:
            self.disconnect_errors_left -= 1
            raise SSHTransferError("ssh disconnect failed")
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def run_command(self, command: RemoteCommand) -> RemoteResult:
        self.log.append(("run", (command,)))
        if not self.connected:
            raise SSHConnectionError("transport is not connected")
        if not self.run_script:
            raise AssertionError("FakeSlurmTransport.run_script exhausted")
        entry = self.run_script.pop(0)
        if isinstance(entry, SSHTransportError):
            if isinstance(entry, SSHTransferError):
                self.connected = False  # mid-operation drop
            raise entry
        return entry

    def push_file(self, local_path: Path, remote_path: RemotePath) -> None:
        self.log.append(("push", (local_path, remote_path)))
        if not self.connected:
            raise SSHConnectionError("transport is not connected")
        self.pushed.append(
            (local_path, remote_path, local_path.read_bytes())
        )

    def pull_file(self, remote_path: RemotePath, local_path: Path) -> None:
        self.log.append(("pull", (remote_path, local_path)))
        if not self.connected:
            raise SSHConnectionError("transport is not connected")
        if not self.pull_script:
            raise AssertionError("FakeSlurmTransport.pull_script exhausted")
        entry = self.pull_script.pop(0)
        if isinstance(entry, SSHTransferError):
            self.connected = False  # mid-transfer drop
            raise entry
        if entry is not None:
            raise entry
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(self.pull_payload, encoding="utf-8")


def make_adapter(
    state_dir: Path,
    *,
    transport: FakeSlurmTransport | None = None,
    credentials: SSHCredentials | None = None,
    modules: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
    max_attempts: int = 3,
    backoff: RecordingBackoff | None = None,
    now: FakeClock | None = None,
) -> tuple[SlurmComputeAdapter, FakeSlurmTransport, FakeClock]:
    """Build an adapter with a fresh FakeSlurmTransport/FakeClock and a
    zero-delay recording backoff unless given."""
    transport = transport if transport is not None else FakeSlurmTransport()
    clock = now if now is not None else FakeClock(FIXED_STAMP)
    creds = credentials if credentials is not None else DEFAULT_CREDENTIALS
    policy = SSHRetryPolicy(
        max_attempts=max_attempts,
        backoff=backoff if backoff is not None else RecordingBackoff(),
    )
    return (
        SlurmComputeAdapter(
            creds,
            state_dir,
            transport=transport,
            modules=modules,
            environment=environment,
            retry_policy=policy,
            now=clock,
        ),
        transport,
        clock,
    )


def read_job_file(state_dir: Path, job_id: str) -> dict[str, object]:
    """The on-disk durable record as parsed JSON."""
    return json.loads(
        (state_dir / "jobs" / f"{job_id}.json").read_text(encoding="utf-8")
    )


def running_job(
    state_dir: Path, *, external_id: int = EXTERNAL_ID
) -> tuple[SlurmComputeAdapter, FakeSlurmTransport, RunContext, str]:
    """A submitted (running) job; returns (adapter, transport, context,
    job_id) with the transport's run_script consumed up to the launch."""
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {external_id}\n"
            ),
        ]
    )
    adapter, _, _ = make_adapter(state_dir, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    adapter.submit(ctx)
    return adapter, transport, ctx, prepared.job_id


def completed_job(
    state_dir: Path,
    *,
    outputs: tuple[str, ...] = ("result.txt",),
    exit_code: int = 0,
) -> tuple[SlurmComputeAdapter, FakeSlurmTransport, RunContext, str]:
    """A completed job (squeue empty, sacct COMPLETED) ready to collect;
    returns (adapter, transport, context, job_id)."""
    adapter, transport, ctx, job_id = running_job(state_dir)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: left the queue
            RemoteResult(
                exit_code=0, stdout=f"COMPLETED|{exit_code}:0\n"
            ),  # sacct: terminal
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    return adapter, transport, ctx, job_id


def run_commands(transport: FakeSlurmTransport) -> list[RemoteCommand]:
    """The remote commands executed so far, in order."""
    return [
        entry[1][0]
        for entry in transport.log
        if entry[0] == "run"
    ]


# ---------------------------------------------------------------------------
# AC-01: the external Slurm job id persists; fresh sessions recover it
# ---------------------------------------------------------------------------


def test_slurm_ac01_submit_persists_external_slurm_job_id(
    tmp_path: Path,
) -> None:
    """``submit`` parses the ``Submitted batch job <id>`` line into the
    record's first-class ``external_id`` field (AC-01): the durable
    record on disk carries the external Slurm job id, and the outcome
    record reports it."""
    adapter, transport, _, job_id = running_job(tmp_path)
    assert job_id == generate_id("job", make_run_id())
    record = read_job_file(tmp_path, job_id)
    assert record["external_id"] == EXTERNAL_ID
    assert record["state"] == "running"
    assert record["submitted_at"] == FIXED_STAMP
    # The sbatch invocation is a RemoteCommand whose argv names the
    # absolute batch script and the absolute job log.
    sbatch = [
        c
        for c in run_commands(transport)
        if c.argv[0] == "sbatch"
    ]
    assert len(sbatch) == 1
    shell = sbatch[0].to_shell()
    assert f"sbatch --chdir {REMOTE_WORKDIR}" in shell
    assert f".sr_{job_id}_slurm.sh" in shell
    assert f".sr_{job_id}_job.log" in shell
    assert adapter.read_job(job_id).external_id == EXTERNAL_ID


def test_slurm_ac01_submit_outcome_carries_external_id(tmp_path: Path) -> None:
    """The SlurmSubmittedJob outcome carries the parsed external id."""
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    assert isinstance(submitted, SlurmSubmittedJob)
    assert submitted.state is JobState.RUNNING
    assert submitted.external_id == EXTERNAL_ID
    assert submitted.submitted_at == FIXED_STAMP
    assert submitted.failure_class is None


def test_slurm_ac01_fresh_instance_statuses_collects_and_cancels_from_record(
    tmp_path: Path,
) -> None:
    """AC-01: a fresh adapter instance over the same state directory
    (its own transport, its own session) recovers the job from the
    durable record alone and drives status/collect/cancel -- the
    submitting Worker is never needed again."""
    _, _, _, job_id = running_job(tmp_path)
    fresh_transport = FakeSlurmTransport()
    fresh = SlurmComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path,
        transport=fresh_transport,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
    )
    # Status probes through the fresh transport, deriving --jobs from
    # the record's external id.
    fresh_transport.run_script.append(RemoteResult(exit_code=0, stdout="RUNNING\n"))
    status = fresh.status(job_id)
    assert isinstance(status, SlurmJobStatus)
    assert status.state is JobState.RUNNING
    assert status.external_id == EXTERNAL_ID
    probes = run_commands(fresh_transport)
    assert len(probes) == 1
    assert "squeue" in probes[0].argv and "--jobs" in probes[0].argv
    assert str(EXTERNAL_ID) in probes[0].argv
    # Cancel through the fresh transport: scancel derived from the
    # record's external id.
    fresh_transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # scancel accepted
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(
                exit_code=0, stdout="CANCELLED|0:0\n"
            ),  # sacct: confirmed
        ]
    )
    cancelled = fresh.cancel(job_id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.external_id == EXTERNAL_ID
    cancels = [
        c for c in run_commands(fresh_transport) if c.argv[0] == "scancel"
    ]
    assert [c.to_shell() for c in cancels] == [f"scancel {EXTERNAL_ID}"]
    # A terminal record is answered from the record alone.
    fresh_transport.log.clear()
    status = fresh.status(job_id)
    assert status.state is JobState.CANCELLED
    assert status.external_id == EXTERNAL_ID
    assert fresh_transport.log == []


def test_slurm_ac01_fresh_instance_collects_from_record(tmp_path: Path) -> None:
    """AC-01: a fresh adapter instance collects a completed job from the
    record alone (outputs and staging derived from the record)."""
    _, _, _, job_id = completed_job(tmp_path)
    fresh_transport = FakeSlurmTransport(pull_script=[None])
    fresh = SlurmComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path,
        transport=fresh_transport,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
    )
    collected = fresh.collect(job_id)
    assert isinstance(collected, SlurmCollectedJob)
    assert collected.state is JobState.COMPLETED
    assert len(collected.artifact_ids) == 1
    assert collected.artifact_ids == (
        generate_id("artifact", job_id, "result.txt"),
    )
    assert collected.artifacts[0].producer == "adapter:compute/slurm_ssh@v1.0"


def test_slurm_ac01_dropped_connection_is_transport_failure_not_job_failure(
    tmp_path: Path,
) -> None:
    """A scripted connection drop mid-submit, exhausted after the
    configured attempts, raises the TRANSPORT failure class with a stable
    message and is recorded on the durable record as
    ``failure_class="transport"`` -- never a job outcome (AC-01)."""
    backoff = RecordingBackoff()
    transport = FakeSlurmTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 3
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport, backoff=backoff)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SSHTransferError) as excinfo:
        adapter.submit(ctx)
    assert str(excinfo.value) == "ssh connection dropped mid-operation"
    assert isinstance(excinfo.value, SSHTransportError)
    assert not isinstance(excinfo.value, SSHRemoteError)
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["state"] == "prepared"
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT
    assert record["error"] == "ssh connection dropped mid-operation"
    assert record.get("external_id") is None


def test_slurm_ac01_unreachable_host_is_connection_failure(
    tmp_path: Path,
) -> None:
    """An unreachable host (connect refused) raises ``SSHConnectionError``
    after exactly the configured attempts, with the stable message, and
    the durable record carries ``failure_class="transport"`` (AC-01)."""
    backoff = RecordingBackoff()
    transport = FakeSlurmTransport(
        connect_failures_left=99,
        connect_error="connection to host 'cluster.example.edu' refused",
    )
    adapter, _, _ = make_adapter(
        tmp_path, transport=transport, backoff=backoff, max_attempts=3
    )
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SSHConnectionError) as excinfo:
        adapter.submit(ctx)
    assert isinstance(excinfo.value, SSHTransportError)
    assert (
        str(excinfo.value)
        == "connection to host 'cluster.example.edu' refused"
    )
    assert backoff.attempts == [1, 2]
    assert [entry[0] for entry in transport.log] == [
        "connect",
        "disconnect",
        "connect",
        "disconnect",
        "connect",
        "disconnect",
    ]
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT


def test_slurm_ac01_sbatch_refusal_is_job_level_not_transport(
    tmp_path: Path,
) -> None:
    """A clean remote refusal of the ``sbatch`` submission is the stable
    ``SlurmJobLaunchError`` (job-level, never retried), recorded on the
    durable record as ``failure_class="job"`` (AC-01)."""
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),  # mkdir accepted
            RemoteResult(
                exit_code=1,
                stderr="sbatch: error: Batch job submission failed:"
                " Invalid account",
            ),
        ]
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SlurmJobLaunchError) as excinfo:
        adapter.submit(ctx)
    assert "sbatch submission of job" in str(excinfo.value)
    assert "Invalid account" in str(excinfo.value)
    assert not isinstance(excinfo.value, SSHTransportError)
    # Not retried: exactly one sbatch attempt.
    sbatch_runs = [
        c for c in run_commands(transport) if c.argv[0] == "sbatch"
    ]
    assert len(sbatch_runs) == 1
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["state"] == "prepared"
    assert record["failure_class"] == FAILURE_CLASS_JOB
    assert "Invalid account" in str(record["error"])
    assert record.get("external_id") is None


def test_slurm_ac01_mkdir_refusal_is_launch_error_not_transport(
    tmp_path: Path,
) -> None:
    """A clean remote refusal of the working-directory creation is the
    stable ``SlurmJobLaunchError`` (job-level, never retried) (AC-01)."""
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(
                exit_code=1,
                stderr="mkdir: cannot create directory '/x': Permission denied",
            )
        ]
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context(working_directory="/x")
    prepared = adapter.prepare(ctx)
    with pytest.raises(SlurmJobLaunchError) as excinfo:
        adapter.submit(ctx)
    assert "failed with status 1" in str(excinfo.value)
    assert "Permission denied" in str(excinfo.value)
    assert not isinstance(excinfo.value, SSHTransportError)
    assert len(transport.log) == 3  # one session, no retry
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_JOB


def test_slurm_ac01_unparseable_submission_is_launch_error(
    tmp_path: Path,
) -> None:
    """An ``sbatch`` answer without a parseable ``Submitted batch job
    <id>`` line is the stable ``SlurmJobLaunchError`` (job-level, never
    retried) and never fabricates an external id (AC-01)."""
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="usage: sbatch [options]..."),
        ]
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SlurmJobLaunchError) as excinfo:
        adapter.submit(ctx)
    assert "did not produce a usable Slurm job id" in str(excinfo.value)
    assert not isinstance(excinfo.value, SSHTransportError)
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_JOB
    assert record.get("external_id") is None


def test_slurm_ac01_dropped_mid_pull_is_transfer_failure(tmp_path: Path) -> None:
    """A connection dropped mid-transfer during collect raises the
    TRANSPORT failure class and the durable record carries
    ``failure_class="transport"`` (AC-01)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.pull_script = [
        SSHTransferError("ssh connection dropped mid-transfer")
    ] * 3
    with pytest.raises(SSHTransferError) as excinfo:
        adapter.collect(job_id)
    assert str(excinfo.value) == "ssh connection dropped mid-transfer"
    assert isinstance(excinfo.value, SSHTransportError)
    record = read_job_file(tmp_path, job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT
    assert record["error"] == "ssh connection dropped mid-transfer"


def test_slurm_ac01_missing_remote_output_is_job_level_not_transport(
    tmp_path: Path,
) -> None:
    """A definitively absent remote output is a clean remote answer: it
    surfaces as the job-level ``ComputeCollectError`` (never an
    ``SSHTransportError``) and is never retried (AC-01 at the collect
    boundary)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.log.clear()  # collect is the only session under test
    transport.pull_script = [
        SSHRemoteFileNotFoundError("no such file: result.txt")
    ]
    with pytest.raises(ComputeCollectError) as excinfo:
        adapter.collect(job_id)
    assert "result.txt" in str(excinfo.value)
    assert not isinstance(excinfo.value, SSHTransportError)
    # Not retried: exactly one pull attempt, exactly one session.
    assert [entry[0] for entry in transport.log] == [
        "connect",
        "pull",
        "disconnect",
    ]
    record = read_job_file(tmp_path, job_id)
    assert record.get("failure_class") is None  # no transport classification


def test_slurm_ac01_status_of_failed_job_carries_classification(
    tmp_path: Path,
) -> None:
    """The status outcome record carries the AC-01 classification: a
    failed job's status says ``failure_class="job"``; a healthy running
    job's status says ``failure_class`` is None."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.append(RemoteResult(exit_code=0, stdout="RUNNING\n"))
    running = adapter.status(job_id)
    assert running.state is JobState.RUNNING
    assert running.failure_class is None
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="FAILED|1:0\n"),
        ]
    )
    failed = adapter.status(job_id)
    assert failed.state is JobState.FAILED
    assert failed.failure_class == FAILURE_CLASS_JOB
    assert failed.error == "slurm job failed: scheduler state 'FAILED'"


def test_slurm_ac01_failure_classification_survives_rehydration(
    tmp_path: Path,
) -> None:
    """The recorded classification is durable: a fresh adapter instance
    over the same state directory re-hydrates ``failure_class="transport"``
    from the record alone (AC-01 recovery discipline)."""
    transport = FakeSlurmTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 3
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SSHTransferError):
        adapter.submit(ctx)
    fresh = SlurmComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path,
        transport=FakeSlurmTransport(),
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
    )
    record = fresh.read_job(prepared.job_id)
    assert record.failure_class == FAILURE_CLASS_TRANSPORT
    assert record.error == "ssh connection dropped mid-operation"


def test_slurm_ac01_healthy_submit_clears_stale_transport_marker(
    tmp_path: Path,
) -> None:
    """The record's ``failure_class`` is the *current* classification,
    never history: after a transport failure is recorded, a fresh
    adapter with a healthy transport resubmits the same prepared job
    and the successful submission clears the stale marker."""
    bad = FakeSlurmTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 3
    )
    adapter_bad, _, _ = make_adapter(tmp_path, transport=bad)
    ctx = make_context()
    prepared = adapter_bad.prepare(ctx)
    with pytest.raises(SSHTransferError):
        adapter_bad.submit(ctx)
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT
    # Second attempt: healthy transport, same state dir, same job.
    good = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter_good, _, _ = make_adapter(tmp_path, transport=good)
    submitted = adapter_good.submit(ctx)
    assert submitted.state is JobState.RUNNING
    assert submitted.external_id == EXTERNAL_ID
    assert submitted.failure_class is None
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["state"] == "running"
    assert record.get("failure_class") is None
    assert record["external_id"] == EXTERNAL_ID


# ---------------------------------------------------------------------------
# AC-02: scheduler-state normalization through the ordered rule table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ACTIVE_SLURM_STATES)
def test_slurm_ac02_active_states_normalize_to_running(
    tmp_path: Path, token: str
) -> None:
    """Every active scheduler state (queued/suspended/completing/...)
    normalizes to ``running`` -- queued means alive and not terminal --
    with the raw observed state carried on the status outcome (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.append(RemoteResult(exit_code=0, stdout=f"{token}\n"))
    status = adapter.status(job_id)
    assert status.state is JobState.RUNNING
    assert status.scheduler_state == token
    assert status.failure_class is None
    # Not terminal: nothing persisted yet, the raw state stays a status
    # observation, and a later probe still contacts the scheduler.
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "running"
    assert "scheduler_state" not in record


@pytest.mark.parametrize("token", COMPLETED_SLURM_STATES)
def test_slurm_ac02_completed_states_normalize_to_completed(
    tmp_path: Path, token: str
) -> None:
    """``COMPLETED``/``CD`` from ``sacct`` normalize to ``completed``
    with the exit code parsed from ``ExitCode`` and the raw state
    persisted (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout=f"{token}|7:0\n"),
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 7
    assert status.failure_class is None
    assert status.scheduler_state == token
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "completed"
    assert record["exit_code"] == 7
    assert record["scheduler_state"] == token
    assert "recovery_note" not in record


@pytest.mark.parametrize("token", FAILED_SLURM_STATES)
def test_slurm_ac02_failed_states_normalize_to_failed(
    tmp_path: Path, token: str
) -> None:
    """Every failure scheduler state (timeout, node failure, OOM,
    preemption, ...) normalizes to ``failed`` with the JOB failure class
    and the stable message (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout=f"{token}|1:0\n"),
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.FAILED
    assert status.failure_class == FAILURE_CLASS_JOB
    assert status.error == f"slurm job failed: scheduler state {token!r}"
    assert status.exit_code == 1
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "failed"
    assert record["failure_class"] == FAILURE_CLASS_JOB
    assert record["scheduler_state"] == token
    assert record["error"] == f"slurm job failed: scheduler state {token!r}"


@pytest.mark.parametrize("token", CANCELLED_SLURM_STATES)
def test_slurm_ac02_cancelled_states_normalize_to_cancelled(
    tmp_path: Path, token: str
) -> None:
    """``CANCELLED``/``CA`` from ``sacct`` normalize to ``cancelled``
    (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout=f"{token}|0:0\n"),
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.CANCELLED
    assert status.failure_class is None
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "cancelled"
    assert record["scheduler_state"] == token


def test_slurm_ac02_unknown_state_defaults_to_failed(tmp_path: Path) -> None:
    """An unrecognized scheduler state (a future Slurm state) is a clean
    remote answer: the total default classifies it as a job-level failure
    carrying the raw state -- never a transport failure (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout="SQUEUE-GHOST\n"),
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.FAILED
    assert status.failure_class == FAILURE_CLASS_JOB
    assert status.error == "unrecognized scheduler state 'SQUEUE-GHOST'"
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "failed"
    assert record["scheduler_state"] == "SQUEUE-GHOST"


def test_slurm_ac02_squeue_active_state_never_queries_sacct(
    tmp_path: Path,
) -> None:
    """An active state observed through ``squeue`` answers the status
    with exactly one remote command -- ``sacct`` is never queried while
    the job is in the queue (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.log.clear()  # the status probe is the only op under test
    transport.run_script.append(RemoteResult(exit_code=0, stdout="RUNNING\n"))
    status = adapter.status(job_id)
    assert status.state is JobState.RUNNING
    assert [c.argv[0] for c in run_commands(transport)] == ["squeue"]


def test_slurm_ac02_terminal_record_answered_from_record_alone(
    tmp_path: Path,
) -> None:
    """Terminal decisions are persisted once; subsequent status calls
    answer from the record alone -- the transport is never contacted
    again (AC-02)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.log.clear()
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    assert transport.log == []


def test_slurm_ac02_job_left_scheduler_view_records_recovery_note(
    tmp_path: Path,
) -> None:
    """A job absent from both ``squeue`` and ``sacct`` (accounting lag)
    is recorded as completed with the stable recovery note; collection
    then verifies the declared outputs independently (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout=""),  # sacct: nothing yet
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code is None
    assert status.recovery_note == SLURM_STATE_UNAVAILABLE_NOTE
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "completed"
    assert record["recovery_note"] == SLURM_STATE_UNAVAILABLE_NOTE
    assert record.get("failure_class") is None
    # The declared output is still collected (independent verification).
    transport.pull_script = [None]
    collected = adapter.collect(job_id)
    assert len(collected.artifact_ids) == 1


def test_slurm_ac02_sacct_reason_suffix_and_exit_code_parsing(
    tmp_path: Path,
) -> None:
    """``sacct`` rows may carry a trailing reason (``CANCELLED by 1000``)
    and an ``ExitCode`` of ``code:signal``; the adapter strips the reason
    and parses the primary exit code (AC-02)."""
    adapter, transport, _, job_id = running_job(tmp_path / "a")
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="CANCELLED by 1000|0:0\n"),
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.CANCELLED
    assert status.scheduler_state == "CANCELLED"
    # A signal-only exit code field ("0:15") parses the primary code 0.
    adapter_b, transport_b, _, job_id_b = running_job(tmp_path / "b")
    transport_b.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="COMPLETED|0:15\n"),
        ]
    )
    status = adapter_b.status(job_id_b)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0


def test_slurm_ac02_identical_observations_byte_identical_decisions() -> None:
    """Normalization is deterministic: identical raw observations
    produce byte-identical decisions (AC-02)."""
    first = normalize_scheduler_state("pending")
    second = normalize_scheduler_state("PENDING\n")
    assert first == second
    assert first.to_dict() == second.to_dict()
    for token in (*ACTIVE_SLURM_STATES, "COMPLETED", "CANCELLED", "FAILED"):
        decision = normalize_scheduler_state(token)
        assert normalize_scheduler_state(token.lower()) == decision
    # The shipped table covers every non-prepared canonical JobState at
    # least once (prepare is a local state, never a scheduler state).
    covered = {rule.state for rule in SLURM_STATE_RULES}
    assert covered == {
        JobState.RUNNING,
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
    }


def test_slurm_normalize_scheduler_state_boundaries() -> None:
    """Normalization is TypeError at the type boundary and ValueError
    for empty observations (an empty observation means the scheduler had
    no row -- the caller handles it)."""
    with pytest.raises(TypeError):
        normalize_scheduler_state(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        normalize_scheduler_state(5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        normalize_scheduler_state("")
    with pytest.raises(ValueError):
        normalize_scheduler_state("   \n")
    decision = normalize_scheduler_state("  pending  ")
    assert decision.scheduler_state == "PENDING"
    assert decision.state is JobState.RUNNING
    assert decision.rule_id == "R-SLURM-S1"
    assert decision.unknown is False


def test_slurm_state_rule_table_is_well_formed_and_total() -> None:
    """The shipped rule table passes the totality validator (unique ids,
    unique uppercase states, exactly one trailing total default), ends
    with the default, and its ids are stable (AC-02)."""
    validate_slurm_state_rules(SLURM_STATE_RULES)
    assert SLURM_STATE_RULES[-1].scheduler_state is None
    assert SLURM_STATE_RULES[-1].rule_id == "R-SLURM-S27"
    assert SLURM_STATE_RULES[-1].state is JobState.FAILED
    assert SLURM_STATE_RULES[0].rule_id == "R-SLURM-S1"
    assert SLURM_STATE_RULES[0].scheduler_state == "PENDING"
    assert len(SLURM_STATE_RULES) == 27
    # The default is the only rule matching everything.
    assert sum(rule.scheduler_state is None for rule in SLURM_STATE_RULES) == 1


def test_slurm_state_rule_table_validator_rejects_malformed_tables() -> None:
    """The totality validator refuses empty tables, duplicate ids,
    duplicate states, non-uppercase states, missing or duplicated
    defaults, and a default that is not trailing (AC-02)."""
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules([])
    with pytest.raises(TypeError):
        validate_slurm_state_rules("not-a-sequence")
    with pytest.raises(TypeError):
        validate_slurm_state_rules([object()])  # type: ignore[list-item]
    base = SLURM_STATE_RULES[0]

    def table(*rules: SlurmStateRule) -> list[SlurmStateRule]:
        return list(rules)

    duplicate_id = table(
        base, SlurmStateRule(base.rule_id, "dup", "PENDING", JobState.RUNNING)
    )
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules(duplicate_id)
    duplicate_state = table(
        base, SlurmStateRule("R-X", "dup", "PENDING", JobState.RUNNING)
    )
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules(duplicate_state)
    lowercase = SlurmStateRule("R-X", "lower", "pending", JobState.RUNNING)
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules([base, lowercase])
    no_default = table(base)
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules(no_default)
    default = SlurmStateRule("R-D", "default", None, JobState.FAILED)
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules([default, default])
    non_trailing = table(default, base)
    with pytest.raises(SlurmAdapterError):
        validate_slurm_state_rules(non_trailing)


# ---------------------------------------------------------------------------
# AC-03: scientific input files are never modified by scheduler retries
# ---------------------------------------------------------------------------


def test_slurm_ac03_input_bytes_identical_after_retry_cycles(
    tmp_path: Path,
) -> None:
    """A real scientific input file is byte-identical after scripted
    transport-failure/retry cycles across submit and status, and its
    content never leaks into any state-directory file (AC-03)."""
    input_path = tmp_path / "inputs" / "data.csv"
    input_path.parent.mkdir(parents=True)
    original = b"temperature,pressure,time\n" * 400 + b"EOF\n"
    input_path.write_bytes(original)
    ctx = make_context(command=("python", "sim.py", "--input", "data.csv"))
    # Submit through one scripted connect failure, then a drop of the
    # mkdir session, each retried under the injected policy (attempt 1:
    # connect refused; attempt 2: connect ok, session drops; attempt 3:
    # reconnect, mkdir accepted).
    backoff = RecordingBackoff()
    transport = FakeSlurmTransport(
        connect_failures_left=1,
        connect_error="connection to host 'cluster.example.edu' refused",
        run_script=[
            SSHTransferError("ssh connection dropped mid-operation"),
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ],
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport, backoff=backoff)
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    assert submitted.state is JobState.RUNNING
    assert backoff.attempts == [1, 2]
    assert input_path.read_bytes() == original
    # Status through a dropped probe, retried, then completed.
    transport.run_script.extend(
        [
            SSHTransferError("ssh connection dropped mid-operation"),
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),
        ]
    )
    status = adapter.status(submitted.job_id)
    assert status.state is JobState.COMPLETED
    assert input_path.read_bytes() == original
    # Collect pulls only declared outputs -- the input is untouched.
    transport.pull_script = [None]
    adapter.collect(submitted.job_id)
    assert input_path.read_bytes() == original
    # The input's content appears in no state-directory file (records,
    # scripts, manifests, staging).
    marker = b"temperature,pressure,time"
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != input_path:
            assert marker not in path.read_bytes(), (
                f"{path} absorbed the scientific input content"
            )
    staged = list((tmp_path / "staging" / submitted.job_id).rglob("*"))
    assert [p.name for p in staged if p.is_file()] == ["result.txt"]


def test_slurm_ac03_input_bytes_identical_after_failed_outcome(
    tmp_path: Path,
) -> None:
    """The scientific input file is byte-identical after a FAILED job
    outcome and after a launch refusal (AC-03)."""
    input_path = tmp_path / "inputs" / "data.csv"
    input_path.parent.mkdir(parents=True)
    original = b"SENSITIVE-SCIENTIFIC-PAYLOAD\n" * 50
    input_path.write_bytes(original)
    ctx = make_context(command=("python", "sim.py", "--input", "data.csv"))
    # Scenario A: the job fails on the cluster (sacct FAILED).
    adapter_a, transport_a, _, job_id_a = running_job(tmp_path)
    transport_a.run_script.extend(
        [
            SSHTransferError("ssh connection dropped mid-operation"),
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="FAILED|1:0\n"),
        ]
    )
    status = adapter_a.status(job_id_a)
    assert status.state is JobState.FAILED
    assert status.failure_class == FAILURE_CLASS_JOB
    assert input_path.read_bytes() == original
    # Scenario B: the submission is refused by the scheduler.
    transport_b = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=1, stderr="sbatch: error: Slurm unavailable"),
        ]
    )
    adapter_b, _, _ = make_adapter(tmp_path / "b", transport=transport_b)
    adapter_b.prepare(ctx)
    with pytest.raises(SlurmJobLaunchError):
        adapter_b.submit(ctx)
    assert input_path.read_bytes() == original
    # The marker appears in no state-directory file of either state dir.
    marker = b"SENSITIVE-SCIENTIFIC-PAYLOAD"
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != input_path:
            assert marker not in path.read_bytes(), (
                f"{path} absorbed the scientific input content"
            )


# ---------------------------------------------------------------------------
# Modules/environment execution metadata
# ---------------------------------------------------------------------------


def test_slurm_launch_script_contains_modules_environment_and_status_capture(
    tmp_path: Path,
) -> None:
    """The generated batch script is a launch wrapper: module loads
    before environment exports before the shell-quoted command, with the
    command's exit status captured in the remote status file and
    re-exited so the scheduler's terminal state matches (Modules-aware
    execution metadata)."""
    modules = ("gcc/13.2.0", "python/3.11.5")
    environment = {"SIM_PARAM": "a b", "DEBUG": "0"}
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter, _, _ = make_adapter(
        tmp_path, transport=transport, modules=modules, environment=environment
    )
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    adapter.submit(ctx)
    assert len(transport.pushed) == 1
    pushed_path, remote_path, script = transport.pushed[0]
    # The script is staged locally and pushed to the remote workdir.
    assert pushed_path == (
        tmp_path / "scripts" / f"{prepared.job_id}.slurm.sh"
    )
    assert pushed_path.is_file()
    assert pushed_path.read_bytes() == script
    assert str(remote_path) == f"{REMOTE_WORKDIR}/.sr_{prepared.job_id}_slurm.sh"
    text = script.decode("utf-8")
    lines = text.splitlines()
    assert lines[0] == "#!/bin/bash"
    # Order: module loads, then exports, then the status-capturing
    # command line.
    module_index = [i for i, line in enumerate(lines) if line.startswith("module load")]
    export_index = [i for i, line in enumerate(lines) if line.startswith("export ")]
    command_index = next(
        i for i, line in enumerate(lines) if "sim.py" in line
    )
    assert lines[module_index[0]] == "module load gcc/13.2.0"
    assert lines[module_index[1]] == "module load python/3.11.5"
    # Environment exports are key-sorted (deterministic script bytes):
    # DEBUG sorts before SIM_PARAM.
    assert lines[export_index[0]] == "export DEBUG='0'"
    assert lines[export_index[1]] == "export SIM_PARAM='a b'"
    assert max(module_index) < min(export_index) < command_index
    # The command is shell-quoted; the status file path is quoted (only
    # when needed -- this path carries no whitespace, so shlex.quote
    # leaves it bare); the wrapper exits with the captured code (so
    # sacct's terminal state always agrees with the command's status).
    assert "python sim.py --param 'a b'" in lines[command_index]
    assert (
        f"echo $_sr_exit_code > {REMOTE_WORKDIR}/.sr_{prepared.job_id}_job.status"
        in lines[command_index]
    )
    assert "exit $_sr_exit_code" in lines[command_index]
    assert "set -e" in text  # module-load failures abort the launch
    assert "set +e" in text  # the command's own status is captured
    # A space-bearing working directory is quoted inside the wrapper
    # (whitespace never breaks the remote shell line).
    transport2 = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter2, _, _ = make_adapter(
        tmp_path / "spaces",
        transport=transport2,
        modules=modules,
        environment=environment,
    )
    ctx2 = make_context(working_directory="/home/alice/my work")
    adapter2.prepare(ctx2)
    adapter2.submit(ctx2)
    pushed2 = transport2.pushed[0][2].decode("utf-8")
    assert (
        f"echo $_sr_exit_code >"
        f" '/home/alice/my work/.sr_{prepared.job_id}_job.status'"
        in pushed2
    )


def test_slurm_record_captures_modules_and_environment(tmp_path: Path) -> None:
    """The durable record captures the modules and the key-sorted
    environment snapshot as stable serializable fields, and the prepared
    outcome carries them (Modules-aware execution metadata)."""
    environment = {"Z_LAST": "9", "A_FIRST": "1", "SIM_PARAM": "a b"}
    adapter, transport, ctx, job_id = running_job(tmp_path)
    # The record written by a modules/environment-aware adapter.
    adapter_mod = make_adapter(
        tmp_path / "mod",
        modules=("gcc/13.2.0",),
        environment=environment,
    )[0]
    ctx_mod = make_context()
    prepared = adapter_mod.prepare(ctx_mod)
    assert isinstance(prepared, SlurmPreparedJob)
    assert prepared.modules == ("gcc/13.2.0",)
    assert prepared.environment == (
        ("A_FIRST", "1"),
        ("SIM_PARAM", "a b"),
        ("Z_LAST", "9"),
    )
    record = read_job_file(tmp_path / "mod", prepared.job_id)
    assert record["modules"] == ["gcc/13.2.0"]
    assert record["environment"] == [
        ["A_FIRST", "1"],
        ["SIM_PARAM", "a b"],
        ["Z_LAST", "9"],
    ]
    # The metadata survives the full cycle (status/collect) unchanged.
    transport_mod = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter_mod2, _, _ = make_adapter(
        tmp_path / "mod",
        transport=transport_mod,
        modules=("gcc/13.2.0",),
        environment=environment,
    )
    adapter_mod2.submit(ctx_mod)
    transport_mod.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),
        ]
    )
    adapter_mod2.status(prepared.job_id)
    record = read_job_file(tmp_path / "mod", prepared.job_id)
    assert record["modules"] == ["gcc/13.2.0"]
    assert record["environment"] == [
        ["A_FIRST", "1"],
        ["SIM_PARAM", "a b"],
        ["Z_LAST", "9"],
    ]
    assert adapter.environment == () and adapter.modules == ()  # defaults


def test_slurm_restage_with_different_modules_rejected(tmp_path: Path) -> None:
    """Re-staging the same run with a different module set is rejected:
    job identity is a pure function of the run id, and the module set is
    part of the staged content."""
    adapter, _, _ = make_adapter(tmp_path, modules=("gcc/13.2.0",))
    ctx = make_context()
    adapter.prepare(ctx)
    adapter_other, _, _ = make_adapter(
        tmp_path, modules=("python/3.11.5",)
    )
    with pytest.raises(ComputeJobStateError):
        adapter_other.prepare(ctx)
    # Identical restage (same modules) is accepted.
    adapter_same, _, _ = make_adapter(tmp_path, modules=("gcc/13.2.0",))
    prepared = adapter_same.prepare(ctx)
    assert prepared.state is JobState.PREPARED


def test_slurm_module_name_validation(tmp_path: Path) -> None:
    """Unsafe module names are rejected at the adapter boundary with the
    stable identity error (shell metacharacters, whitespace, relative
    segments, leading '-'); safe names pass."""
    for bad in (
        "gcc; rm -rf /",
        "..",
        ".",
        "",
        "-gcc",
        "gcc 13",
        "gcc\n13",
        "gcc&x",
        "gcc>x",
        "gcc|echo",
        "gcc$(id)",
        "gcc'x",
        "gcc\"x",
        "gcc\\x",
        "gcc!x",
        "gcc~x",
        "gcc*x",
        "gcc?x",
        "gcc[x]",
        "gcc{x}",
        "gcc#x",
        "gcc`x",
        "g\x00cc",
    ):
        with pytest.raises(SlurmJobIdentityError):
            SlurmComputeAdapter(
                DEFAULT_CREDENTIALS,
                tmp_path,
                transport=FakeSlurmTransport(),
                modules=(bad,),
            )
    for good in ("gcc/13.2.0", "python", "a.b-c_d+@", "openmpi/4.1.6"):
        adapter, _, _ = make_adapter(tmp_path, modules=(good,))
        assert adapter.modules == (good,)
    with pytest.raises(TypeError):
        SlurmComputeAdapter(
            DEFAULT_CREDENTIALS,
            tmp_path,
            transport=FakeSlurmTransport(),
            modules="gcc",  # not a tuple
        )
    with pytest.raises(TypeError):
        SlurmComputeAdapter(
            DEFAULT_CREDENTIALS,
            tmp_path,
            transport=FakeSlurmTransport(),
            modules=("gcc", 13),
        )


def test_slurm_environment_validation(tmp_path: Path) -> None:
    """Unsafe environment variable names/values are rejected with the
    stable identity error; wrong-typed boundaries are TypeError."""
    for bad_key in ("1ABC", "A-B", "A B", "A.B", ""):
        with pytest.raises(SlurmJobIdentityError):
            make_adapter(tmp_path, environment={bad_key: "1"})
    with pytest.raises(SlurmJobIdentityError):
        make_adapter(tmp_path, environment={"A": "has\nnewline"})
    with pytest.raises(SlurmJobIdentityError):
        make_adapter(tmp_path, environment={"A": "has\x00nul"})
    with pytest.raises(TypeError):
        make_adapter(tmp_path, environment={"A": 5})
    with pytest.raises(TypeError):
        make_adapter(tmp_path, environment=["A=1"])
    adapter, _, _ = make_adapter(
        tmp_path,
        environment={"A": "a b'c", "B": "x$y"},
    )
    assert adapter.environment == (("A", "a b'c"), ("B", "x$y"))


def test_slurm_durable_record_schema_has_no_credential_fields(
    tmp_path: Path,
) -> None:
    """The durable record schema carries no credential fields: the
    persisted JSON keys are exactly the documented contract (identity,
    state, remote execution facts, modules/environment metadata,
    classification) -- nothing credential-shaped."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.pull_script = [None]
    adapter.collect(job_id)
    record = read_job_file(tmp_path, job_id)
    assert set(record.keys()) == {
        "record_version",
        "backend",
        "job_id",
        "run_id",
        "state",
        "command",
        "working_directory",
        "outputs",
        "created_at",
        "modules",
        "environment",
        "submitted_at",
        "external_id",
        "scheduler_state",
        "exit_code",
        "completed_at",
        "collected_at",
        "artifact_ids",
    }
    text = json.dumps(record)
    for forbidden in (
        "password",
        "passphrase",
        "private",
        "credential",
        "secret",
        "token",
    ):
        assert forbidden not in text.lower()


def test_slurm_credentials_live_only_on_adapter_boundary(tmp_path: Path) -> None:
    """Credentials are accepted at the adapter constructor boundary and
    never appear in any state-directory file, remote command or pushed
    script (the same discipline as the ssh adapter)."""
    adapter, transport, _ = make_adapter(tmp_path)
    assert adapter.credentials is DEFAULT_CREDENTIALS
    ctx = make_context()
    adapter.prepare(ctx)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter.submit(ctx)
    secrets = [
        DEFAULT_CREDENTIALS.password,
        DEFAULT_CREDENTIALS.key_passphrase,
        DEFAULT_CREDENTIALS.private_key_path,
        DEFAULT_CREDENTIALS.username,
    ]
    state_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert state_files, "the state directory must contain files to walk"
    for path in state_files:
        content = path.read_text(encoding="utf-8", errors="replace")
        for secret in secrets:
            assert secret not in content, f"{path} contains credential {secret!r}"
    for entry_name, entry_args in transport.log:
        text = repr((entry_name, entry_args))
        for secret in secrets:
            assert secret not in text, f"transport log embeds credential: {text}"
    for _, _, script in transport.pushed:
        text = script.decode("utf-8", errors="replace")
        for secret in secrets:
            assert secret not in text, "pushed script embeds a credential"


# ---------------------------------------------------------------------------
# The six-operation ComputeAdapter interface over Slurm-over-SSH
# ---------------------------------------------------------------------------


def test_slurm_full_interface_prepare_submit_status_collect_cancel_resume(
    tmp_path: Path,
) -> None:
    """The full ComputeAdapter interface works end to end on one remote
    Slurm job: prepare stages (no remote contact), submit pushes the
    batch script and invokes sbatch (external id persisted), status
    probes squeue/sacct, collect pulls outputs and registers artifacts
    through the real registry, resume re-attaches to the durable record,
    and cancelling a terminal job is rejected."""
    adapter, transport, clock = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    assert isinstance(prepared, SlurmPreparedJob)
    assert prepared.state is JobState.PREPARED
    assert prepared.job_id == generate_id("job", ctx.run_id)
    assert prepared.working_directory == REMOTE_WORKDIR
    assert prepared.command == ctx.command
    assert prepared.outputs == ctx.outputs
    assert prepared.created_at == FIXED_STAMP
    assert prepared.modules == ()
    assert prepared.environment == ()
    assert prepared.failure_class is None
    assert read_job_file(tmp_path, prepared.job_id)["state"] == "prepared"
    assert transport.log == []  # prepare never touches the remote

    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    submitted = adapter.submit(ctx)
    assert isinstance(submitted, SlurmSubmittedJob)
    assert submitted.job_id == prepared.job_id
    assert submitted.state is JobState.RUNNING
    assert submitted.external_id == EXTERNAL_ID
    assert submitted.submitted_at == FIXED_STAMP
    # The remote operation order is mkdir, push, sbatch -- each its own
    # session, in that order.
    operations = [entry[0] for entry in transport.log]
    assert operations == [
        "connect",
        "run",
        "disconnect",
        "connect",
        "push",
        "disconnect",
        "connect",
        "run",
        "disconnect",
    ]
    sbatch = [c for c in run_commands(transport) if c.argv[0] == "sbatch"]
    assert len(sbatch) == 1
    assert f"--chdir {REMOTE_WORKDIR}" in sbatch[0].to_shell()

    transport.run_script.append(RemoteResult(exit_code=0, stdout="RUNNING\n"))
    status = adapter.status(submitted.job_id)
    assert isinstance(status, SlurmJobStatus)
    assert status.state is JobState.RUNNING
    assert status.external_id == EXTERNAL_ID
    assert status.scheduler_state == "RUNNING"
    assert status.failure_class is None

    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),  # sacct
        ]
    )
    status = adapter.status(submitted.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0
    assert status.scheduler_state == "COMPLETED"

    transport.pull_script = [None]
    collected = adapter.collect(submitted.job_id)
    assert len(collected.artifact_ids) == 1
    artifact_id = generate_id("artifact", submitted.job_id, "result.txt")
    assert collected.artifact_ids == (artifact_id,)
    manifest = collected.artifacts[0]
    assert manifest.artifact_id == artifact_id
    assert manifest.sha256 == compute_sha256(
        tmp_path / "staging" / submitted.job_id / "result.txt"
    )
    assert manifest.metadata["backend"] == SLURM_BACKEND_NAME
    assert manifest.producer == "adapter:compute/slurm_ssh@v1.0"
    assert (tmp_path / "manifests" / f"{artifact_id}.json").is_file()

    resumed = adapter.resume(submitted.job_id)
    assert isinstance(resumed, SlurmResumedJob)
    assert resumed.state is JobState.COMPLETED
    assert resumed.external_id == EXTERNAL_ID
    assert resumed.failure_class is None

    with pytest.raises(ComputeJobStateError):
        adapter.cancel(submitted.job_id)

    # created (prepare), submitted (submit), both transition probes
    # (status calls), and collect's manifest created_at + collected_at:
    # 6 clock reads, all identical.
    assert clock.calls == [FIXED_STAMP] * 6


def test_slurm_identical_inputs_produce_byte_identical_records(
    tmp_path: Path,
) -> None:
    """Identical injected inputs (modules/environment included) produce
    byte-identical durable records, batch scripts and status outcomes
    (deterministic protocol capture)."""
    records: list[bytes] = []
    scripts: list[bytes] = []
    statuses: list[dict[str, object]] = []
    for index in ("1", "2"):
        state = tmp_path / index
        adapter, transport, _ = make_adapter(
            state,
            modules=("gcc/13.2.0",),
            environment={"SIM_PARAM": "a b"},
        )
        ctx = make_context()
        prepared = adapter.prepare(ctx)
        transport.run_script.extend(
            [
                RemoteResult(exit_code=0, stdout=""),
                RemoteResult(
                    exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
                ),
            ]
        )
        submitted = adapter.submit(ctx)
        transport.run_script.extend(
            [
                RemoteResult(exit_code=0, stdout=""),
                RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),
            ]
        )
        status = adapter.status(submitted.job_id)
        records.append(
            (state / "jobs" / f"{prepared.job_id}.json").read_bytes()
        )
        scripts.append(
            (state / "scripts" / f"{prepared.job_id}.slurm.sh").read_bytes()
        )
        statuses.append(status.to_dict())
    assert records[0] == records[1]
    assert scripts[0] == scripts[1]
    assert statuses[0] == statuses[1]


def test_slurm_status_of_terminal_record_is_durable_record_driven(
    tmp_path: Path,
) -> None:
    """Terminal states are answered from the durable record alone: the
    transport is never contacted again for a completed/failed/cancelled
    record."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.log.clear()
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    assert transport.log == []


def test_slurm_submit_requires_prepared_job(tmp_path: Path) -> None:
    """Submitting without prepare, or submitting twice, is rejected with
    the stable state error."""
    adapter, transport, _ = make_adapter(tmp_path)
    ctx = make_context()
    with pytest.raises(ComputeJobNotFoundError):
        adapter.submit(ctx)
    adapter.prepare(ctx)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter.submit(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.submit(ctx)


def test_slurm_prepare_rejects_restaging_different_content(
    tmp_path: Path,
) -> None:
    """Re-staging the same run with different content is rejected: job
    identity is a pure function of the run id."""
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context(command=("python", "sim.py"))
    adapter.prepare(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.prepare(make_context(command=("python", "other.py")))


def test_slurm_collect_requires_completed_job(tmp_path: Path) -> None:
    """Collect requires a completed job (stable state error)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    assert transport.log
    with pytest.raises(ComputeJobStateError):
        adapter.collect(job_id)


def test_slurm_collect_recollect_is_idempotent(tmp_path: Path) -> None:
    """Re-collecting an already-collected job returns the same
    registrations without pulling again or rewriting the record."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.pull_script = [None]
    first = adapter.collect(job_id)
    transport.log.clear()
    second = adapter.collect(job_id)
    assert second.artifact_ids == first.artifact_ids
    assert second.artifacts == first.artifacts
    assert transport.log == []  # idempotent re-collect: no remote contact


def test_slurm_resume_from_durable_record_alone(tmp_path: Path) -> None:
    """A fresh adapter instance over the same state directory recovers a
    running job from the durable record alone and probes its scheduler
    state through its own transport (AC-01/AC-02 recovery)."""
    _, _, _, job_id = running_job(tmp_path)
    fresh_transport = FakeSlurmTransport()
    fresh = SlurmComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path,
        transport=fresh_transport,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
    )
    fresh_transport.run_script.append(RemoteResult(exit_code=0, stdout="RUNNING\n"))
    resumed = fresh.resume(job_id)
    assert resumed.state is JobState.RUNNING
    assert resumed.external_id == EXTERNAL_ID
    fresh_transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),
        ]
    )
    resumed = fresh.resume(job_id)
    assert resumed.state is JobState.COMPLETED


def test_slurm_cancel_prepared_is_a_local_decision(tmp_path: Path) -> None:
    """Cancelling a prepared job never contacts the remote host."""
    adapter, transport, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    cancelled = adapter.cancel(prepared.job_id)
    assert isinstance(cancelled, SlurmCancelledJob)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancelled_at == FIXED_STAMP
    assert cancelled.failure_class is None
    assert transport.log == []


def test_slurm_cancel_running_scancels_and_records_cancellation(
    tmp_path: Path,
) -> None:
    """Cancelling a running job sends ``scancel <external_id>`` and
    records the cancellation; a scheduler-confirmed terminal state after
    scancel carries no recovery note."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # scancel accepted
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout="CANCELLED|0:0\n"),  # confirmed
        ]
    )
    cancelled = adapter.cancel(job_id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.external_id == EXTERNAL_ID
    assert cancelled.failure_class is None
    cancels = [
        c for c in run_commands(transport) if c.argv[0] == "scancel"
    ]
    assert [c.to_shell() for c in cancels] == [f"scancel {EXTERNAL_ID}"]
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "cancelled"
    assert record["cancelled_at"] == FIXED_STAMP
    assert "recovery_note" not in record


def test_slurm_cancel_running_with_unconfirmed_termination_records_note(
    tmp_path: Path,
) -> None:
    """When the post-scancel probe cannot confirm the termination (the
    job left the queue but accounting has not reported it yet), the
    cancellation carries the stable TERMINATE_PENDING_NOTE."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # scancel accepted
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout=""),  # sacct: not yet
        ]
    )
    cancelled = adapter.cancel(job_id)
    assert cancelled.state is JobState.CANCELLED
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "cancelled"
    assert record["recovery_note"] == TERMINATE_PENDING_NOTE


def test_slurm_cancel_requires_non_terminal_job(tmp_path: Path) -> None:
    """Cancelling a terminal job is rejected (stable state error)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.log.clear()
    with pytest.raises(ComputeJobStateError):
        adapter.cancel(job_id)


def test_slurm_adapter_requires_transport_and_valid_boundaries(
    tmp_path: Path,
) -> None:
    """The adapter boundary is strict: a transport is required, and
    wrong-typed credentials/transport are TypeError."""
    with pytest.raises(TypeError):
        SlurmComputeAdapter(DEFAULT_CREDENTIALS, tmp_path)
    with pytest.raises(TypeError):
        SlurmComputeAdapter(
            "not-credentials", tmp_path, transport=FakeSlurmTransport()
        )
    with pytest.raises(TypeError):
        SlurmComputeAdapter(
            DEFAULT_CREDENTIALS, tmp_path, transport=object()
        )
    adapter, _, _ = make_adapter(tmp_path)
    assert adapter.credentials is DEFAULT_CREDENTIALS
    assert isinstance(adapter.transport, FakeSlurmTransport)
    assert isinstance(adapter.retry_policy, SSHRetryPolicy)


def test_slurm_prepare_rejects_unsafe_remote_paths(tmp_path: Path) -> None:
    """Remote working directories are validated before any command
    construction: relative paths, NUL bytes and unsafe declared output
    names are rejected with the stable identity error."""
    adapter, _, _ = make_adapter(tmp_path)
    # Working-directory validation comes from the shared RemotePath
    # discipline (the ssh adapter's stable identity error).
    with pytest.raises(SSHJobIdentityError):
        adapter.prepare(make_context(working_directory="relative/dir"))
    with pytest.raises(SSHJobIdentityError):
        adapter.prepare(make_context(working_directory="/has\x00nul"))
    with pytest.raises(SlurmJobIdentityError):
        adapter.prepare(make_context(outputs=("out name.txt",)))
    with pytest.raises((ComputeJobIdentityError, SlurmJobIdentityError)):
        adapter.prepare(make_context(outputs=("../../escape.txt",)))
    with pytest.raises((ComputeJobIdentityError, SlurmJobIdentityError)):
        adapter.prepare(make_context(outputs=("glob*.txt",)))
    with pytest.raises((ComputeJobIdentityError, SlurmJobIdentityError)):
        adapter.prepare(make_context(working_directory="/ok", outputs=(123,)))


def test_slurm_read_job_validation(tmp_path: Path) -> None:
    """read_job rejects malformed job ids and missing records with the
    stable errors."""
    adapter, _, _ = make_adapter(tmp_path)
    with pytest.raises(SlurmJobIdentityError):
        adapter.read_job("not-a-job-id")
    with pytest.raises(ComputeJobNotFoundError):
        adapter.read_job(generate_id("job", "never-prepared"))


# ---------------------------------------------------------------------------
# The durable record contract
# ---------------------------------------------------------------------------


def test_slurm_durable_record_roundtrip_from_dict(tmp_path: Path) -> None:
    """The durable record round-trips through to_dict/from_dict with the
    external id, modules, environment, scheduler state and classification
    intact."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    record = SlurmJobRecord.from_dict(raw)
    assert record.job_id == job_id
    assert record.state is JobState.COMPLETED
    assert record.external_id == EXTERNAL_ID
    assert record.scheduler_state == "COMPLETED"
    assert record.exit_code == 0
    assert record.failure_class is None
    assert SlurmJobRecord.from_dict(record.to_dict()) == record
    # A running record with modules/environment round-trips too.
    adapter_mod, _, _, _ = running_job(tmp_path / "mod")
    record_mod = adapter_mod.read_job(generate_id("job", make_run_id()))
    assert record_mod.external_id == EXTERNAL_ID


def test_slurm_record_rejects_wrong_backend(tmp_path: Path) -> None:
    """Records of another backend are refused by the slurm record
    contract."""
    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["backend"] = "ssh"
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)


def test_slurm_record_rejects_bad_failure_class(tmp_path: Path) -> None:
    """An unknown ``failure_class`` is refused by the record contract."""
    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["failure_class"] = "warp-speed"
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)


def test_slurm_record_rejects_unsafe_output_names(tmp_path: Path) -> None:
    """A persisted record whose declared output is not a safe remote path
    segment is corrupt (the FND-M9-G02-01 discipline also protects the
    record contract)."""
    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["outputs"] = ["bad name.txt"]
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)


def test_slurm_record_rejects_unsafe_modules_and_environment(
    tmp_path: Path,
) -> None:
    """A persisted record with an unsafe module name or environment
    variable name is corrupt (the record contract re-validates the
    captured execution metadata)."""
    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["modules"] = ["gcc; rm -rf /"]
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)
    raw = read_job_file(tmp_path, job_id)
    raw["environment"] = [["1BAD", "1"]]
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)
    raw = read_job_file(tmp_path, job_id)
    raw["environment"] = [["A", "has\nnewline"]]
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)


def test_slurm_record_rejects_invalid_external_id(tmp_path: Path) -> None:
    """A persisted record whose external id is not a positive int is
    corrupt."""
    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["external_id"] = 0
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)
    raw = read_job_file(tmp_path, job_id)
    raw["external_id"] = -5
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)
    raw = read_job_file(tmp_path, job_id)
    raw["external_id"] = "423554"
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)


def test_slurm_record_rejects_missing_and_unknown_fields(
    tmp_path: Path,
) -> None:
    """The record contract refuses records missing required fields,
    with a mismatched record version, or with an unknown state."""
    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    del raw["command"]
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)
    raw = read_job_file(tmp_path, job_id)
    raw["record_version"] = "0.9"
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)
    raw = read_job_file(tmp_path, job_id)
    raw["state"] = "teleported"
    with pytest.raises(ComputeJobRecordError):
        SlurmJobRecord.from_dict(raw)


def test_slurm_status_outcome_serializes_without_secrets(
    tmp_path: Path,
) -> None:
    """The status outcome dict carries the external id and observed
    scheduler state, and never credential-shaped fields."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.append(RemoteResult(exit_code=0, stdout="PENDING\n"))
    status = adapter.status(job_id)
    data = status.to_dict()
    assert data["state"] == "running"
    assert data["external_id"] == EXTERNAL_ID
    assert data["scheduler_state"] == "PENDING"
    for forbidden in ("password", "passphrase", "private", "credential"):
        assert forbidden not in json.dumps(data).lower()
