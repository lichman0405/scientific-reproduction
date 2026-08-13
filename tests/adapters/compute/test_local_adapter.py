"""Tests for the local ComputeAdapter (DEV-M7-G02, deliverable).

Direct per-AC coverage, named after the acceptance criteria:

* ``test_ac01_*`` -- the six-operation interface
  (prepare/submit/status/collect/cancel/resume) of 15-ADAPTER-SPEC.md
  section 3 and 11-COMPUTATION-SUBSYSTEM.md section 2.
* ``test_ac02_*`` -- job identity persists independently of the worker
  session object: a **fresh adapter instance** over the same state
  directory recovers the job from the durable record alone
  (``<state_dir>/jobs/<job_id>.json``), and ``resume`` re-attaches to
  the underlying process by pid.
* ``test_ac03_*`` -- result collection produces **artifact
  registrations** through the real DEV-M3-G02 ``ArtifactRegistry``
  (one manifest file per artifact under ``<state_dir>/manifests/``,
  deterministic ids, real SHA-256, run id, producer stamp).

Determinism: every test injects a scripted :class:`FakeLauncher`
(processes never touch a real OS process except the fixture-launched
trivial children of the ``test_real_*`` tests, driven to completion via
the launcher's ``wait()``), a :class:`FakeClock` producing the fixed
``FIXED_STAMP`` timestamp (no wall clock), and ``tmp_path`` state
directories. No randomness, no network, no sleeps anywhere.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scientific_reproduction.adapters.compute.local import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BACKEND_NAME,
    EXIT_CODE_UNAVAILABLE_NOTE,
    TERMINATE_PENDING_NOTE,
    CancelledJob,
    CollectedJob,
    ComputeCollectError,
    ComputeJobIdentityError,
    ComputeJobLaunchError,
    ComputeJobNotFoundError,
    ComputeJobRecordError,
    ComputeJobStateError,
    JobRecord,
    JobState,
    JobStatus,
    LocalComputeAdapter,
    PreparedJob,
    ProcessAttachError,
    ProcessHandle,
    ProcessLauncher,
    ProcessProbe,
    ResumedJob,
    RunContext,
    SubmittedJob,
    SubprocessLauncher,
)
from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.ids import generate_id, is_valid_id

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: The adapter's producer stamp written into collected manifests.
PRODUCER_STAMP = f"adapter:{ADAPTER_ID}@v{ADAPTER_VERSION}"


def make_run_id(label: str = "goal-1") -> str:
    """A deterministic generated run id for a fixture label."""
    return generate_id("run", label)


def make_context(
    run_id: str | None = None,
    *,
    command: tuple[str, ...] = (sys.executable, "-c", "pass"),
    working_directory: str = ".",
    outputs: tuple[str, ...] = ("result.txt",),
) -> RunContext:
    """A default run context; pass explicit values to vary one axis."""
    return RunContext(
        run_id=run_id if run_id is not None else make_run_id(),
        command=command,
        working_directory=working_directory,
        outputs=outputs,
    )


def write_output(workdir: Path, name: str = "result.txt", content: str = "42") -> Path:
    """Write a declared output file into a working directory."""
    path = workdir / name
    path.write_text(content, encoding="utf-8")
    return path


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


class FakeProcessHandle(ProcessHandle):
    """Scripted process handle: state changes only on explicit calls.

    ``terminate`` records the call and marks the process exited with
    ``-15`` unless ``terminate_keeps_running`` is set (the process
    ignored the signal, the ``TERMINATE_PENDING_NOTE`` path).
    """

    def __init__(
        self, pid: int, *, probe: ProcessProbe | None = None
    ) -> None:
        self._pid = pid
        self._probe = probe if probe is not None else ProcessProbe(running=True)
        self.terminated = False
        self.killed = False
        self.terminate_keeps_running = False
        self.wait_calls = 0

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> ProcessProbe:
        return self._probe

    def wait(self) -> ProcessProbe:
        self.wait_calls += 1
        return self._probe

    def terminate(self) -> None:
        self.terminated = True
        if not self.terminate_keeps_running:
            self._probe = ProcessProbe(running=False, exit_code=-15)

    def kill(self) -> None:
        self.killed = True
        self._probe = ProcessProbe(running=False, exit_code=-9)

    def mark_running(self) -> None:
        self._probe = ProcessProbe(running=True)

    def mark_exited(self, exit_code: int | None = 0) -> None:
        """Script the process as exited (None = exit status unrecoverable)."""
        self._probe = ProcessProbe(running=False, exit_code=exit_code)


class FakeLauncher(ProcessLauncher):
    """Scripted process launcher: pids assigned 1, 2, ...; no real
    processes ever started."""

    def __init__(self) -> None:
        self._handles: dict[int, FakeProcessHandle] = {}
        self._next_pid = 1
        self.launch_calls: list[tuple[tuple[str, ...], Path]] = []
        self.attach_calls: list[int] = []
        self.fail_launch: BaseException | None = None

    def launch(self, command: Sequence[str], cwd: Path) -> ProcessHandle:
        self.launch_calls.append((tuple(command), Path(cwd)))
        if self.fail_launch is not None:
            raise self.fail_launch
        handle = FakeProcessHandle(self._next_pid)
        self._handles[self._next_pid] = handle
        self._next_pid += 1
        return handle

    def attach(self, pid: int) -> ProcessHandle:
        self.attach_calls.append(pid)
        if pid not in self._handles:
            raise ProcessAttachError(
                f"no process with pid {pid} was launched by this launcher"
            )
        return self._handles[pid]

    def handle(self, pid: int) -> FakeProcessHandle:
        """The scripted handle for ``pid`` (asserts it exists)."""
        if pid not in self._handles:
            raise AssertionError(f"no scripted handle for pid {pid}")
        return self._handles[pid]


def make_adapter(
    state_dir: Path,
    *,
    launcher: ProcessLauncher | None = None,
    now: FakeClock | None = None,
) -> tuple[LocalComputeAdapter, FakeLauncher | None, FakeClock]:
    """Build an adapter with a fresh FakeLauncher/FakeClock unless given."""
    launcher = launcher if launcher is not None else FakeLauncher()
    clock = now if now is not None else FakeClock(FIXED_STAMP)
    return (
        LocalComputeAdapter(state_dir, launcher=launcher, now=clock),
        launcher if isinstance(launcher, FakeLauncher) else None,
        clock,
    )


def read_job_file(state_dir: Path, job_id: str) -> dict[str, object]:
    """The on-disk durable record as parsed JSON."""
    return json.loads(
        (state_dir / "jobs" / f"{job_id}.json").read_text(encoding="utf-8")
    )


def completed_job(
    state_dir: Path,
    tmp_path: Path,
    *,
    outputs: tuple[str, ...] = ("result.txt",),
) -> tuple[LocalComputeAdapter, FakeLauncher, RunContext, str, Path]:
    """A completed job with its output file written; returns
    (adapter, launcher, context, job_id, workdir)."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    for name in outputs:
        write_output(workdir, name)
    launcher = FakeLauncher()
    adapter, _, _ = make_adapter(state_dir, launcher=launcher)
    ctx = make_context(working_directory=str(workdir), outputs=outputs)
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    launcher.handle(submitted.pid).mark_exited(0)
    status = adapter.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    return adapter, launcher, ctx, prepared.job_id, workdir


