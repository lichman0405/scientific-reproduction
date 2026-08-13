"""Git initialization and scientific audit commits (DEV-M3-G01).

Public surface:

* ``init_project_repo`` -- deterministic ``git init`` for a project
  repository (fixed branch name, optional local identity config);
* ``commit_checkpoint`` -- the single audit commit helper, parameterized
  by checkpoint kind: plan freeze (AC-01), goal/protocol revision (AC-02),
  and any other declared governance checkpoint. Author/committer identity
  is supplied explicitly and the commit message is rendered from a fixed
  template, so identical inputs produce identical commits;
* ``CHECKPOINTS`` / ``EVENT_TYPE_TO_CHECKPOINT`` /
  ``HEARTBEAT_EVENT_TYPES`` / ``map_event_to_audit`` -- the audit event
  mapping: core ``ProjectEvent`` types to checkpoint kinds and commit
  message templates (pure data). Heartbeat and runtime-polling event types
  map to the record-only ``heartbeat`` checkpoint: the API exposes no
  commit-creation path for them, so they never create a commit (AC-03);
* ``count_commits`` / ``current_head`` / ``read_file_at`` -- read-only
  repository queries used by tests and audit verification;
* the error hierarchy rooted at ``AuditError``.
"""

from scientific_reproduction.audit.git import (
    AUDIT_CHECKPOINT_KINDS,
    CHECKPOINTS,
    EVENT_TYPE_TO_CHECKPOINT,
    HEARTBEAT_EVENT_TYPES,
    AuditCommitRequest,
    AuditError,
    AuditIdentity,
    CheckpointSpec,
    CommitRecord,
    GitAuditError,
    NotARepositoryError,
    RecordOnlyResult,
    UnknownCheckpointError,
    UnknownEventTypeError,
    commit_checkpoint,
    count_commits,
    current_head,
    init_project_repo,
    map_event_to_audit,
    read_file_at,
    render_checkpoint_message,
)

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
