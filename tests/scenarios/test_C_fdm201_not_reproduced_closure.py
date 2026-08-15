"""Scenario C end-to-end test: strict fail -> research trail -> closure
contract satisfied -> NOT_REPRODUCED + project aggregation + Human
Termination Gate (DEV-M12-G05 AC-03).

Covers 18-TEST-AND-ACCEPTANCE-PLAN.md section 3, scenario C, executed
entirely through the real machinery: the GOAL-EXE-50 execution of the
frozen FDM-201 benchmark (DOI 10.1039/D5TA00771B) fails strict
reproduction and closes NOT_REPRODUCED within a defined scope --

* three valid independent Runs fail strict reproduction with
  **statistically sufficient** evidence (NOT_EQUIVALENT under the frozen
  10 % band, SUFFICIENT under the frozen floor) and valid QC;
* the research trail is exhaustively recorded with the real research
  machinery: five search cycles over the required search family, three
  novel eligible hypotheses (each through the real
  ``track_new_eligible_hypotheses`` tracker), then two consecutive
  zero-novelty cycles -- the real saturation record evaluates SATURATED
  (R-SAT-S1);
* the recovery attempts -- three Recovery Runs under the versioned
  Recovery Goal, each labeled with one eligible hypothesis -- all fail
  acceptance (NOT_EQUIVALENT), ruling out every eligible hypothesis;
* the Closure Contract is then evaluated with the real closure rules:
  all four mandatory gates satisfied (statistics sufficient, execution
  valid, recovery hypotheses exhausted 3/3/0, research saturated) --
  CLOSURE_ALLOWED (R-CLOSE-2) -- **before** the requirement outcome;
* the Supervisor closes INV-0301 NOT_REPRODUCED (R-REQOUT-3), the project
  outcome aggregates to NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (R-PRJ-3)
  and a Human Termination Gate is created (GateType.TERMINATION_GATE,
  OPEN) with the HUMAN_GATE_OPEN decision.

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
from scientific_reproduction.core.ids import generate_id
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
    GateStatus,
    GateType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    HumanGate,
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
)
from scientific_reproduction.core.rules.closure import (
    ClosureOutcome,
    ClosureRecord,
    evaluate_closure,
)
from scientific_reproduction.core.rules.evidence import (
    recovery_hypothesis_eligible,
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
    register_acceptance,
    register_closure_contract,
    register_goal,
)
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.research.saturation import (
    HypothesisCandidate,
    SaturationRecord,
    SearchCycle,
    evaluate_saturation,
    track_new_eligible_hypotheses,
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
#: the requirement the scenario closes.
GOAL_ID = "GOAL-EXE-50"
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
STRICT_RESULT_IDS = ("RES-C1", "RES-C2", "RES-C3")
STRICT_ANALYSIS_RESULT_ID = "RES-C-ANL"
RECOVERY_RUN_IDS = ("RUN-004", "RUN-005", "RUN-006")
RECOVERY_PACKAGE_IDS = ("PKG-004", "PKG-005", "PKG-006")
RECOVERY_ARTIFACT_IDS = ("ART-004", "ART-005", "ART-006")
RECOVERY_RESULT_IDS = ("RES-C-R1", "RES-C-R2", "RES-C-R3")
RECOVERY_ANALYSIS_RESULT_ID = "RES-C-ANL-R"

#: The research trail ids.
HYPOTHESIS_IDS = ("HY-C1", "HY-C2", "HY-C3")
EVIDENCE_IDS = ("EV-C1", "EV-C2", "EV-C3")
CLOSURE_DECISION_ID = "DEC-C1"
GATE_DECISION_ID = "DEC-C2"
GATE_ID = "GATE-T-001"

#: The raw data file the lab result package declares (test_H convention).
REQUIRED_RETURN = ["uptake.csv"]

#: Strict-track batches: three valid independent runs whose mean deviates
#: ~16.9 % from the published target -- outside the frozen 10 % band,
#: with very tight spread: statistically sufficient evidence of failure.
STRICT_BATCHES = (150.0, 151.0, 149.0)

#: Recovery-attempt batches: the three recovery runs (one per eligible
#: hypothesis) still deviate ~16.9 % -- every eligible hypothesis is
#: tested and ruled out.
RECOVERY_ATTEMPT_BATCHES = (150.5, 149.5, 150.5)

#: The five recorded search cycles of the research trail: three cycles
#: each produce one novel eligible hypothesis, then two consecutive
#: cycles produce none (the frozen saturation rule needs two).
CYCLE_NOVELTY = (1, 1, 1, 0, 0)

#: Audit event types of the scenario.
PLAN_FROZEN_EVENT_TYPE = "plan.frozen"
INVENTORY_AUDIT_EVENT_TYPE = "inventory.audit.passed"
RUN_DISPATCHED_EVENT_TYPE = "run.dispatched"
RUN_COMPLETED_EVENT_TYPE = "run.completed"
ANALYSIS_COMPLETED_EVENT_TYPE = "analysis.completed"
RESEARCH_CYCLE_EVENT_TYPE = "research.cycle"
SUPERVISOR_DECISION_EVENT_TYPE = "supervisor.decision"
RECOVERY_CREATED_EVENT_TYPE = "recovery.created"
REQUIREMENT_CLOSED_EVENT_TYPE = "requirement.closed"
PROJECT_OUTCOME_EVENT_TYPE = "project.outcome.recorded"


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


#: The published seed value the batches fail to reproduce (INV-0301).
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
    the adapter's incoming handoff, declaring the required raw data file."""
    incoming = handoff / "incoming" / run_id
    incoming.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": RESULT_MANIFEST_VERSION,
        "package_id": package_id,
        "project_id": "scenario-c",
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
    """The frozen EQUIVALENCE acceptance record of scenario C (same frozen
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


def make_goal(goal_id: str = GOAL_ID) -> GoalContract:
    """The frozen GOAL-EXE-50 goal contract, built from the benchmark goals
    file (its dependency goals are not part of scenario C: no diagnosis
    context is generated, so no dependency records are read)."""
    goals = _load_yaml("goals/goals.yaml")["goals"]
    record = next(g for g in goals if g["goal_id"] == goal_id)
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


def make_recovery_goal() -> GoalContract:
    """The versioned Recovery Goal of scenario C: track RECOVERY, parent
    GOAL-EXE-50, frozen v1, same frozen acceptance -- the goal under which
    the three recovery attempts (one per eligible hypothesis) run."""
    record = next(
        g
        for g in _load_yaml("goals/goals.yaml")["goals"]
        if g["goal_id"] == GOAL_ID
    )
    return GoalContract(
        goal_id=RECOVERY_GOAL_ID,
        title="Single-component C3H6/C2H4 adsorption isotherms -- recovery track",
        unit_process_type=record["unit_process_type"],
        track=GoalTrack.RECOVERY,
        objective=(
            "re-attempt the 298 K / 1 bar C3H6 uptake under the recovery"
            " hypotheses HY-C1..HY-C3 (sample/temperature/adsorbate purity"
            " classes); every eligible hypothesis is tested here and ruled"
            " out before the closure contract is evaluated"
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
    closure file (statistical/execution/recovery/literature axes verbatim;
    the recovery/literature counts are null in the file because they are
    scenario-established facts)."""
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


