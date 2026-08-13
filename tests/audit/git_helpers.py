"""Shared test helpers for the audit subsystem (DEV-M3-G01).

``IDENTITY`` / ``COMMIT_TIME`` pin every deterministic input the audit
commit helper takes, so the tests exercise the deterministic path: same
inputs in, same message and tree out. ``make_repo`` initializes a fresh
repository the way the production API is used. The model builders produce
frozen ``Plan`` / ``GoalContract`` / ``AnalysisProtocolOrResult``
instances with the frozen-schema required fields.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from scientific_reproduction.audit.git import AuditIdentity, init_project_repo
from scientific_reproduction.core.models import (
    AnalysisKind,
    AnalysisProtocolOrResult,
    AuditStatus,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    Plan,
    PlanInventoryAudit,
    PlanStatus,
    PrimaryOrExploratory,
)

#: Deterministic author/committer identity used by every audit test.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed commit time: tests never depend on the wall clock.
COMMIT_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_repo(path: Path) -> Path:
    """Initialize a fresh project repository with the pinned identity."""
    return init_project_repo(path, identity=IDENTITY)


def frozen_plan(
    plan_id: str = "PLAN-001", version: str = "v1"
) -> Plan:
    """A frozen plan with the frozen-schema required fields."""
    return Plan(
        plan_id=plan_id,
        version=version,
        status=PlanStatus.FROZEN,
        inventory_audit=PlanInventoryAudit(
            formally_reported_items=5,
            mapped_items=5,
            unmapped_items=0,
            ambiguous_items=0,
            coverage=1.0,
            status=AuditStatus.PASS,
        ),
        goal_ids=["GOAL-001"],
        requirement_ids=["REQ-001"],
        frozen_at="2026-01-01T00:00:00Z",
    )


def revised_goal(goal_id: str = "GOAL-001", version: str = "v2") -> GoalContract:
    """A revised (frozen, new version) goal contract."""
    return GoalContract(
        goal_id=goal_id,
        title="single-component C3H6 isotherm at 298 K",
        unit_process_type="adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="measure single-component C3H6 isotherm at 298 K",
        requirement_ids=["REQ-001"],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref="ACC-001", frozen=True),
        analysis_protocol_ref="ANL-001",
        replication=GoalReplication(
            independent_required=True, planned_n_policy="n>=1"
        ),
        version=version,
        frozen=True,
    )


def revised_protocol(
    analysis_id: str = "ANL-001", version: str = "v3"
) -> AnalysisProtocolOrResult:
    """A revised (frozen, new version) analysis protocol."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=version,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        frozen=True,
    )


def show_commit(repo: Path, fmt: str) -> str:
    """Return ``git show -s --format=<fmt> HEAD`` output (single line)."""
    process = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", f"--format={fmt}", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return process.stdout.strip()
