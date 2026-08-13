"""Computational convergence and sampling validation hooks (DEV-M9-G05).

Every test name contains "computational" so ``python -m pytest -q
tests/analysis -k computational`` selects the whole suite and nothing
else (no other test in the repository matches the keyword). The
``ac01``/``ac02``/``ac03`` sections map one-to-one to the acceptance
criteria of DEV-M9-G05:

* ``ac01`` -- a scientific convergence failure (drift not settling,
  SCF/GCMC/MD-style iteration non-convergence) is representable as a
  first-class typed result, and the hook is **pure by construction**: it
  has no parameter input to change, no restart/alter API, no I/O, never
  mutates its inputs and never auto-tunes anything -- the hook reports,
  it never adjusts;
* ``ac02`` -- the Monte Carlo/sampling uncertainty hook reports
  uncertainty (mean, standard error, spread, confidence interval) from a
  sample series, deterministic and stdlib-only (``statistics``; no
  scipy/numpy), with stable behavior and stable error messages on
  degenerate inputs (empty series, single sample, nan/inf);
* ``ac03`` -- the validation output populates the exact shapes the
  DEV-M9-G02 acceptance path consumes (``ResultRecord.metrics`` /
  ``uncertainty`` / ``qc_findings``, proven through the real
  ``register_result``), references the frozen ``DecisionMode.CONVERGENCE``,
  and never itself decides acceptance (no PASS/FAIL/REPRODUCED member,
  no requirement state touched).

The deterministic path mirrors ``protocol_helpers``: every fixture uses
fixed identities/timestamps, so all records are deterministic.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from statistics import NormalDist, fmean, stdev

import pytest
from protocol_helpers import FROZEN_AT, init_project, make_protocol

import scientific_reproduction.analysis.computational as computational_module
from scientific_reproduction.analysis.computational import (
    CONVERGENCE_RULES,
    CONVERGENCE_RULESET_VERSION,
    DEFAULT_SAMPLING_CONFIDENCE_LEVEL,
    VALIDATION_DECISION_MODE,
    ComputationalValidationError,
    ConvergenceAssessment,
    ConvergenceCriterion,
    ConvergenceCriterionError,
    ConvergenceInput,
    ConvergenceRuleDecision,
    ConvergenceState,
    ConvergenceStatus,
    InvalidConvergenceInputError,
    InvalidSamplingInputError,
    SamplingInterval,
    SamplingUncertaintyReport,
    UnsupportedDecisionModeError,
    convergence_criterion_from_acceptance,
    convergence_findings,
    convergence_metrics,
    evaluate_convergence,
    sampling_findings,
    sampling_metrics,
    sampling_uncertainty,
    sampling_uncertainty_payload,
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
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ArtifactManifest,
    DecisionMode,
    PrimaryOrExploratory,
)
from scientific_reproduction.planning.plan import (
    read_acceptance,
    read_analysis_protocol,
    register_acceptance,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_manifest(artifact_id: str, *, run_id: str = "RUN-001") -> ArtifactManifest:
    """Build a schema-valid artifact manifest (no file access)."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=f"file:///raw/{artifact_id}.csv",
        sha256="a" * 64,
        size_bytes=1024,
        created_at="2026-01-01T00:00:00Z",
        run_id=run_id,
    )