# ---------------------------------------------------------------------------
# AC-01: the six-operation interface
# ---------------------------------------------------------------------------


def test_ac01_full_interface_prepare_submit_status_collect_cancel_resume(
    tmp_path: Path,
) -> None:
    """The full ComputeAdapter interface works end to end on one job:
    prepare stages, submit launches and returns the persistent job id,
    status reports running then completed, collect registers artifacts,
    resume returns the terminal record, and cancelling a terminal job is
    rejected (AC-01)."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    write_output(workdir, "result.txt")
    adapter, launcher, clock = make_adapter(tmp_path)
    ctx = make_context(working_directory=str(workdir))

    prepared = adapter.prepare(ctx)
    assert isinstance(prepared, PreparedJob)
    assert prepared.state is JobState.PREPARED
    assert prepared.job_id == generate_id("job", ctx.run_id)
    assert prepared.command == ctx.command
    assert prepared.outputs == ctx.outputs
    assert prepared.created_at == FIXED_STAMP
    assert read_job_file(tmp_path, prepared.job_id)["state"] == "prepared"

    submitted = adapter.submit(ctx)
    assert isinstance(submitted, SubmittedJob)
    assert submitted.job_id == prepared.job_id
    assert submitted.state is JobState.RUNNING
    assert submitted.pid == 1
    assert submitted.submitted_at == FIXED_STAMP

    status = adapter.status(submitted.job_id)
    assert isinstance(status, JobStatus)
    assert status.state is JobState.RUNNING
    assert status.pid == 1

    handle = launcher.handle(submitted.pid)
    handle.mark_exited(0)
    status = adapter.status(submitted.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0

    collected = adapter.collect(submitted.job_id)
    assert isinstance(collected, CollectedJob)
    assert collected.artifact_ids == (
        generate_id("artifact", submitted.job_id, "result.txt"),
    )
    assert len(collected.artifacts) == 1
    assert collected.artifacts[0].uri == str(workdir / "result.txt")

    resumed = adapter.resume(submitted.job_id)
    assert isinstance(resumed, ResumedJob)
    assert resumed.state is JobState.COMPLETED

    with pytest.raises(ComputeJobStateError):
        adapter.cancel(submitted.job_id)
    assert clock.calls, "the clock was used for every recorded timestamp"


def test_prepare_stages_durable_record_and_working_directory(tmp_path: Path) -> None:
    """prepare writes the durable record to
    ``<state_dir>/jobs/<job_id>.json`` (AC-02) and creates the working
    directory."""
    workdir = tmp_path / "workdir"
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context(working_directory=str(workdir))

    prepared = adapter.prepare(ctx)
    assert workdir.is_dir()
    path = tmp_path / "jobs" / f"{prepared.job_id}.json"
    assert path.is_file()

    stored = read_job_file(tmp_path, prepared.job_id)
    assert stored["record_version"] == "1.0"
    assert stored["backend"] == "local"
    assert stored["job_id"] == prepared.job_id
    assert stored["run_id"] == ctx.run_id
    assert stored["state"] == "prepared"
    assert stored["command"] == list(ctx.command)
    assert stored["working_directory"] == str(workdir)
    assert stored["outputs"] == list(ctx.outputs)
    assert stored["created_at"] == FIXED_STAMP

    record = adapter.read_job(prepared.job_id)
    assert isinstance(record, JobRecord)
    assert record == JobRecord.from_dict(stored)
    assert record.state is JobState.PREPARED


def test_prepare_is_idempotent_but_rejects_conflicting_stage(tmp_path: Path) -> None:
    """Re-preparing the same run with identical content is a no-op;
    re-staging the same run with different content is rejected (job
    identity is a pure function of the run id)."""
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context()

    first = adapter.prepare(ctx)
    second = adapter.prepare(ctx)
    assert second == first
    assert second.created_at == FIXED_STAMP

    conflicting = make_context(
        run_id=ctx.run_id, command=(sys.executable, "-c", "raise SystemExit(9)")
    )
    with pytest.raises(ComputeJobStateError) as excinfo:
        adapter.prepare(conflicting)
    assert "already prepared with a different" in str(excinfo.value)
    assert first.job_id in str(excinfo.value)


def test_prepare_after_submit_raises(tmp_path: Path) -> None:
    """prepare after submit is a state error: the job already left the
    prepared state."""
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context()
    adapter.prepare(ctx)
    adapter.submit(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.prepare(ctx)


def test_submit_launches_with_staged_command_and_cwd(tmp_path: Path) -> None:
    """submit launches the staged command in the staged working directory
    through the injected launcher and writes the running record."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context(working_directory=str(workdir))
    prepared = adapter.prepare(ctx)

    submitted = adapter.submit(ctx)
    assert launcher is not None
    assert launcher.launch_calls == [(ctx.command, Path(str(workdir)))]
    assert submitted.pid == 1

    stored = read_job_file(tmp_path, prepared.job_id)
    assert stored["state"] == "running"
    assert stored["pid"] == 1
    assert stored["submitted_at"] == FIXED_STAMP


