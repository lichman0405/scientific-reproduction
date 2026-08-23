"""Workspace gate-state wiring tests (issue #144).

The gate-state wiring (``planning/gate_state.py``) is the runtime
consumer of the frozen dependency-gate engines: it reads the registered
Goal contracts (``dependencies``) and the registered Run records from
the real registries, resolves each dependency's ``execution_resolved`` /
``acceptance_resolved`` state through the versioned resolution rule
tables, evaluates the execution/acceptance gates through the frozen pure
evaluators (``core/rules/dependencies.py``) and returns per-Goal
executable/blocked verdicts with the consulted ``matched_rule_id``. The
frozen rule tables are never touched; these tests lock the state-reading
contract (05-GOAL-RUN-SCHEMA.md SS5/SS7, DEV-M2-G02).

Acceptance coverage (issue #144 acceptance criterion 1):

  * AC-01 -- ``test_gate_state_ac01_*``: a workspace with goal A carrying
    a ``hard_gate`` dependency on goal B reports A as execution-blocked
    through the deterministic API while B is unresolved; after B's run
    reaches an accepted result, A reports executable; the consulted
    ``matched_rule_id`` is recorded in the canonical mapping.

Plus the locked-resolution grid (``test_gate_state_resolution_rules_*``:
every Run lifecycle state resolves each axis exactly per the locked
reading -- result-bearing mainline states for execution,
SUBMITTED_FOR_REVIEW/CLOSED for acceptance, CANCELLED/INVALIDATED never),
any-run semantics, the AC-02 soft/informational inertness and AC-03 axis
independence of the frozen engines, unregistered-dependency reporting
(M4-G02), determinism, report shape/versions, TypeError/registry
boundaries and the dispatch-facing ``assert_execution_eligible``.

Every test name contains "gate_state" so
``python -m pytest -q tests/planning -k gate_state`` selects the suite.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    DependencyType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    LifecycleState,
    Run,
    RunType,
)
from scientific_reproduction.planning.gate_state import (
    ACCEPTANCE_RESOLUTION_RULESET_VERSION,
    EXECUTION_RESOLUTION_RULESET_VERSION,
    GATE_STATE_VERSION,
    DependencyResolution,
    GoalExecutionBlockedError,
    assert_execution_eligible,
    evaluate_workspace_gates,
    goal_gate_verdict,
)
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    initialize_project,
)
from scientific_reproduction.planning.plan import (
    GoalNotFoundError,
    register_goal,
)
from scientific_reproduction.workers.run_helpers import (
    RUNS_STATE_DIR,
    register_run,
    transition_run,
)

# ---------------------------------------------------------------------------
# Pins and locked readings (deterministic fixtures, no wall clock)
# ---------------------------------------------------------------------------

#: Deterministic author/committer identity for the initialized project.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects.
DOI = "10.1039/D5TA00771B"

#: Injected actor and recording stamps (no wall clock anywhere).
ACTOR = "worker"
RECORDED_AT = "2026-01-02T00:00:00Z"

#: The locked execution-resolution reading (05-GOAL-RUN-SCHEMA.md SS7):
#: a dependency's execution gate is resolved exactly when the upstream
#: goal has a run in a result-bearing mainline state.
EXECUTION_RESOLVED_STATES = frozenset(
    {
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
    }
)

#: The locked acceptance-resolution reading: a dependency's acceptance
#: gate is resolved exactly when the upstream goal has a run whose
#: evidence has been submitted for review (and remains valid through
#: closure).
ACCEPTANCE_RESOLVED_STATES = frozenset(
    {
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
    }
)

#: The mainline transition chain (state, reason) of the Run lifecycle.
MAINLINE: tuple[tuple[LifecycleState, str], ...] = (
    (LifecycleState.READY, "run queued"),
    (LifecycleState.DISPATCHED, "run dispatched"),
    (LifecycleState.RUNNING_EXTERNAL, "computation started"),
    (LifecycleState.RESULT_AVAILABLE, "raw result produced"),
    (LifecycleState.ANALYZING, "analysis started"),
    (LifecycleState.SUBMITTED_FOR_REVIEW, "result submitted"),
    (LifecycleState.CLOSED, "review passed"),
)

#: Injected transition stamps (one per mainline step).
STAMPS: tuple[str, ...] = tuple(
    f"2026-01-{day:02d}T00:00:00Z" for day in range(3, 10)
)


# ---------------------------------------------------------------------------
# Fixtures (registered through the real registries)
# ---------------------------------------------------------------------------


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_goal(
    goal_id: str,
    *,
    dependencies: tuple[GoalDependency, ...] = (),
    frozen: bool = True,
    version: str = "v1",
) -> GoalContract:
    """Build a schema-valid goal contract (frozen at the formal version)."""
    return GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported isotherm ({goal_id}).",
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the formally reported isotherm dataset.",
        requirement_ids=["REQ-1"],
        dependencies=list(dependencies),
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=frozen),
        analysis_protocol_ref="ANP-1",
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=version,
        frozen=frozen,
    )


def make_run(run_id: str, goal_id: str) -> Run:
    """Build a schema-valid run record for ``goal_id``."""
    return Run(
        run_id=run_id,
        goal_id=goal_id,
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.CREATED,
        goal_version="v1",
        created_at=TIMESTAMP.isoformat(),
    )


def register_hard_gate_chain(root: Path) -> None:
    """Register GOAL-B (upstream, dep-free) and GOAL-A, which hard-gates
    GOAL-B on both axes (the acceptance criterion's goal pair)."""
    register_goal(root, make_goal("GOAL-B"))
    register_goal(
        root,
        make_goal(
            "GOAL-A",
            dependencies=(
                GoalDependency(
                    goal_id="GOAL-B",
                    type=DependencyType.HARD_GATE,
                    execution_gate=True,
                    acceptance_gate=True,
                ),
            ),
        ),
    )


def register_run_at(root: Path, goal_id: str, run_id: str) -> None:
    """Register one CREATED run for ``goal_id`` through the real facade."""
    register_run(
        root,
        make_run(run_id, goal_id),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )


def drive_to(root: Path, run_id: str, to_state: LifecycleState) -> None:
    """Advance ``run_id`` along the mainline until it reaches ``to_state``."""
    for (state, reason), at in zip(MAINLINE, STAMPS):
        transition_run(
            root, run_id, state, actor=ACTOR, reason=reason, at=at
        )
        if state is to_state:
            return
    raise AssertionError(f"{to_state!r} is not a mainline state")


def resolution_of(
    root: Path, goal_id: str, upstream_goal_id: str
) -> DependencyResolution:
    """The DependencyResolution of ``goal_id``'s edge to ``upstream_goal_id``."""
    for resolution in goal_gate_verdict(root, goal_id).resolutions:
        if resolution.dependency.goal_id == upstream_goal_id:
            return resolution
    raise AssertionError(
        f"goal {goal_id!r} declares no dependency on {upstream_goal_id!r}"
    )


# ---------------------------------------------------------------------------
# AC-01: the hard gate blocks until the upstream run is accepted
# ---------------------------------------------------------------------------


def test_gate_state_ac01_hard_gate_blocks_until_upstream_run_accepted(tmp_path):
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    # B unresolved: A's execution and acceptance gates are both BLOCKED,
    # decided by the frozen aggregate rules and recorded with the
    # consulted matched rule ids.
    report = evaluate_workspace_gates(root)
    verdict = report.verdict_for("GOAL-A")
    assert verdict.executable is False
    assert verdict.execution_assessment.outcome.value == "BLOCKED"
    assert verdict.execution_assessment.matched_rule_id == "R-EXEC-G-1"
    assert verdict.execution_blocking_goal_ids == ("GOAL-B",)
    assert verdict.acceptance_allowed is False
    assert verdict.acceptance_assessment.matched_rule_id == "R-ACC-G-1"
    assert verdict.acceptance_blocking_goal_ids == ("GOAL-B",)
    # The resolution trace names the deciding resolution rules and keeps
    # the exact input (registered upstream, empty run profile).
    resolution = verdict.resolutions[0]
    assert resolution.registered is True
    assert resolution.upstream_run_states == ()
    assert resolution.execution_resolved is False
    assert resolution.acceptance_resolved is False
    assert resolution.execution_rule_id == "R-RES-EXEC-2"
    assert resolution.acceptance_rule_id == "R-RES-ACC-2"
    # The workspace report agrees; every registered goal is reported.
    assert report.blocked_goal_ids == ("GOAL-A",)
    assert report.executable_goal_ids == ("GOAL-B",)
    assert goal_gate_verdict(root, "GOAL-A") == verdict
    # The dispatch-facing enforcement raises before anything is written.
    with pytest.raises(GoalExecutionBlockedError) as exc:
        assert_execution_eligible(root, "GOAL-A")
    message = str(exc.value)
    assert "GOAL-A" in message
    assert "R-EXEC-G-1" in message
    assert "GOAL-B" in message
    # After GOAL-B's run is registered and reaches the accepted result
    # (CLOSED), GOAL-A reports executable and acceptance-allowed through
    # the deterministic API.
    register_run_at(root, "GOAL-B", "RUN-B1")
    drive_to(root, "RUN-B1", LifecycleState.CLOSED)
    verdict = goal_gate_verdict(root, "GOAL-A")
    assert verdict.executable is True
    assert verdict.acceptance_allowed is True
    assert verdict.execution_assessment.matched_rule_id == "R-EXEC-G-2"
    assert verdict.acceptance_assessment.matched_rule_id == "R-ACC-G-2"
    resolution = verdict.resolutions[0]
    assert resolution.execution_rule_id == "R-RES-EXEC-1"
    assert resolution.acceptance_rule_id == "R-RES-ACC-1"
    assert resolution.upstream_run_states == (LifecycleState.CLOSED,)
    # The consulted matched rule ids are recorded in the canonical
    # mapping (the /goals gate view).
    report = evaluate_workspace_gates(root)
    assert report.executable_goal_ids == ("GOAL-A", "GOAL-B")
    assert report.blocked_goal_ids == ()
    entry = report.to_dict()["verdicts"][0]
    assert entry["goal_id"] == "GOAL-A"
    assert entry["execution_matched_rule_id"] == "R-EXEC-G-2"
    assert entry["acceptance_matched_rule_id"] == "R-ACC-G-2"


# ---------------------------------------------------------------------------
# The locked resolution grid over the Run lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state", list(LifecycleState), ids=[state.value for state in LifecycleState]
)
def test_gate_state_resolution_rules_cover_the_run_lifecycle(tmp_path, state):
    """Every Run lifecycle state resolves each axis per the locked reading.

    The resolution tables are total (the trailing default always
    matches) and first-match-wins: the deciding decision is the first
    match of the ordered trace, and the aggregate gates follow the
    frozen tables from the resolutions.
    """
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    if state is LifecycleState.CREATED:
        pass  # no upstream run at all: the empty profile
    elif state is LifecycleState.CANCELLED:
        register_run_at(root, "GOAL-B", "RUN-B1")
        transition_run(
            root, "RUN-B1", LifecycleState.CANCELLED,
            actor=ACTOR, reason="hardware failure", at="2026-01-11T00:00:00Z",
        )
    elif state is LifecycleState.INVALIDATED:
        register_run_at(root, "GOAL-B", "RUN-B1")
        drive_to(root, "RUN-B1", LifecycleState.RESULT_AVAILABLE)
        transition_run(
            root, "RUN-B1", LifecycleState.INVALIDATED,
            actor=ACTOR, reason="provenance broken", at="2026-01-11T00:00:00Z",
        )
    else:
        register_run_at(root, "GOAL-B", "RUN-B1")
        drive_to(root, "RUN-B1", state)
    resolution = resolution_of(root, "GOAL-A", "GOAL-B")
    expected_profile = () if state is LifecycleState.CREATED else (state,)
    assert resolution.upstream_run_states == expected_profile
    assert resolution.execution_resolved is (state in EXECUTION_RESOLVED_STATES)
    assert resolution.acceptance_resolved is (
        state in ACCEPTANCE_RESOLVED_STATES
    )
    assert resolution.execution_rule_id == (
        "R-RES-EXEC-1"
        if state in EXECUTION_RESOLVED_STATES
        else "R-RES-EXEC-2"
    )
    assert resolution.acceptance_rule_id == (
        "R-RES-ACC-1"
        if state in ACCEPTANCE_RESOLVED_STATES
        else "R-RES-ACC-2"
    )
    assert resolution.execution_decisions[0].matched is (
        state in EXECUTION_RESOLVED_STATES
    )
    assert resolution.acceptance_decisions[0].matched is (
        state in ACCEPTANCE_RESOLVED_STATES
    )
    verdict = goal_gate_verdict(root, "GOAL-A")
    assert verdict.executable is (state in EXECUTION_RESOLVED_STATES)
    assert verdict.acceptance_allowed is (state in ACCEPTANCE_RESOLVED_STATES)
    assert verdict.execution_assessment.matched_rule_id == (
        "R-EXEC-G-2"
        if state in EXECUTION_RESOLVED_STATES
        else "R-EXEC-G-1"
    )
    assert verdict.acceptance_assessment.matched_rule_id == (
        "R-ACC-G-2"
        if state in ACCEPTANCE_RESOLVED_STATES
        else "R-ACC-G-1"
    )


