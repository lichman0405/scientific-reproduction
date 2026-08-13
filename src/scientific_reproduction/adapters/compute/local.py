"""Local ComputeAdapter: local process execution with a persistent
external-job identity (DEV-M7-G02, deliverable).

Implements the ComputeAdapter contract of the frozen specs:

* ``15-ADAPTER-SPEC.md`` section 3 ("ComputeAdapter"): the minimum
  semantic operations ``prepare`` / ``submit`` / ``status`` / ``collect``
  / ``cancel`` / ``resume``, with ``local`` as the v0.1 reference backend;
* ``11-COMPUTATION-SUBSYSTEM.md`` section 2 (the same six operations),
  section 3 (the adapter -- not the worker prompt -- owns working/scratch
  path conventions, job-ID persistence, result collection and file
  integrity checks) and section 6 ("Long-job behavior": the worker
  submits then exits; the original worker session is never a single
  point of failure).

Persistent external-job identity (AC-02)
----------------------------------------
Every job is represented by a **durable record** on disk at
``<state_dir>/jobs/<job_id>.json`` (one JSON file per job, written
atomically via ``core.atomic.atomic_write`` -- the M1 recovery
discipline). The record -- not any in-memory worker session object --
holds the job's identity and state. A **fresh adapter instance** over
the same state directory can ``status`` / ``collect`` / ``cancel`` /
``resume`` the same job from the durable record alone, and ``resume``
re-attaches to the underlying process by pid (the launcher's
``attach``). The job id is a pure function of the run identity
(``core.ids.generate_id("job", run_id)``), so it is stable,
deterministic and a safe registry id.

The durable record is a **runtime record, not a schema object**: no
``schemas/job.schema.yaml`` exists, so -- like ``core.leases`` -- it is
validated here against the documented :class:`JobRecord` contract
instead of the ``FilesystemStateBackend`` schema gate.

Result collection and artifact registration (AC-03)
---------------------------------------------------
``collect`` registers every declared output as an ``ArtifactManifest``
through the **real** DEV-M3-G02 ``ArtifactRegistry`` (the registration
API behind the workers' ``ARTIFACT_REGISTER`` action,
``14-STATE-GIT-ARTIFACTS.md`` SS6): manifests live one file per artifact
under ``<state_dir>/manifests/``, carry a real SHA-256 and byte size
(``artifacts.checksum``), a deterministic artifact id
(``generate_id("artifact", job_id, output_name)``), the run id and the
adapter's producer stamp. Collection is exactly-once with verified
idempotent re-collection.

Determinism and injectable surfaces
-----------------------------------
Everything a session can vary is injected: the command and working
directory arrive per call in the frozen :class:`RunContext`; the state
directory, the clock (a ``now`` callable producing timestamp strings --
no wall clock in the tested path) and the process launcher
(:class:`ProcessLauncher`) are constructor arguments. The shipped
:class:`SubprocessLauncher` wraps ``subprocess.Popen`` and can re-attach
to a process by pid (liveness always; exit status where the OS allows,
i.e. POSIX). Identical injected inputs produce byte-identical durable
records; failure states are decision records with stable, specific
error strings. No randomness, no network, no wall-clock dependence
anywhere in the tested path.

Error discipline follows the house paradigm: ``TypeError`` at public
boundaries for wrong types, a ``ValueError``-subclass error hierarchy
with stable messages otherwise.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.artifacts.exceptions import (
    ArtifactExistsError,
    ArtifactFileError,
    ArtifactNotFoundError,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import ArtifactManifest

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "BACKEND_NAME",
    "EXIT_CODE_UNAVAILABLE_NOTE",
    "JOBS_STATE_DIR",
    "ARTIFACTS_STATE_DIR",
    "JOB_RECORD_VERSION",
    "TERMINATE_PENDING_NOTE",
    "CancelledJob",
    "CollectedJob",
    "ComputeAdapterError",
    "ComputeCollectError",
    "ComputeJobIdentityError",
    "ComputeJobLaunchError",
    "ComputeJobNotFoundError",
    "ComputeJobRecordError",
    "ComputeJobStateError",
    "JobRecord",
    "JobState",
    "JobStatus",
    "LocalComputeAdapter",
    "PreparedJob",
    "ProcessAttachError",
    "ProcessHandle",
    "ProcessLauncher",
    "ProcessProbe",
    "ResumedJob",
    "RunContext",
    "SubprocessLauncher",
    "SubmittedJob",
    "utc_now",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The backend name stamped into every durable job record (the v0.1
#: reference backend of 15-ADAPTER-SPEC.md section 3).
BACKEND_NAME: str = "local"

#: Adapter identity (mirrors the ``adapter:<id>@v<version>`` producer
#: stamping of the research adapters).
ADAPTER_ID: str = "compute/local"

#: Adapter contract version. Bumped whenever a contract rule changes; the
#: same version always accepts the same run contexts and yields the same
#: records.
ADAPTER_VERSION: str = "1.0"

#: Version of the durable job-record schema (``record_version`` key of
#: :class:`JobRecord`); records of a different version are refused.
JOB_RECORD_VERSION: str = "1.0"

#: Registry directory of the durable job records, relative to the
#: injected state directory (``<state_dir>/jobs/<job_id>.json``).
JOBS_STATE_DIR: str = "jobs"

#: The artifact registry base directory of a compute state directory
#: (``14-STATE-GIT-ARTIFACTS.md`` SS6: manifests live under
#: ``manifests/``; ``<state_dir>/manifests/<artifact_id>.json``).
ARTIFACTS_STATE_DIR: str = "manifests"

#: Stable recovery note written when a re-attached process has exited but
#: its exit status could not be recovered (Windows re-attach, or a pid
#: already reaped by another session). The job is recorded as completed
#: because ``collect`` independently verifies every declared output.
EXIT_CODE_UNAVAILABLE_NOTE: str = (
    "process exited while the adapter was not attached; exit code"
    " unavailable (job recorded as completed; collect verifies declared"
    " outputs)"
)

#: Stable recovery note written when a cancelled process did not exit at
#: the moment of observation after ``terminate`` was requested.
TERMINATE_PENDING_NOTE: str = (
    "terminate was requested; the process had not exited at the moment of"
    " observation"
)

#: Producer stamp written into collected artifact manifests.
_PRODUCER_STAMP: str = f"adapter:{ADAPTER_ID}@v{ADAPTER_VERSION}"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ComputeAdapterError(ValueError):
    """Base error of the local compute adapter subsystem."""


class ComputeJobIdentityError(ComputeAdapterError):
    """Raised for malformed job/run ids and unsafe declared output names."""


class ComputeJobNotFoundError(ComputeAdapterError):
    """Raised when no durable job record exists for a job id."""


class ComputeJobStateError(ComputeAdapterError):
    """Raised when an operation is not allowed in the record's state.

    The state machine is strict: submit requires a prepared job, collect
    requires a completed job, cancel requires a non-terminal job, and a
    prepared job cannot be restaged with different content.
    """


class ComputeJobRecordError(ComputeAdapterError):
    """Raised when a durable job record is corrupt or violates the
    documented :class:`JobRecord` contract."""


class ComputeJobLaunchError(ComputeAdapterError):
    """Raised when the process launcher cannot start the job's command."""