def make_hypothesis_registry() -> EvidenceRegistry:
    """The claim-specific evidence of the three recovery hypotheses: each
    clears the frozen eligibility gate (R >= 3, D >= 2, actionable), so all
    three must be tested before the closure contract can be satisfied."""
    registry = EvidenceRegistry()
    for hypothesis_id, evidence_id in zip(HYPOTHESIS_IDS, EVIDENCE_IDS, strict=True):
        class_label = {
            "HY-C1": "measurement-calibration",
            "HY-C2": "sample-activation",
            "HY-C3": "adsorbate-purity",
        }[hypothesis_id]
        registry = registry.register(
            ClaimSpecificEvidence(
                evidence_id=evidence_id,
                source_id=f"SRC-C{hypothesis_id[-1]}",
                claim_id=f"CLAIM-C{hypothesis_id[-1]}",
                finding=(
                    f"{hypothesis_id}: the uptake deviation is attributed to"
                    f" a {class_label} discrepancy class"
                ),
                assessment=EvidenceAssessment(
                    authority=3,
                    reliability=3,
                    directness=3,
                    reliability_checklist_ref="CL-0001",
                ),
                source_location="17-FDM201-REFERENCE-CASE.md section 2",
                role="diagnosis",
                used_by=[GOAL_ID, REQUIREMENT_ID],
            )
        )
    return registry


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
class ScenarioCResult:
    """Everything the executed scenario produced (frozen, auditable)."""

    root: Path
    project_id: str
    strict_batches: tuple[float, ...]
    recovery_batches: tuple[float, ...]
    strict_verdict: object  # EquivalenceAssessment
    strict_assessment: object  # ReplicateSufficiencyAssessment
    strict_ci: ConfidenceInterval
    recovery_verdict: object  # EquivalenceAssessment
    bounds: EquivalenceBounds
    hypotheses: list[str]
    cycle_novelty: list[int]
    saturation: object  # SaturationAssessment
    closure: object  # ClosureAssessment
    requirement: ReproductionRequirement
    requirement_rule_id: str
    project_outcome: ReproductionOutcome
    project_rule_id: str
    reproducibility: MethodReproducibility
    gate: HumanGate
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


