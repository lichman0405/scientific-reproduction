"""SSH ComputeAdapter: remote execution and file transfer through an
injectable transport boundary, with transient-failure classification
(DEV-M7-G03, deliverable).

Implements the ComputeAdapter contract of the frozen specs over SSH:

* ``15-ADAPTER-SPEC.md`` section 3 (the same six operations as the local
  adapter of DEV-M7-G02, with ``ssh`` as a v0.1 reference backend);
* ``11-COMPUTATION-SUBSYSTEM.md`` section 3 (the adapter -- not the
  worker prompt -- owns connection/session details, working/scratch path
  conventions, job-ID persistence and result collection), section 5
  (allowed engineering retry: SSH transient failure, network timeout,
  verified redownload after transient transfer failure) and section 6
  (long-job behavior: the worker submits then exits; the durable record
  -- not the session object -- holds the job's identity).

Transport boundary (the paramiko decision)
------------------------------------------
Remote execution and file transfer happen through the injectable
:class:`SSHTransport` ABC -- a **pure abstraction, deliberately without
a hard paramiko dependency**: the v0.1 runtime is intentionally
stdlib-only (``pyproject.toml``: ``dependencies = []``), and a concrete
backend belongs to a later milestone (DEV-M7-G04 builds the
Slurm-over-SSH flow on top). A concrete backend (paramiko, socket, or an
ssh/exec CLI wrapper) must translate its connection, socket and timeout
errors into the :class:`SSHTransportError` hierarchy defined here; the
shipped module never imports paramiko. Tests inject a scripted transport
double (test doubles are the frozen objective), so the tested path is
pure, deterministic and network-free.

Transient failure classification (AC-01)
----------------------------------------
Connection-level failures (unreachable host, authentication failure,
connection dropped mid-transfer, timeout) raise the TRANSPORT failure
class :class:`SSHTransportError` -- with the stable subclasses
:class:`SSHConnectionError`, :class:`SSHTransferError` and
:class:`SSHTimeoutError` -- and are recorded on the durable job record
as ``failure_class="transport"`` with the stable error message. A
remotely-completed job with a non-zero remote exit code is the JOB
failure class: the record transitions to ``failed`` with
``failure_class="job"`` and the stable error ``remote command exited
with status <N>``. The classification is structural -- a transient
transport failure can never be confused with the job's own failure
outcome -- and the adapter's outcome records carry it (the
``failure_class`` field: ``"transport"`` | ``"job"`` | ``None``), so a
caller can distinguish "the cluster was unreachable" from "the
computation failed" from either the raised class or the records alone.

Credentials (AC-02)
-------------------
Credentials (password, key passphrase, private key reference) are
accepted at the adapter constructor boundary as
:class:`SSHCredentials` and live only in memory. They are never written
to any state directory, working directory or durable record: the
:class:`SSHJobRecord` schema carries no credential fields, and the
remote commands and paths built from the record never embed them.

Reconnect and bounded retry (AC-03)
-----------------------------------
Every transport operation runs under an injected, deterministic retry
policy (:class:`SSHRetryPolicy`: max attempts + a backoff callable; the
default backoff is exponential, tests inject a zero-delay recording
backoff so the tested path never sleeps). On a transient transport
failure the adapter disconnects, computes the backoff delay, sleeps it
and reconnects -- retry count exactly as configured -- then re-runs the
pending operation; when the attempts are exhausted the failure is
recorded as ``failure_class="transport"`` and re-raised. The reconnect
is observable through the transport double's call log. A mid-operation
drop on a *launch* is retried by resubmission -- the allowed engineering
retry of 11-COMPUTATION-SUBSYSTEM.md section 5 ("identical resubmission
after node/system failure") -- so exactly-once remote launch is not
guaranteed across a drop (documented v0.1 limitation, resolved in the
scheduler-backed DEV-M7-G04 flow).

Determinism and injectable surfaces
-----------------------------------
Everything a session can vary is injected: the transport, the retry
policy, the clock (a ``now`` callable producing timestamp strings -- no
wall clock in the tested path) and the state directory. Identical
injected inputs produce byte-identical durable records (sorted canonical
JSON); failure states are decision records with stable, specific error
strings. No randomness, no wall-clock dependence, no network and no
hidden filesystem access anywhere in the tested path. Error discipline
follows the house paradigm: ``TypeError`` at public boundaries for wrong
types, a ``ValueError``-subclass error hierarchy with stable messages
otherwise.
"""

from __future__ import annotations

import json
import shlex
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, TypeVar