def test_gate_state_any_qualifying_run_resolves(tmp_path):
    """Any-run semantics: one qualifying upstream run resolves the axis.

    A CANCELLED run never resolves, a RESULT_AVAILABLE run resolves
    execution but not acceptance -- the axes stay independent.
    """
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    register_run_at(root, "GOAL-B", "RUN-B1")
    transition_run(
        root, "RUN-B1", LifecycleState.CANCELLED,
        actor=ACTOR, reason="hardware failure", at="2026-01-11T00:00:00Z",
    )
    register_run_at(root, "GOAL-B", "RUN-B2")
    drive_to(root, "RUN-B2", LifecycleState.RESULT_AVAILABLE)
    resolution = resolution_of(root, "GOAL-A", "GOAL-B")
    assert resolution.upstream_run_states == (
        LifecycleState.CANCELLED,
        LifecycleState.RESULT_AVAILABLE,
    )
    assert resolution.execution_resolved is True
    assert resolution.acceptance_resolved is False
    assert resolution.execution_rule_id == "R-RES-EXEC-1"
    assert resolution.acceptance_rule_id == "R-RES-ACC-2"
    verdict = goal_gate_verdict(root, "GOAL-A")
    assert verdict.executable is True
    assert verdict.acceptance_allowed is False


# ---------------------------------------------------------------------------
# The frozen gate semantics over the resolved inputs (AC-02, AC-03)
# ---------------------------------------------------------------------------


