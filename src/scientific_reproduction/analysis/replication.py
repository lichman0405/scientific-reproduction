"""Replicate sufficiency evaluation and additional-run decision hook (DEV-M9-G04).

Implements the **replicate sufficiency evaluator** deliverable of
DEV-M9-G04 over the frozen ``core.models`` acceptance vocabulary and
the DEV-M9-G02 result record shapes (``analysis/results.py``),
grounded in:

* ``07-STATISTICS-AND-ACCEPTANCE.md`` SS2 (independent replication:
  experimental Goals require independent replicates by default, the
  default floor is ``n >= 3``, and the final n is dynamically designed
  from expected variability / required confidence / precision; technical
  replicates and instrument repeats are additional evidence but cannot
  replace independent replication); SS5 (three-way result state -- the
  outcome layer's ``PASS``/``FAIL``/``INCONCLUSIVE`` is fed, never
  decided, by statistics); SS6 (the Supervisor should dynamically add
  Runs when the uncertainty interval is too wide for the frozen decision
  rule); SS8 (every numeric threshold must record its basis -- the
  precision threshold is a frozen input, never inferred from the
  observed result); SS9 (the replication design is frozen before data
  are generated);
* ``12-ANALYSIS-SUBSYSTEM.md`` SS5 (the Analysis Result Package carries
  QC findings, derived metrics and uncertainty/statistics);
* ``20-ARCHITECTURE-DECISIONS.md`` items 9-10 (independent experimental
  replication mandatory with default floor ``n >= 3``, final n
  dynamically designed; technical/instrument repeats cannot replace
  independent replication) and 13-14 (statistics must support
  equivalence/uncertainty; the results layer feeds the outcome layer and
  never closes outcomes by itself).

AC-01 -- independent vs technical/instrument replicates distinguished
--------------------------------------------------------------------
:func:`evaluate_replicate_sufficiency` takes the replicate grouping as
**input** (:class:`ReplicateDecisionInput` -- ``independent`` values
from independent runs and ``technical`` values from technical/
instrument repeats) and treats the groups differently: only the
independent values carry statistical weight (the mean, the standard
deviation and the precision of the mean are computed from them alone),
while technical repeats are recorded (``technical_n``), reported and
never used -- a technical-only count can never satisfy the independent-n
floor, and no replicate type is ever guessed from the values.

AC-02 -- default n>=3 floor enforceable for experimental Goals
--------------------------------------------------------------
The evaluator carries the documented ``DEFAULT_MIN_INDEPENDENT = 3``
floor. The floor can be overridden only through the frozen acceptance
criteria (:func:`replicate_criterion_from_acceptance` reads a numeric
``min_independent`` entry verbatim, exactly like the DEV-M9-G03 margin;
a record without one falls back to the default 3) and an override can
never weaken the floor below 1: non-numeric, non-integer, non-positive
and ambiguous (several differing) overrides are rejected with stable
:class:`ReplicateCriterionError` messages. The floor is also enforced
at the evaluator boundary (``min_independent`` must be an int >= 1).

AC-03 -- insufficient precision yields an additional-run request, never forced PASS/FAIL
-----------------------------------------------------------------------------------------
The status vocabulary of the assessment (:class:`ReplicateStatus`) is
``SUFFICIENT`` / ``INSUFFICIENT`` / ``INDETERMINATE`` -- deliberately
no ``PASS``/``FAIL``/``REPRODUCED`` member, and no collision with the
frozen outcome vocabulary strings (``RequirementOutcome`` carries
``REPRODUCED``/``NOT_REPRODUCED``/``INCONCLUSIVE``, so the inconclusive-
analog member is named ``INDETERMINATE``). No Requirement state is ever
read or written. The assessment carries an explicit additional-run
request shape -- ``requested_additional_runs`` -- whenever the criteria
are not met:

* ``SUFFICIENT`` -- the independent replicate count meets the floor
  and the z-based relative half-width of the mean
  (``z * se / |mean|``, stdlib ``statistics.NormalDist`` only -- no
  scipy, no numpy) is within the frozen precision threshold; no
  additional runs are requested;
* ``INSUFFICIENT`` -- the count meets the floor but the relative
  half-width exceeds the frozen precision threshold: the precision is
  insufficient, and additional independent runs are requested
  (``n * (h / threshold) ** 2`` scaled to bring the half-width within
  the threshold, per-rule determinism like DEV-M9-G03);
* ``INDETERMINATE`` -- the independent replicate count is below the
  floor (or below two, so no sample standard deviation is computable):
  no statistical sufficiency determination is possible yet, and runs
  are requested to reach the floor. The outcome layer maps these
  states onto its own vocabulary; this module never closes an outcome.

The decision runs through the ordered, versioned
``REPLICATION_DECISION_RULES`` rule table (first match wins, trailing
total default, ``matched_rule_id`` never ``None`` with a post-assert).
Degenerate inputs -- fewer than one independent replicate, non-finite
values, a zero independent mean (the relative-half-width criterion is
undefined), non-positive precision thresholds, floors below 1 and
confidence levels outside ``(0, 1)`` -- are rejected up front with
stable one-line :class:`InvalidReplicateInputError` messages instead of
silently changing a decision.

Reporting hooks (AC-03)
-----------------------
:func:`sufficiency_metrics` / :func:`sufficiency_uncertainty_payload` /
:func:`sufficiency_findings` produce the exact shapes the DEV-M9-G02
``ResultRecord`` consumes (``metrics`` -- a list of
``{"metric": ..., "value": ...}`` dicts; ``uncertainty`` -- a dict;
``qc_findings`` -- a list of strings), and
:func:`validate_replication_mode` ties the output to the frozen
``DecisionMode`` vocabulary: the frozen model has **no** ``REPLICATION``
member, so the hook consumes the frozen vocabulary as-is -- it serves
the quantitative experimental modes (``EQUIVALENCE``,
``BOUNDED_INTERVAL``; ``SUPPORTED_REPLICATION_DECISION_MODES``) and
rejects every other mode with a stable :class:`UnsupportedDecisionModeError`
instead of inventing a mode.

Determinism and boundaries
--------------------------
All hooks are pure and deterministic: same inputs -> same assessment,
on every platform; no randomness, no wall clock, no network, no
persistence (the evaluator validates and reports, nothing is written).
``TypeError`` at the public boundaries; value violations follow the
``ValueError``-subclass convention with stable messages;
``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import NormalDist, fmean, stdev
from typing import Any, Callable

from scientific_reproduction.core.models import AcceptanceCriteria, DecisionMode

__all__ = [
    "DEFAULT_MIN_INDEPENDENT",
    "DEFAULT_REPLICATION_CONFIDENCE_LEVEL",
    "REPLICATION_RULESET_VERSION",
    "SUPPORTED_REPLICATION_DECISION_MODES",
    # errors
    "ReplicateAnalysisError",
    "InvalidReplicateInputError",
    "ReplicateCriterionError",
    "UnsupportedDecisionModeError",
    # AC-01/AC-02/AC-03: replicate sufficiency evaluator
    "ReplicateStatus",
    "ReplicateCriterion",
    "ReplicateDecisionInput",
    "ReplicateRule",
    "ReplicateRuleDecision",
    "ReplicateState",
    "ReplicateSufficiencyAssessment",
    "REPLICATION_DECISION_RULES",
    "evaluate_replicate_sufficiency",
    # AC-02/AC-03: frozen acceptance criteria
    "replicate_criterion_from_acceptance",
    "validate_replication_mode",
    # AC-03: reporting hooks feeding the Supervisor acceptance path
    "sufficiency_findings",
    "sufficiency_metrics",
    "sufficiency_uncertainty_payload",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ReplicateAnalysisError(ValueError):
    """Base class for all replicate sufficiency evaluation errors."""


class InvalidReplicateInputError(ReplicateAnalysisError):
    """Raised when a replicate evaluation input is degenerate.

    Covers fewer than one independent replicate, non-finite values, a
    zero independent mean (the relative-half-width criterion is
    undefined for it), a precision threshold that is not a finite
    positive number, floors below 1 and confidence levels outside
    ``(0, 1)``. A degenerate input cannot silently change a sufficiency
    decision, so it is rejected up front with a stable message.
    """


class ReplicateCriterionError(ReplicateAnalysisError):
    """Raised when the frozen acceptance criteria carry no usable criterion.

    AC-02/AC-03: the minimum independent floor and the precision
    threshold are **inputs** from the frozen Acceptance Criteria -- a
    ``min_independent`` override that is not a positive integer, or a
    missing/non-numeric/non-positive/ambiguous ``precision`` entry,
    cannot provide the replication criterion and is rejected instead of
    falling back to anything result-derived.
    """


class UnsupportedDecisionModeError(ReplicateAnalysisError):
    """Raised when an acceptance record uses a mode the hook does not serve.

    AC-03: the frozen ``core.models`` vocabulary has **no** ``REPLICATION``
    member, so the replication hook consumes the frozen modes as-is: it
    serves the quantitative experimental modes (``SUPPORTED_REPLICATION_DECISION_MODES``)
    and rejects every other declared mode with a stable error instead of
    inventing one.
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The default minimum number of independent replicates of an
#: experimental Goal (``07-STATISTICS-AND-ACCEPTANCE.md`` SS2: "Default
#: floor: n >= 3"; ``20-ARCHITECTURE-DECISIONS.md`` item 9). Used when
#: the frozen acceptance criteria carry no ``min_independent`` entry
#: (AC-02).
DEFAULT_MIN_INDEPENDENT: int = 3