class ComputeCollectError(ComputeAdapterError):
    """Raised when a completed job's declared outputs cannot be collected
    (missing output file, unreadable file, or an already-registered
    artifact with different content)."""


class ProcessAttachError(ComputeAdapterError):
    """Raised when a process cannot be attached to or waited on.

    Raised by launchers (unknown pid) and by re-attached handles that
    cannot perform a requested operation on this platform.
    """


# ---------------------------------------------------------------------------
# The injectable clock
# ---------------------------------------------------------------------------

#: A clock callable producing a timestamp string (the durable records'
#: ``created_at``/``submitted_at``/... values). Inject a deterministic
#: callable in tests; the default is UTC wall time.
Clock = Callable[[], str]


def utc_now() -> str:
    """Return the current UTC time as an ISO-8601 timestamp string
    (``YYYY-MM-DDTHH:MM:SS+00:00``)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# The run context (per-call injected inputs)
# ---------------------------------------------------------------------------


def _is_safe_output_name(name: str) -> bool:
    """True iff ``name`` is a safe relative file name.

    Declared outputs resolve to files inside the job's working directory
    and become artifact manifests; a name that escapes the directory
    (path separators, ``.``/``..``), that is absolute, or that carries
    glob metacharacters is rejected on every platform, like the registry
    id rules of the workers layer.
    """
    return (
        name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and not any(char in name for char in "*?[]")
        and Path(name).name == name
    )


@dataclass(frozen=True)
class RunContext:
    """The normalized per-call input of the compute adapter.

    The adapter is an IO boundary: the caller (a computation worker
    mapping from its ``GoalExecutionContextPackage``) names the exact
    command, the working directory the process runs in, and the declared
    output file names ``collect`` must turn into artifact registrations.
    ``run_id`` is the identity-bearing field: the persistent job id is a
    pure function of it (``generate_id("job", run_id)``), so the same run
    always maps to the same external job.

    Raises:
        ComputeJobIdentityError: ``run_id`` is not a well-formed
            generated run id, or a declared output name is not a safe
            relative file name.
        TypeError: ``command`` / ``outputs`` are not tuples, or an entry
            is not a non-empty string; ``working_directory`` is not a
            non-empty string.
    """

    run_id: str
    command: tuple[str, ...]
    working_directory: str
    outputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not is_valid_id(self.run_id, "run"):
            raise ComputeJobIdentityError(
                f"run_id {self.run_id!r} is not a well-formed generated run"
                " id (sr_run_<32 hex chars>)"
            )
        if not isinstance(self.command, tuple) or not self.command:
            raise TypeError(
                "RunContext.command must be a non-empty tuple of"
                " command-line strings"
            )
        for entry in self.command:
            if not isinstance(entry, str) or not entry:
                raise TypeError(
                    "RunContext.command entries must be non-empty strings,"
                    f" got {entry!r}"
                )
        if (
            not isinstance(self.working_directory, str)
            or not self.working_directory.strip()
        ):
            raise TypeError(
                "RunContext.working_directory must be a non-empty string"
                " path"
            )
        if not isinstance(self.outputs, tuple):
            raise TypeError(
                "RunContext.outputs must be a tuple of declared output"
                " file names"
            )
        seen: set[str] = set()
        for name in self.outputs:
            if not isinstance(name, str) or not _is_safe_output_name(name):
                raise ComputeJobIdentityError(
                    f"declared output name {name!r} is not a safe relative"
                    " file name (no '/', no '\\', not '.' or '..', no"
                    " glob metacharacters)"
                )
            if name in seen:
                raise ComputeJobIdentityError(
                    f"declared output {name!r} appears more than once"
                )
            seen.add(name)


# ---------------------------------------------------------------------------
# Process abstraction (injectable launcher)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessProbe:
    """One observation of a process's state.

    ``running=True``: the process is alive. ``running=False`` with
    ``exit_code`` set: the process has exited with that status.
    ``running=False`` with ``exit_code=None``: the process has exited but
    its status could not be recovered (re-attach limits).
    """

    running: bool
    exit_code: int | None = None


class ProcessHandle(ABC):
    """A handle to one process: poll, wait, terminate, kill."""

    @property
    @abstractmethod
    def pid(self) -> int:
        """The process id this handle refers to."""

    @abstractmethod
    def poll(self) -> ProcessProbe:
        """Observe the process without blocking."""

    @abstractmethod
    def wait(self) -> ProcessProbe:
        """Block until the process exits and return its state."""

    @abstractmethod
    def terminate(self) -> None:
        """Ask the process to terminate (best effort, non-blocking)."""

    @abstractmethod
    def kill(self) -> None:
        """Forcefully kill the process (best effort, non-blocking)."""


class ProcessLauncher(ABC):
    """The injectable process boundary of the compute adapter.

    ``launch`` starts a command in a working directory; ``attach``
    re-attaches to a process by pid (the durable record's re-attach
    path). Implementations are deterministic given their inputs: tests
    inject a scripted fake instead of touching real processes.
    """

    @abstractmethod
    def launch(self, command: Sequence[str], cwd: Path) -> ProcessHandle:
        """Start ``command`` in directory ``cwd`` and return a handle.

        Raises:
            ProcessAttachError: the command cannot be started.
        """

    @abstractmethod
    def attach(self, pid: int) -> ProcessHandle:
        """Return a handle to the process with ``pid``.

        Raises:
            ProcessAttachError: no such process can be attached to.
        """


class _SubprocessHandle(ProcessHandle):
    """Handle over a ``subprocess.Popen`` object."""

    def __init__(self, popen: subprocess.Popen) -> None:
        self._popen = popen

    @property
    def pid(self) -> int:
        return self._popen.pid

    def poll(self) -> ProcessProbe:
        code = self._popen.poll()
        if code is None:
            return ProcessProbe(running=True)
        return ProcessProbe(running=False, exit_code=code)

    def wait(self) -> ProcessProbe:
        return ProcessProbe(running=False, exit_code=self._popen.wait())

    def terminate(self) -> None:
        self._popen.terminate()

    def kill(self) -> None:
        self._popen.kill()


class _AttachedSubprocessHandle(ProcessHandle):
    """OS-level re-attachment to a process by pid (no ``Popen`` handle).

    On POSIX the exit status of an exited process is recovered with
    ``os.waitpid`` (WNOHANG for ``poll``, blocking for ``wait``), falling
    back to a liveness check when the child was already reaped elsewhere.
    On Windows the process state is queried with ``OpenProcess`` +
    ``GetExitCodeProcess`` (stdlib ``ctypes``): ``STILL_ACTIVE`` means
    running, an accessible terminated process yields its stored exit
    code, and an inaccessible pid yields ``ProcessProbe(running=False,
    exit_code=None)`` -- the defined "exited, exit code unknown" probe
    (the durable record then carries ``EXIT_CODE_UNAVAILABLE_NOTE``).
    """

    def __init__(self, pid: int) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"pid must be a positive int, got {pid!r}")
        self._pid = pid

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> ProcessProbe:
        if os.name == "posix":
            # os.WNOHANG is always defined on POSIX; getattr keeps the
            # module importable (and mypy clean) where it is not.
            wait_flags = getattr(os, "WNOHANG")
            try:
                waited, status = os.waitpid(self._pid, wait_flags)
            except ChildProcessError:
                waited = 0  # already reaped elsewhere: liveness check below
            if waited == self._pid:
                return ProcessProbe(
                    running=False,
                    exit_code=os.waitstatus_to_exitcode(status),
                )
        elif os.name == "nt":
            return self._poll_windows()
        try:
            os.kill(self._pid, 0)
        except ProcessLookupError:
            return ProcessProbe(running=False, exit_code=None)
        except PermissionError:
            # Exists but owned by another principal: alive.
            return ProcessProbe(running=True)
        return ProcessProbe(running=True)

    def _poll_windows(self) -> ProcessProbe:
        """Windows re-attach probe via GetExitCodeProcess (stdlib only)."""
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            process_query_limited_information, False, self._pid
        )
        if not handle:
            # No accessible process object under this pid: exited, and the
            # status is unrecoverable here (the documented exited-unknown
            # probe).
            return ProcessProbe(running=False, exit_code=None)
        try:
            exit_code = wintypes.DWORD()
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return ProcessProbe(running=False, exit_code=None)
            if exit_code.value == still_active:
                return ProcessProbe(running=True)
            return ProcessProbe(running=False, exit_code=exit_code.value)
        finally:
            kernel32.CloseHandle(handle)

    def wait(self) -> ProcessProbe:
        if os.name != "posix":
            raise ProcessAttachError(
                "wait() on a re-attached process is not supported on this"
                " platform; use the handle returned by launch()"
            )
        try:
            _, status = os.waitpid(self._pid, 0)
        except ChildProcessError:
            # Already reaped: report the current observation instead.
            return self.poll()
        return ProcessProbe(
            running=False, exit_code=os.waitstatus_to_exitcode(status)
        )

    def terminate(self) -> None:
        try:
            os.kill(self._pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone: terminating a dead process is a no-op

    def kill(self) -> None:
        try:
            if os.name == "posix":
                # SIGKILL is always defined on POSIX; getattr keeps the
                # module importable (and mypy clean) where it is not.
                os.kill(self._pid, getattr(signal, "SIGKILL"))
            else:
                # On Windows os.kill(pid, sig) maps to TerminateProcess.
                os.kill(self._pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class SubprocessLauncher(ProcessLauncher):
    """The default process launcher wrapping ``subprocess.Popen``.

    ``launch`` starts the command in the given working directory with
    stdout/stderr discarded (workers write their own output files; output
    capture is the launcher's concern) and remembers the handle per pid,
    so an in-session ``attach`` returns the live handle. ``attach`` of a
    pid this session never launched falls back to the OS-level
    re-attachment of :class:`_AttachedSubprocessHandle`.
    """

    def __init__(self) -> None:
        self._handles: dict[int, ProcessHandle] = {}

    def launch(self, command: Sequence[str], cwd: Path) -> ProcessHandle:
        try:
            popen = subprocess.Popen(
                list(command),
                cwd=str(cwd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError) as exc:
            raise ProcessAttachError(
                f"cannot launch command {list(command)!r} in"
                f" {str(cwd)!r}: {exc}"
            ) from exc
        handle = _SubprocessHandle(popen)
        self._handles[popen.pid] = handle
        return handle

    def attach(self, pid: int) -> ProcessHandle:
        if pid in self._handles:
            return self._handles[pid]
        return _AttachedSubprocessHandle(pid)


# ---------------------------------------------------------------------------
# The durable job record (AC-02: persisted, session-independent identity)
# ---------------------------------------------------------------------------


class JobState(StrEnum):
    """The states of the job state machine.

    ``prepared`` -> ``running`` -> ``completed`` | ``failed``; a
    ``prepared`` or ``running`` job may be ``cancelled``. Terminal states
    (``completed`` / ``failed`` / ``cancelled``) are decided once and the
    record keeps them.
    """

    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class JobRecord:
    """The durable, session-independent record of one external job.

    Persisted at ``<state_dir>/jobs/<job_id>.json`` and re-hydrated from
    disk on every ``status`` / ``collect`` / ``cancel`` / ``resume``
    (the M1 recovery discipline): a fresh adapter instance over the same
    state directory recovers the job from this record alone (AC-02).

    Field names are the exact JSON keys of the persisted record
    (``JobRecord.to_dict`` / ``from_dict`` round-trip them). The record
    is a runtime record -- there is no ``schemas/job.schema.yaml`` -- so
    ``from_dict`` validates against this documented contract instead
    (like ``core.leases``), with stable errors.
    """

    record_version: ClassVar[str] = JOB_RECORD_VERSION

    job_id: str
    run_id: str
    state: JobState
    command: tuple[str, ...]
    working_directory: str
    outputs: tuple[str, ...]
    created_at: str
    submitted_at: str | None = None
    pid: int | None = None
    exit_code: int | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    error: str | None = None
    collected_at: str | None = None
    artifact_ids: tuple[str, ...] = ()
    recovery_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-able dict of the record (``None`` optionals omitted,
        collections always emitted)."""
        data: dict[str, Any] = {
            "record_version": self.record_version,
            "backend": BACKEND_NAME,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "command": list(self.command),
            "working_directory": self.working_directory,
            "outputs": list(self.outputs),
            "created_at": self.created_at,
        }
        for key in (
            "submitted_at",
            "pid",
            "exit_code",
            "completed_at",
            "cancelled_at",
            "error",
            "collected_at",
            "artifact_ids",
            "recovery_note",
        ):
            value = getattr(self, key)
            if value is None:
                continue
            data[key] = list(value) if key == "artifact_ids" else value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> JobRecord:
        """Build a record from a plain dict (the job-record contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            ComputeJobRecordError: a required field is missing or a value
                violates the contract (unknown version/backend/state,
                malformed ids, mistyped or empty fields, unsafe entries).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"JobRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )

        def required(name: str) -> Any:
            if name not in data:
                raise ComputeJobRecordError(
                    f"job record missing required field {name!r}"
                )
            return data[name]

        record_version = required("record_version")
        if record_version != cls.record_version:
            raise ComputeJobRecordError(
                f"job record version {record_version!r} is not supported;"
                f" expected {cls.record_version!r}"
            )
        backend = required("backend")
        if backend != BACKEND_NAME:
            raise ComputeJobRecordError(
                f"job record backend {backend!r} is not {BACKEND_NAME!r}"
            )
        job_id = required("job_id")
        if not isinstance(job_id, str) or not is_valid_id(job_id, "job"):
            raise ComputeJobRecordError(
                f"job record job_id {job_id!r} is not a valid job id"
                " (sr_job_<32 hex chars>)"
            )
        run_id = required("run_id")
        if not isinstance(run_id, str) or not is_valid_id(run_id, "run"):
            raise ComputeJobRecordError(
                f"job record run_id {run_id!r} is not a valid run id"
                " (sr_run_<32 hex chars>)"
            )
        state_raw = required("state")
        try:
            state = JobState(state_raw)
        except ValueError:
            raise ComputeJobRecordError(
                f"job record state {state_raw!r} is not a JobState value"
            ) from None
        command = _require_str_tuple(data, "command", required=True)
        working_directory = _require_nonempty_str(
            required("working_directory"), "working_directory"
        )
        outputs = _require_str_tuple(data, "outputs", required=True)
        for name in outputs:
            if not _is_safe_output_name(name):
                raise ComputeJobRecordError(
                    f"job record output {name!r} is not a safe relative"
                    " file name"
                )
        created_at = _require_nonempty_str(required("created_at"), "created_at")
        submitted_at = _optional_str(data, "submitted_at")
        pid = _optional_int(data, "pid")
        if pid is not None and pid <= 0:
            raise ComputeJobRecordError(
                f"job record pid must be a positive int, got {pid}"
            )
        exit_code = _optional_int(data, "exit_code")
        completed_at = _optional_str(data, "completed_at")
        cancelled_at = _optional_str(data, "cancelled_at")
        error = _optional_str(data, "error")
        collected_at = _optional_str(data, "collected_at")
        artifact_ids = _require_str_tuple(data, "artifact_ids", required=False)
        for artifact_id in artifact_ids:
            if not is_valid_id(artifact_id, "artifact"):
                raise ComputeJobRecordError(
                    f"job record artifact id {artifact_id!r} is not a"
                    " valid artifact id (sr_artifact_<32 hex chars>)"
                )
        recovery_note = _optional_str(data, "recovery_note")
        return cls(
            job_id=job_id,
            run_id=run_id,
            state=state,
            command=command,
            working_directory=working_directory,
            outputs=outputs,
            created_at=created_at,
            submitted_at=submitted_at,
            pid=pid,
            exit_code=exit_code,
            completed_at=completed_at,
            cancelled_at=cancelled_at,
            error=error,
            collected_at=collected_at,
            artifact_ids=artifact_ids,
            recovery_note=recovery_note,
        )


def _require_nonempty_str(value: Any, name: str) -> str:
    """Return ``value`` as a non-empty string or raise a stable error."""
    if not isinstance(value, str) or not value:
        raise ComputeJobRecordError(
            f"job record field {name!r} must be a non-empty string, got"
            f" {value!r}"
        )
    return value


def _optional_str(data: Mapping[str, Any], name: str) -> str | None:
    """Return an optional string field (absent/None -> None)."""
    value = data.get(name)
    if value is None:
        return None
    return _require_nonempty_str(value, name)


def _optional_int(data: Mapping[str, Any], name: str) -> int | None:
    """Return an optional int field (absent/None -> None)."""
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ComputeJobRecordError(
            f"job record field {name!r} must be an int, got {value!r}"
        )
    return value


def _require_str_tuple(
    data: Mapping[str, Any], name: str, *, required: bool
) -> tuple[str, ...]:
    """Return a tuple-of-non-empty-strings field from ``data``.

    Raises:
        ComputeJobRecordError: the field is absent (when ``required``),
            not a list/tuple, or holds a non-string or empty entry.
    """
    value = data.get(name)
    if value is None:
        if required:
            raise ComputeJobRecordError(
                f"job record missing required field {name!r}"
            )
        return ()
    if not isinstance(value, (list, tuple)):
        raise ComputeJobRecordError(
            f"job record field {name!r} must be a list, got"
            f" {type(value).__name__}"
        )
    items = tuple(value)
    for item in items:
        if not isinstance(item, str) or not item:
            raise ComputeJobRecordError(
                f"job record field {name!r} entries must be non-empty"
                f" strings, got {item!r}"
            )
    return items


# ---------------------------------------------------------------------------
# Operation result records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedJob:
    """The outcome of ``prepare``: the staged durable record."""

    job_id: str
    run_id: str
    state: JobState
    created_at: str
    working_directory: str
    command: tuple[str, ...]
    outputs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the prepared job."""
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "working_directory": self.working_directory,
            "command": list(self.command),
            "outputs": list(self.outputs),
        }