from scientific_reproduction.adapters.compute.local import (
    ARTIFACTS_STATE_DIR,
    JOBS_STATE_DIR,
    TERMINATE_PENDING_NOTE,
    Clock,
    ComputeAdapterError,
    ComputeCollectError,
    ComputeJobNotFoundError,
    ComputeJobRecordError,
    ComputeJobStateError,
    JobState,
    ProcessProbe,
    RunContext,
    utc_now,
)
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
    "FAILURE_CLASS_JOB",
    "FAILURE_CLASS_TRANSPORT",
    "SSH_BACKEND_NAME",
    "SSH_EXIT_CODE_UNAVAILABLE_NOTE",
    "SSH_JOB_RECORD_VERSION",
    "SSH_STAGING_DIR",
    "SSHAdapterError",
    "SSHCancelledJob",
    "SSHCollectedJob",
    "SSHComputeAdapter",
    "SSHConnectionError",
    "SSHCredentials",
    "SSHJobIdentityError",
    "SSHJobLaunchError",
    "SSHJobRecord",
    "SSHJobStatus",
    "SSHPreparedJob",
    "SSHRemoteError",
    "SSHRemoteFileNotFoundError",
    "SSHResumedJob",
    "SSHRetryPolicy",
    "SSHSubmittedJob",
    "SSHTimeoutError",
    "SSHTransferError",
    "SSHTransport",
    "SSHTransportError",
    "RemoteCommand",
    "RemotePath",
    "RemoteResult",
    "default_backoff",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The backend name stamped into every durable job record (the v0.1 ssh
#: reference backend of 15-ADAPTER-SPEC.md section 3).
SSH_BACKEND_NAME: str = "ssh"

#: Adapter identity (mirrors the ``adapter:<id>@v<version>`` producer
#: stamping of the local adapter and the research adapters).
ADAPTER_ID: str = "compute/ssh"

#: Adapter contract version. Bumped whenever a contract rule changes; the
#: same version always accepts the same run contexts and yields the same
#: records.
ADAPTER_VERSION: str = "1.0"

#: Version of the durable ssh job-record schema (``record_version`` key
#: of :class:`SSHJobRecord`); records of a different version are refused.
SSH_JOB_RECORD_VERSION: str = "1.0"

#: The ``failure_class`` value of a TRANSPORT failure (connection-level:
#: unreachable host, authentication failure, connection dropped
#: mid-transfer, timeout) -- structurally distinct from any job failure
#: (AC-01).
FAILURE_CLASS_TRANSPORT: str = "transport"

#: The ``failure_class`` value of the job's own failure (non-zero remote
#: exit code, remote launch refusal, missing declared output).
FAILURE_CLASS_JOB: str = "job"

#: Collect staging directory of a compute state directory, relative to
#: the injected state directory: pulled outputs are staged at
#: ``<state_dir>/staging/<job_id>/<output_name>`` before checksumming and
#: artifact registration.
SSH_STAGING_DIR: str = "staging"

#: Stable recovery note written when a detached remote process exited
#: without a recoverable exit status (the status file was never written,
#: e.g. the wrapper process itself was killed). The job is recorded as
#: completed because ``collect`` independently verifies every declared
#: output.
SSH_EXIT_CODE_UNAVAILABLE_NOTE: str = (
    "remote process exited while the adapter was detached; exit code"
    " unavailable (job recorded as completed; collect verifies declared"
    " outputs)"
)

#: Producer stamp written into collected artifact manifests.
_PRODUCER_STAMP: str = f"adapter:{ADAPTER_ID}@v{ADAPTER_VERSION}"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class SSHAdapterError(ComputeAdapterError):
    """Base error of the ssh compute adapter subsystem."""


class SSHJobIdentityError(SSHAdapterError):
    """Raised for malformed job/run ids, unsafe declared output names and
    unsafe remote paths or path segments (the FND-M9-G02-01 discipline:
    no path separators, no glob metacharacters, no whitespace in
    id-bearing fields; remote paths validated before any command
    construction)."""


class SSHJobLaunchError(SSHAdapterError):
    """Raised when the remote launch did not produce a usable launch.

    A clean remote-side answer (mkdir/cd refusal, an unparseable remote
    pid) is a job-level fact, never a connection failure: it does not
    subclass :class:`SSHTransportError` and is never retried.
    """


class SSHRemoteError(SSHAdapterError):
    """Base of clean remote-side answers (the remote answered, and the
    answer is a fact about the remote state, not about the connection).

    Structurally distinct from :class:`SSHTransportError`: a clean
    remote answer is never retried and never classified as a transport
    failure.
    """


class SSHRemoteFileNotFoundError(SSHRemoteError):
    """The remote file is definitively absent (a clean remote answer)."""


class SSHTransportError(SSHAdapterError):
    """TRANSPORT failure base class (AC-01).

    Connection-level failures -- unreachable host, authentication
    failure, connection dropped mid-transfer, timeout -- are the
    ``SSHTransportError`` subclasses; they are raised by the transport
    boundary and retried by the adapter's reconnect loop, and they are
    structurally distinct from any scientific/job failure outcome.
    """


class SSHConnectionError(SSHTransportError):
    """The remote host is unreachable or refused the connection, or
    authentication failed."""


class SSHTransferError(SSHTransportError):
    """The connection dropped mid-operation (execution or file
    transfer)."""


class SSHTimeoutError(SSHTransportError):
    """A transport operation timed out."""


# ---------------------------------------------------------------------------
# Credentials (AC-02: constructor boundary, in memory only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SSHCredentials:
    """Remote connection identity and secrets (AC-02).

    Accepted at the adapter constructor boundary and held **only in
    memory**: nothing here is ever written to a state directory, a
    working directory or a durable record, and the class deliberately
    offers no serialization (no ``to_dict``/``from_dict``/``persist``).

    Args:
        host: remote host name or address (no whitespace).
        port: ssh port (default 22).
        username: remote login name, or None for the default.
        password: password, or None when key-based login is used.
        private_key_path: path of the private key file, or None.
        key_passphrase: passphrase of the private key, or None.

    Raises:
        TypeError: a field has the wrong type.
        ValueError: ``host`` is empty, or ``port`` is out of the valid
            range (1..65535).
    """

    host: str
    port: int = 22
    username: str | None = None
    password: str | None = None
    private_key_path: str | None = None
    key_passphrase: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str):
            raise TypeError(
                "SSHCredentials.host must be a string, got"
                f" {type(self.host).__name__}"
            )
        if not self.host or any(char.isspace() for char in self.host):
            raise ValueError(
                f"SSHCredentials.host must be a non-empty string without"
                f" whitespace, got {self.host!r}"
            )
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise TypeError(
                "SSHCredentials.port must be an int, got"
                f" {type(self.port).__name__}"
            )
        if not 1 <= self.port <= 65535:
            raise ValueError(
                f"SSHCredentials.port must be in 1..65535, got {self.port}"
            )
        for name in ("username", "password", "private_key_path", "key_passphrase"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(
                    f"SSHCredentials.{name} must be a string or None, got"
                    f" {type(value).__name__}"
                )


# ---------------------------------------------------------------------------
# Remote path and command abstraction (deliverable)
# ---------------------------------------------------------------------------


def _is_safe_segment(value: str) -> bool:
    """True iff ``value`` is a safe remote path segment.

    The FND-M9-G02-01 discipline applied to id-bearing remote path
    segments (declared output names embedded in remote file names, the
    adapter's own ``.sr_<job_id>_*`` files): no path separators, no
    ``.``/``..``, no NUL, no glob metacharacters, no whitespace.
    """
    return (
        value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and not any(char in value for char in "*?[]")
        and not any(char.isspace() for char in value)
        and Path(value).name == value
    )


@dataclass(frozen=True)
class RemotePath:
    """A validated remote (POSIX) path.

    Remote paths must be absolute, must not carry NUL or control
    characters, and every segment joined through :meth:`join` must be a
    safe id-bearing segment (:func:`_is_safe_segment`) -- remote paths
    are validated before any command construction, so a shell-quoted
    ``RemotePath`` can never smuggle shell syntax into a remote command.

    Raises:
        TypeError: ``value`` is not a string.
        SSHJobIdentityError: the path is not absolute or carries NUL or
            control characters.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(
                "RemotePath.value must be a string, got"
                f" {type(self.value).__name__}"
            )
        if not self.value.startswith("/"):
            raise SSHJobIdentityError(
                f"remote path {self.value!r} must be an absolute POSIX"
                " path (must start with '/')"
            )
        if "\x00" in self.value or any(
            char in self.value for char in ("\x01", "\x02", "\x03", "\x04")
        ):
            raise SSHJobIdentityError(
                f"remote path {self.value!r} must not contain NUL or"
                " control characters"
            )

    @classmethod
    def segment_is_safe(cls, segment: str) -> bool:
        """True iff ``segment`` is a safe id-bearing remote path segment
        (the FND-M9-G02-01 discipline)."""
        return _is_safe_segment(segment)

    def join(self, *segments: str) -> RemotePath:
        """Return ``self/<segments...>`` with every segment validated as a
        safe id-bearing segment.

        Raises:
            SSHJobIdentityError: a segment is empty, ``.``/``..``, or
                carries separators, glob metacharacters or whitespace.
        """
        result = self.value
        for segment in segments:
            if not _is_safe_segment(segment):
                raise SSHJobIdentityError(
                    f"remote path segment {segment!r} is not safe (no '/',"
                    " no '\\', not '.' or '..', no glob metacharacters, no"
                    " whitespace)"
                )
            result = f"{result.rstrip('/')}/{segment}"
        return RemotePath(result)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class RemoteCommand:
    """A remote command as argv; :meth:`to_shell` renders it as a
    POSIX shell line with every argument shell-quoted (``shlex.join``),
    so validated entries can never break out of the remote shell.

    Raises:
        TypeError: ``argv`` is not a non-empty tuple of non-empty
            strings.
        ValueError: an entry contains a NUL byte (unsafe for a shell
            line on every platform).
    """

    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise TypeError(
                "RemoteCommand.argv must be a non-empty tuple of"
                " command-line strings"
            )
        for entry in self.argv:
            if not isinstance(entry, str) or not entry:
                raise TypeError(
                    "RemoteCommand.argv entries must be non-empty"
                    f" strings, got {entry!r}"
                )
            if "\x00" in entry:
                raise ValueError(
                    "RemoteCommand.argv entries must not contain NUL"
                    " characters"
                )

    def to_shell(self) -> str:
        """The shell line executed remotely (each entry shell-quoted)."""
        return shlex.join(self.argv)


@dataclass(frozen=True)
class RemoteResult:
    """The outcome of one remote command execution.

    Raises:
        TypeError: ``exit_code`` is not an int, or ``stdout``/``stderr``
            are not strings.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.exit_code, bool) or not isinstance(
            self.exit_code, int
        ):
            raise TypeError(
                "RemoteResult.exit_code must be an int, got"
                f" {type(self.exit_code).__name__}"
            )
        if not isinstance(self.stdout, str):
            raise TypeError(
                "RemoteResult.stdout must be a string, got"
                f" {type(self.stdout).__name__}"
            )
        if not isinstance(self.stderr, str):
            raise TypeError(
                "RemoteResult.stderr must be a string, got"
                f" {type(self.stderr).__name__}"
            )