#: The two-sided confidence level of the mean precision interval by
#: default.
DEFAULT_REPLICATION_CONFIDENCE_LEVEL: float = 0.95

#: Version of the replicate-sufficiency rule table. Bumped whenever a
#: rule changes; recorded in every assessment so old assessments stay
#: interpretable.
REPLICATION_RULESET_VERSION: str = "1.0"

#: The frozen quantitative experimental decision modes the replication
#: hook feeds into (``core/models.py`` ``DecisionMode``). The frozen
#: vocabulary has no ``REPLICATION`` member, so the hook names the modes
#: it serves and rejects everything else (AC-03).
SUPPORTED_REPLICATION_DECISION_MODES: tuple[DecisionMode, ...] = (
    DecisionMode.EQUIVALENCE,
    DecisionMode.BOUNDED_INTERVAL,
)

#: The uncertainty method name reported by
#: :func:`sufficiency_uncertainty_payload` (consumes the same vocabulary
#: as ``ResultRecord.uncertainty``).
_UNCERTAINTY_METHOD: str = "confidence_interval"


def _require_number(value: Any, label: str) -> float:
    """Reject non-numeric values at the numeric boundaries (TypeError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number, got {type(value).__name__}")
    return float(value)


def _require_finite(value: Any, label: str) -> float:
    """Reject non-finite numeric values (stable InvalidReplicateInputError)."""
    number = _require_number(value, label)
    if not math.isfinite(number):
        raise InvalidReplicateInputError(
            f"{label} must be a finite number, got {value!r}"
        )
    return number


def _require_confidence_level(value: Any, label: str) -> float:
    """Reject confidence levels outside the open interval (0, 1)."""
    number = _require_finite(value, label)
    if not 0.0 < number < 1.0:
        raise InvalidReplicateInputError(
            f"{label} must be strictly between 0 and 1, got {value!r}"
        )
    return number


def _require_precision_threshold(value: Any, label: str) -> float:
    """Reject precision thresholds that are not finite positive numbers."""
    number = _require_finite(value, label)
    if number <= 0:
        raise InvalidReplicateInputError(
            f"{label} must be a finite positive number, got {value!r}"
        )
    return number


def _require_min_independent(value: Any, label: str) -> int:
    """Reject minimum-independent floors that are not ints of at least 1.

    AC-02: the floor is a replicate count; booleans and non-ints are
    type violations, and an override below 1 (silently weakening the
    floor to zero or negative) is a value violation that is always
    rejected.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an int, got {type(value).__name__}")
    if value < 1:
        raise InvalidReplicateInputError(
            f"{label} must be at least 1, got {value!r}"
        )
    return value


