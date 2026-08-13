"""Confidence interval and equivalence testing primitives (DEV-M9-G03).

Implements the **quantitative validation interfaces** deliverable of
DEV-M9-G03 over the frozen ``core.models`` acceptance vocabulary,
grounded in:

* ``07-STATISTICS-AND-ACCEPTANCE.md`` SS3 (preferred acceptance logic:
  confidence intervals, effect sizes, equivalence tests (e.g. TOST));
  SS4 (forbidden shortcut: ``p > 0.05`` from a difference test is not
  sufficient evidence that two results are equivalent); SS5 (three-way
  result state: interval sufficiently inside the region -> PASS,
  interval sufficiently outside / evidence of non-equivalence -> FAIL,
  interval overlaps decision boundaries or evidence is insufficient ->
  INCONCLUSIVE); SS8 (every numeric margin must record its basis -- no
  global "+/-10% for everything" rule, so margins are never invented
  from the observed result); SS9 (freeze the equivalence margin before
  data are observed);
* ``20-ARCHITECTURE-DECISIONS.md`` items 13-14 (statistics must support
  equivalence/uncertainty; "p>0.05 means same" is forbidden;
  PASS/FAIL/INCONCLUSIVE required);
* ``core/models.py`` ``AcceptanceCriteria`` (``criteria`` entries carry
  the frozen numeric ``margin``; ``decision_mode``; ``target`` is the
  published seed value the analysis step computes the effect against).

Deliverable 1 -- CI utilities
-----------------------------
:func:`mean_confidence_interval` / :func:`effect_confidence_interval`
compute the symmetric z-based confidence interval of a mean-type effect
metric from its point estimate and standard error (pure math, stdlib
``statistics.NormalDist`` only -- no scipy, no randomness, no wall
clock). :class:`ConfidenceInterval` is the frozen typed interval (lower,
upper, confidence level, optional standard error) with the standard
shape checks; the interval is computed as ``estimate +/- z * se`` where
``z`` is the standard-normal critical value of the two-sided level
(:func:`z_critical_value`). Degenerate inputs (non-finite values,
non-positive standard error, confidence levels outside ``(0, 1)``,
inverted bounds) are rejected with stable ``InvalidStatisticInputError``
messages.

Deliverable 2 -- equivalence / TOST-style decision interface
------------------------------------------------------------
:func:`decide_equivalence` performs the two one-sided comparisons of a
TOST-style decision over the frozen inputs -- the effect estimate, its
:class:`ConfidenceInterval` and the :class:`EquivalenceBounds` -- and
produces a deterministic three-way :class:`EquivalenceVerdict`
(``EQUIVALENT`` / ``NOT_EQUIVALENT`` / ``INCONCLUSIVE``) through the
ordered, versioned ``EQUIVALENCE_DECISION_RULES`` rule table (first
match wins, trailing total default, ``matched_rule_id`` never ``None``
with a post-assert). The point-estimate alone never decides: the
uncertainty interval carries the evidence, exactly as ``07-...`` SS5's
"uncertainty interval" wording requires.

The verdict vocabulary maps onto the frozen outcome vocabulary
(``RequirementOutcome`` REPRODUCED / NOT_REPRODUCED / INCONCLUSIVE) at
the outcome layer (``core/rules/outcome.py``) -- this module **decides
only and never closes outcomes**: no Requirement state is read or
written, no verdict member carries the REPRODUCED value, and no public
decision function accepts a p-value. The forbidden shortcut is
impossible **by construction** (DEV-M9-G03 AC-01):

* the verdict vocabulary has no ``REPRODUCED`` member;
* no decision API takes a p-value -- non-significance can enter only as
  a confidence interval that contains 0 (:attr:`ConfidenceInterval.crosses_zero`);
* ``R-EQ-1`` (EQUIVALENT) requires the interval to lie **entirely
  inside the frozen bounds** -- a non-significant result whose interval
  is not fully inside the bounds is INCONCLUSIVE (``R-EQ-3``), never
  EQUIVALENT. A non-significant interval that *is* fully inside the
  bounds is equivalence evidence (TOST), not "p>0.05 alone".

AC-02 -- wide interval crossing the equivalence bounds: an interval
that straddles at least one bound fails the TOST comparison and is
INCONCLUSIVE (``R-EQ-3``) -- never EQUIVALENT, never REPRODUCED.

Deliverable 3 -- effect/uncertainty reporting hooks
---------------------------------------------------
:func:`effect_metrics` / :func:`uncertainty_report` produce the derived
metric entries and the uncertainty payload in the exact shapes the
DEV-M9-G02 result records consume (``ResultRecord.metrics`` -- a list
of ``{"metric": ..., "value": ...}`` dicts -- and
``ResultRecord.uncertainty`` -- a dict), so a quantitative decision can
be attached to an analysis result package without any reshaping.

AC-03 -- equivalence margins are frozen inputs
-----------------------------------------------
:func:`equivalence_bounds_from_acceptance` reads the equivalence
half-width from the registered, frozen ``AcceptanceCriteria`` record
(``criteria`` entries carrying a numeric ``margin``, exactly as
registered via ``planning.plan.register_acceptance`` /
``read_acceptance``). The margin is a half-width around the published
target, so in effect space (observed minus target) the region is the
symmetric ``[-margin, +margin]``. The bounds are a pure function of the
acceptance record -- never computed from the effect estimate, its
confidence interval or any other observed value. A record without a
numeric positive margin is rejected with a stable
``EquivalenceMarginError``; ambiguous (differing) margins are rejected
too.

Determinism and boundaries
--------------------------
All functions are pure and deterministic: same inputs -> same decision,
on every platform; no randomness, no wall clock, no network, no
persistence (the decision interface is pure -- nothing is written).
``TypeError`` at the public boundaries; value violations follow the
``ValueError``-subclass convention with stable messages;
``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from statistics import NormalDist
from typing import Any, Callable

from scientific_reproduction.core.models import AcceptanceCriteria

__all__ = [
    "DEFAULT_CONFIDENCE_LEVEL",
    "RULESET_VERSION",
    # errors
    "StatisticsError",
    "InvalidStatisticInputError",
    "EquivalenceMarginError",
    # CI utilities (deliverable 1)
    "ConfidenceInterval",
    "mean_confidence_interval",
    "effect_confidence_interval",
    "z_critical_value",
    # equivalence / TOST-style decision interface (deliverable 2)
    "EquivalenceVerdict",
    "EquivalenceBounds",
    "EquivalenceDecisionInput",
    "EquivalenceDecisionRule",
    "EquivalenceRuleDecision",
    "EquivalenceAssessment",
    "EQUIVALENCE_DECISION_RULES",
    "decide_equivalence",
    # effect/uncertainty reporting hooks (deliverable 3)
    "effect_metrics",
    "uncertainty_report",
    # AC-03: frozen acceptance margins
    "equivalence_bounds_from_acceptance",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class StatisticsError(ValueError):
    """Base class for all statistics primitive errors."""


class InvalidStatisticInputError(StatisticsError):
    """Raised when a statistic input is degenerate.

    Covers non-finite numbers, non-positive standard errors, confidence
    levels outside ``(0, 1)`` and inverted interval/bounds. A degenerate
    input cannot silently change a decision, so it is rejected up front
    with a stable message.
    """


class EquivalenceMarginError(StatisticsError):
    """Raised when the frozen acceptance criteria carry no usable margin.

    AC-03: equivalence margins are inputs from the frozen Acceptance
    Criteria -- a record without a numeric positive ``margin`` (or with
    several differing margins) cannot provide the equivalence region and
    is rejected instead of falling back to anything result-derived.
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The two-sided confidence level of the CI utilities by default.
DEFAULT_CONFIDENCE_LEVEL: float = 0.95