# ---------------------------------------------------------------------------
# The injectable transport boundary (pure abstraction, no paramiko)
# ---------------------------------------------------------------------------


class SSHTransport(ABC):
    """Remote execution and file-transfer boundary (injectable).

    The adapter performs **all** remote work through this interface; a
    concrete backend (paramiko/socket/ssh CLI) must translate its
    connection, socket and timeout errors into the
    :class:`SSHTransportError` subclasses -- unreachable host and
    authentication failure as :class:`SSHConnectionError`, a connection
    dropped mid-operation as :class:`SSHTransferError`, a timeout as
    :class:`SSHTimeoutError`. A clean remote answer that a file does not
    exist is :class:`SSHRemoteFileNotFoundError` (not a transport
    error: it is never retried). Implementations are deterministic given
    their inputs; tests inject a scripted double instead of touching a
    real network.

    ``connect`` establishes a session; ``disconnect`` closes it (closing
    an already-closed session is a no-op); ``run_command`` executes a
    shell line; ``push_file``/``pull_file`` transfer one file.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish the remote session.

        Raises:
            SSHConnectionError: the host is unreachable, refused the
                connection, or authentication failed.
            SSHTimeoutError: the connection attempt timed out.
        """

    @abstractmethod
    def disconnect(self) -> None:
        """Close the remote session. Closing an already-closed session is
        a no-op."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True iff a session is currently established."""

    @abstractmethod
    def run_command(self, command: RemoteCommand) -> RemoteResult:
        """Execute ``command``'s shell line remotely and return its result.

        Raises:
            SSHConnectionError: no session is established.
            SSHTransferError: the connection dropped mid-execution.
            SSHTimeoutError: the execution timed out.
        """

    @abstractmethod
    def push_file(self, local_path: Path, remote_path: RemotePath) -> None:
        """Copy the local file ``local_path`` to ``remote_path``.

        Raises:
            SSHConnectionError: no session is established.
            SSHTransferError: the connection dropped mid-transfer.
            SSHTimeoutError: the transfer timed out.
        """

    @abstractmethod
    def pull_file(self, remote_path: RemotePath, local_path: Path) -> None:
        """Copy the remote file ``remote_path`` to ``local_path``.

        Raises:
            SSHConnectionError: no session is established.
            SSHRemoteFileNotFoundError: the remote file is definitively
                absent (a clean remote answer, never retried).
            SSHTransferError: the connection dropped mid-transfer.
            SSHTimeoutError: the transfer timed out.
        """


