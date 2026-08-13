"""Confidence interval and equivalence testing primitives (DEV-M9-G03).

Every test name contains "statistic" so ``python -m pytest -q
tests/analysis -k statistic`` selects the whole suite. The
``ac01``/``ac02``/``ac03`` sections map one-to-one to the acceptance
criteria of DEV-M9-G03:

* ``ac01`` -- p > 0.05 alone can never produce REPRODUCED: the decision
  vocabulary has no REPRODUCED member, no public decision API accepts a
  p-value, and every non-significant shape (a confidence interval
  crossing zero) without equivalence evidence (the interval not fully
  inside the frozen bounds) is decided INCONCLUSIVE -- never EQUIVALENT;
* ``ac02`` -- a wide interval crossing the equivalence bounds produces
  INCONCLUSIVE: the TOST comparison fails and the verdict is
  INCONCLUSIVE -- never EQUIVALENT, never "equivalent" -- including
  intervals wider than the region that straddle both bounds;
* ``ac03`` -- equivalence margins are inputs from the frozen Acceptance
  Criteria: ``equivalence_bounds_from_acceptance`` reads the margin
  verbatim from the registered acceptance record (via
  ``register_acceptance`` / ``read_acceptance``), honors it even when it
  differs from any result-derived value, and has no path to derive
  margins from the observed result.

Determinism: every assertion runs the pure functions of
``scientific_reproduction/analysis/statistics.py`` twice or against
fixed expected values; no randomness, no wall clock, no network, no
persistence (the decision interface is pure).
"""

from __future__ import annotations

import inspect
import math
from dataclasses import FrozenInstanceError, replace

import pytest
from protocol_helpers import init_project

