"""FDM-201 simulated scenario D -- adsorption statistically inconclusive (DEV-M9-G06).

Scenario D is the frozen acceptance fixture **S3** of
``examples/fdm-201/simulated-scenarios.md``: three batch results have
wide uncertainty overlapping the equivalence bounds. Expected (frozen
acceptance): **no PASS/FAIL** -- the outcome layer stays
OPEN/INCONCLUSIVE-consistent -- and dynamic additional runs are
generated according to the frozen sample-size policy.

Every test name contains ``D`` and the module basename matches the
``tests/scenarios/test_D*`` glob, so the frozen verification
``python -m pytest -q tests/scenarios -k "D or F"`` selects this suite.
The ``ac01`` sections map one-to-one to acceptance criterion AC-01 of
DEV-M9-G06:

* Scenario D cannot be coerced to PASS: the equivalence verdict
  vocabulary (``EquivalenceVerdict``) has no PASS/FAIL/REPRODUCED
  member, the wide data are INCONCLUSIVE (``R-EQ-3`` -- a
  non-significant confidence interval overlapping the frozen bounds,
  never EQUIVALENT, never REPRODUCED), the replicate sufficiency status
  is never SUFFICIENT for the wide data (``R-REP-I1`` INSUFFICIENT),
  and the analysis path never writes a ``RequirementOutcome.REPRODUCED``
  -- the requirements registry stays empty and the outcome layer keeps
  the OPEN/INCONCLUSIVE vocabulary;
* Scenario D can request more independent Runs: the real
  ``evaluate_replicate_sufficiency`` assessment carries
  ``requested_additional_runs >= 1``, and the request scales per the
  frozen sample-size formula ``ceil(n * (h / threshold) ** 2) - n``
  (``h`` = z-based relative half-width) when the spread widens.

The scenario runs the merged analysis stack end to end against
simulated FDM-201 adsorption data: the deterministic one-paper project
(``ANL-1`` ``v1`` frozen PRIMARY protocol, raw manifests ``ART-001``..,
the frozen EQUIVALENCE acceptance ``ACC-1`` carrying the margin, the
precision and the independent-n floor), three simulated batches executed
as independent runs (``RUN-001``..``RUN-003``), the real
``analysis.statistics`` (``equivalence_bounds_from_acceptance`` /
``decide_equivalence`` / effect-CI math / reporting hooks), the real
``analysis.replication`` (``replicate_criterion_from_acceptance`` /
``evaluate_replicate_sufficiency`` / reporting hooks) and the real
``analysis.results.register_result`` (exactly-once registry, reference
resolution included).

Determinism mirrors the M9 suites: fixed identities/timestamps, pinned
safe ids only (``ANL-1``, ``ART-001``, ``ACC-1``, ``RUN-001``,
``REQ-1``), no randomness, no wall clock, no network.
"""

from __future__ import annotations

import ast
import inspect
import math
from dataclasses import FrozenInstanceError, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import stdev

import pytest

import scientific_reproduction.analysis.replication as replication_module
import scientific_reproduction.analysis.results as results_module
import scientific_reproduction.analysis.statistics as statistics_module
from scientific_reproduction.analysis.protocols import (
    freeze_primary_protocol,
    register_analysis_record,
)
from scientific_reproduction.analysis.replication import (
    DEFAULT_MIN_INDEPENDENT,
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
    list_results,
    read_result,
    register_result,
)
from scientific_reproduction.analysis.statistics import (
    EquivalenceBounds,
    EquivalenceVerdict,
    decide_equivalence,
    effect_confidence_interval,
    effect_metrics,
    equivalence_bounds_from_acceptance,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ArtifactManifest,
    Criticality,
    DecisionMode,
    PrimaryOrExploratory,
    RequirementOutcome,
)
from scientific_reproduction.core.rules.outcome import (
    RequirementClosureState,
    RequirementOutcomeRecord,
    classify_requirement_outcome,
)
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.inventory import REQUIREMENTS_STATE_DIR
from scientific_reproduction.planning.plan import (
    read_acceptance,
    read_analysis_protocol,
    register_acceptance,
)

#: The standard-normal critical value of the 0.95 two-sided level --
#: pinned so the interval math is asserted against a fixed value.
Z_095 = 1.959963984540054