def _z_critical_value(confidence_level: float) -> float:
    """The two-sided standard-normal critical value of a level in (0, 1)."""
    return NormalDist().inv_cdf((1.0 + confidence_level) / 2.0)


# ---------------------------------------------------------------------------
# AC-01: the replicate grouping is input, never guessed from values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicateDecisionInput:
    """The observed replicate grouping and the frozen evaluation inputs.

    ``independent`` holds the measured values of the **independent**
    runs of the Goal (they carry the statistical weight); ``technical``
    holds the measured values of technical/instrument repeats (recorded
    and reported, but they never count toward the independent-n floor
    and never enter the precision computation -- AC-01). The grouping
    is **input** by the caller: no replicate type is ever guessed from
    the values. ``min_independent`` is the independent-n floor
    (``DEFAULT_MIN_INDEPENDENT`` = 3 unless overridden by the frozen
    acceptance criteria, AC-02) and ``precision_threshold`` is the
    frozen relative-half-width threshold of the mean precision
    criterion (AC-03). The series are stored as tuple copies, so later
    mutation of the caller's lists cannot change the assessment.

    Raises:
        TypeError: a field has the wrong type (the series must be
            tuples/lists of numbers; ``min_independent`` an int;
            ``precision_threshold`` a number; booleans are rejected as
            numbers).
        InvalidReplicateInputError: the independent series is empty, a
            value is non-finite, ``min_independent`` is below 1, the
            precision threshold is not a finite positive number, or the
            confidence level is outside ``(0, 1)``.
    """

    independent: Sequence[float]
    precision_threshold: float
    technical: Sequence[float] = ()
    confidence_level: float = DEFAULT_REPLICATION_CONFIDENCE_LEVEL
    min_independent: int = DEFAULT_MIN_INDEPENDENT

    def __post_init__(self) -> None:
        if not isinstance(self.independent, (tuple, list)):
            raise TypeError(
                "independent must be a tuple or list of numbers, got"
                f" {type(self.independent).__name__}"
            )
        independent = tuple(
            _require_number(value, f"independent replicate {index}")
            for index, value in enumerate(self.independent)
        )
        if not independent:
            raise InvalidReplicateInputError(
                "replicate sufficiency requires at least one independent"
                " replicate; technical/instrument repeats alone never"
                " satisfy the independent-n floor (AC-01)"
            )
        object.__setattr__(self, "independent", independent)
        for index, value in enumerate(independent):
            _require_finite(value, f"independent replicate {index}")
        if not isinstance(self.technical, (tuple, list)):
            raise TypeError(
                f"technical must be a tuple or list of numbers, got"
                f" {type(self.technical).__name__}"
            )
        technical = tuple(
            _require_number(value, f"technical replicate {index}")
            for index, value in enumerate(self.technical)
        )
        object.__setattr__(self, "technical", technical)
        for index, value in enumerate(technical):
            _require_finite(value, f"technical replicate {index}")
        _require_confidence_level(
            self.confidence_level, "ReplicateDecisionInput.confidence_level"
        )
        _require_min_independent(
            self.min_independent, "ReplicateDecisionInput.min_independent"
        )
        object.__setattr__(
            self,
            "precision_threshold",
            _require_precision_threshold(
                self.precision_threshold, "ReplicateDecisionInput.precision_threshold"
            ),
        )


# ---------------------------------------------------------------------------
# AC-02/AC-03: the frozen replication criterion of an acceptance record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicateCriterion:
    """The frozen replication criterion of an acceptance record (AC-02/03).

    ``min_independent`` is the independent-n floor
    (``DEFAULT_MIN_INDEPENDENT`` when the record carries no override)
    and ``precision_threshold`` is the frozen relative-half-width
    threshold. Both are **inputs** from the registered, frozen
    Acceptance Criteria -- never inferred from the observed result.
    Frozen and hashable so "same record -> same criterion" is directly
    testable.

    Raises:
        TypeError: ``min_independent`` is not an int, or
            ``precision_threshold`` is not a number.
        ReplicateCriterionError: ``min_independent`` is below 1, or
            ``precision_threshold`` is not a finite positive number.
    """

    min_independent: int
    precision_threshold: float

    def __post_init__(self) -> None:
        _require_min_independent(
            self.min_independent, "ReplicateCriterion.min_independent"
        )
        value = _require_precision_threshold(
            self.precision_threshold, "ReplicateCriterion.precision_threshold"
        )
        object.__setattr__(self, "precision_threshold", value)


