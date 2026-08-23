"""Tests for the Result Package completeness validator (issue #155).

The module (``monitoring/result_completeness.py``) ships the runtime
primitive behind the Execution Monitor's "validate minimal Result
Package completeness" duty (``13-EXECUTION-MONITOR.md`` SS2): the pure
rule-table evaluator ``evaluate_result_package_completeness`` over the
registered ``WorkerResultPackage`` and the frozen ``GoalContract``,
plus the monitor-facing gate ``validate_result_package_completeness``
that resolves both records through the registries and is consulted
before a run advances to analysis. Coverage maps to the frozen
acceptance criteria:

* AC-01 -- ``test_result_completeness_*``: a Result Package missing a
  Goal-declared output is reported INCOMPLETE through the deterministic
  API with per-output detail (which outputs are uncovered and why);
  a complete package passes; the monitor contract's duty maps to a
  shipped function.

Test groups:

* the pure evaluator: complete package -> COMPLETE (R-RPC-C1);
  missing declared output -> INCOMPLETE (R-RPC-I1) with per-output
  coverage records; the matching rule (id precedence over name, the
  first *usable* key, artifacts + fact ids + fact names only -- the
  data section does not participate); unkeyable declarations;
  vacuous pass on zero declared outputs; matched_by detail;
* determinism and paradigm: identical inputs -> identical verdicts,
  order-independent coverage sets, the versioned ordered rule table,
  frozen records, TypeError at the public boundaries, stable
  ResultCompletenessError for identity/frozen-contract violations;
* the gate: injected readers, the registered-state end-to-end path,
  the referential-integrity checks (run state, exactly one package,
  goal/version agreement) and the TypeError boundaries.

Determinism: every test uses typed in-memory records or a pinned
identity/timestamp project (``initialize_project``), generated ids and
the fixed ``FIXED_STAMP`` -- no wall clock, no randomness, no network.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    ArtifactManifest,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    LifecycleState,
    Run,
    RunType,
    WorkerRole,
)
from scientific_reproduction.monitoring import MonitoringError
from scientific_reproduction.monitoring.result_completeness import (
    COMPLETENESS_RULES,
    COMPLETENESS_RULESET_VERSION,
    OutputCoverage,
    ResultCompletenessAudit,
    ResultCompletenessDecision,
    ResultCompletenessError,
    ResultCompletenessInput,
    ResultCompletenessVerdict,
    evaluate_result_package_completeness,
    validate_result_package_completeness,
)
from scientific_reproduction.planning.init import initialize_project
from scientific_reproduction.planning.plan import read_goal, register_goal
from scientific_reproduction.workers.results import (
    WorkerData,
    WorkerFact,
    WorkerResultPackage,
    read_worker_result,
    register_worker_result,
)
from scientific_reproduction.workers.run_helpers import (
    register_run,
    transition_run,
)

# ---------------------------------------------------------------------------
# Fixtures (deterministic: pinned identity/timestamp, generated ids)
# ---------------------------------------------------------------------------

#: Deterministic author/committer identity for the initialized project.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects.
DOI = "10.1039/D5TA00771B"

#: The test's worker role.
ROLE = WorkerRole.EXPERIMENT_WORKER

#: The frozen goal version every test record carries.
GOAL_VERSION = "v1"

#: Injected actor and stamps (no wall clock anywhere).
ACTOR = "monitor"
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: A pinned, well-formed generated context id (the exact shape the
#: DEV-M6-G01 ``GoalExecutionContextPackage`` generator produces).
CONTEXT_ID = generate_id("context", "GOAL-1", GOAL_VERSION, ROLE.value)

#: The mainline walk to RESULT_AVAILABLE (``core.rules.lifecycle``).
RESULT_AVAILABLE_WALK: tuple[LifecycleState, ...] = (
    LifecycleState.READY,
    LifecycleState.DISPATCHED,
    LifecycleState.RUNNING_EXTERNAL,
    LifecycleState.RESULT_AVAILABLE,
)

#: The result-bearing states (mirror the reconcile/trigger modules):
#: the only runs the gate accepts.
RESULT_RECORDED_STATES: tuple[LifecycleState, ...] = (
    LifecycleState.RESULT_AVAILABLE,
    LifecycleState.ANALYZING,
    LifecycleState.SUBMITTED_FOR_REVIEW,
    LifecycleState.CLOSED,
    LifecycleState.INVALIDATED,
)

#: The states the gate must reject: no durable result recorded.
NON_RESULT_STATES: tuple[LifecycleState, ...] = (
    LifecycleState.CREATED,
    LifecycleState.READY,
    LifecycleState.DISPATCHED,
    LifecycleState.RUNNING_EXTERNAL,
    LifecycleState.CANCELLED,
)


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_frozen_goal(
    goal_id: str = "GOAL-1",
    *,
    outputs: tuple[Any, ...] = (),
) -> GoalContract:
    """Build the frozen Goal Contract (version ``v1``, ``frozen`` True)."""
    return GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported isotherm ({goal_id}).",
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the formally reported isotherm dataset.",
        requirement_ids=["REQ-1"],
        dependencies=[],
        outputs=list(outputs),
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=True),
        analysis_protocol_ref="ANP-1",
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=GOAL_VERSION,
        frozen=True,
    )


def make_fact(fact_id: str, name: str, value: float = 1.0) -> WorkerFact:
    """Build one worker fact."""
    return WorkerFact(fact_id=fact_id, name=name, value=value)


def make_data(data_id: str, name: str) -> WorkerData:
    """Build one structured-output entry."""
    return WorkerData(data_id=data_id, name=name, format="csv")


def make_package(
    *,
    result_id: str = "RES-1",
    goal_id: str = "GOAL-1",
    goal_version: str = GOAL_VERSION,
    run_ref: str | None = None,
    input_artifact_ids: tuple[str, ...] = ("input-1.csv",),
    output_artifact_ids: tuple[str, ...] = (),
    facts: tuple[WorkerFact, ...] = (),
    data: tuple[WorkerData, ...] = (),
) -> WorkerResultPackage:
    """Build a record-valid result package (default: one input artifact)."""
    return WorkerResultPackage(
        result_id=result_id,
        context_id=CONTEXT_ID,
        worker_role=ROLE,
        goal_id=goal_id,
        goal_version=goal_version,
        run_ref=run_ref,
        facts=list(facts),
        data=list(data),
        input_artifact_ids=list(input_artifact_ids),
        output_artifact_ids=list(output_artifact_ids),
    )


def make_run(
    run_id: str = "RUN-1",
    goal_id: str = "GOAL-1",
    state: LifecycleState = LifecycleState.RESULT_AVAILABLE,
    goal_version: str = GOAL_VERSION,
) -> Run:
    """Build a durable Run record."""
    return Run(
        run_id=run_id,
        goal_id=goal_id,
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=state,
        goal_version=goal_version,
        created_at=TIMESTAMP.isoformat(),
    )


def make_run_id(index: int = 1) -> str:
    """A deterministic run id (``sr_run_<32 hex>``)."""
    return generate_id("run", f"goal-{index}", f"seq-{index}")


def register_artifact(root: Path, artifact_id: str) -> None:
    """Register one artifact manifest under ``manifests/`` (real registry)."""
    ArtifactRegistry(root / "manifests").register(
        ArtifactManifest(
            artifact_id=artifact_id,
            uri=f"file:///raw/{artifact_id}",
            sha256="a" * 64,
            size_bytes=1024,
            created_at="2026-01-01T00:00:00Z",
        )
    )


def register_walked_run(
    root: Path, run_id: str, goal_id: str = "GOAL-1"
) -> None:
    """Register a run and walk it to RESULT_AVAILABLE (real registries)."""
    register_run(
        root,
        make_run(run_id=run_id, goal_id=goal_id, state=LifecycleState.CREATED),
        actor=ACTOR,
        recorded_at=FIXED_STAMP,
    )
    for target in RESULT_AVAILABLE_WALK:
        transition_run(
            root,
            run_id,
            target,
            actor=ACTOR,
            reason="test-walk",
            at=FIXED_STAMP,
        )


def coverage_of(audit: ResultCompletenessAudit) -> tuple[OutputCoverage, ...]:
    """The audit's per-output coverage records."""
    return audit.output_coverage