def test_gate_state_soft_dependency_never_blocks(tmp_path):
    """AC-02: a soft dependency is an ordering hint, never a block."""
    root = init_project(tmp_path)
    register_goal(root, make_goal("GOAL-B"))
    register_goal(
        root,
        make_goal(
            "GOAL-SOFT",
            dependencies=(
                GoalDependency(
                    goal_id="GOAL-B",
                    type=DependencyType.SOFT_DEPENDENCY,
                    execution_gate=True,
                    acceptance_gate=True,
                ),
            ),
        ),
    )
    verdict = goal_gate_verdict(root, "GOAL-SOFT")
    assert verdict.executable is True
    assert verdict.execution_assessment.matched_rule_id == "R-EXEC-G-2"
    assert verdict.execution_blocking_goal_ids == ()
    assert "GOAL-B" in verdict.execution_assessment.pending_non_blocking_goal_ids
    assert verdict.acceptance_allowed is True
    assert "GOAL-B" in verdict.acceptance_assessment.pending_non_blocking_goal_ids


def test_gate_state_informational_dependency_is_inert(tmp_path):
    """AC-02: an informational dependency is recorded only -- no gating,
    no ordering influence."""
    root = init_project(tmp_path)
    register_goal(root, make_goal("GOAL-B"))
    register_goal(
        root,
        make_goal(
            "GOAL-INFO",
            dependencies=(
                GoalDependency(
                    goal_id="GOAL-B",
                    type=DependencyType.INFORMATIONAL,
                ),
            ),
        ),
    )
    verdict = goal_gate_verdict(root, "GOAL-INFO")
    assert verdict.executable is True
    assert verdict.acceptance_allowed is True
    assert verdict.execution_blocking_goal_ids == ()
    assert verdict.execution_assessment.pending_non_blocking_goal_ids == ()
    assert verdict.acceptance_blocking_goal_ids == ()
    assert verdict.acceptance_assessment.pending_non_blocking_goal_ids == ()
    assert verdict.execution_assessment.matched_rule_id == "R-EXEC-G-2"
    assert verdict.acceptance_assessment.matched_rule_id == "R-ACC-G-2"


