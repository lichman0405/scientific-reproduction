"""Tests for the ssh ComputeAdapter transport layer (DEV-M7-G03,
deliverable).

Direct per-AC coverage, named after the acceptance criteria:

* ``test_ssh_ac01_*`` -- transient connection failure is distinguishable
  from scientific job failure: transport-level errors (unreachable host,
  authentication failure, connection dropped mid-transfer, timeout)
  raise/record the TRANSPORT failure class (``SSHTransportError``
  subclasses, ``failure_class="transport"`` on the durable record and on
  the outcome records) with stable, distinct messages, while a
  remotely-completed job with a non-zero remote exit code records the
  JOB failure class (``failure_class="job"``, stable error ``remote
  command exited with status <N>``). A clean remote answer (missing
  remote file, launch refusal) is never classified as a transport
  failure and never retried.
* ``test_ssh_ac02_*`` -- credentials are not persisted in project
  state: passwords, key passphrases and private key references are
  accepted at the adapter constructor boundary, live only in memory and
  never appear in any state directory file, durable record field,
  remote command or transferred path.
* ``test_ssh_ac03_*`` -- mock SSH integration covers reconnect
  behavior: a scripted transport double simulates a dropped connection
  mid-operation; the adapter reconnects and retries the pending
  operation under a deterministic injected retry policy (max attempts +
  zero-delay recording backoff). Reconnect-success, permanent-failure,
  exact retry counts and the reconnect call log are all asserted.

Interface and abstraction tests (``test_ssh_*``) cover the full
six-operation ComputeAdapter contract over the scripted transport, the
durable record contract (backend ``"ssh"``, no credential fields,
``failure_class``), the remote path and command abstraction (safe
segments, shell quoting) and the injectable surfaces.

Determinism: every test injects a scripted :class:`FakeTransport` (no
network), a :class:`FakeClock` producing the fixed ``FIXED_STAMP``
timestamp (no wall clock), a zero-delay backoff (no sleeps) and
``tmp_path`` state directories. No randomness, no network, no sleeps
anywhere.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest

from scientific_reproduction.adapters.compute.local import (
    ComputeCollectError,
    ComputeJobIdentityError,
    ComputeJobNotFoundError,
    ComputeJobRecordError,
    ComputeJobStateError,
    JobState,
    RunContext,
)
from scientific_reproduction.adapters.compute.ssh import (
    FAILURE_CLASS_JOB,
    FAILURE_CLASS_TRANSPORT,
    SSH_EXIT_CODE_UNAVAILABLE_NOTE,
    RemoteCommand,
    RemotePath,
    RemoteResult,
    SSHComputeAdapter,
    SSHConnectionError,
    SSHCredentials,
    SSHJobIdentityError,
    SSHJobLaunchError,
    SSHJobStatus,
    SSHPreparedJob,
    SSHRemoteError,
    SSHRemoteFileNotFoundError,
    SSHResumedJob,
    SSHRetryPolicy,
    SSHSubmittedJob,
    SSHTimeoutError,
    SSHTransferError,
    SSHTransport,
    SSHTransportError,
    default_backoff,
)
from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.ids import generate_id

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: The default remote working directory of the fixtures.
REMOTE_WORKDIR = "/home/alice/scratch/work-1"

#: The default scripted credentials (secrets deliberately distinctive so
#: the AC-02 persistence walk can prove their absence).
DEFAULT_CREDENTIALS = SSHCredentials(
    host="cluster.example.edu",
    username="cred-user-77",
    password="s3cr3t-p@ssw0rd-9",
    private_key_path=r"C:\keys\id_ed25519",
    key_passphrase="pa55phrase-7-xyz",
)


def make_run_id(label: str = "goal-1") -> str:
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


class FakeTransport(SSHTransport):
    """Scripted SSHTransport double: no network, all behavior scripted.

    * ``connect_failures_left`` -- that many ``connect`` calls raise
      ``SSHConnectionError`` (scripted unreachable/refused host /
      authentication failure), then connect succeeds.
    * ``run_script`` -- a queue of ``RemoteResult`` (returned in order)
      or ``SSHTransportError`` (raised; an ``SSHTransferError`` also
      drops the session, ``connected`` -> False, simulating a
      mid-operation drop).
    * ``pull_script`` -- a queue of ``None`` (write ``pull_payload`` to
      the local path, simulating a successful transfer) or exceptions
      (raised; an ``SSHTransferError`` also drops the session).
    * ``disconnect_errors_left`` -- that many ``disconnect`` calls raise
      (the adapter's ``_close_quietly`` must not let them mask a retry).
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
            raise AssertionError("FakeTransport.run_script exhausted")
        entry = self.run_script.pop(0)
        if isinstance(entry, SSHTransportError):
            if isinstance(entry, SSHTransferError):
                self.connected = False  # mid-operation drop
            raise entry
        return entry

    def push_file(self, local_path: Path, remote_path: RemotePath) -> None:
        self.log.append(("push", (local_path, remote_path)))

    def pull_file(self, remote_path: RemotePath, local_path: Path) -> None:
        self.log.append(("pull", (remote_path, local_path)))
        if not self.connected:
            raise SSHConnectionError("transport is not connected")
        if not self.pull_script:
            raise AssertionError("FakeTransport.pull_script exhausted")
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
    transport: FakeTransport | None = None,
    credentials: SSHCredentials | None = None,
    max_attempts: int = 3,
    backoff: RecordingBackoff | None = None,
    now: FakeClock | None = None,
) -> tuple[SSHComputeAdapter, FakeTransport, FakeClock]:
    """Build an adapter with a fresh FakeTransport/FakeClock and a
    zero-delay recording backoff unless given."""
    transport = transport if transport is not None else FakeTransport()
    clock = now if now is not None else FakeClock(FIXED_STAMP)
    creds = credentials if credentials is not None else DEFAULT_CREDENTIALS
    policy = SSHRetryPolicy(
        max_attempts=max_attempts,
        backoff=backoff if backoff is not None else RecordingBackoff(),
    )
    return (
        SSHComputeAdapter(
            creds,
            state_dir,
            transport=transport,
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
    state_dir: Path, *, remote_pid: int = 4242
) -> tuple[SSHComputeAdapter, FakeTransport, RunContext, str]:
    """A submitted (running) job; returns (adapter, transport, context,
    job_id) with the transport's run_script consumed up to the launch."""
    transport = FakeTransport(
        run_script=[RemoteResult(exit_code=0, stdout=f"{remote_pid}\n")]
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
) -> tuple[SSHComputeAdapter, FakeTransport, RunContext, str]:
    """A completed job (remote exit 0) ready to collect; returns
    (adapter, transport, context, job_id)."""
    adapter, transport, ctx, job_id = running_job(state_dir)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=1),  # liveness: dead
            RemoteResult(exit_code=0, stdout="0\n"),  # status file: exit 0
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    return adapter, transport, ctx, job_id