# ---------------------------------------------------------------------------
# The retry policy (AC-03: bounded, deterministic, injected)
# ---------------------------------------------------------------------------


def default_backoff(attempt: int) -> float:
    """The default exponential backoff delay in seconds for ``attempt``
    (1-based), capped at 30 seconds."""
    return min(30.0, 0.5 * (2 ** (attempt - 1)))


@dataclass(frozen=True)
class SSHRetryPolicy:
    """Bounded retry policy of transport operations (AC-03).

    ``max_attempts`` bounds how many times a pending transport operation
    is attempted (the first attempt counts; ``max_attempts`` reconnect
    attempts happen before a permanent failure is recorded and raised).
    ``backoff`` is the delay between attempts; tests inject a recording
    zero-delay backoff so the tested path never sleeps.

    Raises:
        TypeError: ``max_attempts`` is not an int, or ``backoff`` is not
            callable.
        ValueError: ``max_attempts`` is < 1.
    """

    max_attempts: int = 3
    backoff: Callable[[int], float] = default_backoff

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            raise TypeError(
                "SSHRetryPolicy.max_attempts must be an int, got"
                f" {type(self.max_attempts).__name__}"
            )
        if self.max_attempts < 1:
            raise ValueError(
                "SSHRetryPolicy.max_attempts must be >= 1, got"
                f" {self.max_attempts}"
            )
        if not callable(self.backoff):
            raise TypeError(
                "SSHRetryPolicy.backoff must be callable, got"
                f" {type(self.backoff).__name__}"
            )


# ---------------------------------------------------------------------------
# The durable job record (persisted identity, ssh backend)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SSHJobRecord:
    """The durable, session-independent record of one remote job.

    Persisted at ``<state_dir>/jobs/<job_id>.json`` and re-hydrated from
    disk on every operation (the M1 recovery discipline): a fresh adapter
    instance over the same state directory recovers the job from this
    record alone. The record carries **no credential fields** (AC-02):
    credentials live only on the adapter's constructor boundary. The
    ``failure_class`` field carries the AC-01 classification -- ``None``
    (healthy), ``"transport"`` (a permanent connection-level failure was
    observed) or ``"job"`` (the remote computation itself failed).

    Field names are the exact JSON keys of the persisted record
    (``to_dict`` / ``from_dict`` round-trip them). The record is a
    runtime record -- there is no ``schemas/job.schema.yaml`` -- so
    ``from_dict`` validates against this documented contract instead
    (like ``core.leases``), with stable errors.
    """

    record_version: ClassVar[str] = SSH_JOB_RECORD_VERSION

    job_id: str
    run_id: str
    state: JobState
    command: tuple[str, ...]
    working_directory: str
    outputs: tuple[str, ...]
    created_at: str
    submitted_at: str | None = None
    remote_pid: int | None = None
    exit_code: int | None = None
    failure_class: str | None = None
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
            "backend": SSH_BACKEND_NAME,
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
            "remote_pid",
            "exit_code",
            "failure_class",
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
    def from_dict(cls, data: Mapping[str, Any]) -> SSHJobRecord:
        """Build a record from a plain dict (the ssh job-record contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            ComputeJobRecordError: a required field is missing or a value
                violates the contract (unknown version/backend/state,
                malformed ids, unsafe paths or output names, unknown
                ``failure_class``, mistyped fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "SSHJobRecord.from_dict expects a mapping, got"
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
        if backend != SSH_BACKEND_NAME:
            raise ComputeJobRecordError(
                f"job record backend {backend!r} is not"
                f" {SSH_BACKEND_NAME!r}"
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
        try:
            RemotePath(working_directory)
        except (TypeError, ValueError) as exc:
            raise ComputeJobRecordError(
                f"job record working_directory {working_directory!r} is"
                f" not a valid remote path: {exc}"
            ) from exc
        outputs = _require_str_tuple(data, "outputs", required=True)
        for name in outputs:
            if not _is_safe_segment(name):
                raise ComputeJobRecordError(
                    f"job record output {name!r} is not a safe remote path"
                    " segment (no '/', no '\\', not '.' or '..', no glob"
                    " metacharacters, no whitespace)"
                )
        created_at = _require_nonempty_str(required("created_at"), "created_at")
        submitted_at = _optional_str(data, "submitted_at")
        remote_pid = _optional_int(data, "remote_pid")
        if remote_pid is not None and remote_pid <= 0:
            raise ComputeJobRecordError(
                f"job record remote_pid must be a positive int, got"
                f" {remote_pid}"
            )
        exit_code = _optional_int(data, "exit_code")
        failure_class = _optional_str(data, "failure_class")
        if failure_class not in (None, FAILURE_CLASS_TRANSPORT, FAILURE_CLASS_JOB):
            raise ComputeJobRecordError(
                f"job record failure_class {failure_class!r} is not one of"
                f" {None!r}, {FAILURE_CLASS_TRANSPORT!r},"
                f" {FAILURE_CLASS_JOB!r}"
            )
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
            remote_pid=remote_pid,
            exit_code=exit_code,
            failure_class=failure_class,
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
# The remote launch wrapper (remote path/command abstraction in action)
# ---------------------------------------------------------------------------


def _single_quote(value: str) -> str:
    """POSIX single-quote a shell fragment (``'`` -> ``'\\''``)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _build_launch_command(
    job_id: str,
    working_directory: RemotePath,
    command: tuple[str, ...],
) -> RemoteCommand:
    """The remote launch wrapper of one job (pure, deterministic).

    Runs ``mkdir -p`` on the working directory, then backgrounds the
    shell-quoted command via ``nohup sh -c '<command>; echo $? >
    <statusfile>'`` with stdout/stderr redirected to the job log, and
    echoes the remote pid. Every interpolated value is validated before
    command construction: the job id is a generated id, the working
    directory is a validated :class:`RemotePath`, and the log/status
    file names are safe id-bearing segments joined onto it. The returned
    command is executed by the remote default shell (``sh -c``).

    Raises:
        SSHJobIdentityError: ``job_id`` is not a generated job id.
    """
    if not isinstance(job_id, str) or not is_valid_id(job_id, "job"):
        raise SSHJobIdentityError(
            f"job id {job_id!r} is not a generated job id"
            " (sr_job_<32 hex chars>)"
        )
    log_path = working_directory.join(f".sr_{job_id}_job.log")
    status_path = working_directory.join(f".sr_{job_id}_job.status")
    inner = f"{shlex.join(command)}; echo $? > {shlex.quote(status_path.value)}"
    line = (
        f"mkdir -p -- {shlex.quote(working_directory.value)}"
        f" && cd -- {shlex.quote(working_directory.value)}"
        f" && (nohup sh -c {_single_quote(inner)}"
        f" > {shlex.quote(log_path.value)} 2>&1 & echo $!)"
    )
    return RemoteCommand(("sh", "-c", line))