def test_submit_without_prepare_raises_not_found(tmp_path: Path) -> None:
    """submit of an unprepared run raises a stable not-found error; no
    process is ever launched."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    with pytest.raises(ComputeJobNotFoundError) as excinfo:
        adapter.submit(ctx)
    assert generate_id("job", ctx.run_id) in str(excinfo.value)
    assert launcher is not None
    assert launcher.launch_calls == []


def test_submit_twice_raises_state_error(tmp_path: Path) -> None:
    """a job can be submitted exactly once."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    adapter.prepare(ctx)
    adapter.submit(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.submit(ctx)
    assert launcher is not None
    assert len(launcher.launch_calls) == 1


def test_status_reports_running_from_process_state(tmp_path: Path) -> None:
    """status of a running job reports the live process state through the
    launcher's attach, without writing a decision."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    status = adapter.status(submitted.job_id)
    assert status.state is JobState.RUNNING
    assert status.pid == submitted.pid
    assert status.exit_code is None
    assert launcher is not None
    assert launcher.attach_calls == [submitted.pid]
    assert read_job_file(tmp_path, submitted.job_id)["state"] == "running"


def test_status_records_exit_zero_as_completed(tmp_path: Path) -> None:
    """status observing a zero exit records the completed decision with
    the exit code and completion timestamp."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    launcher.handle(submitted.pid).mark_exited(0)
    status = adapter.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0

    stored = read_job_file(tmp_path, prepared.job_id)
    assert stored["state"] == "completed"
    assert stored["exit_code"] == 0
    assert stored["completed_at"] == FIXED_STAMP


def test_status_records_nonzero_exit_as_failed_with_stable_error(
    tmp_path: Path,
) -> None:
    """status observing a non-zero exit records a failed decision with a
    stable, specific error string."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    launcher.handle(submitted.pid).mark_exited(3)
    status = adapter.status(prepared.job_id)
    assert status.state is JobState.FAILED
    assert status.exit_code == 3
    assert status.error == "process exited with status 3"

    stored = read_job_file(tmp_path, prepared.job_id)
    assert stored["error"] == "process exited with status 3"


def test_status_records_unknown_exit_as_completed_with_recovery_note(
    tmp_path: Path,
) -> None:
    """status observing an exited process whose status cannot be
    recovered (Windows re-attach / already reaped) records completed with
    the stable recovery note; collect still verifies outputs."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    launcher.handle(submitted.pid).mark_exited(None)
    status = adapter.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code is None
    assert status.recovery_note == EXIT_CODE_UNAVAILABLE_NOTE


def test_status_of_terminal_record_is_durable_record_driven(tmp_path: Path) -> None:
    """once the record is terminal, status answers from the durable
    record alone: the launcher is never contacted again."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    launcher.handle(submitted.pid).mark_exited(0)
    assert adapter.status(prepared.job_id).state is JobState.COMPLETED

    assert launcher is not None
    launcher.attach_calls.clear()
    for _ in range(3):
        status = adapter.status(prepared.job_id)
        assert status.state is JobState.COMPLETED
        assert status.exit_code == 0
    assert launcher.attach_calls == []


def test_status_unknown_job_raises_not_found(tmp_path: Path) -> None:
    """status of a job with no durable record raises the stable
    not-found error."""
    adapter, _, _ = make_adapter(tmp_path)
    job_id = generate_id("job", make_run_id("never-prepared"))
    with pytest.raises(ComputeJobNotFoundError):
        adapter.status(job_id)


def test_invalid_job_id_raises_stable_identity_error(tmp_path: Path) -> None:
    """every operation validates the job id and raises the stable
    identity error for a malformed one."""
    adapter, _, _ = make_adapter(tmp_path)
    for operation in (
        adapter.status,
        adapter.collect,
        adapter.cancel,
        adapter.resume,
        adapter.read_job,
    ):
        with pytest.raises(ComputeJobIdentityError):
            operation("not-a-job-id")


def test_run_context_shape_is_validated() -> None:
    """RunContext validates its identity-bearing shape at the boundary:
    malformed run ids and unsafe output names raise
    ComputeJobIdentityError; mistyped fields raise TypeError."""
    with pytest.raises(ComputeJobIdentityError):
        make_context(run_id="not-an-id")
    with pytest.raises(ComputeJobIdentityError):
        make_context(run_id="sr_goal_" + "a" * 32)

    with pytest.raises(TypeError):
        RunContext(run_id=make_run_id(), command=(), working_directory=".")
    with pytest.raises(TypeError):
        RunContext(
            run_id=make_run_id(),
            command=("python", ""),
            working_directory=".",
        )
    with pytest.raises(TypeError):
        RunContext(
            run_id=make_run_id(),
            command=("python", "-c", "pass"),
            working_directory="  ",
        )

    for unsafe in ("../result.txt", "a/b.txt", "a\\b.txt", ".", "..", "*", "?.txt"):
        with pytest.raises(ComputeJobIdentityError):
            make_context(outputs=(unsafe,))
    with pytest.raises(ComputeJobIdentityError):
        make_context(outputs=("same.txt", "same.txt"))


def test_cancel_stops_running_job_and_records_decision(tmp_path: Path) -> None:
    """cancel terminates the running process and records the cancelled
    decision with the observed exit status."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    cancelled = adapter.cancel(prepared.job_id)
    assert isinstance(cancelled, CancelledJob)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.exit_code == -15
    assert cancelled.cancelled_at == FIXED_STAMP
    handle = launcher.handle(submitted.pid)
    assert handle.terminated is True

    stored = read_job_file(tmp_path, prepared.job_id)
    assert stored["state"] == "cancelled"
    assert stored["exit_code"] == -15
    assert stored["cancelled_at"] == FIXED_STAMP


def test_cancel_prepared_job_marks_it_cancelled(tmp_path: Path) -> None:
    """cancel of a prepared (not yet submitted) job records the cancelled
    decision without touching any process."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)

    cancelled = adapter.cancel(prepared.job_id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.exit_code is None
    assert launcher is not None
    assert launcher.launch_calls == []
    assert launcher.attach_calls == []
    assert read_job_file(tmp_path, prepared.job_id)["state"] == "cancelled"


def test_cancel_terminal_job_raises(tmp_path: Path) -> None:
    """cancel of a completed or already-cancelled job is a state error."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    launcher.handle(submitted.pid).mark_exited(0)
    adapter.status(prepared.job_id)
    with pytest.raises(ComputeJobStateError):
        adapter.cancel(prepared.job_id)

    second = make_context(run_id=make_run_id("cancel-twice"))
    second_prepared = adapter.prepare(second)
    adapter.cancel(second_prepared.job_id)
    with pytest.raises(ComputeJobStateError):
        adapter.cancel(second_prepared.job_id)


def test_cancel_records_recovery_note_when_process_ignores_terminate(
    tmp_path: Path,
) -> None:
    """when the process is still running after terminate, the cancelled
    decision carries the stable pending-termination recovery note."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    launcher.handle(submitted.pid).terminate_keeps_running = True
    cancelled = adapter.cancel(prepared.job_id)
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.exit_code is None
    assert read_job_file(tmp_path, prepared.job_id)["recovery_note"] == (
        TERMINATE_PENDING_NOTE
    )


def test_resume_reattaches_to_running_job(tmp_path: Path) -> None:
    """resume of a running job re-attaches by pid and reports the process
    state without restarting anything."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    resumed = adapter.resume(prepared.job_id)
    assert resumed.state is JobState.RUNNING
    assert resumed.pid == submitted.pid

    launcher.handle(submitted.pid).mark_exited(0)
    resumed = adapter.resume(prepared.job_id)
    assert resumed.state is JobState.COMPLETED
    assert resumed.exit_code == 0
    assert launcher.launch_calls == [(ctx.command, Path("."))]


def test_launch_failure_is_a_stable_decision_error(tmp_path: Path) -> None:
    """a launcher failure surfaces as ComputeJobLaunchError and the
    durable record stays prepared -- a decision record, not an exception
    trail."""
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)

    launcher.fail_launch = OSError("cannot start process")
    with pytest.raises(ComputeJobLaunchError) as excinfo:
        adapter.submit(ctx)
    assert prepared.job_id in str(excinfo.value)

    launcher.fail_launch = ProcessAttachError("boom")
    with pytest.raises(ComputeJobLaunchError):
        adapter.submit(ctx)

    assert read_job_file(tmp_path, prepared.job_id)["state"] == "prepared"
    assert adapter.status(prepared.job_id).state is JobState.PREPARED