@dataclass(frozen=True)
class SubmittedJob:
    """The outcome of ``submit``: the launched job and its persistent id."""

    job_id: str
    run_id: str
    state: JobState
    pid: int | None
    submitted_at: str

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the submitted job."""
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "pid": self.pid,
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True)
class JobStatus:
    """The outcome of ``status``: durable record + process observation."""

    job_id: str
    run_id: str
    state: JobState
    pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    collected_at: str | None = None
    recovery_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the status."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
        }
        for key in (
            "pid",
            "exit_code",
            "error",
            "collected_at",
            "recovery_note",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class CancelledJob:
    """The outcome of ``cancel``: the cancellation decision record."""

    job_id: str
    run_id: str
    state: JobState
    exit_code: int | None
    cancelled_at: str

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the cancellation."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "cancelled_at": self.cancelled_at,
        }
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        return data


@dataclass(frozen=True)
class ResumedJob:
    """The outcome of ``resume``: re-attachment to the durable record."""

    job_id: str
    run_id: str
    state: JobState
    pid: int | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the resumed job."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
        }
        if self.pid is not None:
            data["pid"] = self.pid
        if self.exit_code is not None:
            data["exit_code"] = self.exit_code
        return data


@dataclass(frozen=True)
class CollectedJob:
    """The outcome of ``collect``: the registered artifact registrations.

    ``artifacts`` are the frozen ``ArtifactManifest`` records registered
    through the real artifact registry (AC-03), in deterministic sorted
    artifact-id order; ``artifact_ids`` are their ids.
    """

    job_id: str
    run_id: str
    state: JobState
    collected_at: str
    artifact_ids: tuple[str, ...]
    artifacts: tuple[ArtifactManifest, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the collection (artifacts as manifest dicts)."""
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "collected_at": self.collected_at,
            "artifact_ids": list(self.artifact_ids),
            "artifacts": [manifest.to_dict() for manifest in self.artifacts],
        }


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class LocalComputeAdapter:
    """Local process execution adapter (15-ADAPTER-SPEC.md section 3).

    Args:
        state_dir: the injected state directory. Durable job records
            live at ``<state_dir>/jobs/<job_id>.json`` and collected
            artifact manifests at ``<state_dir>/manifests/``. A fresh
            adapter instance over the same state directory recovers the
            same jobs (AC-02).
        launcher: the process boundary (default
            :class:`SubprocessLauncher`); inject a scripted fake in
            tests. May be a ``str`` or ``Path``.
        now: injectable clock returning a timestamp string (default
            ``utc_now``); all recorded timestamps come from it -- no
            wall clock in the tested path.
    """

    def __init__(
        self,
        state_dir: str | Path,
        *,
        launcher: ProcessLauncher | None = None,
        now: Clock | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._launcher = launcher if launcher is not None else SubprocessLauncher()
        self._now_fn = now if now is not None else utc_now
        self._jobs_dir = self._state_dir / JOBS_STATE_DIR
        self._registry = ArtifactRegistry(self._state_dir / ARTIFACTS_STATE_DIR)

    # -- identity and persistence -----------------------------------------

    @property
    def state_dir(self) -> Path:
        """The injected state directory."""
        return self._state_dir

    @property
    def launcher(self) -> ProcessLauncher:
        """The injected process boundary (also usable by tests to drive
        real or scripted processes deterministically)."""
        return self._launcher

    def _job_id_for(self, run_context: RunContext) -> str:
        return generate_id("job", run_context.run_id)

    def _check_job_id(self, job_id: str) -> None:
        if not isinstance(job_id, str) or not is_valid_id(job_id, "job"):
            raise ComputeJobIdentityError(
                f"invalid job id {job_id!r}: expected a generated job id"
                " (sr_job_<32 hex chars>)"
            )

    def _validate_context(self, run_context: RunContext) -> None:
        if not isinstance(run_context, RunContext):
            raise TypeError(
                "run_context must be a RunContext, got"
                f" {type(run_context).__name__}"
            )

    def _job_path(self, job_id: str) -> Path:
        self._check_job_id(job_id)
        return self._jobs_dir / f"{job_id}.json"

    def _canonical(self, data: dict[str, Any]) -> str:
        """Deterministic JSON: same record always byte-identical."""
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)

    def _write_record(self, record: JobRecord) -> None:
        atomic_write(self._job_path(record.job_id), self._canonical(record.to_dict()))

    def _try_read(self, job_id: str) -> JobRecord | None:
        """Return the durable record, or None when absent."""
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        return self._read_record(job_id)

    def _read_record(self, job_id: str) -> JobRecord:
        """Re-hydrate the durable record from disk (the M1 recovery
        discipline: every operation re-reads, never trusts session state)."""
        path = self._job_path(job_id)
        if not path.is_file():
            if path.exists():
                raise ComputeJobRecordError(
                    f"job record at {path} is not a regular file"
                )
            raise ComputeJobNotFoundError(
                f"no durable job record for job {job_id!r} at {path}; call"
                " prepare(run_context) and submit(run_context) first"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComputeJobRecordError(
                f"corrupt job record at {path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ComputeJobRecordError(
                f"corrupt job record at {path}: expected a JSON object"
            )
        try:
            return JobRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise ComputeJobRecordError(
                f"corrupt job record at {path}: {exc}"
            ) from exc

    def read_job(self, job_id: str) -> JobRecord:
        """Return the durable record of a job (re-hydrated from disk).

        Raises:
            ComputeJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        return self._read_record(job_id)

    # -- state machine decisions ------------------------------------------

    def _transition(
        self, record: JobRecord, probe: ProcessProbe, *, now: str
    ) -> JobRecord:
        """Pure next-state decision from a process observation.

        ``probe.running`` leaves the record untouched. Exit status 0 is
        ``completed``; a non-zero status is ``failed`` with a stable,
        specific error; an unrecoverable exit status is ``completed``
        with ``EXIT_CODE_UNAVAILABLE_NOTE`` (collection then verifies
        the declared outputs independently).
        """
        if probe.running:
            return record
        if probe.exit_code == 0:
            return replace(
                record,
                state=JobState.COMPLETED,
                exit_code=0,
                completed_at=now,
            )
        if probe.exit_code is not None:
            return replace(
                record,
                state=JobState.FAILED,
                exit_code=probe.exit_code,
                completed_at=now,
                error=f"process exited with status {probe.exit_code}",
            )
        return replace(
            record,
            state=JobState.COMPLETED,
            completed_at=now,
            recovery_note=EXIT_CODE_UNAVAILABLE_NOTE,
        )

    def _probe_and_transition(self, record: JobRecord) -> JobRecord:
        """Attach to a running job's process, decide the next state and
        persist the decision record (status/resume share this path)."""
        if record.state is not JobState.RUNNING:
            raise ComputeJobStateError(
                f"job {record.job_id!r} is {record.state.value}; expected"
                " running"
            )
        if record.pid is None:
            raise ComputeJobRecordError(
                f"job record {record.job_id!r} is running but carries no"
                " pid; the record is inconsistent"
            )
        probe = self._launcher.attach(record.pid).poll()
        updated = self._transition(record, probe, now=self._now())
        if updated is not record:
            self._write_record(updated)
        return updated

    def _register_verified(
        self, manifest: ArtifactManifest, job_id: str
    ) -> ArtifactManifest:
        """Register one collected manifest, tolerating a prior identical
        registration (idempotent re-collection after a partial failure).

        Raises:
            ComputeCollectError: an artifact with the same deterministic
                id is already registered with different content.
        """
        try:
            self._registry.register(manifest)
        except ArtifactExistsError:
            existing = self._registry.get(manifest.artifact_id)
            if (existing.sha256, existing.size_bytes, existing.uri) != (
                manifest.sha256,
                manifest.size_bytes,
                manifest.uri,
            ):
                raise ComputeCollectError(
                    f"artifact {manifest.artifact_id!r} of job {job_id!r}"
                    " is already registered with different content"
                ) from None
            return existing
        return self._registry.get(manifest.artifact_id)

    # -- the ComputeAdapter interface (15-ADAPTER-SPEC.md section 3) ------

    def prepare(self, run_context: RunContext) -> PreparedJob:
        """Stage the run: create the durable record and the working
        directory, returning the prepared record (AC-01).

        Idempotent for an identical stage; re-staging the same run with
        different content is rejected (job identity is a pure function of
        the run id).

        Raises:
            TypeError: ``run_context`` is not a ``RunContext``.
            ComputeJobIdentityError: malformed run id or unsafe declared
                output name.
            ComputeJobStateError: the run's job already left the prepared
                state, or is prepared with different content.
        """
        self._validate_context(run_context)
        job_id = self._job_id_for(run_context)
        record = self._try_read(job_id)
        if record is None:
            record = JobRecord(
                job_id=job_id,
                run_id=run_context.run_id,
                state=JobState.PREPARED,
                command=run_context.command,
                working_directory=run_context.working_directory,
                outputs=run_context.outputs,
                created_at=self._now(),
            )
            self._write_record(record)
        elif record.state is JobState.PREPARED:
            staged = (
                record.command,
                record.working_directory,
                record.outputs,
            )
            incoming = (
                run_context.command,
                run_context.working_directory,
                run_context.outputs,
            )
            if staged != incoming:
                raise ComputeJobStateError(
                    f"job {job_id!r} is already prepared with a different"
                    " command/working directory/outputs; job identity is a"
                    " pure function of the run id, so a prepared job"
                    " cannot be restaged"
                )
        else:
            raise ComputeJobStateError(
                f"job {job_id!r} is already {record.state.value}; prepare"
                " is only valid before submission"
            )
        Path(record.working_directory).mkdir(parents=True, exist_ok=True)
        return PreparedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            created_at=record.created_at,
            working_directory=record.working_directory,
            command=record.command,
            outputs=record.outputs,
        )

    def submit(self, run_context: RunContext) -> SubmittedJob:
        """Launch the staged command and return the submit record with the
        persistent job id (AC-01/AC-02).

        The process is started through the injected launcher with the
        staged command and working directory; the durable record is
        transitioned to ``running`` (or directly to the terminal state
        when the process had already exited at the moment of
        observation). A second submit of the same job is rejected.

        Raises:
            TypeError: ``run_context`` is not a ``RunContext``.
            ComputeJobIdentityError: malformed run id.
            ComputeJobNotFoundError: the run was not prepared (call
                ``prepare`` first).
            ComputeJobStateError: the job is not in ``prepared`` state.
            ComputeJobLaunchError: the command could not be launched.
        """
        self._validate_context(run_context)
        job_id = self._job_id_for(run_context)
        record = self._read_record(job_id)
        if record.state is not JobState.PREPARED:
            raise ComputeJobStateError(
                f"job {job_id!r} is {record.state.value}; submit requires"
                " a prepared job (call prepare(run_context) first)"
            )
        try:
            handle = self._launcher.launch(
                record.command, Path(record.working_directory)
            )
        except (OSError, ValueError) as exc:
            raise ComputeJobLaunchError(
                f"failed to launch job {job_id!r}: {exc}"
            ) from exc
        submitted_at = self._now()
        running = replace(
            record,
            state=JobState.RUNNING,
            pid=handle.pid,
            submitted_at=submitted_at,
        )
        updated = self._transition(running, handle.poll(), now=submitted_at)
        self._write_record(updated)
        return SubmittedJob(
            job_id=updated.job_id,
            run_id=updated.run_id,
            state=updated.state,
            pid=updated.pid,
            submitted_at=submitted_at,
        )

    def status(self, job_id: str) -> JobStatus:
        """Report the job's state from the durable record + process state
        (AC-01).

        Terminal states are answered from the durable record alone (no
        process access); a ``running`` record probes the process through
        the launcher and persists the observed completion/failure
        decision.

        Raises:
            ComputeJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state is JobState.RUNNING:
            record = self._probe_and_transition(record)
        return JobStatus(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            pid=record.pid,
            exit_code=record.exit_code,
            error=record.error,
            collected_at=record.collected_at,
            recovery_note=record.recovery_note,
        )

    def collect(self, job_id: str) -> CollectedJob:
        """Gather the outputs of a completed job into artifact
        registrations (AC-01/AC-03).

        Every declared output of the durable record is registered as an
        ``ArtifactManifest`` through the **real** DEV-M3-G02
        ``ArtifactRegistry`` (``<state_dir>/manifests/``) with a
        deterministic artifact id
        (``generate_id("artifact", job_id, output_name)``), the file's
        real SHA-256 and byte size, the run id and the adapter's producer
        stamp. The durable record is stamped ``collected_at`` and names
        the registered ids. Re-collecting an already-collected job is
        idempotent: the same registrations are returned, nothing is
        rewritten.

        Raises:
            ComputeJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobStateError: the job is not ``completed``.
            ComputeCollectError: a declared output is missing or
                unreadable, or an already-registered artifact id carries
                different content.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state is not JobState.COMPLETED:
            raise ComputeJobStateError(
                f"job {job_id!r} is {record.state.value}; collect requires"
                " a completed job"
            )
        if record.collected_at is not None:
            manifests: tuple[ArtifactManifest, ...] = ()
            try:
                manifests = tuple(
                    self._registry.get(aid) for aid in record.artifact_ids
                )
            except ArtifactNotFoundError as exc:
                raise ComputeCollectError(
                    f"job {job_id!r} was collected but a registered artifact"
                    " is missing from the artifact registry"
                    f" ({ARTIFACTS_STATE_DIR}/); the state directory is"
                    f" inconsistent: {exc}"
                ) from exc
            return CollectedJob(
                job_id=record.job_id,
                run_id=record.run_id,
                state=record.state,
                collected_at=record.collected_at,
                artifact_ids=record.artifact_ids,
                artifacts=manifests,
            )
        manifests_list: list[ArtifactManifest] = []
        for name in sorted(record.outputs):
            path = Path(record.working_directory) / name
            if not path.is_file():
                raise ComputeCollectError(
                    f"job {job_id!r} completed but its declared output"
                    f" {name!r} is missing at {path}"
                )
            try:
                sha256 = compute_sha256(path)
            except ArtifactFileError as exc:
                raise ComputeCollectError(
                    f"cannot checksum declared output {name!r} of job"
                    f" {job_id!r}: {exc}"
                ) from exc
            artifact_id = generate_id("artifact", job_id, name)
            manifest = ArtifactManifest(
                artifact_id=artifact_id,
                uri=str(path),
                sha256=sha256,
                size_bytes=path.stat().st_size,
                created_at=self._now(),
                run_id=record.run_id,
                producer=_PRODUCER_STAMP,
                metadata={
                    "job_id": job_id,
                    "output_name": name,
                    "backend": BACKEND_NAME,
                },
            )
            manifests_list.append(self._register_verified(manifest, job_id))
        artifacts = tuple(sorted(manifests_list, key=lambda m: m.artifact_id))
        artifact_ids = tuple(m.artifact_id for m in artifacts)
        collected_at = self._now()
        updated = replace(
            record,
            collected_at=collected_at,
            artifact_ids=artifact_ids,
        )
        self._write_record(updated)
        return CollectedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            collected_at=collected_at,
            artifact_ids=artifact_ids,
            artifacts=artifacts,
        )

    def cancel(self, job_id: str) -> CancelledJob:
        """Cancel a prepared or running job (AC-01).

        A running job's process is terminated through the launcher and
        the observation (exit status, or a recovery note when the process
        had not exited at the moment of observation) is recorded with the
        cancellation decision. A prepared job is cancelled without any
        process access. Cancelling a terminal job is rejected.

        Raises:
            ComputeJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobStateError: the job is already terminal.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state in (
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        ):
            raise ComputeJobStateError(
                f"job {job_id!r} is already {record.state.value}; only"
                " prepared or running jobs can be cancelled"
            )
        cancelled_at = self._now()
        updated = replace(
            record, state=JobState.CANCELLED, cancelled_at=cancelled_at
        )
        if record.state is JobState.RUNNING:
            if record.pid is None:
                raise ComputeJobRecordError(
                    f"job record {record.job_id!r} is running but carries"
                    " no pid; the record is inconsistent"
                )
            handle = self._launcher.attach(record.pid)
            handle.terminate()
            probe = handle.poll()
            if not probe.running:
                updated = replace(updated, exit_code=probe.exit_code)
                if probe.exit_code is None:
                    updated = replace(
                        updated, recovery_note=EXIT_CODE_UNAVAILABLE_NOTE
                    )
            else:
                updated = replace(updated, recovery_note=TERMINATE_PENDING_NOTE)
        self._write_record(updated)
        return CancelledJob(
            job_id=updated.job_id,
            run_id=updated.run_id,
            state=JobState.CANCELLED,
            exit_code=updated.exit_code,
            cancelled_at=cancelled_at,
        )

    def resume(self, job_id: str) -> ResumedJob:
        """Re-attach to an existing durable job record (AC-01/AC-02).

        The record is re-hydrated from disk (the recovery discipline of
        M1); a ``running`` record's process is re-attached through the
        launcher by pid and the observed completion/failure decision is
        persisted. Prepared and terminal records are returned as they
        are -- resume never depends on the session object that submitted
        the job.

        Raises:
            ComputeJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state is JobState.RUNNING:
            record = self._probe_and_transition(record)
        return ResumedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            pid=record.pid,
            exit_code=record.exit_code,
        )

    def _now(self) -> str:
        return self._now_fn()