# ---------------------------------------------------------------------------
# Operation result records (frozen decision records with classification)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SSHPreparedJob:
    """The outcome of ``prepare``: the staged durable record."""

    job_id: str
    run_id: str
    state: JobState
    created_at: str
    working_directory: str
    command: tuple[str, ...]
    outputs: tuple[str, ...]
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the prepared job."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "working_directory": self.working_directory,
            "command": list(self.command),
            "outputs": list(self.outputs),
        }
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class
        return data


@dataclass(frozen=True)
class SSHSubmittedJob:
    """The outcome of ``submit``: the launched remote job, its remote pid
    and the AC-01 classification."""

    job_id: str
    run_id: str
    state: JobState
    remote_pid: int | None
    submitted_at: str
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the submitted job."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "remote_pid": self.remote_pid,
            "submitted_at": self.submitted_at,
        }
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class
        return data


@dataclass(frozen=True)
class SSHJobStatus:
    """The outcome of ``status``: durable record + remote observation,
    carrying the AC-01 ``failure_class``."""

    job_id: str
    run_id: str
    state: JobState
    remote_pid: int | None = None
    exit_code: int | None = None
    error: str | None = None
    failure_class: str | None = None
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
            "remote_pid",
            "exit_code",
            "error",
            "failure_class",
            "collected_at",
            "recovery_note",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class SSHCancelledJob:
    """The outcome of ``cancel``: the cancellation decision record."""

    job_id: str
    run_id: str
    state: JobState
    remote_pid: int | None = None
    exit_code: int | None = None
    cancelled_at: str | None = None
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the cancellation."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
        }
        for key in ("remote_pid", "exit_code", "cancelled_at", "failure_class"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class SSHResumedJob:
    """The outcome of ``resume``: re-attachment to the durable record."""

    job_id: str
    run_id: str
    state: JobState
    remote_pid: int | None = None
    exit_code: int | None = None
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the resumed job."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
        }
        for key in ("remote_pid", "exit_code", "failure_class"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class SSHCollectedJob:
    """The outcome of ``collect``: the registered artifact registrations.

    ``artifacts`` are the frozen ``ArtifactManifest`` records registered
    through the real artifact registry, in deterministic sorted
    artifact-id order; ``artifact_ids`` are their ids.
    """

    job_id: str
    run_id: str
    state: JobState
    collected_at: str
    artifact_ids: tuple[str, ...]
    artifacts: tuple[ArtifactManifest, ...]
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the collection (artifacts as manifest dicts)."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "collected_at": self.collected_at,
            "artifact_ids": list(self.artifact_ids),
            "artifacts": [manifest.to_dict() for manifest in self.artifacts],
        }
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class
        return data


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


