"""Deterministic Git initialization and scientific audit commits (DEV-M3-G01).

Implements the Git side of the M3 audit architecture: project state lives
in a Git repository whose history records meaningful scientific/governance
checkpoints -- plan freeze, goal contract revision, analysis protocol
revision, ... -- and never operational noise
(``14-STATE-GIT-ARTIFACTS.md`` SS5).

Public API
----------
* ``init_project_repo`` -- deterministic ``git init`` (fixed branch name,
  optional repository-local identity config).
* ``commit_checkpoint`` -- the single audit commit helper, parameterized
  by checkpoint kind (AC-01 plan freeze, AC-02 goal/protocol revision).
  It stages exactly the given files and commits with an explicitly
  supplied author/committer identity and an injectable commit time; the
  message is rendered from a fixed template (``CHECKPOINTS``) that
  includes the object id and version, so identical inputs produce
  identical commits.
* ``map_event_to_audit`` and the pure-data mappings ``CHECKPOINTS`` /
  ``EVENT_TYPE_TO_CHECKPOINT`` / ``HEARTBEAT_EVENT_TYPES`` -- the audit
  event mapping: core ``ProjectEvent`` types resolve to checkpoint kinds
  and message templates. Heartbeat and runtime-polling event types
  resolve to the record-only ``heartbeat`` checkpoint (AC-03): the API
  exposes no commit-creation path for them, so invoking the heartbeat
  path never creates a commit, no matter how often it is called.
* ``count_commits`` / ``current_head`` / ``read_file_at`` -- read-only
  repository queries for tests and audit verification.

Determinism
-----------
* Author and committer are passed explicitly (``AuditIdentity``) and
  injected as ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` environment
  variables; git config is never consulted for identity, so commits are
  identical on any machine regardless of local/global config.
* The commit time is injectable (``commit_time``); testable paths pass a
  fixed value. Without one, ``datetime.now(timezone.utc)`` is used.
* No network access and no prompt dependence: ``GIT_TERMINAL_PROMPT=0``
  is always set, and all git invocations use ``subprocess`` with explicit
  argument lists (never shell strings), so the same commands run on
  Windows and POSIX.

All failures raise the subsystem error hierarchy rooted at ``AuditError``.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Literal, Sequence, TypeAlias

from scientific_reproduction.core.models import ProjectEvent

__all__ = [
    "AUDIT_CHECKPOINT_KINDS",
    "CHECKPOINTS",
    "EVENT_TYPE_TO_CHECKPOINT",
    "HEARTBEAT_EVENT_TYPES",
    "AuditCommitRequest",
    "AuditError",
    "AuditIdentity",
    "CheckpointSpec",
    "CommitRecord",
    "GitAuditError",
    "NotARepositoryError",
    "RecordOnlyResult",
    "UnknownCheckpointError",
    "UnknownEventTypeError",
    "commit_checkpoint",
    "count_commits",
    "current_head",
    "init_project_repo",
    "map_event_to_audit",
    "read_file_at",
    "render_checkpoint_message",
]


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class AuditError(Exception):
    """Base class for all audit subsystem errors."""


class NotARepositoryError(AuditError, ValueError):
    """Raised when an operation requires a Git repository and the target is not one."""


class UnknownCheckpointError(AuditError, ValueError):
    """Raised when a checkpoint kind is not declared in ``CHECKPOINTS``."""


class UnknownEventTypeError(AuditError, ValueError):
    """Raised when a ``ProjectEvent`` type has no audit mapping.

    Governance events must be mapped explicitly: an event type that is
    not in ``EVENT_TYPE_TO_CHECKPOINT`` (and not classified as a
    heartbeat type) would otherwise be silently skipped or silently
    committed, both of which break the audit record.
    """


class GitAuditError(AuditError, RuntimeError):
    """Raised when a git command invoked by this module fails."""


# ---------------------------------------------------------------------------
# Pure data: checkpoint kinds, message templates, event mapping (AC mapping)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckpointSpec:
    """A declared audit checkpoint.

    Attributes:
        kind: stable checkpoint identifier (also the dict key).
        action: ``"commit"`` for checkpoints that produce an auditable
            commit, ``"record_only"`` for checkpoints that must never
            create a commit (heartbeat/runtime polling, AC-03).
        message_template: deterministic commit message template; fields
            ``{object_id}`` and ``{version}`` are filled from the audit
            call. ``None`` for record-only checkpoints.
    """

    kind: str
    action: Literal["commit", "record_only"]
    message_template: str | None


#: Declared audit checkpoints: checkpoint kind -> spec. The commit kinds
#: mirror the governance checkpoint list of 14-STATE-GIT-ARTIFACTS.md
#: SS5 ("Plan v1 frozen", "Goal contract revision", ...); the message
#: templates follow the same plain scientific-governance style.
CHECKPOINTS: dict[str, CheckpointSpec] = {
    "project.initialized": CheckpointSpec(
        kind="project.initialized",
        action="commit",
        message_template="project initialized",
    ),
    "plan.freeze": CheckpointSpec(
        kind="plan.freeze",
        action="commit",
        message_template="plan {object_id} version {version} frozen",
    ),
    "goal.revision": CheckpointSpec(
        kind="goal.revision",
        action="commit",
        message_template="goal contract {object_id} revised to version {version}",
    ),
    "protocol.revision": CheckpointSpec(
        kind="protocol.revision",
        action="commit",
        message_template="analysis protocol {object_id} revised to version {version}",
    ),
    "inventory.audit.passed": CheckpointSpec(
        kind="inventory.audit.passed",
        action="commit",
        message_template="inventory audit passed",
    ),
    "recovery.created": CheckpointSpec(
        kind="recovery.created",
        action="commit",
        message_template="recovery plan created",
    ),
    "requirement.closed": CheckpointSpec(
        kind="requirement.closed",
        action="commit",
        message_template="requirement {object_id} closed",
    ),
    "project.outcome": CheckpointSpec(
        kind="project.outcome",
        action="commit",
        message_template="project final outcome recorded",
    ),
    # AC-03: heartbeats and runtime polling are governance *noise*; the
    # checkpoint exists only so the mapping can resolve them to an
    # explicit, documented record-only outcome -- never a commit.
    "heartbeat": CheckpointSpec(
        kind="heartbeat",
        action="record_only",
        message_template=None,
    ),
}

#: The set of checkpoint kinds that produce auditable commits.
AUDIT_CHECKPOINT_KINDS: frozenset[str] = frozenset(
    kind for kind, spec in CHECKPOINTS.items() if spec.action == "commit"
)

#: Mapping from core ``ProjectEvent.event_type`` values (free-form
#: strings, cf. ``schemas/event.schema.yaml``) to checkpoint kinds.
#: Event types not listed here raise ``UnknownEventTypeError`` so that a
#: new governance event can never silently bypass the audit mapping.
EVENT_TYPE_TO_CHECKPOINT: dict[str, str] = {
    "project.initialized": "project.initialized",
    "plan.frozen": "plan.freeze",
    "goal.revised": "goal.revision",
    "protocol.revised": "protocol.revision",
    "inventory.audit.passed": "inventory.audit.passed",
    "recovery.created": "recovery.created",
    "requirement.closed": "requirement.closed",
    "project.outcome.recorded": "project.outcome",
    # Record-only event types (AC-03): runtime polling / heartbeat noise.
    "heartbeat": "heartbeat",
    "run.heartbeat": "heartbeat",
    "lease.heartbeat": "heartbeat",
    "runtime.poll": "heartbeat",
    "run.polled": "heartbeat",
    "worker.heartbeat": "heartbeat",
}

#: Core event types that are runtime/heartbeat noise. They map to the
#: record-only ``heartbeat`` checkpoint: recorded in the event log, never
#: committed to Git (AC-03).
HEARTBEAT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "heartbeat",
        "run.heartbeat",
        "lease.heartbeat",
        "runtime.poll",
        "run.polled",
        "worker.heartbeat",
    }
)


@dataclass(frozen=True)
class AuditIdentity:
    """Deterministic author/committer identity for audit commits."""

    name: str
    email: str


@dataclass(frozen=True)
class CommitRecord:
    """The result of an audit commit.

    Attributes:
        commit_sha: the full SHA-1 of the created commit (``HEAD`` after
            the commit). Recordable into the governing object's
            ``frozen_commit``-style audit field.
        message: the deterministic commit message that was used.
        kind: the checkpoint kind that produced the commit.
        object_id: the governed object id (plan id, goal id, ...).
        version: the governed object version, if any.
    """

    commit_sha: str
    message: str
    kind: str
    object_id: str | None
    version: str | None


@dataclass(frozen=True)
class AuditCommitRequest:
    """A resolved audit outcome for a governance event: commit this.

    Attributes:
        kind: the checkpoint kind the event maps to.
        message: the deterministic commit message for the event.
        object_id: the audited object id from the event (if any).
        version: the audited object version from the event payload (if any).
    """

    kind: str
    message: str
    object_id: str | None
    version: str | None


@dataclass(frozen=True)
class RecordOnlyResult:
    """The documented outcome of mapping a heartbeat event to audit.

    Heartbeat/runtime polling must never create a commit (AC-03); this
    result is what the heartbeat path always returns. ``committed`` and
    ``commits_created`` are literal constants -- the result has no
    commit-creation capability at all.
    """

    kind: str = "heartbeat"
    committed: bool = False
    commits_created: int = 0
    note: str = "record only, never commit (AC-03)"


#: Result of ``map_event_to_audit``: either a commit request (governance
#: checkpoints) or a record-only result (heartbeats, AC-03).
EventAuditMapping: TypeAlias = AuditCommitRequest | RecordOnlyResult


# ---------------------------------------------------------------------------
# Checkpoint resolution and message rendering
# ---------------------------------------------------------------------------


def checkpoint_for_kind(kind: str) -> CheckpointSpec:
    """Return the declared spec for ``kind``.

    Raises:
        UnknownCheckpointError: ``kind`` is not declared in ``CHECKPOINTS``.
    """
    spec = CHECKPOINTS.get(kind)
    if spec is None:
        raise UnknownCheckpointError(
            f"unknown audit checkpoint kind {kind!r}; declared kinds:"
            f" {', '.join(sorted(CHECKPOINTS))}"
        )
    return spec


def checkpoint_for_event(event_type: str) -> CheckpointSpec:
    """Return the checkpoint spec that ``event_type`` maps to.

    Raises:
        UnknownEventTypeError: the event type has no audit mapping.
    """
    kind = EVENT_TYPE_TO_CHECKPOINT.get(event_type)
    if kind is None:
        raise UnknownEventTypeError(
            f"event type {event_type!r} has no audit mapping; declare it in"
            " EVENT_TYPE_TO_CHECKPOINT or classify it as a heartbeat type"
        )
    return checkpoint_for_kind(kind)


def render_checkpoint_message(
    kind: str,
    *,
    object_id: str | None = None,
    version: str | None = None,
) -> str:
    """Render the deterministic commit message for ``kind``.

    The template is the checkpoint's declared ``message_template`` with
    ``{object_id}`` / ``{version}`` filled in. Every field the template
    declares must be supplied: a message is never rendered with a
    ``None`` placeholder, so identical inputs always yield identical
    messages.

    Args:
        kind: a commit checkpoint kind declared in ``CHECKPOINTS``.
        object_id: the governed object id (plan id, goal id, ...).
        version: the governed object version.

    Raises:
        UnknownCheckpointError: ``kind`` is not declared.
        ValueError: ``kind`` is record-only (has no message), or the
            template declares a field that was not supplied.
    """
    spec = checkpoint_for_kind(kind)
    if spec.message_template is None:
        raise ValueError(
            f"checkpoint kind {kind!r} is record-only and has no commit message"
        )
    template = spec.message_template
    declared = {name for _, name, _, _ in Formatter().parse(template) if name}
    values: dict[str, Any] = {"object_id": object_id, "version": version}
    missing = sorted(name for name in declared if values[name] is None)
    if missing:
        raise ValueError(
            f"checkpoint {kind!r} message template {template!r} requires"
            f" field(s): {', '.join(missing)}"
        )
    return template.format(**values)


# ---------------------------------------------------------------------------
# Repository initialization
# ---------------------------------------------------------------------------


def init_project_repo(
    path: str | Path,
    *,
    branch: str = "main",
    identity: AuditIdentity | None = None,
) -> Path:
    """Initialize a Git repository at ``path`` (deterministic, idempotent).

    Creates the directory (with parents) if needed and runs
    ``git init -b <branch>`` -- ``main`` by default, the integration
    branch. When ``identity`` is
    given it is written into the repository-local config so plain git
    commands run inside the repo also use it; the audit commit helper
    itself never relies on that config (it injects identity explicitly).

    ``git init`` is safe to rerun, so initializing an already-initialized
    repository is a no-op.

    Args:
        path: directory to initialize (created when missing).
        branch: initial branch name.
        identity: optional identity stored in repository-local config.

    Returns:
        The resolved repository root.

    Raises:
        GitAuditError: git init failed.
    """
    repo_root = Path(path).resolve()
    repo_root.mkdir(parents=True, exist_ok=True)
    _run_git(repo_root, ["init", "-b", branch])
    if identity is not None:
        _validate_identity(identity)
        _run_git(repo_root, ["config", "user.name", identity.name])
        _run_git(repo_root, ["config", "user.email", identity.email])
    return repo_root


# ---------------------------------------------------------------------------
# The audit commit helper (AC-01, AC-02)
# ---------------------------------------------------------------------------


def commit_checkpoint(
    repo_path: str | Path,
    *,
    kind: str,
    object_id: str | None = None,
    version: str | None = None,
    files: Sequence[str | Path] = (),
    identity: AuditIdentity,
    committer: AuditIdentity | None = None,
    commit_time: datetime | None = None,
) -> CommitRecord:
    """Create one auditable checkpoint commit (AC-01 plan freeze, AC-02 revisions).

    Stages exactly the given ``files`` and commits them with the
    explicitly supplied author/committer identity and the deterministic
    message rendered by ``render_checkpoint_message``. Commit time is
    injectable for determinism (testable paths pass a fixed value;
    defaults to ``datetime.now(timezone.utc)``).

    Args:
        repo_path: the repository root (must already be a Git repository,
            e.g. created by ``init_project_repo``).
        kind: a *commit* checkpoint kind declared in ``CHECKPOINTS``.
        object_id: the governed object id (plan id, goal id, ...).
        version: the governed object version.
        files: the files to stage and commit, as absolute or relative
            paths inside the repository.
        identity: the author identity for the commit.
        committer: the committer identity; defaults to ``identity``.
        commit_time: timezone-aware commit timestamp (author and
            committer dates); defaults to now(UTC).

    Returns:
        A ``CommitRecord`` with the created commit's SHA and message.

    Raises:
        TypeError: ``identity``/``committer`` are not ``AuditIdentity``,
            or a supplied id/version is not a string.
        ValueError: ``kind`` is record-only (heartbeat -- never commits,
            AC-03), ``files`` is empty or contains a path outside the
            repository or a non-existent file, the message template
            requires an unsupplied field, or ``commit_time`` is naive.
        NotARepositoryError: ``repo_path`` is not a Git repository.
        GitAuditError: a git command failed (e.g. nothing to commit).
    """
    repo_root = Path(repo_path).resolve()
    _require_repo(repo_root)
    spec = checkpoint_for_kind(kind)
    if spec.action == "record_only":
        raise ValueError(
            f"checkpoint kind {kind!r} is record-only (AC-03): heartbeat and"
            " runtime-polling checkpoints never create commits"
        )
    if object_id is not None and not isinstance(object_id, str):
        raise TypeError(
            f"object_id must be a str, got {type(object_id).__name__}"
        )
    if version is not None and not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    _validate_identity(identity)
    if committer is not None:
        _validate_identity(committer)
    if not files:
        raise ValueError("commit_checkpoint requires at least one file to stage")
    if commit_time is None:
        commit_time = datetime.now(timezone.utc)
    if commit_time.tzinfo is None:
        raise ValueError("commit_time must be timezone-aware")

    message = render_checkpoint_message(spec.kind, object_id=object_id, version=version)
    staged = [_stage_path(repo_root, file) for file in files]
    git_date = _git_date(commit_time)
    effective_committer = committer if committer is not None else identity
    commit_env = {
        "GIT_AUTHOR_NAME": identity.name,
        "GIT_AUTHOR_EMAIL": identity.email,
        "GIT_COMMITTER_NAME": effective_committer.name,
        "GIT_COMMITTER_EMAIL": effective_committer.email,
        "GIT_AUTHOR_DATE": git_date,
        "GIT_COMMITTER_DATE": git_date,
    }
    _run_git(repo_root, ["add", "--", *staged])
    _run_git(repo_root, ["commit", "-m", message], env=commit_env)
    head = current_head(repo_root)
    if head is None:
        raise GitAuditError(
            "commit was reported but no HEAD exists; the repository is not"
            " in a committed state"
        )
    return CommitRecord(
        commit_sha=head,
        message=message,
        kind=spec.kind,
        object_id=object_id,
        version=version,
    )


# ---------------------------------------------------------------------------
# Audit event mapping (AC-03 heartbeat exclusion)
# ---------------------------------------------------------------------------


def map_event_to_audit(event: ProjectEvent) -> EventAuditMapping:
    """Resolve a recorded ``ProjectEvent`` to its audit outcome.

    Governance event types (``plan.frozen``, ``goal.revised``, ...)
    resolve to an ``AuditCommitRequest`` carrying the deterministic
    commit message; the commit itself is created through
    ``commit_checkpoint`` once the governing files are available.
    Heartbeat/runtime-polling event types resolve to ``RecordOnlyResult``
    -- recorded, never committed (AC-03): this function has no commit
    capability and no repository access at all, so the heartbeat path
    provably creates zero commits however often it is invoked.

    Args:
        event: the recorded ``ProjectEvent`` to map.

    Raises:
        TypeError: ``event`` is not a ``ProjectEvent``.
        UnknownEventTypeError: the event type has no audit mapping.
        ValueError: the event lacks a field its checkpoint's message
            template requires (e.g. a ``payload["version"]`` string for a
            revision checkpoint), or the version payload is not a string.
    """
    if not isinstance(event, ProjectEvent):
        raise TypeError(
            f"map_event_to_audit expects a ProjectEvent, got {type(event).__name__}"
        )
    spec = checkpoint_for_event(event.event_type)
    if spec.action == "record_only":
        return RecordOnlyResult(kind=spec.kind)
    version = event.payload.get("version")
    if version is not None and not isinstance(version, str):
        raise ValueError(
            "event payload 'version' must be a string for commit checkpoints,"
            f" got {type(version).__name__}"
        )
    message = render_checkpoint_message(
        spec.kind, object_id=event.object_id, version=version
    )
    return AuditCommitRequest(
        kind=spec.kind,
        message=message,
        object_id=event.object_id,
        version=version,
    )


# ---------------------------------------------------------------------------
# Read-only repository queries
# ---------------------------------------------------------------------------


def count_commits(repo_path: str | Path) -> int:
    """Return the number of commits on ``HEAD`` (0 for a fresh repository).

    Raises:
        NotARepositoryError: ``repo_path`` is not a Git repository.
        GitAuditError: git itself failed (not merely "no commits yet").
    """
    repo_root = Path(repo_path).resolve()
    _require_repo(repo_root)
    process = _run_git(repo_root, ["rev-list", "--count", "HEAD"], check=False)
    if process.returncode != 0:
        # No HEAD yet: a freshly initialized repository has zero commits.
        return 0
    return int(process.stdout.strip())


def current_head(repo_path: str | Path) -> str | None:
    """Return the full SHA-1 of ``HEAD``, or None for an empty repository.

    Raises:
        NotARepositoryError: ``repo_path`` is not a Git repository.
    """
    repo_root = Path(repo_path).resolve()
    _require_repo(repo_root)
    process = _run_git(
        repo_root,
        ["rev-parse", "--verify", "--quiet", "HEAD"],
        check=False,
    )
    if process.returncode != 0:
        return None
    return process.stdout.strip()


def read_file_at(
    repo_path: str | Path,
    rel_path: str | Path,
    *,
    ref: str = "HEAD",
) -> str:
    """Return the content of ``rel_path`` as recorded at ``ref`` (utf-8).

    The content comes from the git object store (``git show``), not from
    the working copy, so it is exactly what the audit commit recorded.

    Raises:
        NotARepositoryError: ``repo_path`` is not a Git repository.
        GitAuditError: ``ref`` or the path does not exist in git.
    """
    repo_root = Path(repo_path).resolve()
    _require_repo(repo_root)
    process = _run_git(repo_root, ["show", f"{ref}:{_to_posix(rel_path)}"])
    return process.stdout


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_git(
    repo_root: Path,
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <repo_root> <args>`` with explicit args and no prompts."""
    full_env = {**os.environ, **(env or {})}
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    process = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        env=full_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and process.returncode != 0:
        raise GitAuditError(
            f"git {' '.join(args)} failed with exit code {process.returncode}:"
            f" {process.stderr.strip()}"
        )
    return process


