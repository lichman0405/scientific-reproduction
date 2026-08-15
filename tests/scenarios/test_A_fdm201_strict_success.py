"""Scenario A end-to-end test: strict success closes without Recovery
(DEV-M12-G05 AC-01).

Covers 18-TEST-AND-ACCEPTANCE-PLAN.md section 3, scenario A, executed
entirely through the real machinery: the GOAL-EXE-50 execution of the
frozen FDM-201 benchmark (DOI 10.1039/D5TA00771B) closes as *strict
success* --

* three **valid independent Runs** are executed against the frozen goal
  through the real filesystem lab adapter (the only scripted element is
  the lab result package appearing in the adapter's incoming handoff --
  the adapter's external boundary, same convention as scenario H);
* the analysis step evaluates the batches under the frozen acceptance
  criteria (ASM-A1-TOL-01 10% margin around the INV-0301 reported
  value, ASM-A1-N-01 independent floor) with the real statistics stack:
  EQUIVALENT and statistically SUFFICIENT;
* the Supervisor closes the Requirement INV-0301 as REPRODUCED with
  method reproducibility DIRECTLY_REPRODUCIBLE (real outcome rules:
  R-REQOUT-1, R-PRJ-1 FULLY_REPRODUCED, R-MR-3 worst-of);
* **no Recovery** is entered anywhere -- no recovery Goal record, no
  RECOVERY_ENTRY decision, no ``recovery.created`` audit event -- and
  the audit chain (goal freeze, inventory registration, run dispatch,
  run completion, analysis, requirement closure, project outcome) is
  complete and strictly ordered.

All timestamps come from a FakeClock fixed to one stamp and all ids are
deterministic, so the scenario is byte-deterministic: the replay test
runs the executor twice in separate workspaces and compares the full
durable state tree.

Every benchmark-derived constant (target value, tolerance band,
independent floor, closure-contract saturation rule) is parsed at
import time from the frozen benchmark files under
``benchmarks/fdm201/``, never invented here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from scientific_reproduction.adapters.lab.base import (
    CollectionResult,
    DispatchRecord,
    DispatchState,
)
from scientific_reproduction.adapters.lab.filesystem import FilesystemLabAdapter
from scientific_reproduction.adapters.lab.manifest import RESULT_MANIFEST_VERSION
from scientific_reproduction.analysis.protocols import (
    freeze_primary_protocol,
    register_analysis_record,
)
from scientific_reproduction.analysis.replication import (
    ReplicateStatus,
    evaluate_replicate_sufficiency,
    replicate_criterion_from_acceptance,
    sufficiency_findings,
    sufficiency_metrics,
    sufficiency_uncertainty_payload,
)
from scientific_reproduction.analysis.results import (
    ARTIFACTS_STATE_DIR,
    ResultRecord,
    read_result,
    register_result,
)
from scientific_reproduction.analysis.statistics import (
    ConfidenceInterval,
    EquivalenceBounds,
    EquivalenceVerdict,
    decide_equivalence,
    effect_confidence_interval,
    effect_metrics,
    equivalence_bounds_from_acceptance,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import AuditIdentity, map_event_to_audit
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ArtifactManifest,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Criticality,
    DecisionMode,
    DecisionType,
    DependencyType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    InventoryItemType,
    LabExecutionPackage,
    LifecycleState,
    MappingStatus,
    MethodReproducibility,
    PrimaryOrExploratory,
    ProjectEvent,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    Run,
    RunExternal,
    RunType,
    SupervisorDecision,
)
from scientific_reproduction.core.rules.outcome import (
    MethodReproducibilityRecord,
    ReproductionOutcome,
    RequirementOutcomeRecord,
    aggregate_method_reproducibility,
    aggregate_project_outcome,
    classify_requirement_outcome,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
    read_project_state,
)
from scientific_reproduction.planning.inventory import (
    read_requirement,
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    read_acceptance,
    read_analysis_protocol,
    read_goal,
    register_acceptance,
    register_closure_contract,
    register_goal,
)

# ---------------------------------------------------------------------------
# Frozen benchmark grounding (parsed from the benchmark files, never
# invented): benchmarks/fdm201/{goals,plans,inventory}
# ---------------------------------------------------------------------------

BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "fdm201"

#: Primary target DOI of the one-paper project (17-FDM201-REFERENCE-CASE.md).
DOI = "10.1039/D5TA00771B"

#: Deterministic author/committer identity (mirrors protocol_helpers).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp for the frozen PRIMARY protocol.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Fixed stamp for every scenario-side timestamp (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: Frozen goal / acceptance / protocol / closure ids of the benchmark
#: (benchmarks/fdm201/goals/goals.yaml, plans/analysis_plan.yaml,
#: plans/closure.yaml) and the requirement the scenario closes.
GOAL_ID = "GOAL-EXE-50"
ACCEPTANCE_ID = "ACC-1"
PROTOCOL_ID = "ANL-030"
CLOSURE_ID = "CC-EXPERIMENT"
REQUIREMENT_ID = "INV-0301"
GOAL_VERSION = "v1"

#: The scenario's registry ids (deterministic; no generated-id collisions).
RUN_IDS = ("RUN-001", "RUN-002", "RUN-003")
PACKAGE_IDS = ("PKG-001", "PKG-002", "PKG-003")
ARTIFACT_IDS = ("ART-001", "ART-002", "ART-003")
RESULT_IDS = ("RES-A1", "RES-A2", "RES-A3")
ANALYSIS_RESULT_ID = "RES-A-ANL"

#: The raw data file the lab result package declares (test_H convention).
REQUIRED_RETURN = ["uptake.csv"]

#: The three simulated independent batch uptakes of scenario A
#: (cm3 g-1 at 298 K / 1 bar): sample spread 0.7 around the published
#: target -- the mean interval lies entirely inside the frozen 10 % band.
A_BATCHES = (180.5, 181.2, 179.8)

#: Audit event types of the scenario (canonical governance vocabulary from
#: audit/git.py where it exists; scenario vocabulary elsewhere).
PLAN_FROZEN_EVENT_TYPE = "plan.frozen"
INVENTORY_AUDIT_EVENT_TYPE = "inventory.audit.passed"
RUN_DISPATCHED_EVENT_TYPE = "run.dispatched"
RUN_COMPLETED_EVENT_TYPE = "run.completed"
RUN_TRANSITION_EVENT_TYPE = "run.transition"
ANALYSIS_COMPLETED_EVENT_TYPE = "analysis.completed"
SUPERVISOR_DECISION_EVENT_TYPE = "supervisor.decision"
REQUIREMENT_CLOSED_EVENT_TYPE = "requirement.closed"
PROJECT_OUTCOME_EVENT_TYPE = "project.outcome.recorded"

#: The frozen requirement-closure decision id of the supervisor.
CLOSURE_DECISION_ID = "DEC-A1"


def _load_yaml(relative: str) -> dict:
    path = BENCHMARK_ROOT / relative
    assert path.is_file(), f"benchmark file missing: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def benchmark_target() -> float:
    """The published C3H6 uptake of INV-0301 (cm3 g-1 at 298 K / 1 bar),
    parsed from the frozen inventory seed fact."""
    items = _load_yaml("inventory/INVENTORY.yaml")["items"]
    item = next(i for i in items if i["item_id"] == REQUIREMENT_ID)
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*cm3 g-1", item["reported_value"])
    assert match, (
        f"INV-0301 reported_value not parseable: {item['reported_value']!r}"
    )
    return float(match.group(1))


def benchmark_margin(target: float) -> float:
    """The equivalence half-width: the frozen 10 % relative band of
    ASM-A1-TOL-01 around the target (effect-space region
    [-TARGET*0.10, +TARGET*0.10])."""
    assumptions = _load_yaml("plans/assumptions.yaml")["assumptions"]
    tol = next(a for a in assumptions if a["assumption_id"] == "ASM-A1-TOL-01")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*\(([0-9]+)%\)", tol["value"])
    assert match, f"ASM-A1-TOL-01 value not parseable: {tol['value']!r}"
    relative = float(match.group(1))
    percent = int(match.group(2))
    assert percent == round(relative * 100), (
        f"ASM-A1-TOL-01 fraction/percent mismatch: {tol['value']!r}"
    )
    return target * relative


def benchmark_min_independent() -> int:
    """The frozen independent-n floor of ASM-A1-N-01."""
    assumptions = _load_yaml("plans/assumptions.yaml")["assumptions"]
    n = next(a for a in assumptions if a["assumption_id"] == "ASM-A1-N-01")
    match = re.search(r"^(\d+)\s*\(minimum_n = (\d+)", n["value"])
    assert match, f"ASM-A1-N-01 value not parseable: {n['value']!r}"
    assert match.group(1) == match.group(2), (
        f"ASM-A1-N-01 floor mismatch: {n['value']!r}"
    )
    return int(match.group(1))


#: The published seed value the batches reproduce (INV-0301, REPORTED-NON-FINAL).
TARGET = benchmark_target()

#: The frozen equivalence half-width around the target (10 % per ASM-A1-TOL-01).
MARGIN = benchmark_margin(TARGET)

#: The frozen independent-n floor (ASM-A1-N-01: minimum_n = 2).
MIN_INDEPENDENT = 2

#: The frozen relative-half-width precision threshold of the mean (frozen
#: policy of 07-STATISTICS-AND-ACCEPTANCE.md SS8/SS9; the same value the
#: analysis suite's scenario D pins -- CC-EXPERIMENT requires "replicate
#: spread and comparison statistics per ANL-090 before scientific closure").
PRECISION = 0.1

#: The frozen saturation rule of the closure contract (CC-EXPERIMENT
#: literature.required_zero_novelty_cycles; the closure record carries it).
REQUIRED_ZERO_NOVELTY_CYCLES = 2


# ---------------------------------------------------------------------------
# Deterministic scenario machinery (no wall clock, no network, no sleeps)
# ---------------------------------------------------------------------------


class FakeClock:
    """Injectable clock: the single fixed stamp repeats forever and every
    read is recorded -- no wall clock anywhere in the tested path."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