def replicate_criterion_from_acceptance(
    acceptance: AcceptanceCriteria,
) -> ReplicateCriterion:
    """Extract the replication criterion from a frozen acceptance record.

    AC-02/AC-03: the independent-n floor and the precision threshold are
    **inputs** from the registered, frozen ``AcceptanceCriteria`` record
    -- never inferred from the observed result. The record's ``criteria``
    entries are scanned for a numeric ``min_independent`` (the floor
    override; absent -> ``DEFAULT_MIN_INDEPENDENT`` = 3, the default
    floor of ``07-...`` SS2) and for a numeric ``precision`` (the frozen
    relative-half-width threshold of the mean; **required** -- the
    precision criterion is the analog of the DEV-M9-G03 margin). Entries
    without those keys (e.g. equivalence-margin entries) are ignored. The
    function is a pure function of the record: it cannot see the result,
    and its return is byte-for-byte the stored values.

    Raises:
        TypeError: ``acceptance`` is not an ``AcceptanceCriteria``.
        ReplicateCriterionError: a ``min_independent`` override is not a
            positive integer, a ``precision`` entry is missing, not a
            positive finite number, or several **differing** values of
            either key are ambiguous.
    """
    if not isinstance(acceptance, AcceptanceCriteria):
        raise TypeError(
            "replicate_criterion_from_acceptance expects an AcceptanceCriteria,"
            f" got {type(acceptance).__name__}"
        )
    floors: set[int] = set()
    thresholds: set[float] = set()
    for entry in acceptance.criteria:
        if not isinstance(entry, dict):
            continue
        if "min_independent" in entry:
            value = entry["min_independent"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ReplicateCriterionError(
                    "min_independent must be a positive integer in the frozen"
                    f" acceptance criteria, got {value!r} (AC-02)"
                )
            if value < 1:
                raise ReplicateCriterionError(
                    "the independent replicate floor must not be weakened"
                    f" below 1, got {value!r} in the frozen acceptance"
                    " criteria (AC-02)"
                )
            floors.add(value)
        if "precision" in entry:
            value = entry["precision"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ReplicateCriterionError(
                    "precision must be a finite positive number in the frozen"
                    f" acceptance criteria, got {value!r} (AC-03)"
                )
            number = float(value)
            if not math.isfinite(number) or number <= 0:
                raise ReplicateCriterionError(
                    "precision must be a finite positive number in the frozen"
                    f" acceptance criteria, got {value!r} (AC-03)"
                )
            thresholds.add(number)
    if len(floors) > 1:
        raise ReplicateCriterionError(
            "ambiguous min_independent overrides in the frozen acceptance"
            f" criteria: {', '.join(str(f) for f in sorted(floors))}; exactly"
            " one floor is required (AC-02)"
        )
    if not thresholds:
        raise ReplicateCriterionError(
            "no precision criterion in the frozen acceptance criteria: a"
            " numeric 'precision' entry is required; the precision threshold"
            " is a frozen input, never inferred from the result (AC-03)"
        )
    if len(thresholds) > 1:
        raise ReplicateCriterionError(
            "ambiguous precision values in the frozen acceptance criteria:"
            f" {', '.join(str(t) for t in sorted(thresholds))}; exactly one"
            " precision threshold is required (AC-03)"
        )
    floor = floors.pop() if floors else DEFAULT_MIN_INDEPENDENT
    return ReplicateCriterion(
        min_independent=floor, precision_threshold=thresholds.pop()
    )


def validate_replication_mode(acceptance: AcceptanceCriteria) -> None:
    """Reject acceptance records that cannot consume replicate sufficiency.

    AC-03: the frozen ``core.models`` vocabulary has **no** ``REPLICATION``
    member, so the replication hook consumes the frozen ``DecisionMode``
    vocabulary as-is: its output feeds Supervisor acceptance under the
    quantitative experimental modes (``SUPPORTED_REPLICATION_DECISION_MODES``
    -- ``EQUIVALENCE`` and ``BOUNDED_INTERVAL``) only. An acceptance
    record declaring any other frozen mode (e.g. ``CONVERGENCE``, the
    computational mode of DEV-M9-G05, or ``CUSTOM``) is rejected with a
    stable :class:`UnsupportedDecisionModeError`; a matching record
    passes silently. The hook only checks the frozen mode -- it never
    decides acceptance itself.

    Raises:
        TypeError: ``acceptance`` is not an ``AcceptanceCriteria``.
        UnsupportedDecisionModeError: the record's ``decision_mode`` is
            not one of ``SUPPORTED_REPLICATION_DECISION_MODES``.
    """
    if not isinstance(acceptance, AcceptanceCriteria):
        raise TypeError(
            f"validate_replication_mode expects an AcceptanceCriteria, got"
            f" {type(acceptance).__name__}"
        )
    if acceptance.decision_mode not in SUPPORTED_REPLICATION_DECISION_MODES:
        supported = ", ".join(
            mode.value for mode in SUPPORTED_REPLICATION_DECISION_MODES
        )
        raise UnsupportedDecisionModeError(
            "replicate sufficiency output feeds acceptance under the"
            f" quantitative experimental decision modes ({supported}) only;"
            f" acceptance {acceptance.acceptance_id!r} declares decision_mode"
            f" {acceptance.decision_mode.value!r} -- the frozen vocabulary has"
            " no REPLICATION mode, and this hook never invents one (AC-03)"
        )


# ---------------------------------------------------------------------------
# AC-01/AC-02/AC-03: the replicate sufficiency evaluator
# ---------------------------------------------------------------------------


class ReplicateStatus(StrEnum):
    """The replicate sufficiency state of an observed replicate group.

    * ``SUFFICIENT`` -- the independent replicate count meets the frozen
      minimum floor and the z-based relative half-width of the mean is
      within the frozen precision threshold: the replicate evidence is
      sufficient, no additional runs are requested.
    * ``INSUFFICIENT`` -- the count meets the floor but the relative
      half-width exceeds the frozen precision threshold: the precision
      is insufficient (``07-...`` SS5: evidence insufficient; SS6: add
      Runs when the interval is too wide) and additional independent
      runs are requested.
    * ``INDETERMINATE`` -- the independent replicate count is below the
      floor (or below two, so no sample standard deviation is
      computable): a statistical sufficiency determination is not
      possible yet and additional runs are requested to reach the floor.

    This is a report of the **evidence**, never an acceptance decision:
    the vocabulary deliberately contains no ``PASS``/``FAIL``/
    ``REPRODUCED`` member, and no member collides with the frozen
    outcome vocabulary strings (``RequirementOutcome`` carries
    ``REPRODUCED``/``NOT_REPRODUCED``/``INCONCLUSIVE`` -- the
    inconclusive-analog member is named ``INDETERMINATE``). Requirement
    closure stays with the Supervisor/acceptance layer (AC-03).
    """

    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT = "INSUFFICIENT"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class ReplicateRule:
    """One entry of the ordered replicate-sufficiency rule table."""

    rule_id: str
    description: str
    status: ReplicateStatus
    predicate: Callable[[ReplicateState], bool]


@dataclass(frozen=True)
class ReplicateRuleDecision:
    """Record of one replicate-rule evaluation (audit trail)."""

    rule_id: str
    description: str
    status: ReplicateStatus
    matched: bool


@dataclass(frozen=True)
class ReplicateState:
    """The observed grouping and the precision measures of the decision.

    ``mean`` is the mean of the **independent** values (technical
    repeats never enter the statistics -- AC-01); ``standard_deviation``
    / ``standard_error`` / ``half_width`` / ``relative_half_width`` are
    the sample standard deviation, its standard error, the z-based
    half-width of the mean at the input confidence level and that
    half-width relative to ``abs(mean)`` (all ``None`` when fewer than
    two independent replicates exist, so no sample standard deviation is
    computable). All measures are pure functions of the input, computed
    with the stdlib ``statistics`` module only.

    Raises:
        TypeError: a field has the wrong type.
        InvalidReplicateInputError: a measure is non-finite, the
            deviation measures are not ``None`` while fewer than two
            independent replicates exist, or the relative half-width is
            not ``None`` while the standard error is.
    """

    input: ReplicateDecisionInput
    mean: float
    standard_deviation: float | None
    standard_error: float | None
    half_width: float | None
    relative_half_width: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.input, ReplicateDecisionInput):
            raise TypeError(
                "ReplicateState.input must be a ReplicateDecisionInput, got"
                f" {type(self.input).__name__}"
            )
        _require_finite(self.mean, "ReplicateState.mean")
        for label in (
            "ReplicateState.standard_deviation",
            "ReplicateState.standard_error",
            "ReplicateState.half_width",
            "ReplicateState.relative_half_width",
        ):
            value = getattr(self, label.rsplit(".", 1)[1])
            if value is not None:
                _require_finite(value, label)
        if len(self.input.independent) < 2:
            if any(
                measure is not None
                for measure in (
                    self.standard_deviation,
                    self.standard_error,
                    self.half_width,
                    self.relative_half_width,
                )
            ):
                raise InvalidReplicateInputError(
                    "ReplicateState measures must be None when fewer than"
                    " two independent replicates exist"
                )
        elif self.standard_deviation is None or self.standard_error is None:
            raise InvalidReplicateInputError(
                "ReplicateState.standard_deviation and standard_error are"
                " required when at least two independent replicates exist"
            )
        if self.relative_half_width is not None and self.standard_error is None:
            raise InvalidReplicateInputError(
                "ReplicateState.relative_half_width must be None when the"
                " standard error is None"
            )

    @property
    def independent_n(self) -> int:
        """The number of independent replicate values (the statistical n)."""
        return len(self.input.independent)

    @property
    def technical_n(self) -> int:
        """The number of technical/instrument repeat values (informational)."""
        return len(self.input.technical)


@dataclass(frozen=True)
class ReplicateSufficiencyAssessment:
    """Full, auditable result of one replicate sufficiency decision.

    ``input`` is the exact replicate grouping and frozen evaluation
    inputs; ``state`` the derived precision measures; ``status`` the
    decided :class:`ReplicateStatus`; ``decisions`` records the outcome
    of every rule in the table (in evaluation order); ``matched_rule_id``
    names the deciding rule (never ``None``: the trailing total default
    always matches); ``ruleset_version`` records the rule table version
    (``REPLICATION_RULESET_VERSION``); ``requested_additional_runs`` is
    the additional-run request shape (AC-03): ``0`` exactly when the
    status is ``SUFFICIENT``, at least ``1`` otherwise. No Requirement
    state is read or written: the assessment only *informs* the outcome
    layer.

    Raises:
        TypeError: a field has the wrong type.
        InvalidReplicateInputError: ``matched_rule_id`` does not name a
            recorded decision, the status does not match the matched
            decision, or ``requested_additional_runs`` contradicts the
            status (nonzero while SUFFICIENT, zero otherwise).
    """

    ruleset_version: str
    input: ReplicateDecisionInput
    state: ReplicateState
    status: ReplicateStatus
    decisions: tuple[ReplicateRuleDecision, ...]
    matched_rule_id: str
    requested_additional_runs: int

    def __post_init__(self) -> None:
        if not isinstance(self.ruleset_version, str):
            raise TypeError(
                "ReplicateSufficiencyAssessment.ruleset_version must be a str,"
                f" got {type(self.ruleset_version).__name__}"
            )
        if not isinstance(self.input, ReplicateDecisionInput):
            raise TypeError(
                "ReplicateSufficiencyAssessment.input must be a"
                f" ReplicateDecisionInput, got {type(self.input).__name__}"
            )
        if not isinstance(self.state, ReplicateState):
            raise TypeError(
                "ReplicateSufficiencyAssessment.state must be a ReplicateState,"
                f" got {type(self.state).__name__}"
            )
        if not isinstance(self.status, ReplicateStatus):
            raise TypeError(
                "ReplicateSufficiencyAssessment.status must be a"
                f" ReplicateStatus, got {type(self.status).__name__}"
            )
        if not isinstance(self.decisions, tuple) or not all(
            isinstance(decision, ReplicateRuleDecision)
            for decision in self.decisions
        ):
            raise TypeError(
                "ReplicateSufficiencyAssessment.decisions must be a tuple of"
                f" ReplicateRuleDecision, got {type(self.decisions).__name__}"
            )
        if not isinstance(self.matched_rule_id, str):
            raise TypeError(
                "ReplicateSufficiencyAssessment.matched_rule_id must be a str,"
                f" got {type(self.matched_rule_id).__name__}"
            )
        matched = next(
            (d for d in self.decisions if d.rule_id == self.matched_rule_id),
            None,
        )
        if matched is None:
            raise InvalidReplicateInputError(
                "ReplicateSufficiencyAssessment.matched_rule_id must name one"
                f" of the recorded rule decisions, got"
                f" {self.matched_rule_id!r}"
            )
        if not matched.matched or matched.status is not self.status:
            raise InvalidReplicateInputError(
                "ReplicateSufficiencyAssessment status must match the matched"
                f" rule decision ({self.matched_rule_id})"
            )
        if isinstance(self.requested_additional_runs, bool) or not isinstance(
            self.requested_additional_runs, int
        ):
            raise TypeError(
                "ReplicateSufficiencyAssessment.requested_additional_runs must"
                f" be an int, got {type(self.requested_additional_runs).__name__}"
            )
        if self.requested_additional_runs < 0:
            raise InvalidReplicateInputError(
                "ReplicateSufficiencyAssessment.requested_additional_runs must"
                f" not be negative, got {self.requested_additional_runs!r}"
            )
        if self.status is ReplicateStatus.SUFFICIENT:
            if self.requested_additional_runs != 0:
                raise InvalidReplicateInputError(
                    "a SUFFICIENT replicate assessment must not request"
                    f" additional runs, got {self.requested_additional_runs}"
                )
        elif self.requested_additional_runs < 1:
            raise InvalidReplicateInputError(
                "an assessment that is not SUFFICIENT must request at least"
                f" one additional run, got {self.requested_additional_runs}"
            )

    @property
    def sufficient(self) -> bool:
        """True exactly when the replicate evidence is SUFFICIENT."""
        return self.status is ReplicateStatus.SUFFICIENT

    @property
    def independent_n(self) -> int:
        """The number of independent replicate values (the statistical n)."""
        return self.state.independent_n

    @property
    def technical_n(self) -> int:
        """The number of technical/instrument repeat values (informational)."""
        return self.state.technical_n

    @property
    def relative_half_width(self) -> float | None:
        """The z-based relative half-width of the mean (None when n < 2)."""
        return self.state.relative_half_width

    @property
    def mean_interval(self) -> tuple[float | None, float | None]:
        """The z-based confidence interval of the mean ``(lower, upper)``.

        ``(None, None)`` when fewer than two independent replicates
        exist (no standard error is computable). Technical repeats never
        enter the interval (AC-01).
        """
        half_width = self.state.half_width
        if half_width is None:
            return None, None
        return self.state.mean - half_width, self.state.mean + half_width


#: The ordered replicate-sufficiency rule table. First match wins; order
#: is normative. Predicates are pure functions of the
#: :class:`ReplicateState` only, and ``R-REP-U1`` is the total default so
#: every replicate group is decided. The precision comparison needs two
#: independent replicates (a sample standard deviation); ``R-REP-S1`` /
#: ``R-REP-I1`` partition the states at/above the floor and the
#: ``max(floor, 2)`` independent-n gate, so everything below that gate
#: (including every group with a single independent replicate) falls to
#: the INDETERMINATE default -- never forced PASS/FAIL (AC-03).
REPLICATION_DECISION_RULES: tuple[ReplicateRule, ...] = (
    ReplicateRule(
        rule_id="R-REP-S1",
        description=(
            "the independent replicate count meets the frozen minimum floor"
            " and the z-based relative half-width of the mean is within the"
            " frozen precision threshold: the replicate evidence is"
            " sufficient, no additional runs are requested"
            " (07-STATISTICS-AND-ACCEPTANCE.md SS2: default floor n >= 3,"
            " final n designed from required precision; SS5: the uncertainty"
            " interval is narrow enough)"
        ),
        status=ReplicateStatus.SUFFICIENT,
        predicate=lambda s: (
            s.independent_n >= max(s.input.min_independent, 2)
            and s.relative_half_width is not None
            and s.relative_half_width <= s.input.precision_threshold
        ),
    ),
    ReplicateRule(
        rule_id="R-REP-I1",
        description=(
            "the independent replicate count meets the frozen minimum floor"
            " but the z-based relative half-width of the mean exceeds the"
            " frozen precision threshold: the precision is insufficient and"
            " additional independent runs are requested to bring the"
            " half-width within the threshold"
            " (07-STATISTICS-AND-ACCEPTANCE.md SS5: evidence insufficient;"
            " SS6: dynamically add Runs when the interval is too wide for the"
            " frozen decision rule)"
        ),
        status=ReplicateStatus.INSUFFICIENT,
        predicate=lambda s: (
            s.independent_n >= max(s.input.min_independent, 2)
            and s.relative_half_width is not None
            and s.relative_half_width > s.input.precision_threshold
        ),
    ),
    ReplicateRule(
        rule_id="R-REP-U1",
        description=(
            "the independent replicate count is below the frozen minimum"
            " floor (or below two, so no sample standard deviation is"
            " computable): a statistical sufficiency determination is not"
            " possible yet and additional independent runs are requested to"
            " reach the floor (total default)"
        ),
        status=ReplicateStatus.INDETERMINATE,
        predicate=lambda s: True,
    ),
)


def _measures(
    input_: ReplicateDecisionInput,
) -> tuple[float, float | None, float | None, float | None, float | None]:
    """The precision measures of the independent group (pure, stdlib only).

    Returns ``(mean, standard_deviation, standard_error, half_width,
    relative_half_width)`` per :class:`ReplicateState`. Technical repeats
    never enter any measure (AC-01). A zero independent mean is rejected
    up front: the relative-half-width criterion is undefined for it.
    """
    mean = fmean(input_.independent)
    n = len(input_.independent)
    if n < 2:
        return mean, None, None, None, None
    if mean == 0.0:
        raise InvalidReplicateInputError(
            "the independent replicate mean is zero: the relative-half-width"
            " precision criterion is undefined for a zero mean (AC-03)"
        )
    sample_deviation = stdev(input_.independent)
    standard_error = sample_deviation / math.sqrt(n)
    half_width = _z_critical_value(input_.confidence_level) * standard_error
    relative_half_width = half_width / abs(mean)
    return mean, sample_deviation, standard_error, half_width, relative_half_width


def _requested_additional_runs(
    status: ReplicateStatus, state: ReplicateState
) -> int:
    """The additional-run request shape of a decided assessment (AC-03).

    ``SUFFICIENT`` -> ``0``. ``INSUFFICIENT`` -> the smallest number of
    additional independent runs that brings the z-based relative
    half-width within the frozen threshold (the half-width scales with
    ``1 / sqrt(n)``, so ``n_required = ceil(n * (h / threshold) ** 2)``;
    at least 1). ``INDETERMINATE`` -> the runs needed to reach the floor
    (at least 1 -- a single independent replicate still needs a second
    one before any standard deviation exists). The request is a pure
    integer function of the state; deterministic on every platform.
    """
    n = state.independent_n
    if status is ReplicateStatus.SUFFICIENT:
        return 0
    if status is ReplicateStatus.INSUFFICIENT:
        h = state.relative_half_width
        assert h is not None  # INSUFFICIENT requires n >= 2 (R-REP-I1)
        n_required = math.ceil(n * (h / state.input.precision_threshold) ** 2)
        return max(n_required - n, 1)
    # INDETERMINATE: reach the floor (or at least one more run when the
    # floor is already met but no standard deviation is computable, i.e.
    # a single independent replicate).
    floor = state.input.min_independent
    return max(floor - n, 1)


def evaluate_replicate_sufficiency(
    independent: Sequence[float],
    technical: Sequence[float] = (),
    *,
    min_independent: int = DEFAULT_MIN_INDEPENDENT,
    precision_threshold: float,
    confidence_level: float = DEFAULT_REPLICATION_CONFIDENCE_LEVEL,
) -> ReplicateSufficiencyAssessment:
    """Evaluate the sufficiency of an observed replicate group (AC-01/02/03).

    Pure and deterministic: the status is a pure function of the
    replicate grouping and the **frozen** evaluation inputs
    (``REPLICATION_DECISION_RULES``, first match wins, trailing total
    default). Only the independent values carry statistical weight
    (AC-01: the grouping is input -- never guessed from the values --
    and technical repeats never satisfy the independent-n floor); the
    floor defaults to ``DEFAULT_MIN_INDEPENDENT`` = 3 and can only be
    overridden via the frozen acceptance criteria (AC-02); an
    insufficient precision is reported as ``INSUFFICIENT`` with an
    explicit additional-run request, never as a forced PASS/FAIL
    (AC-03). Nothing is persisted and no Requirement state is touched:
    the Supervisor consumes the assessment.

    Args:
        independent: the measured values of the **independent** runs
            (at least one, finite numbers; they carry the statistical
            weight).
        technical: the measured values of technical/instrument repeats
            (may be empty; recorded and reported but never counted
            toward the floor and never entering the precision
            computation -- AC-01).
        min_independent: the independent-n floor (default 3; an int of
            at least 1 -- the floor can never be weakened below 1,
            AC-02).
        precision_threshold: the frozen relative-half-width threshold of
            the mean precision criterion (a finite positive number;
            required -- the analog of the DEV-M9-G03 margin, AC-03).
        confidence_level: the two-sided level of the mean interval
            (default 0.95, strictly between 0 and 1).

    Returns:
        The full :class:`ReplicateSufficiencyAssessment` (status, rule
        trace, precision measures and the additional-run request).

    Raises:
        TypeError: an argument has the wrong type (including a
            ``min_independent`` that is not an int).
        InvalidReplicateInputError: a value is degenerate (fewer than
            one independent replicate, non-finite values, a zero
            independent mean, a non-positive/missing precision
            threshold, a floor below 1, a confidence level outside
            ``(0, 1)``).
    """
    decision_input = ReplicateDecisionInput(
        independent=independent,
        precision_threshold=precision_threshold,
        technical=technical,
        confidence_level=confidence_level,
        min_independent=min_independent,
    )
    mean, deviation, standard_error, half_width, relative_half_width = _measures(
        decision_input
    )
    state = ReplicateState(
        input=decision_input,
        mean=mean,
        standard_deviation=deviation,
        standard_error=standard_error,
        half_width=half_width,
        relative_half_width=relative_half_width,
    )
    decisions: list[ReplicateRuleDecision] = []
    matched_rule_id: str | None = None
    matched_status = ReplicateStatus.INDETERMINATE  # unreachable default
    for rule in REPLICATION_DECISION_RULES:
        hit = rule.predicate(state)
        decisions.append(
            ReplicateRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                status=rule.status,
                matched=hit,
            )
        )
        if hit and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_status = rule.status
    # R-REP-U1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return ReplicateSufficiencyAssessment(
        ruleset_version=REPLICATION_RULESET_VERSION,
        input=decision_input,
        state=state,
        status=matched_status,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
        requested_additional_runs=_requested_additional_runs(matched_status, state),
    )