def uncovered_of(audit: ResultCompletenessAudit) -> tuple[OutputCoverage, ...]:
    """The audit's uncovered per-output records."""
    return audit.uncovered_outputs


# ---------------------------------------------------------------------------
# AC: a complete package passes (COMPLETE, R-RPC-C1)
# ---------------------------------------------------------------------------


def test_result_completeness_complete_package_verdict() -> None:
    """Every declared output covered by artifacts/facts -> COMPLETE with
    per-output covered records and the full rule trace (AC-01)."""
    goal = make_frozen_goal(
        outputs=(
            {"id": "iso-1.csv"},
            {"name": "equilibrium capacity"},
        )
    )
    package = make_package(
        output_artifact_ids=("iso-1.csv",),
        facts=(make_fact("cap-qst", "equilibrium capacity"),),
    )
    audit = evaluate_result_package_completeness(package, goal)
    assert isinstance(audit, ResultCompletenessAudit)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    assert audit.complete
    assert audit.matched_rule_id == "R-RPC-C1"
    assert audit.goal_id == "GOAL-1"
    assert audit.result_id == "RES-1"
    assert audit.goal_version == GOAL_VERSION
    assert uncovered_of(audit) == ()
    coverage = coverage_of(audit)
    assert len(coverage) == 2
    first, second = coverage
    assert first.output_index == 0
    assert first.key_kind == "id"
    assert first.key_value == "iso-1.csv"
    assert first.covered
    assert first.matched_by == ("artifact:iso-1.csv",)
    assert first.detail is None
    assert second.output_index == 1
    assert second.key_kind == "name"
    assert second.key_value == "equilibrium capacity"
    assert second.covered
    assert second.matched_by == ("fact_name:equilibrium capacity",)
    assert second.detail is None
    # The verdict rule trace: R-RPC-I1 unmatched, R-RPC-C1 matched.
    assert audit.decisions == (
        ResultCompletenessDecision(
            rule_id="R-RPC-I1",
            description=COMPLETENESS_RULES[0].description,
            verdict=ResultCompletenessVerdict.INCOMPLETE,
            matched=False,
        ),
        ResultCompletenessDecision(
            rule_id="R-RPC-C1",
            description=COMPLETENESS_RULES[1].description,
            verdict=ResultCompletenessVerdict.COMPLETE,
            matched=True,
        ),
    )