#: Deterministic author/committer identity (mirrors protocol_helpers).
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp for the frozen PRIMARY protocol.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Primary target DOI of the one-paper project (17-FDM201-REFERENCE-CASE.md).
DOI = "10.1039/D5TA00771B"

#: The published seed value the batches reproduce (mmol g^-1 uptake).
TARGET = 10.0

#: The frozen equivalence half-width around the target (effect-space
#: region [-MARGIN, +MARGIN]; 5 % of the target).
MARGIN = 0.5

#: The frozen relative-half-width precision threshold of the mean.
PRECISION = 0.1

#: The frozen independent-n floor (07-... SS2 default: n >= 3).
MIN_INDEPENDENT = 3

#: Scenario D wide batch values: three independent batches whose spread
#: (sample sd 5) makes the mean interval straddle the equivalence bounds.
WIDE_BATCHES = (10.0, 15.0, 5.0)

#: A mid-spread group of three batches (sample sd 2): still INSUFFICIENT,
#: requesting fewer additional runs than the wide group.
MID_BATCHES = (10.0, 12.0, 8.0)

#: A tight group of three batches (sample sd 0.1): the interval lies
#: entirely inside the frozen bounds -- the contrast case that shows the
#: wide verdict is evidence-driven, never coerced.
TIGHT_BATCHES = (10.0, 10.2, 10.1)

#: The artifact manifest ids of the three simulated batches (run-linked).
BATCH_ARTIFACTS = ("ART-001", "ART-002", "ART-003")

#: The run ids of the three simulated batches.
BATCH_RUNS = ("RUN-001", "RUN-002", "RUN-003")

#: The result ids of the three batch result records.
BATCH_RESULTS = ("RES-D1", "RES-D2", "RES-D3")

#: The analysis result id of the equivalence + sufficiency evidence.
ANALYSIS_RESULT = "RES-D-ANL"


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
    """Build the schema-valid draft PRIMARY adsorption analysis protocol."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[{"name": "batch_uptake_isotherm_fit"}],
    )


def make_manifest(artifact_id: str, *, run_id: str) -> ArtifactManifest:
    """Build a schema-valid raw artifact manifest (no file access)."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=f"file:///raw/{artifact_id}.csv",
        sha256="a" * 64,
        size_bytes=1024,
        created_at="2026-01-01T00:00:00Z",
        run_id=run_id,
    )


def make_acceptance(acceptance_id: str) -> AcceptanceCriteria:
    """Build the frozen EQUIVALENCE acceptance record of scenario D.

    The single criteria entry carries the frozen margin (DEV-M9-G03 AC-03),
    the frozen independent-n floor and the frozen precision threshold
    (DEV-M9-G04 AC-02/AC-03); ``target`` is the published seed value the
    analysis step computes the effect against.
    """
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id="G-1",
        version="v1",
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
            "frozen FDM-201 adsorption equivalence criteria: margin from"
            " the published uncertainty, precision and independent floor"
            " per the frozen sample-size policy"
            " (07-STATISTICS-AND-ACCEPTANCE.md SS8/SS9)"
        ),
    )


def build_scenario_workspace(tmp_path: Path) -> Path:
    """Initialize the project with the registered entities the scenario uses.

    Registers, deterministically: the frozen PRIMARY protocol ``ANL-1``
    ``v1`` (DEV-M9-G01 registry), the three batch artifact manifests
    (``manifests/``) and the frozen EQUIVALENCE acceptance ``ACC-1``.
    """
    root = init_project(tmp_path)
    register_analysis_record(root, make_protocol("ANL-1"))
    draft = read_analysis_protocol(root, "ANL-1")
    freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    for artifact_id, run_id in zip(BATCH_ARTIFACTS, BATCH_RUNS, strict=True):
        registry.register(make_manifest(artifact_id, run_id=run_id))
    register_acceptance(root, make_acceptance("ACC-1"))
    return root


@dataclass(frozen=True)
class ScenarioDResult:
    """Everything the executed scenario produced (frozen, auditable)."""

    root: Path
    batches: tuple[float, ...]
    mean: float
    standard_error: float
    effect: float
    ci_lower: float
    ci_upper: float
    bounds: EquivalenceBounds
    verdict: object  # EquivalenceAssessment
    criterion: object  # ReplicateCriterion
    assessment: object  # ReplicateSufficiencyAssessment
    analysis_record: ResultRecord


