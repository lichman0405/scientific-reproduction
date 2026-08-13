"""Tests for the audit event mapping and the heartbeat exclusion (DEV-M3-G01).

Covers the audit event mapping deliverable (pure-data mappings from core
``ProjectEvent`` types to checkpoint kinds and commit message templates)
and AC-03: the heartbeat/runtime-polling path is explicitly OUT -- it maps
to a documented record-only result and provably creates zero commits, even
under repeated polling.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git_helpers import COMMIT_TIME, IDENTITY, make_repo

from scientific_reproduction.audit.git import (
    AUDIT_CHECKPOINT_KINDS,
    CHECKPOINTS,
    EVENT_TYPE_TO_CHECKPOINT,
    HEARTBEAT_EVENT_TYPES,
    AuditCommitRequest,
    AuditError,
    RecordOnlyResult,
    UnknownEventTypeError,
    commit_checkpoint,
    count_commits,
    map_event_to_audit,
    render_checkpoint_message,
)
from scientific_reproduction.core.models import ProjectEvent


def heartbeat_event(event_type: str = "heartbeat") -> ProjectEvent:
    return ProjectEvent(
        event_id=f"EV-{event_type}",
        timestamp="2026-01-01T00:00:00Z",
        actor="monitor",
        event_type=event_type,
    )


def governance_event(
    event_type: str,
    *,
    object_id: str | None = None,
    version: str | None = None,
) -> ProjectEvent:
    payload = {"version": version} if version is not None else {}
    return ProjectEvent(
        event_id=f"EV-{event_type}",
        timestamp="2026-01-01T00:00:00Z",
        actor="supervisor",
        event_type=event_type,
        object_id=object_id,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# The pure-data mappings (audit event mapping deliverable)
# ---------------------------------------------------------------------------


def test_checkpoint_kinds_are_documented_pure_data() -> None:
    expected_kinds = {
        "project.initialized",
        "plan.freeze",
        "goal.revision",
        "protocol.revision",
        "inventory.audit.passed",
        "recovery.created",
        "requirement.closed",
        "project.outcome",
        "heartbeat",
    }
    assert set(CHECKPOINTS) == expected_kinds
    for kind, spec in CHECKPOINTS.items():
        assert spec.kind == kind
        assert spec.action in ("commit", "record_only")
        if spec.action == "commit":
            assert spec.message_template is not None
        else:
            assert spec.message_template is None
    # The checkpoint set mirrors the governance checkpoint list of
    # 14-STATE-GIT-ARTIFACTS.md SS5 (plan freeze, goal contract revision,
    # acceptance/analysis-protocol revision, recovery, requirement
    # closure, final outcome) -- and nothing for heartbeats but the
    # record-only marker.
    assert AUDIT_CHECKPOINT_KINDS == expected_kinds - {"heartbeat"}


def test_event_types_map_to_checkpoint_kinds() -> None:
    assert EVENT_TYPE_TO_CHECKPOINT["plan.frozen"] == "plan.freeze"
    assert EVENT_TYPE_TO_CHECKPOINT["goal.revised"] == "goal.revision"
    assert EVENT_TYPE_TO_CHECKPOINT["protocol.revised"] == "protocol.revision"
    assert (
        EVENT_TYPE_TO_CHECKPOINT["inventory.audit.passed"]
        == "inventory.audit.passed"
    )
    assert EVENT_TYPE_TO_CHECKPOINT["project.initialized"] == "project.initialized"
    assert EVENT_TYPE_TO_CHECKPOINT["recovery.created"] == "recovery.created"
    assert EVENT_TYPE_TO_CHECKPOINT["requirement.closed"] == "requirement.closed"
    assert (
        EVENT_TYPE_TO_CHECKPOINT["project.outcome.recorded"] == "project.outcome"
    )


def test_every_event_type_resolves_to_a_declared_checkpoint() -> None:
    for event_type, kind in EVENT_TYPE_TO_CHECKPOINT.items():
        assert kind in CHECKPOINTS, f"{event_type} -> unknown kind {kind!r}"


def test_heartbeat_event_types_are_documented_and_record_only() -> None:
    assert HEARTBEAT_EVENT_TYPES == {
        "heartbeat",
        "run.heartbeat",
        "lease.heartbeat",
        "runtime.poll",
        "run.polled",
        "worker.heartbeat",
    }
    for event_type in sorted(HEARTBEAT_EVENT_TYPES):
        assert EVENT_TYPE_TO_CHECKPOINT[event_type] == "heartbeat"
        assert CHECKPOINTS["heartbeat"].action == "record_only"
    # Heartbeat types never appear among commit-producing checkpoints.
    assert HEARTBEAT_EVENT_TYPES.isdisjoint(
        {et for et, k in EVENT_TYPE_TO_CHECKPOINT.items() if k in AUDIT_CHECKPOINT_KINDS}
    )


def test_unknown_event_type_raises_loudly() -> None:
    with pytest.raises(UnknownEventTypeError, match="no audit mapping"):
        map_event_to_audit(governance_event("mystery.event"))


def test_map_event_to_audit_requires_a_project_event() -> None:
    with pytest.raises(TypeError, match="ProjectEvent"):
        map_event_to_audit("plan.frozen")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Message rendering (deterministic templates)
# ---------------------------------------------------------------------------


def test_plan_freeze_message_template() -> None:
    assert (
        render_checkpoint_message(
            "plan.freeze", object_id="PLAN-001", version="v1"
        )
        == "plan PLAN-001 version v1 frozen"
    )


def test_revision_message_templates() -> None:
    assert (
        render_checkpoint_message(
            "goal.revision", object_id="GOAL-001", version="v2"
        )
        == "goal contract GOAL-001 revised to version v2"
    )
    assert (
        render_checkpoint_message(
            "protocol.revision", object_id="ANL-001", version="v3"
        )
        == "analysis protocol ANL-001 revised to version v3"
    )
    assert (
        render_checkpoint_message("requirement.closed", object_id="REQ-001")
        == "requirement REQ-001 closed"
    )


def test_placeholder_free_templates_ignore_supplied_fields() -> None:
    assert render_checkpoint_message("project.initialized") == "project initialized"
    assert (
        render_checkpoint_message(
            "project.initialized", object_id="whatever", version="v9"
        )
        == "project initialized"
    )


def test_message_rendering_requires_declared_fields() -> None:
    with pytest.raises(ValueError, match="requires field\\(s\\): version"):
        render_checkpoint_message("plan.freeze", object_id="PLAN-001")
    with pytest.raises(ValueError, match="requires field\\(s\\): object_id"):
        render_checkpoint_message("requirement.closed", version="v1")


def test_message_rendering_rejects_record_only_kinds() -> None:
    with pytest.raises(ValueError, match="record-only"):
        render_checkpoint_message("heartbeat")
    with pytest.raises(AuditError, match="unknown audit checkpoint kind"):
        render_checkpoint_message("no.such.checkpoint")


# ---------------------------------------------------------------------------
# Event mapping produces commit requests for governance events
# ---------------------------------------------------------------------------


def test_plan_frozen_event_maps_to_commit_request() -> None:
    result = map_event_to_audit(
        governance_event("plan.frozen", object_id="PLAN-001", version="v1")
    )
    assert isinstance(result, AuditCommitRequest)
    assert result.kind == "plan.freeze"
    assert result.message == "plan PLAN-001 version v1 frozen"
    assert result.object_id == "PLAN-001"
    assert result.version == "v1"


def test_goal_revision_event_maps_to_commit_request() -> None:
    result = map_event_to_audit(
        governance_event("goal.revised", object_id="GOAL-001", version="v2")
    )
    assert isinstance(result, AuditCommitRequest)
    assert result.kind == "goal.revision"
    assert result.message == "goal contract GOAL-001 revised to version v2"


def test_revision_event_without_version_raises_loudly() -> None:
    with pytest.raises(ValueError, match="requires field\\(s\\): version"):
        map_event_to_audit(governance_event("goal.revised", object_id="GOAL-001"))


# ---------------------------------------------------------------------------
# AC-03: heartbeat/runtime polling never creates Git commits
# ---------------------------------------------------------------------------


def test_heartbeat_mapping_is_documented_noop() -> None:
    result = map_event_to_audit(heartbeat_event())
    assert isinstance(result, RecordOnlyResult)
    assert result.kind == "heartbeat"
    assert result.committed is False
    assert result.commits_created == 0
    assert "record only, never commit" in result.note


def test_heartbeat_path_creates_zero_commits_even_under_repeated_polling(
    tmp_path: Path,
) -> None:
    """AC-03: repeated heartbeat/poll invocations leave the commit count
    unchanged -- before any checkpoint exists and after one does."""
    repo = make_repo(tmp_path / "project")
    heartbeat_types = sorted(HEARTBEAT_EVENT_TYPES)

    # Poll long before any checkpoint: still zero commits.
    for i in range(25):
        result = map_event_to_audit(
            heartbeat_event(heartbeat_types[i % len(heartbeat_types)])
        )
        assert result.commits_created == 0
    for event_type in heartbeat_types:
        map_event_to_audit(heartbeat_event(event_type))
    assert count_commits(repo) == 0

    # One governance checkpoint commit.
    plan_file = repo / "plans" / "PLAN-001.json"
    plan_file.parent.mkdir()
    plan_file.write_text("{}", encoding="utf-8")
    commit_checkpoint(
        repo,
        kind="plan.freeze",
        object_id="PLAN-001",
        version="v1",
        files=[plan_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )
    assert count_commits(repo) == 1

    # Keep polling: the commit count must stay unchanged.
    for i in range(25):
        map_event_to_audit(
            heartbeat_event(heartbeat_types[i % len(heartbeat_types)])
        )
        assert count_commits(repo) == 1