# ---------------------------------------------------------------------------
# AC: a missing declared output -> INCOMPLETE with per-output detail
# ---------------------------------------------------------------------------


def test_result_completeness_missing_declared_output_incomplete_with_detail(
) -> None:
    """One declared output uncovered -> INCOMPLETE (R-RPC-I1) with
    per-output coverage records naming exactly the missing output."""
    goal = make_frozen_goal(
        outputs=(
            {"id": "iso-1.csv"},
            {"id": "pxrd-1.csv"},
        )
    )
    package = make_package(output_artifact_ids=("iso-1.csv",))
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    assert not audit.complete
    assert audit.matched_rule_id == "R-RPC-I1"
    coverage = coverage_of(audit)
    assert len(coverage) == 2
    assert coverage[0].covered
    uncovered = uncovered_of(audit)
    assert len(uncovered) == 1
    missing = uncovered[0]
    assert missing.output_index == 1
    assert missing.key_kind == "id"
    assert missing.key_value == "pxrd-1.csv"
    assert not missing.covered
    assert missing.matched_by == ()
    assert missing.detail is not None
    assert "pxrd-1.csv" in missing.detail
    # The trace records R-RPC-I1 as the first matched rule (the
    # verdict source); R-RPC-C1's predicate always matches, exactly
    # like the R-AUD-P1 default in planning/audit.py.
    assert audit.decisions[0].matched
    assert audit.decisions[1].matched