# ---------------------------------------------------------------------------
# AC-01: transient connection failure vs scientific job failure
# ---------------------------------------------------------------------------


def test_ssh_ac01_dropped_connection_is_transport_failure_not_job_failure(
    tmp_path: Path,
) -> None:
    """A scripted connection drop mid-launch, exhausted after the
    configured attempts, raises the TRANSPORT failure class with a stable
    message and is recorded on the durable record as
    ``failure_class="transport"`` -- never a job outcome (AC-01)."""
    backoff = RecordingBackoff()
    transport = FakeTransport(
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


def test_ssh_ac01_nonzero_remote_exit_is_job_failure(
    tmp_path: Path,
) -> None:
    """A remotely-completed job with a non-zero remote exit code records
    the JOB failure class: state failed, ``failure_class="job"`` and the
    stable error ``remote command exited with status <N>`` on both the
    durable record and the status outcome -- no exception (AC-01)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=1),  # liveness: dead
            RemoteResult(exit_code=0, stdout="3\n"),  # status file: exit 3
        ]
    )
    status = adapter.status(job_id)
    assert isinstance(status, SSHJobStatus)
    assert status.state is JobState.FAILED
    assert status.exit_code == 3
    assert status.failure_class == FAILURE_CLASS_JOB
    assert status.error == "remote command exited with status 3"
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "failed"
    assert record["failure_class"] == FAILURE_CLASS_JOB
    assert record["error"] == "remote command exited with status 3"


def test_ssh_ac01_transport_and_job_failure_are_structurally_distinct(
    tmp_path: Path,
) -> None:
    """The two outcome classes are structurally distinct with stable,
    distinct messages: the dropped connection raises a transport error
    (record ``failure_class="transport"``), the non-zero remote exit is
    a failed record (``failure_class="job"``) -- and repeating each
    scenario yields byte-identical outcomes (AC-01)."""
    # Scenario A: dropped connection -> TRANSPORT failure class.
    state_a = tmp_path / "a"
    backoff_a = RecordingBackoff()
    transport_a = FakeTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 3
    )
    adapter_a, _, _ = make_adapter(
        state_a, transport=transport_a, backoff=backoff_a
    )
    ctx = make_context()
    prepared_a = adapter_a.prepare(ctx)
    with pytest.raises(SSHTransferError) as excinfo_a:
        adapter_a.submit(ctx)
    record_a = read_job_file(state_a, prepared_a.job_id)
    assert isinstance(excinfo_a.value, SSHTransportError)
    assert not isinstance(excinfo_a.value, SSHRemoteError)
    assert record_a["failure_class"] == FAILURE_CLASS_TRANSPORT
    # Scenario B: non-zero remote exit -> JOB failure class.
    state_b = tmp_path / "b"
    adapter_b, transport_b, _ = make_adapter(state_b)
    prepared_b = adapter_b.prepare(ctx)
    transport_b.run_script.append(RemoteResult(exit_code=0, stdout="4242\n"))
    adapter_b.submit(ctx)
    transport_b.run_script.extend(
        [
            RemoteResult(exit_code=1),
            RemoteResult(exit_code=0, stdout="3\n"),
        ]
    )
    status_b = adapter_b.status(prepared_b.job_id)
    record_b = read_job_file(state_b, prepared_b.job_id)
    assert status_b.state is JobState.FAILED
    assert status_b.failure_class == FAILURE_CLASS_JOB
    assert record_b["failure_class"] == FAILURE_CLASS_JOB
    # Distinct, stable messages.
    assert str(excinfo_a.value) == "ssh connection dropped mid-operation"
    assert status_b.error == "remote command exited with status 3"
    assert str(excinfo_a.value) != status_b.error
    # Repeat scenario A -> identical outcome (stability across runs).
    state_a2 = tmp_path / "a2"
    transport_a2 = FakeTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 3
    )
    adapter_a2, _, _ = make_adapter(
        state_a2, transport=transport_a2, backoff=RecordingBackoff()
    )
    adapter_a2.prepare(ctx)
    with pytest.raises(SSHTransferError) as excinfo_a2:
        adapter_a2.submit(ctx)
    assert str(excinfo_a2.value) == str(excinfo_a.value)


def test_ssh_ac01_unreachable_host_is_connection_failure(tmp_path: Path) -> None:
    """An unreachable host (connect refused) raises ``SSHConnectionError``
    after exactly the configured attempts, with the stable message, and
    the durable record carries ``failure_class="transport"`` (AC-01)."""
    backoff = RecordingBackoff()
    transport = FakeTransport(
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
    assert record["error"] == "connection to host 'cluster.example.edu' refused"


def test_ssh_ac01_authentication_failure_is_transport_class(
    tmp_path: Path,
) -> None:
    """An authentication failure at connect is the TRANSPORT failure
    class (``SSHConnectionError``) with a stable, distinct message --
    never a job outcome (AC-01)."""
    transport = FakeTransport(
        connect_failures_left=99,
        connect_error="authentication failed for user 'alice'",
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SSHConnectionError) as excinfo:
        adapter.submit(ctx)
    assert "authentication failed for user 'alice'" in str(excinfo.value)
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT
    assert "authentication failed" in str(record["error"])


def test_ssh_ac01_timeout_is_transport_class(tmp_path: Path) -> None:
    """A timeout is the TRANSPORT failure class (``SSHTimeoutError``)
    with a stable message (AC-01)."""
    transport = FakeTransport(
        run_script=[SSHTimeoutError("ssh operation timed out after 60s")] * 3
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SSHTimeoutError) as excinfo:
        adapter.submit(ctx)
    assert isinstance(excinfo.value, SSHTransportError)
    assert str(excinfo.value) == "ssh operation timed out after 60s"
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT


def test_ssh_ac01_dropped_mid_pull_is_transfer_failure(tmp_path: Path) -> None:
    """A connection dropped mid-transfer during collect raises the
    TRANSPORT failure class and the durable record carries
    ``failure_class="transport"`` (AC-01)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.pull_script = [SSHTransferError("ssh connection dropped mid-transfer")] * 3
    with pytest.raises(SSHTransferError) as excinfo:
        adapter.collect(job_id)
    assert str(excinfo.value) == "ssh connection dropped mid-transfer"
    assert isinstance(excinfo.value, SSHTransportError)
    record = read_job_file(tmp_path, job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT
    assert record["error"] == "ssh connection dropped mid-transfer"


def test_ssh_ac01_missing_remote_output_is_job_level_not_transport(
    tmp_path: Path,
) -> None:
    """A definitively absent remote output is a clean remote answer: it
    surfaces as the job-level ``ComputeCollectError`` (never an
    ``SSHTransportError``) and is never retried -- the structural
    distinction of AC-01 at the collect boundary."""
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


def test_ssh_ac01_launch_refusal_is_job_level_not_transport(
    tmp_path: Path,
) -> None:
    """A clean remote refusal of the launch (mkdir/cd failure) is the
    stable ``SSHJobLaunchError`` (job-level, never retried), recorded on
    the durable record as ``failure_class="job"`` (AC-01)."""
    transport = FakeTransport(
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
    with pytest.raises(SSHJobLaunchError) as excinfo:
        adapter.submit(ctx)
    assert "failed with status 1" in str(excinfo.value)
    assert not isinstance(excinfo.value, SSHTransportError)
    assert len(transport.log) == 3  # one session, no retry
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["state"] == "prepared"
    assert record["failure_class"] == FAILURE_CLASS_JOB
    assert "Permission denied" in str(record["error"])


def test_ssh_ac01_remote_exit_code_unavailable_records_recovery_note(
    tmp_path: Path,
) -> None:
    """A remote process that exited without a recoverable exit status
    (no status file) is recorded as completed with the stable recovery
    note -- collection then verifies the declared outputs independently."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=1),  # liveness: dead
            RemoteResult(exit_code=1, stderr="cat: no such file"),  # no status
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code is None
    assert status.recovery_note == SSH_EXIT_CODE_UNAVAILABLE_NOTE
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "completed"
    assert record["recovery_note"] == SSH_EXIT_CODE_UNAVAILABLE_NOTE
    assert record.get("failure_class") is None


def test_ssh_ac01_failure_classification_survives_rehydration(
    tmp_path: Path,
) -> None:
    """The recorded classification is durable: a fresh adapter instance
    over the same state directory re-hydrates ``failure_class="transport"``
    from the record alone (AC-01/AC-02 recovery discipline)."""
    transport = FakeTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 3
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(SSHTransferError):
        adapter.submit(ctx)
    fresh = SSHComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path,
        transport=FakeTransport(),
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
    )
    record = fresh.read_job(prepared.job_id)
    assert record.failure_class == FAILURE_CLASS_TRANSPORT
    assert record.error == "ssh connection dropped mid-operation"


def test_ssh_ac01_healthy_probe_clears_stale_transport_marker(
    tmp_path: Path,
) -> None:
    """The record's ``failure_class`` is the *current* classification,
    never history: after a transport failure is recorded, a fresh
    adapter with a healthy transport resubmits and completes the job,
    and the healthy terminal transition clears the stale marker."""
    # First attempt: transport permanently down -> transport marker.
    bad = FakeTransport(
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
    good = FakeTransport(
        run_script=[RemoteResult(exit_code=0, stdout="4242\n")]
    )
    adapter_good, _, _ = make_adapter(tmp_path, transport=good)
    submitted = adapter_good.submit(ctx)
    assert submitted.state is JobState.RUNNING
    assert submitted.failure_class is None
    good.run_script.extend(
        [
            RemoteResult(exit_code=1),
            RemoteResult(exit_code=0, stdout="0\n"),
        ]
    )
    status = adapter_good.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    record = read_job_file(tmp_path, prepared.job_id)
    assert record.get("failure_class") is None


def test_ssh_ac01_status_of_failed_job_carries_classification(
    tmp_path: Path,
) -> None:
    """The status outcome record carries the AC-01 classification: a
    failed job's status says ``failure_class="job"``; a healthy running
    job's status says ``failure_class`` is None."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.append(RemoteResult(exit_code=0))  # liveness: alive
    running = adapter.status(job_id)
    assert running.state is JobState.RUNNING
    assert running.failure_class is None
    transport.run_script.extend(
        [
            RemoteResult(exit_code=1),
            RemoteResult(exit_code=0, stdout="9\n"),
        ]
    )
    failed = adapter.status(job_id)
    assert failed.state is JobState.FAILED
    assert failed.failure_class == FAILURE_CLASS_JOB
    assert failed.error == "remote command exited with status 9"


# ---------------------------------------------------------------------------
# AC-02: credentials are never persisted in project state
# ---------------------------------------------------------------------------


def test_ssh_ac02_full_cycle_never_persists_credentials(tmp_path: Path) -> None:
    """After a full prepare/submit/status/collect cycle, no file under
    the injected state directory contains any credential value, and
    nothing credential-shaped appears in the remote commands or paths
    the adapter constructed (AC-02)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.pull_script = [None]  # one declared output pulled
    collected = adapter.collect(job_id)
    assert len(collected.artifact_ids) == 1
    secrets = [
        DEFAULT_CREDENTIALS.password,
        DEFAULT_CREDENTIALS.key_passphrase,
        DEFAULT_CREDENTIALS.private_key_path,
        DEFAULT_CREDENTIALS.username,
    ]
    # Walk every file under the state directory (records, manifests,
    # staging) -- the credential value must appear nowhere.
    state_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert state_files, "the state directory must contain files to walk"
    for path in state_files:
        content = path.read_text(encoding="utf-8", errors="replace")
        for secret in secrets:
            assert secret not in content, f"{path} contains credential {secret!r}"
    # The transport call log (remote commands and paths) must not embed
    # credentials either.
    for entry_name, entry_args in transport.log:
        text = repr((entry_name, entry_args))
        for secret in secrets:
            assert secret not in text, f"transport log embeds credential: {text}"


def test_ssh_ac02_durable_record_schema_has_no_credential_fields(
    tmp_path: Path,
) -> None:
    """The durable record schema carries no credential fields: the
    persisted JSON keys are exactly the documented contract (identity,
    state, remote execution facts, classification) -- nothing
    credential-shaped (AC-02)."""
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
        "submitted_at",
        "remote_pid",
        "exit_code",
        "completed_at",
        "collected_at",
        "artifact_ids",
    }
    text = json.dumps(record)
    for forbidden in ("password", "passphrase", "private", "credential",
                      "secret", "token"):
        assert forbidden not in text.lower()


def test_ssh_ac02_credentials_live_only_on_adapter_boundary(
    tmp_path: Path,
) -> None:
    """Credentials are accepted at the adapter constructor boundary and
    live only in memory: the adapter holds the injected object, and
    ``SSHCredentials`` deliberately offers no serialization (AC-02)."""
    adapter, transport, _ = make_adapter(tmp_path)
    assert adapter.credentials is DEFAULT_CREDENTIALS
    assert adapter.credentials.password == "s3cr3t-p@ssw0rd-9"
    for method in ("to_dict", "from_dict", "persist", "save", "to_json"):
        assert not hasattr(SSHCredentials, method), (
            f"SSHCredentials must not offer {method}"
        )
    # A launch command never embeds credentials.
    ctx = make_context()
    adapter.prepare(ctx)
    transport.run_script.append(RemoteResult(exit_code=0, stdout="4242\n"))
    adapter.submit(ctx)
    launch = transport.log[1][1][0]
    assert isinstance(launch, RemoteCommand)
    shell = launch.to_shell()
    assert DEFAULT_CREDENTIALS.password not in shell
    assert DEFAULT_CREDENTIALS.key_passphrase not in shell


# ---------------------------------------------------------------------------
# AC-03: mock SSH integration covers reconnect behavior
# ---------------------------------------------------------------------------


def test_ssh_ac03_reconnect_succeeds_and_operation_completes(
    tmp_path: Path,
) -> None:
    """Connect fails twice, then the reconnect succeeds and the pending
    launch completes: the operation is retried exactly as configured and
    the backoff received attempts 1 and 2 (AC-03)."""
    backoff = RecordingBackoff()
    transport = FakeTransport(
        connect_failures_left=2,
        connect_error="connection to host 'cluster.example.edu' refused",
        run_script=[RemoteResult(exit_code=0, stdout="4242\n")],
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport, backoff=backoff)
    ctx = make_context()
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    assert submitted.state is JobState.RUNNING
    assert submitted.remote_pid == 4242
    assert backoff.attempts == [1, 2]
    assert backoff.delays == [0.0, 0.0]
    connects = [e for e in transport.log if e[0] == "connect"]
    runs = [e for e in transport.log if e[0] == "run"]
    assert len(connects) == 3
    assert len(runs) == 1


def test_ssh_ac03_reconnect_fails_permanently_stable_transport_failure(
    tmp_path: Path,
) -> None:
    """When the reconnect never succeeds, the outcome is a stable
    TRANSPORT failure after exactly ``max_attempts`` connect attempts --
    no further retries (AC-03)."""
    backoff = RecordingBackoff()
    transport = FakeTransport(
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
    assert (
        str(excinfo.value)
        == "connection to host 'cluster.example.edu' refused"
    )
    connects = [e for e in transport.log if e[0] == "connect"]
    runs = [e for e in transport.log if e[0] == "run"]
    assert len(connects) == 3
    assert len(runs) == 0
    assert backoff.attempts == [1, 2]
    record = read_job_file(tmp_path, prepared.job_id)
    assert record["failure_class"] == FAILURE_CLASS_TRANSPORT


def test_ssh_ac03_mid_operation_drop_reconnects_and_retries(
    tmp_path: Path,
) -> None:
    """A scripted drop mid-launch is retried after a reconnect: the
    pending operation completes and the call log shows the exact
    connect/run/disconnect/connect/run/disconnect sequence (AC-03)."""
    transport = FakeTransport(
        run_script=[
            SSHTransferError("ssh connection dropped mid-operation"),
            RemoteResult(exit_code=0, stdout="4242\n"),
        ]
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    assert submitted.remote_pid == 4242
    assert [entry[0] for entry in transport.log] == [
        "connect",
        "run",
        "disconnect",
        "connect",
        "run",
        "disconnect",
    ]
    assert transport.is_connected() is False  # closed after the operation


def test_ssh_ac03_retry_count_exactly_as_configured(tmp_path: Path) -> None:
    """The retry count is exactly ``max_attempts`` for both configured
    values (2 and 5): the transport sees that many run attempts before
    the permanent transport failure (AC-03)."""
    for attempts in (2, 5):
        state = tmp_path / str(attempts)
        transport = FakeTransport(
            run_script=[
                SSHTransferError("ssh connection dropped mid-operation")
            ]
            * attempts
        )
        adapter, _, _ = make_adapter(
            state, transport=transport, max_attempts=attempts
        )
        ctx = make_context()
        prepared = adapter.prepare(ctx)
        with pytest.raises(SSHTransferError):
            adapter.submit(ctx)
        runs = [e for e in transport.log if e[0] == "run"]
        connects = [e for e in transport.log if e[0] == "connect"]
        assert len(runs) == attempts
        assert len(connects) == attempts
        record = read_job_file(state, prepared.job_id)
        assert record["failure_class"] == FAILURE_CLASS_TRANSPORT


def test_ssh_ac03_backoff_receives_attempt_numbers_and_no_sleeps(
    tmp_path: Path,
) -> None:
    """The injected backoff is called with 1-based attempt numbers for
    every inter-attempt delay and returns 0.0 (the tested path never
    sleeps) (AC-03)."""
    backoff = RecordingBackoff()
    transport = FakeTransport(
        run_script=[SSHTransferError("ssh connection dropped mid-operation")] * 4
    )
    adapter, _, _ = make_adapter(
        tmp_path, transport=transport, backoff=backoff, max_attempts=4
    )
    ctx = make_context()
    adapter.prepare(ctx)
    with pytest.raises(SSHTransferError):
        adapter.submit(ctx)
    assert backoff.attempts == [1, 2, 3]
    assert backoff.delays == [0.0, 0.0, 0.0]


def test_ssh_ac03_dropped_status_probe_recovers(tmp_path: Path) -> None:
    """A dropped liveness probe is retried after a reconnect and the
    status operation completes with the observed terminal state
    (AC-03)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            SSHTransferError("ssh connection dropped mid-operation"),
            RemoteResult(exit_code=1),  # liveness after reconnect: dead
            RemoteResult(exit_code=0, stdout="0\n"),  # status file: exit 0
        ]
    )
    status = adapter.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0
    assert [entry[0] for entry in transport.log] == [
        "connect",
        "run",
        "disconnect",
        "connect",
        "run",
        "disconnect",
        "connect",
        "run",
        "disconnect",
        "connect",
        "run",
        "disconnect",
    ]


def test_ssh_ac03_dropped_pull_recovers(tmp_path: Path) -> None:
    """A transfer dropped mid-pull is retried after a reconnect and the
    collection completes with the artifact registered (AC-03)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.log.clear()  # collect is the only session under test
    transport.pull_script = [
        SSHTransferError("ssh connection dropped mid-transfer"),
        None,
    ]
    collected = adapter.collect(job_id)
    assert len(collected.artifact_ids) == 1
    pulls = [e for e in transport.log if e[0] == "pull"]
    assert len(pulls) == 2
    assert [entry[0] for entry in transport.log] == [
        "connect",
        "pull",
        "disconnect",
        "connect",
        "pull",
        "disconnect",
    ]


def test_ssh_ac03_disconnect_failure_does_not_mask_retry(tmp_path: Path) -> None:
    """A failing disconnect during the retry handling never masks the
    pending operation: the drop is retried and the operation completes
    (AC-03)."""
    transport = FakeTransport(
        run_script=[
            SSHTransferError("ssh connection dropped mid-operation"),
            RemoteResult(exit_code=0, stdout="4242\n"),
        ],
        disconnect_errors_left=1,
    )
    adapter, _, _ = make_adapter(tmp_path, transport=transport)
    ctx = make_context()
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    assert submitted.remote_pid == 4242
    runs = [e for e in transport.log if e[0] == "run"]
    assert len(runs) == 2


def test_ssh_ac03_retry_policy_boundaries() -> None:
    """The injected retry policy validates its boundary strictly
    (TypeError at type boundaries, ValueError for value violations)."""
    with pytest.raises(TypeError):
        SSHRetryPolicy(max_attempts=True)
    with pytest.raises(TypeError):
        SSHRetryPolicy(max_attempts="3")
    with pytest.raises(ValueError):
        SSHRetryPolicy(max_attempts=0)
    with pytest.raises(TypeError):
        SSHRetryPolicy(backoff=5)
    policy = SSHRetryPolicy(max_attempts=5)
    assert policy.max_attempts == 5


def test_ssh_default_backoff_is_exponential_capped() -> None:
    """The default backoff is deterministic exponential, capped at 30s."""
    assert default_backoff(1) == 0.5
    assert default_backoff(2) == 1.0
    assert default_backoff(3) == 2.0
    assert default_backoff(4) == 4.0
    assert default_backoff(7) == 30.0  # 0.5 * 2**6 = 32 -> capped
    assert default_backoff(10) == 30.0


# ---------------------------------------------------------------------------
# The six-operation ComputeAdapter interface over SSH
# ---------------------------------------------------------------------------


def test_ssh_full_interface_prepare_submit_status_collect_cancel_resume(
    tmp_path: Path,
) -> None:
    """The full ComputeAdapter interface works end to end on one remote
    job: prepare stages, submit launches remotely and returns the remote
    pid, status probes the remote process through the transport, collect
    pulls outputs and registers artifacts through the real registry,
    resume re-attaches to the durable record, and cancelling a terminal
    job is rejected."""
    adapter, transport, clock = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    assert isinstance(prepared, SSHPreparedJob)
    assert prepared.state is JobState.PREPARED
    assert prepared.job_id == generate_id("job", ctx.run_id)
    assert prepared.working_directory == REMOTE_WORKDIR
    assert prepared.command == ctx.command
    assert prepared.outputs == ctx.outputs
    assert prepared.created_at == FIXED_STAMP
    assert prepared.failure_class is None
    assert read_job_file(tmp_path, prepared.job_id)["state"] == "prepared"
    assert transport.log == []  # prepare never touches the remote

    transport.run_script.append(RemoteResult(exit_code=0, stdout="4242\n"))
    submitted = adapter.submit(ctx)
    assert isinstance(submitted, SSHSubmittedJob)
    assert submitted.job_id == prepared.job_id
    assert submitted.state is JobState.RUNNING
    assert submitted.remote_pid == 4242
    assert submitted.submitted_at == FIXED_STAMP
    # The launch command is the remote path/command abstraction in
    # action: mkdir + cd + nohup background wrapper with status capture.
    launch = transport.log[1][1][0]
    assert isinstance(launch, RemoteCommand)
    shell = launch.to_shell()
    assert f"mkdir -p -- {REMOTE_WORKDIR}" in shell
    assert "nohup sh -c" in shell
    assert "sim.py" in shell
    assert f".sr_{prepared.job_id}_job.status" in shell

    transport.run_script.append(RemoteResult(exit_code=0))
    status = adapter.status(submitted.job_id)
    assert isinstance(status, SSHJobStatus)
    assert status.state is JobState.RUNNING
    assert status.remote_pid == 4242
    assert status.failure_class is None

    transport.run_script.extend(
        [
            RemoteResult(exit_code=1),
            RemoteResult(exit_code=0, stdout="0\n"),
        ]
    )
    status = adapter.status(submitted.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0

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
    assert manifest.metadata["backend"] == "ssh"
    assert manifest.producer == "adapter:compute/ssh@v1.0"
    assert (tmp_path / "manifests" / f"{artifact_id}.json").is_file()

    resumed = adapter.resume(submitted.job_id)
    assert isinstance(resumed, SSHResumedJob)
    assert resumed.state is JobState.COMPLETED
    assert resumed.failure_class is None

    with pytest.raises(ComputeJobStateError):
        adapter.cancel(submitted.job_id)

    # created (prepare), submitted (submit), completed probe 1 and 2
    # (both status calls stamp the transition decision), and collect's
    # manifest created_at + collected_at: 6 clock reads, all identical.
    assert clock.calls == [FIXED_STAMP] * 6


def test_ssh_identical_inputs_produce_byte_identical_records(
    tmp_path: Path,
) -> None:
    """Identical injected inputs produce byte-identical durable records
    and outcome records (deterministic protocol capture)."""
    records: list[bytes] = []
    statuses: list[dict[str, object]] = []
    for index in ("1", "2"):
        state = tmp_path / index
        adapter, transport, _ = make_adapter(state)
        ctx = make_context()
        prepared = adapter.prepare(ctx)
        transport.run_script.append(RemoteResult(exit_code=0, stdout="4242\n"))
        submitted = adapter.submit(ctx)
        transport.run_script.extend(
            [
                RemoteResult(exit_code=1),
                RemoteResult(exit_code=0, stdout="0\n"),
            ]
        )
        status = adapter.status(submitted.job_id)
        records.append(
            (state / "jobs" / f"{prepared.job_id}.json").read_bytes()
        )
        statuses.append(status.to_dict())
    assert records[0] == records[1]
    assert statuses[0] == statuses[1]


def test_ssh_status_of_terminal_record_is_durable_record_driven(
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


def test_ssh_submit_requires_prepared_job(tmp_path: Path) -> None:
    """Submitting without prepare, or submitting twice, is rejected with
    the stable state error."""
    adapter, transport, _ = make_adapter(tmp_path)
    ctx = make_context()
    with pytest.raises(ComputeJobNotFoundError):
        adapter.submit(ctx)
    adapter.prepare(ctx)
    transport.run_script.append(RemoteResult(exit_code=0, stdout="4242\n"))
    adapter.submit(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.submit(ctx)


def test_ssh_prepare_rejects_restaging_different_content(tmp_path: Path) -> None:
    """Re-staging the same run with different content is rejected: job
    identity is a pure function of the run id."""
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context(command=("python", "sim.py"))
    adapter.prepare(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.prepare(make_context(command=("python", "other.py")))


def test_ssh_collect_requires_completed_job(tmp_path: Path) -> None:
    """Collect requires a completed job (stable state error)."""
    adapter, transport, _, job_id = running_job(tmp_path)
    assert transport.log
    with pytest.raises(ComputeJobStateError):
        adapter.collect(job_id)


def test_ssh_collect_recollect_is_idempotent(tmp_path: Path) -> None:
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


def test_ssh_resume_from_durable_record_alone(tmp_path: Path) -> None:
    """A fresh adapter instance over the same state directory recovers a
    running job from the durable record alone and probes its remote
    process through its own transport (AC-01/AC-02 recovery)."""
    _, _, _, job_id = running_job(tmp_path)
    fresh_transport = FakeTransport()
    fresh = SSHComputeAdapter(
        DEFAULT_CREDENTIALS,
        tmp_path,
        transport=fresh_transport,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
    )
    fresh_transport.run_script.append(RemoteResult(exit_code=0))
    resumed = fresh.resume(job_id)
    assert resumed.state is JobState.RUNNING
    assert resumed.remote_pid == 4242
    fresh_transport.run_script.extend(
        [
            RemoteResult(exit_code=1),
            RemoteResult(exit_code=0, stdout="0\n"),
        ]
    )
    resumed = fresh.resume(job_id)
    assert resumed.state is JobState.COMPLETED


def test_ssh_cancel_prepared_is_a_local_decision(tmp_path: Path) -> None:
    """Cancelling a prepared job never contacts the remote host."""
    adapter, transport, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    cancelled = adapter.cancel(prepared.job_id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancelled_at == FIXED_STAMP
    assert transport.log == []


def test_ssh_cancel_running_terminates_remotely(tmp_path: Path) -> None:
    """Cancelling a running job sends a remote kill, observes the exit
    status from the remote status file and records the cancellation."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [RemoteResult(exit_code=0)]  # kill accepted
        + [
            RemoteResult(exit_code=1),  # post-kill liveness: dead
            RemoteResult(exit_code=0, stdout="143\n"),  # SIGTERM exit
        ]
    )
    cancelled = adapter.cancel(job_id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.exit_code == 143
    assert cancelled.failure_class is None
    kill_commands = [
        entry[1][0]
        for entry in transport.log
        if entry[0] == "run" and entry[1][0].to_shell() == "kill 4242"
    ]
    assert len(kill_commands) == 1
    assert isinstance(kill_commands[0], RemoteCommand)
    assert kill_commands[0].to_shell() == "kill 4242"
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "cancelled"
    assert record["exit_code"] == 143


def test_ssh_cancel_requires_non_terminal_job(tmp_path: Path) -> None:
    """Cancelling a terminal job is rejected (stable state error)."""
    adapter, transport, _, job_id = completed_job(tmp_path)
    transport.log.clear()
    with pytest.raises(ComputeJobStateError):
        adapter.cancel(job_id)


def test_ssh_adapter_requires_transport_and_valid_boundaries(
    tmp_path: Path,
) -> None:
    """The adapter boundary is strict: a transport is required, and
    wrong-typed credentials/transport are TypeError."""
    with pytest.raises(TypeError):
        SSHComputeAdapter(DEFAULT_CREDENTIALS, tmp_path)
    with pytest.raises(TypeError):
        SSHComputeAdapter(
            "not-credentials", tmp_path, transport=FakeTransport()
        )
    with pytest.raises(TypeError):
        SSHComputeAdapter(
            DEFAULT_CREDENTIALS, tmp_path, transport=object()
        )
    adapter, _, _ = make_adapter(tmp_path)
    assert adapter.credentials is DEFAULT_CREDENTIALS
    assert isinstance(adapter.transport, FakeTransport)
    assert isinstance(adapter.retry_policy, SSHRetryPolicy)


def test_ssh_prepare_rejects_unsafe_remote_paths(tmp_path: Path) -> None:
    """Remote working directories are validated before any command
    construction: relative paths, NUL bytes and unsafe declared output
    names are rejected with the stable identity error."""
    adapter, _, _ = make_adapter(tmp_path)
    with pytest.raises(SSHJobIdentityError):
        adapter.prepare(make_context(working_directory="relative/dir"))
    with pytest.raises(SSHJobIdentityError):
        adapter.prepare(make_context(working_directory="/has\x00nul"))
    with pytest.raises(SSHJobIdentityError):
        adapter.prepare(make_context(outputs=("out name.txt",)))
    # Glob metacharacters and ".." are rejected already at the RunContext
    # boundary (the sibling adapter's stable identity error); whitespace
    # passes RunContext and is rejected by the ssh segment discipline.
    with pytest.raises((ComputeJobIdentityError, SSHJobIdentityError)):
        adapter.prepare(make_context(outputs=("../../escape.txt",)))
    with pytest.raises((ComputeJobIdentityError, SSHJobIdentityError)):
        adapter.prepare(make_context(outputs=("glob*.txt",)))
    with pytest.raises((ComputeJobIdentityError, SSHJobIdentityError)):
        adapter.prepare(make_context(working_directory="/ok", outputs=(123,)))

# ---------------------------------------------------------------------------
# Remote path and command abstraction
# ---------------------------------------------------------------------------


def test_ssh_remote_path_validation_and_join() -> None:
    """RemotePath validates absolute POSIX paths and safe segments."""
    path = RemotePath("/home/alice/scratch/work-1")
    assert str(path) == "/home/alice/scratch/work-1"
    joined = path.join("result.txt", "nested", "a.b-c_d")
    assert str(joined) == "/home/alice/scratch/work-1/result.txt/nested/a.b-c_d"
    with pytest.raises(SSHJobIdentityError):
        RemotePath("relative")
    with pytest.raises(SSHJobIdentityError):
        RemotePath("/has\x00nul")
    with pytest.raises(TypeError):
        RemotePath(123)
    for bad in ("..", ".", "", "a/b", "a b", "glob*", "q[1]"):
        with pytest.raises(SSHJobIdentityError):
            path.join(bad)
    assert RemotePath.segment_is_safe("result.txt")
    assert not RemotePath.segment_is_safe("a b")


def test_ssh_remote_path_accepts_spaces_but_quotes_them(
    tmp_path: Path,
) -> None:
    """A working directory may contain spaces (it is configuration, not
    an id-bearing segment); the launch command shell-quotes it so no
    whitespace ever breaks the remote shell line."""
    adapter, transport, _ = make_adapter(tmp_path)
    ctx = make_context(working_directory="/home/alice/my work")
    prepared = adapter.prepare(ctx)
    transport.run_script.append(RemoteResult(exit_code=0, stdout="4242\n"))
    adapter.submit(ctx)
    launch = transport.log[1][1][0]
    assert isinstance(launch, RemoteCommand)
    # The inner shell line (argv[2] of the sh -c wrapper) quotes the
    # space-bearing working directory with shlex.quote.
    line = launch.argv[2]
    assert "mkdir -p -- '/home/alice/my work'" in line
    assert "&& cd -- '/home/alice/my work'" in line
    # The status file of the launched job is captured by the inner line.
    status_file = f".sr_{prepared.job_id}_job.status"
    assert status_file in line


def test_ssh_remote_command_shell_quoting_roundtrip() -> None:
    """RemoteCommand renders a shell line that round-trips through
    shlex.split: argv entries survive quoting untouched."""
    command = RemoteCommand(("echo", "a b", "c'd", "d\"e", "$HOME", "a&b"))
    assert shlex.split(command.to_shell()) == [
        "echo",
        "a b",
        "c'd",
        'd"e',
        "$HOME",
        "a&b",
    ]
    assert RemoteCommand(("python", "sim.py")).to_shell() == "python sim.py"


def test_ssh_remote_command_and_result_validation() -> None:
    """RemoteCommand/RemoteResult validate their boundaries strictly."""
    with pytest.raises(TypeError):
        RemoteCommand(())
    with pytest.raises(TypeError):
        RemoteCommand(("",))
    with pytest.raises(TypeError):
        RemoteCommand(("echo", 1))
    with pytest.raises(TypeError):
        RemoteCommand("echo hi")
    with pytest.raises(ValueError):
        RemoteCommand(("echo", "bad\x00nul"))
    with pytest.raises(TypeError):
        RemoteResult(exit_code=True)
    with pytest.raises(TypeError):
        RemoteResult(exit_code=0, stdout=5)
    result = RemoteResult(exit_code=0, stdout="42", stderr="")
    assert result.exit_code == 0
    assert result.stdout == "42"


def test_ssh_credentials_validation() -> None:
    """SSHCredentials validates its boundary strictly."""
    with pytest.raises(TypeError):
        SSHCredentials(host=123)
    with pytest.raises(ValueError):
        SSHCredentials(host="")
    with pytest.raises(ValueError):
        SSHCredentials(host="bad host")
    with pytest.raises(TypeError):
        SSHCredentials(host="h", port=True)
    with pytest.raises(ValueError):
        SSHCredentials(host="h", port=0)
    with pytest.raises(ValueError):
        SSHCredentials(host="h", port=70000)
    with pytest.raises(TypeError):
        SSHCredentials(host="h", password=5)
    with pytest.raises(TypeError):
        SSHCredentials(host="h", username=5)
    creds = SSHCredentials(host="h", port=2222, username="alice")
    assert creds.port == 2222
    assert creds.password is None
    assert creds.key_passphrase is None


# ---------------------------------------------------------------------------
# The durable record contract
# ---------------------------------------------------------------------------


def test_ssh_durable_record_roundtrip_from_dict(tmp_path: Path) -> None:
    """The durable record round-trips through to_dict/from_dict with the
    classification and remote pid intact."""
    adapter, transport, _, job_id = running_job(tmp_path)
    transport.run_script.extend(
        [
            RemoteResult(exit_code=1),
            RemoteResult(exit_code=0, stdout="3\n"),
        ]
    )
    adapter.status(job_id)
    raw = read_job_file(tmp_path, job_id)
    from scientific_reproduction.adapters.compute.ssh import SSHJobRecord

    record = SSHJobRecord.from_dict(raw)
    assert record.job_id == job_id
    assert record.state is JobState.FAILED
    assert record.remote_pid == 4242
    assert record.exit_code == 3
    assert record.failure_class == FAILURE_CLASS_JOB
    assert record.error == "remote command exited with status 3"
    assert SSHJobRecord.from_dict(record.to_dict()) == record


def test_ssh_record_rejects_wrong_backend(tmp_path: Path) -> None:
    """Records of another backend are refused by the ssh record
    contract."""
    from scientific_reproduction.adapters.compute.ssh import SSHJobRecord

    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["backend"] = "local"
    with pytest.raises(ComputeJobRecordError):
        SSHJobRecord.from_dict(raw)


def test_ssh_record_rejects_bad_failure_class(tmp_path: Path) -> None:
    """An unknown ``failure_class`` is refused by the record contract."""
    from scientific_reproduction.adapters.compute.ssh import SSHJobRecord

    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["failure_class"] = "warp-speed"
    with pytest.raises(ComputeJobRecordError):
        SSHJobRecord.from_dict(raw)


def test_ssh_record_rejects_unsafe_output_names(tmp_path: Path) -> None:
    """A persisted record whose declared output is not a safe remote path
    segment is corrupt (the FND-M9-G02-01 discipline also protects the
    record contract)."""
    from scientific_reproduction.adapters.compute.ssh import SSHJobRecord

    adapter, _, _, job_id = running_job(tmp_path)
    raw = read_job_file(tmp_path, job_id)
    raw["outputs"] = ["bad name.txt"]
    with pytest.raises(ComputeJobRecordError):
        SSHJobRecord.from_dict(raw)


def test_ssh_read_job_validation(tmp_path: Path) -> None:
    """read_job rejects malformed job ids and missing records with the
    stable errors."""
    adapter, _, _ = make_adapter(tmp_path)
    with pytest.raises(SSHJobIdentityError):
        adapter.read_job("not-a-job-id")
    with pytest.raises(ComputeJobNotFoundError):
        adapter.read_job(generate_id("job", "never-prepared"))