def test_gate_state_axes_are_independent(tmp_path):
    """AC-03: execution and acceptance gates decide independently.

    Once GOAL-B's run reaches RESULT_AVAILABLE, GOAL-A may execute while
    its acceptance gate stays BLOCKED until the upstream evidence is
    submitted for review (the FDM-201 BET pattern).
    """
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    register_run_at(root, "GOAL-B", "RUN-B1")
    drive_to(root, "RUN-B1", LifecycleState.RESULT_AVAILABLE)
    verdict = goal_gate_verdict(root, "GOAL-A")
    assert verdict.executable is True
    assert verdict.acceptance_allowed is False
    assert verdict.execution_assessment.matched_rule_id == "R-EXEC-G-2"
    assert verdict.acceptance_assessment.matched_rule_id == "R-ACC-G-1"


def test_gate_state_unregistered_dependency_reported_and_blocks(tmp_path):
    """A dependency whose goal has no registered contract resolves never
    and is reported explicitly (M4-G02: unresolved refs, never silent)."""
    root = init_project(tmp_path)
    register_goal(
        root,
        make_goal(
            "GOAL-A",
            dependencies=(
                GoalDependency(
                    goal_id="GOAL-MISSING",
                    type=DependencyType.HARD_GATE,
                    execution_gate=True,
                ),
            ),
        ),
    )
    report = evaluate_workspace_gates(root)
    assert report.unresolved_dependency_goal_ids == ("GOAL-MISSING",)
    verdict = report.verdict_for("GOAL-A")
    assert verdict.executable is False
    assert verdict.execution_blocking_goal_ids == ("GOAL-MISSING",)
    resolution = verdict.resolutions[0]
    assert resolution.registered is False
    assert resolution.upstream_run_states == ()
    assert resolution.execution_resolved is False
    assert resolution.acceptance_resolved is False
    assert resolution.execution_rule_id == "R-RES-EXEC-2"
    assert resolution.acceptance_rule_id == "R-RES-ACC-2"