def test_result_completeness_multiple_missing_outputs_all_named() -> None:
    """Every uncovered declared output appears in the per-output detail,
    in declaration order."""
    goal = make_frozen_goal(
        outputs=(
            {"id": "iso-1.csv"},
            {"id": "pxrd-1.csv"},
            {"id": "tga-1.csv"},
        )
    )
    package = make_package(output_artifact_ids=("iso-1.csv",))
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    uncovered = uncovered_of(audit)
    assert [record.key_value for record in uncovered] == [
        "pxrd-1.csv",
        "tga-1.csv",
    ]
    assert [record.output_index for record in uncovered] == [1, 2]


# ---------------------------------------------------------------------------
# The matching rule (deterministic, test-locked)
# ---------------------------------------------------------------------------


def test_result_completeness_id_precedence_over_name() -> None:
    """R-OUT-K1: a declaration carrying both keys matches by ``id`` --
    a covered ``name`` does not cover an unmatched ``id``."""
    goal = make_frozen_goal(
        outputs=({"id": "pxrd-1.csv", "name": "iso-1.csv"},)
    )
    package = make_package(output_artifact_ids=("iso-1.csv",))
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    record = coverage_of(audit)[0]
    assert record.key_kind == "id"
    assert record.key_value == "pxrd-1.csv"
    assert not record.covered


def test_result_completeness_first_usable_key_wins() -> None:
    """The first *usable* stable key matches: an unusable ``id`` (empty
    string) falls back to a usable ``name`` (R-OUT-K2)."""
    goal = make_frozen_goal(
        outputs=({"id": "", "name": "equilibrium capacity"},)
    )
    package = make_package(
        facts=(make_fact("cap-qst", "equilibrium capacity"),)
    )
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    record = coverage_of(audit)[0]
    assert record.key_kind == "name"
    assert record.key_value == "equilibrium capacity"
    assert record.covered


def test_result_completeness_input_artifact_ids_cover() -> None:
    """A declared output covered by an input artifact id is covered."""
    goal = make_frozen_goal(outputs=({"id": "input-1.csv"},))
    package = make_package(input_artifact_ids=("input-1.csv",))
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    record = coverage_of(audit)[0]
    assert record.matched_by == ("artifact:input-1.csv",)


def test_result_completeness_fact_id_covers() -> None:
    """A declared output covered by a fact id is covered."""
    goal = make_frozen_goal(outputs=({"id": "cap-qst"},))
    package = make_package(
        facts=(make_fact("cap-qst", "equilibrium capacity"),)
    )
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    assert coverage_of(audit)[0].matched_by == ("fact_id:cap-qst",)


def test_result_completeness_fact_name_covers() -> None:
    """A declared output covered by a fact name is covered."""
    goal = make_frozen_goal(outputs=({"name": "equilibrium capacity"},))
    package = make_package(
        facts=(make_fact("cap-qst", "equilibrium capacity"),)
    )
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    assert coverage_of(audit)[0].matched_by == (
        "fact_name:equilibrium capacity",
    )


def test_result_completeness_data_section_does_not_cover() -> None:
    """Test-locked exclusion: the data section never participates --
    a declared output matching only a data id/name is uncovered (the
    issue's "artifacts/facts" reconciliation surface)."""
    goal = make_frozen_goal(
        outputs=({"id": "fit-1.json"}, {"name": "fit summary"})
    )
    package = make_package(data=(make_data("fit-1.json", "fit summary"),))
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    assert [record.covered for record in coverage_of(audit)] == [
        False,
        False,
    ]


def test_result_completeness_matched_by_lists_all_matches_sorted() -> None:
    """A key carried by several package identifiers lists every match,
    sorted (artifact / fact_id / fact_name vocabulary)."""
    goal = make_frozen_goal(outputs=({"id": "cap-qst"},))
    package = make_package(
        output_artifact_ids=("cap-qst",),
        facts=(make_fact("cap-qst", "cap-qst"),),
    )
    audit = evaluate_result_package_completeness(package, goal)
    record = coverage_of(audit)[0]
    assert record.covered
    assert record.matched_by == (
        "artifact:cap-qst",
        "fact_id:cap-qst",
        "fact_name:cap-qst",
    )


