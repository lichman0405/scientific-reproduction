"""FDM-201 simulated scenario F -- DFT convergence requires parameter change (DEV-M9-G06).

Scenario F is the frozen acceptance fixture **S5** of
``examples/fdm-201/simulated-scenarios.md``: the job technically runs
but scientific convergence fails; changing smearing/mixing/convergence
policy would alter the method. Expected (frozen acceptance): **Worker
reports facts; Supervisor decides diagnosis/research/recovery.**

Every test name contains ``F`` and the module basename matches the
``tests/scenarios/test_F*`` glob, so the frozen verification
``python -m pytest -q tests/scenarios -k "D or F"`` selects this suite.
The ``ac02`` sections map one-to-one to acceptance criterion AC-02 of
DEV-M9-G06:

* the failure is reported as facts and deviations -- never a silent
  retry: the simulated computation worker returns a
  ``WorkerResultPackage`` (real ``workers.results.register_worker_result``)
  carrying the convergence facts and a ``DeviationType.FAILURE``
  deviation, and the analysis path registers a ``ResultRecord`` (real
  ``analysis.results.register_result``) carrying the convergence
  metrics/findings; exactly one execution is ever recorded;
* no automatic parameter mutation happens: the convergence module
  (``analysis.computational``, DEV-M9-G05) has no parameter-tuning
  surface (no ``set_``/``adjust``/``restart``/``smearing`` callable), the
  simulated worker never changes the frozen smearing/mixing/convergence
  parameters -- the failure leaves the frozen protocol and acceptance
  byte-identical ("changing them would alter the method") -- and the
  Supervisor decision surface (``core.models.SupervisorDecision`` /
  ``DecisionType``) is where diagnosis/research/recovery decisions would
  live: the worker layer never fabricates one (no decision record is
  written by the analysis/worker path).

The scenario runs the merged analysis stack end to end against simulated
FDM-201 DFT data: the deterministic one-paper project (``ANL-1`` ``v1``
frozen PRIMARY protocol carrying the smearing/mixing method policy, raw
manifests ``ART-001``/``ART-002``, the frozen CONVERGENCE acceptance
``ACC-1`` with the tolerance), the real ``analysis.computational``
(``evaluate_convergence`` over the non-settling SCF iteration series,
``R-CONV-N1``/``R-CONV-D1``, reporting hooks), the real
``workers.results.register_worker_result`` and the real
``analysis.results.register_result``.

Determinism mirrors the M9 suites: fixed identities/timestamps, pinned
safe ids only (``ANL-1``, ``ART-001``, ``ACC-1``, ``RUN-001``,
``REQ-1``), pinned generated context id, no randomness, no wall clock,
no network.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import FrozenInstanceError, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scientific_reproduction.analysis.computational as computational_module
import scientific_reproduction.analysis.results as results_module
import scientific_reproduction.workers.results as workers_results_module
from scientific_reproduction.analysis.computational import (
    ConvergenceAssessment,
    ConvergenceStatus,
    convergence_criterion_from_acceptance,
    convergence_findings,
    convergence_metrics,
    evaluate_convergence,
    validate_acceptance_mode,
)
from scientific_reproduction.analysis.protocols import (
    freeze_primary_protocol,
    register_analysis_record,
)
from scientific_reproduction.analysis.results import (
    ARTIFACTS_STATE_DIR,
    ResultRecord,
    read_result,
    register_result,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ArtifactManifest,
    DecisionMode,
    DecisionType,
    PrimaryOrExploratory,
    SupervisorDecision,
    WorkerRole,
)
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.plan import (
    read_acceptance,
    read_analysis_protocol,
    register_acceptance,
)
from scientific_reproduction.workers.results import (
    DeviationType,
    WorkerData,
    WorkerDeviation,
    WorkerFact,
    WorkerResultPackage,
    list_worker_results,
    read_worker_result,
    register_worker_result,
)

#: Deterministic author/committer identity (mirrors protocol_helpers).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp for the frozen PRIMARY protocol.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Fixed injectable completion timestamp of the simulated worker.
COMPLETED_AT = "2026-06-01T12:00:00Z"

#: Primary target DOI of the one-paper project (17-FDM201-REFERENCE-CASE.md).
DOI = "10.1039/D5TA00771B"

#: The frozen convergence tolerance of the CONVERGENCE acceptance
#: (hartree; the frozen drift threshold of the SCF protocol).
TOLERANCE = 1e-6

#: The frozen DFT method policy of the PRIMARY protocol. Changing any of
#: these (smearing/mixing/convergence policy) would alter the method
#: (S5) -- the simulated worker must leave them untouched.
SMearing_WIDTH = 0.05  # hartree, frozen gaussian smearing width
MIXING_SCHEME = "pulay"

#: The frozen SCF iteration budget of the run.
SCF_MAX_ITERATIONS = 3

#: The SCF energy series that does NOT settle: the trailing-window drift
#: (0.25) exceeds the frozen tolerance at the end of the budget
#: (R-CONV-N1 iteration non-convergence).
NOT_CONVERGED_SERIES = (1.0, 0.6, 0.35)

#: The SCF energy series whose drift grows at every step: the final drift
#: exceeds every earlier drift and the frozen tolerance (R-CONV-D1).
DIVERGING_SERIES = (1.0, 1.1, 1.35, 1.85)

#: The pinned generated context id of the answered computation context
#: (the exact shape the GoalExecutionContextPackage generator produces).
CONTEXT_ID = generate_id(
    "context", "FDM-201", "G-1", "v1", WorkerRole.COMPUTATION_WORKER.value
)

#: Worker/analysis ids of the scenario (all safe registry ids).
WORKER_RESULT_ID = "RES-F-WR-1"
ANALYSIS_RESULT_ID = "RES-F-1"
RUN_REF = "RUN-001"
INPUT_ARTIFACT = "ART-001"
OUTPUT_ARTIFACT = "ART-002"
REQUIREMENT_REF = "REQ-1"
GOAL_ID = "G-1"


# ---------------------------------------------------------------------------
# Deterministic project fixtures (self-contained: scenario tests live in
# their own directory, so the analysis-suite protocol_helpers are not on
# the import path)
# ---------------------------------------------------------------------------


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_protocol(analysis_id: str) -> AnalysisProtocolOrResult:
    """Build the draft PRIMARY DFT protocol carrying the frozen method policy.

    The methods entry records the frozen smearing/mixing/convergence
    policy of the SCF method: changing it would alter the method (S5).
    """
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[
            {
                "name": "dft_scf",
                "smearing": "gaussian",
                "smearing_width": SMearing_WIDTH,
                "mixing": MIXING_SCHEME,
                "max_iterations": SCF_MAX_ITERATIONS,
                "tolerance": TOLERANCE,
            }
        ],
    )


def make_manifest(artifact_id: str, *, run_id: str) -> ArtifactManifest:
    """Build a schema-valid raw artifact manifest (no file access)."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=f"file:///raw/{artifact_id}.json",
        sha256="a" * 64,
        size_bytes=1024,
        created_at="2026-01-01T00:00:00Z",
        run_id=run_id,
    )