def test_corrupt_durable_record_raises_stable_error(tmp_path: Path) -> None:
    """a corrupt or contract-violating durable record surfaces as a
    stable ComputeJobRecordError, never a crash."""
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    job_path = tmp_path / "jobs" / f"{prepared.job_id}.json"
    valid = read_job_file(tmp_path, prepared.job_id)  # the contract baseline

    job_path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ComputeJobRecordError) as excinfo:
        adapter.status(prepared.job_id)
    assert "corrupt" in str(excinfo.value)

    job_path.write_text(json.dumps(dict(valid, record_version="9.9")), encoding="utf-8")
    with pytest.raises(ComputeJobRecordError):
        adapter.status(prepared.job_id)

    missing_state = dict(valid)
    del missing_state["state"]
    job_path.write_text(json.dumps(missing_state), encoding="utf-8")
    with pytest.raises(ComputeJobRecordError):
        adapter.status(prepared.job_id)


def test_identical_inputs_produce_byte_identical_records(tmp_path: Path) -> None:
    """identical injected inputs (same run id, command, working
    directory, outputs, fixed clock) produce byte-identical durable
    records in independent state directories."""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first, _, _ = make_adapter(first_dir)
    second_adapter, _, _ = make_adapter(second_dir)
    ctx = make_context()  # working_directory "." resolves identically

    first_job = first.prepare(ctx)
    second_job = second_adapter.prepare(ctx)
    assert first_job.job_id == second_job.job_id
    first.submit(ctx)
    second_adapter.submit(ctx)

    first_bytes = (first_dir / "jobs" / f"{first_job.job_id}.json").read_bytes()
    second_bytes = (second_dir / "jobs" / f"{second_job.job_id}.json").read_bytes()
    assert first_bytes == second_bytes