def execute_scenario_c(root: Path) -> ScenarioCResult:
    """Execute scenario C end to end and return the full evidence trail."""
    clock = FakeClock()
    project_id = init_project(root)
    log = ProjectEventLog(root)
    _register_planning(root, log)

    # -- strict-track execution: statistically sufficient failure, valid QC -
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
    strict_verdict, strict_assessment, _strict_effect, strict_ci, bounds = _analyze(
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

    # -- research trail: five cycles, three novel eligible hypotheses -------
    registry = make_hypothesis_registry()
    hypotheses: list[str] = []
    cycle_novelty: list[int] = []
    known: list[str] = []
    for cycle_index, novel_count in enumerate(CYCLE_NOVELTY):
        if novel_count:
            hypothesis_id = HYPOTHESIS_IDS[cycle_index]
            assessment = registry.get_assessment(
                f"SRC-C{hypothesis_id[-1]}", f"CLAIM-C{hypothesis_id[-1]}"
            )
            assert assessment is not None
            assert recovery_hypothesis_eligible(assessment)  # R 3, D 3
            novelty = track_new_eligible_hypotheses(
                [
                    HypothesisCandidate(
                        hypothesis_ref=hypothesis_id,
                        assessment=assessment,
                    )
                ],
                known_eligible_hypotheses=known,
            )
            hypotheses.append(hypothesis_id)
        else:
            novelty = track_new_eligible_hypotheses([], known_eligible_hypotheses=known)
        known.extend(hypotheses[-novelty.count :])
        cycle_novelty.append(novelty.count)
        log.append(
            ProjectEvent(
                event_id=generate_id(
                    "event", RESEARCH_CYCLE_EVENT_TYPE, f"cycle-{cycle_index}"
                ),
                timestamp=FIXED_STAMP,
                actor="research-worker",
                event_type=RESEARCH_CYCLE_EVENT_TYPE,
                object_id=GOAL_ID,
                reason="recorded expansion search cycle of the research trail",
                payload={
                    "cycle_index": cycle_index,
                    "new_eligible_hypotheses": novelty.count,
                    "hypotheses": hypotheses[-novelty.count :] or [],
                },
            )
        )
    saturation = evaluate_saturation(
        SaturationRecord(
            cycles=tuple(
                SearchCycle(
                    cycle_index=cycle_index,
                    search_family="uptake-discrepancy root causes",
                    completed=True,
                    new_eligible_hypotheses=novel,
                )
                for cycle_index, novel in enumerate(CYCLE_NOVELTY)
            ),
            required_search_families_completed=True,
        )
    )

    # -- versioned recovery goal + recovery attempts (one per hypothesis) ---
    register_goal(root, make_recovery_goal())
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
                "hypotheses": list(HYPOTHESIS_IDS),
            },
        )
    )
    recovery_executor = BatchExecutor(
        root, project_id=project_id, goal_id=RECOVERY_GOAL_ID, clock=clock
    )
    recovery_runs: list[Run] = []
    for run_id, package_id, artifact_id, result_id, value, hypothesis_id in zip(
        RECOVERY_RUN_IDS,
        RECOVERY_PACKAGE_IDS,
        RECOVERY_ARTIFACT_IDS,
        RECOVERY_RESULT_IDS,
        RECOVERY_ATTEMPT_BATCHES,
        HYPOTHESIS_IDS,
        strict=True,
    ):
        run, _collection = recovery_executor.execute(
            run_id=run_id,
            package_id=package_id,
            artifact_id=artifact_id,
            result_id=result_id,
            value=value,
            deviations=[
                {
                    "kind": "recovery",
                    "hypothesis_ref": hypothesis_id,
                    "reason": (
                        f"recovery attempt testing the eligible hypothesis"
                        f" {hypothesis_id}"
                    ),
                }
            ],
        )
        recovery_runs.append(run)
    recovery_verdict, recovery_assessment, _effect, recovery_ci, _bounds = _analyze(
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
            reason="recovery-attempt analysis: still NOT_EQUIVALENT",
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

    # -- closure contract evaluation (BEFORE the requirement outcome) --------
    contract = make_closure_contract()
    closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=bool(strict_assessment.sufficient),
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            eligible_hypotheses_total=len(HYPOTHESIS_IDS),
            tested_or_ruled_out=len(HYPOTHESIS_IDS),
            required_search_families_completed=(
                saturation.required_search_families_completed
            ),
            consecutive_zero_novelty_cycles=(
                saturation.consecutive_zero_novelty_cycles
            ),
            required_zero_novelty_cycles=(
                contract.literature.required_zero_novelty_cycles
            ),
        )
    )
    assert closure.outcome is ClosureOutcome.CLOSURE_ALLOWED
    closure_decision = SupervisorDecision(
        decision_id=CLOSURE_DECISION_ID,
        decision_type=DecisionType.REQUIREMENT_CLOSURE,
        actor="supervisor",
        timestamp=FIXED_STAMP,
        affected_refs=[REQUIREMENT_ID, RECOVERY_GOAL_ID],
        rationale=(
            "all four mandatory closure gates are satisfied before the"
            " requirement outcome: statistics sufficient (SUFFICIENT"
            " NOT_EQUIVALENT), execution valid (all runs CLOSED, valid QC),"
            " recovery hypotheses exhausted (3 tested / ruled out, 0"
            " remaining), research saturated (two consecutive zero-novelty"
            " cycles, required search family completed) -- R-CLOSE-2"
        ),
        evidence_refs=[
            STRICT_ANALYSIS_RESULT_ID,
            RECOVERY_ANALYSIS_RESULT_ID,
            *EVIDENCE_IDS,
        ],
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
            reason="supervisor records the closure-contract decision",
            payload={
                "closure_rule_id": closure.matched_rule_id,
                "closure_allowed": closure.closure_allowed,
                "blocked_gate_ids": list(closure.blocked_gate_ids),
                "decision": closure_decision.to_dict(),
            },
        )
    )

    # -- requirement outcome: NOT_REPRODUCED --------------------------------
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
            outcome=RequirementOutcome.NOT_REPRODUCED,
            method_reproducibility=MethodReproducibility.NOT_REPRODUCIBLE,
        ),
    )
    record = RequirementOutcomeRecord(
        requirement_id=REQUIREMENT_ID,
        criticality=Criticality.CRITICAL,
        outcome=RequirementOutcome.NOT_REPRODUCED,
    )
    requirement_assessment = classify_requirement_outcome(record)
    project_assessment = aggregate_project_outcome(
        [record], closure_allowed=closure.closure_allowed
    )
    reproducibility_assessment = aggregate_method_reproducibility(
        [
            MethodReproducibilityRecord(
                requirement_id=REQUIREMENT_ID,
                reproducibility=MethodReproducibility.NOT_REPRODUCIBLE,
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
            reason="requirement INV-0301 closed NOT_REPRODUCED",
            payload={
                "outcome": RequirementOutcome.NOT_REPRODUCED.value,
                "method_reproducibility": (
                    MethodReproducibility.NOT_REPRODUCIBLE.value
                ),
                "requirement_rule_id": requirement_assessment.matched_rule_id,
                "closure_rule_id": closure.matched_rule_id,
                "decision_id": CLOSURE_DECISION_ID,
            },
        )
    )

    # -- Human Termination Gate ---------------------------------------------
    gate = HumanGate(
        gate_id=GATE_ID,
        gate_type=GateType.TERMINATION_GATE,
        status=GateStatus.OPEN,
        trigger=(
            "requirement INV-0301 closed NOT_REPRODUCED under a satisfied"
            " closure contract (R-CLOSE-2, R-REQOUT-3)"
        ),
        affected_refs=[REQUIREMENT_ID, GOAL_ID, RECOVERY_GOAL_ID],
        requested_decision=(
            "whether to terminate the reproduction of INV-0301 within the"
            " defined scope or to re-scope under change control"
        ),
        evidence_refs=[STRICT_ANALYSIS_RESULT_ID, RECOVERY_ANALYSIS_RESULT_ID],
        default_safe_action=(
            "archive the NOT_REPRODUCED finding with the full audit chain"
            " and aggregate the project outcome"
        ),
    )
    gate_decision = SupervisorDecision(
        decision_id=GATE_DECISION_ID,
        decision_type=DecisionType.HUMAN_GATE_OPEN,
        actor="supervisor",
        timestamp=FIXED_STAMP,
        affected_refs=[GATE_ID, REQUIREMENT_ID],
        rationale=(
            "Human Termination Gate opened after the NOT_REPRODUCED"
            " requirement closure: the closure contract was satisfied, all"
            " eligible recovery hypotheses were tested and ruled out, and"
            " the research trail is saturated"
        ),
        evidence_refs=[REQUIREMENT_ID, *EVIDENCE_IDS],
    )
    log.append(
        ProjectEvent(
            event_id=generate_id(
                "event", SUPERVISOR_DECISION_EVENT_TYPE, GATE_DECISION_ID
            ),
            timestamp=FIXED_STAMP,
            actor="supervisor",
            event_type=SUPERVISOR_DECISION_EVENT_TYPE,
            object_id=REQUIREMENT_ID,
            reason="supervisor records the human-termination-gate decision",
            payload={"gate": gate.to_dict(), "decision": gate_decision.to_dict()},
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
                "termination_gate": GATE_ID,
            },
        )
    )

    return ScenarioCResult(
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
        strict_ci=strict_ci,
        recovery_verdict=recovery_verdict,
        bounds=bounds,
        hypotheses=hypotheses,
        cycle_novelty=cycle_novelty,
        saturation=saturation,
        closure=closure,
        requirement=requirement,
        requirement_rule_id=requirement_assessment.matched_rule_id,
        project_outcome=project_assessment.outcome,
        project_rule_id=project_assessment.matched_rule_id,
        reproducibility=reproducibility_assessment.reproducibility,
        gate=gate,
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
# Scenario C tests (each maps to AC-03 of DEV-M12-G05)
# ---------------------------------------------------------------------------


def test_C_ac03_frozen_benchmark_grounding_parses() -> None:
    """The scenario constants are the benchmark's own frozen values (see
    scenario A): the INV-0301 seed fact, the ASM-A1-TOL-01 band, the
    ASM-A1-N-01 floor and the frozen zero-novelty rule of CC-EXPERIMENT."""
    assert TARGET == 180.5
    assert MARGIN == pytest.approx(18.05)
    assert MIN_INDEPENDENT == 2
    assert make_closure_contract().literature.required_zero_novelty_cycles == 2


def test_C_ac03_strict_failure_is_statistically_sufficient_with_valid_qc(
    tmp_path: Path,
) -> None:
    """AC-03 "strict fail statistically sufficient": the three valid
    independent runs deviate far outside the frozen band (R-EQ-2
    NOT_EQUIVALENT) with statistically sufficient evidence (SUFFICIENT,
    no additional runs requested) and valid QC (every batch result record
    carries a QC finding)."""
    root = tmp_path / "scenario-c"
    result = execute_scenario_c(root)
    assert result.strict_batches == pytest.approx(STRICT_BATCHES)
    assert result.strict_verdict.verdict is EquivalenceVerdict.NOT_EQUIVALENT
    assert result.strict_verdict.matched_rule_id == "R-EQ-2"
    assert result.strict_ci.upper < result.bounds.lower  # interval wholly below band
    assert result.strict_assessment.sufficient
    assert result.strict_assessment.status is ReplicateStatus.SUFFICIENT
    assert result.strict_assessment.requested_additional_runs == 0
    for result_id in STRICT_RESULT_IDS:
        assert read_result(root, result_id).qc_findings, (
            f"batch result {result_id} carries no QC finding"
        )


def test_C_ac03_all_eligible_hypotheses_tested_or_ruled_out(tmp_path: Path) -> None:
    """AC-03 "all eligible hypotheses tested/ruled out": three hypotheses
    each clear the frozen eligibility gate (R >= 3, D >= 2) and each is
    tested by exactly one recovery run under the versioned Recovery Goal;
    every recovery attempt fails acceptance, so 3/3 are ruled out and 0
    remain."""
    root = tmp_path / "scenario-c"
    result = execute_scenario_c(root)
    assert result.hypotheses == list(HYPOTHESIS_IDS)
    assert result.cycle_novelty == [1, 1, 1, 0, 0]
    assert result.recovery_verdict.verdict is EquivalenceVerdict.NOT_EQUIVALENT
    assert result.recovery_verdict.matched_rule_id == "R-EQ-2"
    store = FilesystemStateBackend(root)
    for run_id, hypothesis_id in zip(
        RECOVERY_RUN_IDS, HYPOTHESIS_IDS, strict=True
    ):
        run = Run.from_dict(store.read("run", run_id))
        assert run.goal_id == RECOVERY_GOAL_ID
        assert run.deviations[0]["hypothesis_ref"] == hypothesis_id
    from scientific_reproduction.planning.plan import read_goal

    goal = read_goal(root, RECOVERY_GOAL_ID)
    assert goal.track is GoalTrack.RECOVERY and goal.parent_goal_id == GOAL_ID
    closure_record = result.closure.record
    assert closure_record.eligible_hypotheses_total == 3
    assert closure_record.tested_or_ruled_out == 3
    assert closure_record.recovery_hypotheses_remaining == 0


def test_C_ac03_research_saturation_met(tmp_path: Path) -> None:
    """AC-03 "research saturation met": the real saturation record over
    the five recorded cycles evaluates SATURATED (R-SAT-S1) -- the two
    consecutive zero-novelty cycles meet the frozen rule and the required
    search family is completed."""
    root = tmp_path / "scenario-c"
    result = execute_scenario_c(root)
    saturation = result.saturation
    assert saturation.verdict.value == "SATURATED"
    assert saturation.matched_rule_id == "R-SAT-S1"
    assert saturation.consecutive_zero_novelty_cycles == 2
    assert saturation.required_search_families_completed is True
    assert len(saturation.record.cycles) == 5
    assert [c.new_eligible_hypotheses for c in saturation.record.cycles] == [
        1,
        1,
        1,
        0,
        0,
    ]


def test_C_ac03_closure_contract_satisfied_before_not_reproduced(
    tmp_path: Path,
) -> None:
    """AC-03 "Closure before non-reproduced outcome": the closure contract
    is evaluated with the real closure rules BEFORE the requirement
    outcome is recorded -- all four mandatory gates satisfied, CLOSURE_
    ALLOWED (R-CLOSE-2), and the closure decision event precedes the
    requirement.closed event in the audit stream."""
    root = tmp_path / "scenario-c"
    result = execute_scenario_c(root)
    closure = result.closure
    assert closure.outcome is ClosureOutcome.CLOSURE_ALLOWED
    assert closure.closure_allowed is True
    assert closure.matched_rule_id == "R-CLOSE-2"
    assert closure.blocked_gate_ids == ()
    satisfied = [
        gate for gate in closure.gate_decisions if gate.state.value == "SATISFIED"
    ]
    assert len(satisfied) == 4  # all four mandatory gates
    # the first supervisor.decision is the closure decision (DEC-C1); the
    # requirement outcome is recorded only after it
    types = [record.event.event_type for record in result.events]
    order = {event_type: types.index(event_type) for event_type in types}
    assert order[SUPERVISOR_DECISION_EVENT_TYPE] < order[REQUIREMENT_CLOSED_EVENT_TYPE]
    closure_record = next(
        record
        for record in result.events
        if record.event.event_type == REQUIREMENT_CLOSED_EVENT_TYPE
    )
    assert closure_record.event.payload["closure_rule_id"] == "R-CLOSE-2"


def test_C_ac03_requirement_closed_not_reproduced_with_project_aggregation(
    tmp_path: Path,
) -> None:
    """AC-03 outcome: the requirement closes NOT_REPRODUCED (R-REQOUT-3)
    and the project outcome aggregates to
    NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (R-PRJ-3) under the satisfied
    closure contract; the method is rated NOT_REPRODUCIBLE."""
    root = tmp_path / "scenario-c"
    result = execute_scenario_c(root)
    assert result.requirement.outcome is RequirementOutcome.NOT_REPRODUCED
    assert (
        result.requirement.method_reproducibility
        is MethodReproducibility.NOT_REPRODUCIBLE
    )
    assert result.requirement_rule_id == "R-REQOUT-3"
    assert (
        result.project_outcome
        is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    )
    assert result.project_rule_id == "R-PRJ-3"
    assert result.reproducibility is MethodReproducibility.NOT_REPRODUCIBLE


def test_C_ac03_human_termination_gate_created(tmp_path: Path) -> None:
    """AC-03 "Human Termination Gate": the gate record is a
    TERMINATION_GATE in OPEN status with the frozen schema shape, opened
    through the supervisor's HUMAN_GATE_OPEN decision after the
    NOT_REPRODUCED closure."""
    root = tmp_path / "scenario-c"
    result = execute_scenario_c(root)
    gate = result.gate
    assert gate.gate_id == GATE_ID
    assert gate.gate_type is GateType.TERMINATION_GATE
    assert gate.status is GateStatus.OPEN
    assert REQUIREMENT_ID in gate.affected_refs
    assert gate.default_safe_action
    gate_event = next(
        record
        for record in result.events
        if record.event.event_type == SUPERVISOR_DECISION_EVENT_TYPE
        and record.event.payload.get("gate", {}).get("gate_id") == GATE_ID
    )
    assert gate_event.event.payload["gate"]["status"] == "OPEN"
    # the gate opens strictly after the NOT_REPRODUCED requirement closure
    closed_sequence = next(
        record.sequence
        for record in result.events
        if record.event.event_type == REQUIREMENT_CLOSED_EVENT_TYPE
    )
    assert gate_event.sequence > closed_sequence


def test_C_ac03_deterministic_replay(tmp_path: Path) -> None:
    """AC-03 determinism: executing the scenario twice in separate
    workspaces produces byte-identical durable state and identical
    outcomes (fixed clock, fixed seeds, scripted lab return only)."""
    first = execute_scenario_c(tmp_path / "first")
    second = execute_scenario_c(tmp_path / "second")
    assert tree_bytes(first.root) == tree_bytes(second.root)
    assert (
        first.strict_verdict.matched_rule_id
        == second.strict_verdict.matched_rule_id
        == "R-EQ-2"
    )
    assert first.closure.matched_rule_id == second.closure.matched_rule_id == "R-CLOSE-2"
    assert first.closure.closure_allowed is second.closure.closure_allowed is True
    assert first.requirement_rule_id == second.requirement_rule_id == "R-REQOUT-3"
    assert first.project_rule_id == second.project_rule_id == "R-PRJ-3"
    assert first.saturation.matched_rule_id == second.saturation.matched_rule_id
    assert first.gate.to_dict() == second.gate.to_dict()
    assert first.clock.calls == second.clock.calls
    assert len(first.clock.calls) > 0  # the clock was really used