def execute_scenario_d(
    root: Path,
    batches: tuple[float, ...] = WIDE_BATCHES,
) -> ScenarioDResult:
    """Execute scenario D end to end and return the full evidence trail.

    Runs the merged analysis stack against the simulated batches: the
    three batch results are executed as independent runs and registered
    through the real ``register_result``; the analysis step then reads the
    frozen criterion (margin + precision + floor) from the registered
    acceptance, evaluates replicate sufficiency over the three batch
    values, computes the effect confidence interval against the published
    target and decides equivalence -- and registers the analysis result
    record carrying the equivalence and sufficiency evidence. The verdict
    and the assessment are the pure outputs of the real modules; the
    records are the exact registered bytes.
    """
    acceptance = read_acceptance(root, "ACC-1")
    bounds = equivalence_bounds_from_acceptance(acceptance)
    criterion = replicate_criterion_from_acceptance(acceptance)

    # Execute the three simulated batches as independent runs.
    for index, value in enumerate(batches):
        result = ResultRecord(
            result_id=BATCH_RESULTS[index],
            analysis_id="ANL-1",
            protocol_version="v1",
            run_ref=BATCH_RUNS[index],
            input_artifact_ids=[BATCH_ARTIFACTS[index]],
            primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
            acceptance_ref="ACC-1",
            metrics=[{"metric": "batch_level_uptake", "value": value}],
            qc_findings=[
                f"batch {BATCH_RUNS[index]} executed; raw value {value:g}"
                " mmol g^-1 carried into the replicate analysis"
            ],
        )
        register_result(root, result)

    # The analysis step over the three batches as independent replicates.
    assessment = evaluate_replicate_sufficiency(
        batches,
        min_independent=criterion.min_independent,
        precision_threshold=criterion.precision_threshold,
    )
    mean = assessment.state.mean
    standard_error = assessment.state.standard_error
    assert standard_error is not None  # three batches: n >= 2
    effect = mean - TARGET
    ci = effect_confidence_interval(effect, standard_error)
    verdict = decide_equivalence(effect, ci, bounds)

    metrics = effect_metrics("uptake_effect", effect, ci)
    metrics += sufficiency_metrics("uptake", assessment)
    analysis_record = ResultRecord(
        result_id=ANALYSIS_RESULT,
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref=BATCH_RUNS[0],
        input_artifact_ids=[BATCH_ARTIFACTS[0]],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        acceptance_ref="ACC-1",
        requirement_refs=["REQ-1"],
        metrics=metrics,
        uncertainty=sufficiency_uncertainty_payload(assessment),
        qc_findings=sufficiency_findings(assessment),
        warnings=[
            f"equivalence decision {verdict.verdict.value}"
            f" ({verdict.matched_rule_id}): the effect confidence interval"
            " overlaps the frozen equivalence bounds; no PASS/FAIL and no"
            " RequirementOutcome.REPRODUCED is ever written by this path"
        ],
    )
    register_result(root, analysis_record)
    return ScenarioDResult(
        root=root,
        batches=batches,
        mean=mean,
        standard_error=standard_error,
        effect=effect,
        ci_lower=ci.lower,
        ci_upper=ci.upper,
        bounds=bounds,
        verdict=verdict,
        criterion=criterion,
        assessment=assessment,
        analysis_record=analysis_record,
    )


def _import_paths(module: object) -> set[str]:
    """The dotted import roots of a module's source (ast, deterministic)."""
    source = inspect.getsource(module)
    tree = ast.parse(source)
    paths: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            paths.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            paths.add(node.module.split(".")[0])
    return paths


# ---------------------------------------------------------------------------
# AC-01: the wide evidence cannot be coerced to PASS
# ---------------------------------------------------------------------------