# ---------------------------------------------------------------------------
# AC-02: job identity persists independently of the worker session
# ---------------------------------------------------------------------------


def test_ac02_resume_from_durable_record_alone(tmp_path: Path) -> None:
    """after the submitting session is disposed, a fresh adapter over the
    same state directory resumes the running job from the durable record
    alone (11-COMPUTATION-SUBSYSTEM.md section 6: the original worker
    session is never a single point of failure)."""
    shared_launcher = FakeLauncher()
    ctx = make_context()
    session_a = LocalComputeAdapter(
        tmp_path, launcher=shared_launcher, now=FakeClock(FIXED_STAMP)
    )
    prepared = session_a.prepare(ctx)
    submitted = session_a.submit(ctx)
    assert submitted.state is JobState.RUNNING

    # Session A is disposed: only the durable record and the process
    # boundary remain.
    session_b = LocalComputeAdapter(
        tmp_path, launcher=shared_launcher, now=FakeClock(FIXED_STAMP)
    )
    resumed = session_b.resume(prepared.job_id)
    assert resumed.state is JobState.RUNNING
    assert resumed.pid == submitted.pid
    assert session_b.read_job(prepared.job_id).created_at == FIXED_STAMP

    shared_launcher.handle(submitted.pid).mark_exited(0)
    status = session_b.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0


