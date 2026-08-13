"""Slurm-over-SSH ComputeAdapter: sbatch/squeue/sacct/scancel job flow
through the injectable SSH transport, with Modules-aware execution
metadata (DEV-M7-G04, deliverable).

Implements the ComputeAdapter contract of the frozen specs over SSH to
a Slurm cluster:

* ``15-ADAPTER-SPEC.md`` section 3 (the same six operations as the
  local and ssh adapters, with ``slurm_ssh`` as the primary v0.1
  backend);
* ``11-COMPUTATION-SUBSYSTEM.md`` section 2 (the same six operations),
  section 3 (the adapter -- not the worker prompt -- owns connection
  details, module-loading mapping, working/scratch path conventions,
  ``sbatch`` submission, ``squeue``/``sacct`` status inspection,
  ``scancel``, result collection and job-ID persistence) and section 6
  (long-job behavior: the worker submits then exits; the durable record
  -- not the session object -- holds the job's identity).

Transport reuse (DEV-M7-G03)
----------------------------
All remote execution and file transfer happen through the injectable
:class:`SSHTransport` ABC imported from ``ssh.py`` -- no transport
logic is duplicated. ``sbatch`` submission pushes the generated batch
script with ``push_file`` and runs ``sbatch`` through
``run_command``; status probes run ``squeue`` (active states) and
``sacct`` (terminal states); cancellation runs ``scancel``. The
documented error-translation contract is inherited: connection-level
failures raise the ``SSHTransportError`` subclasses, are retried under
the injected :class:`SSHRetryPolicy` and are recorded on the durable
record as ``failure_class="transport"``; a clean remote answer is
never retried and never classified as a transport failure.

External Slurm job id (AC-01)
-----------------------------
``submit`` parses the ``Submitted batch job <id>`` line of ``sbatch``
into the **external Slurm job id** (e.g. ``423554``) and stores it as
a first-class field (``external_id``) of the durable record at
``<state_dir>/jobs/<job_id>.json``. A **fresh adapter instance** over
the same state directory can ``status``/``collect``/``cancel``/
``resume`` the job from the record alone -- the submitting Worker
session is never required again (11-COMPUTATION-SUBSYSTEM.md section
6). All scheduler queries derive their ``--jobs`` argument from the
record's ``external_id``, never from session state.

State normalization (AC-02)
---------------------------
Scheduler states observed through ``squeue`` (``%T`` column:
``PENDING``/``RUNNING``/...) and ``sacct`` (``State``:
``COMPLETED``/``FAILED``/``CANCELLED``/...) are normalized to the
canonical :class:`JobState` vocabulary through the ordered
:class:`SLURM_STATE_RULES` table (stable rule ids ``R-SLURM-S1..Sn``,
first match wins, trailing total default). A queued job (``PENDING``)
is reported as ``running`` -- the durable record has no queued state;
queued means alive and not yet terminal -- and the raw observed state
is carried on the status outcome and on the terminal record so queued
versus actually-running remains distinguishable. Terminal decisions
are persisted once; a terminal record is answered from the record
alone (the transport is never contacted again). Totality is proven by
:func:`validate_slurm_state_rules` (unique ids, unique scheduler
states, exactly one trailing total default) and asserted by the
evaluator's post-assert, mirroring the convergence rule-table
discipline.

Scientific inputs are never modified (AC-03)
--------------------------------------------
The adapter never writes to the scientific input: it stages outputs
only. ``submit`` generates a batch script from the record (command,
modules, environment) and pushes it; ``collect`` pulls declared
outputs; retries under the retry policy re-run transport operations
that are idempotent in their remote effect. Input files are read-only
by construction -- a test proves the input bytes are identical after
scripted transport-failure/retry cycles and after failed job outcomes.

Modules/environment metadata
----------------------------
The launch command supports ``module load <name>`` statements and
environment overrides applied by the generated batch script before the
job command. The durable record captures the module list and the
environment snapshot as documented fields (stable, serializable,
secret-free by contract: credentials live only on the constructor
boundary and never appear in any record field -- the same discipline
as the ssh adapter).

Determinism and injectable surfaces
-----------------------------------
Everything a session can vary is injected: the transport, the retry
policy, the clock (a ``now`` callable producing timestamp strings --
no wall clock in the tested path) and the state directory. Identical
injected inputs produce byte-identical durable records (sorted
canonical JSON) and byte-identical batch scripts. No randomness, no
wall-clock dependence, no network and no hidden filesystem access
anywhere in the tested path. Error discipline follows the house
paradigm: ``TypeError`` at public boundaries for wrong types, a
``ValueError``-subclass error hierarchy with stable messages
otherwise.
"""

from __future__ import annotations

