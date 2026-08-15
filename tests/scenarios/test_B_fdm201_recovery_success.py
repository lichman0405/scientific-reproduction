"""Scenario B end-to-end test: strict fail -> recovery success
(DEV-M12-G05 AC-02).

Covers 18-TEST-AND-ACCEPTANCE-PLAN.md section 3, scenario B, executed
entirely through the real machinery: the GOAL-EXE-50 execution of the
frozen FDM-201 benchmark (DOI 10.1039/D5TA00771B) fails strict
reproduction and closes REPRODUCED_WITH_RECOVERY --

* three valid independent Runs fail strict reproduction with
  **statistically sufficient** evidence (NOT_EQUIVALENT under the frozen
  10 % band, SUFFICIENT under the frozen floor);
* the **Diagnosis Worker** runs against the frozen goal through the real
  context machinery (``generate_goal_context``) and registers its result
  package through the real worker-result registry;
* the Supervisor issues a **Research Request** through the real
  supervisor-only API and the search cycle produces an **eligible
  hypothesis** (real ``recovery_hypothesis_eligible`` gate, real
  ``track_new_eligible_hypotheses`` novelty tracking, real saturation
  record);
* a **versioned Recovery Goal** (track RECOVERY, parent GOAL-EXE-50,
  frozen ``v1``) is registered and the Recovery Runs -- labeled with the
  recovery hypothesis -- pass strict acceptance;
* the Supervisor closes the Requirement INV-0301 as
  REPRODUCED_WITH_RECOVERY with method reproducibility
  REPRODUCIBLE_WITH_MINOR_RECOVERY (real outcome rules: R-REQOUT-2,
  R-PRJ-1 FULLY_REPRODUCED, R-MR-3 worst-of) -- the recovery-level
  evidence is measurably less precise than the direct evidence (the
  recovery mean interval is wider), grounding "lower method
  reproducibility".

Every benchmark-derived constant is parsed at import time from the
frozen benchmark files under ``benchmarks/fdm201/``, all timestamps come
from a FakeClock fixed to one stamp, and all ids are deterministic, so
the scenario is byte-deterministic (the replay test compares the full
durable state tree).
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
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ArtifactManifest,
    ClaimSpecificEvidence,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Criticality,
    DecisionMode,
    DecisionType,
    DependencyType,
    EvidenceAssessment,
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
    ReproductionOutcome,
    ReproductionRequirement,
    RequirementOutcome,
    Run,
    RunExternal,
    RunType,
    SupervisorDecision,
    WorkerRole,
)
from scientific_reproduction.core.rules.outcome import (
    MethodReproducibilityRecord,
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
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.research.requests import issue_research_request
from scientific_reproduction.research.saturation import (
    HypothesisCandidate,
    SaturationRecord,
    SearchCycle,
    evaluate_saturation,
    track_new_eligible_hypotheses,
)
from scientific_reproduction.workers.context import generate_goal_context
from scientific_reproduction.workers.results import (
    DeviationType,
    WorkerDeviation,
    WorkerFact,
    WorkerResultPackage,
    register_worker_result,
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

#: Frozen goal / acceptance / protocol / closure ids of the benchmark and
#: the requirement the scenario closes. GOAL-EXE-50 depends on the
#: synthesis/characterization/activation goals GOAL-EXE-20/30/40 (hard
#: gate for EXE-20, soft gates for EXE-30/40); the diagnosis context
#: resolves every dependency's declared outputs through the real registry.
GOAL_ID = "GOAL-EXE-50"
DEPENDENCY_GOAL_IDS = ("GOAL-EXE-20", "GOAL-EXE-30", "GOAL-EXE-40")
DEPENDENCY_ACCEPTANCE_IDS = {
    "GOAL-EXE-20": "ACC-2",
    "GOAL-EXE-30": "ACC-3",
    "GOAL-EXE-40": "ACC-4",
}
RECOVERY_GOAL_ID = "GOAL-EXE-50-R1"
ACCEPTANCE_ID = "ACC-1"
PROTOCOL_ID = "ANL-030"
CLOSURE_ID = "CC-EXPERIMENT"
REQUIREMENT_ID = "INV-0301"
GOAL_VERSION = "v1"

#: The scenario's registry ids (deterministic; no generated-id collisions).
STRICT_RUN_IDS = ("RUN-001", "RUN-002", "RUN-003")
STRICT_PACKAGE_IDS = ("PKG-001", "PKG-002", "PKG-003")
STRICT_ARTIFACT_IDS = ("ART-001", "ART-002", "ART-003")
STRICT_RESULT_IDS = ("RES-B1", "RES-B2", "RES-B3")
STRICT_ANALYSIS_RESULT_ID = "RES-B-ANL-1"
RECOVERY_RUN_IDS = ("RUN-004", "RUN-005", "RUN-006")
RECOVERY_PACKAGE_IDS = ("PKG-004", "PKG-005", "PKG-006")
RECOVERY_ARTIFACT_IDS = ("ART-004", "ART-005", "ART-006")
RECOVERY_RESULT_IDS = ("RES-B-R1", "RES-B-R2", "RES-B-R3")
RECOVERY_ANALYSIS_RESULT_ID = "RES-B-ANL-R"

#: The diagnosis worker result id and the research artifacts.
DIAGNOSIS_RESULT_ID = "RES-B-DX"
RESEARCH_REQUEST_ID = "REQ-R-001"
RECOVERY_HYPOTHESIS = "HY-0001"
RECOVERY_DECISION_ID = "DEC-B2"
CLOSURE_DECISION_ID = "DEC-B3"

#: The raw data file the lab result package declares (test_H convention).
REQUIRED_RETURN = ["uptake.csv"]

#: Strict-track batches: three valid independent runs whose mean deviates
#: ~17 % from the published target -- outside the frozen 10 % band, with
#: tight enough spread to be statistically sufficient evidence of failure.
STRICT_BATCHES = (150.0, 152.0, 148.0)

#: Recovery-track batches: the recovery hypothesis predicts a temperature
#: calibration offset; corrected batches land inside the frozen band with
#: wider spread than the strict-track batches (recovery-level evidence,
#: measurably less precise than the direct evidence).
RECOVERY_BATCHES = (176.5, 181.5, 180.5)

#: Audit event types of the scenario (canonical governance vocabulary from
#: audit/git.py where it exists; scenario vocabulary elsewhere).
PLAN_FROZEN_EVENT_TYPE = "plan.frozen"
INVENTORY_AUDIT_EVENT_TYPE = "inventory.audit.passed"
RUN_DISPATCHED_EVENT_TYPE = "run.dispatched"
RUN_COMPLETED_EVENT_TYPE = "run.completed"
ANALYSIS_COMPLETED_EVENT_TYPE = "analysis.completed"
DIAGNOSIS_COMPLETED_EVENT_TYPE = "diagnosis.completed"
RESEARCH_REQUEST_EVENT_TYPE = "research.request"
RESEARCH_CYCLE_EVENT_TYPE = "research.cycle"
SUPERVISOR_DECISION_EVENT_TYPE = "supervisor.decision"
RECOVERY_CREATED_EVENT_TYPE = "recovery.created"
REQUIREMENT_CLOSED_EVENT_TYPE = "requirement.closed"
PROJECT_OUTCOME_EVENT_TYPE = "project.outcome.recorded"

#: The claim-specific evidence of the recovery hypothesis (R 4, D 3) and
#: its ineligible contrast (R 2 -- below the frozen R >= 3 gate).
EVIDENCE_IDS = ("EV-0001", "EV-0002")


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
#: policy of 07-STATISTICS-AND-ACCEPTANCE.md SS8/SS9).
PRECISION = 0.1


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
        "project_id": "scenario-b",
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
    same shape as the frozen benchmark record)."""
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
    """The frozen EQUIVALENCE acceptance record of scenario B (same frozen
    criteria as the benchmark acceptance design)."""
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