def test_result_completeness_unkeyable_declarations_uncovered_with_reason(
) -> None:
    """R-OUT-U1: non-mapping declarations and mappings without a usable
    id/name are unkeyable -> uncovered, each with its reason."""
    goal = make_frozen_goal(
        outputs=(
            "a plain string declaration",
            {"description": "no id or name"},
            {"id": 7},
            {"id": "", "name": ""},
        )
    )
    package = make_package()
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    coverage = coverage_of(audit)
    assert len(coverage) == 4
    assert all(record.key_kind is None for record in coverage)
    assert all(record.key_value is None for record in coverage)
    assert all(not record.covered for record in coverage)
    assert coverage[0].detail is not None
    assert "not a mapping" in coverage[0].detail
    assert coverage[1].detail is not None
    assert "no 'id' or 'name' key" in coverage[1].detail
    assert coverage[2].detail is not None
    assert "'id' is not a non-empty string" in coverage[2].detail
    # Both keys unusable: the id reason wins (id precedes name).
    assert coverage[3].detail is not None
    assert "'id' is not a non-empty string" in coverage[3].detail
    assert uncovered_of(audit) == coverage


def test_result_completeness_zero_declared_outputs_passes_vacuously() -> None:
    """A goal declaring no outputs has no coverage obligation -> COMPLETE
    (the planning/audit.py vacuous-pass convention)."""
    goal = make_frozen_goal(outputs=())
    package = make_package()
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    assert audit.matched_rule_id == "R-RPC-C1"
    assert coverage_of(audit) == ()
    assert uncovered_of(audit) == ()


def test_result_completeness_duplicate_declarations_evaluated_independently(
) -> None:
    """Each declared output entry is evaluated on its own: two identical
    declarations produce two coverage records."""
    goal = make_frozen_goal(outputs=({"id": "iso-1.csv"}, {"id": "iso-1.csv"}))
    package = make_package(output_artifact_ids=("iso-1.csv",))
    audit = evaluate_result_package_completeness(package, goal)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    coverage = coverage_of(audit)
    assert len(coverage) == 2
    assert [record.output_index for record in coverage] == [0, 1]
    assert all(record.covered for record in coverage)


# ---------------------------------------------------------------------------
# Determinism, the rule table, frozen records
# ---------------------------------------------------------------------------


def test_result_completeness_deterministic_identical_inputs_identical_verdict(
) -> None:
    """Same state -> same verdict: repeated evaluation and reordered
    package sections yield byte-equal records."""
    goal = make_frozen_goal(
        outputs=(
            {"id": "iso-1.csv"},
            {"name": "equilibrium capacity"},
            {"id": "pxrd-1.csv"},
        )
    )
    package = make_package(
        output_artifact_ids=("iso-1.csv",),
        facts=(make_fact("cap-qst", "equilibrium capacity"),),
    )
    audit = evaluate_result_package_completeness(package, goal)
    assert evaluate_result_package_completeness(package, goal) == audit
    reordered = make_package(
        input_artifact_ids=tuple(reversed(package.input_artifact_ids)),
        output_artifact_ids=tuple(reversed(package.output_artifact_ids)),
        facts=tuple(reversed(package.facts)),
    )
    assert (
        evaluate_result_package_completeness(reordered, goal) == audit
    )


def test_result_completeness_rule_table_is_versioned_and_ordered() -> None:
    """The ordered rule table: R-RPC-I1 first, R-RPC-C1 the always-
    matching default, one ruleset version."""
    assert COMPLETENESS_RULESET_VERSION == "1.0"
    assert [rule.rule_id for rule in COMPLETENESS_RULES] == [
        "R-RPC-I1",
        "R-RPC-C1",
    ]
    assert COMPLETENESS_RULES[0].verdict is ResultCompletenessVerdict.INCOMPLETE
    assert COMPLETENESS_RULES[1].verdict is ResultCompletenessVerdict.COMPLETE
    # The default rule matches every state (first match wins => the
    # trace is deterministic); the INCOMPLETE rule matches exactly
    # when something is uncovered.
    complete_audit = evaluate_result_package_completeness(
        make_package(), make_frozen_goal(outputs=())
    )
    complete_input = _rule_input_of(complete_audit)
    assert COMPLETENESS_RULES[1].predicate(complete_input)
    assert not COMPLETENESS_RULES[0].predicate(complete_input)
    incomplete_audit = evaluate_result_package_completeness(
        make_package(), make_frozen_goal(outputs=({"id": "iso-1.csv"},))
    )
    incomplete_input = _rule_input_of(incomplete_audit)
    assert COMPLETENESS_RULES[0].predicate(incomplete_input)
    assert COMPLETENESS_RULES[1].predicate(incomplete_input)