def script_result_package(
    handoff: Path, run_id: str, package_id: str, value: float
) -> Path:
    """The lab result of one batch: a Result Package for the run appears in
    the adapter's incoming handoff, declaring the required raw data file.
    The raw data file carries the measured batch uptake -- the same value
    the batch result record later reports (single source of truth)."""
    incoming = handoff / "incoming" / run_id
    incoming.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": RESULT_MANIFEST_VERSION,
        "package_id": package_id,
        "project_id": "scenario-a",
        "goal_id": GOAL_ID,
        "run_id": run_id,
        "files": list(REQUIRED_RETURN),
        "notes": ["scripted lab result at the adapter's external boundary"],
    }
    atomic_write(
        incoming / "result-manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    atomic_write(
        incoming / "uptake.csv",
        f"batch,uptake_cm3_g1_at_298K_1bar\n{run_id},{value:g}\n",
    )
    return incoming


def make_protocol() -> AnalysisProtocolOrResult:
    """The schema-valid draft PRIMARY protocol ANL-030 (isotherm analysis,
    same shape as the frozen benchmark record: kind protocol, profile
    ROUTINE_ANALYSIS, PRIMARY, v1)."""
    return AnalysisProtocolOrResult(
        analysis_id=PROTOCOL_ID,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[{"name": "isotherm_uptake_comparison_298K_1bar"}],
    )