def test_ac02_fresh_session_status_and_collect_job_from_durable_record(
    tmp_path: Path,
) -> None:
    """the brief's exact scenario: adapter A submits, A is disposed, and
    a fresh adapter B statuses and collects the job from the durable
    record alone."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    write_output(workdir, "result.txt")
    shared_launcher = FakeLauncher()
    ctx = make_context(working_directory=str(workdir))

    session_a = LocalComputeAdapter(
        tmp_path, launcher=shared_launcher, now=FakeClock(FIXED_STAMP)
    )
    prepared = session_a.prepare(ctx)
    submitted = session_a.submit(ctx)
    assert submitted.state is JobState.RUNNING

    session_b = LocalComputeAdapter(
        tmp_path, launcher=shared_launcher, now=FakeClock(FIXED_STAMP)
    )
    status = session_b.status(prepared.job_id)
    assert status.state is JobState.RUNNING
    assert status.job_id == prepared.job_id
    assert status.run_id == ctx.run_id

    shared_launcher.handle(submitted.pid).mark_exited(0)
    status = session_b.status(prepared.job_id)
    assert status.state is JobState.COMPLETED

    collected = session_b.collect(prepared.job_id)
    assert collected.artifact_ids == (
        generate_id("artifact", prepared.job_id, "result.txt"),
    )
    assert session_b.read_job(prepared.job_id).collected_at == FIXED_STAMP


def test_ac02_real_process_job_survives_worker_session(tmp_path: Path) -> None:
    """a real child process launched by one adapter instance is
    status/collect-able by a fresh adapter instance with its own fresh
    launcher -- the re-attach path (liveness; exit status only where the
    OS recovers it, else the stable recovery note)."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    script = (
        "import pathlib, sys;"
        " pathlib.Path(sys.argv[1]).write_text('42', encoding='utf-8')"
    )
    command = (sys.executable, "-c", script, str(workdir / "result.txt"))
    ctx = make_context(command=command, working_directory=str(workdir))

    session_a = LocalComputeAdapter(
        tmp_path, launcher=SubprocessLauncher(), now=FakeClock(FIXED_STAMP)
    )
    prepared = session_a.prepare(ctx)
    submitted = session_a.submit(ctx)

    # Drive the child deterministically to completion before disposing
    # the submitting session (no sleeps, no polling loops).
    probe = session_a.launcher.attach(submitted.pid).wait()
    assert not probe.running

    session_b = LocalComputeAdapter(
        tmp_path, launcher=SubprocessLauncher(), now=FakeClock(FIXED_STAMP)
    )
    status = session_b.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    if status.exit_code is not None:
        assert status.exit_code == 0
    else:
        assert status.recovery_note == EXIT_CODE_UNAVAILABLE_NOTE

    collected = session_b.collect(prepared.job_id)
    assert collected.artifact_ids == (
        generate_id("artifact", prepared.job_id, "result.txt"),
    )
    manifest = collected.artifacts[0]
    assert manifest.sha256 == compute_sha256(workdir / "result.txt")
    assert manifest.uri == str(workdir / "result.txt")