def make_goal(goal_id: str = GOAL_ID, *, recovery: bool = False) -> GoalContract:
    """The frozen GOAL-EXE-50 goal contract (or its versioned Recovery
    track GOAL-EXE-50-R1, or the dependency goal GOAL-EXE-20), built from
    the benchmark goals file."""
    goals = _load_yaml("goals/goals.yaml")["goals"]
    record = next(g for g in goals if g["goal_id"] == goal_id)
    if not recovery:
        return GoalContract(
            goal_id=goal_id,
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
            acceptance=GoalAcceptance(
                criteria_ref=(
                    DEPENDENCY_ACCEPTANCE_IDS.get(goal_id, ACCEPTANCE_ID)
                ),
                frozen=True,
            ),
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
    return GoalContract(
        goal_id=RECOVERY_GOAL_ID,
        title="Single-component C3H6/C2H4 adsorption isotherms -- recovery track",
        unit_process_type=record["unit_process_type"],
        track=GoalTrack.RECOVERY,
        objective=(
            "re-attempt the 298 K / 1 bar C3H6 uptake under the recovery"
            " hypothesis HY-0001 (temperature calibration offset); the"
            " recovery goal is versioned v1 and references its strict"
            " parent GOAL-EXE-50"
        ),
        requirement_ids=[REQUIREMENT_ID],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref=ACCEPTANCE_ID, frozen=True),
        analysis_protocol_ref=record["analysis_protocol_ref"],
        replication=GoalReplication(
            independent_required=record["replication"]["independent_required"],
            planned_n_policy=record["replication"]["planned_n_policy"],
            minimum_n=record["replication"]["minimum_n"],
        ),
        version=GOAL_VERSION,
        frozen=True,
        parent_goal_id=GOAL_ID,
        resource_ids=list(record["resource_ids"]),
        assumption_ids=["ASM-A1-N-01", "ASM-A1-TOL-01"],
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


def make_goal_acceptance(goal_id: str) -> AcceptanceCriteria:
    """The primary acceptance record of one dependency goal (its AC-01
    criterion text from the frozen goals file). The benchmark encodes
    acceptance inline per goal; the registry requires a ``criteria_ref``,
    so the scenario registers one acceptance record per goal (the uptake
    goal's ACC-1 mirrors its own criteria the same way)."""
    goals = _load_yaml("goals/goals.yaml")["goals"]
    record = next(g for g in goals if g["goal_id"] == goal_id)
    return AcceptanceCriteria(
        acceptance_id=DEPENDENCY_ACCEPTANCE_IDS[goal_id],
        goal_id=goal_id,
        version=GOAL_VERSION,
        frozen=True,
        decision_mode=DecisionMode.CATEGORICAL,
        criteria=[
            {
                "metric": "protocol_conformance",
                "criterion": record["acceptance_criteria"][0],
            }
        ],
        rationale=(
            f"primary acceptance criterion AC-01 of the frozen {goal_id}"
            " record (protocol conformance); the benchmark's inline"
            " acceptance text is registered here because the goal contract"
            " requires a criteria_ref"
        ),
    )


def make_inventory_item() -> ReproductionInventoryItem:
    """The INV-0301 inventory item, built from the benchmark inventory file."""
    items = _load_yaml("inventory/INVENTORY.yaml")["items"]
    record = next(i for i in items if i["item_id"] == REQUIREMENT_ID)
    provenance = record["provenance"]
    # The benchmark file's ``category`` is the spec's letter classification;
    # the frozen inventory-item schema takes the ``item_type`` vocabulary.
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


def make_evidence_registry() -> EvidenceRegistry:
    """The claim-specific evidence registry of the diagnosis: the recovery
    hypothesis's evidence (R 4 / D 3 -- eligible under the frozen gate) and
    an ineligible contrast (R 2 -- below the R >= 3 gate)."""
    eligible = ClaimSpecificEvidence(
        evidence_id=EVIDENCE_IDS[0],
        source_id="SRC-001",
        claim_id="CLAIM-0001",
        finding=(
            "measured batch uptakes deviate -16.9 % from the published C3H6"
            " value; the reported instrument temperature offset is a likely"
            " root cause"
        ),
        assessment=EvidenceAssessment(
            authority=3,
            reliability=4,
            directness=3,
            reliability_checklist_ref="CL-0001",
        ),
        source_location="17-FDM201-REFERENCE-CASE.md section 2",
        role="diagnosis",
        used_by=[GOAL_ID, REQUIREMENT_ID],
    )
    ineligible = ClaimSpecificEvidence(
        evidence_id=EVIDENCE_IDS[1],
        source_id="SRC-002",
        claim_id="CLAIM-0002",
        finding="sample purity hypothesis (anecdotal, unverified)",
        assessment=EvidenceAssessment(
            authority=2,
            reliability=2,
            directness=2,
            reliability_checklist_ref="CL-0001",
        ),
        source_location="internal note",
        role="diagnosis",
        used_by=[GOAL_ID],
    )
    return EvidenceRegistry().register(eligible).register(ineligible)


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
    ) -> tuple[Run, CollectionResult]:
        """Run one batch: dispatch, collect the scripted lab result, record
        the durable Run and the batch result record; return the final Run."""
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
        register_result(
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
        return completed, collection

    def transition(self, run_id: str, to: LifecycleState) -> None:
        """The durable run lifecycle transition (CC-EXPERIMENT execution
        validity), each with its audit event."""
        stamp = self._clock()
        record = self._runs.read("run", run_id)
        run = Run.from_dict(record)
        updated = replace(run, lifecycle_state=to, updated_at=stamp)
        self._runs.write("run", run_id, updated.to_dict())
        self._log.append(
            ProjectEvent(
                event_id=generate_id(
                    "event", "run.transition", run_id, to.value
                ),
                timestamp=stamp,
                actor="supervisor",
                event_type="run.transition",
                object_id=self._goal_id,
                run_id=run_id,
                from_=run.lifecycle_state.value,
                to=to.value,
                reason="run lifecycle closed per CC-EXPERIMENT execution validity",
            )
        )


@dataclass(frozen=True)
class ScenarioBResult:
    """Everything the executed scenario produced (frozen, auditable)."""

    root: Path
    project_id: str
    strict_batches: tuple[float, ...]
    recovery_batches: tuple[float, ...]
    strict_verdict: object  # EquivalenceAssessment
    strict_assessment: object  # ReplicateSufficiencyAssessment
    recovery_verdict: object  # EquivalenceAssessment
    recovery_assessment: object  # ReplicateSufficiencyAssessment
    strict_effect: float
    recovery_effect: float
    strict_ci: ConfidenceInterval
    recovery_ci: ConfidenceInterval
    bounds: EquivalenceBounds
    diagnosis_context_id: str
    diagnosis_result: WorkerResultPackage
    research_request: object  # IssuedResearchRequest
    saturation: object  # SaturationAssessment
    recovery_goal: GoalContract
    recovery_runs: list[Run]
    requirement: ReproductionRequirement
    requirement_rule_id: str
    project_outcome: ReproductionOutcome
    project_rule_id: str
    reproducibility: MethodReproducibility
    reproducibility_rule_id: str
    events: list[EventRecord]
    clock: FakeClock


def init_project(root: Path) -> str:
    """Initialize one workspace under the fixed timestamp and identity and
    return its project id (house convention of the scenario tests)."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    state = read_project_state(root)
    return state.project_id


def _register_planning(root: Path, log: ProjectEventLog) -> None:
    """The deterministic planning freeze: protocol, acceptance, closure
    contract, frozen goal and inventory item, with their audit events."""
    register_analysis_record(root, make_protocol())
    draft = read_analysis_protocol(root, PROTOCOL_ID)
    freeze_result = freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    assert freeze_result.frozen_record.frozen
    # The dependency goals come first: the frozen GOAL-EXE-50 contract
    # declares their outputs as upstream results, and the diagnosis context
    # resolves every dependency through the real registry (an unregistered
    # dependency raises GoalNotFoundError).
    for dependency_id in DEPENDENCY_GOAL_IDS:
        register_acceptance(root, make_goal_acceptance(dependency_id))
        register_goal(root, make_goal(dependency_id))
    register_acceptance(root, make_acceptance())
    register_closure_contract(root, make_closure_contract())
    register_goal(root, make_goal())
    register_inventory_item(root, make_inventory_item())
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


def _analyze(
    root: Path,
    result_ids: tuple[str, ...],
    *,
    result_id: str,
    artifact_id: str,
) -> tuple[object, object, float, ConfidenceInterval, EquivalenceBounds]:
    """The shared analysis step: read the registered batch values, evaluate
    sufficiency under the frozen criterion and equivalence under the frozen
    bounds, register the analysis result record; return the verdict, the
    assessment, the effect, the interval and the bounds."""
    acceptance = read_acceptance(root, ACCEPTANCE_ID)
    bounds = equivalence_bounds_from_acceptance(acceptance)
    criterion = replicate_criterion_from_acceptance(acceptance)
    batches = tuple(read_result(root, rid).metrics[0]["value"] for rid in result_ids)
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
    register_result(
        root,
        ResultRecord(
            result_id=result_id,
            analysis_id=PROTOCOL_ID,
            protocol_version=GOAL_VERSION,
            run_ref=result_ids[0],
            input_artifact_ids=[artifact_id],
            primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
            acceptance_ref=ACCEPTANCE_ID,
            requirement_refs=[REQUIREMENT_ID],
            metrics=metrics,
            uncertainty=sufficiency_uncertainty_payload(assessment),
            qc_findings=sufficiency_findings(assessment),
            warnings=[
                f"equivalence decision {verdict.verdict.value}"
                f" ({verdict.matched_rule_id}): interval"
                f" [{ci.lower:.6g}, {ci.upper:.6g}] vs frozen band"
                f" [{bounds.lower:.6g}, {bounds.upper:.6g}]"
            ],
        ),
    )
    return verdict, assessment, effect, ci, bounds


def execute_scenario_b(root: Path) -> ScenarioBResult:
    """Execute scenario B end to end and return the full evidence trail."""
    clock = FakeClock()
    project_id = init_project(root)
    log = ProjectEventLog(root)
    _register_planning(root, log)

    # -- strict-track execution: statistically sufficient failure ------------
    strict_executor = BatchExecutor(
        root, project_id=project_id, goal_id=GOAL_ID, clock=clock
    )
    for run_id, package_id, artifact_id, result_id, value in zip(
        STRICT_RUN_IDS,
        STRICT_PACKAGE_IDS,
        STRICT_ARTIFACT_IDS,
        STRICT_RESULT_IDS,
        STRICT_BATCHES,
        strict=True,
    ):
        strict_executor.execute(
            run_id=run_id,
            package_id=package_id,
            artifact_id=artifact_id,
            result_id=result_id,
            value=value,
        )
    strict_verdict, strict_assessment, strict_effect, strict_ci, bounds = _analyze(
        root,
        STRICT_RESULT_IDS,
        result_id=STRICT_ANALYSIS_RESULT_ID,
        artifact_id=STRICT_ARTIFACT_IDS[0],
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", ANALYSIS_COMPLETED_EVENT_TYPE, STRICT_ANALYSIS_RESULT_ID
            ),
            timestamp=FIXED_STAMP,
            actor="analysis-worker",
            event_type=ANALYSIS_COMPLETED_EVENT_TYPE,
            object_id=GOAL_ID,
            run_id=STRICT_RUN_IDS[0],
            reason="strict-track analysis: NOT_EQUIVALENT with sufficient evidence",
            payload={
                "analysis_id": PROTOCOL_ID,
                "equivalence_rule_id": strict_verdict.matched_rule_id,
                "equivalence_verdict": strict_verdict.verdict.value,
                "sufficiency_status": strict_assessment.status.value,
            },
        )
    )
    for run_id in STRICT_RUN_IDS:
        strict_executor.transition(run_id, LifecycleState.CLOSED)

    # -- diagnosis worker against the frozen goal -----------------------------
    goal = read_goal(root, GOAL_ID)
    evidence_registry = make_evidence_registry()
    context = generate_goal_context(
        root,
        goal,
        worker_role=WorkerRole.DIAGNOSIS_WORKER,
        evidence_registry=evidence_registry,
    )
    diagnosis_result = register_worker_result(
        root,
        WorkerResultPackage(
            result_id=DIAGNOSIS_RESULT_ID,
            context_id=context.package.context_id,
            worker_role=WorkerRole.DIAGNOSIS_WORKER,
            goal_id=GOAL_ID,
            goal_version=GOAL_VERSION,
            run_ref=STRICT_RUN_IDS[0],
            facts=[
                WorkerFact(
                    fact_id="F-0001",
                    name="batch_uptake_relative_deviation",
                    value=-0.169,
                    unit="dimensionless",
                    requirement_refs=[REQUIREMENT_ID],
                )
            ],
            data=[],
            deviations=[
                WorkerDeviation(
                    deviation_id="DEV-B1",
                    kind=DeviationType.FAILURE,
                    description=(
                        "measured 298 K / 1 bar batch uptake 150.0 cm3 g-1"
                        " vs published 180.5 cm3 g-1: relative deviation"
                        " -16.9 %, outside the frozen 10 % band"
                    ),
                    requirement_refs=[REQUIREMENT_ID],
                )
            ],
            input_artifact_ids=[STRICT_ARTIFACT_IDS[0]],
            output_artifact_ids=[],
            decision_refs=[],
            completed_at=FIXED_STAMP,
        ),
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", DIAGNOSIS_COMPLETED_EVENT_TYPE, DIAGNOSIS_RESULT_ID
            ),
            timestamp=FIXED_STAMP,
            actor="diagnosis-worker",
            event_type=DIAGNOSIS_COMPLETED_EVENT_TYPE,
            object_id=GOAL_ID,
            run_id=STRICT_RUN_IDS[0],
            reason="diagnosis worker result registered against the frozen goal",
            payload={"result_id": DIAGNOSIS_RESULT_ID},
        )
    )

    # -- research request and the eligible recovery hypothesis ----------------
    research_request = issue_research_request(
        request_id=RESEARCH_REQUEST_ID,
        question=(
            "identify the root cause of the C3H6 298 K / 1 bar uptake"
            " deviation (-16.9 %) against the published INV-0301 value"
        ),
        origin_refs=[GOAL_ID, DIAGNOSIS_RESULT_ID],
        required_search_families=["uptake-discrepancy root causes"],
        minimum_reliability=3,
        minimum_directness=2,
        issued_at=FIXED_STAMP,
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", RESEARCH_REQUEST_EVENT_TYPE, RESEARCH_REQUEST_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=RESEARCH_REQUEST_EVENT_TYPE,
            object_id=GOAL_ID,
            reason="supervisor issued the research request (real API)",
            payload={
                "request_id": research_request.request.request_id,
                "question": research_request.request.question,
                "status": research_request.request.status.value,
                "required_search_families": list(
                    research_request.request.required_search_families
                ),
                "minimum_reliability": research_request.request.minimum_reliability,
                "minimum_directness": research_request.request.minimum_directness,
            },
        )
    )
    eligible_assessment = evidence_registry.get_assessment("SRC-001", "CLAIM-0001")
    assert eligible_assessment is not None
    novelty = track_new_eligible_hypotheses(
        [
            HypothesisCandidate(
                hypothesis_ref=RECOVERY_HYPOTHESIS,
                assessment=eligible_assessment,
            )
        ],
        known_eligible_hypotheses=[],
    )
    assert novelty.count == 1
    saturation = evaluate_saturation(
        SaturationRecord(
            cycles=(
                SearchCycle(
                    cycle_index=0,
                    search_family="uptake-discrepancy root causes",
                    completed=True,
                    new_eligible_hypotheses=novelty.count,
                ),
            ),
            required_search_families_completed=None,
        )
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", RESEARCH_CYCLE_EVENT_TYPE, "cycle-0"
            ),
            timestamp=FIXED_STAMP,
            actor="research-worker",
            event_type=RESEARCH_CYCLE_EVENT_TYPE,
            object_id=GOAL_ID,
            reason="search cycle 0: one novel eligible hypothesis",
            payload={
                "cycle_index": 0,
                "new_eligible_hypotheses": novelty.count,
                "hypotheses": [RECOVERY_HYPOTHESIS],
            },
        )
    )

    # -- versioned recovery goal ---------------------------------------------
    recovery_decision = SupervisorDecision(
        decision_id=RECOVERY_DECISION_ID,
        decision_type=DecisionType.RECOVERY_ENTRY,
        actor="supervisor",
        timestamp=FIXED_STAMP,
        affected_refs=[GOAL_ID, RECOVERY_GOAL_ID, REQUIREMENT_ID],
        rationale=(
            "enter the recovery track on the eligible hypothesis HY-0001"
            " (evidence gate R >= 3 and D >= 2 and scientifically"
            " actionable per CC-EXPERIMENT): versioned Recovery Goal"
            " GOAL-EXE-50-R1 with track RECOVERY"
        ),
        evidence_refs=[EVIDENCE_IDS[0]],
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", SUPERVISOR_DECISION_EVENT_TYPE, RECOVERY_DECISION_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=SUPERVISOR_DECISION_EVENT_TYPE,
            object_id=GOAL_ID,
            reason="supervisor records the recovery-entry decision",
            payload=recovery_decision.to_dict(),
        )
    )
    recovery_goal = register_goal(root, make_goal(recovery=True))
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", RECOVERY_CREATED_EVENT_TYPE, RECOVERY_GOAL_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=RECOVERY_CREATED_EVENT_TYPE,
            object_id=RECOVERY_GOAL_ID,
            reason="versioned Recovery Goal registered (track RECOVERY)",
            payload={
                "parent_goal_id": GOAL_ID,
                "track": GoalTrack.RECOVERY.value,
                "version": GOAL_VERSION,
                "hypothesis": RECOVERY_HYPOTHESIS,
            },
        )
    )

    # -- recovery runs: labeled with the hypothesis, then pass ---------------
    recovery_label = [
        {
            "kind": "recovery",
            "hypothesis_ref": RECOVERY_HYPOTHESIS,
            "reason": (
                "run executed under the versioned Recovery Goal on the"
                " eligible hypothesis HY-0001 (temperature calibration"
                " offset)"
            ),
        }
    ]
    recovery_executor = BatchExecutor(
        root, project_id=project_id, goal_id=RECOVERY_GOAL_ID, clock=clock
    )
    recovery_runs: list[Run] = []
    for run_id, package_id, artifact_id, result_id, value in zip(
        RECOVERY_RUN_IDS,
        RECOVERY_PACKAGE_IDS,
        RECOVERY_ARTIFACT_IDS,
        RECOVERY_RESULT_IDS,
        RECOVERY_BATCHES,
        strict=True,
    ):
        run, _collection = recovery_executor.execute(
            run_id=run_id,
            package_id=package_id,
            artifact_id=artifact_id,
            result_id=result_id,
            value=value,
            deviations=recovery_label,
        )
        recovery_runs.append(run)
    (
        recovery_verdict,
        recovery_assessment,
        recovery_effect,
        recovery_ci,
        _bounds,
    ) = _analyze(
        root,
        RECOVERY_RESULT_IDS,
        result_id=RECOVERY_ANALYSIS_RESULT_ID,
        artifact_id=RECOVERY_ARTIFACT_IDS[0],
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", ANALYSIS_COMPLETED_EVENT_TYPE, RECOVERY_ANALYSIS_RESULT_ID
            ),
            timestamp=FIXED_STAMP,
            actor="analysis-worker",
            event_type=ANALYSIS_COMPLETED_EVENT_TYPE,
            object_id=RECOVERY_GOAL_ID,
            run_id=RECOVERY_RUN_IDS[0],
            reason="recovery-track analysis: EQUIVALENT under the frozen band",
            payload={
                "analysis_id": PROTOCOL_ID,
                "equivalence_rule_id": recovery_verdict.matched_rule_id,
                "equivalence_verdict": recovery_verdict.verdict.value,
                "sufficiency_status": recovery_assessment.status.value,
            },
        )
    )
    for run_id in RECOVERY_RUN_IDS:
        recovery_executor.transition(run_id, LifecycleState.CLOSED)

    # -- supervisor closure: REPRODUCED_WITH_RECOVERY -------------------------
    closure_decision = SupervisorDecision(
        decision_id=CLOSURE_DECISION_ID,
        decision_type=DecisionType.REQUIREMENT_CLOSURE,
        actor="supervisor",
        timestamp=FIXED_STAMP,
        affected_refs=[REQUIREMENT_ID, RECOVERY_GOAL_ID],
        rationale=(
            "requirement INV-0301 closes REPRODUCED_WITH_RECOVERY: the"
            " recovery runs on hypothesis HY-0001 pass the frozen acceptance"
            " (R-EQ-1, SUFFICIENT); the recovery-level evidence is less"
            " precise than the direct evidence, so method reproducibility"
            " is REPRODUCIBLE_WITH_MINOR_RECOVERY"
        ),
        evidence_refs=[RECOVERY_ANALYSIS_RESULT_ID, EVIDENCE_IDS[0]],
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", SUPERVISOR_DECISION_EVENT_TYPE, CLOSURE_DECISION_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=SUPERVISOR_DECISION_EVENT_TYPE,
            object_id=RECOVERY_GOAL_ID,
            reason="supervisor records the requirement-closure decision",
            payload=closure_decision.to_dict(),
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
            goal_ids=[GOAL_ID, RECOVERY_GOAL_ID],
            outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY,
            method_reproducibility=MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY,
        ),
    )
    record = RequirementOutcomeRecord(
        requirement_id=REQUIREMENT_ID,
        criticality=Criticality.CRITICAL,
        outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY,
    )
    requirement_assessment = classify_requirement_outcome(record)
    project_assessment = aggregate_project_outcome([record])
    reproducibility_assessment = aggregate_method_reproducibility(
        [
            MethodReproducibilityRecord(
                requirement_id=REQUIREMENT_ID,
                reproducibility=(
                    MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY
                ),
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
            reason="requirement INV-0301 closed REPRODUCED_WITH_RECOVERY",
            payload={
                "outcome": RequirementOutcome.REPRODUCED_WITH_RECOVERY.value,
                "method_reproducibility": (
                    MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY.value
                ),
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

    return ScenarioBResult(
        root=root,
        project_id=project_id,
        strict_batches=tuple(
            read_result(root, rid).metrics[0]["value"] for rid in STRICT_RESULT_IDS
        ),
        recovery_batches=tuple(
            read_result(root, rid).metrics[0]["value"]
            for rid in RECOVERY_RESULT_IDS
        ),
        strict_verdict=strict_verdict,
        strict_assessment=strict_assessment,
        recovery_verdict=recovery_verdict,
        recovery_assessment=recovery_assessment,
        strict_effect=strict_effect,
        recovery_effect=recovery_effect,
        strict_ci=strict_ci,
        recovery_ci=recovery_ci,
        bounds=bounds,
        diagnosis_context_id=context.package.context_id,
        diagnosis_result=diagnosis_result.package,
        research_request=research_request,
        saturation=saturation,
        recovery_goal=recovery_goal,
        recovery_runs=recovery_runs,
        requirement=requirement,
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
    workspace's own absolute path normalized out and the git working tree
    excluded (see scenario A)."""
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


# ---------------------------------------------------------------------------
# Scenario B tests (each maps to AC-02 of DEV-M12-G05)
# ---------------------------------------------------------------------------


def test_B_ac02_frozen_benchmark_grounding_parses() -> None:
    """The scenario constants are the benchmark's own frozen values (see
    scenario A): the INV-0301 seed fact, the ASM-A1-TOL-01 band and the
    ASM-A1-N-01 floor -- not numbers invented here."""
    assert TARGET == 180.5
    assert MARGIN == pytest.approx(18.05)
    assert MIN_INDEPENDENT == 2


def test_B_ac02_strict_failure_is_statistically_sufficient(tmp_path: Path) -> None:
    """AC-02 "strict fail": the three valid independent runs deviate far
    outside the frozen band (R-EQ-2 NOT_EQUIVALENT) with statistically
    sufficient evidence (SUFFICIENT, no additional runs requested)."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    assert result.strict_batches == pytest.approx(STRICT_BATCHES)
    assert result.strict_verdict.verdict is EquivalenceVerdict.NOT_EQUIVALENT
    assert result.strict_verdict.matched_rule_id == "R-EQ-2"
    assert result.strict_ci.upper < result.bounds.lower  # interval wholly below band
    assert result.strict_assessment.sufficient
    assert result.strict_assessment.status is ReplicateStatus.SUFFICIENT
    assert result.strict_assessment.requested_additional_runs == 0


def test_B_ac02_diagnosis_worker_runs_against_the_frozen_goal(
    tmp_path: Path,
) -> None:
    """AC-02 "Diagnosis Worker": the real context machinery generates the
    diagnosis context for the frozen goal and the worker result registers
    with the exact context id and the failure evidence."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    assert is_valid_id(result.diagnosis_context_id, "context")
    assert result.diagnosis_result.context_id == result.diagnosis_context_id
    assert result.diagnosis_result.worker_role is WorkerRole.DIAGNOSIS_WORKER
    assert result.diagnosis_result.goal_id == GOAL_ID
    assert result.diagnosis_result.goal_version == GOAL_VERSION
    assert result.diagnosis_result.run_ref == STRICT_RUN_IDS[0]
    assert result.diagnosis_result.input_artifact_ids == [STRICT_ARTIFACT_IDS[0]]
    assert result.diagnosis_result.facts[0].name == "batch_uptake_relative_deviation"
    assert result.diagnosis_result.deviations[0].kind == DeviationType.FAILURE
    # the persisted record is byte-identical to the constructed one
    from scientific_reproduction.workers.results import read_worker_result

    persisted = read_worker_result(root, DIAGNOSIS_RESULT_ID)
    assert persisted.to_dict() == result.diagnosis_result.to_dict()


def test_B_ac02_research_request_issued_by_supervisor(tmp_path: Path) -> None:
    """AC-02 "Research Request": the request is issued through the real
    supervisor-only API (OPEN, requested_by supervisor) and recorded."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    request = result.research_request
    assert request.request.request_id == RESEARCH_REQUEST_ID
    assert request.request.requested_by == "supervisor"
    assert request.request.status.value == "OPEN"
    assert request.request.origin_refs == [GOAL_ID, DIAGNOSIS_RESULT_ID]
    assert request.request.minimum_reliability == 3
    assert request.request.minimum_directness == 2
    types = [record.event.event_type for record in result.events]
    assert types.count(RESEARCH_REQUEST_EVENT_TYPE) == 1


def test_B_ac02_eligible_hypothesis_under_the_frozen_gate(tmp_path: Path) -> None:
    """AC-02 "eligible hypothesis": the recovery hypothesis passes the real
    R >= 3 and D >= 2 actionable gate, is tracked as novel, and the real
    saturation record reflects the still-open research state (not
    saturated -- the search cycle produced novelty, so closure cannot
    happen; the recovery track absorbs the eligible hypothesis)."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    assessment = make_evidence_registry().get_assessment("SRC-001", "CLAIM-0001")
    assert assessment is not None
    from scientific_reproduction.core.rules.evidence import (
        recovery_hypothesis_eligible,
    )

    assert recovery_hypothesis_eligible(assessment)  # R 4 >= 3, D 3 >= 2
    ineligible = make_evidence_registry().get_assessment("SRC-002", "CLAIM-0002")
    assert ineligible is not None
    assert not recovery_hypothesis_eligible(ineligible)  # R 2 < 3
    assert result.saturation.verdict.value == "NOT_SATURATED"
    assert result.saturation.consecutive_zero_novelty_cycles == 0


def test_B_ac02_versioned_recovery_goal_and_labeled_recovery_runs(
    tmp_path: Path,
) -> None:
    """AC-02 "versioned Recovery Goal/Run": the recovery goal is a frozen
    v1 record with track RECOVERY and parent GOAL-EXE-50; the recovery runs
    reference it and carry the recovery-hypothesis label."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    goal = read_goal(root, RECOVERY_GOAL_ID)
    assert goal.track is GoalTrack.RECOVERY
    assert goal.parent_goal_id == GOAL_ID
    assert goal.frozen and goal.version == GOAL_VERSION
    assert goal.acceptance.criteria_ref == ACCEPTANCE_ID
    assert result.recovery_goal.track is GoalTrack.RECOVERY
    store = FilesystemStateBackend(root)
    for run_id, run in zip(RECOVERY_RUN_IDS, result.recovery_runs, strict=True):
        assert run.goal_id == RECOVERY_GOAL_ID
        assert run.run_type is RunType.INDEPENDENT_REPLICATE
        assert run.deviations[0]["hypothesis_ref"] == RECOVERY_HYPOTHESIS
        persisted = Run.from_dict(store.read("run", run_id))
        assert persisted.goal_id == RECOVERY_GOAL_ID
        assert persisted.deviations[0]["hypothesis_ref"] == RECOVERY_HYPOTHESIS


def test_B_ac02_recovery_runs_pass_with_lower_method_reproducibility(
    tmp_path: Path,
) -> None:
    """AC-02 "Recovery Runs pass": the recovery batches are EQUIVALENT
    (R-EQ-1) and SUFFICIENT, but the recovery-level evidence is measurably
    less precise than the direct evidence -- the interval is wider, so the
    aggregated method reproducibility is REPRODUCIBLE_WITH_MINOR_RECOVERY
    (lower than DIRECTLY_REPRODUCIBLE in the real rating order)."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    assert result.recovery_verdict.verdict is EquivalenceVerdict.EQUIVALENT
    assert result.recovery_verdict.matched_rule_id == "R-EQ-1"
    assert result.recovery_assessment.sufficient
    assert result.recovery_assessment.status is ReplicateStatus.SUFFICIENT
    # recovery evidence is less precise than direct evidence
    recovery_width = result.recovery_ci.upper - result.recovery_ci.lower
    strict_width = result.strict_ci.upper - result.strict_ci.lower
    # widths are not directly comparable across means; compare the relative
    # half-width of the mean (the frozen precision metric)
    direct_h = result.strict_assessment.relative_half_width
    recovery_h = result.recovery_assessment.relative_half_width
    assert direct_h is not None and recovery_h is not None
    assert recovery_h > direct_h, (
        "recovery evidence should be less precise than the direct evidence"
    )
    assert recovery_width > strict_width
    # the requirement closes REPRODUCED_WITH_RECOVERY, not plain REPRODUCED
    assert result.requirement.outcome is RequirementOutcome.REPRODUCED_WITH_RECOVERY
    assert (
        result.requirement.method_reproducibility
        is MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY
    )
    assert result.requirement_rule_id == "R-REQOUT-2"
    assert result.project_outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert result.project_rule_id == "R-PRJ-1"
    assert (
        result.reproducibility is MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY
    )
    assert result.reproducibility_rule_id == "R-MR-3"
    from scientific_reproduction.core.rules.outcome import (
        METHOD_REPRODUCIBILITY_ORDER,
    )

    assert METHOD_REPRODUCIBILITY_ORDER.index(
        MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY
    ) > METHOD_REPRODUCIBILITY_ORDER.index(MethodReproducibility.DIRECTLY_REPRODUCIBLE)


def test_B_ac02_audit_chain_records_the_recovery_track(tmp_path: Path) -> None:
    """AC-02 audit chain: the strict failure, the diagnosis, the research
    request, the recovery-entry decision and the recovery-created event all
    appear in order before the requirement closes REPRODUCED_WITH_RECOVERY."""
    root = tmp_path / "scenario-b"
    result = execute_scenario_b(root)
    types = [record.event.event_type for record in result.events]
    assert types.count(RECOVERY_CREATED_EVENT_TYPE) == 1
    assert types.count(SUPERVISOR_DECISION_EVENT_TYPE) == 2
    assert types.count(DIAGNOSIS_COMPLETED_EVENT_TYPE) == 1
    assert types.count(RESEARCH_REQUEST_EVENT_TYPE) == 1
    # ordering: research request -> recovery entry -> recovery created ->
    # requirement closed -> project outcome (first occurrence of each type)
    order = {event_type: types.index(event_type) for event_type in types}
    assert order[RESEARCH_REQUEST_EVENT_TYPE] < order[RECOVERY_CREATED_EVENT_TYPE]
    assert order[RECOVERY_CREATED_EVENT_TYPE] < order[REQUIREMENT_CLOSED_EVENT_TYPE]
    assert order[REQUIREMENT_CLOSED_EVENT_TYPE] < order[PROJECT_OUTCOME_EVENT_TYPE]
    closure_record = next(
        record
        for record in result.events
        if record.event.event_type == REQUIREMENT_CLOSED_EVENT_TYPE
    )
    assert (
        closure_record.event.payload["outcome"]
        == RequirementOutcome.REPRODUCED_WITH_RECOVERY.value
    )


def test_B_ac02_deterministic_replay(tmp_path: Path) -> None:
    """AC-02 determinism: executing the scenario twice in separate
    workspaces produces byte-identical durable state and identical
    outcomes (fixed clock, fixed seeds, scripted lab return only)."""
    first = execute_scenario_b(tmp_path / "first")
    second = execute_scenario_b(tmp_path / "second")
    assert tree_bytes(first.root) == tree_bytes(second.root)
    assert (
        first.strict_verdict.matched_rule_id
        == second.strict_verdict.matched_rule_id
        == "R-EQ-2"
    )
    assert (
        first.recovery_verdict.matched_rule_id
        == second.recovery_verdict.matched_rule_id
        == "R-EQ-1"
    )
    assert first.diagnosis_context_id == second.diagnosis_context_id
    assert (
        first.requirement.outcome
        is second.requirement.outcome
        is RequirementOutcome.REPRODUCED_WITH_RECOVERY
    )
    assert first.project_outcome is second.project_outcome
    assert first.clock.calls == second.clock.calls
    assert len(first.clock.calls) > 0  # the clock was really used