def make_acceptance() -> AcceptanceCriteria:
    """The frozen EQUIVALENCE acceptance record of scenario A: the single
    criteria entry carries the frozen margin (ASM-A1-TOL-01), the frozen
    independent-n floor (ASM-A1-N-01) and the frozen precision threshold;
    ``target`` is the published INV-0301 seed value."""
    return AcceptanceCriteria(
        acceptance_id=ACCEPTANCE_ID,
        goal_id=GOAL_ID,
        version=GOAL_VERSION,
        frozen=True,
        decision_mode=DecisionMode.EQUIVALENCE,
        target=TARGET,
        criteria=[
            {
                "metric": "batch_level_uptake",
                "margin": MARGIN,
                "min_independent": MIN_INDEPENDENT,
                "precision": PRECISION,
            }
        ],
        rationale=(
            "frozen FDM-201 C3H6 uptake equivalence criteria: 10 % relative"
            " band per ASM-A1-TOL-01 around the INV-0301 reported value,"
            " independent floor per ASM-A1-N-01, precision threshold per"
            " 07-STATISTICS-AND-ACCEPTANCE.md SS8/SS9"
        ),
    )


def make_goal() -> GoalContract:
    """The frozen GOAL-EXE-50 goal contract, built from the benchmark goals
    file (dependencies, replication policy, assumption/resource ids, closure
    contract ref) with the scenario's frozen acceptance link."""
    goals = _load_yaml("goals/goals.yaml")["goals"]
    record = next(g for g in goals if g["goal_id"] == GOAL_ID)
    return GoalContract(
        goal_id=GOAL_ID,
        title=record["title"],
        unit_process_type=record["unit_process_type"],
        track=GoalTrack(record["track"]),
        objective=record["objective"],
        requirement_ids=list(record["requirement_ids"]),
        dependencies=[
            GoalDependency(
                goal_id=dependency["goal_id"],
                type=DependencyType(dependency["type"]),
                execution_gate=dependency.get("execution_gate", False),
                acceptance_gate=dependency.get("acceptance_gate", False),
            )
            for dependency in record["dependencies"]
        ],
        acceptance=GoalAcceptance(criteria_ref=ACCEPTANCE_ID, frozen=True),
        analysis_protocol_ref=record["analysis_protocol_ref"],
        replication=GoalReplication(
            independent_required=record["replication"]["independent_required"],
            planned_n_policy=record["replication"]["planned_n_policy"],
            minimum_n=record["replication"]["minimum_n"],
        ),
        version=record["version"],
        frozen=True,
        resource_ids=list(record["resource_ids"]),
        assumption_ids=list(record["assumption_ids"]),
        closure_contract_ref=record["closure_contract_ref"],
        frozen_at=record["frozen_at"],
    )


def make_closure_contract() -> ClosureContract:
    """The frozen CC-EXPERIMENT closure contract, built from the benchmark
    closure file (statistical/execution/recovery/literature axes verbatim)."""
    contracts = _load_yaml("plans/closure.yaml")["closure_contracts"]
    record = next(c for c in contracts if c["closure_id"] == CLOSURE_ID)
    recovery = record["recovery"]
    literature = record["literature"]
    return ClosureContract(
        closure_id=CLOSURE_ID,
        frozen=record["frozen"],
        statistical_sufficiency=dict(record["statistical_sufficiency"]),
        execution_validity=dict(record["execution_validity"]),
        diagnosis=dict(record["diagnosis"]),
        recovery=ClosureRecovery(
            eligibility_rule=dict(recovery["eligibility_rule"]),
            eligible_hypotheses_total=recovery["eligible_hypotheses_total"],
            tested_or_ruled_out=recovery["tested_or_ruled_out"],
            remaining=recovery["remaining"],
        ),
        literature=ClosureLiterature(
            required_search_families_completed=(
                literature["required_search_families_completed"]
            ),
            consecutive_zero_novelty_cycles=(
                literature["consecutive_zero_novelty_cycles"]
            ),
            required_zero_novelty_cycles=literature["required_zero_novelty_cycles"],
        ),
        closure_allowed=record["closure_allowed"],
    )


def make_inventory_item() -> ReproductionInventoryItem:
    """The INV-0301 inventory item, built from the benchmark inventory file
    (the requirement the scenario closes maps this item). ``mapping_status``
    is always recomputed by the real rule table at registration from the
    requirements registered at that moment; the requirement record is
    created later at closure time (records are immutable and once-only), so
    the item stores the UNMAPPED state the rule table actually computes
    here -- the requirement's ``inventory_items`` edge carries the mapping."""
    items = _load_yaml("inventory/INVENTORY.yaml")["items"]
    record = next(i for i in items if i["item_id"] == REQUIREMENT_ID)
    provenance = record["provenance"]
    # The benchmark file's ``category`` is the spec's letter classification;
    # the frozen inventory-item schema takes the ``item_type`` vocabulary.
    # INV-0301 is a single-component isotherm measurement: an experiment.
    category_to_type = {"c": InventoryItemType.EXPERIMENT}
    assert record["category"] in category_to_type, (
        f"unmapped inventory category {record['category']!r}"
    )
    return ReproductionInventoryItem(
        inventory_id=record["item_id"],
        source_id=provenance["source_ids"][0],
        item_type=category_to_type[record["category"]],
        formal_report=True,
        description=record["description"],
        mapping_status=MappingStatus.UNMAPPED,
        source_location=provenance["part"],
        requirement_ids=[REQUIREMENT_ID],
    )