# ---------------------------------------------------------------------------
# AC-03: result collection produces artifact registrations
# ---------------------------------------------------------------------------


def test_ac03_collect_registers_artifact_manifests_through_real_registry(
    tmp_path: Path,
) -> None:
    """collect registers every declared output through the real
    DEV-M3-G02 ArtifactRegistry: one manifest file per artifact under
    ``<state_dir>/manifests/`` with a deterministic id, real SHA-256 and
    byte size, the run id and the adapter's producer stamp."""
    adapter, launcher, _ = make_adapter(tmp_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    write_output(workdir, "b.txt", content="second")
    write_output(workdir, "a.txt", content="first")
    ctx = make_context(working_directory=str(workdir), outputs=("b.txt", "a.txt"))
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    launcher.handle(submitted.pid).mark_exited(0)
    adapter.status(prepared.job_id)

    collected = adapter.collect(prepared.job_id)
    registry = ArtifactRegistry(tmp_path / "manifests")

    assert collected.artifact_ids == (
        generate_id("artifact", prepared.job_id, "a.txt"),
        generate_id("artifact", prepared.job_id, "b.txt"),
    )
    assert [m.artifact_id for m in registry.list()] == list(collected.artifact_ids)
    assert len(list(registry.list())) == 2

    by_name = {m.metadata["output_name"]: m for m in collected.artifacts}
    assert set(by_name) == {"a.txt", "b.txt"}
    for name, manifest in by_name.items():
        path = workdir / name
        assert manifest.artifact_id == generate_id(
            "artifact", prepared.job_id, name
        )
        assert is_valid_id(manifest.artifact_id, "artifact")
        assert manifest.uri == str(path)
        assert manifest.sha256 == compute_sha256(path)
        assert manifest.size_bytes == path.stat().st_size
        assert manifest.run_id == ctx.run_id
        assert manifest.producer == PRODUCER_STAMP
        assert manifest.created_at == FIXED_STAMP
        assert manifest.metadata == {
            "job_id": prepared.job_id,
            "output_name": name,
            "backend": BACKEND_NAME,
        }
        assert (tmp_path / "manifests" / f"{manifest.artifact_id}.json").is_file()

    assert read_job_file(tmp_path, prepared.job_id)["collected_at"] == FIXED_STAMP
    assert read_job_file(tmp_path, prepared.job_id)["artifact_ids"] == list(
        collected.artifact_ids
    )


def test_collect_recollect_is_idempotent(tmp_path: Path) -> None:
    """re-collecting an already-collected job returns the same
    registrations without rewriting anything."""
    adapter, _, _, job_id, _ = completed_job(tmp_path, tmp_path)

    first = adapter.collect(job_id)
    second = adapter.collect(job_id)
    assert second.artifact_ids == first.artifact_ids
    assert second.collected_at == first.collected_at == FIXED_STAMP
    assert [m.to_dict() for m in second.artifacts] == [
        m.to_dict() for m in first.artifacts
    ]
    assert len(list(ArtifactRegistry(tmp_path / "manifests").list())) == 1


def test_collect_missing_declared_output_raises_stable_error(tmp_path: Path) -> None:
    """collect of a completed job whose declared output file is missing
    raises the stable collect error naming the output."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    adapter, launcher, _ = make_adapter(tmp_path)
    ctx = make_context(working_directory=str(workdir))
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)
    launcher.handle(submitted.pid).mark_exited(0)
    adapter.status(prepared.job_id)

    with pytest.raises(ComputeCollectError) as excinfo:
        adapter.collect(prepared.job_id)
    assert "result.txt" in str(excinfo.value)
    assert read_job_file(tmp_path, prepared.job_id).get("collected_at") is None


def test_collect_requires_completed_job(tmp_path: Path) -> None:
    """collect of a prepared or running job is a state error."""
    adapter, _, _ = make_adapter(tmp_path)
    ctx = make_context()
    prepared = adapter.prepare(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.collect(prepared.job_id)
    submitted = adapter.submit(ctx)
    with pytest.raises(ComputeJobStateError):
        adapter.collect(submitted.job_id)


# ---------------------------------------------------------------------------
# Real subprocess integration (fixture-launched trivial children only)
# ---------------------------------------------------------------------------


def test_real_subprocess_completes_and_collects_outputs(tmp_path: Path) -> None:
    """end to end with the default SubprocessLauncher: a trivial child
    writes its declared output, the job completes, and collect registers
    it through the real registry."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    script = (
        "import pathlib, sys;"
        " pathlib.Path(sys.argv[1]).write_text('42', encoding='utf-8')"
    )
    command = (sys.executable, "-c", script, str(workdir / "result.txt"))
    adapter = LocalComputeAdapter(
        tmp_path, launcher=SubprocessLauncher(), now=FakeClock(FIXED_STAMP)
    )
    ctx = make_context(command=command, working_directory=str(workdir))
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    probe = adapter.launcher.attach(submitted.pid).wait()
    assert not probe.running
    status = adapter.status(prepared.job_id)
    assert status.state is JobState.COMPLETED
    assert status.exit_code == 0

    collected = adapter.collect(prepared.job_id)
    assert collected.artifact_ids == (
        generate_id("artifact", prepared.job_id, "result.txt"),
    )
    manifest = collected.artifacts[0]
    expected = compute_sha256(workdir / "result.txt")
    assert manifest.sha256 == expected
    assert manifest.size_bytes == (workdir / "result.txt").stat().st_size


def test_real_subprocess_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    """a child exiting non-zero is recorded as a failed decision with the
    stable error."""
    command = (sys.executable, "-c", "import sys; sys.exit(3)")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    adapter = LocalComputeAdapter(
        tmp_path, launcher=SubprocessLauncher(), now=FakeClock(FIXED_STAMP)
    )
    ctx = make_context(command=command, working_directory=str(workdir))
    prepared = adapter.prepare(ctx)
    submitted = adapter.submit(ctx)

    adapter.launcher.attach(submitted.pid).wait()
    status = adapter.status(prepared.job_id)
    assert status.state is JobState.FAILED
    assert status.exit_code == 3
    assert status.error == "process exited with status 3"

    with pytest.raises(ComputeJobStateError):
        adapter.collect(prepared.job_id)