def _require_repo(repo_root: Path) -> None:
    """Raise ``NotARepositoryError`` unless ``repo_root`` has a .git dir."""
    if not (repo_root / ".git").is_dir():
        raise NotARepositoryError(
            f"{repo_root} is not a Git repository (no .git directory);"
            " initialize it with init_project_repo first"
        )


def _validate_identity(identity: AuditIdentity) -> None:
    if not isinstance(identity, AuditIdentity):
        raise TypeError(
            f"identity must be an AuditIdentity, got {type(identity).__name__}"
        )
    if not identity.name or not identity.email:
        raise ValueError("AuditIdentity name and email must be non-empty")


def _stage_path(repo_root: Path, file: str | Path) -> str:
    """Validate ``file`` and return its repository-relative POSIX path."""
    resolved = Path(file).resolve()
    try:
        rel = resolved.relative_to(repo_root)
    except ValueError:
        raise ValueError(
            f"file {resolved} is outside the repository {repo_root}"
        ) from None
    if not resolved.is_file():
        raise ValueError(f"file {resolved} does not exist")
    return rel.as_posix()


def _to_posix(path: str | Path) -> str:
    return Path(path).as_posix()


def _git_date(commit_time: datetime) -> str:
    """Format a timezone-aware datetime as git's ISO-8601 UTC date."""
    return commit_time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