def make_acceptance(
    acceptance_id: str, *, goal_id: str = "G-1"
) -> AcceptanceCriteria:
    """Build a schema-valid CONVERGENCE acceptance criteria record."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=goal_id,
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.CONVERGENCE,
        criteria=[{"metric": "scf_energy", "tolerance": 1e-6}],
    )


def make_assessment(
    iterations=(1.0, 0.9, 0.85, 0.8),
    tolerance: float = 0.1,
    *,
    window: int = 1,
    max_iterations: int | None = None,
) -> ConvergenceAssessment:
    """Build a deterministic CONVERGED assessment (drift 0.1 -> 0.05)."""
    return evaluate_convergence(
        iterations, tolerance, window=window, max_iterations=max_iterations
    )


def make_report(samples=(2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0)) -> SamplingUncertaintyReport:
    """Build a deterministic sampling report (mean 5, spread 7, n 8)."""
    return sampling_uncertainty(samples)


def build_result_workspace(tmp_path: Path) -> Path:
    """Initialize a project with the registered entities a result references.

    Registers, deterministically: the frozen PRIMARY protocol ``ANL-1``
    ``v1`` (DEV-M9-G01 registry), the raw artifact manifest ``ART-001``
    (the project ``manifests/`` artifact registry) and the CONVERGENCE
    acceptance criteria ``ACC-1`` (with the frozen tolerance).
    """
    root = init_project(tmp_path)
    register_analysis_record(root, make_protocol("ANL-1"))
    draft = read_analysis_protocol(root, "ANL-1")
    freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    ArtifactRegistry(root / ARTIFACTS_STATE_DIR).register(make_manifest("ART-001"))
    register_acceptance(root, make_acceptance("ACC-1"))
    return root


# ---------------------------------------------------------------------------
# AC-01: convergence failure representable without auto-changing parameters
# ---------------------------------------------------------------------------


def test_computational_ac01_not_converged_failure_representable():
    # The iteration series ends above the frozen tolerance without settling:
    # iteration non-convergence is a first-class typed result (AC-01).
    assessment = evaluate_convergence((1.0, 0.6, 0.35), 0.1)
    assert isinstance(assessment, ConvergenceAssessment)
    assert assessment.status is ConvergenceStatus.NOT_CONVERGED
    assert assessment.failure is True
    assert assessment.converged is False
    assert assessment.matched_rule_id == "R-CONV-N1"
    assert assessment.settling_drift == pytest.approx(0.25)


def test_computational_ac01_diverging_drift_failure_representable():
    # The drift is not settling: the last step exceeds every earlier drift
    # and the frozen tolerance (energy/force drift growing, AC-01).
    assessment = evaluate_convergence((1.0, 1.1, 1.35, 1.85), 0.1)
    assert assessment.status is ConvergenceStatus.DIVERGING
    assert assessment.failure is True
    assert assessment.matched_rule_id == "R-CONV-D1"
    assert assessment.final_drift == pytest.approx(0.5)
    assert assessment.state.prior_max_drift == pytest.approx(0.25)


def test_computational_ac01_converged_within_tolerance():
    # A series that settles within the frozen tolerance is CONVERGED --
    # the scientific convergence success state.
    assessment = evaluate_convergence((1.0, 0.9, 0.85), 0.1)
    assert assessment.status is ConvergenceStatus.CONVERGED
    assert assessment.failure is False
    assert assessment.converged is True
    assert assessment.matched_rule_id == "R-CONV-C1"


def test_computational_ac01_window_changes_verdict():
    # The trailing window is normative: a settled last step is not enough
    # when the window still contains a drift above the tolerance.
    series = (1.0, 0.05, 0.0)
    one_step = evaluate_convergence(series, 0.3, window=1)
    two_step = evaluate_convergence(series, 0.3, window=2)
    assert one_step.status is ConvergenceStatus.CONVERGED
    assert two_step.status is ConvergenceStatus.NOT_CONVERGED
    assert two_step.settling_drift == pytest.approx(0.95)


def test_computational_ac01_late_settling_is_not_diverging():
    # An early large drift with a late settling end is NOT_CONVERGED when
    # above tolerance -- never DIVERGING (the trend is downward, AC-01).
    assessment = evaluate_convergence((0.9, 0.4, 0.1), 0.05)
    assert assessment.status is ConvergenceStatus.NOT_CONVERGED
    assert assessment.matched_rule_id == "R-CONV-N1"
    assert assessment.state.prior_max_drift == pytest.approx(0.5)


def test_computational_ac01_observation_inputs_only_no_parameter_surface():
    # AC-01 by construction: the hook's inputs are observations and frozen
    # protocol values only -- there is no parameter object to mutate.
    params = inspect.signature(evaluate_convergence).parameters
    assert tuple(params) == ("iterations", "tolerance", "window", "max_iterations")
    for token in ("params", "parameters", "kwargs"):
        assert token not in params


def test_computational_ac01_no_auto_tuning_restart_api():
    # No public callable of the module can tune, adjust, restart or alter
    # a calculation: the hook reports, it never adjusts (AC-01).
    public_names = [
        name
        for name in dir(computational_module)
        if not name.startswith("_") and callable(getattr(computational_module, name))
    ]
    for name in public_names:
        lowered = name.lower()
        for prefix in ("tune", "adjust", "restart", "rerun", "resubmit", "set_", "alter"):
            assert not lowered.startswith(prefix), name
    source = inspect.getsource(computational_module)
    assert "atomic_write" not in source


def test_computational_ac01_assessment_is_frozen():
    # The assessment is a frozen dataclass: the validation result cannot be
    # mutated after construction (AC-01 -- the hook reports, never adjusts).
    assessment = make_assessment()
    assert is_dataclass(assessment)
    with pytest.raises(FrozenInstanceError):
        assessment.status = ConvergenceStatus.DIVERGING
    with pytest.raises(FrozenInstanceError):
        assessment.state.input.tolerance = 99.0


def test_computational_ac01_input_series_never_mutated():
    # The hook never mutates its inputs, and a mutable input list is copied
    # into the frozen assessment: mutating the caller's list afterwards
    # cannot change the result.
    series = [1.0, 0.5, 0.25]
    snapshot = list(series)
    assessment = evaluate_convergence(series, 0.1)
    assert series == snapshot
    series[2] = 99.0
    assert assessment.state.input.iterations == (1.0, 0.5, 0.25)
    assert assessment.status is ConvergenceStatus.NOT_CONVERGED


def test_computational_ac01_no_io_no_persistence_in_source():
    # Source inspection: the module performs no I/O, no writes, no network
    # and no persistence -- the hook reports in memory only (AC-01).
    source = inspect.getsource(computational_module)
    for forbidden in (
        "open(",
        "pathlib",
        "atomic_write",
        "write_text",
        "write_bytes",
        "os.",
        "sys.",
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "import json",
        "json.dumps",
    ):
        assert forbidden not in source, forbidden


def test_computational_ac01_typeerror_at_boundaries():
    # Wrong types are rejected with TypeError before any value validation.
    with pytest.raises(TypeError):
        evaluate_convergence("1.0, 0.5", 0.1)
    with pytest.raises(TypeError):
        evaluate_convergence((1.0, 0.5), "0.1")
    with pytest.raises(TypeError):
        evaluate_convergence((1.0, 0.5), 0.1, window=1.5)
    with pytest.raises(TypeError):
        evaluate_convergence((1.0, 0.5), 0.1, window=True)
    with pytest.raises(TypeError):
        evaluate_convergence((1.0, 0.5), 0.1, max_iterations="10")
    with pytest.raises(TypeError):
        evaluate_convergence((1.0, True), 0.1)


def test_computational_ac01_contradictory_inputs_rejected():
    # Degenerate and contradictory values are rejected with stable
    # InvalidConvergenceInputError messages (never silently accepted).
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0,), 0.1)  # single iteration: no drift
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 0.5), 0.0)  # zero tolerance
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 0.5), -0.1)  # negative tolerance
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 0.5), math.nan)
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 0.5), math.inf)
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 0.5), 0.1, window=0)
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 0.5), 0.1, max_iterations=0)
    with pytest.raises(InvalidConvergenceInputError):
        # A series longer than the declared budget is a contradiction.
        evaluate_convergence((1.0, 0.5, 0.25), 0.1, max_iterations=2)


def test_computational_ac01_stable_error_messages():
    # The same degenerate input always raises the same one-line message.
    with pytest.raises(InvalidConvergenceInputError) as first:
        evaluate_convergence((1.0, math.nan, 0.5), 0.1)
    with pytest.raises(InvalidConvergenceInputError) as second:
        evaluate_convergence((1.0, math.nan, 0.5), 0.1)
    assert str(first.value) == str(second.value)
    assert "iteration 1" in str(first.value)
    assert "finite number" in str(first.value)


def test_computational_ac01_budget_exhausted_reported():
    # The assessment reports whether the series ran the full declared
    # budget (informational -- the verdict stays purely on the series).
    full = evaluate_convergence((1.0, 0.5, 0.25), 0.1, max_iterations=3)
    partial = evaluate_convergence((1.0, 0.5, 0.25), 0.1, max_iterations=10)
    unset = evaluate_convergence((1.0, 0.5, 0.25), 0.1)
    assert full.budget_exhausted is True
    assert full.iterations_used == 3
    assert partial.budget_exhausted is False
    assert unset.budget_exhausted is False


# ---------------------------------------------------------------------------
# AC-02: Monte Carlo / sampling uncertainty hook
# ---------------------------------------------------------------------------


def test_computational_ac02_sampling_mean_se_spread_values():
    # The classic 8-sample series: mean 5.0, spread 7, and the standard
    # deviation/standard error match the stdlib statistics module exactly.
    samples = (2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0)
    report = make_report(samples)
    assert report.n == 8
    assert report.mean == 5.0 == fmean(samples)
    assert report.spread == 7.0
    assert report.minimum == 2.0
    assert report.maximum == 9.0
    assert report.standard_deviation == pytest.approx(stdev(samples))
    assert report.standard_error == pytest.approx(stdev(samples) / math.sqrt(8))


def test_computational_ac02_confidence_interval_math():
    # The normal-approximation interval: mean +/- z * se at the level,
    # z from the stdlib NormalDist critical value.
    report = make_report()
    z = NormalDist().inv_cdf((1.0 + DEFAULT_SAMPLING_CONFIDENCE_LEVEL) / 2.0)
    expected_lower = report.mean - z * report.standard_error
    expected_upper = report.mean + z * report.standard_error
    assert report.lower == pytest.approx(expected_lower)
    assert report.upper == pytest.approx(expected_upper)
    assert report.interval.confidence_level == 0.95
    assert report.lower < report.mean < report.upper


def test_computational_ac02_higher_confidence_wider_interval():
    narrow = sampling_uncertainty((2.0, 4.0, 5.0, 7.0), 0.90)
    wide = sampling_uncertainty((2.0, 4.0, 5.0, 7.0), 0.99)
    assert narrow.lower > wide.lower
    assert narrow.upper < wide.upper


def test_computational_ac02_determinism_repeated_calls():
    one = make_report()
    two = make_report()
    assert one == two
    assert one.interval == two.interval
    assert is_dataclass(one)


def test_computational_ac02_empty_samples_stable_error():
    with pytest.raises(InvalidSamplingInputError) as exc:
        sampling_uncertainty([])
    assert "at least two samples" in str(exc.value)
    assert "got 0" in str(exc.value)


def test_computational_ac02_single_sample_stable_error():
    with pytest.raises(InvalidSamplingInputError) as exc:
        sampling_uncertainty([3.0])
    assert "at least two samples" in str(exc.value)
    assert "got 1" in str(exc.value)


def test_computational_ac02_nan_sample_stable_error():
    with pytest.raises(InvalidSamplingInputError) as exc:
        sampling_uncertainty([1.0, math.nan, 3.0])
    assert "sample 1" in str(exc.value)
    assert "finite number" in str(exc.value)
    with pytest.raises(InvalidSamplingInputError) as again:
        sampling_uncertainty([1.0, math.nan, 3.0])
    assert str(again.value) == str(exc.value)


def test_computational_ac02_inf_sample_stable_error():
    with pytest.raises(InvalidSamplingInputError) as exc:
        sampling_uncertainty([1.0, 2.0, math.inf])
    assert "sample 2" in str(exc.value)
    assert "finite number" in str(exc.value)


def test_computational_ac02_non_numeric_samples_typeerror():
    with pytest.raises(TypeError):
        sampling_uncertainty([1.0, "two"])
    with pytest.raises(TypeError):
        sampling_uncertainty([1.0, True])
    with pytest.raises(TypeError):
        sampling_uncertainty("1.0, 2.0")
    with pytest.raises(TypeError):
        sampling_uncertainty([1.0, [2.0]])


def test_computational_ac02_confidence_level_boundary_rejected():
    for level in (0.0, 1.0, -0.1, 1.5, math.nan, math.inf):
        with pytest.raises(InvalidSamplingInputError):
            sampling_uncertainty([1.0, 2.0, 3.0], level)
    with pytest.raises(TypeError):
        sampling_uncertainty([1.0, 2.0, 3.0], "0.95")


def test_computational_ac02_zero_spread_point_interval():
    # A constant series has zero spread and a point interval (se = 0).
    report = sampling_uncertainty([2.5, 2.5, 2.5])
    assert report.spread == 0.0
    assert report.standard_error == 0.0
    assert report.lower == report.mean == report.upper == 2.5


def test_computational_ac02_samples_never_mutated():
    samples = [2.0, 4.0, 5.0, 7.0]
    snapshot = list(samples)
    report = sampling_uncertainty(samples)
    assert samples == snapshot
    samples[0] = -100.0
    assert report.mean == pytest.approx(4.5)
    assert report.interval.lower == pytest.approx(report.lower)


def test_computational_ac02_stdlib_only_no_numpy_scipy():
    # Source inspection: the sampling hook is stdlib-only (statistics;
    # no scipy, no numpy, no external statistics package).
    source = inspect.getsource(computational_module)
    for forbidden in (
        "import numpy",
        "numpy.",
        "import scipy",
        "scipy.",
        "import pandas",
        "pandas.",
    ):
        assert forbidden not in source, forbidden
    report = make_report()
    assert report.standard_deviation == pytest.approx(
        stdev((2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0))
    )


# ---------------------------------------------------------------------------
# AC-03: validation output feeds Supervisor acceptance
# ---------------------------------------------------------------------------


def test_computational_ac03_metrics_shape_fits_result_record():
    # convergence_metrics/sampling_metrics produce exactly the
    # ResultRecord.metrics shape (a list of {"metric", "value"} dicts).
    assessment = make_assessment()
    entries = convergence_metrics("scf_energy", assessment)
    assert entries == [
        {"metric": "scf_energy", "value": assessment.final_drift},
        {"metric": "scf_energy_settling_drift", "value": assessment.settling_drift},
        {"metric": "scf_energy_max_drift", "value": assessment.state.max_drift},
        {"metric": "scf_energy_iterations", "value": 4},
    ]
    report = make_report()
    sample_entries = sampling_metrics("mc_uptake", report)
    assert sample_entries == [
        {"metric": "mc_uptake", "value": report.mean},
        {"metric": "mc_uptake_standard_error", "value": report.standard_error},
        {"metric": "mc_uptake_spread", "value": report.spread},
    ]
    record = ResultRecord(
        result_id="RES-COMP-1",
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref="RUN-001",
        input_artifact_ids=["ART-001"],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        metrics=entries + sample_entries,
    )
    assert record.metrics[0] == {"metric": "scf_energy", "value": assessment.final_drift}


def test_computational_ac03_uncertainty_shape_fits_result_record():
    # sampling_uncertainty_payload produces exactly the
    # ResultRecord.uncertainty shape (a dict).
    report = make_report()
    payload = sampling_uncertainty_payload(report)
    assert set(payload) == {
        "method",
        "n",
        "mean",
        "standard_deviation",
        "standard_error",
        "minimum",
        "maximum",
        "spread",
        "confidence_level",
        "lower",
        "upper",
    }
    assert payload["method"] == "sampling"
    assert payload["n"] == 8
    assert payload["mean"] == report.mean
    assert payload["lower"] == report.lower
    assert payload["upper"] == report.upper
    record = ResultRecord(
        result_id="RES-COMP-2",
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref="RUN-001",
        input_artifact_ids=["ART-001"],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        uncertainty=payload,
    )
    assert record.uncertainty["method"] == "sampling"


def test_computational_ac03_qc_findings_shape_fits_result_record():
    # convergence_findings/sampling_findings produce exactly the
    # ResultRecord.qc_findings shape (a list of non-empty strings).
    assessment = make_assessment()
    findings = convergence_findings(assessment)
    assert isinstance(findings, list) and all(isinstance(f, str) for f in findings)
    assert "R-CONV-C1" in findings[0]
    failure = evaluate_convergence((1.0, 0.6, 0.35), 0.1)
    failure_findings = convergence_findings(failure)
    assert "R-CONV-N1" in failure_findings[0]
    report = make_report()
    sample_findings = sampling_findings("mc_uptake", report)
    assert "sampling of mc_uptake" in sample_findings[0]
    assert "8 samples" in sample_findings[0]
    record = ResultRecord(
        result_id="RES-COMP-3",
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref="RUN-001",
        input_artifact_ids=["ART-001"],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        qc_findings=findings + sample_findings,
    )
    assert record.qc_findings == findings + sample_findings


def test_computational_ac03_register_result_accepts_validation_output(tmp_path):
    # The full acceptance path: the validation output populates a
    # ResultRecord that registers through the real register_result
    # (exactly-once registry, reference resolution included).
    root = build_result_workspace(tmp_path)
    assessment = make_assessment()
    report = make_report()
    record = ResultRecord(
        result_id="RES-COMP-4",
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref="RUN-001",
        input_artifact_ids=["ART-001"],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        acceptance_ref="ACC-1",
        metrics=convergence_metrics("scf_energy", assessment)
        + sampling_metrics("mc_uptake", report),
        uncertainty=sampling_uncertainty_payload(report),
        qc_findings=convergence_findings(assessment)
        + sampling_findings("mc_uptake", report),
    )
    registered = register_result(root, record)
    stored = read_result(root, "RES-COMP-4")
    assert stored == registered
    assert stored.metrics[0] == {"metric": "scf_energy", "value": assessment.final_drift}
    assert stored.uncertainty["method"] == "sampling"
    assert stored.uncertainty["n"] == 8
    assert "R-CONV-C1" in stored.qc_findings[0]


def test_computational_ac03_validation_decision_mode_is_convergence():
    # The hook references the frozen DecisionMode.CONVERGENCE member
    # (core/models.py) as the mode its output feeds into.
    assert VALIDATION_DECISION_MODE is DecisionMode.CONVERGENCE
    assert VALIDATION_DECISION_MODE.value == "convergence"


def test_computational_ac03_validate_acceptance_mode_accepts_convergence():
    # A frozen CONVERGENCE acceptance record passes the mode check.
    acceptance = make_acceptance("ACC-1")
    assert validate_acceptance_mode(acceptance) is None


def test_computational_ac03_validate_acceptance_mode_rejects_other_modes():
    # An acceptance record under any other frozen decision mode cannot
    # consume computational validation output (AC-03).
    for mode in (
        DecisionMode.EQUIVALENCE,
        DecisionMode.BOUNDED_INTERVAL,
        DecisionMode.CATEGORICAL,
        DecisionMode.TREND,
        DecisionMode.STRUCTURAL_MATCH,
        DecisionMode.CUSTOM,
    ):
        acceptance = AcceptanceCriteria(
            acceptance_id="ACC-OTHER",
            goal_id="G-1",
            version="v1",
            frozen=True,
            decision_mode=mode,
            criteria=[{"metric": "uptake", "margin": 0.1}],
        )
        with pytest.raises(UnsupportedDecisionModeError) as exc:
            validate_acceptance_mode(acceptance)
        assert "CONVERGENCE" in str(exc.value)
        assert mode.value in str(exc.value)


def test_computational_ac03_validate_acceptance_mode_typeerror():
    with pytest.raises(TypeError):
        validate_acceptance_mode({"acceptance_id": "ACC-1"})


def test_computational_ac03_tolerance_read_from_acceptance_verbatim(tmp_path):
    # The frozen tolerance is read from the registered acceptance record
    # byte-for-byte -- never inferred from the run (07-... SS8 / AC-03).
    root = build_result_workspace(tmp_path)
    criterion = convergence_criterion_from_acceptance(read_acceptance(root, "ACC-1"))
    assert criterion == ConvergenceCriterion(tolerance=1e-6)
    assert criterion.tolerance == 1e-6


def test_computational_ac03_criterion_tolerance_reproduces_verdict():
    # The criterion extracted from an acceptance record reproduces the
    # classification of the observed series (same tolerance, same verdict).
    criterion = ConvergenceCriterion(tolerance=0.1)
    assessment = evaluate_convergence((1.0, 0.9, 0.85), criterion.tolerance)
    assert assessment.status is ConvergenceStatus.CONVERGED
    tight = ConvergenceCriterion(tolerance=0.01)
    assert (
        evaluate_convergence((1.0, 0.9, 0.85), tight.tolerance).status
        is ConvergenceStatus.NOT_CONVERGED
    )


def test_computational_ac03_no_tolerance_entry_rejected():
    acceptance = AcceptanceCriteria(
        acceptance_id="ACC-NO",
        goal_id="G-1",
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.CONVERGENCE,
        criteria=[{"metric": "batch_level_uptake", "margin": 0.1}],
    )
    with pytest.raises(ConvergenceCriterionError) as exc:
        convergence_criterion_from_acceptance(acceptance)
    assert "no convergence tolerance" in str(exc.value)
    assert "never inferred from the run" in str(exc.value)


def test_computational_ac03_ambiguous_tolerances_rejected():
    acceptance = AcceptanceCriteria(
        acceptance_id="ACC-AMB",
        goal_id="G-1",
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.CONVERGENCE,
        criteria=[
            {"metric": "scf_energy", "tolerance": 1e-6},
            {"metric": "force", "tolerance": 1e-4},
        ],
    )
    with pytest.raises(ConvergenceCriterionError) as exc:
        convergence_criterion_from_acceptance(acceptance)
    assert "ambiguous convergence tolerances" in str(exc.value)
    assert "exactly one tolerance" in str(exc.value)


def test_computational_ac03_non_numeric_or_non_positive_tolerance_rejected():
    for bad in ("tight", True, 0.0, -1e-6, math.nan, math.inf, None):
        acceptance = AcceptanceCriteria(
            acceptance_id="ACC-BAD",
            goal_id="G-1",
            version="v1",
            frozen=True,
            decision_mode=DecisionMode.CONVERGENCE,
            criteria=[{"metric": "scf_energy", "tolerance": bad}],
        )
        with pytest.raises(ConvergenceCriterionError):
            convergence_criterion_from_acceptance(acceptance)


def test_computational_ac03_never_decides_acceptance():
    # The module never decides acceptance: no PASS/FAIL/REPRODUCED member
    # in any vocabulary, no requirement state touched, and the finding
    # text stays scientific (no PASS/FAIL verdict words).
    for member in ConvergenceStatus:
        assert "PASS" not in member.value
        assert "FAIL" not in member.value
        assert member.value != "REPRODUCED"
    assert "PASS" not in computational_module.__all__
    assert "REPRODUCED" not in computational_module.__all__
    assert not hasattr(computational_module, "REPRODUCED")
    source = inspect.getsource(computational_module)
    assert "RequirementOutcome" not in source
    # The status vocabulary is exactly the scientific state vocabulary.
    assert [status.value for status in ConvergenceStatus] == [
        "CONVERGED",
        "NOT_CONVERGED",
        "DIVERGING",
    ]
    # The report hooks never emit PASS/FAIL verdict strings.
    finding = convergence_findings(evaluate_convergence((1.0, 0.6, 0.35), 0.1))[0]
    assert "PASS" not in finding and "FAIL" not in finding


# ---------------------------------------------------------------------------
# Paradigm and boundaries
# ---------------------------------------------------------------------------


def test_computational_boundary_errors_are_valueerror_subclasses():
    assert issubclass(ComputationalValidationError, ValueError)
    assert issubclass(InvalidConvergenceInputError, ComputationalValidationError)
    assert issubclass(InvalidSamplingInputError, ComputationalValidationError)
    assert issubclass(ConvergenceCriterionError, ComputationalValidationError)
    assert issubclass(UnsupportedDecisionModeError, ComputationalValidationError)
    # Stable one-line messages: the same degenerate input always raises
    # the same message text.
    with pytest.raises(InvalidSamplingInputError) as first:
        sampling_uncertainty([math.inf, 1.0])
    with pytest.raises(InvalidSamplingInputError) as second:
        sampling_uncertainty([math.inf, 1.0])
    assert str(first.value) == str(second.value)
    assert "finite number" in str(first.value)


def test_computational_boundary_degenerate_input_grid():
    # Every degenerate input is rejected with a stable error class --
    # nothing silently degrades a validation result.
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, math.inf), 0.1)
    with pytest.raises(InvalidConvergenceInputError):
        evaluate_convergence((1.0, 2.0, math.nan), 0.1)
    with pytest.raises(InvalidSamplingInputError):
        sampling_uncertainty([])
    with pytest.raises(InvalidSamplingInputError):
        sampling_uncertainty([1.0])
    with pytest.raises(InvalidSamplingInputError):
        sampling_uncertainty([1.0, 2.0, math.nan])
    with pytest.raises(InvalidSamplingInputError):
        sampling_uncertainty([1.0, 2.0, math.inf])
    with pytest.raises(InvalidSamplingInputError):
        sampling_uncertainty([1.0, 2.0], 1.0)
    with pytest.raises(InvalidSamplingInputError):
        sampling_uncertainty([1.0, 2.0], 0.0)


def test_computational_boundary_determinism_across_repeated_calls():
    # Pure functions: same inputs, same outputs, on every call.
    series = (1.0, 0.7, 0.45, 0.22)
    one = evaluate_convergence(series, 0.3, window=2, max_iterations=8)
    two = evaluate_convergence(series, 0.3, window=2, max_iterations=8)
    assert one == two
    assert one.state == two.state
    samples = (2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0)
    first = sampling_uncertainty_payload(sampling_uncertainty(samples))
    second = sampling_uncertainty_payload(sampling_uncertainty(samples))
    assert first == second


def test_computational_boundary_rule_table_first_match_wins_total_default():
    # Every evaluation records the outcome of every rule; the trailing
    # total default always matches so matched_rule_id is never None.
    cases = (
        ((1.0, 0.5, 0.25), 0.1),
        ((1.0, 1.2, 1.6), 0.1),
        ((1.0, 0.6, 0.35), 0.1),
    )
    for series, tolerance in cases:
        assessment = evaluate_convergence(series, tolerance)
        assert assessment.matched_rule_id is not None
        assert len(assessment.decisions) == len(CONVERGENCE_RULES)
        assert [d.rule_id for d in assessment.decisions] == [
            rule.rule_id for rule in CONVERGENCE_RULES
        ]
        assert sum(1 for d in assessment.decisions if d.matched) >= 1
        matched = next(
            d for d in assessment.decisions if d.rule_id == assessment.matched_rule_id
        )
        assert matched.matched
        assert matched.status is assessment.status
        # First-match-wins: the matched rule is the first matching one.
        first_matched = next(d for d in assessment.decisions if d.matched)
        assert first_matched.rule_id == assessment.matched_rule_id


def test_computational_boundary_module_all_exports_resolve():
    for name in computational_module.__all__:
        assert hasattr(computational_module, name), name
    assert len(computational_module.__all__) == len(set(computational_module.__all__))


def test_computational_boundary_module_is_pure_no_io_no_randomness():
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
    ):
        assert forbidden not in source, forbidden
    # Deterministic end to end.
    assessment = evaluate_convergence((1.0, 0.5, 0.25), 0.1)
    assert convergence_findings(assessment) == convergence_findings(assessment)


def test_computational_boundary_frozen_records_reject_mutation():
    records = (
        make_assessment(),
        make_report(),
        ConvergenceInput(iterations=(1.0, 0.5), tolerance=0.1),
        ConvergenceState(
            input=ConvergenceInput(iterations=(1.0, 0.5), tolerance=0.1),
            final_drift=0.5,
            settling_drift=0.5,
            max_drift=0.5,
            prior_max_drift=None,
        ),
        SamplingInterval(lower=0.0, upper=1.0, confidence_level=0.95),
        ConvergenceCriterion(tolerance=0.1),
        ConvergenceRuleDecision(
            rule_id="R-CONV-C1",
            description="default",
            status=ConvergenceStatus.CONVERGED,
            matched=True,
        ),
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)


def test_computational_boundary_ruleset_version_recorded():
    assessment = make_assessment()
    assert assessment.ruleset_version == CONVERGENCE_RULESET_VERSION == "1.0"


def test_computational_boundary_findings_stable_one_line_strings():
    # Findings are single stable lines: the same assessment always yields
    # the same finding text, with no embedded newlines.
    for assessment in (
        evaluate_convergence((1.0, 0.9, 0.85), 0.1),
        evaluate_convergence((1.0, 0.6, 0.35), 0.1),
        evaluate_convergence((1.0, 1.2, 1.6), 0.1),
    ):
        findings = convergence_findings(assessment)
        assert len(findings) == 1
        assert "\n" not in findings[0]
        assert convergence_findings(assessment) == findings
    report = make_report()
    findings = sampling_findings("mc_uptake", report)
    assert len(findings) == 1
    assert "\n" not in findings[0]
    assert sampling_findings("mc_uptake", report) == findings


def test_computational_boundary_metric_name_validation():
    assessment = make_assessment()
    report = make_report()
    with pytest.raises(TypeError):
        convergence_metrics(42, assessment)
    with pytest.raises(InvalidConvergenceInputError):
        convergence_metrics("  ", assessment)
    with pytest.raises(TypeError):
        convergence_metrics("x", None)
    with pytest.raises(TypeError):
        sampling_metrics(None, report)
    with pytest.raises(InvalidSamplingInputError):
        sampling_metrics("  ", report)
    with pytest.raises(TypeError):
        sampling_metrics("x", None)
    with pytest.raises(TypeError):
        sampling_findings("x", "report")
    with pytest.raises(TypeError):
        sampling_uncertainty_payload("report")
    with pytest.raises(TypeError):
        convergence_findings("assessment")


def test_computational_boundary_assessment_integrity_checks():
    # Direct construction with inconsistent data is rejected (the status
    # must match the matched decision; the matched id must be recorded).
    state = ConvergenceState(
        input=ConvergenceInput(iterations=(1.0, 0.5), tolerance=0.1),
        final_drift=0.5,
        settling_drift=0.5,
        max_drift=0.5,
        prior_max_drift=None,
    )
    decisions = (
        ConvergenceRuleDecision(
            rule_id="R-CONV-C1",
            description="default",
            status=ConvergenceStatus.CONVERGED,
            matched=True,
        ),
    )
    with pytest.raises(InvalidConvergenceInputError):
        ConvergenceAssessment(
            ruleset_version="1.0",
            state=state,
            status=ConvergenceStatus.NOT_CONVERGED,
            decisions=decisions,
            matched_rule_id="R-CONV-C1",
        )
    with pytest.raises(InvalidConvergenceInputError):
        ConvergenceAssessment(
            ruleset_version="1.0",
            state=state,
            status=ConvergenceStatus.CONVERGED,
            decisions=decisions,
            matched_rule_id="R-NOPE",
        )


def test_computational_boundary_sampling_interval_validation():
    with pytest.raises(InvalidSamplingInputError):
        SamplingInterval(lower=2.0, upper=1.0, confidence_level=0.95)
    with pytest.raises(InvalidSamplingInputError):
        SamplingInterval(lower=0.0, upper=1.0, confidence_level=1.5)
    with pytest.raises(TypeError):
        SamplingInterval(lower="0", upper=1.0, confidence_level=0.95)


def test_computational_boundary_int_and_float_tolerances_equal():
    # Integer numeric inputs are accepted and normalized to floats.
    integer = evaluate_convergence((1, 0, 1), 1)
    floats = evaluate_convergence((1.0, 0.0, 1.0), 1.0)
    assert integer == floats
    assert integer.state.input.tolerance == 1.0