def test_D_three_batches_wide_uncertainty_overlaps_equivalence_bounds(tmp_path):
    # S3: "three batch results have wide uncertainty overlapping
    # equivalence bounds" -- the three simulated batches have a large
    # spread (sample sd 5) and the mean interval straddles both frozen
    # bounds (it is wider than the region and overlaps every bound).
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_d(root)
    batches = scenario.batches
    assert len(batches) == 3
    assert stdev(batches) == pytest.approx(5.0)
    assert stdev(batches) > 2 * MARGIN
    # The interval overlaps every frozen bound: lower below the region,
    # upper above the region -- the "wide uncertainty overlapping the
    # equivalence bounds" of S3.
    assert scenario.ci_lower < scenario.bounds.lower
    assert scenario.ci_lower < scenario.bounds.upper
    assert scenario.ci_upper > scenario.bounds.upper
    assert scenario.ci_upper > scenario.bounds.lower


def test_D_equivalence_verdict_is_inconclusive_not_equivalent(tmp_path):
    # The decision over the wide evidence is INCONCLUSIVE through the
    # ordered rule table: the interval is not entirely inside the bounds
    # (no R-EQ-1) and not entirely outside them either (no R-EQ-2) --
    # R-EQ-3, the "interval overlaps the decision boundaries" default.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_d(root)
    verdict = scenario.verdict
    assert verdict.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert verdict.matched_rule_id == "R-EQ-3"
    assert verdict.equivalent is False
    # The interval crosses zero: a non-significant difference -- the
    # forbidden "p > 0.05 alone" shape that can never establish
    # equivalence by itself (DEV-M9-G03 AC-01).
    assert verdict.input.ci.crosses_zero is True
    # The point estimate is inside the region -- yet the decision is not
    # EQUIVALENT: the uncertainty interval carries the evidence.
    assert scenario.bounds.lower < scenario.effect < scenario.bounds.upper


def test_D_equivalence_vocabulary_has_no_pass_or_reproduced_member():
    # AC-01 by construction: the equivalence verdict vocabulary has no
    # PASS/FAIL/REPRODUCED member -- statistics decides, the outcome layer
    # closes, and nothing on this path can emit a pass verdict.
    values = [member.value for member in EquivalenceVerdict]
    assert values == ["EQUIVALENT", "NOT_EQUIVALENT", "INCONCLUSIVE"]
    for value in values:
        assert "PASS" not in value
        assert "FAIL" not in value
        assert value != "REPRODUCED"
    assert not hasattr(statistics_module, "REPRODUCED")
    assert not hasattr(statistics_module, "PASS")


def test_D_verdict_inconclusive_string_is_frozen_outcome_string():
    # The INCONCLUSIVE verdict is the frozen outcome vocabulary's string
    # value (RequirementOutcome.INCONCLUSIVE): the decision vocabulary
    # feeds the outcome layer as-is, and the only closure consistent with
    # the wide evidence is INCONCLUSIVE -- never REPRODUCED.
    assert EquivalenceVerdict.INCONCLUSIVE.value == RequirementOutcome.INCONCLUSIVE.value
    assert EquivalenceVerdict.EQUIVALENT.value == "EQUIVALENT"
    assert RequirementOutcome.REPRODUCED.value == "REPRODUCED"


def test_D_wide_data_never_sufficient_not_coerced_to_pass(tmp_path):
    # The replicate sufficiency status is never SUFFICIENT for the wide
    # data: the relative half-width exceeds the frozen precision
    # threshold, so the assessment is INSUFFICIENT (R-REP-I1) -- the
    # "insufficient evidence" report, never a forced pass or fail.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_d(root)
    assessment = scenario.assessment
    assert assessment.status is ReplicateStatus.INSUFFICIENT
    assert assessment.matched_rule_id == "R-REP-I1"
    assert assessment.sufficient is False
    assert assessment.relative_half_width > PRECISION
    # The status vocabulary itself has no PASS/FAIL member.
    for member in ReplicateStatus:
        assert "PASS" not in member.value
        assert "FAIL" not in member.value
        assert member.value != "REPRODUCED"