def init_project(root: Path) -> str:
    """Initialize a deterministic one-paper project at ``root``; return the
    persisted project id (the lab packages must reference it)."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return read_project_state(root).project_id


class BatchExecutor:
    """Executes one batch end to end through the real machinery: dispatch
    through the real filesystem lab adapter, scripted lab result, status and
    collection, durable Run record, artifact manifest and batch result
    record -- plus the run-level audit events."""

    def __init__(
        self,
        root: Path,
        *,
        project_id: str,
        goal_id: str,
        clock: FakeClock,
    ) -> None:
        self._root = root
        self._project_id = project_id
        self._goal_id = goal_id
        self._clock = clock
        self._adapter = FilesystemLabAdapter(root / "lab")
        self._runs = FilesystemStateBackend(root)
        self._manifests = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
        self._log = ProjectEventLog(root)

    def execute(
        self,
        *,
        run_id: str,
        package_id: str,
        artifact_id: str,
        result_id: str,
        value: float,
        deviations: list[dict[str, object]] | None = None,
    ) -> tuple[BatchExecution, CollectionResult]:
        """Run one batch: dispatch, collect the scripted lab result, record
        the durable Run and the batch result record."""
        stamp = self._clock()
        package = LabExecutionPackage(
            package_id=package_id,
            project_id=self._project_id,
            goal_id=self._goal_id,
            run_id=run_id,
            objective=(
                "measure the C3H6 single-component uptake at 298 K / 1 bar"
                " per the frozen protocol ANL-030"
            ),
            procedure=[
                {
                    "step": 1,
                    "action": (
                        "equilibrate the activated sample at 298 K and dose"
                        " C3H6 to 1 bar"
                    ),
                }
            ],
            required_return=list(REQUIRED_RETURN),
        )
        dispatch = self._adapter.dispatch(package, dispatched_at=stamp)
        run = Run(
            run_id=run_id,
            goal_id=self._goal_id,
            run_type=RunType.INDEPENDENT_REPLICATE,
            lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
            goal_version=GOAL_VERSION,
            external=RunExternal(
                backend="filesystem", dispatch_id=dispatch.dispatch_id
            ),
            artifacts=[],
            deviations=deviations or [],
            engineering_retries=[],
            created_at=stamp,
            updated_at=stamp,
        )
        self._runs.write("run", run_id, run.to_dict())
        self._log.append(
            ProjectEvent(
                event_id=generate_id("event", RUN_DISPATCHED_EVENT_TYPE, run_id),
                timestamp=stamp,
                actor="experiment-worker",
                event_type=RUN_DISPATCHED_EVENT_TYPE,
                object_id=self._goal_id,
                run_id=run_id,
                to=LifecycleState.RUNNING_EXTERNAL.value,
                reason="package dispatched through the filesystem lab adapter",
                payload={
                    "dispatch_id": dispatch.dispatch_id,
                    "backend": "filesystem",
                },
            )
        )

        # The lab returns the result package in the incoming handoff.
        script_result_package(self._root / "lab", run_id, package_id, value)
        status = self._adapter.status(dispatch.dispatch_id)
        assert status.state is DispatchState.RESULT_AVAILABLE
        collection = self._adapter.collect(dispatch.dispatch_id)

        completed = replace(run, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
        self._runs.write("run", run_id, completed.to_dict())
        self._log.append(
            ProjectEvent(
                event_id=generate_id("event", RUN_COMPLETED_EVENT_TYPE, run_id),
                timestamp=stamp,
                actor="experiment-worker",
                event_type=RUN_COMPLETED_EVENT_TYPE,
                object_id=self._goal_id,
                run_id=run_id,
                from_=LifecycleState.RUNNING_EXTERNAL.value,
                to=LifecycleState.RESULT_AVAILABLE.value,
                reason="result package collected from the lab handoff",
                payload={
                    "manifest_version": RESULT_MANIFEST_VERSION,
                    "files": list(REQUIRED_RETURN),
                },
            )
        )

        manifest = ArtifactManifest(
            artifact_id=artifact_id,
            uri=f"file:///raw/{artifact_id}.csv",
            sha256="a" * 64,
            size_bytes=1024,
            created_at=stamp,
            run_id=run_id,
            producer="lab",
        )
        self._manifests.register(manifest)
        result = register_result(
            self._root,
            ResultRecord(
                result_id=result_id,
                analysis_id=PROTOCOL_ID,
                protocol_version=GOAL_VERSION,
                run_ref=run_id,
                input_artifact_ids=[artifact_id],
                primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
                acceptance_ref=ACCEPTANCE_ID,
                requirement_refs=[REQUIREMENT_ID],
                metrics=[{"metric": "batch_level_uptake", "value": value}],
                qc_findings=[
                    f"batch {run_id} executed; raw value {value:g} cm3 g-1"
                    " carried into the replicate analysis"
                ],
            ),
        )
        return BatchExecution(
            run_id=run_id,
            package_id=package_id,
            artifact_id=artifact_id,
            result_id=result_id,
            value=value,
            dispatch=dispatch,
            run=completed,
            collection=collection,
            result=result,
        ), collection

    def transition(self, run_id: str, to: LifecycleState) -> None:
        """The durable run lifecycle transition RESULT_AVAILABLE -> ANALYZING
        -> SUBMITTED_FOR_REVIEW -> CLOSED (CC-EXPERIMENT execution-validity
        requirement), each with its audit event."""
        stamp = self._clock()
        record = self._runs.read("run", run_id)
        run = Run.from_dict(record)
        updated = replace(run, lifecycle_state=to, updated_at=stamp)
        self._runs.write("run", run_id, updated.to_dict())
        self._log.append(
            ProjectEvent(
                event_id=generate_id(
                    "event", RUN_TRANSITION_EVENT_TYPE, run_id, to.value
                ),
                timestamp=stamp,
                actor="supervisor",
                event_type=RUN_TRANSITION_EVENT_TYPE,
                object_id=self._goal_id,
                run_id=run_id,
                from_=run.lifecycle_state.value,
                to=to.value,
                reason="run lifecycle closed per CC-EXPERIMENT execution validity",
            )
        )


@dataclass(frozen=True)
class BatchExecution:
    """Everything one batch execution produced (frozen, auditable)."""

    run_id: str
    package_id: str
    artifact_id: str
    result_id: str
    value: float
    dispatch: DispatchRecord
    run: Run
    collection: CollectionResult
    result: ResultRecord


@dataclass(frozen=True)
class ScenarioAResult:
    """Everything the executed scenario produced (frozen, auditable)."""

    root: Path
    project_id: str
    batches: tuple[float, ...]
    mean: float
    standard_error: float
    effect: float
    ci: ConfidenceInterval
    bounds: EquivalenceBounds
    verdict: object  # EquivalenceAssessment
    criterion: object  # ReplicateCriterion
    assessment: object  # ReplicateSufficiencyAssessment
    analysis_record: ResultRecord
    requirement: ReproductionRequirement
    requirement_state: object  # RequirementClosureState
    requirement_rule_id: str
    project_outcome: ReproductionOutcome
    project_rule_id: str
    reproducibility: MethodReproducibility
    reproducibility_rule_id: str
    events: list[EventRecord]
    clock: FakeClock


def execute_scenario_a(root: Path) -> ScenarioAResult:
    """Execute scenario A end to end and return the full evidence trail.

    The Supervisor of the scenario is a deterministic role actor over the
    real machinery: it reads the registered analysis evidence, decides
    REQUIREMENT_CLOSURE, closes the Requirement record with the final
    outcome, aggregates the project outcome and method reproducibility
    with the real rule tables, and records every step as an audit event.
    """
    clock = FakeClock()
    project_id = init_project(root)

    # -- planning freeze ----------------------------------------------------
    register_analysis_record(root, make_protocol())
    draft = read_analysis_protocol(root, PROTOCOL_ID)
    freeze_result = freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    assert freeze_result.frozen_record.frozen
    assert freeze_result.frozen_record.protocol_version == GOAL_VERSION
    register_acceptance(root, make_acceptance())
    register_closure_contract(root, make_closure_contract())
    register_goal(root, make_goal())
    register_inventory_item(root, make_inventory_item())

    log = ProjectEventLog(root)
    log.append(
        ProjectEvent(
            event_id=generate_id("event", PLAN_FROZEN_EVENT_TYPE, GOAL_ID, GOAL_VERSION),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=PLAN_FROZEN_EVENT_TYPE,
            object_id=GOAL_ID,
            reason="frozen goal contract GOAL-EXE-50 registered",
            payload={"version": GOAL_VERSION},
        )
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", INVENTORY_AUDIT_EVENT_TYPE, REQUIREMENT_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=INVENTORY_AUDIT_EVENT_TYPE,
            object_id=REQUIREMENT_ID,
            reason="inventory item INV-0301 registered from the benchmark file",
            payload={"item_status": "INVENTORIED-DEFERRED"},
        )
    )

    # -- execution: three valid independent runs ---------------------------
    executor = BatchExecutor(root, project_id=project_id, goal_id=GOAL_ID, clock=clock)
    executions: list[BatchExecution] = []
    for run_id, package_id, artifact_id, result_id, value in zip(
        RUN_IDS, PACKAGE_IDS, ARTIFACT_IDS, RESULT_IDS, A_BATCHES, strict=True
    ):
        execution, _collection = executor.execute(
            run_id=run_id,
            package_id=package_id,
            artifact_id=artifact_id,
            result_id=result_id,
            value=value,
        )
        executions.append(execution)

    # -- analysis under the frozen acceptance -------------------------------
    acceptance = read_acceptance(root, ACCEPTANCE_ID)
    bounds = equivalence_bounds_from_acceptance(acceptance)
    criterion = replicate_criterion_from_acceptance(acceptance)
    batches = tuple(
        read_result(root, result_id).metrics[0]["value"]
        for result_id in RESULT_IDS
    )
    assessment = evaluate_replicate_sufficiency(
        batches,
        min_independent=criterion.min_independent,
        precision_threshold=criterion.precision_threshold,
    )
    standard_error = assessment.state.standard_error
    assert standard_error is not None  # three independent batches: n >= 2
    effect = assessment.state.mean - TARGET
    ci = effect_confidence_interval(effect, standard_error)
    verdict = decide_equivalence(effect, ci, bounds)
    metrics = effect_metrics("uptake_effect", effect, ci)
    metrics += sufficiency_metrics("uptake", assessment)
    analysis_record = register_result(
        root,
        ResultRecord(
            result_id=ANALYSIS_RESULT_ID,
            analysis_id=PROTOCOL_ID,
            protocol_version=GOAL_VERSION,
            run_ref=RUN_IDS[0],
            input_artifact_ids=[ARTIFACT_IDS[0]],
            primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
            acceptance_ref=ACCEPTANCE_ID,
            requirement_refs=[REQUIREMENT_ID],
            metrics=metrics,
            uncertainty=sufficiency_uncertainty_payload(assessment),
            qc_findings=sufficiency_findings(assessment),
            warnings=[
                f"equivalence decision {verdict.verdict.value}"
                f" ({verdict.matched_rule_id}): the effect confidence"
                " interval lies inside the frozen equivalence bounds"
            ],
        ),
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", ANALYSIS_COMPLETED_EVENT_TYPE, ANALYSIS_RESULT_ID
            ),
            timestamp=FIXED_STAMP,
            actor="analysis-worker",
            event_type=ANALYSIS_COMPLETED_EVENT_TYPE,
            object_id=GOAL_ID,
            run_id=RUN_IDS[0],
            reason="analysis result registered under the frozen protocol ANL-030",
            payload={
                "analysis_id": PROTOCOL_ID,
                "protocol_version": GOAL_VERSION,
                "equivalence_rule_id": verdict.matched_rule_id,
                "equivalence_verdict": verdict.verdict.value,
                "sufficiency_status": assessment.status.value,
            },
        )
    )

    # -- run lifecycle closed per CC-EXPERIMENT execution validity ----------
    for run_id in RUN_IDS:
        executor.transition(run_id, LifecycleState.ANALYZING)
        executor.transition(run_id, LifecycleState.SUBMITTED_FOR_REVIEW)
        executor.transition(run_id, LifecycleState.CLOSED)

    # -- supervisor closure (no recovery anywhere) --------------------------
    decision = SupervisorDecision(
        decision_id=CLOSURE_DECISION_ID,
        decision_type=DecisionType.REQUIREMENT_CLOSURE,
        actor="supervisor",
        timestamp=FIXED_STAMP,
        affected_refs=[REQUIREMENT_ID, GOAL_ID],
        rationale=(
            "requirement INV-0301 closes REPRODUCED: the three valid"
            " independent runs reproduce the published C3H6 uptake within"
            " the frozen 10 % band (R-EQ-1) with statistically sufficient"
            " evidence; no Recovery is entered"
        ),
        evidence_refs=[ANALYSIS_RESULT_ID],
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", SUPERVISOR_DECISION_EVENT_TYPE, CLOSURE_DECISION_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=SUPERVISOR_DECISION_EVENT_TYPE,
            object_id=GOAL_ID,
            reason="supervisor records the requirement-closure decision",
            payload=decision.to_dict(),
        )
    )

    requirement = register_requirement(
        root,
        ReproductionRequirement(
            requirement_id=REQUIREMENT_ID,
            statement=(
                "C3H6 single-component uptake at 298 K, 1 bar"
                " (INV-0301, benchmark title)"
            ),
            inventory_items=[REQUIREMENT_ID],
            criticality=Criticality.CRITICAL,
            goal_ids=[GOAL_ID],
            outcome=RequirementOutcome.REPRODUCED,
            method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
        ),
    )
    record = RequirementOutcomeRecord(
        requirement_id=REQUIREMENT_ID,
        criticality=Criticality.CRITICAL,
        outcome=RequirementOutcome.REPRODUCED,
    )
    requirement_assessment = classify_requirement_outcome(record)
    project_assessment = aggregate_project_outcome([record])
    reproducibility_assessment = aggregate_method_reproducibility(
        [
            MethodReproducibilityRecord(
                requirement_id=REQUIREMENT_ID,
                reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
            )
        ]
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", REQUIREMENT_CLOSED_EVENT_TYPE, REQUIREMENT_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=REQUIREMENT_CLOSED_EVENT_TYPE,
            object_id=REQUIREMENT_ID,
            reason="requirement INV-0301 closed REPRODUCED without Recovery",
            payload={
                "outcome": RequirementOutcome.REPRODUCED.value,
                "method_reproducibility": MethodReproducibility.DIRECTLY_REPRODUCIBLE.value,
                "requirement_rule_id": requirement_assessment.matched_rule_id,
                "decision_id": CLOSURE_DECISION_ID,
            },
        )
    )
    log.append(
        ProjectEvent(
            event_id=generate_id("event", PROJECT_OUTCOME_EVENT_TYPE, "project"),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=PROJECT_OUTCOME_EVENT_TYPE,
            object_id=REQUIREMENT_ID,
            reason="project outcome aggregated from the closed requirements",
            payload={
                "reproduction_outcome": project_assessment.outcome.value,
                "project_rule_id": project_assessment.matched_rule_id,
                "method_reproducibility": (
                    reproducibility_assessment.reproducibility.value
                ),
            },
        )
    )

    return ScenarioAResult(
        root=root,
        project_id=project_id,
        batches=batches,
        mean=assessment.state.mean,
        standard_error=standard_error,
        effect=effect,
        ci=ci,
        bounds=bounds,
        verdict=verdict,
        criterion=criterion,
        assessment=assessment,
        analysis_record=analysis_record,
        requirement=requirement,
        requirement_state=requirement_assessment.state,
        requirement_rule_id=requirement_assessment.matched_rule_id,
        project_outcome=project_assessment.outcome,
        project_rule_id=project_assessment.matched_rule_id,
        reproducibility=reproducibility_assessment.reproducibility,
        reproducibility_rule_id=reproducibility_assessment.matched_rule_id,
        events=log.list_events(),
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Scenario helpers (same conventions as the house scenario tests)
# ---------------------------------------------------------------------------


def tree_bytes(root: Path) -> bytes:
    """The byte-identical snapshot of the durable state tree, with the
    workspace's own absolute path normalized out (the real adapter records
    the injected outgoing path on the dispatch record, which is
    workspace-dependent but never scenario-dependent; it appears both plain
    and JSON-escaped, i.e. with doubled backslashes). The git working tree
    created by ``initialize_project`` (``.git/``) is excluded: it is
    internal repository metadata, not scenario state."""
    raw_root = str(root).encode("utf-8")
    escaped_root = raw_root.replace(b"\\", b"\\\\")
    chunks: list[bytes] = []
    for p in sorted(
        (p for p in root.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(root).as_posix(),
    ):
        if ".git" in p.relative_to(root).parts:
            continue
        chunks.append(
            p.read_bytes()
            .replace(escaped_root, b"<workspace>")
            .replace(raw_root, b"<workspace>")
        )
    return b"\n".join(chunks)


def event_records(root: Path) -> list[Path]:
    """The persisted scenario event files, in sequence-file name order."""
    return sorted(
        (root / "events").glob("*.json"), key=lambda p: p.name
    )


def run_files(root: Path) -> list[Path]:
    return sorted(
        (root / "runs").glob("*.json"), key=lambda p: p.name
    )


def goals(root: Path) -> list[Path]:
    return sorted((root / "goals").glob("*.json"), key=lambda p: p.name)


# ---------------------------------------------------------------------------
# Scenario A tests (each maps to AC-01 of DEV-M12-G05)
# ---------------------------------------------------------------------------


def test_A_ac01_frozen_benchmark_grounding_parses() -> None:
    """The scenario constants are the benchmark's own frozen values: the
    INV-0301 seed fact, the ASM-A1-TOL-01 band, the ASM-A1-N-01 floor and
    the CC-EXPERIMENT saturation rule -- not numbers invented here."""
    assert TARGET == 180.5
    assert MARGIN == pytest.approx(18.05)
    assert MIN_INDEPENDENT == 2
    assert REQUIRED_ZERO_NOVELTY_CYCLES == 2


def test_A_ac01_planning_freeze_registers_the_frozen_goal(tmp_path: Path) -> None:
    """The frozen goal contract registered from the benchmark record: track
    STRICT_REPRODUCTION, formal version v1, frozen, with the acceptance,
    protocol and closure refs of the benchmark file."""
    root = tmp_path / "scenario-a"
    execute_scenario_a(root)
    goal = read_goal(root, GOAL_ID)
    assert goal.track is GoalTrack.STRICT_REPRODUCTION
    assert goal.frozen
    assert goal.version == GOAL_VERSION
    assert goal.acceptance.criteria_ref == ACCEPTANCE_ID
    assert goal.analysis_protocol_ref == PROTOCOL_ID
    assert goal.closure_contract_ref == CLOSURE_ID
    assert REQUIREMENT_ID in goal.requirement_ids
    assert goal.replication.minimum_n == MIN_INDEPENDENT
    # the recovery track is never entered: no recovery goal is registered
    assert goals(root) == [root / "goals" / f"{GOAL_ID}.json"]
    assert [g.track for g in (read_goal(root, p.stem) for p in goals(root))] == [
        GoalTrack.STRICT_REPRODUCTION
    ]


def test_A_ac01_three_valid_independent_runs(tmp_path: Path) -> None:
    """AC-01 "valid independent Runs": three INDEPENDENT_REPLICATE runs
    dispatched through the real adapter, results returned at the adapter's
    external boundary, collected, and the durable run records closed."""
    root = tmp_path / "scenario-a"
    execute_scenario_a(root)
    assert len(event_records(root)) > 0
    assert len(run_files(root)) == 3
    store = FilesystemStateBackend(root)
    for run_id in RUN_IDS:
        run = Run.from_dict(store.read("run", run_id))
        assert run.goal_id == GOAL_ID
        assert run.run_type is RunType.INDEPENDENT_REPLICATE
        assert run.lifecycle_state is LifecycleState.CLOSED
        assert run.external is not None and run.external.backend == "filesystem"
        # the collection really happened: status resolves RESULT_AVAILABLE
        adapter = FilesystemLabAdapter(root / "lab")
        status = adapter.status(run.external.dispatch_id)
        assert status.state is DispatchState.RESULT_AVAILABLE
        collection = adapter.collect(run.external.dispatch_id)
        assert collection.manifest.run_id == run_id
        assert collection.manifest.goal_id == GOAL_ID
        assert collection.manifest.files == tuple(REQUIRED_RETURN)
        assert collection.manifest.manifest_version == RESULT_MANIFEST_VERSION


def test_A_ac01_analysis_equivalence_and_sufficiency(tmp_path: Path) -> None:
    """AC-01 "Analysis supports frozen acceptance": the real statistics
    stack decides EQUIVALENT (R-EQ-1) with the mean interval entirely inside
    the frozen 10 % band, statistically SUFFICIENT under the frozen floor,
    and the analysis result record persists the evidence."""
    root = tmp_path / "scenario-a"
    result = execute_scenario_a(root)
    # the analysis input provably comes from the registered batch records
    assert result.batches == pytest.approx(A_BATCHES)
    assert result.mean == pytest.approx(180.5)
    assert result.ci.lower >= result.bounds.lower
    assert result.ci.upper <= result.bounds.upper
    assert result.verdict.verdict is EquivalenceVerdict.EQUIVALENT
    assert result.verdict.matched_rule_id == "R-EQ-1"
    assert result.assessment.sufficient
    assert result.assessment.status == ReplicateStatus.SUFFICIENT
    assert result.assessment.relative_half_width < PRECISION
    assert result.assessment.requested_additional_runs == 0
    recorded = read_result(root, ANALYSIS_RESULT_ID)
    assert recorded.protocol_version == GOAL_VERSION
    assert recorded.acceptance_ref == ACCEPTANCE_ID
    metric_names = [m["metric"] for m in recorded.metrics]
    assert "uptake_effect" in metric_names
    assert "uptake_relative_half_width" in metric_names
    assert recorded.uncertainty["n"] == 3
    assert recorded.uncertainty["mean"] == pytest.approx(180.5)
    assert recorded.uncertainty["confidence_level"] == 0.95
    assert recorded.qc_findings == sufficiency_findings(result.assessment)


def test_A_ac01_requirement_closed_reproduced_without_recovery(
    tmp_path: Path,
) -> None:
    """AC-01 "Supervisor closes Goal/Requirement reproduced; expected no
    Recovery": the requirement record closes REPRODUCED with method
    reproducibility DIRECTLY_REPRODUCIBLE; the outcome rules match
    R-REQOUT-1 / R-PRJ-1 / R-MR-3; no RECOVERY_ENTRY decision and no
    recovery-created event exist anywhere."""
    root = tmp_path / "scenario-a"
    result = execute_scenario_a(root)
    requirement = read_requirement(root, REQUIREMENT_ID)
    assert requirement.outcome is RequirementOutcome.REPRODUCED
    assert requirement.method_reproducibility is MethodReproducibility.DIRECTLY_REPRODUCIBLE
    assert requirement.criticality is Criticality.CRITICAL
    assert requirement.inventory_items == [REQUIREMENT_ID]
    assert requirement.goal_ids == [GOAL_ID]
    assert result.requirement_rule_id == "R-REQOUT-1"
    assert result.project_outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert result.project_rule_id == "R-PRJ-1"
    assert result.reproducibility is MethodReproducibility.DIRECTLY_REPRODUCIBLE
    assert result.reproducibility_rule_id == "R-MR-3"
    # no recovery anywhere in the audit chain
    for record in result.events:
        assert record.event.event_type != "recovery.created"
        decision_type = record.event.payload.get("decision_type")
        assert decision_type != DecisionType.RECOVERY_ENTRY.value, (
            f"unexpected recovery entry: {record.event.event_type}"
            f" {record.event.payload}"
        )


def test_A_ac01_audit_chain_complete(tmp_path: Path) -> None:
    """AC-01 "audit chain complete": the event log holds the full lifecycle
    in sequence -- goal freeze, inventory registration, three dispatches,
    three completions, the run lifecycle transitions, analysis, the
    supervisor closure decision, requirement closure and project outcome --
    and the governance events map through the real audit mapper."""
    root = tmp_path / "scenario-a"
    result = execute_scenario_a(root)
    events = result.events
    assert events == sorted(events, key=lambda record: record.sequence)
    types = [record.event.event_type for record in events]
    assert types.count(PLAN_FROZEN_EVENT_TYPE) == 1
    assert types.count(INVENTORY_AUDIT_EVENT_TYPE) == 1
    assert types.count(RUN_DISPATCHED_EVENT_TYPE) == 3
    assert types.count(RUN_COMPLETED_EVENT_TYPE) == 3
    assert types.count(RUN_TRANSITION_EVENT_TYPE) == 9
    assert types.count(ANALYSIS_COMPLETED_EVENT_TYPE) == 1
    assert types.count(SUPERVISOR_DECISION_EVENT_TYPE) == 1
    assert types.count(REQUIREMENT_CLOSED_EVENT_TYPE) == 1
    assert types.count(PROJECT_OUTCOME_EVENT_TYPE) == 1
    # the governance events map through the real audit mapper
    closure_event = next(
        record.event
        for record in events
        if record.event.event_type == REQUIREMENT_CLOSED_EVENT_TYPE
    )
    mapping = map_event_to_audit(closure_event)
    assert mapping.kind == "requirement.closed"
    outcome_event = next(
        record.event
        for record in events
        if record.event.event_type == PROJECT_OUTCOME_EVENT_TYPE
    )
    assert map_event_to_audit(outcome_event).kind == "project.outcome"


def test_A_ac01_deterministic_replay(tmp_path: Path) -> None:
    """AC-01 determinism: executing the scenario twice in separate
    workspaces produces byte-identical durable state and identical
    outcomes (fixed clock, fixed seeds, scripted lab return only)."""
    first = execute_scenario_a(tmp_path / "first")
    second = execute_scenario_a(tmp_path / "second")
    assert tree_bytes(first.root) == tree_bytes(second.root)
    assert first.batches == second.batches
    assert first.verdict.matched_rule_id == second.verdict.matched_rule_id == "R-EQ-1"
    assert first.assessment.sufficient == second.assessment.sufficient is True
    assert first.requirement_rule_id == second.requirement_rule_id == "R-REQOUT-1"
    assert first.project_outcome is second.project_outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert first.clock.calls == second.clock.calls
    assert len(first.clock.calls) > 0  # the clock was really used