def _rule_input_of(audit: ResultCompletenessAudit) -> ResultCompletenessInput:
    """Rebuild the rule-table input of an audit (predicate plumbing)."""
    return ResultCompletenessInput(
        goal_id=audit.goal_id,
        result_id=audit.result_id,
        goal_version=audit.goal_version,
        output_coverage=audit.output_coverage,
        uncovered_outputs=audit.uncovered_outputs,
    )


def test_result_completeness_records_are_frozen() -> None:
    """The verdict and coverage records are frozen (house discipline)."""
    audit = evaluate_result_package_completeness(
        make_package(), make_frozen_goal(outputs=({"id": "iso-1.csv"},))
    )
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    with pytest.raises(FrozenInstanceError):
        audit.verdict = ResultCompletenessVerdict.COMPLETE  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        coverage_of(audit)[0].covered = True  # type: ignore[misc]


def test_result_completeness_audit_record_validates_invariants() -> None:
    """The audit record rejects an uncovered set that is not the subset
    of the coverage set (deterministic-record discipline)."""
    goal = make_frozen_goal(outputs=({"id": "iso-1.csv"},))
    audit = evaluate_result_package_completeness(make_package(), goal)
    covered_record = coverage_of(audit)[0]
    assert not covered_record.covered
    forged_uncovered = replace(covered_record, covered=True,
                               matched_by=("artifact:iso-1.csv",))
    with pytest.raises(ResultCompletenessError):
        ResultCompletenessAudit(
            verdict=ResultCompletenessVerdict.INCOMPLETE,
            goal_id=audit.goal_id,
            result_id=audit.result_id,
            goal_version=audit.goal_version,
            output_coverage=audit.output_coverage,
            uncovered_outputs=(forged_uncovered,),
            decisions=audit.decisions,
            matched_rule_id=audit.matched_rule_id,
        )


# ---------------------------------------------------------------------------
# TypeError boundaries and stable contract errors of the pure validator
# ---------------------------------------------------------------------------


def test_result_completeness_typeerror_boundaries() -> None:
    """TypeError at the public type boundaries (house paradigm)."""
    package = make_package()
    goal = make_frozen_goal(outputs=())
    with pytest.raises(TypeError):
        evaluate_result_package_completeness(package.to_dict(), goal)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_result_package_completeness(package, goal.to_dict())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_result_package_completeness(
            package, replace(goal, outputs="not-a-list")  # type: ignore[arg-type]
        )


def test_result_completeness_identity_and_frozen_contract_violations() -> None:
    """Stable ResultCompletenessError for a package/contract goal
    mismatch and for an unfrozen contract; ValueError-based hierarchy."""
    package = make_package()
    goal = make_frozen_goal(outputs=())
    with pytest.raises(ResultCompletenessError, match="different"):
        evaluate_result_package_completeness(
            package, replace(goal, goal_id="GOAL-2")
        )
    with pytest.raises(ResultCompletenessError, match="not frozen"):
        evaluate_result_package_completeness(package, replace(goal, frozen=False))
    assert issubclass(ResultCompletenessError, MonitoringError)
    assert issubclass(MonitoringError, ValueError)


# ---------------------------------------------------------------------------
# The monitor-facing gate (injected readers)
# ---------------------------------------------------------------------------