def test_D_no_requirement_reproduced_written_by_analysis_path(tmp_path):
    # AC-01 (a): no RequirementOutcome.REPRODUCED is ever written by the
    # analysis path. After the full scenario the requirements registry
    # holds no record at all -- the analysis path registers result
    # records only, and requirement_refs stays a pure linkage that never
    # closes an outcome.
    root = build_scenario_workspace(tmp_path)
    execute_scenario_d(root)
    requirements_dir = root / REQUIREMENTS_STATE_DIR
    assert requirements_dir.is_dir()  # created by the init workspace tree
    assert list(requirements_dir.glob("*.json")) == []
    # The linkage is pure: the analysis record references REQ-1 but no
    # requirement record exists anywhere in the workspace.
    stored = read_result(root, ANALYSIS_RESULT)
    assert stored.requirement_refs == ["REQ-1"]
    assert list(requirements_dir.glob("*.json")) == []


def test_D_outcome_layer_stays_open_or_inconclusive_consistent():
    # AC-01 (a): the outcome layer keeps the OPEN/INCONCLUSIVE vocabulary.
    # A Requirement that is still OPEN classifies to UNDETERMINED (the
    # outcome rules' default, R-REQOUT-5) and INCONCLUSIVE evidence
    # classifies to INCONCLUSIVE (R-REQOUT-4) -- and the analysis path
    # never constructs the review record these rules consume, so no
    # REPRODUCED closure can be coerced out of the scenario.
    open_record = RequirementOutcomeRecord(
        requirement_id="REQ-1",
        criticality=Criticality.CRITICAL,
        outcome=RequirementOutcome.OPEN,
    )
    open_assessment = classify_requirement_outcome(open_record)
    assert open_assessment.matched_rule_id == "R-REQOUT-5"
    assert open_assessment.state is RequirementClosureState.UNDETERMINED
    inconclusive_record = replace(open_record, outcome=RequirementOutcome.INCONCLUSIVE)
    inconclusive_assessment = classify_requirement_outcome(inconclusive_record)
    assert inconclusive_assessment.matched_rule_id == "R-REQOUT-4"
    assert inconclusive_assessment.state is RequirementClosureState.INCONCLUSIVE
    # The scenario's own requirement never gets a record at all (see
    # test_D_no_requirement_reproduced_written_by_analysis_path): its
    # outcome stays OPEN in the workspace.
    assert RequirementOutcome.OPEN.value == "OPEN"


def test_D_registered_results_carry_inconclusive_evidence_no_verdict_words(tmp_path):
    # The evidence lands in the real result registry: three batch records
    # plus the analysis record, all read back byte-identical, carrying the
    # effect interval, the sufficiency metrics and the QC findings --
    # with no PASS/FAIL verdict words anywhere on the path.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_d(root)
    for batch_result in BATCH_RESULTS:
        stored = read_result(root, batch_result)
        assert stored.analysis_id == "ANL-1"
        assert stored.protocol_version == "v1"
        assert stored.acceptance_ref == "ACC-1"
    stored = read_result(root, ANALYSIS_RESULT)
    assert stored == scenario.analysis_record
    by_name = {entry["metric"]: entry["value"] for entry in stored.metrics}
    assert by_name["uptake"] == pytest.approx(scenario.mean)
    assert by_name["uptake_effect"] == pytest.approx(scenario.effect)
    assert by_name["uptake_effect_ci_lower"] == pytest.approx(scenario.ci_lower)
    assert by_name["uptake_effect_ci_upper"] == pytest.approx(scenario.ci_upper)
    assert by_name["uptake_requested_additional_runs"] >= 1
    assert stored.uncertainty["method"] == "confidence_interval"
    assert stored.uncertainty["n"] == 3
    finding = stored.qc_findings[0]
    assert "PASS" not in finding and "FAIL" not in finding
    assert "additional independent run" in finding


def test_D_contrast_tight_data_equivalent_wide_data_inconclusive(tmp_path):
    # The verdict is a pure function of the evidence: the same analysis on
    # tight data (interval entirely inside the frozen bounds) is
    # EQUIVALENT (R-EQ-1) and SUFFICIENT -- while the wide data stays
    # INCONCLUSIVE and INSUFFICIENT. The wide scenario is never coerced;
    # the decision boundary is the frozen bounds and the frozen precision.
    tight_root = build_scenario_workspace(tmp_path / "tight")
    tight = execute_scenario_d(tight_root, batches=TIGHT_BATCHES)
    assert tight.verdict.verdict is EquivalenceVerdict.EQUIVALENT
    assert tight.verdict.matched_rule_id == "R-EQ-1"
    assert tight.assessment.status is ReplicateStatus.SUFFICIENT
    assert tight.assessment.requested_additional_runs == 0
    # The wide group under the identical frozen acceptance is never
    # coerced to the tight result: INCONCLUSIVE / INSUFFICIENT.
    wide_root = build_scenario_workspace(tmp_path / "wide")
    wide = execute_scenario_d(wide_root, batches=WIDE_BATCHES)
    assert wide.verdict.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert wide.verdict.matched_rule_id == "R-EQ-3"
    assert wide.assessment.status is ReplicateStatus.INSUFFICIENT
    assert wide.assessment.requested_additional_runs >= 1