# ---------------------------------------------------------------------------
# Determinism, shape, boundaries
# ---------------------------------------------------------------------------


def test_gate_state_determinism(tmp_path):
    """Same registered state always yields the identical report."""
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    register_run_at(root, "GOAL-B", "RUN-B1")
    drive_to(root, "RUN-B1", LifecycleState.SUBMITTED_FOR_REVIEW)
    first = evaluate_workspace_gates(root)
    second = evaluate_workspace_gates(root)
    assert first == second
    assert first.to_dict() == second.to_dict()
    assert goal_gate_verdict(root, "GOAL-A") == first.verdict_for("GOAL-A")


def test_gate_state_report_records_versions_and_matched_rules(tmp_path):
    """The canonical mapping records every version and matched rule id."""
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    exported = evaluate_workspace_gates(root).to_dict()
    assert exported["gate_state_version"] == GATE_STATE_VERSION
    assert exported["unresolved_dependency_goal_ids"] == []
    entry = exported["verdicts"][0]
    assert entry["goal_id"] == "GOAL-A"
    assert entry["executable"] is False
    assert entry["execution_matched_rule_id"] == "R-EXEC-G-1"
    assert entry["acceptance_matched_rule_id"] == "R-ACC-G-1"
    assert entry["execution_blocking_goal_ids"] == ["GOAL-B"]
    resolution = entry["resolutions"][0]
    assert resolution["goal_id"] == "GOAL-B"
    assert resolution["type"] == "hard_gate"
    assert resolution["execution_rule_id"] == "R-RES-EXEC-2"
    assert resolution["execution_resolution_ruleset_version"] == (
        EXECUTION_RESOLUTION_RULESET_VERSION
    )
    assert resolution["acceptance_resolution_ruleset_version"] == (
        ACCEPTANCE_RESOLUTION_RULESET_VERSION
    )
    # The /goals gate view is canonical JSON (plain values only).
    assert json.loads(json.dumps(exported)) == exported