def test_result_completeness_gate_injected_readers_evaluate_verdict(
    tmp_path: Path,
) -> None:
    """The gate resolves through the injected readers and returns the
    evaluator's verdict for the resolved records."""
    calls: dict[str, list[str]] = {"run": [], "results": [], "goal": []}
    goal = make_frozen_goal(outputs=({"id": "iso-1.csv"},))
    package = make_package(
        run_ref="RUN-1", output_artifact_ids=("iso-1.csv",)
    )
    run = make_run(run_id="RUN-1", state=LifecycleState.RESULT_AVAILABLE)

    def run_reader(run_id: str) -> Run:
        calls["run"].append(run_id)
        return run

    def results_reader(run_id: str) -> tuple[WorkerResultPackage, ...]:
        calls["results"].append(run_id)
        return (package,)

    def goal_reader(goal_id: str) -> GoalContract:
        calls["goal"].append(goal_id)
        return goal

    audit = validate_result_package_completeness(
        tmp_path,
        "RUN-1",
        run_reader=run_reader,
        results_reader=results_reader,
        goal_reader=goal_reader,
    )
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    assert audit == evaluate_result_package_completeness(package, goal)
    assert calls == {
        "run": ["RUN-1"],
        "results": ["RUN-1"],
        "goal": ["GOAL-1"],
    }


def test_result_completeness_gate_accepts_every_result_bearing_state(
    tmp_path: Path,
) -> None:
    """RESULT_AVAILABLE or later is accepted; every pre-result state is
    rejected with the stable contract error naming the state."""
    goal = make_frozen_goal(outputs=())
    package = make_package(run_ref="RUN-1")
    for state in RESULT_RECORDED_STATES:
        run = make_run(run_id="RUN-1", state=state)
        audit = validate_result_package_completeness(
            tmp_path,
            "RUN-1",
            run_reader=lambda _run_id, run=run: run,
            results_reader=lambda _run_id: (package,),
            goal_reader=lambda _goal_id: goal,
        )
        assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    for state in NON_RESULT_STATES:
        run = make_run(run_id="RUN-1", state=state)
        with pytest.raises(ResultCompletenessError, match="RESULT_AVAILABLE"):
            validate_result_package_completeness(
                tmp_path,
                "RUN-1",
                run_reader=lambda _run_id, run=run: run,
                results_reader=lambda _run_id: (package,),
                goal_reader=lambda _goal_id: goal,
            )


def test_result_completeness_gate_package_resolution_violations(
    tmp_path: Path,
) -> None:
    """Exactly one registered Result Package must resolve for the run:
    zero or several raise the stable contract error."""
    goal = make_frozen_goal(outputs=())
    run = make_run(run_id="RUN-1", state=LifecycleState.RESULT_AVAILABLE)
    with pytest.raises(ResultCompletenessError, match="no registered Result"):
        validate_result_package_completeness(
            tmp_path,
            "RUN-1",
            run_reader=lambda _run_id: run,
            results_reader=lambda _run_id: (),
            goal_reader=lambda _goal_id: goal,
        )
    packages = (
        make_package(result_id="RES-1", run_ref="RUN-1"),
        make_package(result_id="RES-2", run_ref="RUN-1"),
    )
    with pytest.raises(ResultCompletenessError, match="RES-1, RES-2"):
        validate_result_package_completeness(
            tmp_path,
            "RUN-1",
            run_reader=lambda _run_id: run,
            results_reader=lambda _run_id: packages,
            goal_reader=lambda _goal_id: goal,
        )


def test_result_completeness_gate_run_package_identity_violations(
    tmp_path: Path,
) -> None:
    """The run and its Result Package must name the same goal and the
    same frozen goal version (referential integrity)."""
    goal = make_frozen_goal(outputs=())
    run = make_run(run_id="RUN-1", state=LifecycleState.RESULT_AVAILABLE)
    mismatched_goal = make_package(run_ref="RUN-1", goal_id="GOAL-2")
    with pytest.raises(ResultCompletenessError, match="same goal"):
        validate_result_package_completeness(
            tmp_path,
            "RUN-1",
            run_reader=lambda _run_id: run,
            results_reader=lambda _run_id: (mismatched_goal,),
            goal_reader=lambda _goal_id: goal,
        )
    mismatched_version = make_package(run_ref="RUN-1", goal_version="v2")
    with pytest.raises(ResultCompletenessError, match="same frozen goal"):
        validate_result_package_completeness(
            tmp_path,
            "RUN-1",
            run_reader=lambda _run_id: run,
            results_reader=lambda _run_id: (mismatched_version,),
            goal_reader=lambda _goal_id: goal,
        )


