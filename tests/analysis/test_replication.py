"""Replicate sufficiency evaluation and additional-run decision hook (DEV-M9-G04).

Every test name contains "replic" so ``python -m pytest -q
tests/analysis -k replic`` selects the whole suite and nothing else (no
other test in ``tests/analysis`` matches the keyword). The
``ac01``/``ac02``/``ac03`` sections map one-to-one to the acceptance
criteria of DEV-M9-G04:

* ``ac01`` -- independent vs technical/instrument replicates are
  distinguished: the replicate grouping is taken as input (never
  guessed from the values), only the independent values carry
  statistical weight, and a technical-only count never satisfies the
  independent-n floor;
* ``ac02`` -- the default ``n >= 3`` floor is enforceable for
  experimental Goals: ``DEFAULT_MIN_INDEPENDENT`` = 3 by default, an
  override is read verbatim from the frozen acceptance criteria, and
  the floor can never be weakened below 1 (non-numeric, non-positive
  and ambiguous overrides are rejected with stable messages);
* ``ac03`` -- insufficient precision yields the additional-run request
  shape (``requested_additional_runs``) on the frozen assessment --
  never a forced PASS/FAIL: the status vocabulary has no
  PASS/FAIL/REPRODUCED member and does not collide with the frozen
  outcome vocabulary strings, no Requirement state is read or written,
  the precision threshold is read from the frozen acceptance verbatim,
  and the reporting hooks populate the exact shapes the DEV-M9-G02
  acceptance path consumes (``ResultRecord.metrics`` / ``uncertainty``
  / ``qc_findings``, proven through the real ``register_result``).

The deterministic path mirrors ``protocol_helpers``: every fixture uses
fixed identities/timestamps, so all records are deterministic.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError, is_dataclass, replace
from pathlib import Path
from statistics import fmean, stdev

import pytest
from protocol_helpers import FROZEN_AT, init_project, make_protocol

import scientific_reproduction.analysis.replication as replication_module
from scientific_reproduction.analysis.protocols import (
    freeze_primary_protocol,
    register_analysis_record,
)
from scientific_reproduction.analysis.replication import (
    DEFAULT_MIN_INDEPENDENT,
    DEFAULT_REPLICATION_CONFIDENCE_LEVEL,
    REPLICATION_DECISION_RULES,
    REPLICATION_RULESET_VERSION,
    SUPPORTED_REPLICATION_DECISION_MODES,
    InvalidReplicateInputError,
    ReplicateAnalysisError,
    ReplicateCriterion,
    ReplicateCriterionError,
    ReplicateDecisionInput,
    ReplicateRuleDecision,
    ReplicateState,
    ReplicateStatus,
    ReplicateSufficiencyAssessment,
    UnsupportedDecisionModeError,
    evaluate_replicate_sufficiency,
    replicate_criterion_from_acceptance,
    sufficiency_findings,
    sufficiency_metrics,
    sufficiency_uncertainty_payload,
    validate_replication_mode,
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
    RequirementOutcome,
)
from scientific_reproduction.planning.plan import (
    read_acceptance,
    read_analysis_protocol,
    register_acceptance,
)

#: The standard-normal critical value of the 0.95 two-sided level --
#: pinned so the interval math is asserted against a fixed value.
Z_095 = 1.959963984540054

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
    acceptance_id: str,
    *,
    goal_id: str = "G-1",
    decision_mode: DecisionMode = DecisionMode.EQUIVALENCE,
    min_independent: int | None = None,
    precision: float = 0.1,
) -> AcceptanceCriteria:
    """Build a schema-valid frozen EQUIVALENCE acceptance record."""
    entry: dict[str, object] = {"metric": "batch_level_uptake"}
    if min_independent is not None:
        entry["min_independent"] = min_independent
    entry["precision"] = precision
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=goal_id,
        version="v1",
        frozen=True,
        decision_mode=decision_mode,
        criteria=[entry],
    )


def make_sufficient(
    independent=(10.0, 10.2, 10.1),
    technical=(),
    *,
    min_independent: int = DEFAULT_MIN_INDEPENDENT,
    precision_threshold: float = 0.1,
    confidence_level: float = DEFAULT_REPLICATION_CONFIDENCE_LEVEL,
) -> ReplicateSufficiencyAssessment:
    """Build a deterministic SUFFICIENT assessment (mean 10.1, n 3)."""
    return evaluate_replicate_sufficiency(
        independent,
        technical,
        min_independent=min_independent,
        precision_threshold=precision_threshold,
        confidence_level=confidence_level,
    )


def make_wide_assessment(
    independent=(10.0, 15.0, 5.0),
    *,
    min_independent: int = DEFAULT_MIN_INDEPENDENT,
    precision_threshold: float = 0.1,
) -> ReplicateSufficiencyAssessment:
    """Build a deterministic INSUFFICIENT assessment (mean 10, sd 5)."""
    return evaluate_replicate_sufficiency(
        independent,
        min_independent=min_independent,
        precision_threshold=precision_threshold,
    )


def build_result_workspace(tmp_path: Path) -> Path:
    """Initialize a project with the registered entities a result references.

    Registers, deterministically: the frozen PRIMARY protocol ``ANL-1``
    ``v1`` (DEV-M9-G01 registry), the raw artifact manifest ``ART-001``
    (the project ``manifests/`` artifact registry) and the EQUIVALENCE
    acceptance criteria ``ACC-1`` (with the frozen replication criterion).
    """
    root = init_project(tmp_path)
    register_analysis_record(root, make_protocol("ANL-1"))
    draft = read_analysis_protocol(root, "ANL-1")
    freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    ArtifactRegistry(root / ARTIFACTS_STATE_DIR).register(make_manifest("ART-001"))
    register_acceptance(root, make_acceptance("ACC-1"))
    return root


# ---------------------------------------------------------------------------
# AC-01: independent vs technical/instrument replicates distinguished
# ---------------------------------------------------------------------------


def test_replic_ac01_technical_repeats_never_satisfy_independent_floor():
    # A group of 2 independent replicates plus 6 technical/instrument
    # repeats stays below the default floor of 3: the technical "n" never
    # counts toward the independent-n floor (07-... SS2, ADR 10).
    assessment = make_sufficient(independent=(10.0, 10.1), technical=(10.0, 9.95, 10.05, 9.9, 10.1, 10.0))
    assert assessment.independent_n == 2
    assert assessment.technical_n == 6
    assert assessment.status is ReplicateStatus.INDETERMINATE
    assert assessment.requested_additional_runs == 1
    # The technical repeats never entered the precision statistics: with
    # the same independent values alone the assessment is identical.
    without_technical = make_sufficient(independent=(10.0, 10.1))
    assert assessment.status == without_technical.status
    assert assessment.requested_additional_runs == without_technical.requested_additional_runs


def test_replic_ac01_technical_only_evidence_rejected():
    # A technical-only "n" is not independent evidence at all: an empty
    # independent group is rejected with a stable message instead of
    # treating the technical repeats as replicates.
    with pytest.raises(InvalidReplicateInputError) as exc:
        evaluate_replicate_sufficiency(
            [], technical=(10.0, 10.05, 9.95), precision_threshold=0.1
        )
    assert "at least one independent replicate" in str(exc.value)
    assert "technical" in str(exc.value)


def test_replic_ac01_technical_values_never_enter_statistics():
    # The mean, standard deviation and precision measures are computed
    # from the independent values alone: wildly spread technical repeats
    # change none of the state measures (the technical group is recorded
    # and reported only, AC-01).
    independent = (10.0, 10.2, 10.1)
    without = make_sufficient(independent=independent)
    with_technical = make_sufficient(
        independent=independent, technical=(1000.0, -1000.0, 0.5, 1e6)
    )
    assert with_technical.technical_n == 4
    assert with_technical.state.mean == without.state.mean
    assert with_technical.state.standard_error == without.state.standard_error
    assert with_technical.relative_half_width == without.relative_half_width
    assert with_technical.status is ReplicateStatus.SUFFICIENT
    assert with_technical.requested_additional_runs == 0


def test_replic_ac01_grouping_is_input_not_guessed_from_values():
    # No replicate type is ever inferred from the values: the same values
    # produce different decisions depending on the grouping the caller
    # supplies -- the grouping is an input (AC-01), and the evaluator has
    # no API shape that would let values auto-classify.
    values = (10.0, 10.2, 10.1)
    as_independent = evaluate_replicate_sufficiency(values, precision_threshold=0.1)
    as_technical = evaluate_replicate_sufficiency(
        (10.0, 10.1), technical=values, precision_threshold=0.1
    )
    assert as_independent.status is ReplicateStatus.SUFFICIENT
    assert as_technical.status is ReplicateStatus.INDETERMINATE
    parameters = set(
        inspect.signature(evaluate_replicate_sufficiency).parameters
    )
    assert "independent" in parameters and "technical" in parameters


def test_replic_ac01_independent_values_carry_the_statistical_weight():
    # The precision measures are exactly the z-based statistics of the
    # independent values: mean, sample standard deviation, standard
    # error and relative half-width (Z_095 pinned).
    independent = (10.0, 10.2, 10.1)
    assessment = make_sufficient(independent=independent)
    expected_mean = fmean(independent)
    expected_sd = stdev(independent)
    expected_se = expected_sd / math.sqrt(3)
    expected_hw = Z_095 * expected_se
    assert assessment.state.mean == pytest.approx(expected_mean)
    assert assessment.state.standard_deviation == pytest.approx(expected_sd)
    assert assessment.state.standard_error == pytest.approx(expected_se)
    assert assessment.state.half_width == pytest.approx(expected_hw)
    assert assessment.relative_half_width == pytest.approx(expected_hw / expected_mean)


def test_replic_ac01_input_series_never_mutated():
    # The hook never mutates its inputs, and mutable input lists are
    # copied into the frozen assessment: mutating the caller's lists
    # afterwards cannot change the result.
    independent = [10.0, 10.2, 10.1]
    technical = [9.9, 10.0]
    snapshot = (list(independent), list(technical))
    assessment = make_sufficient(independent=independent, technical=technical)
    assert (independent, technical) == snapshot
    independent[2] = 99.0
    technical[0] = -99.0
    assert assessment.state.mean == pytest.approx(10.1)
    assert assessment.input.technical == (9.9, 10.0)
    assert assessment.status is ReplicateStatus.SUFFICIENT


# ---------------------------------------------------------------------------
# AC-02: default n>=3 floor enforceable for experimental Goals
# ---------------------------------------------------------------------------


def test_replic_ac02_default_floor_is_three():
    # The documented default floor of experimental Goals is 3
    # (07-... SS2 "Default floor: n >= 3"; ADR 9).
    assert DEFAULT_MIN_INDEPENDENT == 3
    assert make_sufficient(independent=(10.0, 10.2, 10.1)).independent_n == 3


def test_replic_ac02_default_floor_enforced_without_override():
    # Two tight independent replicates are not sufficient under the
    # default floor -- regardless of how precise they look: the floor is
    # a gate, so the assessment is INDETERMINATE with a request for the
    # missing run (AC-02).
    assessment = make_sufficient(independent=(10.0, 10.1))
    assert assessment.status is ReplicateStatus.INDETERMINATE
    assert assessment.matched_rule_id == "R-REP-U1"
    assert assessment.requested_additional_runs == 1
    assert assessment.input.min_independent == 3


def test_replic_ac02_floor_read_from_frozen_acceptance_verbatim(tmp_path):
    # The floor override is an input from the frozen acceptance criteria
    # (a numeric 'min_independent' entry), read verbatim like the
    # DEV-M9-G03 margin; a record without an override yields the default.
    root = init_project(tmp_path)
    register_acceptance(root, make_acceptance("ACC-REP-1", min_independent=4))
    acceptance = read_acceptance(root, "ACC-REP-1")
    assert acceptance.frozen is True
    criterion = replicate_criterion_from_acceptance(acceptance)
    assert criterion.min_independent == 4
    assert criterion.min_independent == acceptance.criteria[0]["min_independent"]
    defaulted = replicate_criterion_from_acceptance(make_acceptance("ACC-REP-2"))
    assert defaulted.min_independent == DEFAULT_MIN_INDEPENDENT == 3


def test_replic_ac02_floor_override_honored_by_evaluator():
    # A frozen floor of 2 is honored: two independent replicates then
    # satisfy the floor and the precision criterion decides.
    overridden = make_sufficient(
        independent=(10.0, 10.1), min_independent=2, precision_threshold=0.1
    )
    assert overridden.status is ReplicateStatus.SUFFICIENT
    assert overridden.requested_additional_runs == 0
    defaulted = make_sufficient(independent=(10.0, 10.1))
    assert defaulted.status is ReplicateStatus.INDETERMINATE


def test_replic_ac02_floor_never_weakened_below_one():
    # An override below 1 (zero, negative) is rejected with a stable
    # message -- the floor cannot be silently weakened below 1 -- both in
    # the acceptance reader and at the evaluator boundary.
    for bad in (0, -1, -5):
        acceptance = replace(
            make_acceptance("ACC-REP-1"),
            criteria=[{"metric": "uptake", "min_independent": bad, "precision": 0.1}],
        )
        with pytest.raises(ReplicateCriterionError) as exc:
            replicate_criterion_from_acceptance(acceptance)
        assert "must not be weakened below 1" in str(exc.value)
        with pytest.raises(InvalidReplicateInputError):
            make_sufficient(min_independent=bad)


def test_replic_ac02_non_numeric_floor_override_rejected():
    # Non-numeric, non-integer and boolean overrides are rejected with
    # stable one-line messages (AC-02).
    for bad in ("three", 2.5, True, math.nan, None):
        acceptance = replace(
            make_acceptance("ACC-REP-1"),
            criteria=[{"metric": "uptake", "min_independent": bad, "precision": 0.1}],
        )
        with pytest.raises(ReplicateCriterionError) as exc:
            replicate_criterion_from_acceptance(acceptance)
        assert "positive integer" in str(exc.value)


def test_replic_ac02_ambiguous_floor_overrides_rejected():
    # Several differing floor overrides are ambiguous and rejected with a
    # stable message (AC-02).
    acceptance = replace(
        make_acceptance("ACC-REP-1"),
        criteria=[
            {"metric": "uptake", "min_independent": 3, "precision": 0.1},
            {"metric": "strain", "min_independent": 5},
        ],
    )
    with pytest.raises(ReplicateCriterionError) as exc:
        replicate_criterion_from_acceptance(acceptance)
    assert "ambiguous min_independent" in str(exc.value)
    assert "exactly one floor" in str(exc.value)


def test_replic_ac02_below_floor_indeterminate_with_floor_request():
    # Below the floor the sufficiency determination is INDETERMINATE and
    # the request is the exact number of independent runs needed to reach
    # the floor (never a forced PASS/FAIL).
    one = make_sufficient(independent=(10.0,))
    assert one.status is ReplicateStatus.INDETERMINATE
    assert one.requested_additional_runs == 2
    two = make_sufficient(independent=(10.0, 10.1))
    assert two.requested_additional_runs == 1


def test_replic_ac02_single_replicate_indeterminate_even_with_floor_one():
    # A floor override of 1 still needs two independent replicates for a
    # statistical precision determination (a sample standard deviation):
    # a single replicate is INDETERMINATE and requests one more run --
    # the floor is never silently weakened into meaninglessness.
    assessment = make_sufficient(independent=(10.0,), min_independent=1)
    assert assessment.status is ReplicateStatus.INDETERMINATE
    assert assessment.requested_additional_runs == 1
    assert assessment.relative_half_width is None


# ---------------------------------------------------------------------------
# AC-03: insufficient precision yields an additional-run request, never PASS/FAIL
# ---------------------------------------------------------------------------


def test_replic_ac03_precision_shortfall_requests_additional_runs():
    # The precision criterion is not met: the z-based relative half-width
    # exceeds the frozen threshold, the status is INSUFFICIENT and the
    # assessment carries an explicit additional-run request (AC-03).
    assessment = make_wide_assessment()
    assert assessment.status is ReplicateStatus.INSUFFICIENT
    assert assessment.matched_rule_id == "R-REP-I1"
    assert assessment.relative_half_width > 0.1
    assert assessment.requested_additional_runs >= 1


def test_replic_ac03_precision_shortfall_never_pass_fail():
    # The request never carries a PASS/FAIL verdict: the status
    # vocabulary is SUFFICIENT/INSUFFICIENT/INDETERMINATE and the
    # findings text names the evidence state, not a verdict.
    assessment = make_wide_assessment()
    for member in ReplicateStatus:
        assert "PASS" not in member.value
        assert "FAIL" not in member.value
    finding = sufficiency_findings(assessment)[0]
    assert "PASS" not in finding and "FAIL" not in finding
    assert "additional independent run" in finding


def test_replic_ac03_request_scales_with_precision_shortfall():
    # The request grows deterministically with the shortfall: a wider
    # spread at the same n and threshold requests at least as many
    # additional independent runs (h scales with 1/sqrt(n), AC-03).
    tighter = make_wide_assessment(independent=(10.0, 12.0, 8.0))
    wider = make_wide_assessment(independent=(10.0, 15.0, 5.0))
    assert tighter.status is ReplicateStatus.INSUFFICIENT
    assert wider.relative_half_width > tighter.relative_half_width
    assert wider.requested_additional_runs > tighter.requested_additional_runs
    # Deterministic formula pinned: ceil(n * (h / threshold)**2) - n.
    h = wider.relative_half_width
    expected = max(
        math.ceil(3 * (h / 0.1) ** 2) - 3, 1
    )
    assert wider.requested_additional_runs == expected


def test_replic_ac03_precision_met_yields_sufficient_without_request():
    # The precision criterion met at/above the floor yields SUFFICIENT
    # with no additional runs requested (AC-03).
    assessment = make_sufficient()
    assert assessment.status is ReplicateStatus.SUFFICIENT
    assert assessment.requested_additional_runs == 0
    assert assessment.sufficient is True


def test_replic_ac03_vocabulary_has_no_pass_fail_or_reproduced_member():
    # The status vocabulary contains no PASS/FAIL/REPRODUCED member and
    # no RequirementOutcome string value (AC-03).
    for member in ReplicateStatus:
        assert "PASS" not in member.value
        assert "FAIL" not in member.value
        assert member.value != "REPRODUCED"
    assert "PASS" not in replication_module.__all__
    assert "FAIL" not in replication_module.__all__
    assert "REPRODUCED" not in replication_module.__all__
    assert not hasattr(replication_module, "REPRODUCED")


def test_replic_ac03_vocabulary_does_not_collide_with_outcome_strings():
    # The frozen outcome vocabulary owns REPRODUCED/NOT_REPRODUCED/
    # INCONCLUSIVE (core/models.py RequirementOutcome): the status
    # vocabulary deliberately avoids every one of those strings, with
    # INDETERMINATE as the inconclusive-analog member (AC-03).
    outcome_values = {member.value for member in RequirementOutcome}
    assert "INCONCLUSIVE" in outcome_values
    status_values = [member.value for member in ReplicateStatus]
    assert status_values == ["SUFFICIENT", "INSUFFICIENT", "INDETERMINATE"]
    for value in status_values:
        assert value not in outcome_values


def test_replic_ac03_no_requirement_state_read_or_written():
    # The module never reads or writes requirement state: no import path
    # into the outcome/requirement layer exists in its source, and the
    # outcome enum is not even an attribute of the module -- nothing here
    # can read or write a Requirement (AC-03: results never close
    # outcomes by themselves).
    source = inspect.getsource(replication_module)
    for forbidden in ("core.rules", "planning", "import outcome", "from outcome"):
        assert forbidden not in source, forbidden
    assert not hasattr(replication_module, "RequirementOutcome")
    assert not hasattr(replication_module, "Requirement")


def test_replic_ac03_precision_read_from_acceptance_verbatim(tmp_path):
    # The precision threshold is an input from the frozen acceptance
    # criteria (a numeric 'precision' entry), read verbatim -- honored
    # even when it differs from any result-derived spread.
    root = init_project(tmp_path)
    register_acceptance(root, make_acceptance("ACC-REP-1", precision=0.6))
    acceptance = read_acceptance(root, "ACC-REP-1")
    criterion = replicate_criterion_from_acceptance(acceptance)
    assert criterion.precision_threshold == 0.6
    assert criterion.precision_threshold == acceptance.criteria[0]["precision"]
    # The wide group (relative half-width ~0.57) is SUFFICIENT under the
    # frozen threshold 0.6 -- the frozen criterion is honored, not the
    # observed spread (under a 0.1 threshold the same group is
    # INSUFFICIENT with a request).
    assessment = make_wide_assessment(precision_threshold=criterion.precision_threshold)
    assert assessment.status is ReplicateStatus.SUFFICIENT
    assert assessment.requested_additional_runs == 0
    strict = make_wide_assessment(precision_threshold=0.1)
    assert strict.status is ReplicateStatus.INSUFFICIENT


def test_replic_ac03_missing_precision_criterion_rejected():
    # A record without a numeric 'precision' entry cannot provide the
    # precision criterion and is rejected with a stable message -- the
    # threshold is never inferred from the result (AC-03).
    acceptance = AcceptanceCriteria(
        acceptance_id="ACC-REP-1",
        goal_id="G-1",
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "batch_level_uptake", "min_independent": 3}],
    )
    with pytest.raises(ReplicateCriterionError) as exc:
        replicate_criterion_from_acceptance(acceptance)
    assert "no precision criterion" in str(exc.value)
    assert "never inferred from the result" in str(exc.value)


def test_replic_ac03_non_positive_or_non_numeric_precision_rejected():
    for bad in (0.0, -0.1, math.nan, math.inf, "10%", True, None):
        acceptance = replace(
            make_acceptance("ACC-REP-1"),
            criteria=[{"metric": "uptake", "precision": bad}],
        )
        with pytest.raises(ReplicateCriterionError) as exc:
            replicate_criterion_from_acceptance(acceptance)
        assert "finite positive number" in str(exc.value)


def test_replic_ac03_ambiguous_precision_rejected():
    # Several differing precision thresholds are ambiguous and rejected.
    acceptance = replace(
        make_acceptance("ACC-REP-1"),
        criteria=[
            {"metric": "uptake", "precision": 0.1},
            {"metric": "strain", "precision": 0.5},
        ],
    )
    with pytest.raises(ReplicateCriterionError) as exc:
        replicate_criterion_from_acceptance(acceptance)
    assert "ambiguous precision" in str(exc.value)
    assert "exactly one precision threshold" in str(exc.value)


def test_replic_ac03_metrics_shape_fits_result_record():
    assessment = make_sufficient()
    metrics = sufficiency_metrics("uptake", assessment)
    for entry in metrics:
        assert set(entry) == {"metric", "value"}
    names = [entry["metric"] for entry in metrics]
    assert names[0] == "uptake"
    assert "uptake_independent_n" in names
    assert "uptake_technical_n" in names
    assert "uptake_requested_additional_runs" in names
    assert "uptake_relative_half_width" in names
    by_name = {entry["metric"]: entry["value"] for entry in metrics}
    assert by_name["uptake"] == pytest.approx(10.1)
    assert by_name["uptake_independent_n"] == 3
    assert by_name["uptake_technical_n"] == 0
    assert by_name["uptake_requested_additional_runs"] == 0


def test_replic_ac03_uncertainty_shape_fits_result_record():
    assessment = make_sufficient()
    payload = sufficiency_uncertainty_payload(assessment)
    assert payload["method"] == "confidence_interval"
    assert payload["n"] == 3
    assert payload["mean"] == pytest.approx(10.1)
    assert payload["confidence_level"] == 0.95
    expected_hw = Z_095 * assessment.state.standard_error
    assert payload["lower"] == pytest.approx(10.1 - expected_hw)
    assert payload["upper"] == pytest.approx(10.1 + expected_hw)


def test_replic_ac03_qc_findings_shape_fits_result_record():
    # Findings are stable one-line scientific strings naming the decided
    # evidence state, the frozen criterion and the request (AC-03).
    for assessment in (
        make_sufficient(),
        make_wide_assessment(),
        make_sufficient(independent=(10.0, 10.1)),
    ):
        findings = sufficiency_findings(assessment)
        assert len(findings) == 1
        assert isinstance(findings[0], str)
        assert "\n" not in findings[0]
        assert sufficiency_findings(assessment) == findings


def test_replic_ac03_register_result_accepts_sufficiency_output(tmp_path):
    # The full acceptance path: the sufficiency output populates a
    # ResultRecord that registers through the real register_result
    # (exactly-once registry, reference resolution included) and reads
    # back byte-identical (AC-03 -- Supervisor-facing analysis result).
    root = build_result_workspace(tmp_path)
    assessment = make_wide_assessment()
    record = ResultRecord(
        result_id="RES-REP-1",
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref="RUN-001",
        input_artifact_ids=["ART-001"],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        acceptance_ref="ACC-1",
        metrics=sufficiency_metrics("uptake", assessment),
        uncertainty=sufficiency_uncertainty_payload(assessment),
        qc_findings=sufficiency_findings(assessment),
    )
    registered = register_result(root, record)
    stored = read_result(root, "RES-REP-1")
    assert stored == registered
    assert stored.metrics[0] == {"metric": "uptake", "value": assessment.state.mean}
    assert stored.uncertainty["method"] == "confidence_interval"
    assert stored.uncertainty["n"] == 3
    assert stored.uncertainty["lower"] == pytest.approx(assessment.mean_interval[0])
    assert "additional independent run" in stored.qc_findings[0]


def test_replic_ac03_validate_mode_accepts_experimental_modes():
    # The hook consumes the frozen DecisionMode vocabulary: the
    # quantitative experimental modes pass the mode check.
    assert SUPPORTED_REPLICATION_DECISION_MODES == (
        DecisionMode.EQUIVALENCE,
        DecisionMode.BOUNDED_INTERVAL,
    )
    for mode in SUPPORTED_REPLICATION_DECISION_MODES:
        acceptance = make_acceptance("ACC-REP-1", decision_mode=mode)
        assert validate_replication_mode(acceptance) is None


def test_replic_ac03_validate_mode_rejects_other_modes():
    # Every other frozen mode is rejected with a stable error -- the
    # frozen vocabulary has no REPLICATION member and the hook never
    # invents one (AC-03).
    for mode in (
        DecisionMode.CATEGORICAL,
        DecisionMode.TREND,
        DecisionMode.STRUCTURAL_MATCH,
        DecisionMode.CONVERGENCE,
        DecisionMode.CUSTOM,
    ):
        acceptance = make_acceptance("ACC-REP-OTHER", decision_mode=mode)
        with pytest.raises(UnsupportedDecisionModeError) as exc:
            validate_replication_mode(acceptance)
        assert "no REPLICATION mode" in str(exc.value)
        assert mode.value in str(exc.value)
    with pytest.raises(TypeError):
        validate_replication_mode({"decision_mode": "equivalence"})


# ---------------------------------------------------------------------------
# Paradigm boundaries
# ---------------------------------------------------------------------------


def test_replic_paradigm_rule_table_first_match_wins_total_default():
    # Every evaluation records the outcome of every rule; the trailing
    # total default always matches so matched_rule_id is never None and
    # the first matching rule decides (first match wins).
    cases = (
        make_sufficient(),
        make_wide_assessment(),
        make_sufficient(independent=(10.0,)),
        make_sufficient(independent=(10.0, 10.1), min_independent=2),
    )
    for assessment in cases:
        assert assessment.matched_rule_id is not None
        assert len(assessment.decisions) == len(REPLICATION_DECISION_RULES)
        assert [d.rule_id for d in assessment.decisions] == [
            rule.rule_id for rule in REPLICATION_DECISION_RULES
        ]
        assert sum(1 for d in assessment.decisions if d.matched) >= 1
        matched = next(
            d for d in assessment.decisions if d.rule_id == assessment.matched_rule_id
        )
        assert matched.matched
        assert matched.status is assessment.status
        first_matched = next(d for d in assessment.decisions if d.matched)
        assert first_matched.rule_id == assessment.matched_rule_id
    # The default rule is the total default.
    assert REPLICATION_DECISION_RULES[-1].rule_id == "R-REP-U1"
    assert REPLICATION_DECISION_RULES[-1].status is ReplicateStatus.INDETERMINATE


def test_replic_paradigm_ruleset_version_recorded():
    assert REPLICATION_RULESET_VERSION == "1.0"
    assert make_sufficient().ruleset_version == REPLICATION_RULESET_VERSION


def test_replic_paradigm_determinism_across_repeated_calls():
    # Pure functions: same inputs, same outputs, on every call.
    one = evaluate_replicate_sufficiency(
        (10.0, 15.0, 5.0),
        technical=(9.9, 10.1),
        min_independent=3,
        precision_threshold=0.1,
        confidence_level=0.9,
    )
    two = evaluate_replicate_sufficiency(
        (10.0, 15.0, 5.0),
        technical=(9.9, 10.1),
        min_independent=3,
        precision_threshold=0.1,
        confidence_level=0.9,
    )
    assert one == two
    assert one.state == two.state
    assert one.decisions == two.decisions
    assert sufficiency_findings(one) == sufficiency_findings(two)


def test_replic_paradigm_module_all_exports_resolve():
    for name in replication_module.__all__:
        assert hasattr(replication_module, name), name
    # Declared exactly once (no duplicate export).
    assert len(replication_module.__all__) == len(set(replication_module.__all__))


def test_replic_paradigm_errors_are_valueerror_subclasses_with_stable_messages():
    assert issubclass(ReplicateAnalysisError, ValueError)
    assert issubclass(InvalidReplicateInputError, ReplicateAnalysisError)
    assert issubclass(ReplicateCriterionError, ReplicateAnalysisError)
    assert issubclass(UnsupportedDecisionModeError, ReplicateAnalysisError)
    # Stable one-line messages: the same degenerate input always raises
    # the same message text.
    with pytest.raises(InvalidReplicateInputError) as first:
        evaluate_replicate_sufficiency((1.0, math.inf), precision_threshold=0.1)
    with pytest.raises(InvalidReplicateInputError) as second:
        evaluate_replicate_sufficiency((1.0, math.inf), precision_threshold=0.1)
    assert str(first.value) == str(second.value)
    assert "finite number" in str(first.value)


def test_replic_paradigm_module_is_pure_no_io_no_randomness():
    source = inspect.getsource(replication_module)
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
        "import json",
    ):
        assert forbidden not in source, forbidden
    # Deterministic end to end.
    assessment = make_wide_assessment()
    assert sufficiency_findings(assessment) == sufficiency_findings(assessment)
    assert sufficiency_metrics("uptake", assessment) == sufficiency_metrics(
        "uptake", assessment
    )


def test_replic_paradigm_frozen_records_reject_mutation():
    records = (
        make_sufficient(),
        make_wide_assessment(),
        ReplicateDecisionInput(
            independent=(10.0, 10.1), precision_threshold=0.1
        ),
        ReplicateState(
            input=ReplicateDecisionInput(
                independent=(10.0, 10.1), precision_threshold=0.1
            ),
            mean=10.05,
            standard_deviation=0.05,
            standard_error=0.05,
            half_width=0.1,
            relative_half_width=0.01,
        ),
        ReplicateCriterion(min_independent=3, precision_threshold=0.1),
        ReplicateRuleDecision(
            rule_id="R-REP-U1",
            description="default",
            status=ReplicateStatus.INDETERMINATE,
            matched=True,
        ),
    )
    for record in records:
        assert is_dataclass(record)
        field_name = next(iter(record.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(record, field_name, None)


def test_replic_paradigm_typeerror_at_boundaries():
    # Wrong types are rejected with TypeError before any value validation.
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency("10.0, 10.1", precision_threshold=0.1)
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency((10.0, True), precision_threshold=0.1)
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency((10.0, 10.1), precision_threshold="0.1")
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency((10.0, 10.1), precision_threshold=True)
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), precision_threshold=0.1, min_independent="3"
        )
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), precision_threshold=0.1, min_independent=True
        )
    with pytest.raises(TypeError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), precision_threshold=0.1, confidence_level="0.95"
        )
    with pytest.raises(TypeError):
        sufficiency_metrics(42, make_sufficient())
    with pytest.raises(TypeError):
        sufficiency_uncertainty_payload("assessment")
    with pytest.raises(TypeError):
        sufficiency_findings(None)
    with pytest.raises(TypeError):
        sufficiency_metrics("uptake", None)


def test_replic_paradigm_degenerate_inputs_rejected():
    # Degenerate and contradictory values are rejected with stable
    # InvalidReplicateInputError messages (never silently accepted):
    # n < 1, non-finite values, invalid confidence levels, non-positive
    # precision thresholds and floors below 1.
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((), precision_threshold=0.1)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((10.0, math.nan), precision_threshold=0.1)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((10.0, math.inf), precision_threshold=0.1)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), technical=(math.nan,), precision_threshold=0.1
        )
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((10.0, 10.1), precision_threshold=0.0)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((10.0, 10.1), precision_threshold=-0.1)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((10.0, 10.1), precision_threshold=math.nan)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency((10.0, 10.1), precision_threshold=math.inf)
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), precision_threshold=0.1, confidence_level=1.0
        )
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), precision_threshold=0.1, confidence_level=0.0
        )
    with pytest.raises(InvalidReplicateInputError):
        evaluate_replicate_sufficiency(
            (10.0, 10.1), precision_threshold=0.1, min_independent=0
        )


def test_replic_paradigm_zero_mean_rejected():
    # The relative-half-width criterion is undefined for a zero mean:
    # the degenerate input is rejected with a stable message (AC-03)
    # instead of producing an infinite or meaningless request.
    with pytest.raises(InvalidReplicateInputError) as exc:
        evaluate_replicate_sufficiency((0.0, 0.0, 0.0), precision_threshold=0.1)
    assert "zero mean" in str(exc.value)
    assert "undefined" in str(exc.value)


def test_replic_paradigm_assessment_integrity_checks():
    # Direct construction with inconsistent data is rejected (the status
    # must match the matched decision; the matched id must be recorded;
    # the request must match the status).
    state = ReplicateState(
        input=ReplicateDecisionInput(
            independent=(10.0, 10.1), precision_threshold=0.1
        ),
        mean=10.05,
        standard_deviation=0.05,
        standard_error=0.05,
        half_width=0.1,
        relative_half_width=0.01,
    )
    decisions = (
        ReplicateRuleDecision(
            rule_id="R-REP-S1",
            description="sufficient",
            status=ReplicateStatus.SUFFICIENT,
            matched=True,
        ),
    )
    with pytest.raises(InvalidReplicateInputError):
        ReplicateSufficiencyAssessment(
            ruleset_version="1.0",
            input=state.input,
            state=state,
            status=ReplicateStatus.INSUFFICIENT,
            decisions=decisions,
            matched_rule_id="R-REP-S1",
            requested_additional_runs=1,
        )
    with pytest.raises(InvalidReplicateInputError):
        ReplicateSufficiencyAssessment(
            ruleset_version="1.0",
            input=state.input,
            state=state,
            status=ReplicateStatus.SUFFICIENT,
            decisions=decisions,
            matched_rule_id="R-NOPE",
            requested_additional_runs=0,
        )
    with pytest.raises(InvalidReplicateInputError):
        ReplicateSufficiencyAssessment(
            ruleset_version="1.0",
            input=state.input,
            state=state,
            status=ReplicateStatus.SUFFICIENT,
            decisions=decisions,
            matched_rule_id="R-REP-S1",
            requested_additional_runs=3,
        )
    with pytest.raises(InvalidReplicateInputError):
        ReplicateSufficiencyAssessment(
            ruleset_version="1.0",
            input=state.input,
            state=state,
            status=ReplicateStatus.INDETERMINATE,
            decisions=decisions,
            matched_rule_id="R-REP-S1",
            requested_additional_runs=0,
        )
    with pytest.raises(InvalidReplicateInputError):
        ReplicateState(
            input=ReplicateDecisionInput(
                independent=(10.0, 10.1), precision_threshold=0.1
            ),
            mean=10.05,
            standard_deviation=None,
            standard_error=None,
            half_width=None,
            relative_half_width=None,
        )
    with pytest.raises(InvalidReplicateInputError):
        ReplicateState(
            input=ReplicateDecisionInput(
                independent=(10.0,), precision_threshold=0.1
            ),
            mean=10.0,
            standard_deviation=0.0,
            standard_error=0.0,
            half_width=0.0,
            relative_half_width=0.0,
        )


def test_replic_paradigm_findings_stable_one_line_strings():
    # Findings are single stable lines: the same assessment always yields
    # the same finding text, with no embedded newlines and no PASS/FAIL.
    for assessment in (
        make_sufficient(),
        make_wide_assessment(),
        make_sufficient(independent=(10.0, 10.1)),
    ):
        findings = sufficiency_findings(assessment)
        assert len(findings) == 1
        assert "\n" not in findings[0]
        assert sufficiency_findings(assessment) == findings
        assert "PASS" not in findings[0] and "FAIL" not in findings[0]


def test_replic_paradigm_metric_name_validation():
    assessment = make_sufficient()
    with pytest.raises(TypeError):
        sufficiency_metrics(42, assessment)
    with pytest.raises(InvalidReplicateInputError):
        sufficiency_metrics("  ", assessment)
    with pytest.raises(TypeError):
        sufficiency_metrics("uptake", "assessment")
    with pytest.raises(TypeError):
        sufficiency_uncertainty_payload(assessment.state)


def test_replic_paradigm_int_inputs_normalized_to_floats():
    # Integer numeric inputs are accepted and normalized to floats.
    integer = evaluate_replicate_sufficiency((10, 10, 10), precision_threshold=1)
    floats = evaluate_replicate_sufficiency((10.0, 10.0, 10.0), precision_threshold=1.0)
    assert integer == floats
    assert integer.input.precision_threshold == 1.0
    assert integer.state.mean == 10.0


def test_replic_paradigm_mean_interval_matches_z_math():
    # The mean interval is exactly estimate +/- z * se at the input level
    # (pinned Z_095), and is None for a single independent replicate.
    assessment = make_sufficient()
    expected_hw = Z_095 * assessment.state.standard_error
    lower, upper = assessment.mean_interval
    assert lower == pytest.approx(10.1 - expected_hw)
    assert upper == pytest.approx(10.1 + expected_hw)
    single = make_sufficient(independent=(10.0,))
    assert single.mean_interval == (None, None)
    assert single.relative_half_width is None
    # Technical repeats never move the interval (AC-01).
    with_technical = make_sufficient(
        independent=(10.0, 10.2, 10.1), technical=(1000.0, 1e-6)
    )
    assert with_technical.mean_interval == assessment.mean_interval