# ---------------------------------------------------------------------------
# AC-01: the assessment requests more independent Runs (frozen policy)
# ---------------------------------------------------------------------------


def test_D_additional_runs_requested_by_frozen_policy(tmp_path):
    # S3: "dynamic additional runs generated according to frozen
    # sample-size policy" -- the real assessment over the wide batches
    # carries a concrete request for more independent runs, and the
    # request is recorded in the registered analysis result.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_d(root)
    assessment = scenario.assessment
    assert assessment.requested_additional_runs >= 1
    # Frozen formula pinned: ceil(n * (h / threshold) ** 2) - n.
    h = assessment.relative_half_width
    expected = max(
        math.ceil(len(scenario.batches) * (h / PRECISION) ** 2)
        - len(scenario.batches),
        1,
    )
    assert assessment.requested_additional_runs == expected
    assert expected == 94  # the deterministic request of the wide group
    stored = read_result(root, ANALYSIS_RESULT)
    by_name = {entry["metric"]: entry["value"] for entry in stored.metrics}
    assert by_name["uptake_requested_additional_runs"] == 94


def test_D_request_scales_with_spread_per_frozen_formula(tmp_path):
    # AC-01 (b): the request scales per the frozen formula when the spread
    # widens -- a wider group at the same n and threshold requests
    # strictly more additional independent runs (the relative half-width
    # scales with the spread, the request with its square).
    mid = execute_scenario_d(
        build_scenario_workspace(tmp_path / "mid"), batches=MID_BATCHES
    )
    wide = execute_scenario_d(
        build_scenario_workspace(tmp_path / "wide"), batches=WIDE_BATCHES
    )
    assert mid.assessment.status is ReplicateStatus.INSUFFICIENT
    assert wide.assessment.relative_half_width > mid.assessment.relative_half_width
    assert (
        wide.assessment.requested_additional_runs
        > mid.assessment.requested_additional_runs
    )
    assert mid.assessment.requested_additional_runs >= 1
    # Deterministic formula pinned for both groups.
    for scenario in (mid, wide):
        h = scenario.assessment.relative_half_width
        expected = max(
            math.ceil(3 * (h / PRECISION) ** 2) - 3, 1
        )
        assert scenario.assessment.requested_additional_runs == expected


def test_D_request_reads_frozen_precision_and_floor_verbatim(tmp_path):
    # The request derives from the frozen acceptance criterion, read
    # verbatim (AC-03 of DEV-M9-G04): the precision threshold and the
    # independent-n floor of the registered record drive the assessment,
    # and a stricter frozen threshold yields a larger request -- the
    # policy is frozen, never inferred from the observed spread.
    root = build_scenario_workspace(tmp_path)
    acceptance = read_acceptance(root, "ACC-1")
    criterion = replicate_criterion_from_acceptance(acceptance)
    assert criterion.precision_threshold == PRECISION
    assert criterion.min_independent == MIN_INDEPENDENT == DEFAULT_MIN_INDEPENDENT
    scenario = execute_scenario_d(root)
    assert scenario.criterion == criterion
    assert scenario.assessment.input.precision_threshold == PRECISION
    assert scenario.assessment.input.min_independent == 3