import scientific_reproduction.analysis.statistics as statistics_module
from scientific_reproduction.analysis.statistics import (
    DEFAULT_CONFIDENCE_LEVEL,
    EQUIVALENCE_DECISION_RULES,
    ConfidenceInterval,
    EquivalenceAssessment,
    EquivalenceBounds,
    EquivalenceDecisionInput,
    EquivalenceMarginError,
    EquivalenceVerdict,
    InvalidStatisticInputError,
    StatisticsError,
    decide_equivalence,
    effect_confidence_interval,
    effect_metrics,
    equivalence_bounds_from_acceptance,
    mean_confidence_interval,
    uncertainty_report,
    z_critical_value,
)
from scientific_reproduction.core.models import AcceptanceCriteria, DecisionMode
from scientific_reproduction.planning.plan import (
    read_acceptance,
    register_acceptance,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

#: The standard-normal critical value of the 0.95 two-sided level
#: (``statistics.NormalDist().inv_cdf(0.975)``) -- pinned so the CI math
#: is asserted against a fixed expected value.
Z_095 = 1.959963984540054


def make_acceptance(
    acceptance_id: str,
    margin: float,
    *,
    goal_id: str = "G-1",
    decision_mode: DecisionMode = DecisionMode.EQUIVALENCE,
) -> AcceptanceCriteria:
    """Build a schema-valid frozen acceptance criteria record."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=goal_id,
        version="v1",
        frozen=True,
        decision_mode=decision_mode,
        criteria=[{"metric": "effect", "margin": margin}],
    )


def make_bounds(margin: float) -> EquivalenceBounds:
    """The symmetric effect-space region of one frozen margin."""
    return EquivalenceBounds(lower=-margin, upper=margin)


def make_ci(lower: float, upper: float) -> ConfidenceInterval:
    """A confidence interval with deterministic endpoints."""
    return ConfidenceInterval(lower=lower, upper=upper)


# ---------------------------------------------------------------------------
# Deliverable 1: CI utilities
# ---------------------------------------------------------------------------


def test_statistic_ci_mean_interval_math_and_level():
    ci = mean_confidence_interval(2.0, 0.5)
    assert ci.lower == pytest.approx(2.0 - Z_095 * 0.5)
    assert ci.upper == pytest.approx(2.0 + Z_095 * 0.5)
    assert ci.confidence_level == DEFAULT_CONFIDENCE_LEVEL == 0.95
    assert ci.standard_error == pytest.approx(0.5)
    assert ci.contains(2.0)
    assert ci.width == pytest.approx(2 * Z_095 * 0.5)


def test_statistic_ci_effect_difference_interval_centered_on_effect():
    ci = effect_confidence_interval(0.5, 0.25, confidence_level=0.9)
    z90 = z_critical_value(0.9)
    assert ci.lower == pytest.approx(0.5 - z90 * 0.25)
    assert ci.upper == pytest.approx(0.5 + z90 * 0.25)
    assert ci.confidence_level == 0.9


def test_statistic_ci_deterministic_and_repeatable():
    first = mean_confidence_interval(1.0, 0.2, confidence_level=0.9)
    second = mean_confidence_interval(1.0, 0.2, confidence_level=0.9)
    assert first == second
    assert first is not second
    # Same inputs -> same interval on every call (pure function).
    assert first.lower == second.lower and first.upper == second.upper


def test_statistic_ci_higher_level_wider_interval():
    narrow = mean_confidence_interval(0.0, 1.0, confidence_level=0.9)
    wide = mean_confidence_interval(0.0, 1.0, confidence_level=0.99)
    assert wide.width > narrow.width
    assert wide.lower < narrow.lower and wide.upper > narrow.upper


def test_statistic_ci_confidence_level_boundary_rejected():
    for level in (0.0, 1.0, 1.5, -0.1):
        with pytest.raises(InvalidStatisticInputError) as exc:
            mean_confidence_interval(0.0, 1.0, confidence_level=level)
        assert "strictly between 0 and 1" in str(exc.value)


def test_statistic_ci_degenerate_standard_error_rejected():
    for se in (0.0, -1.0, math.nan, math.inf, -math.inf):
        with pytest.raises(InvalidStatisticInputError) as exc:
            mean_confidence_interval(0.0, se)
        # Non-positive -> "positive finite number"; non-finite -> "finite".
        assert "standard_error must be" in str(exc.value)


def test_statistic_ci_nonfinite_mean_rejected():
    for mean in (math.nan, math.inf, -math.inf):
        with pytest.raises(InvalidStatisticInputError) as exc:
            mean_confidence_interval(mean, 1.0)
        assert "mean must be finite" in str(exc.value)
    for effect in (math.nan, math.inf):
        with pytest.raises(InvalidStatisticInputError) as exc:
            effect_confidence_interval(effect, 1.0)
        assert "effect must be finite" in str(exc.value)


def test_statistic_ci_wrong_types_raise_typeerror():
    with pytest.raises(TypeError) as exc:
        mean_confidence_interval("zero", 1.0)
    assert "mean must be a number" in str(exc.value)
    with pytest.raises(TypeError) as exc:
        mean_confidence_interval(0.0, None)
    assert "standard_error must be a number" in str(exc.value)
    with pytest.raises(TypeError) as exc:
        mean_confidence_interval(True, 1.0)
    assert "mean must be a number" in str(exc.value)


def test_statistic_ci_interval_inverted_endpoints_rejected():
    with pytest.raises(InvalidStatisticInputError) as exc:
        ConfidenceInterval(lower=2.0, upper=1.0)
    assert "lower must be <= upper" in str(exc.value)


def test_statistic_ci_interval_nonfinite_endpoints_rejected():
    for lower, upper in ((math.nan, 1.0), (0.0, math.inf)):
        with pytest.raises(InvalidStatisticInputError):
            ConfidenceInterval(lower=lower, upper=upper)


def test_statistic_ci_interval_contains_and_crosses_zero():
    crossing = make_ci(-1.0, 2.0)
    assert crossing.crosses_zero
    assert crossing.contains(0.0)
    assert crossing.contains(-1.0) and crossing.contains(2.0)
    assert not crossing.contains(2.5)
    assert not make_ci(1.0, 2.0).crosses_zero
    assert not make_ci(-2.0, -1.0).crosses_zero


def test_statistic_ci_interval_negative_standard_error_rejected():
    with pytest.raises(InvalidStatisticInputError) as exc:
        ConfidenceInterval(lower=-1.0, upper=1.0, standard_error=0.0)
    assert "standard_error must be a positive finite number" in str(exc.value)


def test_statistic_ci_interval_is_frozen():
    ci = make_ci(-1.0, 1.0)
    with pytest.raises(FrozenInstanceError):
        ci.lower = 0.0  # type: ignore[misc]


def test_statistic_ci_default_confidence_level_is_095():
    assert DEFAULT_CONFIDENCE_LEVEL == 0.95
    assert ConfidenceInterval(-1.0, 1.0).confidence_level == 0.95


def test_statistic_z_critical_value_exact_and_monotonic():
    assert z_critical_value(0.95) == pytest.approx(Z_095)
    assert z_critical_value(0.9) == pytest.approx(1.6448536269514722)
    assert z_critical_value(0.8) < z_critical_value(0.9) < z_critical_value(0.95)
    with pytest.raises(TypeError):
        z_critical_value("0.95")


# ---------------------------------------------------------------------------
# Deliverable 2: equivalence / TOST-style decision interface
# ---------------------------------------------------------------------------


def test_statistic_equivalence_ci_inside_bounds_equivalent():
    assessment = decide_equivalence(0.0, make_ci(-0.05, 0.05), make_bounds(0.1))
    assert assessment.verdict is EquivalenceVerdict.EQUIVALENT
    assert assessment.equivalent
    assert assessment.matched_rule_id == "R-EQ-1"


def test_statistic_equivalence_ci_below_bounds_not_equivalent():
    assessment = decide_equivalence(-2.0, make_ci(-3.0, -1.5), make_bounds(1.0))
    assert assessment.verdict is EquivalenceVerdict.NOT_EQUIVALENT
    assert not assessment.equivalent
    assert assessment.matched_rule_id == "R-EQ-2"


def test_statistic_equivalence_ci_above_bounds_not_equivalent():
    assessment = decide_equivalence(2.0, make_ci(1.5, 3.0), make_bounds(1.0))
    assert assessment.verdict is EquivalenceVerdict.NOT_EQUIVALENT
    assert assessment.matched_rule_id == "R-EQ-2"


def test_statistic_equivalence_ci_straddles_lower_bound_inconclusive():
    # Overlaps the lower decision boundary: evidence is insufficient.
    assessment = decide_equivalence(-0.5, make_ci(-1.5, 0.5), make_bounds(1.0))
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert not assessment.equivalent
    assert assessment.matched_rule_id == "R-EQ-3"


def test_statistic_equivalence_ci_straddles_upper_bound_inconclusive():
    assessment = decide_equivalence(0.5, make_ci(-0.5, 1.5), make_bounds(1.0))
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert assessment.matched_rule_id == "R-EQ-3"


def test_statistic_equivalence_deterministic_assessment():
    first = decide_equivalence(0.2, make_ci(0.0, 0.4), make_bounds(0.5))
    second = decide_equivalence(0.2, make_ci(0.0, 0.4), make_bounds(0.5))
    assert first == second
    assert first.ruleset_version == statistics_module.RULESET_VERSION
    assert first.input.effect == 0.2
    assert first.input.ci == make_ci(0.0, 0.4)
    assert first.input.bounds == make_bounds(0.5)


def test_statistic_equivalence_assessment_records_rule_trace():
    assessment = decide_equivalence(0.0, make_ci(-0.05, 0.05), make_bounds(0.1))
    assert len(assessment.decisions) == len(EQUIVALENCE_DECISION_RULES) == 3
    assert [d.rule_id for d in assessment.decisions] == ["R-EQ-1", "R-EQ-2", "R-EQ-3"]
    # First match wins: R-EQ-1 decided, the trailing total default
    # (predicate always True) still evaluated for the audit trail.
    assert assessment.decisions[0].matched
    assert not assessment.decisions[1].matched
    assert assessment.decisions[2].matched
    assert assessment.matched_rule_id == "R-EQ-1"
    assert assessment.decisions[-1].verdict is EquivalenceVerdict.INCONCLUSIVE


def test_statistic_equivalence_decision_rule_default_is_total():
    # Every conceivable interval maps to exactly one verdict: the trailing
    # default (R-EQ-3) makes the mapping total over the whole grid.
    verdicts: set[EquivalenceVerdict] = set()
    for lower in (-2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
        for upper in (-1.0, -0.5, 0.0, 0.5, 1.0, 2.0):
            if upper < lower:
                continue
            verdicts.add(decide_equivalence(0.0, make_ci(lower, upper), make_bounds(1.0)).verdict)
    assert verdicts == {
        EquivalenceVerdict.EQUIVALENT,
        EquivalenceVerdict.NOT_EQUIVALENT,
        EquivalenceVerdict.INCONCLUSIVE,
    }


def test_statistic_equivalence_wrong_types_raise_typeerror():
    with pytest.raises(TypeError) as exc:
        decide_equivalence(0.0, "ci", make_bounds(1.0))
    assert "ci must be a ConfidenceInterval" in str(exc.value)
    with pytest.raises(TypeError) as exc:
        decide_equivalence(0.0, make_ci(-1.0, 1.0), (-1.0, 1.0))
    assert "bounds must be an EquivalenceBounds" in str(exc.value)
    with pytest.raises(TypeError) as exc:
        decide_equivalence(True, make_ci(-1.0, 1.0), make_bounds(1.0))
    assert "effect must be a number" in str(exc.value)


def test_statistic_equivalence_nonfinite_effect_rejected():
    with pytest.raises(InvalidStatisticInputError) as exc:
        decide_equivalence(math.nan, make_ci(-1.0, 1.0), make_bounds(1.0))
    assert "effect must be finite" in str(exc.value)


def test_statistic_equivalence_bounds_require_positive_width():
    with pytest.raises(InvalidStatisticInputError) as exc:
        EquivalenceBounds(lower=-1.0, upper=-1.0)
    assert "strictly greater than" in str(exc.value)
    with pytest.raises(InvalidStatisticInputError):
        EquivalenceBounds(lower=1.0, upper=-1.0)


def test_statistic_equivalence_bounds_nonfinite_rejected():
    with pytest.raises(InvalidStatisticInputError):
        EquivalenceBounds(lower=math.nan, upper=1.0)
    with pytest.raises(InvalidStatisticInputError):
        EquivalenceBounds(lower=-1.0, upper=math.inf)


def test_statistic_equivalence_input_record_validates_fields():
    with pytest.raises(TypeError) as exc:
        EquivalenceDecisionInput(effect=0.0, ci=make_ci(-1.0, 1.0), bounds=(-1.0, 1.0))
    assert "bounds must be an EquivalenceBounds" in str(exc.value)
    with pytest.raises(InvalidStatisticInputError):
        EquivalenceDecisionInput(effect=math.inf, ci=make_ci(-1.0, 1.0), bounds=make_bounds(1.0))


def test_statistic_equivalence_point_interval_inside_region_equivalent():
    # A zero-width interval strictly inside the region is equivalence
    # evidence at the decision level (pure math; real intervals carry a
    # positive SE, which the CI utilities enforce).
    assessment = decide_equivalence(0.0, ConfidenceInterval(0.0, 0.0), make_bounds(1.0))
    assert assessment.verdict is EquivalenceVerdict.EQUIVALENT


def test_statistic_equivalence_interval_exactly_region_is_inconclusive():
    # Touching both decision boundaries is not "sufficiently inside".
    assessment = decide_equivalence(0.0, make_ci(-1.0, 1.0), make_bounds(1.0))
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE


# ---------------------------------------------------------------------------
# AC-01: p > 0.05 alone can never produce REPRODUCED
# ---------------------------------------------------------------------------

#: Non-significant shapes (intervals crossing zero) with **no** equivalence
#: evidence (not fully inside the bounds): each must be INCONCLUSIVE.
NON_SIGNIFICANT_SHAPES: tuple[tuple[float, float, float], ...] = (
    (-0.2, 0.2, 0.1),  # crosses zero, straddles both bounds
    (-2.0, 0.5, 1.0),  # crosses zero, straddles the lower bound
    (-0.5, 2.0, 1.0),  # crosses zero, straddles the upper bound
    (-3.0, 3.0, 1.0),  # crosses zero, wider than the whole region
    (-0.1, 0.1, 0.05),  # crosses zero, touches both bounds (not inside)
    (-1.0, 1.0, 0.9),  # crosses zero, wider than a narrow region
)


@pytest.mark.parametrize(
    "lower,upper,margin",
    NON_SIGNIFICANT_SHAPES,
    ids=[f"ci[{lo},{hi}]m{mg}" for lo, hi, mg in NON_SIGNIFICANT_SHAPES],
)
def test_statistic_ac01_non_significant_without_equivalence_never_equivalent(
    lower, upper, margin
):
    ci = make_ci(lower, upper)
    assert ci.crosses_zero  # the p > 0.05 shape (interval contains 0)
    assessment = decide_equivalence(0.0, ci, make_bounds(margin))
    # p > 0.05 alone, without the interval fully inside the frozen bounds,
    # can never produce EQUIVALENT (the decision-side of REPRODUCED).
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert not assessment.equivalent
    assert assessment.matched_rule_id == "R-EQ-3"


def test_statistic_ac01_verdict_vocabulary_has_no_reproduced_value():
    # By construction: the decision vocabulary carries no REPRODUCED
    # member -- statistics decides, the outcome layer closes.
    assert set(EquivalenceVerdict) == {
        EquivalenceVerdict.EQUIVALENT,
        EquivalenceVerdict.NOT_EQUIVALENT,
        EquivalenceVerdict.INCONCLUSIVE,
    }
    values = {member.value for member in EquivalenceVerdict}
    assert "REPRODUCED" not in values
    assert "NOT_REPRODUCED" not in values


def test_statistic_ac01_decision_api_has_no_p_value_path():
    # By construction: no public decision entry point accepts a p-value,
    # and the decision input record carries no significance field -- there
    # is no path from "p > 0.05" alone to any verdict.
    assert set(inspect.signature(decide_equivalence).parameters) == {
        "effect",
        "ci",
        "bounds",
    }
    assert set(EquivalenceDecisionInput.__dataclass_fields__) == {
        "effect",
        "ci",
        "bounds",
    }
    assert "p_value" not in EquivalenceAssessment.__dataclass_fields__
    assert "p_value" not in statistics_module.EquivalenceVerdict.__members__


def test_statistic_ac01_non_significant_inside_region_is_equivalence_evidence():
    # TOST distinction: p > 0.05 *with* the interval fully inside the
    # frozen region IS equivalence evidence (the region, not the p-value,
    # decides). This is the "not alone" case of AC-01.
    ci = mean_confidence_interval(0.0, 0.02)  # ~ +/-0.04, crosses zero
    assert ci.crosses_zero
    assessment = decide_equivalence(0.0, ci, make_bounds(0.1))
    assert assessment.verdict is EquivalenceVerdict.EQUIVALENT
    assert assessment.matched_rule_id == "R-EQ-1"


def test_statistic_ac01_all_non_significant_shapes_never_equivalent_grid():
    for lower in (-0.4, -0.2, -0.1):
        for upper in (0.1, 0.2, 0.4):
            ci = make_ci(lower, upper)
            assert ci.crosses_zero
            for margin in (0.05, 0.15, 0.3):
                if ci.lower > -margin and ci.upper < margin:
                    continue  # inside the region: equivalence evidence
                assessment = decide_equivalence(0.0, ci, make_bounds(margin))
                assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
                assert not assessment.equivalent


# ---------------------------------------------------------------------------
# AC-02: wide interval crossing equivalence bounds produces INCONCLUSIVE
# ---------------------------------------------------------------------------

#: Intervals crossing at least one bound (several wider than the region).
STRADDLING_SHAPES: tuple[tuple[float, float, float], ...] = (
    (-1.5, 1.5, 1.0),  # wider than the region, straddles both bounds
    (-3.0, 3.0, 1.0),  # much wider, straddles both bounds
    (-10.0, 10.0, 1.0),  # extremely wide, straddles both bounds
    (-1.5, 0.8, 1.0),  # wider than the region, straddles the lower bound
    (-0.8, 1.5, 1.0),  # wider than the region, straddles the upper bound
    (-0.5, 2.0, 1.0),  # crosses the upper bound, wider on that side
    (-2.0, 0.5, 1.0),  # crosses the lower bound, wider on that side
)


@pytest.mark.parametrize(
    "lower,upper,margin",
    STRADDLING_SHAPES,
    ids=[f"ci[{lo},{hi}]m{mg}" for lo, hi, mg in STRADDLING_SHAPES],
)
def test_statistic_ac02_interval_crossing_bounds_inconclusive(lower, upper, margin):
    ci = make_ci(lower, upper)
    assert ci.width >= 2 * margin  # as wide as or wider than the region
    bounds = make_bounds(margin)
    assert ci.lower < bounds.lower and ci.upper > bounds.upper or (
        (ci.lower < bounds.lower) != (ci.upper > bounds.upper)
    )
    assessment = decide_equivalence(0.0, ci, bounds)
    # The TOST comparison fails (not equivalent) and the decision is
    # INCONCLUSIVE -- never EQUIVALENT, never "equivalent".
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert not assessment.equivalent
    assert assessment.matched_rule_id == "R-EQ-3"


def test_statistic_ac02_wide_interval_crossing_both_bounds_inconclusive():
    bounds = make_bounds(1.0)
    wide = make_ci(-1.5, 1.5)
    assert wide.lower < bounds.lower and wide.upper > bounds.upper
    assessment = decide_equivalence(0.0, wide, bounds)
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert assessment.verdict is not EquivalenceVerdict.EQUIVALENT
    assert assessment.verdict is not EquivalenceVerdict.NOT_EQUIVALENT
    # No REPRODUCED anywhere in the assessment record.
    assert "REPRODUCED" not in assessment.verdict.value


def test_statistic_ac02_never_equivalent_for_any_straddling_shape():
    for lower, upper, margin in STRADDLING_SHAPES:
        assessment = decide_equivalence(0.0, make_ci(lower, upper), make_bounds(margin))
        assert not assessment.equivalent
        assert assessment.verdict is not EquivalenceVerdict.EQUIVALENT


# ---------------------------------------------------------------------------
# AC-03: equivalence margins are inputs from frozen Acceptance Criteria
# ---------------------------------------------------------------------------


def test_statistic_ac03_margins_read_from_registered_acceptance_verbatim(tmp_path):
    root = init_project(tmp_path)
    register_acceptance(root, make_acceptance("ACC-STAT-1", margin=0.05))
    acceptance = read_acceptance(root, "ACC-STAT-1")
    assert acceptance.frozen is True
    bounds = equivalence_bounds_from_acceptance(acceptance)
    # The bounds are the stored margin, verbatim: [-0.05, +0.05].
    assert bounds == EquivalenceBounds(lower=-0.05, upper=0.05)
    assert bounds.lower == -acceptance.criteria[0]["margin"]
    assert bounds.upper == acceptance.criteria[0]["margin"]


def test_statistic_ac03_margin_honored_when_differing_from_result_derived(tmp_path):
    root = init_project(tmp_path)
    register_acceptance(root, make_acceptance("ACC-STAT-1", margin=0.05))
    bounds = equivalence_bounds_from_acceptance(read_acceptance(root, "ACC-STAT-1"))
    # The observed result is *tight* (CI ~ +/-0.04): a margin inferred from
    # the result (e.g. 2*SE) would be ~0.04 and the interval would straddle
    # it (INCONCLUSIVE). The frozen margin 0.05 is honored as given, so the
    # interval lies fully inside and the decision is EQUIVALENT.
    ci = mean_confidence_interval(0.0, 0.02)
    assert ci.upper < bounds.upper
    assessment = decide_equivalence(0.0, ci, bounds)
    assert assessment.verdict is EquivalenceVerdict.EQUIVALENT
    assert bounds.lower == -0.05 and bounds.upper == 0.05


def test_statistic_ac03_margin_honored_when_wider_than_result_spread(tmp_path):
    root = init_project(tmp_path)
    register_acceptance(root, make_acceptance("ACC-STAT-1", margin=0.05))
    bounds = equivalence_bounds_from_acceptance(read_acceptance(root, "ACC-STAT-1"))
    # The observed result is *wide* (CI ~ +/-0.39): a margin inferred from
    # the result would be ~0.39 and the interval would be inside it
    # (EQUIVALENT). The frozen margin 0.05 is honored as given, so the
    # interval straddles the bounds and the decision is INCONCLUSIVE.
    ci = mean_confidence_interval(0.0, 0.2)
    assert ci.upper > bounds.upper
    assessment = decide_equivalence(0.0, ci, bounds)
    assert assessment.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert not assessment.equivalent


def test_statistic_ac03_bounds_never_derived_from_result(tmp_path):
    root = init_project(tmp_path)
    register_acceptance(root, make_acceptance("ACC-STAT-1", margin=0.05))
    acceptance = read_acceptance(root, "ACC-STAT-1")
    before = equivalence_bounds_from_acceptance(acceptance)
    # The extractor is a pure function of the acceptance record: it takes
    # no effect/CI/result input at all (proven by signature) and repeated
    # extraction with any result shape around it returns the same bounds.
    assert set(inspect.signature(equivalence_bounds_from_acceptance).parameters) == {
        "acceptance"
    }
    decide_equivalence(5.0, mean_confidence_interval(5.0, 10.0), before)
    after = equivalence_bounds_from_acceptance(acceptance)
    assert before == after == EquivalenceBounds(lower=-0.05, upper=0.05)


def test_statistic_ac03_different_margins_yield_different_bounds():
    for margin in (0.05, 0.5, 2.5):
        bounds = equivalence_bounds_from_acceptance(make_acceptance("ACC", margin))
        assert bounds == EquivalenceBounds(lower=-margin, upper=margin)


def test_statistic_ac03_no_margin_entry_rejected():
    acceptance = AcceptanceCriteria(
        acceptance_id="ACC-STAT-1",
        goal_id="G-1",
        version="v1",
        frozen=True,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "batch_level_uptake", "rule": "to_be_frozen"}],
    )
    with pytest.raises(EquivalenceMarginError) as exc:
        equivalence_bounds_from_acceptance(acceptance)
    assert "no equivalence margin" in str(exc.value)
    assert "never inferred from the result" in str(exc.value)


def test_statistic_ac03_non_numeric_margin_rejected():
    acceptance = make_acceptance("ACC-STAT-1", margin=0.1)
    acceptance = replace(
        acceptance, criteria=[{"metric": "effect", "margin": "10%"}]
    )
    with pytest.raises(EquivalenceMarginError) as exc:
        equivalence_bounds_from_acceptance(acceptance)
    assert "positive finite number" in str(exc.value)


def test_statistic_ac03_non_positive_margin_rejected():
    for margin in (0.0, -1.0, math.nan):
        acceptance = replace(
            make_acceptance("ACC-STAT-1", margin=0.1),
            criteria=[{"metric": "effect", "margin": margin}],
        )
        with pytest.raises(EquivalenceMarginError) as exc:
            equivalence_bounds_from_acceptance(acceptance)
        assert "positive finite number" in str(exc.value)


def test_statistic_ac03_ambiguous_margins_rejected():
    acceptance = replace(
        make_acceptance("ACC-STAT-1", margin=0.1),
        criteria=[
            {"metric": "effect", "margin": 0.1},
            {"metric": "strain", "margin": 0.5},
        ],
    )
    with pytest.raises(EquivalenceMarginError) as exc:
        equivalence_bounds_from_acceptance(acceptance)
    assert "ambiguous equivalence margins" in str(exc.value)


def test_statistic_ac03_acceptance_type_boundary():
    with pytest.raises(TypeError) as exc:
        equivalence_bounds_from_acceptance({"margin": 0.1})
    assert "expects an AcceptanceCriteria" in str(exc.value)


def test_statistic_ac03_decision_uses_given_bounds_only():
    # The decision interface takes the bounds as given: flipping the frozen
    # margin flips the verdict; the effect/CI never feed the bounds back.
    ci = make_ci(-0.2, 0.2)
    tight = decide_equivalence(0.0, ci, make_bounds(0.1))
    loose = decide_equivalence(0.0, ci, make_bounds(0.5))
    assert tight.verdict is EquivalenceVerdict.INCONCLUSIVE
    assert loose.verdict is EquivalenceVerdict.EQUIVALENT
    assert tight.input.bounds == make_bounds(0.1)
    assert loose.input.bounds == make_bounds(0.5)


# ---------------------------------------------------------------------------
# Deliverable 3: effect/uncertainty reporting hooks
# ---------------------------------------------------------------------------


def test_statistic_report_uncertainty_dict_shape():
    ci = mean_confidence_interval(1.0, 0.5)
    report = uncertainty_report(ci)
    assert set(report) == {
        "method",
        "confidence_level",
        "lower",
        "upper",
        "standard_error",
    }
    assert report["method"] == "confidence_interval"
    assert report["confidence_level"] == 0.95
    assert report["lower"] == ci.lower
    assert report["upper"] == ci.upper
    assert report["standard_error"] == 0.5


def test_statistic_report_uncertainty_omits_unknown_standard_error():
    report = uncertainty_report(make_ci(-1.0, 1.0))
    assert set(report) == {"method", "confidence_level", "lower", "upper"}
    assert "standard_error" not in report


def test_statistic_report_effect_metrics_entries():
    ci = mean_confidence_interval(0.5, 0.25)
    entries = effect_metrics("uptake_difference", 0.5, ci)
    assert entries == [
        {"metric": "uptake_difference", "value": 0.5},
        {"metric": "uptake_difference_ci_lower", "value": ci.lower},
        {"metric": "uptake_difference_ci_upper", "value": ci.upper},
    ]


def test_statistic_report_entries_fit_result_record_shapes(tmp_path):
    # The hooks produce exactly the shapes the DEV-M9-G02 result records
    # consume (ResultRecord.metrics / ResultRecord.uncertainty): a record
    # carrying both constructs without reshaping.
    from scientific_reproduction.analysis.results import ResultRecord
    from scientific_reproduction.core.models import PrimaryOrExploratory

    ci = mean_confidence_interval(0.5, 0.25)
    record = ResultRecord(
        result_id="RES-STAT-1",
        analysis_id="ANL-1",
        protocol_version="v1",
        run_ref="RUN-001",
        input_artifact_ids=["ART-001"],
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        metrics=effect_metrics("uptake_difference", 0.5, ci),
        uncertainty=uncertainty_report(ci),
    )
    assert record.metrics[0] == {"metric": "uptake_difference", "value": 0.5}
    assert record.uncertainty["method"] == "confidence_interval"
    assert record.uncertainty["lower"] == ci.lower
    assert record.uncertainty["upper"] == ci.upper


def test_statistic_report_metric_name_validation():
    ci = make_ci(-1.0, 1.0)
    with pytest.raises(TypeError) as exc:
        effect_metrics(42, 0.5, ci)
    assert "metric_name must be a str" in str(exc.value)
    with pytest.raises(InvalidStatisticInputError) as exc:
        effect_metrics("  ", 0.5, ci)
    assert "non-empty string" in str(exc.value)
    with pytest.raises(TypeError) as exc:
        uncertainty_report("ci")
    assert "ci must be a ConfidenceInterval" in str(exc.value)


# ---------------------------------------------------------------------------
# Paradigm boundaries
# ---------------------------------------------------------------------------


def test_statistic_module_all_exports_resolve():
    for name in statistics_module.__all__:
        assert hasattr(statistics_module, name), name
    # Declared exactly once (no duplicate export).
    assert len(statistics_module.__all__) == len(set(statistics_module.__all__))


def test_statistic_errors_are_valueerror_subclasses_with_stable_messages():
    assert issubclass(StatisticsError, ValueError)
    assert issubclass(InvalidStatisticInputError, StatisticsError)
    assert issubclass(EquivalenceMarginError, StatisticsError)
    # Stable one-line messages: the same degenerate input always raises the
    # same message text.
    with pytest.raises(InvalidStatisticInputError) as first:
        mean_confidence_interval(0.0, -1.0)
    with pytest.raises(InvalidStatisticInputError) as second:
        mean_confidence_interval(0.0, -1.0)
    assert str(first.value) == str(second.value)
    assert "positive finite number" in str(first.value)


def test_statistic_module_is_pure_no_io_no_randomness():
    source = inspect.getsource(statistics_module)
    for forbidden in (
        "import random",
        "random.",
        "time.time",
        "datetime.now",
        "urllib",
        "requests",
        "socket",
        "open(",
    ):
        assert forbidden not in source
    # Pure functions: same inputs, same outputs, no state.
    ci = make_ci(-0.05, 0.05)
    one = decide_equivalence(0.0, ci, make_bounds(0.1))
    two = decide_equivalence(0.0, ci, make_bounds(0.1))
    assert one == two