#: Version of the equivalence-decision rule table. Bumped whenever a rule
#: changes; recorded in every assessment so old decisions stay
#: interpretable.
RULESET_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Deliverable 1: CI utilities (pure math, stdlib only)
# ---------------------------------------------------------------------------


def _require_number(value: Any, label: str) -> float:
    """Reject non-numeric values at the numeric boundaries (TypeError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number, got {type(value).__name__}")
    return float(value)


def _require_finite(value: Any, label: str) -> float:
    """Reject non-finite numeric values (stable InvalidStatisticInputError)."""
    number = _require_number(value, label)
    if not math.isfinite(number):
        raise InvalidStatisticInputError(f"{label} must be finite, got {value!r}")
    return number


def _require_positive(value: Any, label: str) -> float:
    """Reject non-positive numeric values (standard errors, margins)."""
    number = _require_finite(value, label)
    if number <= 0:
        raise InvalidStatisticInputError(
            f"{label} must be a positive finite number, got {value!r}"
        )
    return number


def _require_confidence_level(value: Any, label: str) -> float:
    """Reject confidence levels outside the open interval (0, 1)."""
    number = _require_finite(value, label)
    if not 0.0 < number < 1.0:
        raise InvalidStatisticInputError(
            f"{label} must be strictly between 0 and 1, got {value!r}"
        )
    return number


@dataclass(frozen=True)
class ConfidenceInterval:
    """A frozen two-sided confidence interval of an effect metric.

    ``lower`` / ``upper`` are the interval endpoints (``lower <= upper``);
    ``confidence_level`` is the two-sided level the interval was computed
    at (``DEFAULT_CONFIDENCE_LEVEL`` when not specified); ``standard_error``
    is the standard error the interval was computed from (``None`` when
    unknown -- e.g. an interval constructed directly). The interval is
    hashable and comparable, so "same inputs -> same interval" is directly
    testable.

    The interval is pure evidence: the equivalence decision
    (:func:`decide_equivalence`) is a function of the interval against the
    frozen bounds, never of a p-value (:attr:`crosses_zero` carries the
    ``p > 0.05`` reading for the tests of DEV-M9-G03 AC-01).

    Raises:
        TypeError: ``lower`` / ``upper`` / ``confidence_level`` /
            ``standard_error`` have the wrong type.
        InvalidStatisticInputError: an endpoint is non-finite,
            ``lower > upper``, the confidence level is outside ``(0, 1)``,
            or the standard error is not positive.
    """

    lower: float
    upper: float
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL
    standard_error: float | None = None

    def __post_init__(self) -> None:
        _require_finite(self.lower, "ConfidenceInterval.lower")
        _require_finite(self.upper, "ConfidenceInterval.upper")
        if self.lower > self.upper:
            raise InvalidStatisticInputError(
                "ConfidenceInterval.lower must be <= upper, got"
                f" ({self.lower}, {self.upper})"
            )
        _require_confidence_level(
            self.confidence_level, "ConfidenceInterval.confidence_level"
        )
        if self.standard_error is not None:
            _require_positive(
                self.standard_error, "ConfidenceInterval.standard_error"
            )

    @property
    def width(self) -> float:
        """The interval width (``upper - lower``)."""
        return self.upper - self.lower

    @property
    def crosses_zero(self) -> bool:
        """True iff the interval contains 0.

        For an interval at two-sided level ``1 - alpha``, containing 0 is
        the difference-test reading of ``p > alpha`` -- the effect is not
        significant. This is exactly the "no significant difference" shape
        that DEV-M9-G03 AC-01 forbids treating as equivalence evidence on
        its own.
        """
        return self.lower <= 0 <= self.upper

    def contains(self, value: Any) -> bool:
        """True iff ``value`` lies within the closed interval.

        Raises:
            TypeError: ``value`` is not a number.
            InvalidStatisticInputError: ``value`` is non-finite.
        """
        number = _require_finite(value, "value")
        return self.lower <= number <= self.upper


def z_critical_value(confidence_level: float = DEFAULT_CONFIDENCE_LEVEL) -> float:
    """Return the standard-normal critical value of a two-sided level.

    The ``z`` of ``estimate +/- z * se``: the ``(1 + level) / 2``
    quantile of the standard normal distribution, computed with the
    stdlib ``statistics.NormalDist`` (pure, deterministic -- no scipy
    dependency).

    Raises:
        TypeError: ``confidence_level`` is not a number.
        InvalidStatisticInputError: ``confidence_level`` is not strictly
            between 0 and 1.
    """
    level = _require_confidence_level(confidence_level, "confidence_level")
    return NormalDist().inv_cdf((1.0 + level) / 2.0)


def _symmetric_interval(
    estimate: Any, label: str, standard_error: Any, confidence_level: float
) -> ConfidenceInterval:
    """The shared ``estimate +/- z * se`` computation of the CI utilities."""
    center = _require_finite(estimate, label)
    se = _require_positive(standard_error, "standard_error")
    level = _require_confidence_level(confidence_level, "confidence_level")
    z = z_critical_value(level)
    half_width = z * se
    return ConfidenceInterval(
        lower=center - half_width,
        upper=center + half_width,
        confidence_level=level,
        standard_error=se,
    )


def mean_confidence_interval(
    mean: float,
    standard_error: float,
    *,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> ConfidenceInterval:
    """Compute the confidence interval of a mean-type effect metric.

    The symmetric z-based interval ``mean +/- z * se`` (``07-...`` SS3:
    confidence intervals are a preferred acceptance tool for continuous
    quantitative results). The interval carries the level and the
    standard error so the reporting hooks
    (:func:`uncertainty_report` / :func:`effect_metrics`) can attach them
    to a result record.

    Args:
        mean: the point estimate of the metric.
        standard_error: its standard error (strictly positive).
        confidence_level: the two-sided level, strictly between 0 and 1
            (default ``DEFAULT_CONFIDENCE_LEVEL`` = 0.95).

    Returns:
        The frozen :class:`ConfidenceInterval`.

    Raises:
        TypeError: an argument has the wrong type.
        InvalidStatisticInputError: a value is non-finite, the standard
            error is not positive, or the level is outside ``(0, 1)``.
    """
    return _symmetric_interval(mean, "mean", standard_error, confidence_level)


def effect_confidence_interval(
    effect: float,
    standard_error: float,
    *,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> ConfidenceInterval:
    """Compute the confidence interval of an effect (deviation) metric.

    The symmetric z-based interval of the reproduction effect -- the
    deviation of an observed value from the published target value --
    ``effect +/- z * se``. Same math as :func:`mean_confidence_interval`
    with the effect-metric semantics; the equivalence decision consumes
    the interval in effect space (bounds from
    :func:`equivalence_bounds_from_acceptance`).

    Raises:
        TypeError: an argument has the wrong type.
        InvalidStatisticInputError: a value is non-finite, the standard
            error is not positive, or the level is outside ``(0, 1)``.
    """
    return _symmetric_interval(effect, "effect", standard_error, confidence_level)


# ---------------------------------------------------------------------------
# Deliverable 2: equivalence / TOST-style decision interface
# ---------------------------------------------------------------------------


class EquivalenceVerdict(StrEnum):
    """The three-way quantitative decision vocabulary (TOST-style).

    * ``EQUIVALENT`` -- the uncertainty interval lies entirely inside the
      frozen equivalence bounds: both one-sided comparisons reject
      non-equivalence (``07-...`` SS5: interval sufficiently inside the
      region -> PASS). This is the decision-side evidence the outcome
      layer maps to ``RequirementOutcome.REPRODUCED``.
    * ``NOT_EQUIVALENT`` -- the interval lies entirely outside the region
      on one side: evidence of non-equivalence (SS5: -> FAIL; maps to
      ``RequirementOutcome.NOT_REPRODUCED``).
    * ``INCONCLUSIVE`` -- the interval overlaps at least one bound or is
      wider than the region (SS5: interval overlaps decision boundaries
      or evidence is insufficient -> INCONCLUSIVE; maps to
      ``RequirementOutcome.INCONCLUSIVE``).

    The vocabulary deliberately contains **no** ``REPRODUCED`` member
    (DEV-M9-G03 AC-01): statistics decides, the outcome layer closes, and
    "no significant difference" (an interval crossing zero without being
    inside the bounds) is INCONCLUSIVE -- never EQUIVALENT. The
    ``INCONCLUSIVE`` value is the frozen outcome vocabulary's string
    value.
    """

    EQUIVALENT = "EQUIVALENT"
    NOT_EQUIVALENT = "NOT_EQUIVALENT"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class EquivalenceBounds:
    """The frozen equivalence region of an effect metric.

    ``lower`` / ``upper`` delimit the acceptance region in effect space
    (observed minus target). The region must have positive width
    (``upper > lower``): a zero-width or inverted region is a degenerate
    margin and is rejected. Bounds are pure inputs -- provided by the
    frozen Acceptance Criteria (AC-03, :func:`equivalence_bounds_from_acceptance`),
    never computed from the result.

    Raises:
        TypeError: ``lower`` / ``upper`` have the wrong type.
        InvalidStatisticInputError: a bound is non-finite, or
            ``upper <= lower``.
    """

    lower: float
    upper: float

    def __post_init__(self) -> None:
        _require_finite(self.lower, "EquivalenceBounds.lower")
        _require_finite(self.upper, "EquivalenceBounds.upper")
        if self.upper <= self.lower:
            raise InvalidStatisticInputError(
                "EquivalenceBounds.upper must be strictly greater than"
                f" lower, got ({self.lower}, {self.upper})"
            )


@dataclass(frozen=True)
class EquivalenceDecisionInput:
    """The exact inputs an equivalence decision is a pure function of.

    ``effect`` is the point estimate of the reproduction effect;
    ``ci`` is its uncertainty interval (the decision evidence);
    ``bounds`` are the frozen equivalence region (AC-03). Frozen and
    hashable so "same inputs -> same decision" is directly testable and
    every assessment records its exact inputs (auditability).

    Raises:
        TypeError: a field has the wrong type.
        InvalidStatisticInputError: ``effect`` is non-finite.
    """

    effect: float
    ci: ConfidenceInterval
    bounds: EquivalenceBounds

    def __post_init__(self) -> None:
        _require_finite(self.effect, "EquivalenceDecisionInput.effect")
        if not isinstance(self.ci, ConfidenceInterval):
            raise TypeError(
                "EquivalenceDecisionInput.ci must be a ConfidenceInterval,"
                f" got {type(self.ci).__name__}"
            )
        if not isinstance(self.bounds, EquivalenceBounds):
            raise TypeError(
                "EquivalenceDecisionInput.bounds must be an"
                f" EquivalenceBounds, got {type(self.bounds).__name__}"
            )


@dataclass(frozen=True)
class EquivalenceDecisionRule:
    """One entry of the ordered equivalence-decision rule table."""

    rule_id: str
    description: str
    verdict: EquivalenceVerdict
    predicate: Callable[[EquivalenceDecisionInput], bool]


@dataclass(frozen=True)
class EquivalenceRuleDecision:
    """Record of one equivalence-rule evaluation (audit trail)."""

    rule_id: str
    description: str
    verdict: EquivalenceVerdict
    matched: bool


@dataclass(frozen=True)
class EquivalenceAssessment:
    """Full, auditable result of one equivalence decision.

    ``input`` is the exact decision input; ``verdict`` is the decided
    :class:`EquivalenceVerdict`; ``decisions`` records the outcome of
    every rule in the table (in evaluation order); ``matched_rule_id``
    names the deciding rule (``None`` is impossible: the trailing total
    default always matches); ``ruleset_version`` records the rule table
    version (``RULESET_VERSION``). No Requirement state is read or
    written: the verdict only *informs* the outcome layer.
    """

    ruleset_version: str
    input: EquivalenceDecisionInput
    verdict: EquivalenceVerdict
    decisions: tuple[EquivalenceRuleDecision, ...]
    matched_rule_id: str

    @property
    def equivalent(self) -> bool:
        """True exactly when both TOST comparisons rejected non-equivalence."""
        return self.verdict is EquivalenceVerdict.EQUIVALENT


#: The ordered equivalence-decision rule table. First match wins; order is
#: normative. Predicates are pure functions of the
#: :class:`EquivalenceDecisionInput` only, and ``R-EQ-3`` is the total
#: default so every interval is decided. The comparisons are point-
#: membership conventions: an interval is EQUIVALENT when *every* point
#: lies strictly inside the region, NOT_EQUIVALENT when *no* point lies
#: inside the region (the interval is wholly on one side), and
#: INCONCLUSIVE otherwise (at least one point inside but not all --
#: overlapping a decision boundary, AC-02).
EQUIVALENCE_DECISION_RULES: tuple[EquivalenceDecisionRule, ...] = (
    EquivalenceDecisionRule(
        rule_id="R-EQ-1",
        description=(
            "the confidence interval lies entirely inside the frozen"
            " equivalence bounds: both one-sided comparisons reject"
            " non-equivalence, the effect is equivalent"
            " (07-STATISTICS-AND-ACCEPTANCE.md SS5: uncertainty interval"
            " sufficiently inside the region -> PASS)"
        ),
        verdict=EquivalenceVerdict.EQUIVALENT,
        predicate=lambda i: (
            i.ci.lower > i.bounds.lower and i.ci.upper < i.bounds.upper
        ),
    ),
    EquivalenceDecisionRule(
        rule_id="R-EQ-2",
        description=(
            "the confidence interval lies entirely on one side of the"
            " equivalence region (no point inside the bounds): evidence of"
            " non-equivalence (07-STATISTICS-AND-ACCEPTANCE.md SS5:"
            " uncertainty interval sufficiently outside the region /"
            " evidence of non-equivalence -> FAIL)"
        ),
        verdict=EquivalenceVerdict.NOT_EQUIVALENT,
        predicate=lambda i: (
            i.ci.upper <= i.bounds.lower or i.ci.lower >= i.bounds.upper
        ),
    ),
    EquivalenceDecisionRule(
        rule_id="R-EQ-3",
        description=(
            "the confidence interval overlaps at least one equivalence"
            " bound or is wider than the region: the interval overlaps"
            " decision boundaries / evidence is insufficient, the decision"
            " is INCONCLUSIVE -- never EQUIVALENT, never REPRODUCED"
            " (07-STATISTICS-AND-ACCEPTANCE.md SS5 -> INCONCLUSIVE;"
            " DEV-M9-G03 AC-01/AC-02: p > 0.05 alone, without the interval"
            " fully inside the frozen bounds, can never establish"
            " equivalence; total default)"
        ),
        verdict=EquivalenceVerdict.INCONCLUSIVE,
        predicate=lambda i: True,
    ),
)


def decide_equivalence(
    effect: float,
    ci: ConfidenceInterval,
    bounds: EquivalenceBounds,
) -> EquivalenceAssessment:
    """Decide the equivalence verdict of one effect metric (TOST-style).

    Pure and deterministic: the verdict is a pure function of the effect
    estimate, its confidence interval and the frozen equivalence bounds
    (``EQUIVALENCE_DECISION_RULES``, first match wins, trailing total
    default). The point estimate alone never decides -- the interval
    carries the evidence. No p-value is accepted anywhere on this path,
    so a non-significant result (an interval crossing zero) can never
    yield EQUIVALENT unless the interval lies entirely inside the frozen
    bounds (DEV-M9-G03 AC-01 by construction). An interval overlapping
    at least one bound -- including intervals wider than the region -- is
    INCONCLUSIVE (AC-02). Nothing is persisted and no Requirement state
    is touched: the outcome layer closes outcomes from this verdict.

    Args:
        effect: the point estimate of the reproduction effect.
        ci: the uncertainty interval of the effect (its CI or one built
            from its SE via the CI utilities).
        bounds: the frozen equivalence region (AC-03).

    Returns:
        The full :class:`EquivalenceAssessment` (verdict plus the
        auditable rule trace).

    Raises:
        TypeError: an argument has the wrong type.
        InvalidStatisticInputError: ``effect`` is non-finite.
    """
    if not isinstance(ci, ConfidenceInterval):
        raise TypeError(
            f"ci must be a ConfidenceInterval, got {type(ci).__name__}"
        )
    if not isinstance(bounds, EquivalenceBounds):
        raise TypeError(
            f"bounds must be an EquivalenceBounds, got {type(bounds).__name__}"
        )
    decision_input = EquivalenceDecisionInput(effect=effect, ci=ci, bounds=bounds)
    decisions: list[EquivalenceRuleDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = EquivalenceVerdict.INCONCLUSIVE  # unreachable default
    for rule in EQUIVALENCE_DECISION_RULES:
        hit = rule.predicate(decision_input)
        decisions.append(
            EquivalenceRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                matched=hit,
            )
        )
        if hit and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
    # R-EQ-3 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return EquivalenceAssessment(
        ruleset_version=RULESET_VERSION,
        input=decision_input,
        verdict=matched_verdict,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# AC-03: equivalence margins are inputs from frozen Acceptance Criteria
# ---------------------------------------------------------------------------


def equivalence_bounds_from_acceptance(
    acceptance: AcceptanceCriteria,
) -> EquivalenceBounds:
    """Extract the equivalence region from a frozen acceptance record.

    AC-03: the equivalence margins are **inputs** from the registered,
    frozen ``AcceptanceCriteria`` record -- never inferred from the
    observed result. The record's ``criteria`` entries are scanned for a
    numeric ``margin`` (the equivalence half-width around the published
    target); in effect space (observed minus target) the region is the
    symmetric ``[-margin, +margin]``. Entries without a ``margin`` (e.g.
    replication-design criteria) are ignored. The function is a pure
    function of the record: it cannot see the result, and its return is
    byte-for-byte the stored margin.

    Raises:
        TypeError: ``acceptance`` is not an ``AcceptanceCriteria``.
        EquivalenceMarginError: the record carries no numeric ``margin``
            (a ``margin`` value that is not a positive finite number is
            rejected, and several **differing** margins are ambiguous and
            rejected).
    """
    if not isinstance(acceptance, AcceptanceCriteria):
        raise TypeError(
            "equivalence_bounds_from_acceptance expects an AcceptanceCriteria,"
            f" got {type(acceptance).__name__}"
        )
    margins: set[float] = set()
    for entry in acceptance.criteria:
        if not isinstance(entry, dict) or "margin" not in entry:
            continue
        margin = entry["margin"]
        if isinstance(margin, bool) or not isinstance(margin, (int, float)):
            raise EquivalenceMarginError(
                "equivalence margin must be a positive finite number in the"
                f" frozen acceptance criteria, got {margin!r} (AC-03)"
            )
        value = float(margin)
        if not math.isfinite(value) or value <= 0:
            raise EquivalenceMarginError(
                "equivalence margin must be a positive finite number in the"
                f" frozen acceptance criteria, got {margin!r} (AC-03)"
            )
        margins.add(value)
    if not margins:
        raise EquivalenceMarginError(
            "no equivalence margin in the frozen acceptance criteria: a"
            " numeric 'margin' entry is required; margins are never inferred"
            " from the result (AC-03)"
        )
    if len(margins) > 1:
        raise EquivalenceMarginError(
            "ambiguous equivalence margins in the frozen acceptance criteria:"
            f" {', '.join(str(m) for m in sorted(margins))}; exactly one"
            " margin is required (AC-03)"
        )
    margin = margins.pop()
    return EquivalenceBounds(lower=-margin, upper=margin)


# ---------------------------------------------------------------------------
# Deliverable 3: effect/uncertainty reporting hooks
# ---------------------------------------------------------------------------

#: The uncertainty method name reported by :func:`uncertainty_report`
#: (consumes the same vocabulary as ``ResultRecord.uncertainty``).
_UNCERTAINTY_METHOD: str = "confidence_interval"


def uncertainty_report(ci: ConfidenceInterval) -> dict[str, Any]:
    """Build the uncertainty payload of a decision for a result record.

    The hook producing the ``uncertainty`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.uncertainty`` -- a dict): method,
    two-sided confidence level, interval endpoints and (when known) the
    standard error. Deterministic: a pure function of the interval.

    Raises:
        TypeError: ``ci`` is not a ``ConfidenceInterval``.
    """
    if not isinstance(ci, ConfidenceInterval):
        raise TypeError(
            f"ci must be a ConfidenceInterval, got {type(ci).__name__}"
        )
    report: dict[str, Any] = {
        "method": _UNCERTAINTY_METHOD,
        "confidence_level": ci.confidence_level,
        "lower": ci.lower,
        "upper": ci.upper,
    }
    if ci.standard_error is not None:
        report["standard_error"] = ci.standard_error
    return report


def effect_metrics(
    metric_name: str, effect: float, ci: ConfidenceInterval
) -> list[dict[str, Any]]:
    """Build the derived-metric entries of an effect for a result record.

    The hook producing the ``metrics`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.metrics`` -- a list of
    ``{"metric": ..., "value": ...}`` dicts): the effect point estimate
    and its confidence interval endpoints, in deterministic order.

    Raises:
        TypeError: ``metric_name`` is not a str, or ``ci`` is not a
            ``ConfidenceInterval``.
        InvalidStatisticInputError: ``metric_name`` is empty/blank, or
            ``effect`` is non-finite.
    """
    if not isinstance(metric_name, str):
        raise TypeError(
            f"metric_name must be a str, got {type(metric_name).__name__}"
        )
    if not metric_name.strip():
        raise InvalidStatisticInputError(
            f"metric_name must be a non-empty string, got {metric_name!r}"
        )
    if not isinstance(ci, ConfidenceInterval):
        raise TypeError(
            f"ci must be a ConfidenceInterval, got {type(ci).__name__}"
        )
    number = _require_finite(effect, "effect")
    return [
        {"metric": metric_name, "value": number},
        {"metric": f"{metric_name}_ci_lower", "value": ci.lower},
        {"metric": f"{metric_name}_ci_upper", "value": ci.upper},
    ]