def test_D_below_floor_indeterminate_requests_runs_not_pass_fail(tmp_path):
    # Even below the determination point the request is explicit: two
    # wide batches (below the frozen floor of 3) are INDETERMINATE and
    # request the run needed to reach the floor -- still no PASS/FAIL.
    two = (10.0, 15.0)
    assessment = evaluate_replicate_sufficiency(
        two, min_independent=MIN_INDEPENDENT, precision_threshold=PRECISION
    )
    assert assessment.status is ReplicateStatus.INDETERMINATE
    assert assessment.matched_rule_id == "R-REP-U1"
    assert assessment.requested_additional_runs == 1
    finding = sufficiency_findings(assessment)[0]
    assert "PASS" not in finding and "FAIL" not in finding


# ---------------------------------------------------------------------------
# Paradigm boundaries (deterministic path, safe ids, purity)
# ---------------------------------------------------------------------------


def test_D_deterministic_scenario_repeatable(tmp_path):
    # Same workspace inputs -> same scenario evidence: the assessment, the
    # verdict and the registered records are byte-identical across
    # repeated executions on fresh workspaces (no randomness, no wall
    # clock anywhere in the path).
    first_root = build_scenario_workspace(tmp_path / "first")
    first = execute_scenario_d(first_root)
    second_root = build_scenario_workspace(tmp_path / "second")
    second = execute_scenario_d(second_root)
    assert first.assessment == second.assessment
    assert first.verdict == second.verdict
    assert first.criterion == second.criterion
    assert read_result(first_root, ANALYSIS_RESULT) == read_result(
        second_root, ANALYSIS_RESULT
    )
    assert first.assessment.requested_additional_runs == 94


def test_D_scenario_uses_safe_ids_only(tmp_path):
    # Every id on the scenario path is a safe registry id: pinned
    # alphanumeric ids with no path separators and no glob metacharacters
    # (the artifact-id boundary hardened by FND-M9-G02-01).
    ids = (
        *BATCH_ARTIFACTS,
        *BATCH_RUNS,
        *BATCH_RESULTS,
        ANALYSIS_RESULT,
        "ANL-1",
        "ACC-1",
        "REQ-1",
        "G-1",
    )
    for value in ids:
        assert value not in ("", ".", "..")
        assert "/" not in value and "\\" not in value
        assert not any(char in value for char in "*?[]")
    root = build_scenario_workspace(tmp_path)
    execute_scenario_d(root)
    registered_ids = {record.result_id for record in list_results(root)}
    assert registered_ids == {*BATCH_RESULTS, ANALYSIS_RESULT}


def test_D_analysis_path_never_imports_requirement_closure_layers():
    # AC-01 (a): the modules of the analysis path never import the
    # requirement closure layers (core.rules outcome/closure, the
    # planning.inventory requirement registry) -- requirement closure is
    # structurally out of reach of statistics, replication and results.
    for module in (statistics_module, replication_module, results_module):
        paths = _import_paths(module)
        for forbidden in ("rules", "inventory"):
            assert forbidden not in paths, module.__name__
        assert "RequirementOutcome" not in paths, module.__name__


def test_D_analysis_modules_pure_no_io_no_randomness(tmp_path):
    # The decision modules are pure: no randomness, no wall clock, no
    # network and no mutation surface in the statistics/replication
    # sources; the results module writes only through the frozen
    # atomic registry of its own records.
    for module in (statistics_module, replication_module):
        source = inspect.getsource(module)
        for forbidden in (
            "import random",
            "random.",
            "time.time",
            "datetime.now",
            "urllib",
            "requests",
            "socket",
            "os.",
            "sys.",
            "pathlib",
        ):
            assert forbidden not in source, forbidden
        for name in dir(module):
            if name.startswith("_") or not callable(getattr(module, name)):
                continue
            lowered = name.lower()
            for prefix in ("tune", "adjust", "restart", "rerun", "resubmit", "set_"):
                assert not lowered.startswith(prefix), name
    # The end-to-end scenario still runs deterministically on this path.
    root = build_scenario_workspace(tmp_path)
    execute_scenario_d(root)


def test_D_frozen_records_reject_mutation(tmp_path):
    # Frozen dataclasses throughout the evidence trail: the scenario
    # records cannot be mutated after construction.
    root = build_scenario_workspace(tmp_path)
    scenario = execute_scenario_d(root)
    records = (
        scenario.analysis_record,
        scenario.assessment.input,
        scenario.assessment.state,
        scenario.verdict.input,
        scenario.verdict.input.ci,
        scenario.bounds,
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)