def make_acceptance(acceptance_id: str) -> AcceptanceCriteria:
    """Build the frozen CONVERGENCE acceptance record of scenario F."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=GOAL_ID,
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.CONVERGENCE,
        criteria=[{"metric": "scf_energy", "tolerance": TOLERANCE}],
        rationale=(
            "frozen FDM-201 DFT convergence criteria: the SCF drift"
            " tolerance is a frozen protocol input, never inferred from"
            " the observed run (07-STATISTICS-AND-ACCEPTANCE.md SS8)"
        ),
    )


def build_scenario_workspace(tmp_path: Path) -> Path:
    """Initialize the project with the registered entities the scenario uses.

    Registers, deterministically: the frozen PRIMARY protocol ``ANL-1``
    ``v1`` (DEV-M9-G01 registry) carrying the smearing/mixing method
    policy, the input/output artifact manifests (``manifests/``) and the
    frozen CONVERGENCE acceptance ``ACC-1`` with the tolerance.
    """
    root = init_project(tmp_path)
    register_analysis_record(root, make_protocol("ANL-1"))
    draft = read_analysis_protocol(root, "ANL-1")
    freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    registry.register(make_manifest(INPUT_ARTIFACT, run_id=RUN_REF))
    registry.register(make_manifest(OUTPUT_ARTIFACT, run_id=RUN_REF))
    register_acceptance(root, make_acceptance("ACC-1"))
    return root


def make_worker_facts(
    assessment: ConvergenceAssessment,
) -> list[WorkerFact]:
    """The convergence facts of the simulated DFT worker (AC-02).

    Facts carry the observed convergence state and the frozen method
    parameters actually used -- the smearing/mixing values are the frozen
    protocol values, never changed by the worker.
    """
    return [
        WorkerFact(
            fact_id="F-1",
            name="scf_convergence_status",
            value=assessment.status.value,
        ),
        WorkerFact(
            fact_id="F-2", name="scf_final_drift", value=assessment.final_drift, unit="hartree"
        ),
        WorkerFact(
            fact_id="F-3",
            name="scf_settling_drift",
            value=assessment.settling_drift,
            unit="hartree",
        ),
        WorkerFact(
            fact_id="F-4", name="scf_iterations_used", value=assessment.iterations_used
        ),
        WorkerFact(
            fact_id="F-5", name="scf_max_iterations", value=SCF_MAX_ITERATIONS
        ),
        WorkerFact(
            fact_id="F-6", name="smearing_width", value=SMearing_WIDTH, unit="hartree"
        ),
        WorkerFact(fact_id="F-7", name="mixing_scheme", value=MIXING_SCHEME),
        WorkerFact(
            fact_id="F-8", name="convergence_tolerance", value=TOLERANCE, unit="hartree"
        ),
        WorkerFact(fact_id="F-9", name="job_technical_status", value="completed"),
    ]


def make_failure_deviation(assessment: ConvergenceAssessment) -> WorkerDeviation:
    """The failure deviation of the simulated DFT worker (AC-02).

    ``DeviationType.FAILURE`` is the factual engineering vocabulary
    (10-EXPERIMENT-SUBSYSTEM.md SS4: report "failures/interruptions") --
    an execution fact, never a requirement verdict. The deviation states
    that the method parameters were left unchanged: changing the
    smearing/mixing/convergence policy would alter the method (S5).
    """
    tolerance = assessment.state.input.tolerance
    return WorkerDeviation(
        deviation_id="DEV-1",
        kind=DeviationType.FAILURE,
        description=(
            "scientific convergence not reached: the trailing-window drift"
            f" {assessment.settling_drift:.6g} exceeds the frozen tolerance"
            f" {tolerance:.6g} ({assessment.matched_rule_id}); smearing/"
            " mixing/convergence parameters left unchanged -- changing them"
            " would alter the method (S5)"
        ),
        requirement_refs=[REQUIREMENT_REF],
    )


def make_worker_package(assessment: ConvergenceAssessment) -> WorkerResultPackage:
    """The worker result package of the simulated DFT worker (AC-02).

    The package carries the convergence facts, the structured SCF output
    and the FAILURE deviation; ``decision_refs`` stays empty -- the
    worker reports facts, it never fabricates a Supervisor decision.
    """
    return WorkerResultPackage(
        result_id=WORKER_RESULT_ID,
        context_id=CONTEXT_ID,
        worker_role=WorkerRole.COMPUTATION_WORKER,
        goal_id=GOAL_ID,
        goal_version="v1",
        run_ref=RUN_REF,
        facts=make_worker_facts(assessment),
        data=[
            WorkerData(
                data_id="D-1",
                name="scf_energy_series",
                format="json",
                summary={"iterations": assessment.iterations_used},
            )
        ],
        deviations=[make_failure_deviation(assessment)],
        input_artifact_ids=[INPUT_ARTIFACT],
        output_artifact_ids=[OUTPUT_ARTIFACT],
        decision_refs=[],
        environment={
            "executor": "simulated-fdm201-scf",
            "parameter_mutation": "none",
        },
        completed_at=COMPLETED_AT,
    )


def make_analysis_record(assessment: ConvergenceAssessment) -> ResultRecord:
    """The analysis result record of the convergence validation (AC-02).

    Carries the convergence metrics and the failure QC finding through
    the real ``register_result``; the warning records that no parameter
    mutation was performed.
    """
    return ResultRecord(
        result_id=ANALYSIS_RESULT_ID,
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref=RUN_REF,
        input_artifact_ids=[INPUT_ARTIFACT],
        output_artifact_ids=[OUTPUT_ARTIFACT],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        acceptance_ref="ACC-1",
        requirement_refs=[REQUIREMENT_REF],
        metrics=convergence_metrics("scf_drift", assessment),
        qc_findings=convergence_findings(assessment),
        warnings=[
            "scientific convergence failure reported as facts and"
            " deviations; no parameter mutation performed (smearing/mixing/"
            " convergence policy unchanged -- changing it would alter the"
            " method)"
        ],
    )


@dataclass(frozen=True)
class ScenarioFResult:
    """Everything the executed scenario produced (frozen, auditable)."""

    root: Path
    iterations: tuple[float, ...]
    tolerance: float
    assessment: ConvergenceAssessment
    worker_package: WorkerResultPackage
    analysis_record: ResultRecord


def execute_scenario_f(
    root: Path,
    iterations: tuple[float, ...] = NOT_CONVERGED_SERIES,
    max_iterations: int = SCF_MAX_ITERATIONS,
) -> ScenarioFResult:
    """Execute scenario F end to end and return the full evidence trail.

    Runs the merged analysis stack against the simulated DFT job: the
    convergence validation classifies the observed SCF iteration series
    against the frozen tolerance (a scientific failure), the computation
    worker registers its result package through the real
    ``register_worker_result`` (facts + FAILURE deviation, no decision
    refs, no parameter mutation) and the analysis path registers the
    convergence result record through the real ``register_result``.
    """
    acceptance = read_acceptance(root, "ACC-1")
    validate_acceptance_mode(acceptance)
    criterion = convergence_criterion_from_acceptance(acceptance)
    assessment = evaluate_convergence(
        iterations,
        criterion.tolerance,
        window=1,
        max_iterations=max_iterations,
    )
    register_worker_result(root, make_worker_package(assessment))
    analysis_record = make_analysis_record(assessment)
    register_result(root, analysis_record)
    return ScenarioFResult(
        root=root,
        iterations=iterations,
        tolerance=criterion.tolerance,
        assessment=assessment,
        worker_package=make_worker_package(assessment),
        analysis_record=analysis_record,
    )


def _code_identifiers(module: object) -> set[str]:
    """The identifiers a module's code actually references (ast).

    Docstrings are skipped: the module docstrings of the worker/analysis
    layers *discuss* the Supervisor decision surface to document that the
    package carries no decision semantics, so the scan is over the parsed
    AST (names and attributes in code) -- docstring text is a plain string
    constant and never produces Name/Attribute nodes.
    """
    tree = ast.parse(inspect.getsource(module))
    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    return identifiers


def _snapshot_state_files(root: Path) -> dict[str, bytes]:
    """Byte snapshot of the frozen-state trees of the workspace.

    Covers the protocol registry, the acceptance registry and the
    artifact manifest registry -- everything the scenario must leave
    untouched.
    """
    files: dict[str, bytes] = {}
    for rel_dir in ("protocols", "acceptance", "manifests"):
        base = root / rel_dir
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    files[str(path.relative_to(root))] = path.read_bytes()
    return files


# ---------------------------------------------------------------------------
# AC-02 (a): the failure is reported as facts/deviations, never a silent retry
# ---------------------------------------------------------------------------


def test_F_job_runs_technically_but_scientific_convergence_fails(tmp_path):
    # S5: "job technically runs but scientific convergence fails" -- the
    # simulated SCF job completes (technical fact "completed") while the
    # convergence validation classifies the non-settling series as
    # NOT_CONVERGED (R-CONV-N1) at the end of the frozen iteration budget.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(root)
    assert scenario.assessment.status is ConvergenceStatus.NOT_CONVERGED
    assert scenario.assessment.matched_rule_id == "R-CONV-N1"
    assert scenario.assessment.failure is True
    assert scenario.assessment.converged is False
    assert scenario.assessment.budget_exhausted is True
    facts = {fact.fact_id: fact for fact in scenario.worker_package.facts}
    assert facts["F-9"].name == "job_technical_status"
    assert facts["F-9"].value == "completed"


def test_F_diverging_series_failure_is_representable(tmp_path):
    # The other scientific-failure shape of the frozen rules: a series
    # whose per-iteration drift grows at every step is DIVERGING
    # (R-CONV-D1) -- a first-class typed failure, never silently adjusted.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(
        root, iterations=DIVERGING_SERIES, max_iterations=len(DIVERGING_SERIES)
    )
    assert scenario.assessment.status is ConvergenceStatus.DIVERGING
    assert scenario.assessment.matched_rule_id == "R-CONV-D1"
    assert scenario.assessment.failure is True
    assert scenario.assessment.final_drift > scenario.assessment.state.prior_max_drift
    facts = {fact.fact_id: fact for fact in scenario.worker_package.facts}
    assert facts["F-1"].value == "DIVERGING"


def test_F_worker_reports_failure_as_facts_and_deviation(tmp_path):
    # AC-02 (a): the worker result package carries the failure as typed
    # content -- the convergence facts (status, drifts, tolerance) and a
    # DeviationType.FAILURE deviation -- registered through the real
    # register_worker_result and read back byte-identical.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(root)
    stored = read_worker_result(root, WORKER_RESULT_ID)
    assert stored == scenario.worker_package
    assert len(stored.deviations) == 1
    deviation = stored.deviations[0]
    assert deviation.kind is DeviationType.FAILURE
    assert "scientific convergence not reached" in deviation.description
    assert "R-CONV-N1" in deviation.description
    assert "left unchanged" in deviation.description
    facts = {fact.fact_id: fact for fact in stored.facts}
    assert facts["F-1"].value == "NOT_CONVERGED"
    assert facts["F-2"].value == scenario.assessment.final_drift
    assert facts["F-3"].value == scenario.assessment.settling_drift
    assert facts["F-8"].value == scenario.tolerance
    assert stored.goal_id == GOAL_ID and stored.goal_version == "v1"
    assert stored.run_ref == RUN_REF


def test_F_analysis_record_carries_convergence_failure(tmp_path):
    # AC-02 (a): the analysis result record carries the convergence
    # metrics and the failure QC finding through the real register_result
    # -- the scientific failure lands in the analysis evidence, not in a
    # silent retry.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(root)
    stored = read_result(root, ANALYSIS_RESULT_ID)
    assert stored == scenario.analysis_record
    by_name = {entry["metric"]: entry["value"] for entry in stored.metrics}
    assert by_name["scf_drift"] == scenario.assessment.final_drift
    assert by_name["scf_drift_settling_drift"] == scenario.assessment.settling_drift
    assert by_name["scf_drift_max_drift"] == scenario.assessment.state.max_drift
    assert by_name["scf_drift_iterations"] == scenario.assessment.iterations_used
    finding = stored.qc_findings[0]
    assert "convergence" in finding
    assert "exceeds the frozen tolerance" in finding
    assert "PASS" not in finding and "FAIL" not in finding
    assert stored.acceptance_ref == "ACC-1"


def test_F_failure_reported_never_silent_retry(tmp_path):
    # AC-02 (a): the failure is on the record -- exactly one worker result
    # and one analysis result exist, the runs registry holds no
    # re-execution, and no retry policy record was written: the worker
    # layer reports, it never silently retries.
    root = build_scenario_workspace(tmp_path)
    execute_scenario_f(root)
    worker_results = list_worker_results(root)
    assert len(worker_results) == 1
    assert worker_results[0].deviations[0].kind is DeviationType.FAILURE
    runs_dir = root / "runs"
    assert list(runs_dir.glob("*.json")) == []
    assert (root / "resources").is_dir()  # retry policies would live here
    assert list((root / "resources").glob("*retry*")) == []
    # The analysis/worker modules expose no retry/resubmit callable.
    for module in (workers_results_module, results_module, computational_module):
        for name in dir(module):
            if name.startswith("_") or not callable(getattr(module, name)):
                continue
            lowered = name.lower()
            for prefix in ("retry", "resubmit", "rerun"):
                assert not lowered.startswith(prefix), name


def test_F_deviation_type_is_execution_fact_vocabulary_not_verdict():
    # The deviation vocabulary is the factual engineering vocabulary of
    # 10-EXPERIMENT-SUBSYSTEM.md SS4 ("report all deviations from
    # protocol; failures/interruptions") -- "failure" here is an execution
    # fact, never a requirement verdict (AC-02 of DEV-M6-G02).
    values = [member.value for member in DeviationType]
    assert values == ["protocol_deviation", "failure", "interruption"]
    for value in values:
        assert value.upper() not in ("PASS", "FAIL", "REPRODUCED")


# ---------------------------------------------------------------------------
# AC-02 (b): no automatic parameter mutation -- the failure is reported,
# the method is untouched, the Supervisor decides
# ---------------------------------------------------------------------------


def test_F_convergence_module_has_no_parameter_tuning_surface():
    # AC-02 (b) by construction: the convergence hook's inputs are
    # observations and frozen protocol values only -- there is no
    # parameter object to change, no set_/adjust/restart API, no
    # smearing-tuning callable anywhere on the public surface.
    parameters = inspect.signature(evaluate_convergence).parameters
    assert tuple(parameters) == (
        "iterations",
        "tolerance",
        "window",
        "max_iterations",
    )
    public_names = [
        name
        for name in dir(computational_module)
        if not name.startswith("_") and callable(getattr(computational_module, name))
    ]
    for name in public_names:
        lowered = name.lower()
        assert "smearing" not in lowered, name
        for prefix in (
            "tune",
            "adjust",
            "restart",
            "rerun",
            "resubmit",
            "set_",
            "alter",
        ):
            assert not lowered.startswith(prefix), name


def test_F_worker_does_not_change_smearing_mixing_parameters(tmp_path):
    # AC-02 (b): the simulated worker's facts carry the frozen
    # smearing/mixing values verbatim -- the worker executed with the
    # method policy as frozen, and its environment records that no
    # parameter mutation occurred (S5: changing the policy would alter
    # the method).
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(root)
    facts = {fact.fact_id: fact for fact in scenario.worker_package.facts}
    assert facts["F-6"].value == SMearing_WIDTH
    assert facts["F-7"].value == MIXING_SCHEME
    assert facts["F-5"].value == SCF_MAX_ITERATIONS
    assert facts["F-8"].value == TOLERANCE
    assert scenario.worker_package.environment["parameter_mutation"] == "none"
    # The facts match the frozen protocol method policy exactly.
    protocol = read_analysis_protocol(root, "ANL-1")
    method = protocol.methods[0]
    assert method["smearing_width"] == SMearing_WIDTH
    assert method["mixing"] == MIXING_SCHEME
    assert method["max_iterations"] == SCF_MAX_ITERATIONS
    assert method["tolerance"] == TOLERANCE


def test_F_failure_leaves_frozen_protocol_and_acceptance_byte_identical(tmp_path):
    # AC-02 (b): the method boundary -- the failure path never touches the
    # frozen protocol, the frozen acceptance or the artifact manifests.
    # Every byte of the frozen state trees is identical before and after
    # the scenario: changing smearing/mixing/convergence policy would
    # alter the method, and this path provably does not.
    root = build_scenario_workspace(tmp_path)
    before = _snapshot_state_files(root)
    assert before  # the frozen protocol/acceptance/manifests exist
    execute_scenario_f(root)
    after = _snapshot_state_files(root)
    assert before == after
    # The failure is still on the record -- the untouched frozen state is
    # not a missed report.
    assert read_worker_result(root, WORKER_RESULT_ID).deviations[0].kind is (
        DeviationType.FAILURE
    )


def test_F_supervisor_decision_surface_not_fabricated_by_worker(tmp_path):
    # AC-02 (b): the Supervisor decision surface (core.models
    # SupervisorDecision / DecisionType) is where diagnosis/research/
    # recovery decisions would live -- the worker layer never fabricates
    # one: the package's decision_refs stays empty, the decisions
    # registry holds no record, and the analysis/worker modules never
    # construct or import a SupervisorDecision.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(root)
    assert scenario.worker_package.decision_refs == []
    decisions_dir = root / "decisions"
    assert decisions_dir.is_dir()  # created by the init workspace tree
    assert list(decisions_dir.glob("*.json")) == []
    for module in (workers_results_module, results_module, computational_module):
        identifiers = _code_identifiers(module)
        assert "SupervisorDecision" not in identifiers, module.__name__
        assert "DecisionType" not in identifiers, module.__name__


def test_F_supervisor_decision_vocabulary_is_diagnosis_research_recovery():
    # S5: "Worker reports facts; Supervisor decides diagnosis/research/
    # recovery" -- the frozen Supervisor decision vocabulary (DecisionType)
    # is exactly the surface where those decisions would live, and the
    # SupervisorDecision record is actor-"supervisor"-typed: the scenario
    # path cannot produce one (no construction anywhere, see
    # test_F_supervisor_decision_surface_not_fabricated_by_worker).
    for member in (
        DecisionType.RESEARCH_REQUEST,
        DecisionType.RECOVERY_ENTRY,
        DecisionType.METHOD_REDESIGN_ENTRY,
        DecisionType.GOAL_REVISION,
        DecisionType.REQUIREMENT_CLOSURE,
    ):
        assert member in DecisionType
    assert DecisionType.RECOVERY_ENTRY.value == "RECOVERY_ENTRY"
    assert DecisionType.RESEARCH_REQUEST.value == "RESEARCH_REQUEST"
    # The frozen record shape pins the supervisor actor.
    assert "actor" in SupervisorDecision.__dataclass_fields__


# ---------------------------------------------------------------------------
# Paradigm boundaries (deterministic path, safe ids, purity)
# ---------------------------------------------------------------------------


def test_F_deterministic_scenario_repeatable(tmp_path):
    # Same workspace inputs -> same scenario evidence: the assessment, the
    # worker package and the analysis record are byte-identical across
    # repeated executions on fresh workspaces (no randomness, no wall
    # clock anywhere in the path).
    first = execute_scenario_f(build_scenario_workspace(tmp_path / "first"))
    second = execute_scenario_f(build_scenario_workspace(tmp_path / "second"))
    assert first.assessment == second.assessment
    assert first.worker_package == second.worker_package
    assert first.analysis_record == second.analysis_record
    assert read_worker_result(first.root, WORKER_RESULT_ID) == read_worker_result(
        second.root, WORKER_RESULT_ID
    )
    assert read_result(first.root, ANALYSIS_RESULT_ID) == read_result(
        second.root, ANALYSIS_RESULT_ID
    )


def test_F_scenario_uses_safe_ids_only(tmp_path):
    # Every id on the scenario path is a safe registry id: pinned
    # alphanumeric ids with no path separators and no glob metacharacters
    # (the artifact-id boundary hardened by FND-M9-G02-01).
    ids = (
        "ANL-1",
        "ACC-1",
        INPUT_ARTIFACT,
        OUTPUT_ARTIFACT,
        RUN_REF,
        WORKER_RESULT_ID,
        ANALYSIS_RESULT_ID,
        REQUIREMENT_REF,
        GOAL_ID,
        CONTEXT_ID,
        "F-1",
        "DEV-1",
        "D-1",
    )
    for value in ids:
        assert value not in ("", ".", "..")
        assert "/" not in value and "\\" not in value
        assert not any(char in value for char in "*?[]")
    root = build_scenario_workspace(tmp_path)
    execute_scenario_f(root)
    assert {result.result_id for result in list_worker_results(root)} == {
        WORKER_RESULT_ID
    }
    assert {result.result_id for result in results_module.list_results(root)} == {
        ANALYSIS_RESULT_ID
    }


def test_F_frozen_records_reject_mutation(tmp_path):
    # Frozen dataclasses throughout the evidence trail: the scenario
    # records cannot be mutated after construction.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_f(root)
    records = (
        scenario.assessment,
        scenario.assessment.state,
        scenario.assessment.state.input,
        scenario.worker_package,
        scenario.worker_package.facts[0],
        scenario.worker_package.deviations[0],
        scenario.analysis_record,
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)


def test_F_computational_module_pure_no_io_no_randomness(tmp_path):
    # The convergence hook is pure by construction: no randomness, no wall
    # clock, no network, no persistence and no parameter-mutation surface
    # in the module source (AC-01 of DEV-M9-G05).
    source = inspect.getsource(computational_module)
    for forbidden in (
        "import random",
        "random.",
        "time.time",
        "datetime.now",
        "timezone",
        "urllib",
        "requests",
        "socket",
        "open(",
        "os.",
        "sys.",
        "pathlib",
        "atomic_write",
    ):
        assert forbidden not in source, forbidden
    # The scenario still executes deterministically on this path.
    root = build_scenario_workspace(tmp_path)
    execute_scenario_f(root)
    assert len(list_worker_results(root)) == 1
