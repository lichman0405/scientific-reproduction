"""Tests for Git initialization and audit checkpoint commits (DEV-M3-G01).

Covers AC-01 (plan freeze produces an auditable commit), AC-02 (goal and
protocol revision produce auditable commits through the same helper,
parameterized by checkpoint kind), the execution-phase checkpoints (goal
review, run closure, outcome updates, recovery entry), and the
deterministic design contract: explicit author/committer identity, fixed
message templates, injectable commit time -- no wall clock, no git config,
no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from git_helpers import (
    COMMIT_TIME,
    IDENTITY,
    frozen_plan,
    make_repo,
    revised_goal,
    revised_protocol,
    show_commit,
)

from scientific_reproduction.audit.git import (
    AuditError,
    AuditIdentity,
    GitAuditError,
    NotARepositoryError,
    commit_checkpoint,
    count_commits,
    current_head,
    init_project_repo,
    read_file_at,
)

# ---------------------------------------------------------------------------
# Git project initialization
# ---------------------------------------------------------------------------


def test_init_creates_a_git_repository(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    assert (repo / ".git").is_dir()
    assert current_head(repo) is None
    assert count_commits(repo) == 0


def test_init_creates_missing_directories(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path / "a" / "b" / "c")
    assert (repo / ".git").is_dir()
    assert count_commits(repo) == 0


def test_init_is_idempotent(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    again = init_project_repo(repo, identity=IDENTITY)
    assert again == repo
    assert count_commits(repo) == 0


def test_init_uses_main_branch_by_default(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    assert repo.name == "project"
    head_ref = (repo / ".git" / "HEAD").read_text(encoding="utf-8")
    assert head_ref.strip().endswith("refs/heads/main")


def test_init_writes_identity_into_local_config(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    config = (repo / ".git" / "config").read_text(encoding="utf-8")
    assert "name = Audit Bot" in config
    assert "email = audit@example.org" in config


def test_count_commits_rejects_non_repository(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError):
        count_commits(tmp_path / "not-a-repo")


# ---------------------------------------------------------------------------
# AC-01: plan freeze produces an auditable commit
# ---------------------------------------------------------------------------


def test_plan_freeze_commit_is_auditable(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    plan = frozen_plan()
    plan_file = repo / "plans" / f"{plan.plan_id}.json"
    plan_file.parent.mkdir()
    plan_file.write_text(
        json.dumps(plan.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    record = commit_checkpoint(
        repo,
        kind="plan.freeze",
        object_id=plan.plan_id,
        version=plan.version,
        files=[plan_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    # A commit exists, it is HEAD, and the repository records exactly it.
    assert record.commit_sha == current_head(repo)
    assert count_commits(repo) == 1
    # Deterministic message carrying plan id and version.
    assert record.message == "plan PLAN-001 version v1 frozen"
    assert record.kind == "plan.freeze"
    # The frozen plan content is in the commit tree.
    committed = read_file_at(repo, "plans/PLAN-001.json")
    assert json.loads(committed) == plan.to_dict()
    # Deterministic identity and date, independent of any git config.
    assert show_commit(repo, "%an|%ae|%cn|%ce|%aI") == (
        "Audit Bot|audit@example.org|Audit Bot|audit@example.org|"
        "2026-01-01T00:00:00Z"
    )


def test_plan_freeze_commit_is_deterministic_across_repositories(
    tmp_path: Path,
) -> None:
    first = make_repo(tmp_path / "one")
    second = make_repo(tmp_path / "two")
    messages = []
    for repo in (first, second):
        plan = frozen_plan()
        plan_file = repo / "plans" / "PLAN-001.json"
        plan_file.parent.mkdir()
        plan_file.write_text(
            json.dumps(plan.to_dict(), sort_keys=True), encoding="utf-8"
        )
        record = commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id=plan.plan_id,
            version=plan.version,
            files=[plan_file],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )
        messages.append(record.message)
    assert messages == ["plan PLAN-001 version v1 frozen"] * 2
    # Same identity and same message in both repositories.
    assert show_commit(first, "%an <%ae>|%s") == show_commit(second, "%an <%ae>|%s")


def test_plan_freeze_commits_accumulate_on_sequence(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    for version in ("v1", "v2"):
        plan = frozen_plan(version=version)
        plan_file = repo / "plans" / "PLAN-001.json"
        plan_file.parent.mkdir(exist_ok=True)
        plan_file.write_text(
            json.dumps(plan.to_dict(), sort_keys=True), encoding="utf-8"
        )
        commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id=plan.plan_id,
            version=plan.version,
            files=[plan_file],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )
    assert count_commits(repo) == 2


# ---------------------------------------------------------------------------
# AC-02: goal/protocol revision produces an auditable commit
# ---------------------------------------------------------------------------


def test_goal_revision_commit_is_auditable(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    goal = revised_goal()
    goal_file = repo / "goals" / f"{goal.goal_id}.json"
    goal_file.parent.mkdir()
    goal_file.write_text(
        json.dumps(goal.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    record = commit_checkpoint(
        repo,
        kind="goal.revision",
        object_id=goal.goal_id,
        version=goal.version,
        files=[goal_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    assert record.message == "goal contract GOAL-001 revised to version v2"
    assert record.commit_sha == current_head(repo)
    assert count_commits(repo) == 1
    assert json.loads(read_file_at(repo, "goals/GOAL-001.json")) == goal.to_dict()


def test_protocol_revision_commit_is_auditable(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    protocol = revised_protocol()
    protocol_file = repo / "analysis" / f"{protocol.analysis_id}.json"
    protocol_file.parent.mkdir()
    protocol_file.write_text(
        json.dumps(protocol.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )

    record = commit_checkpoint(
        repo,
        kind="protocol.revision",
        object_id=protocol.analysis_id,
        version=protocol.protocol_version,
        files=[protocol_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    assert (
        record.message
        == "analysis protocol ANL-001 revised to version v3"
    )
    assert record.commit_sha == current_head(repo)
    assert count_commits(repo) == 1
    assert json.loads(read_file_at(repo, "analysis/ANL-001.json")) == (
        protocol.to_dict()
    )


def test_revision_checkpoints_share_the_same_helper(tmp_path: Path) -> None:
    """AC-02: the same commit helper serves every checkpoint kind."""
    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plans" / "PLAN-001.json"
    plan_file.parent.mkdir()
    plan_file.write_text("{}", encoding="utf-8")
    goal_file = repo / "goals" / "GOAL-001.json"
    goal_file.parent.mkdir()
    goal_file.write_text("{}", encoding="utf-8")

    plan_record = commit_checkpoint(
        repo,
        kind="plan.freeze",
        object_id="PLAN-001",
        version="v1",
        files=[plan_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )
    goal_record = commit_checkpoint(
        repo,
        kind="goal.revision",
        object_id="GOAL-001",
        version="v2",
        files=[goal_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    assert count_commits(repo) == 2
    assert plan_record.message == "plan PLAN-001 version v1 frozen"
    assert goal_record.message == "goal contract GOAL-001 revised to version v2"
    assert plan_record.commit_sha != goal_record.commit_sha


# ---------------------------------------------------------------------------
# Execution-phase checkpoints: goal review, run closure, outcome updates,
# recovery entry all commit through the same sanctioned helper
# ---------------------------------------------------------------------------


def test_execution_phase_checkpoints_commit_through_the_helper(
    tmp_path: Path,
) -> None:
    """Run-level governance milestones (goal review, run closure,
    requirement-outcome updates, recovery entry) are commit checkpoints:
    supervisors never need raw git commits for them."""
    repo = make_repo(tmp_path / "project")
    goal_file = repo / "goals" / "GOAL-001.json"
    goal_file.parent.mkdir()
    goal_file.write_text("{}", encoding="utf-8")
    run_file = repo / "runs" / "RUN-COMP-017-01.json"
    run_file.parent.mkdir()
    run_file.write_text("{}", encoding="utf-8")
    req_file = repo / "requirements" / "REQ-001.json"
    req_file.parent.mkdir()
    req_file.write_text("{}", encoding="utf-8")

    review = commit_checkpoint(
        repo,
        kind="goal.reviewed",
        object_id="GOAL-001",
        files=[goal_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )
    closed = commit_checkpoint(
        repo,
        kind="run.closed",
        object_id="RUN-COMP-017-01",
        files=[run_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )
    outcome = commit_checkpoint(
        repo,
        kind="requirement.outcome.updated",
        object_id="REQ-001",
        files=[req_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )
    # The goal state changed again (track switch), so the file differs
    # from the goal-review commit.
    goal_file.write_text('{"track": "RECOVERY"}', encoding="utf-8")
    recovery = commit_checkpoint(
        repo,
        kind="recovery.entry",
        object_id="GOAL-001",
        files=[goal_file],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    assert count_commits(repo) == 4
    assert review.message == "goal contract GOAL-001 reviewed"
    assert closed.message == "run RUN-COMP-017-01 closed"
    assert outcome.message == "requirement REQ-001 outcome updated"
    assert recovery.message == "goal GOAL-001 entered recovery"
    shas = [
        review.commit_sha,
        closed.commit_sha,
        outcome.commit_sha,
        recovery.commit_sha,
    ]
    assert len(set(shas)) == 4
    assert recovery.commit_sha == current_head(repo)


# ---------------------------------------------------------------------------
# Determinism and failure modes of the commit helper
# ---------------------------------------------------------------------------


def test_commit_requires_an_initialized_repository(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError):
        commit_checkpoint(
            tmp_path / "not-a-repo",
            kind="plan.freeze",
            object_id="PLAN-001",
            version="v1",
            files=[tmp_path / "plan.json"],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )


def test_commit_rejects_unknown_checkpoint_kind(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    with pytest.raises(AuditError, match="unknown audit checkpoint kind"):
        commit_checkpoint(
            repo,
            kind="no.such.checkpoint",
            object_id="PLAN-001",
            files=[plan_file],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )
    assert count_commits(repo) == 0


def test_commit_rejects_record_only_kind(tmp_path: Path) -> None:
    """AC-03: the commit helper itself refuses heartbeat checkpoints."""
    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="record-only"):
        commit_checkpoint(
            repo,
            kind="heartbeat",
            object_id="PLAN-001",
            files=[plan_file],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )
    assert count_commits(repo) == 0


def test_commit_rejects_empty_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    with pytest.raises(ValueError, match="at least one file"):
        commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id="PLAN-001",
            version="v1",
            files=[],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )


def test_commit_rejects_files_outside_repository(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the repository"):
        commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id="PLAN-001",
            version="v1",
            files=[outside],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )
    assert count_commits(repo) == 0


def test_commit_rejects_missing_files(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    missing = repo / "never-written.json"
    with pytest.raises(ValueError, match="does not exist"):
        commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id="PLAN-001",
            version="v1",
            files=[missing],
            identity=IDENTITY,
            commit_time=COMMIT_TIME,
        )


def test_commit_rejects_naive_commit_time(tmp_path: Path) -> None:
    from datetime import datetime

    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="timezone-aware"):
        commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id="PLAN-001",
            version="v1",
            files=[plan_file],
            identity=IDENTITY,
            commit_time=datetime(2026, 1, 1),
        )


def test_commit_rejects_invalid_identity_type(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    with pytest.raises(TypeError, match="AuditIdentity"):
        commit_checkpoint(
            repo,
            kind="plan.freeze",
            object_id="PLAN-001",
            version="v1",
            files=[plan_file],
            identity="Audit Bot",  # type: ignore[arg-type]
            commit_time=COMMIT_TIME,
        )


def test_commit_uses_explicit_committer_when_given(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plan.json"
    plan_file.write_text("{}", encoding="utf-8")
    committer = AuditIdentity(name="Committer Bot", email="committer@example.org")
    commit_checkpoint(
        repo,
        kind="plan.freeze",
        object_id="PLAN-001",
        version="v1",
        files=[plan_file],
        identity=IDENTITY,
        committer=committer,
        commit_time=COMMIT_TIME,
    )
    assert show_commit(repo, "%an|%ae|%cn|%ce") == (
        "Audit Bot|audit@example.org|Committer Bot|committer@example.org"
    )


def test_commit_with_nothing_to_commit_raises_git_error(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "project")
    plan_file = repo / "plan.json"
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
    with pytest.raises(GitAuditError):
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