# ---------------------------------------------------------------------------
# AC-03: reporting hooks (exact ResultRecord shapes)
# ---------------------------------------------------------------------------


def _require_metric_name(metric_name: Any) -> str:
    """Validate a metric name at the reporting-hook boundary."""
    if not isinstance(metric_name, str):
        raise TypeError(
            f"metric_name must be a str, got {type(metric_name).__name__}"
        )
    if not metric_name.strip():
        raise InvalidReplicateInputError(
            f"metric_name must be a non-empty string, got {metric_name!r}"
        )
    return metric_name


def sufficiency_metrics(
    metric_name: str, assessment: ReplicateSufficiencyAssessment
) -> list[dict[str, Any]]:
    """Build the derived-metric entries of a sufficiency assessment.

    The hook producing the ``metrics`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.metrics`` -- a list of
    ``{"metric": ..., "value": ...}`` dicts): the independent mean, the
    independent/technical counts, the standard error and relative
    half-width (when computable), the frozen floor and precision
    threshold and the additional-run request, in deterministic order.

    Raises:
        TypeError: ``metric_name`` is not a str, or ``assessment`` is not
            a ``ReplicateSufficiencyAssessment``.
        InvalidReplicateInputError: ``metric_name`` is empty/blank.
    """
    name = _require_metric_name(metric_name)
    if not isinstance(assessment, ReplicateSufficiencyAssessment):
        raise TypeError(
            "assessment must be a ReplicateSufficiencyAssessment, got"
            f" {type(assessment).__name__}"
        )
    metrics: list[dict[str, Any]] = [
        {"metric": name, "value": assessment.state.mean},
        {"metric": f"{name}_independent_n", "value": assessment.independent_n},
        {"metric": f"{name}_technical_n", "value": assessment.technical_n},
        {
            "metric": f"{name}_min_independent",
            "value": assessment.input.min_independent,
        },
        {
            "metric": f"{name}_precision_threshold",
            "value": assessment.input.precision_threshold,
        },
        {
            "metric": f"{name}_requested_additional_runs",
            "value": assessment.requested_additional_runs,
        },
    ]
    if assessment.state.standard_error is not None:
        metrics.append(
            {
                "metric": f"{name}_standard_error",
                "value": assessment.state.standard_error,
            }
        )
    if assessment.relative_half_width is not None:
        metrics.append(
            {
                "metric": f"{name}_relative_half_width",
                "value": assessment.relative_half_width,
            }
        )
    return metrics