def test_result_completeness_gate_typeerror_boundaries(tmp_path: Path) -> None:
    """TypeError at the gate's public boundaries (house paradigm)."""
    goal = make_frozen_goal(outputs=())
    package = make_package(run_ref="RUN-1")
    run = make_run(run_id="RUN-1", state=LifecycleState.RESULT_AVAILABLE)
    root = tmp_path
    with pytest.raises(TypeError):
        validate_result_package_completeness(123, "RUN-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_result_package_completeness(root, 123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        validate_result_package_completeness(
            root, "RUN-1", run_reader="not-callable"  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        validate_result_package_completeness(
            root,
            "RUN-1",
            run_reader=lambda _run_id: "not-a-run",  # type: ignore[return-value]
            results_reader=lambda _run_id: (package,),
            goal_reader=lambda _goal_id: goal,
        )
    with pytest.raises(TypeError):
        validate_result_package_completeness(
            root,
            "RUN-1",
            run_reader=lambda _run_id: run,
            results_reader=lambda _run_id: [package],  # type: ignore[return-value]
            goal_reader=lambda _goal_id: goal,
        )
    with pytest.raises(TypeError):
        validate_result_package_completeness(
            root,
            "RUN-1",
            run_reader=lambda _run_id: run,
            results_reader=lambda _run_id: (package,),
            goal_reader=lambda _goal_id: "not-a-goal",  # type: ignore[return-value]
        )


# ---------------------------------------------------------------------------
# The gate over the real registries (the shipped call path)
# ---------------------------------------------------------------------------


def test_result_completeness_gate_registered_state_end_to_end(
    tmp_path: Path,
) -> None:
    """The shipped call path: the gate resolves the registered Result
    Package (by run_ref) and the frozen Goal Contract from the workspace
    registries and returns the COMPLETE verdict."""
    root = init_project(tmp_path / "project")
    run_id = make_run_id(1)
    register_goal(
        root,
        make_frozen_goal(
            outputs=({"id": "iso-1.csv"}, {"name": "equilibrium capacity"})
        ),
    )
    register_walked_run(root, run_id)
    register_artifact(root, "input-1.csv")
    register_artifact(root, "iso-1.csv")
    register_worker_result(
        root,
        make_package(
            run_ref=run_id,
            input_artifact_ids=("input-1.csv",),
            output_artifact_ids=("iso-1.csv",),
            facts=(make_fact("cap-qst", "equilibrium capacity"),),
        ),
    )
    audit = validate_result_package_completeness(root, run_id)
    assert audit.verdict is ResultCompletenessVerdict.COMPLETE
    assert audit.result_id == "RES-1"
    # The gate's verdict equals the pure evaluator over the exact
    # registered records.
    assert audit == evaluate_result_package_completeness(
        read_worker_result(root, "RES-1"), read_goal(root, "GOAL-1")
    )


def test_result_completeness_gate_registered_state_incomplete_end_to_end(
    tmp_path: Path,
) -> None:
    """The shipped call path reports INCOMPLETE with per-output detail
    when the registered package misses a declared output."""
    root = init_project(tmp_path / "project")
    run_id = make_run_id(1)
    register_goal(
        root,
        make_frozen_goal(
            outputs=({"id": "iso-1.csv"}, {"id": "pxrd-1.csv"})
        ),
    )
    register_walked_run(root, run_id)
    register_artifact(root, "input-1.csv")
    register_artifact(root, "iso-1.csv")
    register_worker_result(
        root,
        make_package(
            run_ref=run_id,
            input_artifact_ids=("input-1.csv",),
            output_artifact_ids=("iso-1.csv",),
        ),
    )
    audit = validate_result_package_completeness(root, run_id)
    assert audit.verdict is ResultCompletenessVerdict.INCOMPLETE
    assert audit.matched_rule_id == "R-RPC-I1"
    uncovered = uncovered_of(audit)
    assert len(uncovered) == 1
    assert uncovered[0].key_value == "pxrd-1.csv"
    assert not uncovered[0].covered