def test_gate_state_reports_draft_goals_too(tmp_path):
    """Every registered goal is reported, frozen or not (the verdict
    answers the dependency-gate question; the dispatch-facing frozen-
    contract enforcement is register_run's job, issue #148)."""
    root = init_project(tmp_path)
    register_goal(
        root, make_goal("GOAL-DRAFT", frozen=False, version="v1-draft")
    )
    assert evaluate_workspace_gates(root).verdict_for("GOAL-DRAFT").executable


def test_gate_state_type_boundaries(tmp_path):
    """TypeError at the public type boundaries."""
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        evaluate_workspace_gates(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        goal_gate_verdict(123, "GOAL-B")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        goal_gate_verdict(root, 123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        assert_execution_eligible(123, "GOAL-B")  # type: ignore[arg-type]


def test_gate_state_registry_boundaries(tmp_path):
    """Registry errors propagate unchanged; corrupt runs surface loudly."""
    with pytest.raises(ProjectNotInitializedError):
        evaluate_workspace_gates(tmp_path)
    with pytest.raises(ProjectNotInitializedError):
        goal_gate_verdict(tmp_path, "GOAL-B")
    root = init_project(tmp_path)
    register_goal(root, make_goal("GOAL-B"))
    with pytest.raises(GoalNotFoundError):
        goal_gate_verdict(root, "GOAL-GHOST")
    with pytest.raises(GoalNotFoundError):
        evaluate_workspace_gates(root).verdict_for("GOAL-GHOST")
    # A corrupt run record is a deterministic error, never silently
    # dropped (unparseable content: the backend's stable corrupt error).
    (root / RUNS_STATE_DIR).mkdir(parents=True, exist_ok=True)
    (root / RUNS_STATE_DIR / "RUN-X.json").write_text(
        "{not json", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt"):
        evaluate_workspace_gates(root)
    # Parseable but schema-shaped-invalid content: the stable wrapper
    # naming the run id (repair the unparseable record first so the
    # wrapper is the deciding error).
    (root / RUNS_STATE_DIR / "RUN-X.json").unlink()
    (root / RUNS_STATE_DIR / "RUN-Y.json").write_text(
        json.dumps({"run_id": "RUN-Y"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt run record for 'RUN-Y'"):
        evaluate_workspace_gates(root)


def test_gate_state_assert_execution_eligible(tmp_path):
    """The dispatch-facing enforcement: raise on BLOCKED, return on ALLOWED."""
    root = init_project(tmp_path)
    register_hard_gate_chain(root)
    with pytest.raises(GoalExecutionBlockedError, match="R-EXEC-G-1"):
        assert_execution_eligible(root, "GOAL-A")
    allowed = assert_execution_eligible(root, "GOAL-B")
    assert allowed.executable is True
    assert allowed.execution_assessment.matched_rule_id == "R-EXEC-G-2"