class SSHComputeAdapter:
    """SSH remote execution adapter (15-ADAPTER-SPEC.md section 3).

    Args:
        credentials: the remote connection identity and secrets,
            accepted at the constructor boundary and held in memory only
            (AC-02) -- never persisted to any state directory, working
            directory or durable record.
        state_dir: the injected state directory. Durable job records
            live at ``<state_dir>/jobs/<job_id>.json``, collected
            artifact manifests at ``<state_dir>/manifests/`` and pulled
            outputs are staged at ``<state_dir>/staging/<job_id>/``. A
            fresh adapter instance over the same state directory recovers
            the same jobs from the records alone.
        transport: the injectable remote boundary (required -- the pure
            abstraction of this milestone has no shipped real backend;
            a paramiko/socket wrapper would translate its errors into
            :class:`SSHTransportError` subclasses). Tests inject a
            scripted double.
        retry_policy: the bounded reconnect/retry policy (AC-03);
            defaults to :class:`SSHRetryPolicy` (3 attempts, exponential
            backoff).
        now: injectable clock returning a timestamp string (default
            ``utc_now``); all recorded timestamps come from it -- no wall
            clock in the tested path.

    Raises:
        TypeError: ``credentials`` is not an ``SSHCredentials``, or
            ``transport`` is not an ``SSHTransport``.
    """

    def __init__(
        self,
        credentials: SSHCredentials,
        state_dir: str | Path,
        *,
        transport: SSHTransport,
        retry_policy: SSHRetryPolicy | None = None,
        now: Clock | None = None,
    ) -> None:
        if not isinstance(credentials, SSHCredentials):
            raise TypeError(
                "credentials must be an SSHCredentials, got"
                f" {type(credentials).__name__}"
            )
        if not isinstance(transport, SSHTransport):
            raise TypeError(
                "transport must be an SSHTransport, got"
                f" {type(transport).__name__}"
            )
        self._credentials = credentials
        self._state_dir = Path(state_dir)
        self._transport = transport
        self._retry_policy = (
            retry_policy if retry_policy is not None else SSHRetryPolicy()
        )
        self._now_fn = now if now is not None else utc_now
        self._jobs_dir = self._state_dir / JOBS_STATE_DIR
        self._staging_dir = self._state_dir / SSH_STAGING_DIR
        self._registry = ArtifactRegistry(self._state_dir / ARTIFACTS_STATE_DIR)

    # -- identity, persistence and injectable surfaces ---------------------

    @property
    def credentials(self) -> SSHCredentials:
        """The injected credentials (in memory only; AC-02)."""
        return self._credentials

    @property
    def state_dir(self) -> Path:
        """The injected state directory."""
        return self._state_dir

    @property
    def transport(self) -> SSHTransport:
        """The injected remote boundary (also usable by tests to drive
        scripted transports deterministically)."""
        return self._transport

    @property
    def retry_policy(self) -> SSHRetryPolicy:
        """The injected reconnect/retry policy (AC-03)."""
        return self._retry_policy

    def _job_id_for(self, run_context: RunContext) -> str:
        return generate_id("job", run_context.run_id)

    def _check_job_id(self, job_id: str) -> None:
        if not isinstance(job_id, str) or not is_valid_id(job_id, "job"):
            raise SSHJobIdentityError(
                f"invalid job id {job_id!r}: expected a generated job id"
                " (sr_job_<32 hex chars>)"
            )

    def _validate_context(self, run_context: RunContext) -> None:
        if not isinstance(run_context, RunContext):
            raise TypeError(
                "run_context must be a RunContext, got"
                f" {type(run_context).__name__}"
            )
        # Remote paths validated before any command construction.
        RemotePath(run_context.working_directory)
        for name in run_context.outputs:
            if not _is_safe_segment(name):
                raise SSHJobIdentityError(
                    f"declared output {name!r} is not a safe remote path"
                    " segment (no '/', no '\\', not '.' or '..', no glob"
                    " metacharacters, no whitespace)"
                )

    def _job_path(self, job_id: str) -> Path:
        self._check_job_id(job_id)
        return self._jobs_dir / f"{job_id}.json"

    def _canonical(self, data: dict[str, Any]) -> str:
        """Deterministic JSON: same record always byte-identical."""
        return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)

    def _write_record(self, record: SSHJobRecord) -> None:
        atomic_write(
            self._job_path(record.job_id), self._canonical(record.to_dict())
        )

    def _try_read(self, job_id: str) -> SSHJobRecord | None:
        """Return the durable record, or None when absent."""
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        return self._read_record(job_id)

    def _read_record(self, job_id: str) -> SSHJobRecord:
        """Re-hydrate the durable record from disk (the M1 recovery
        discipline: every operation re-reads, never trusts session
        state)."""
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
            return SSHJobRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise ComputeJobRecordError(
                f"corrupt job record at {path}: {exc}"
            ) from exc

    def read_job(self, job_id: str) -> SSHJobRecord:
        """Return the durable record of a job (re-hydrated from disk).

        Raises:
            SSHJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        return self._read_record(job_id)

    # -- transport lifecycle and reconnect (AC-03) -------------------------

    def _open(self) -> None:
        self._transport.connect()

    def _close_quietly(self) -> None:
        """Close the transport session; a failing close (or an
        already-closed session) never masks the operation's outcome."""
        try:
            self._transport.disconnect()
        except SSHTransportError:
            pass

    def _execute(self, operation: Callable[[], _T]) -> _T:
        """Run one transport operation under the injected retry policy.

        The session is opened before the operation and closed afterwards
        (every transport operation is its own session). On a transient
        :class:`SSHTransportError` the session is closed, the backoff
        delay is computed and slept, and the next attempt reconnects and
        re-runs the pending operation -- at most ``max_attempts`` times
        (AC-03). When the attempts are exhausted the last error is
        re-raised; a clean remote answer (:class:`SSHRemoteError`) is
        never retried. Non-transport errors propagate with the session
        closed.
        """
        for attempt in range(1, self._retry_policy.max_attempts + 1):
            try:
                self._open()
                result = operation()
            except SSHTransportError:
                if attempt >= self._retry_policy.max_attempts:
                    raise
                time.sleep(self._retry_policy.backoff(attempt))
                continue
            finally:
                self._close_quietly()
            return result
        raise AssertionError(
            "unreachable: SSHRetryPolicy.max_attempts is >= 1 by contract"
        )

    def _classify_transport_failure(
        self, record: SSHJobRecord, exc: SSHTransportError
    ) -> None:
        """Persist a permanent transport failure on the durable record
        (AC-01: the outcome records carry the classification) and let
        ``exc`` propagate."""
        self._write_record(
            replace(
                record,
                failure_class=FAILURE_CLASS_TRANSPORT,
                error=str(exc),
            )
        )

    # -- remote observation and state machine decisions --------------------

    def _probe_remote(self, record: SSHJobRecord) -> ProcessProbe:
        """Probe the remote process of a running job (liveness, then the
        exit-status file once it is dead). Every probe runs under the
        retry policy; a permanent transport failure propagates as
        :class:`SSHTransportError` (the caller records the
        classification)."""
        if record.remote_pid is None:
            raise ComputeJobRecordError(
                f"job record {record.job_id!r} is running but carries no"
                " remote_pid; the record is inconsistent"
            )
        liveness = self._execute(
            lambda: self._transport.run_command(
                RemoteCommand(("kill", "-0", str(record.remote_pid)))
            )
        )
        if liveness.exit_code == 0:
            return ProcessProbe(running=True)
        status_path = RemotePath(record.working_directory).join(
            f".sr_{record.job_id}_job.status"
        )
        status = self._execute(
            lambda: self._transport.run_command(
                RemoteCommand(("cat", str(status_path)))
            )
        )
        if status.exit_code == 0:
            text = status.stdout.strip()
            if text.isdigit():
                return ProcessProbe(running=False, exit_code=int(text))
        return ProcessProbe(running=False, exit_code=None)

    def _transition(
        self, record: SSHJobRecord, probe: ProcessProbe, *, now: str
    ) -> SSHJobRecord:
        """Pure next-state decision from a remote observation.

        ``probe.running`` leaves the record untouched. Exit status 0 is
        ``completed`` (healthy: any stale failure classification is
        cleared); a non-zero status is ``failed`` with
        ``failure_class="job"`` and the stable error ``remote command
        exited with status <N>`` (AC-01); an unrecoverable exit status
        is ``completed`` with ``SSH_EXIT_CODE_UNAVAILABLE_NOTE``
        (collection then verifies the declared outputs independently).
        """
        if probe.running:
            return record
        if probe.exit_code == 0:
            return replace(
                record,
                state=JobState.COMPLETED,
                exit_code=0,
                completed_at=now,
                failure_class=None,
                error=None,
            )
        if probe.exit_code is not None:
            return replace(
                record,
                state=JobState.FAILED,
                exit_code=probe.exit_code,
                completed_at=now,
                failure_class=FAILURE_CLASS_JOB,
                error=f"remote command exited with status {probe.exit_code}",
            )
        return replace(
            record,
            state=JobState.COMPLETED,
            completed_at=now,
            recovery_note=SSH_EXIT_CODE_UNAVAILABLE_NOTE,
            failure_class=None,
            error=None,
        )

    def _probe_and_transition(self, record: SSHJobRecord) -> SSHJobRecord:
        """Probe a running job remotely, decide the next state and persist
        the decision record (status/resume share this path).

        A healthy probe of a still-running job clears a stale transport
        classification (the ``failure_class`` of the record is the
        *current* classification, never history).
        """
        if record.state is not JobState.RUNNING:
            raise ComputeJobStateError(
                f"job {record.job_id!r} is {record.state.value}; expected"
                " running"
            )
        probe = self._probe_remote(record)
        updated = self._transition(record, probe, now=self._now())
        if updated is not record:
            self._write_record(updated)
        elif record.failure_class is not None:
            updated = replace(record, failure_class=None, error=None)
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

    def _now(self) -> str:
        return self._now_fn()

    # -- the ComputeAdapter interface (15-ADAPTER-SPEC.md section 3) -------

    def prepare(self, run_context: RunContext) -> SSHPreparedJob:
        """Stage the run: create the durable record (no remote contact
        yet -- the remote working directory is prepared by ``submit``).

        Idempotent for an identical stage; re-staging the same run with
        different content is rejected (job identity is a pure function of
        the run id). The remote working directory must be an absolute
        POSIX path and every declared output a safe id-bearing remote
        path segment (the FND-M9-G02-01 discipline, validated before any
        command construction).

        Raises:
            TypeError: ``run_context`` is not a ``RunContext``.
            SSHJobIdentityError: unsafe remote working directory or
                unsafe declared output name.
            ComputeJobStateError: the run's job already left the prepared
                state, or is prepared with different content.
        """
        self._validate_context(run_context)
        job_id = self._job_id_for(run_context)
        record = self._try_read(job_id)
        if record is None:
            record = SSHJobRecord(
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
        return SSHPreparedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            created_at=record.created_at,
            working_directory=record.working_directory,
            command=record.command,
            outputs=record.outputs,
            failure_class=record.failure_class,
        )

    def submit(self, run_context: RunContext) -> SSHSubmittedJob:
        """Launch the staged command remotely and return the submit record
        with the persistent job id and the remote pid (AC-01/AC-02/AC-03).

        The remote launch runs under the retry policy (a mid-operation
        drop reconnects and resubmits -- the allowed engineering retry of
        11-COMPUTATION-SUBSYSTEM.md section 5); the remote working
        directory is created by the launch wrapper. A clean remote
        refusal (mkdir/cd failure) or an unparseable remote pid is the
        stable ``SSHJobLaunchError`` (job-level, never retried); a
        permanent transport failure records ``failure_class="transport"``
        on the durable record and re-raises the transport error. A
        second submit of the same job is rejected.

        Raises:
            TypeError: ``run_context`` is not a ``RunContext``.
            SSHJobIdentityError: unsafe remote working directory or
                unsafe declared output name.
            ComputeJobNotFoundError: the run was not prepared (call
                ``prepare`` first).
            ComputeJobStateError: the job is not in ``prepared`` state.
            SSHJobLaunchError: the remote launch was refused or did not
                produce a usable remote pid.
            SSHTransportError: a permanent transport failure (recorded
                on the durable record as ``failure_class="transport"``).
        """
        self._validate_context(run_context)
        job_id = self._job_id_for(run_context)
        record = self._read_record(job_id)
        if record.state is not JobState.PREPARED:
            raise ComputeJobStateError(
                f"job {job_id!r} is {record.state.value}; submit requires"
                " a prepared job (call prepare(run_context) first)"
            )
        launch_command = _build_launch_command(
            job_id, RemotePath(record.working_directory), record.command
        )
        try:
            result = self._execute(
                lambda: self._transport.run_command(launch_command)
            )
        except SSHTransportError as exc:
            self._classify_transport_failure(record, exc)
            raise
        if result.exit_code != 0:
            message = (
                f"remote launch of job {job_id!r} failed with status"
                f" {result.exit_code}: {result.stderr.strip()}"
            )
            self._write_record(
                replace(
                    record,
                    failure_class=FAILURE_CLASS_JOB,
                    error=message,
                )
            )
            raise SSHJobLaunchError(message)
        text = result.stdout.strip()
        try:
            remote_pid = int(text)
        except ValueError:
            remote_pid = 0
        if remote_pid <= 0:
            message = (
                f"remote launch of job {job_id!r} did not produce a usable"
                f" remote pid (stdout {result.stdout!r})"
            )
            self._write_record(
                replace(
                    record,
                    failure_class=FAILURE_CLASS_JOB,
                    error=message,
                )
            )
            raise SSHJobLaunchError(message)
        submitted_at = self._now()
        updated = replace(
            record,
            state=JobState.RUNNING,
            remote_pid=remote_pid,
            submitted_at=submitted_at,
            failure_class=None,
            error=None,
        )
        self._write_record(updated)
        return SSHSubmittedJob(
            job_id=updated.job_id,
            run_id=updated.run_id,
            state=updated.state,
            remote_pid=updated.remote_pid,
            submitted_at=submitted_at,
            failure_class=updated.failure_class,
        )

    def status(self, job_id: str) -> SSHJobStatus:
        """Report the job's state from the durable record + remote
        observation, carrying the AC-01 classification (AC-01).

        Terminal and prepared states are answered from the durable record
        alone (no remote contact); a ``running`` record is probed
        remotely under the retry policy and the observed
        completion/failure decision is persisted. A non-zero remote exit
        is recorded as ``failure_class="job"``; a permanent transport
        failure records ``failure_class="transport"`` and raises the
        transport error.

        Raises:
            SSHJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state is JobState.RUNNING:
            try:
                record = self._probe_and_transition(record)
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
        return SSHJobStatus(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            remote_pid=record.remote_pid,
            exit_code=record.exit_code,
            error=record.error,
            failure_class=record.failure_class,
            collected_at=record.collected_at,
            recovery_note=record.recovery_note,
        )

    def collect(self, job_id: str) -> SSHCollectedJob:
        """Gather the outputs of a completed job into artifact
        registrations (AC-01/AC-02).

        Every declared output of the durable record is pulled from the
        remote working directory to the local staging directory
        (``<state_dir>/staging/<job_id>/``, each pull under the retry
        policy) and registered as an ``ArtifactManifest`` through the
        **real** DEV-M3-G02 ``ArtifactRegistry`` (``<state_dir>/
        manifests/``) with a deterministic artifact id
        (``generate_id("artifact", job_id, output_name)``), the staged
        file's real SHA-256 and byte size, the run id and the adapter's
        producer stamp. The durable record is stamped ``collected_at``
        and names the registered ids. Re-collecting an already-collected
        job is idempotent: the same registrations are returned, nothing
        is rewritten.

        Raises:
            SSHJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobStateError: the job is not ``completed``.
            ComputeCollectError: a declared output is missing on the
                remote host or was not staged, or an already-registered
                artifact id carries different content.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
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
            return SSHCollectedJob(
                job_id=record.job_id,
                run_id=record.run_id,
                state=record.state,
                collected_at=record.collected_at,
                artifact_ids=record.artifact_ids,
                artifacts=manifests,
                failure_class=record.failure_class,
            )
        workdir = RemotePath(record.working_directory)
        staged: list[tuple[str, Path]] = []
        for name in sorted(record.outputs):
            remote = workdir.join(name)
            local = self._staging_dir / record.job_id / name
            try:
                self._execute(lambda: self._transport.pull_file(remote, local))
            except SSHRemoteFileNotFoundError as exc:
                raise ComputeCollectError(
                    f"job {job_id!r} completed but its declared output"
                    f" {name!r} is missing on the remote host ({remote}):"
                    f" {exc}"
                ) from exc
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
            if not local.is_file():
                raise ComputeCollectError(
                    f"job {job_id!r} completed but its declared output"
                    f" {name!r} was not staged at {local}"
                )
            staged.append((name, local))
        manifests_list: list[ArtifactManifest] = []
        for name, local in staged:
            try:
                sha256 = compute_sha256(local)
            except ArtifactFileError as exc:
                raise ComputeCollectError(
                    f"cannot checksum declared output {name!r} of job"
                    f" {job_id!r}: {exc}"
                ) from exc
            artifact_id = generate_id("artifact", job_id, name)
            manifest = ArtifactManifest(
                artifact_id=artifact_id,
                uri=str(local),
                sha256=sha256,
                size_bytes=local.stat().st_size,
                created_at=self._now(),
                run_id=record.run_id,
                producer=_PRODUCER_STAMP,
                metadata={
                    "job_id": job_id,
                    "output_name": name,
                    "backend": SSH_BACKEND_NAME,
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
            failure_class=None,
            error=None,
        )
        self._write_record(updated)
        return SSHCollectedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            collected_at=collected_at,
            artifact_ids=artifact_ids,
            artifacts=artifacts,
            failure_class=updated.failure_class,
        )

    def cancel(self, job_id: str) -> SSHCancelledJob:
        """Cancel a prepared or running job (AC-01/AC-03).

        A running job's remote process is terminated with ``kill`` under
        the retry policy and the observation (exit status recovered from
        the remote status file, or a stable recovery note when the
        process had not exited at the moment of observation) is recorded
        with the cancellation decision. A prepared job is cancelled
        without any remote contact. Cancelling a terminal job is
        rejected.

        Raises:
            SSHJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobStateError: the job is already terminal.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
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
            record,
            state=JobState.CANCELLED,
            cancelled_at=cancelled_at,
            failure_class=None,
            error=None,
        )
        if record.state is JobState.RUNNING:
            if record.remote_pid is None:
                raise ComputeJobRecordError(
                    f"job record {record.job_id!r} is running but carries"
                    " no remote_pid; the record is inconsistent"
                )
            try:
                self._execute(
                    lambda: self._transport.run_command(
                        RemoteCommand(("kill", str(record.remote_pid)))
                    )
                )
                probe = self._probe_remote(record)
                if not probe.running:
                    updated = replace(updated, exit_code=probe.exit_code)
                    if probe.exit_code is None:
                        updated = replace(
                            updated,
                            recovery_note=SSH_EXIT_CODE_UNAVAILABLE_NOTE,
                        )
                else:
                    updated = replace(
                        updated, recovery_note=TERMINATE_PENDING_NOTE
                    )
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
        self._write_record(updated)
        return SSHCancelledJob(
            job_id=updated.job_id,
            run_id=updated.run_id,
            state=JobState.CANCELLED,
            remote_pid=updated.remote_pid,
            exit_code=updated.exit_code,
            cancelled_at=cancelled_at,
            failure_class=updated.failure_class,
        )

    def resume(self, job_id: str) -> SSHResumedJob:
        """Re-attach to an existing durable job record (AC-01/AC-02).

        The record is re-hydrated from disk (the recovery discipline of
        M1); a ``running`` record's remote process is probed under the
        retry policy and the observed completion/failure decision is
        persisted. Prepared and terminal records are returned as they
        are -- resume never depends on the session object that submitted
        the job.

        Raises:
            SSHJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state is JobState.RUNNING:
            try:
                record = self._probe_and_transition(record)
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
        return SSHResumedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            remote_pid=record.remote_pid,
            exit_code=record.exit_code,
            failure_class=record.failure_class,
        )
