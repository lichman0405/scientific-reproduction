"""Integration: Lab/compute external adapters end to end across fresh
instances (DEV-M7-G05, acceptance criteria AC-01/AC-02/AC-03).

End-to-end fixtures over the **real** state machinery proving the three
frozen acceptance criteria with a worker that "exits" between
operations -- a fresh adapter instance over the same state directory is
the assertion that the original worker session is never needed again:

* ``test_m7g05_ac01_*`` -- the lab dispatch outlives the original
  worker: one ``FilesystemLabAdapter`` instance dispatches the
  experiment package (real filesystem handoff under ``tmp_path``), the
  worker exits, and a **fresh instance** over the same handoff root
  detects the returned Result Package, collects it and associates it
  with the correct Run -- never guessed, never cross-matched. A second
  dispatch of the same package is refused across instances (exactly
  once, the original handoff never overwritten).
* ``test_m7g05_ac02_*`` -- the Slurm-style job outlives the original
  worker: one ``SlurmComputeAdapter`` instance submits the job through
  a scripted transport (the "mock Slurm lifecycle"), the worker exits,
  and a **fresh instance** -- its own transport, its own session -- over
  the same state directory reconciles the job (status/resume/collect)
  from the durable record alone. The terminal decision is persisted
  once and later answered from the record alone with zero remote
  contact.
* ``test_m7g05_ac03_*`` -- the run/record state references the external
  ids deterministically: the lab ``dispatch_id`` and the Slurm
  ``job_id`` are pure functions of the run identity (the
  ``generate_id`` discipline -- same inputs, same ids), appear in the
  persisted dispatch/job records and in a schema-valid Run's
  ``external`` reference, and never credentials -- injected secret
  values are scanned out of every state file, transport log and pushed
  script.

Determinism: no network, no sleeps, no wall clock, no randomness. The
Slurm adapter runs against the scripted :class:`FakeSlurmTransport`
with :class:`FakeClock` (fixed ``FIXED_STAMP``) and a zero-delay
:class:`RecordingBackoff`, exactly as the DEV-M7-G04 unit suite does
(``tests/adapters/compute/test_slurm_ssh_adapter.py``); the lab handoff
uses the real filesystem under ``tmp_path``. Artifact registration,
checksums and ids exercise the **real** ``ArtifactRegistry`` /
``compute_sha256`` / ``generate_id`` machinery -- nothing is mocked at
the core layer.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from scientific_reproduction.adapters.compute.local import JobState, RunContext
from scientific_reproduction.adapters.compute.slurm_ssh import (
    SLURM_BACKEND_NAME,
    SlurmCollectedJob,
    SlurmComputeAdapter,
    SlurmSubmittedJob,
)
from scientific_reproduction.adapters.compute.ssh import (
    RemoteCommand,
    RemotePath,
    RemoteResult,
    SSHConnectionError,
    SSHCredentials,
    SSHRetryPolicy,
    SSHTransferError,
    SSHTransport,
    SSHTransportError,
)
from scientific_reproduction.adapters.lab.base import (
    DispatchState,
    DispatchStatus,
    DuplicateDispatchError,
)
from scientific_reproduction.adapters.lab.filesystem import (
    DISPATCH_RECORD_FILENAME,
    FilesystemLabAdapter,
)
from scientific_reproduction.adapters.lab.manifest import RESULT_MANIFEST_VERSION
from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.schema_validation import validate_object

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: The default remote working directory of the fixtures.
REMOTE_WORKDIR = "/home/alice/scratch/work-1"

#: The default scripted external Slurm job id of a submitted job.
EXTERNAL_ID = 930411

#: The default scripted credentials (secrets deliberately distinctive so
#: the persistence walk can prove their absence -- AC-03).
DEFAULT_CREDENTIALS = SSHCredentials(
    host="cluster.example.edu",
    username="cred-user-77",
    password="s3cr3t-p@ssw0rd-9",
    private_key_path=r"C:\keys\id_ed25519",
    key_passphrase="pa55phrase-7-xyz",
)

#: The deterministic project/goal of the fixture runs (the run/package/
#: job ids are then pure functions of the run identity -- AC-03).
PROJECT_ID = generate_id("project", "dev-m7-g05")
GOAL_ID = generate_id("goal", "dev-m7-g05")


# ---------------------------------------------------------------------------
# Run and lab fixtures
# ---------------------------------------------------------------------------


def make_run_id(label: str = "dev-m7-g05") -> str:
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


def make_package(
    run_id: str,
    *,
    package_id: str | None = None,
    required_return: tuple[str, ...] = ("raw-data.csv",),
) -> dict[str, Any]:
    """A minimal schema-valid ``lab-execution-package`` mapping whose
    ``package_id`` is itself a pure function of the run id (so the
    dispatch id is a pure function of the run identity -- AC-03)."""
    return {
        "package_id": (
            package_id if package_id is not None else generate_id("package", run_id)
        ),
        "project_id": PROJECT_ID,
        "goal_id": GOAL_ID,
        "run_id": run_id,
        "objective": "synthesize the target compound per the frozen protocol",
        "procedure": [{"step": 1, "action": "weigh the precursor"}],
        "required_return": list(required_return),
    }


def make_result_manifest(
    run_id: str,
    package_id: str,
    *,
    files: tuple[str, ...] = ("raw-data.csv",),
) -> dict[str, Any]:
    """A returned Result Package manifest for the reference flow."""
    return {
        "manifest_version": RESULT_MANIFEST_VERSION,
        "package_id": package_id,
        "project_id": PROJECT_ID,
        "goal_id": GOAL_ID,
        "run_id": run_id,
        "files": list(files),
        "notes": [],
    }


def write_result_package(
    base: Path,
    run_id: str,
    manifest: dict[str, Any],
    files: dict[str, str | bytes] | None = None,
) -> Path:
    """Write a returned Result Package into ``base/incoming/<run_id>/``
    (canonical result-manifest JSON plus the declared data files)."""
    incoming = base / "incoming" / run_id
    incoming.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    (incoming / "result-manifest.json").write_text(canonical, encoding="utf-8")
    for name, content in (files or {}).items():
        data = content.encode("utf-8") if isinstance(content, str) else content
        (incoming / name).write_bytes(data)
    return incoming


def read_dispatch_record(base: Path, run_id: str) -> dict[str, Any]:
    """The on-disk dispatch record as parsed JSON."""
    return json.loads(
        (base / "outgoing" / run_id / DISPATCH_RECORD_FILENAME).read_text(
            encoding="utf-8"
        )
    )


def make_run(
    run_id: str,
    *,
    backend: str | None = None,
    dispatch_id: str | None = None,
    job_id: str | None = None,
) -> Run:
    """A schema-valid Run whose ``external`` reference names the external
    adapter ids (AC-03: the run state references the external ids)."""
    return Run(
        run_id=run_id,
        goal_id=GOAL_ID,
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
        goal_version="1.0",
        external=RunExternal(
            backend=backend,
            dispatch_id=dispatch_id,
            job_id=job_id,
        ),
    )


# ---------------------------------------------------------------------------
# Slurm fixtures (the DEV-M7-G04 scripted-transport pattern, mirrored)
# ---------------------------------------------------------------------------


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
    all behavior scripted (the DEV-M7-G04 pattern).

    * ``connect_failures_left`` -- that many ``connect`` calls raise
      ``SSHConnectionError``, then connect succeeds.
    * ``run_script`` -- a queue of ``RemoteResult`` (returned in order)
      or ``SSHTransportError`` (raised; an ``SSHTransferError`` also
      drops the session, ``connected`` -> False). The queue is shared by
      every remote command (mkdir, sbatch, squeue, sacct, scancel).
    * ``pull_script`` -- a queue of ``None`` (write ``pull_payload`` to
      the local path) or exceptions (raised; an ``SSHTransferError``
      also drops the session).
    * ``pushed`` -- the local content of every pushed batch script.
    * ``log`` -- ordered call log of ``("connect"|"disconnect"|"run"|
      "push"|"pull", args)`` entries, making the reconnect observable.
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
        self.pushed.append((local_path, remote_path, local_path.read_bytes()))

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


def make_fresh_adapter(
    state_dir: Path,
) -> tuple[SlurmComputeAdapter, FakeSlurmTransport]:
    """A fresh adapter instance over the same state directory: its own
    transport, its own session -- the heart of the AC-01/AC-02
    "outlives the worker" assertion."""
    transport = FakeSlurmTransport()
    adapter = SlurmComputeAdapter(
        DEFAULT_CREDENTIALS,
        state_dir,
        transport=transport,
        retry_policy=SSHRetryPolicy(max_attempts=3, backoff=RecordingBackoff()),
        now=FakeClock(FIXED_STAMP),
    )
    return adapter, transport


def read_job_file(state_dir: Path, job_id: str) -> dict[str, object]:
    """The on-disk durable job record as parsed JSON."""
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
# AC-01: the lab dispatch outlives the original worker; a fresh instance
# detects, collects and associates the returned package with its Run
# ---------------------------------------------------------------------------


def test_m7g05_ac01_lab_fresh_instance_detects_and_collects_returned_package(
    tmp_path: Path,
) -> None:
    """AC-01: one adapter instance dispatches the experiment package
    (real filesystem handoff); the original worker exits; a fresh
    instance over the same handoff root answers status from the recorded
    dispatch alone, detects the returned Result Package, collects it and
    associates it with the correct Run."""
    base = tmp_path / "lab"
    run_id = make_run_id("dev-m7-g05-ac01")
    package = make_package(run_id)
    package_id = package["package_id"]

    # Worker 1 dispatches the experiment package (AC-01 outgoing handoff).
    worker_one = FilesystemLabAdapter(base)
    record = worker_one.dispatch(package, dispatched_at=FIXED_STAMP)
    assert record.dispatch_id == generate_id("dispatch", package_id, run_id)
    assert record.run_id == run_id
    assert Path(record.outgoing_path) == base / "outgoing" / run_id

    # Worker 1 exits. A fresh instance over the same handoff root
    # answers status from the persisted dispatch record alone.
    worker_two = FilesystemLabAdapter(base)
    status = worker_two.status(record.dispatch_id)
    assert isinstance(status, DispatchStatus)
    assert status.state is DispatchState.RUNNING_EXTERNAL
    assert status.run_id == run_id

    # The lab returns the Result Package while the worker is gone.
    write_result_package(
        base,
        run_id,
        make_result_manifest(run_id, package_id),
        {"raw-data.csv": b"temp=298.15,pressure=101.3\n"},
    )
    status = worker_two.status(record.dispatch_id)
    assert status.state is DispatchState.RESULT_AVAILABLE

    # The fresh instance collects it and associates it with the correct
    # Run -- the dispatching Worker is never needed again (AC-01).
    collected = worker_two.collect(record.dispatch_id)
    assert collected.dispatch_id == record.dispatch_id
    assert collected.run_id == run_id
    assert collected.manifest.run_id == run_id
    assert collected.manifest.package_id == package_id
    assert collected.collected_files == ("raw-data.csv",)
    assert Path(collected.result_path) == base / "incoming" / run_id


def test_m7g05_ac01_lab_fresh_instance_collects_only_its_own_run(
    tmp_path: Path,
) -> None:
    """AC-01: two runs dispatched by one worker; the returned package of
    each is collected by a fresh instance and associated with exactly
    its own Run -- never guessed, never cross-matched."""
    base = tmp_path / "lab"
    run_a = make_run_id("dev-m7-g05-ac01-a")
    run_b = make_run_id("dev-m7-g05-ac01-b")
    package_a = make_package(run_a)
    package_b = make_package(run_b)

    worker_one = FilesystemLabAdapter(base)
    dispatch_a = worker_one.dispatch(package_a, dispatched_at=FIXED_STAMP)
    dispatch_b = worker_one.dispatch(package_b, dispatched_at=FIXED_STAMP)
    assert dispatch_a.dispatch_id != dispatch_b.dispatch_id

    # The lab returns both packages while the worker is gone.
    write_result_package(
        base,
        run_a,
        make_result_manifest(run_a, package_a["package_id"]),
        {"raw-data.csv": b"yield=0.87\n"},
    )
    write_result_package(
        base,
        run_b,
        make_result_manifest(run_b, package_b["package_id"]),
        {"raw-data.csv": b"yield=0.91\n"},
    )

    # A fresh instance collects each dispatch and associates it with its
    # own Run.
    monitor = FilesystemLabAdapter(base)
    collected_a = monitor.collect(dispatch_a.dispatch_id)
    assert collected_a.run_id == run_a
    assert collected_a.manifest.run_id == run_a
    assert collected_a.manifest.package_id == package_a["package_id"]
    collected_b = monitor.collect(dispatch_b.dispatch_id)
    assert collected_b.run_id == run_b
    assert collected_b.manifest.run_id == run_b
    assert collected_b.manifest.package_id == package_b["package_id"]


def test_m7g05_ac01_lab_dispatch_is_exactly_once_across_instances(
    tmp_path: Path,
) -> None:
    """AC-01: dispatches are exactly-once even across adapter instances
    -- the dispatch id is a pure function of the package identity, so a
    fresh instance re-dispatching the same package is refused loudly and
    the original handoff is never overwritten."""
    base = tmp_path / "lab"
    run_id = make_run_id("dev-m7-g05-ac01-dup")
    package = make_package(run_id)
    package_id = package["package_id"]

    worker_one = FilesystemLabAdapter(base)
    worker_one.dispatch(package, dispatched_at=FIXED_STAMP)
    dispatch_file = base / "outgoing" / run_id / DISPATCH_RECORD_FILENAME
    before = dispatch_file.read_bytes()

    worker_two = FilesystemLabAdapter(base)
    with pytest.raises(DuplicateDispatchError) as excinfo:
        worker_two.dispatch(package, dispatched_at=FIXED_STAMP)
    assert package_id in str(excinfo.value)
    assert run_id in str(excinfo.value)
    assert dispatch_file.read_bytes() == before


# ---------------------------------------------------------------------------
# AC-02: the Slurm job outlives the original worker; a fresh instance
# reconciles it, with the terminal decision persisted once
# ---------------------------------------------------------------------------


def test_m7g05_ac02_slurm_fresh_instance_reconciles_status_resume_collect(
    tmp_path: Path,
) -> None:
    """AC-02: one adapter instance submits the job through its own
    scripted transport/session; the worker exits; a fresh instance (its
    own transport, its own session) over the same state directory
    reconciles the job from the durable record alone -- status derives
    the scheduler queries from the record's external id, resume
    re-attaches, the terminal decision is persisted once, and collect
    registers the outputs through the real artifact registry."""
    run_id = make_run_id("dev-m7-g05-ac02")
    ctx = make_context(run_id=run_id)

    # Worker 1 submits the job (mkdir accepted, sbatch answers with the
    # external Slurm job id).
    transport_a = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter_a, _, _ = make_adapter(tmp_path, transport=transport_a)
    prepared = adapter_a.prepare(ctx)
    submitted = adapter_a.submit(ctx)
    assert isinstance(submitted, SlurmSubmittedJob)
    assert submitted.state is JobState.RUNNING
    assert submitted.external_id == EXTERNAL_ID
    job_id = prepared.job_id

    # Worker 1 exits. A fresh instance reconciles the job from the
    # durable record alone.
    fresh, transport_b = make_fresh_adapter(tmp_path)
    transport_b.run_script.append(RemoteResult(exit_code=0, stdout="RUNNING\n"))
    status = fresh.status(job_id)
    assert status.state is JobState.RUNNING
    assert status.external_id == EXTERNAL_ID
    # The fresh probe derives --jobs from the record's external id.
    probes = run_commands(transport_b)
    assert len(probes) == 1
    assert "squeue" in probes[0].argv
    assert "--jobs" in probes[0].argv
    assert str(EXTERNAL_ID) in probes[0].argv

    # resume re-attaches through the fresh session (still queued).
    transport_b.run_script.append(RemoteResult(exit_code=0, stdout="PENDING\n"))
    resumed = fresh.resume(job_id)
    assert resumed.state is JobState.RUNNING
    assert resumed.external_id == EXTERNAL_ID

    # The scheduler completes the job; the fresh instance observes it
    # and persists the terminal decision.
    transport_b.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),  # sacct
        ]
    )
    status = fresh.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.external_id == EXTERNAL_ID
    record = read_job_file(tmp_path, job_id)
    assert record["state"] == "completed"
    assert record["external_id"] == EXTERNAL_ID
    assert record["scheduler_state"] == "COMPLETED"

    # The fresh instance collects the completed job from the record
    # alone: outputs pulled through its own transport and registered
    # through the real artifact registry.
    transport_b.pull_script = [None]
    collected = fresh.collect(job_id)
    assert isinstance(collected, SlurmCollectedJob)
    assert collected.state is JobState.COMPLETED
    artifact_id = generate_id("artifact", job_id, "result.txt")
    assert collected.artifact_ids == (artifact_id,)
    assert collected.artifacts[0].run_id == run_id
    assert collected.artifacts[0].sha256 == compute_sha256(
        tmp_path / "staging" / job_id / "result.txt"
    )
    assert (tmp_path / "manifests" / f"{artifact_id}.json").is_file()

    # The terminal decision was persisted once: a later status answers
    # from the record alone with zero remote contact (AC-02).
    transport_b.log.clear()
    status = fresh.status(job_id)
    assert status.state is JobState.COMPLETED
    assert transport_b.log == []


def test_m7g05_ac02_slurm_terminal_record_answered_without_remote_contact(
    tmp_path: Path,
) -> None:
    """AC-02: a job observed terminal by the original worker is answered
    from the record alone by a fresh instance -- status and resume both
    complete without ever contacting the scheduler through the fresh
    transport."""
    _, _, _, job_id = completed_job(tmp_path)

    fresh, transport_fresh = make_fresh_adapter(tmp_path)
    status = fresh.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.external_id == EXTERNAL_ID
    resumed = fresh.resume(job_id)
    assert resumed.state is JobState.COMPLETED
    assert resumed.external_id == EXTERNAL_ID
    assert transport_fresh.log == []


# ---------------------------------------------------------------------------
# AC-03: run/record state references the external ids deterministically
# (generate_id discipline), never credentials
# ---------------------------------------------------------------------------


def test_m7g05_ac03_same_run_references_both_external_ids_deterministically(
    tmp_path: Path,
) -> None:
    """AC-03: the lab dispatch id and the Slurm job id of one Run are
    deterministic pure functions of the run identity -- recomputing them
    from the same inputs yields the same ids, they appear in the
    persisted dispatch/job records, and a schema-valid Run references
    both; the reference round-trips through the real model machinery."""
    run_id = make_run_id("dev-m7-g05-ac03")
    base = tmp_path / "lab"
    compute = tmp_path / "compute"

    # Lab dispatch for this run.
    package = make_package(run_id)
    package_id = package["package_id"]
    dispatch = FilesystemLabAdapter(base).dispatch(package, dispatched_at=FIXED_STAMP)
    dispatch_id = dispatch.dispatch_id
    assert is_valid_id(dispatch_id, "dispatch")

    # Slurm job for the same run.
    ctx = make_context(run_id=run_id)
    transport = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter, _, _ = make_adapter(compute, transport=transport)
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    job_id = submitted.job_id
    assert is_valid_id(job_id, "job")

    # Same inputs, same ids: recomputing from the same run identity
    # yields the same values, across both backends.
    assert generate_id("dispatch", package_id, run_id) == dispatch_id
    assert generate_id("job", run_id) == job_id
    assert dispatch_id != job_id

    # The persisted records carry both ids.
    dispatch_record = read_dispatch_record(base, run_id)
    assert dispatch_record["dispatch_id"] == dispatch_id
    assert dispatch_record["run_id"] == run_id
    job_record = read_job_file(compute, job_id)
    assert job_record["job_id"] == job_id
    assert job_record["run_id"] == run_id
    assert job_record["external_id"] == EXTERNAL_ID

    # The run state references both external ids deterministically and
    # round-trips through the real model machinery.
    run = make_run(
        run_id,
        backend=SLURM_BACKEND_NAME,
        dispatch_id=dispatch_id,
        job_id=job_id,
    )
    data = run.to_dict()
    assert data["external"]["dispatch_id"] == dispatch_id
    assert data["external"]["job_id"] == job_id
    assert validate_object("run", data) == []
    restored = Run.from_dict(data)
    assert restored.external is not None
    assert restored.external.dispatch_id == dispatch_id
    assert restored.external.job_id == job_id

    # Credentials never appear in any persisted state file, transport
    # log or pushed script (AC-03: ids in the state, credentials nowhere).
    secrets = [
        value
        for value in (
            DEFAULT_CREDENTIALS.password,
            DEFAULT_CREDENTIALS.key_passphrase,
            DEFAULT_CREDENTIALS.private_key_path,
            DEFAULT_CREDENTIALS.username,
        )
        if value is not None
    ]
    state_files = [p for p in compute.rglob("*") if p.is_file()]
    assert state_files, "the compute state directory must contain files"
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


def test_m7g05_ac03_collected_artifact_ids_deterministic_and_persisted(
    tmp_path: Path,
) -> None:
    """AC-03: collected artifact ids are deterministic pure functions of
    the job id and output name (``generate_id("artifact", job_id, name)``
    -- same inputs, same ids), appear in the persisted job record and
    the registered manifest, and carry the real checksum of the staged
    output."""
    adapter, transport, ctx, job_id = completed_job(tmp_path)
    transport.pull_script = [None]
    collected = adapter.collect(job_id)

    artifact_id = generate_id("artifact", job_id, "result.txt")
    assert is_valid_id(artifact_id, "artifact")
    assert collected.artifact_ids == (artifact_id,)
    assert generate_id("artifact", job_id, "result.txt") == artifact_id
    assert collected.artifacts[0].artifact_id == artifact_id
    assert collected.artifacts[0].run_id == ctx.run_id
    assert collected.artifacts[0].metadata["job_id"] == job_id
    assert collected.artifacts[0].sha256 == compute_sha256(
        tmp_path / "staging" / job_id / "result.txt"
    )
    record = read_job_file(tmp_path, job_id)
    assert record["artifact_ids"] == [artifact_id]
    assert (tmp_path / "manifests" / f"{artifact_id}.json").is_file()


def test_m7g05_ac03_full_cycle_state_carries_ids_and_never_credentials(
    tmp_path: Path,
) -> None:
    """AC-03: after the full Slurm lifecycle -- submit through worker 1,
    terminal observation and collection through a fresh instance -- the
    deterministic ids appear in every persisted state artifact while no
    credential value appears anywhere: every state file (records,
    scripts, staging, manifests), both transports' logs and the pushed
    batch script are scanned."""
    run_id = make_run_id("dev-m7-g05-ac03-cycle")
    ctx = make_context(run_id=run_id)
    job_id = generate_id("job", run_id)

    # Worker 1 submits the job through its own transport.
    transport_a = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter_a, _, _ = make_adapter(tmp_path, transport=transport_a)
    prepared = adapter_a.prepare(ctx)
    adapter_a.submit(ctx)
    assert prepared.job_id == job_id  # the predicted id is the created id

    # Worker 1 exits; a fresh instance drives the job to completion and
    # collects it.
    fresh, transport_b = make_fresh_adapter(tmp_path)
    transport_b.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),  # sacct
        ]
    )
    status = fresh.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.external_id == EXTERNAL_ID
    transport_b.pull_script = [None]
    collected = fresh.collect(job_id)
    assert collected.artifact_ids == (generate_id("artifact", job_id, "result.txt"),)

    # The deterministic ids are present in the persisted state.
    record = read_job_file(tmp_path, job_id)
    assert record["job_id"] == job_id
    assert record["run_id"] == run_id
    assert record["external_id"] == EXTERNAL_ID
    assert record["artifact_ids"] == [generate_id("artifact", job_id, "result.txt")]

    # And no credential value appears anywhere.
    secrets = [
        value
        for value in (
            DEFAULT_CREDENTIALS.password,
            DEFAULT_CREDENTIALS.key_passphrase,
            DEFAULT_CREDENTIALS.private_key_path,
            DEFAULT_CREDENTIALS.username,
        )
        if value is not None
    ]
    state_files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert state_files, "the state directory must contain files to walk"
    for path in state_files:
        content = path.read_text(encoding="utf-8", errors="replace")
        for secret in secrets:
            assert secret not in content, f"{path} contains credential {secret!r}"
    for entry_name, entry_args in [*transport_a.log, *transport_b.log]:
        text = repr((entry_name, entry_args))
        for secret in secrets:
            assert secret not in text, f"transport log embeds credential: {text}"
    for _, _, script in transport_a.pushed:
        text = script.decode("utf-8", errors="replace")
        for secret in secrets:
            assert secret not in text, "pushed script embeds a credential"


# ---------------------------------------------------------------------------
# Supporting end-to-end flow: one Run, both external backends, both
# workers gone, fresh instances finish both flows
# ---------------------------------------------------------------------------


def test_m7g05_end_to_end_single_run_both_backends_across_fresh_instances(
    tmp_path: Path,
) -> None:
    """The full integration story for one Run: worker 1 dispatches the
    experiment package to the lab and submits the computation to the
    Slurm cluster; both workers exit; fresh adapter instances over the
    same state directories detect and collect the returned lab package,
    reconcile the job to completion and collect its outputs; the Run
    state ends referencing both external ids deterministically."""
    run_id = make_run_id("dev-m7-g05-e2e")
    base = tmp_path / "lab"
    compute = tmp_path / "compute"
    package = make_package(run_id)
    package_id = package["package_id"]
    ctx = make_context(run_id=run_id)

    # Worker 1: lab dispatch + Slurm submit.
    worker_one = FilesystemLabAdapter(base)
    dispatch_record = worker_one.dispatch(package, dispatched_at=FIXED_STAMP)
    transport_a = FakeSlurmTransport(
        run_script=[
            RemoteResult(exit_code=0, stdout=""),
            RemoteResult(
                exit_code=0, stdout=f"Submitted batch job {EXTERNAL_ID}\n"
            ),
        ]
    )
    adapter_a, _, _ = make_adapter(compute, transport=transport_a)
    prepared = adapter_a.prepare(ctx)
    adapter_a.submit(ctx)
    job_id = prepared.job_id

    # Both workers exit. The lab returns the Result Package.
    write_result_package(
        base,
        run_id,
        make_result_manifest(run_id, package_id),
        {"raw-data.csv": b"yield=0.87\n"},
    )

    # Fresh instances finish both flows from the persisted state alone.
    lab_monitor = FilesystemLabAdapter(base)
    status = lab_monitor.status(dispatch_record.dispatch_id)
    assert status.state is DispatchState.RESULT_AVAILABLE
    collected_package = lab_monitor.collect(dispatch_record.dispatch_id)
    assert collected_package.run_id == run_id
    assert collected_package.manifest.package_id == package_id
    assert collected_package.collected_files == ("raw-data.csv",)

    cluster_monitor, transport_b = make_fresh_adapter(compute)
    transport_b.run_script.extend(
        [
            RemoteResult(exit_code=0, stdout=""),  # squeue: gone
            RemoteResult(exit_code=0, stdout="COMPLETED|0:0\n"),  # sacct
        ]
    )
    status = cluster_monitor.status(job_id)
    assert status.state is JobState.COMPLETED
    assert status.external_id == EXTERNAL_ID
    transport_b.pull_script = [None]
    collected_job = cluster_monitor.collect(job_id)
    assert collected_job.artifact_ids == (
        generate_id("artifact", job_id, "result.txt"),
    )

    # The Run references both external ids deterministically.
    run = make_run(
        run_id,
        backend=SLURM_BACKEND_NAME,
        dispatch_id=dispatch_record.dispatch_id,
        job_id=job_id,
    )
    data = run.to_dict()
    assert data["external"]["dispatch_id"] == generate_id(
        "dispatch", package_id, run_id
    )
    assert data["external"]["job_id"] == generate_id("job", run_id)
    assert validate_object("run", data) == []