def sufficiency_uncertainty_payload(
    assessment: ReplicateSufficiencyAssessment,
) -> dict[str, Any]:
    """Build the uncertainty payload of a sufficiency assessment.

    The hook producing the ``uncertainty`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.uncertainty`` -- a dict): method,
    the independent n, the mean, the confidence level and the z-based
    interval endpoints of the mean (plus the standard error when
    computable). Deterministic: a pure function of the assessment.

    Raises:
        TypeError: ``assessment`` is not a
            ``ReplicateSufficiencyAssessment``.
    """
    if not isinstance(assessment, ReplicateSufficiencyAssessment):
        raise TypeError(
            "assessment must be a ReplicateSufficiencyAssessment, got"
            f" {type(assessment).__name__}"
        )
    payload: dict[str, Any] = {
        "method": _UNCERTAINTY_METHOD,
        "n": assessment.independent_n,
        "mean": assessment.state.mean,
        "confidence_level": assessment.input.confidence_level,
    }
    if assessment.state.standard_error is not None:
        payload["standard_error"] = assessment.state.standard_error
    lower, upper = assessment.mean_interval
    if lower is not None and upper is not None:
        payload["lower"] = lower
        payload["upper"] = upper
    return payload


def sufficiency_findings(
    assessment: ReplicateSufficiencyAssessment,
) -> list[str]:
    """Build the QC-finding lines of a sufficiency assessment.

    The hook producing the ``qc_findings`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.qc_findings`` -- a list of strings):
    one stable one-line finding naming the decided status, the frozen
    criterion and the additional-run request. The finding reports the
    state of the **evidence** -- it never emits PASS/FAIL verdict words
    (AC-03).

    Raises:
        TypeError: ``assessment`` is not a
            ``ReplicateSufficiencyAssessment``.
    """
    if not isinstance(assessment, ReplicateSufficiencyAssessment):
        raise TypeError(
            "assessment must be a ReplicateSufficiencyAssessment, got"
            f" {type(assessment).__name__}"
        )
    n = assessment.independent_n
    threshold = assessment.input.precision_threshold
    if assessment.status is ReplicateStatus.SUFFICIENT:
        h = assessment.relative_half_width
        # SUFFICIENT requires n >= 2 (R-REP-S1), so the half-width exists.
        assert h is not None
        line = (
            "replicate sufficiency: "
            f"{n} independent replicates, relative half-width {h:.6g} within"
            f" the frozen precision threshold {threshold:.6g}; no additional"
            f" runs requested ({assessment.matched_rule_id})"
        )
    elif assessment.status is ReplicateStatus.INSUFFICIENT:
        h = assessment.relative_half_width
        # INSUFFICIENT requires n >= 2 (R-REP-I1), so the half-width exists.
        assert h is not None
        line = (
            "replicate sufficiency: relative half-width"
            f" {h:.6g} exceeds the frozen precision threshold {threshold:.6g}"
            f" at {n} independent replicates;"
            f" {assessment.requested_additional_runs} additional independent"
            f" run(s) requested ({assessment.matched_rule_id})"
        )
    else:
        line = (
            "replicate sufficiency: "
            f"{n} independent replicate(s) below the determination point"
            f" (independent-n floor {assessment.input.min_independent});"
            f" {assessment.requested_additional_runs} additional independent"
            f" run(s) requested ({assessment.matched_rule_id})"
        )
    return [line]