import json
import re
import shlex
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence, TypeVar

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
    RunContext,
    utc_now,
)
from scientific_reproduction.adapters.compute.ssh import (
    FAILURE_CLASS_JOB,
    FAILURE_CLASS_TRANSPORT,
    RemoteCommand,
    RemotePath,
    RemoteResult,
    SSHCredentials,
    SSHRemoteFileNotFoundError,
    SSHRetryPolicy,
    SSHTransport,
    SSHTransportError,
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
    "SLURM_BACKEND_NAME",
    "SLURM_JOB_RECORD_VERSION",
    "SLURM_SCRIPTS_STATE_DIR",
    "SLURM_STAGING_DIR",
    "SLURM_STATE_RULES",
    "SLURM_STATE_UNAVAILABLE_NOTE",
    "SlurmAdapterError",
    "SlurmCancelledJob",
    "SlurmCollectedJob",
    "SlurmComputeAdapter",
    "SlurmJobIdentityError",
    "SlurmJobLaunchError",
    "SlurmJobRecord",
    "SlurmJobStatus",
    "SlurmPreparedJob",
    "SlurmResumedJob",
    "SlurmStateDecision",
    "SlurmStateRule",
    "SlurmSubmittedJob",
    "normalize_scheduler_state",
    "validate_slurm_state_rules",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The backend name stamped into every durable job record (the primary
#: v0.1 slurm_ssh backend of 15-ADAPTER-SPEC.md section 3).
SLURM_BACKEND_NAME: str = "slurm_ssh"

#: Adapter identity (mirrors the ``adapter:<id>@v<version>`` producer
#: stamping of the local, ssh and research adapters).
ADAPTER_ID: str = "compute/slurm_ssh"

#: Adapter contract version. Bumped whenever a contract rule changes; the
#: same version always accepts the same run contexts and yields the same
#: records.
ADAPTER_VERSION: str = "1.0"

#: Version of the durable slurm job-record schema (``record_version`` key
#: of :class:`SlurmJobRecord`); records of a different version are
#: refused.
SLURM_JOB_RECORD_VERSION: str = "1.0"

#: Collect staging directory of a compute state directory, relative to
#: the injected state directory: pulled outputs are staged at
#: ``<state_dir>/staging/<job_id>/<output_name>`` before checksumming and
#: artifact registration (mirrors the ssh adapter).
SLURM_STAGING_DIR: str = "staging"

#: Directory of the generated batch scripts, relative to the injected
#: state directory: ``submit`` writes the script for a job at
#: ``<state_dir>/scripts/<job_id>.slurm.sh`` before pushing it remotely.
SLURM_SCRIPTS_STATE_DIR: str = "scripts"

#: Stable recovery note written when a job leaves the scheduler's view
#: without a reported terminal state (neither ``squeue`` nor ``sacct``
#: reported it, e.g. accounting lag or a disabled accounting daemon).
#: The job is recorded as completed because ``collect`` independently
#: verifies every declared output.
SLURM_STATE_UNAVAILABLE_NOTE: str = (
    "the job left the scheduler's view without a reported terminal state"
    " (neither squeue nor sacct reported it); job recorded as completed;"
    " collect verifies declared outputs"
)

#: Producer stamp written into collected artifact manifests.
_PRODUCER_STAMP: str = f"adapter:{ADAPTER_ID}@v{ADAPTER_VERSION}"

#: Characters allowed in a module name (``module load <name>``): module
#: names like ``gcc/13.2.0`` legitimately carry ``/``, ``.`` and ``-``,
#: so the ssh safe-segment discipline is widened for this one field;
#: shell metacharacters are never allowed.
_MODULE_NAME_ALLOWED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "._+@:/-"
)


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class SlurmAdapterError(ComputeAdapterError):
    """Base error of the slurm-over-ssh compute adapter subsystem."""


class SlurmJobIdentityError(SlurmAdapterError):
    """Raised for malformed job/run ids, unsafe declared output names,
    unsafe module names and unsafe environment variable names (the
    FND-M9-G02-01 discipline applied to every id-bearing field: no path
    separators, no glob metacharacters, no whitespace)."""


class SlurmJobLaunchError(SlurmAdapterError):
    """Raised when the remote submission did not produce a usable launch.

    A clean remote-side answer (mkdir refusal, an ``sbatch`` refusal, an
    unparseable ``Submitted batch job <id>`` line) is a job-level fact,
    never a connection failure: it does not subclass
    :class:`SSHTransportError` and is never retried.
    """


# ---------------------------------------------------------------------------
# Module/environment vocabulary (FND-M9-G02-01 applied to the wrapper)
# ---------------------------------------------------------------------------


def _is_safe_module_name(name: str) -> bool:
    """True iff ``name`` is a safe module name.

    Module names are embedded in the generated batch script as
    ``module load <name>`` lines, so shell metacharacters, whitespace
    and NUL are rejected; only alphanumerics and ``._+@:/-`` are
    allowed (``gcc/13.2.0``, ``python/3.11.5``, ...).
    """
    return (
        name not in ("", ".", "..")
        and not name.startswith("-")
        and all(char in _MODULE_NAME_ALLOWED for char in name)
    )


_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_safe_env_name(name: str) -> bool:
    """True iff ``name`` is a safe shell variable name."""
    return _ENV_NAME_PATTERN.fullmatch(name) is not None


def _is_safe_env_value(value: str) -> bool:
    """True iff ``value`` can be embedded in the generated batch script
    (no NUL, no newline; quotes are single-quote-escaped on embedding)."""
    return "\x00" not in value and "\n" not in value and "\r" not in value


def _validate_modules(modules: Any) -> tuple[str, ...]:
    """Validate the constructor ``modules`` and return it as a tuple."""
    if not isinstance(modules, tuple) or not all(
        isinstance(entry, str) for entry in modules
    ):
        raise TypeError(
            "modules must be a tuple of module name strings, got"
            f" {type(modules).__name__}"
        )
    for name in modules:
        if not _is_safe_module_name(name):
            raise SlurmJobIdentityError(
                f"module name {name!r} is not safe (only alphanumerics and"
                " '._+@:/-', no whitespace, no shell metacharacters, no"
                " leading '-')"
            )
    return tuple(modules)


def _validate_environment(environment: Any) -> tuple[tuple[str, str], ...]:
    """Validate the constructor ``environment`` and return it as a
    key-sorted tuple of pairs (deterministic record bytes)."""
    if environment is None:
        return ()
    if not isinstance(environment, Mapping):
        raise TypeError(
            "environment must be a mapping of variable name to value, got"
            f" {type(environment).__name__}"
        )
    pairs: list[tuple[str, str]] = []
    for key, value in environment.items():
        if not isinstance(key, str) or not _is_safe_env_name(key):
            raise SlurmJobIdentityError(
                f"environment variable name {key!r} is not a safe shell"
                " variable name (^[A-Za-z_][A-Za-z0-9_]*$)"
            )
        if not isinstance(value, str):
            raise TypeError(
                f"environment value of {key!r} must be a string, got"
                f" {type(value).__name__}"
            )
        if not _is_safe_env_value(value):
            raise SlurmJobIdentityError(
                f"environment value of {key!r} must not contain NUL or"
                " newline characters"
            )
        pairs.append((key, value))
    return tuple(sorted(pairs))


# ---------------------------------------------------------------------------
# The ordered scheduler-state normalization table (AC-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlurmStateRule:
    """One entry of the ordered scheduler-state rule table.

    ``scheduler_state`` is the normalized (uppercased) token observed
    from ``squeue``/``sacct``; ``None`` marks the **trailing total
    default** -- the last rule, matching every input, so the table is
    total and every scheduler state is classified.
    """

    rule_id: str
    description: str
    scheduler_state: str | None
    state: JobState


@dataclass(frozen=True)
class SlurmStateDecision:
    """One classification of an observed scheduler state.

    ``scheduler_state`` is the normalized token; ``state`` the canonical
    :class:`JobState` it maps to; ``rule_id``/``description`` name the
    deciding rule (never ``None``: the trailing total default always
    matches); ``unknown`` is True exactly when the total default matched
    (the token was not a documented scheduler state).
    """

    scheduler_state: str
    state: JobState
    rule_id: str
    description: str
    unknown: bool

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the decision (the rule trace of one
        observation)."""
        return {
            "scheduler_state": self.scheduler_state,
            "state": self.state.value,
            "rule_id": self.rule_id,
            "description": self.description,
            "unknown": self.unknown,
        }


#: The ordered scheduler-state rule table. First match wins; order is
#: normative; ``R-SLURM-S27`` is the total default so every observed
#: state is classified (AC-02). A queued/suspended/completing job is
#: ``running`` (alive and not yet terminal -- the durable record has no
#: queued state); the raw state stays observable on the status outcome
#: and the terminal record. An unrecognized state is a clean remote
#: answer: it is a job-level outcome (``failed``) with the raw state in
#: the stable error -- never a transport failure.
SLURM_STATE_RULES: tuple[SlurmStateRule, ...] = (
    SlurmStateRule(
        rule_id="R-SLURM-S1",
        description="queued, waiting for allocation (alive, not yet"
        " terminal)",
        scheduler_state="PENDING",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S2",
        description="queued (squeue compact abbreviation of PENDING)",
        scheduler_state="PD",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S3",
        description="allocating/being configured (alive)",
        scheduler_state="CONFIGURING",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S4",
        description="configuring (squeue compact abbreviation)",
        scheduler_state="CF",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S5",
        description="suspended (alive; will resume)",
        scheduler_state="SUSPENDED",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S6",
        description="suspended (squeue compact abbreviation)",
        scheduler_state="S",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S7",
        description="running on the allocated nodes",
        scheduler_state="RUNNING",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S8",
        description="running (squeue compact abbreviation)",
        scheduler_state="R",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S9",
        description="completing (alive; not yet terminal)",
        scheduler_state="COMPLETING",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S10",
        description="completing (squeue compact abbreviation)",
        scheduler_state="CG",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S11",
        description="requeued (alive; will resume)",
        scheduler_state="REQUEUED",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S12",
        description="requeued (squeue compact abbreviation)",
        scheduler_state="RQ",
        state=JobState.RUNNING,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S13",
        description="completed with exit status 0",
        scheduler_state="COMPLETED",
        state=JobState.COMPLETED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S14",
        description="completed (squeue/sacct compact abbreviation)",
        scheduler_state="CD",
        state=JobState.COMPLETED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S15",
        description="cancelled by a user or administrator",
        scheduler_state="CANCELLED",
        state=JobState.CANCELLED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S16",
        description="cancelled (squeue/sacct compact abbreviation)",
        scheduler_state="CA",
        state=JobState.CANCELLED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S17",
        description="failed (non-zero exit or batch script failure)",
        scheduler_state="FAILED",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S18",
        description="failed (squeue/sacct compact abbreviation)",
        scheduler_state="F",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S19",
        description="terminated upon reaching its time limit",
        scheduler_state="TIMEOUT",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S20",
        description="timeout (squeue/sacct compact abbreviation)",
        scheduler_state="TO",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S21",
        description="terminated due to node failure",
        scheduler_state="NODE_FAIL",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S22",
        description="node failure (squeue/sacct compact abbreviation)",
        scheduler_state="NF",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S23",
        description="terminated due to out-of-memory",
        scheduler_state="OUT_OF_MEMORY",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S24",
        description="out of memory (squeue/sacct compact abbreviation)",
        scheduler_state="OOM",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S25",
        description="terminated due to preemption",
        scheduler_state="PREEMPTED",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S26",
        description="preempted (squeue/sacct compact abbreviation)",
        scheduler_state="PR",
        state=JobState.FAILED,
    ),
    SlurmStateRule(
        rule_id="R-SLURM-S27",
        description="total default: unrecognized scheduler state"
        " (recorded as a job-level failure carrying the raw state)",
        scheduler_state=None,
        state=JobState.FAILED,
    ),
)


def validate_slurm_state_rules(rules: Sequence[SlurmStateRule]) -> None:
    """Prove the rule table is well-formed and **total**.

    Checks: a non-empty sequence of :class:`SlurmStateRule` entries with
    unique non-empty rule ids, unique non-empty uppercase scheduler
    states, exactly one total default (``scheduler_state`` is None), and
    the total default as the **trailing** rule -- totality of a
    first-match-wins table is only proven by a trailing default.

    Raises:
        TypeError: ``rules`` is not a sequence of ``SlurmStateRule``.
        SlurmAdapterError: the table violates a totality invariant.
    """
    if not isinstance(rules, Sequence) or isinstance(rules, (str, bytes)):
        raise TypeError(
            "rules must be a sequence of SlurmStateRule entries, got"
            f" {type(rules).__name__}"
        )
    if not rules:
        raise SlurmAdapterError(
            "the Slurm state rule table must not be empty"
        )
    seen_ids: set[str] = set()
    seen_states: set[str] = set()
    defaults = 0
    for index, rule in enumerate(rules):
        if not isinstance(rule, SlurmStateRule):
            raise TypeError(
                f"rules entry {index} must be a SlurmStateRule, got"
                f" {type(rule).__name__}"
            )
        if not rule.rule_id:
            raise SlurmAdapterError(
                f"Slurm state rule {index} has an empty rule_id"
            )
        if rule.rule_id in seen_ids:
            raise SlurmAdapterError(
                f"duplicate Slurm state rule id {rule.rule_id!r}"
            )
        seen_ids.add(rule.rule_id)
        if rule.scheduler_state is None:
            defaults += 1
            continue
        if not rule.scheduler_state:
            raise SlurmAdapterError(
                f"Slurm state rule {rule.rule_id!r} has an empty scheduler"
                " state"
            )
        if rule.scheduler_state != rule.scheduler_state.upper():
            raise SlurmAdapterError(
                f"Slurm state rule {rule.rule_id!r} uses a non-uppercase"
                f" scheduler state {rule.scheduler_state!r}; observed"
                " tokens are uppercased before matching"
            )
        if rule.scheduler_state in seen_states:
            raise SlurmAdapterError(
                f"duplicate scheduler state {rule.scheduler_state!r} in the"
                " Slurm state rule table"
            )
        seen_states.add(rule.scheduler_state)
    if defaults != 1:
        raise SlurmAdapterError(
            "the Slurm state rule table must have exactly one total"
            f" default, got {defaults}"
        )
    if rules[-1].scheduler_state is not None:
        raise SlurmAdapterError(
            "the trailing Slurm state rule must be the total default"
            " (a first-match-wins table is only total with a trailing"
            " default)"
        )


def normalize_scheduler_state(raw: str) -> SlurmStateDecision:
    """Normalize one observed scheduler state token to the canonical
    :class:`JobState` vocabulary (AC-02).

    The token is stripped, reduced to its first whitespace-separated
    word (``sacct`` can report ``CANCELLED by 1000`` -- the reason is
    dropped) and uppercased; the first matching rule of
    :class:`SLURM_STATE_RULES` decides. Identical inputs always produce
    byte-identical decisions.

    Raises:
        TypeError: ``raw`` is not a string.
        ValueError: ``raw`` is empty or whitespace-only (the table is
            total over non-empty tokens; an empty observation means the
            scheduler had no row and is handled by the caller).
    """
    if not isinstance(raw, str):
        raise TypeError(
            f"scheduler state must be a str, got {type(raw).__name__}"
        )
    stripped = raw.strip()
    if not stripped:
        raise ValueError("scheduler state must be a non-empty string")
    token = stripped.split(None, 1)[0].upper()
    matched: SlurmStateRule | None = None
    for rule in SLURM_STATE_RULES:
        if rule.scheduler_state is None or rule.scheduler_state == token:
            matched = rule
            break
    # The trailing total default always matches, so this can never be
    # None (mirrors the convergence rule table's post-assert).
    assert matched is not None
    return SlurmStateDecision(
        scheduler_state=token,
        state=matched.state,
        rule_id=matched.rule_id,
        description=matched.description,
        unknown=matched.scheduler_state is None,
    )


def _first_token(text: str) -> str | None:
    """The first whitespace-separated token of ``text``, or None when
    ``text`` has no non-whitespace content."""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if parts:
            return parts[0]
    return None


def _parse_exit_code(field: str) -> int | None:
    """Parse the primary exit code of a ``sacct`` ``ExitCode`` field
    (``"0:0"`` -> 0); None when the field carries no parseable code."""
    primary = field.split(":", 1)[0].strip()
    if primary.isdigit():
        return int(primary)
    return None


def _parse_submitted_job_id(result: RemoteResult) -> int | None:
    """Parse the external Slurm job id from an ``sbatch`` answer.

    ``sbatch`` prints ``Submitted batch job <id>`` on stdout (or
    stderr); the first match wins.
    """
    text = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"Submitted batch job\s+(\d+)", text)
    if match is None:
        return None
    return int(match.group(1))


# ---------------------------------------------------------------------------
# The durable job record (AC-01: persisted external Slurm job id)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlurmJobRecord:
    """The durable, session-independent record of one Slurm job.

    Persisted at ``<state_dir>/jobs/<job_id>.json`` and re-hydrated from
    disk on every operation (the M1 recovery discipline): a fresh adapter
    instance over the same state directory recovers the job from this
    record alone (AC-01 -- the **external Slurm job id** is a first-class
    field, ``external_id``, so a fresh session can ``status``/``collect``/
    ``cancel``/``resume`` it without the submitting Worker). The record
    carries **no credential fields** and no secrets: credentials live
    only on the adapter's constructor boundary; ``modules`` and
    ``environment`` are the caller-supplied, serializable execution
    metadata snapshot (AC-02-style discipline). The ``failure_class``
    field carries the classification -- ``None`` (healthy), ``"transport"``
    (a permanent connection-level failure was observed) or ``"job"``
    (the remote computation itself failed) -- and ``scheduler_state``
    records the raw observed token that led to a terminal decision.

    Field names are the exact JSON keys of the persisted record
    (``to_dict`` / ``from_dict`` round-trip them). The record is a
    runtime record -- there is no ``schemas/job.schema.yaml`` -- so
    ``from_dict`` validates against this documented contract instead
    (like the ssh adapter), with stable errors.
    """

    record_version: ClassVar[str] = SLURM_JOB_RECORD_VERSION

    job_id: str
    run_id: str
    state: JobState
    command: tuple[str, ...]
    working_directory: str
    outputs: tuple[str, ...]
    created_at: str
    modules: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    submitted_at: str | None = None
    external_id: int | None = None
    scheduler_state: str | None = None
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
            "backend": SLURM_BACKEND_NAME,
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "command": list(self.command),
            "working_directory": self.working_directory,
            "outputs": list(self.outputs),
            "created_at": self.created_at,
            "modules": list(self.modules),
            "environment": [[key, value] for key, value in self.environment],
        }
        for key in (
            "submitted_at",
            "external_id",
            "scheduler_state",
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
    def from_dict(cls, data: Mapping[str, Any]) -> SlurmJobRecord:
        """Build a record from a plain dict (the slurm job-record
        contract).

        Raises:
            TypeError: ``data`` is not a mapping.
            ComputeJobRecordError: a required field is missing or a value
                violates the contract (unknown version/backend/state,
                malformed ids, unsafe paths, output names, module names
                or environment fields, unknown ``failure_class``,
                mistyped fields).
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "SlurmJobRecord.from_dict expects a mapping, got"
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
        if backend != SLURM_BACKEND_NAME:
            raise ComputeJobRecordError(
                f"job record backend {backend!r} is not"
                f" {SLURM_BACKEND_NAME!r}"
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
            if not RemotePath.segment_is_safe(name):
                raise ComputeJobRecordError(
                    f"job record output {name!r} is not a safe remote path"
                    " segment (no '/', no '\\', not '.' or '..', no glob"
                    " metacharacters, no whitespace)"
                )
        created_at = _require_nonempty_str(required("created_at"), "created_at")
        modules = _require_str_tuple(data, "modules", required=False)
        for name in modules:
            if not _is_safe_module_name(name):
                raise ComputeJobRecordError(
                    f"job record module {name!r} is not a safe module name"
                    " (only alphanumerics and '._+@:/-', no whitespace, no"
                    " shell metacharacters)"
                )
        environment = _require_env_pairs(data, "environment")
        submitted_at = _optional_str(data, "submitted_at")
        external_id = _optional_int(data, "external_id")
        if external_id is not None and external_id <= 0:
            raise ComputeJobRecordError(
                f"job record external_id must be a positive int, got"
                f" {external_id}"
            )
        scheduler_state = _optional_str(data, "scheduler_state")
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
            modules=modules,
            environment=environment,
            submitted_at=submitted_at,
            external_id=external_id,
            scheduler_state=scheduler_state,
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


def _require_env_pairs(
    data: Mapping[str, Any], name: str
) -> tuple[tuple[str, str], ...]:
    """Return the ``[name, value]`` pair entries of an environment field.

    Raises:
        ComputeJobRecordError: the field is absent (defaults to empty),
            not a list of two-entry lists, or an entry violates the safe
            variable-name/value contract.
    """
    value = data.get(name)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ComputeJobRecordError(
            f"job record field {name!r} must be a list of [name, value]"
            f" pairs, got {type(value).__name__}"
        )
    pairs: list[tuple[str, str]] = []
    for entry in value:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or not isinstance(entry[1], str)
        ):
            raise ComputeJobRecordError(
                f"job record field {name!r} entries must be [name, value]"
                f" pairs of strings, got {entry!r}"
            )
        key, env_value = entry
        if not _is_safe_env_name(key):
            raise ComputeJobRecordError(
                f"job record environment name {key!r} is not a safe shell"
                " variable name (^[A-Za-z_][A-Za-z0-9_]*$)"
            )
        if not _is_safe_env_value(env_value):
            raise ComputeJobRecordError(
                f"job record environment value of {key!r} must not contain"
                " NUL or newline characters"
            )
        pairs.append((key, env_value))
    return tuple(sorted(pairs))


# ---------------------------------------------------------------------------
# The batch script generation (launch wrapper capturing status)
# ---------------------------------------------------------------------------


def _single_quote(value: str) -> str:
    """POSIX single-quote a shell fragment (``'`` -> ``'\\''``)."""
    return "'" + value.replace("'", "'\\''") + "'"


def _build_batch_script(
    job_id: str,
    working_directory: RemotePath,
    command: tuple[str, ...],
    modules: tuple[str, ...],
    environment: tuple[tuple[str, str], ...],
) -> str:
    """The batch script of one job (pure, deterministic).

    The script is the Slurm-side launch wrapper: it loads the declared
    modules (a failing ``module load`` aborts the job under ``set -e``),
    exports the declared environment, runs the shell-quoted command and
    captures its exit status in the status file
    (``<workdir>/.sr_<job_id>_job.status``) before exiting with it -- so
    the scheduler's terminal state (``sacct`` ``State``/``ExitCode``)
    always matches the command's own exit status. Every interpolated
    value is validated before construction: the job id is a generated
    id, the working directory a validated :class:`RemotePath`, the
    command entries are shell-quoted and the module names and
    environment keys pass the safe-vocabulary discipline. The same
    inputs always produce byte-identical script content.

    Raises:
        SlurmJobIdentityError: ``job_id`` is not a generated job id.
    """
    if not isinstance(job_id, str) or not is_valid_id(job_id, "job"):
        raise SlurmJobIdentityError(
            f"job id {job_id!r} is not a generated job id"
            " (sr_job_<32 hex chars>)"
        )
    status_path = working_directory.join(f".sr_{job_id}_job.status")
    lines: list[str] = [
        "#!/bin/bash",
        f"# generated by adapter:{ADAPTER_ID}@v{ADAPTER_VERSION}; do not"
        " edit",
        "set -e",
    ]
    for module in modules:
        lines.append(f"module load {module}")
    for key, value in environment:
        lines.append(f"export {key}={_single_quote(value)}")
    lines.append("set +e")
    lines.append(
        f"{shlex.join(command)}"
        f"; _sr_exit_code=$?; echo $_sr_exit_code >"
        f" {shlex.quote(status_path.value)}; exit $_sr_exit_code"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Operation result records (frozen decision records)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlurmPreparedJob:
    """The outcome of ``prepare``: the staged durable record, including
    the captured Modules/environment execution metadata."""

    job_id: str
    run_id: str
    state: JobState
    created_at: str
    working_directory: str
    command: tuple[str, ...]
    outputs: tuple[str, ...]
    modules: tuple[str, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
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
            "modules": list(self.modules),
            "environment": [[key, value] for key, value in self.environment],
        }
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class
        return data


@dataclass(frozen=True)
class SlurmSubmittedJob:
    """The outcome of ``submit``: the submitted job and its **external
    Slurm job id** (AC-01)."""

    job_id: str
    run_id: str
    state: JobState
    external_id: int | None
    submitted_at: str
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the submitted job."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
            "external_id": self.external_id,
            "submitted_at": self.submitted_at,
        }
        if self.failure_class is not None:
            data["failure_class"] = self.failure_class
        return data


@dataclass(frozen=True)
class SlurmJobStatus:
    """The outcome of ``status``: durable record + scheduler observation,
    carrying the external id, the raw observed scheduler state (None when
    the answer came from the record alone) and the ``failure_class``."""

    job_id: str
    run_id: str
    state: JobState
    external_id: int | None = None
    scheduler_state: str | None = None
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
            "external_id",
            "scheduler_state",
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
class SlurmCancelledJob:
    """The outcome of ``cancel``: the cancellation decision record."""

    job_id: str
    run_id: str
    state: JobState
    external_id: int | None = None
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
        for key in ("external_id", "exit_code", "cancelled_at", "failure_class"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class SlurmResumedJob:
    """The outcome of ``resume``: re-attachment to the durable record."""

    job_id: str
    run_id: str
    state: JobState
    external_id: int | None = None
    exit_code: int | None = None
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the resumed job."""
        data: dict[str, Any] = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state.value,
        }
        for key in ("external_id", "exit_code", "failure_class"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class SlurmCollectedJob:
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
# The scheduler probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlurmSchedulerProbe:
    """One observation of the scheduler's view of a job.

    ``state`` is the canonical state of the observation, or None when
    the job left the scheduler's view without a reported terminal state;
    ``scheduler_state`` the raw observed token; ``exit_code`` the
    ``sacct`` exit code when available; ``unknown`` whether the
    observation matched the total default of the rule table.
    """

    state: JobState | None
    scheduler_state: str | None = None
    exit_code: int | None = None
    unknown: bool = False


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

_T = TypeVar("_T")


class SlurmComputeAdapter:
    """Slurm-over-SSH compute adapter (15-ADAPTER-SPEC.md section 3).

    All remote work happens through the injectable :class:`SSHTransport`
    imported from the ssh adapter (DEV-M7-G03): ``sbatch`` submission
    (script pushed with ``push_file``, invoked with ``run_command``),
    ``squeue``/``sacct`` status probes and ``scancel`` cancellation.
    Transport failures are retried under the injected retry policy and
    recorded as ``failure_class="transport"``; clean remote answers are
    job-level facts, never retried.

    Args:
        credentials: the remote connection identity and secrets,
            accepted at the constructor boundary and held in memory only
            -- never persisted to any state directory, working directory
            or durable record.
        state_dir: the injected state directory. Durable job records
            live at ``<state_dir>/jobs/<job_id>.json``, collected
            artifact manifests at ``<state_dir>/manifests/``, generated
            batch scripts at ``<state_dir>/scripts/`` and pulled outputs
            are staged at ``<state_dir>/staging/<job_id>/``. A fresh
            adapter instance over the same state directory recovers the
            same jobs from the records alone.
        transport: the injectable remote boundary (required -- tests
            inject a scripted double).
        modules: the Modules statements applied by the generated batch
            script before the job command (``module load <name>``); a
            tuple of safe module names, captured on the durable record
            (a stable, serializable snapshot).
        environment: caller-supplied environment overrides applied by
            the generated batch script and captured on the durable
            record (a stable, serializable snapshot -- no secrets; the
            adapter never writes anything else to the job's environment).
        retry_policy: the bounded reconnect/retry policy; defaults to
            :class:`SSHRetryPolicy` (3 attempts, exponential backoff).
        now: injectable clock returning a timestamp string (default
            ``utc_now``); all recorded timestamps come from it -- no
            wall clock in the tested path.

    Raises:
        TypeError: ``credentials`` is not an ``SSHCredentials``,
            ``transport`` is not an ``SSHTransport``, ``modules`` is not
            a tuple of strings, or ``environment`` is not a mapping of
            strings.
        SlurmJobIdentityError: an unsafe module name or environment
            variable name.
    """

    def __init__(
        self,
        credentials: SSHCredentials,
        state_dir: str | Path,
        *,
        transport: SSHTransport,
        modules: tuple[str, ...] = (),
        environment: Mapping[str, str] | None = None,
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
        self._modules = _validate_modules(modules)
        self._environment = _validate_environment(environment)
        self._retry_policy = (
            retry_policy if retry_policy is not None else SSHRetryPolicy()
        )
        self._now_fn = now if now is not None else utc_now
        self._jobs_dir = self._state_dir / JOBS_STATE_DIR
        self._scripts_dir = self._state_dir / SLURM_SCRIPTS_STATE_DIR
        self._staging_dir = self._state_dir / SLURM_STAGING_DIR
        self._registry = ArtifactRegistry(self._state_dir / ARTIFACTS_STATE_DIR)

    # -- identity, persistence and injectable surfaces ---------------------

    @property
    def credentials(self) -> SSHCredentials:
        """The injected credentials (in memory only)."""
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
    def modules(self) -> tuple[str, ...]:
        """The Modules statements applied by the batch script and
        captured on the durable record."""
        return self._modules

    @property
    def environment(self) -> tuple[tuple[str, str], ...]:
        """The caller-supplied environment snapshot (key-sorted pairs)."""
        return self._environment

    @property
    def retry_policy(self) -> SSHRetryPolicy:
        """The injected reconnect/retry policy."""
        return self._retry_policy

    def _job_id_for(self, run_context: RunContext) -> str:
        return generate_id("job", run_context.run_id)

    def _check_job_id(self, job_id: str) -> None:
        if not isinstance(job_id, str) or not is_valid_id(job_id, "job"):
            raise SlurmJobIdentityError(
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
            if not RemotePath.segment_is_safe(name):
                raise SlurmJobIdentityError(
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

    def _write_record(self, record: SlurmJobRecord) -> None:
        atomic_write(
            self._job_path(record.job_id), self._canonical(record.to_dict())
        )

    def _try_read(self, job_id: str) -> SlurmJobRecord | None:
        """Return the durable record, or None when absent."""
        path = self._job_path(job_id)
        if not path.is_file():
            return None
        return self._read_record(job_id)

    def _read_record(self, job_id: str) -> SlurmJobRecord:
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
            return SlurmJobRecord.from_dict(raw)
        except (TypeError, ValueError) as exc:
            raise ComputeJobRecordError(
                f"corrupt job record at {path}: {exc}"
            ) from exc

    def read_job(self, job_id: str) -> SlurmJobRecord:
        """Return the durable record of a job (re-hydrated from disk).

        Raises:
            SlurmJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        return self._read_record(job_id)

    # -- transport lifecycle and reconnect ---------------------------------

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
        re-runs the pending operation -- at most ``max_attempts`` times.
        When the attempts are exhausted the last error is re-raised; a
        clean remote answer (non-zero exit status, empty observation) is
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
        self, record: SlurmJobRecord, exc: SSHTransportError
    ) -> None:
        """Persist a permanent transport failure on the durable record
        and let ``exc`` propagate."""
        self._write_record(
            replace(
                record,
                failure_class=FAILURE_CLASS_TRANSPORT,
                error=str(exc),
            )
        )

    def _now(self) -> str:
        return self._now_fn()

    # -- scheduler observation and state machine decisions -----------------

    def _probe_scheduler(self, record: SlurmJobRecord) -> SlurmSchedulerProbe:
        """Observe the scheduler's view of a running job (AC-02).

        ``squeue`` is queried first (active states: PENDING/RUNNING/
        ...). When the job left the queue, ``sacct`` is queried for the
        terminal state and exit code. A job absent from both is the
        state-unavailable probe (the caller records the stable recovery
        note). Every probe runs under the retry policy; a permanent
        transport failure propagates as :class:`SSHTransportError` (the
        caller records the classification).
        """
        if record.external_id is None:
            raise ComputeJobRecordError(
                f"job record {record.job_id!r} is running but carries no"
                " external Slurm job id; the record is inconsistent"
            )
        external_id = record.external_id
        active = self._execute(
            lambda: self._transport.run_command(
                RemoteCommand(
                    (
                        "squeue",
                        "--noheader",
                        "--jobs",
                        str(external_id),
                        "--format",
                        "%T",
                    )
                )
            )
        )
        token = _first_token(active.stdout)
        if token is not None:
            decision = normalize_scheduler_state(token)
            return SlurmSchedulerProbe(
                state=decision.state,
                scheduler_state=decision.scheduler_state,
                unknown=decision.unknown,
            )
        accounting = self._execute(
            lambda: self._transport.run_command(
                RemoteCommand(
                    (
                        "sacct",
                        "--noheader",
                        "--parsable",
                        "--allocations",
                        "--jobs",
                        str(external_id),
                        "--format",
                        "State,ExitCode",
                    )
                )
            )
        )
        for line in accounting.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split("|")
            token = _first_token(fields[0]) if fields else None
            exit_code = _parse_exit_code(fields[1]) if len(fields) > 1 else None
            if token is not None:
                decision = normalize_scheduler_state(token)
                return SlurmSchedulerProbe(
                    state=decision.state,
                    scheduler_state=token,
                    exit_code=exit_code,
                    unknown=decision.unknown,
                )
        return SlurmSchedulerProbe(state=None)

    def _transition(
        self, record: SlurmJobRecord, probe: SlurmSchedulerProbe, *, now: str
    ) -> SlurmJobRecord:
        """Pure next-state decision from a scheduler observation.

        A running observation leaves the record untouched. A terminal
        observation (COMPLETED/FAILED/CANCELLED) is persisted with the
        raw scheduler state; a failed observation carries
        ``failure_class="job"`` and a stable error (an unrecognized
        scheduler state is a clean remote answer, classified as a
        job-level outcome with the raw state in the error -- never a
        transport failure); an unavailable observation (the job left the
        scheduler's view) is ``completed`` with the stable recovery note
        (collection then verifies the declared outputs independently).
        """
        if probe.state is JobState.RUNNING:
            return record
        if probe.state is JobState.COMPLETED:
            return replace(
                record,
                state=JobState.COMPLETED,
                exit_code=probe.exit_code,
                completed_at=now,
                scheduler_state=probe.scheduler_state,
                failure_class=None,
                error=None,
            )
        if probe.state is JobState.FAILED:
            token = probe.scheduler_state or "UNKNOWN"
            message = (
                f"unrecognized scheduler state {token!r}"
                if probe.unknown
                else f"slurm job failed: scheduler state {token!r}"
            )
            return replace(
                record,
                state=JobState.FAILED,
                exit_code=probe.exit_code,
                completed_at=now,
                scheduler_state=token,
                failure_class=FAILURE_CLASS_JOB,
                error=message,
            )
        if probe.state is JobState.CANCELLED:
            return replace(
                record,
                state=JobState.CANCELLED,
                cancelled_at=now,
                scheduler_state=probe.scheduler_state,
                failure_class=None,
                error=None,
            )
        return replace(
            record,
            state=JobState.COMPLETED,
            completed_at=now,
            recovery_note=SLURM_STATE_UNAVAILABLE_NOTE,
            failure_class=None,
            error=None,
        )

    def _probe_and_transition(
        self, record: SlurmJobRecord
    ) -> tuple[SlurmJobRecord, str | None]:
        """Probe a running job's scheduler state, decide the next state,
        persist the decision record and return it with the raw observed
        token (status/resume share this path).

        A healthy probe of a still-running job clears a stale transport
        classification (the record's ``failure_class`` is the *current*
        classification, never history).
        """
        if record.state is not JobState.RUNNING:
            raise ComputeJobStateError(
                f"job {record.job_id!r} is {record.state.value}; expected"
                " running"
            )
        probe = self._probe_scheduler(record)
        updated = self._transition(record, probe, now=self._now())
        if updated is not record:
            self._write_record(updated)
        elif record.failure_class is not None:
            updated = replace(record, failure_class=None, error=None)
            self._write_record(updated)
        return updated, probe.scheduler_state

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

    # -- the ComputeAdapter interface (15-ADAPTER-SPEC.md section 3) -------

    def prepare(self, run_context: RunContext) -> SlurmPreparedJob:
        """Stage the run: create the durable record with the captured
        Modules/environment metadata (no remote contact yet -- the
        remote working directory and the batch script are prepared by
        ``submit``).

        Idempotent for an identical stage; re-staging the same run with
        different content (command/working directory/outputs/modules/
        environment) is rejected (job identity is a pure function of the
        run id). The remote working directory must be an absolute POSIX
        path and every declared output a safe id-bearing remote path
        segment (the FND-M9-G02-01 discipline, validated before any
        command construction).

        Raises:
            TypeError: ``run_context`` is not a ``RunContext``.
            SlurmJobIdentityError: unsafe remote working directory or
                unsafe declared output name.
            ComputeJobStateError: the run's job already left the prepared
                state, or is prepared with different content.
        """
        self._validate_context(run_context)
        job_id = self._job_id_for(run_context)
        record = self._try_read(job_id)
        if record is None:
            record = SlurmJobRecord(
                job_id=job_id,
                run_id=run_context.run_id,
                state=JobState.PREPARED,
                command=run_context.command,
                working_directory=run_context.working_directory,
                outputs=run_context.outputs,
                created_at=self._now(),
                modules=self._modules,
                environment=self._environment,
            )
            self._write_record(record)
        elif record.state is JobState.PREPARED:
            staged = (
                record.command,
                record.working_directory,
                record.outputs,
                record.modules,
                record.environment,
            )
            incoming = (
                run_context.command,
                run_context.working_directory,
                run_context.outputs,
                self._modules,
                self._environment,
            )
            if staged != incoming:
                raise ComputeJobStateError(
                    f"job {job_id!r} is already prepared with a different"
                    " command/working directory/outputs/modules/"
                    "environment; job identity is a pure function of the"
                    " run id, so a prepared job cannot be restaged"
                )
        else:
            raise ComputeJobStateError(
                f"job {job_id!r} is already {record.state.value}; prepare"
                " is only valid before submission"
            )
        return SlurmPreparedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            created_at=record.created_at,
            working_directory=record.working_directory,
            command=record.command,
            outputs=record.outputs,
            modules=record.modules,
            environment=record.environment,
            failure_class=record.failure_class,
        )

    def submit(self, run_context: RunContext) -> SlurmSubmittedJob:
        """Submit the staged job to the Slurm scheduler and return the
        submit record with the persistent job id and the **external
        Slurm job id** parsed from the ``sbatch`` answer (AC-01).

        The batch script is generated from the durable record (command,
        modules, environment), written locally
        (``<state_dir>/scripts/<job_id>.slurm.sh``), the remote working
        directory is created (``mkdir -p``), the script is pushed and
        ``sbatch`` is invoked in it (``--chdir`` + absolute output log;
        the script captures the command's exit status as in the ssh
        adapter). Every step runs under the retry policy (a mid-operation
        drop reconnects and re-runs the pending step -- the allowed
        engineering retry of 11-COMPUTATION-SUBSYSTEM.md section 5). A
        clean remote refusal (mkdir/sbatch failure) or an unparseable
        submission answer is the stable ``SlurmJobLaunchError``
        (job-level, never retried); a permanent transport failure records
        ``failure_class="transport"`` on the durable record and re-raises
        the transport error. A second submit of the same job is rejected.

        Raises:
            TypeError: ``run_context`` is not a ``RunContext``.
            SlurmJobIdentityError: unsafe remote working directory or
                unsafe declared output name.
            ComputeJobNotFoundError: the run was not prepared (call
                ``prepare`` first).
            ComputeJobStateError: the job is not in ``prepared`` state.
            SlurmJobLaunchError: the remote submission was refused or did
                not produce a usable external Slurm job id.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
        """
        self._validate_context(run_context)
        job_id = self._job_id_for(run_context)
        record = self._read_record(job_id)
        if record.state is not JobState.PREPARED:
            raise ComputeJobStateError(
                f"job {job_id!r} is {record.state.value}; submit requires"
                " a prepared job (call prepare(run_context) first)"
            )
        workdir = RemotePath(record.working_directory)
        script_name = f".sr_{job_id}_slurm.sh"
        script_path = self._scripts_dir / f"{job_id}.slurm.sh"
        atomic_write(
            script_path,
            _build_batch_script(
                job_id,
                workdir,
                record.command,
                record.modules,
                record.environment,
            ),
        )
        try:
            prepared_dir = self._execute(
                lambda: self._transport.run_command(
                    RemoteCommand(("mkdir", "-p", "--", workdir.value))
                )
            )
            if prepared_dir.exit_code != 0:
                message = (
                    f"remote launch of job {job_id!r} failed with status"
                    f" {prepared_dir.exit_code}:"
                    f" {prepared_dir.stderr.strip()}"
                )
                self._write_record(
                    replace(
                        record,
                        failure_class=FAILURE_CLASS_JOB,
                        error=message,
                    )
                )
                raise SlurmJobLaunchError(message)
            self._execute(
                lambda: self._transport.push_file(
                    script_path, workdir.join(script_name)
                )
            )
            log_path = workdir.join(f".sr_{job_id}_job.log")
            result = self._execute(
                lambda: self._transport.run_command(
                    RemoteCommand(
                        (
                            "sbatch",
                            "--chdir",
                            workdir.value,
                            "--output",
                            log_path.value,
                            "--",
                            workdir.join(script_name).value,
                        )
                    )
                )
            )
        except SSHTransportError as exc:
            self._classify_transport_failure(record, exc)
            raise
        if result.exit_code != 0:
            message = (
                f"sbatch submission of job {job_id!r} failed with status"
                f" {result.exit_code}: {result.stderr.strip()}"
            )
            self._write_record(
                replace(
                    record,
                    failure_class=FAILURE_CLASS_JOB,
                    error=message,
                )
            )
            raise SlurmJobLaunchError(message)
        external_id = _parse_submitted_job_id(result)
        if external_id is None:
            message = (
                f"sbatch submission of job {job_id!r} did not produce a"
                f" usable Slurm job id (stdout {result.stdout!r}, stderr"
                f" {result.stderr!r})"
            )
            self._write_record(
                replace(
                    record,
                    failure_class=FAILURE_CLASS_JOB,
                    error=message,
                )
            )
            raise SlurmJobLaunchError(message)
        submitted_at = self._now()
        updated = replace(
            record,
            state=JobState.RUNNING,
            external_id=external_id,
            submitted_at=submitted_at,
            failure_class=None,
            error=None,
        )
        self._write_record(updated)
        return SlurmSubmittedJob(
            job_id=updated.job_id,
            run_id=updated.run_id,
            state=updated.state,
            external_id=updated.external_id,
            submitted_at=submitted_at,
            failure_class=updated.failure_class,
        )

    def status(self, job_id: str) -> SlurmJobStatus:
        """Report the job's state from the durable record + scheduler
        observation, carrying the external id and the raw observed state
        (AC-01/AC-02).

        Terminal and prepared states are answered from the durable record
        alone (no remote contact); a ``running`` record is probed through
        ``squeue``/``sacct`` under the retry policy and the observed
        terminal decision is persisted (once -- subsequent status calls
        answer from the record alone). A failed observation is recorded
        as ``failure_class="job"``; a permanent transport failure records
        ``failure_class="transport"`` and raises the transport error.

        Raises:
            SlurmJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            ComputeJobRecordError: the stored record is corrupt.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        observed: str | None = None
        if record.state is JobState.RUNNING:
            try:
                record, observed = self._probe_and_transition(record)
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
        return SlurmJobStatus(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            external_id=record.external_id,
            scheduler_state=(
                record.scheduler_state
                if record.scheduler_state is not None
                else observed
            ),
            exit_code=record.exit_code,
            error=record.error,
            failure_class=record.failure_class,
            collected_at=record.collected_at,
            recovery_note=record.recovery_note,
        )

    def collect(self, job_id: str) -> SlurmCollectedJob:
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
            SlurmJobIdentityError: invalid ``job_id``.
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
            return SlurmCollectedJob(
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
                    "backend": SLURM_BACKEND_NAME,
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
        return SlurmCollectedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            collected_at=collected_at,
            artifact_ids=artifact_ids,
            artifacts=artifacts,
            failure_class=updated.failure_class,
        )

    def cancel(self, job_id: str) -> SlurmCancelledJob:
        """Cancel a prepared or running job (AC-01).

        A running job is cancelled with ``scancel <external_id>`` under
        the retry policy and the scheduler's reaction is observed once:
        when the job already left the queue (or reports a terminal
        state), the cancellation is confirmed; when it still shows an
        active state at the moment of observation, the stable
        ``TERMINATE_PENDING_NOTE`` is recorded. A prepared job is
        cancelled without any remote contact. Cancelling a terminal job
        is rejected.

        Raises:
            SlurmJobIdentityError: invalid ``job_id``.
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
            if record.external_id is None:
                raise ComputeJobRecordError(
                    f"job record {record.job_id!r} is running but carries"
                    " no external Slurm job id; the record is inconsistent"
                )
            try:
                self._execute(
                    lambda: self._transport.run_command(
                        RemoteCommand(("scancel", str(record.external_id)))
                    )
                )
                probe = self._probe_scheduler(record)
                if probe.state is None or probe.state is JobState.RUNNING:
                    updated = replace(
                        updated, recovery_note=TERMINATE_PENDING_NOTE
                    )
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
        self._write_record(updated)
        return SlurmCancelledJob(
            job_id=updated.job_id,
            run_id=updated.run_id,
            state=JobState.CANCELLED,
            external_id=updated.external_id,
            exit_code=updated.exit_code,
            cancelled_at=cancelled_at,
            failure_class=updated.failure_class,
        )

    def resume(self, job_id: str) -> SlurmResumedJob:
        """Re-attach to an existing durable job record (AC-01/AC-02).

        The record is re-hydrated from disk (the recovery discipline of
        M1); a ``running`` record's scheduler state is probed under the
        retry policy and the observed terminal decision is persisted.
        Prepared and terminal records are returned as they are -- resume
        never depends on the session object that submitted the job.

        Raises:
            SlurmJobIdentityError: invalid ``job_id``.
            ComputeJobNotFoundError: no durable record for the job.
            SSHTransportError: a permanent transport failure (recorded on
                the durable record as ``failure_class="transport"``).
            ComputeJobRecordError: the stored record is corrupt.
        """
        self._check_job_id(job_id)
        record = self._read_record(job_id)
        if record.state is JobState.RUNNING:
            try:
                record, _ = self._probe_and_transition(record)
            except SSHTransportError as exc:
                self._classify_transport_failure(record, exc)
                raise
        return SlurmResumedJob(
            job_id=record.job_id,
            run_id=record.run_id,
            state=record.state,
            external_id=record.external_id,
            exit_code=record.exit_code,
            failure_class=record.failure_class,
        )
