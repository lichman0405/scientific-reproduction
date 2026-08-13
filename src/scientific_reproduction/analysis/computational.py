"""Generic computational convergence and sampling validation hooks (DEV-M9-G05).

Implements the **computational validation hook interfaces** deliverable
of DEV-M9-G05 over the DEV-M9-G02 result record shapes
(``analysis/results.py``) and the frozen ``core.models`` acceptance
vocabulary, grounded in:

* ``07-STATISTICS-AND-ACCEPTANCE.md`` SS7 (Computational Goals: do not
  force wet-lab statistics onto deterministic/numerical computations;
  use appropriate validation such as numerical convergence,
  finite-size/basis/cutoff/k-point convergence, stochastic sampling
  error for Monte Carlo, block averaging/autocorrelation analysis,
  replicate seeds when stochastic) and SS8 (every numeric margin must
  record its basis -- no global "+/-10% for everything" rule, so the
  convergence tolerance is a frozen input, never inferred from the
  observed run);
* ``12-ANALYSIS-SUBSYSTEM.md`` SS3 (the frozen Primary Analysis Protocol
  includes convergence checks and stochastic sampling analysis) and SS5
  (the Analysis Result Package carries QC findings, derived metrics and
  uncertainty/statistics);
* ``core/models.py`` ``DecisionMode`` -- the ``CONVERGENCE`` member is
  the frozen decision mode the computational validation output feeds
  into (:func:`validate_acceptance_mode`, AC-03).

AC-01 -- convergence failure representable without auto-changing parameters
---------------------------------------------------------------------------
:func:`evaluate_convergence` classifies the **observed** iteration series
(energy/force per SCF/GCMC/MD-style step, or drift per window) against
the frozen ``tolerance`` through the ordered ``CONVERGENCE_RULES`` table
(first match wins, trailing total default, ``matched_rule_id`` never
``None`` with a post-assert) into :class:`ConvergenceStatus`
(``CONVERGED`` / ``NOT_CONVERGED`` / ``DIVERGING`` -- the scientific
state of the run, never an acceptance decision). A scientific
convergence failure (the drift not settling / SCF-GCMC-MD-style iteration
non-convergence) is a first-class typed result (:class:`ConvergenceAssessment`,
``failure`` True). The hook is **pure by construction**: its inputs are
observations and frozen protocol values only -- there is no parameter
input to change, no restart/alter API, no auto-tuning surface, no I/O,
no persistence, and every record is a frozen dataclass (AC-01: the hook
reports, it never adjusts).

AC-02 -- Monte Carlo / sampling uncertainty hook
------------------------------------------------
:func:`sampling_uncertainty` summarizes a sample series (mean, sample
standard deviation, standard error of the mean, spread, normal
approximation confidence interval) deterministically with the stdlib
``statistics`` module only -- no scipy or numpy, and no external
statistics package. Degenerate inputs (empty series, a single sample,
nan/inf samples, out-of-range confidence levels) are rejected up front
with stable :class:`InvalidSamplingInputError` messages instead of
silently changing the report.

AC-03 -- validation output feeds Supervisor acceptance
------------------------------------------------------
:func:`convergence_metrics` / :func:`convergence_findings` /
:func:`sampling_metrics` / :func:`sampling_uncertainty_payload` /
:func:`sampling_findings` produce the exact shapes the DEV-M9-G02
``ResultRecord`` consumes (``metrics`` -- a list of ``{"metric": ...,
"value": ...}`` dicts; ``uncertainty`` -- a dict; ``qc_findings`` -- a
list of strings), and :func:`validate_acceptance_mode` ties the
validation output to the frozen ``DecisionMode.CONVERGENCE`` acceptance
record (:func:`convergence_criterion_from_acceptance` reads the frozen
``tolerance`` of the registered Acceptance Criteria verbatim). The
module **never decides acceptance**: the status vocabulary contains no
PASS/FAIL/REPRODUCED member, nothing reads or writes requirement state,
and requirement closure stays with the Supervisor/acceptance layer
(``core/rules/outcome.py``, ``planning/plan.py``).

Determinism and boundaries
--------------------------
All hooks are pure and deterministic: same inputs -> same assessment,
on every platform; no randomness, no wall clock, no network, no
persistence (the hooks validate and report, nothing is written).
``TypeError`` at the public boundaries; value violations follow the
``ValueError``-subclass convention with stable one-line messages;
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
    "CONVERGENCE_RULES",
    "CONVERGENCE_RULESET_VERSION",
    "DEFAULT_SAMPLING_CONFIDENCE_LEVEL",
    "VALIDATION_DECISION_MODE",
    # errors
    "ComputationalValidationError",
    "ConvergenceCriterionError",
    "InvalidConvergenceInputError",
    "InvalidSamplingInputError",
    "UnsupportedDecisionModeError",
    # AC-01: convergence validation
    "ConvergenceAssessment",
    "ConvergenceInput",
    "ConvergenceRule",
    "ConvergenceRuleDecision",
    "ConvergenceState",
    "ConvergenceStatus",
    "evaluate_convergence",
    # AC-02: sampling uncertainty
    "SamplingInterval",
    "SamplingUncertaintyReport",
    "sampling_uncertainty",
    # AC-03: reporting hooks feeding the Supervisor acceptance path
    "ConvergenceCriterion",
    "convergence_criterion_from_acceptance",
    "convergence_findings",
    "convergence_metrics",
    "sampling_findings",
    "sampling_metrics",
    "sampling_uncertainty_payload",
    "validate_acceptance_mode",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ComputationalValidationError(ValueError):
    """Base class for all computational validation hook errors."""


class InvalidConvergenceInputError(ComputationalValidationError):
    """Raised when a convergence input is degenerate.

    Covers iteration series with fewer than two entries (no drift is
    computable), non-finite values, non-positive tolerances, window/budget
    violations and series that exceed the declared iteration budget. A
    degenerate input cannot silently change a validation result, so it is
    rejected up front with a stable message.
    """


class InvalidSamplingInputError(ComputationalValidationError):
    """Raised when a sampling input is degenerate.

    Covers empty/single-sample series (no standard error is computable),
    non-finite samples and confidence levels outside ``(0, 1)``. A
    degenerate input cannot silently change a report, so it is rejected up
    front with a stable message.
    """


class ConvergenceCriterionError(ComputationalValidationError):
    """Raised when the frozen acceptance criteria carry no usable tolerance.

    AC-03: the convergence tolerance is an **input** from the frozen
    Acceptance Criteria -- a record without a numeric positive
    ``tolerance`` entry (or with several differing ones) cannot provide
    the convergence criterion and is rejected instead of falling back to
    anything run-derived.
    """


class UnsupportedDecisionModeError(ComputationalValidationError):
    """Raised when an acceptance record does not use the CONVERGENCE mode.

    AC-03: computational validation output feeds Supervisor acceptance
    under the frozen ``DecisionMode.CONVERGENCE`` only; an acceptance
    record declaring another decision mode cannot consume it.
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the convergence-decision rule table. Bumped whenever a rule
#: changes; recorded in every assessment so old assessments stay
#: interpretable.
CONVERGENCE_RULESET_VERSION: str = "1.0"

#: The two-sided confidence level of the sampling uncertainty hook by
#: default.
DEFAULT_SAMPLING_CONFIDENCE_LEVEL: float = 0.95

#: The frozen decision mode the computational validation output feeds
#: into (``core/models.py`` ``DecisionMode.CONVERGENCE``). The hook
#: references it; it never decides anything by itself (AC-03).
VALIDATION_DECISION_MODE: DecisionMode = DecisionMode.CONVERGENCE

#: The uncertainty method name reported by
#: :func:`sampling_uncertainty_payload`.
_SAMPLING_UNCERTAINTY_METHOD: str = "sampling"


def _require_number(value: Any, label: str) -> float:
    """Reject non-numeric values at the numeric boundaries (TypeError)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a number, got {type(value).__name__}")
    return float(value)


def _require_finite(
    value: Any, label: str, error_cls: type[ComputationalValidationError]
) -> None:
    """Reject non-finite values with a stable one-line message."""
    number = _require_number(value, label)
    if not math.isfinite(number):
        raise error_cls(f"{label} must be a finite number, got {value!r}")


def _require_metric_name(
    metric_name: Any, error_cls: type[ComputationalValidationError]
) -> str:
    """Validate a metric name at the reporting-hook boundary."""
    if not isinstance(metric_name, str):
        raise TypeError(
            f"metric_name must be a str, got {type(metric_name).__name__}"
        )
    if not metric_name.strip():
        raise error_cls(f"metric_name must be a non-empty string, got {metric_name!r}")
    return metric_name


# ---------------------------------------------------------------------------
# AC-01: convergence validation (pure reporting, no parameter adjustment)
# ---------------------------------------------------------------------------


class ConvergenceStatus(StrEnum):
    """The scientific convergence state of an observed iteration series.

    * ``CONVERGED`` -- the trailing convergence window settled within the
      frozen tolerance;
    * ``NOT_CONVERGED`` -- the series ended (budget exhausted) before the
      trailing window settled within the frozen tolerance (SCF/GCMC/MD-
      style iteration non-convergence);
    * ``DIVERGING`` -- the per-iteration drift is not settling: the last
      step moved the observable beyond every earlier step and beyond the
      frozen tolerance (energy/force drift not settling).

    This is a report of the **observable**, never an acceptance decision:
    the vocabulary deliberately contains no ``PASS``/``FAIL``/
    ``REPRODUCED`` member -- requirement closure stays with the
    Supervisor/acceptance layer (AC-03).
    """

    CONVERGED = "CONVERGED"
    NOT_CONVERGED = "NOT_CONVERGED"
    DIVERGING = "DIVERGING"


@dataclass(frozen=True)
class ConvergenceInput:
    """The observed iteration series and the frozen protocol inputs.

    ``iterations`` is the per-iteration observable of the run (e.g. the
    SCF energy per step, the GCMC block energy, the MD-window drift);
    ``tolerance`` is the **frozen** drift threshold of the protocol
    (``07-STATISTICS-AND-ACCEPTANCE.md`` SS8: a margin must record its
    basis -- the tolerance is never inferred from the observed run);
    ``window`` is the number of trailing per-iteration drift steps over
    which convergence must settle (1 = the classic last-step SCF
    delta-energy criterion); ``max_iterations`` is the declared iteration
    budget of the run (None = no budget declared; a series longer than
    the budget is a contradictory input and is rejected). Frozen and
    hashable so "same series -> same assessment" is directly testable;
    the series is stored as a tuple copy of the input, so later mutation
    of the caller's list cannot change the assessment (AC-01: the hook
    never mutates its inputs).

    Raises:
        TypeError: a field has the wrong type (the series must be a
            tuple/list of numbers; ``window``/``max_iterations`` ints;
            booleans are rejected as numbers).
        InvalidConvergenceInputError: the series has fewer than two
            entries, a value is non-finite, the tolerance is not a
            positive finite number, ``window``/``max_iterations`` are
            below 1, or the series exceeds the declared budget.
    """

    iterations: Sequence[float]
    tolerance: float
    window: int = 1
    max_iterations: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.iterations, (tuple, list)):
            raise TypeError(
                "iterations must be a tuple or list of numbers, got"
                f" {type(self.iterations).__name__}"
            )
        series = tuple(
            _require_number(value, f"iteration {index}")
            for index, value in enumerate(self.iterations)
        )
        object.__setattr__(self, "iterations", series)
        if len(series) < 2:
            raise InvalidConvergenceInputError(
                "convergence evaluation requires at least two iterations to"
                f" compute a per-iteration drift, got {len(series)}"
            )
        for index, value in enumerate(series):
            _require_finite(value, f"iteration {index}", InvalidConvergenceInputError)
        tolerance = _require_number(self.tolerance, "tolerance")
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise InvalidConvergenceInputError(
                f"tolerance must be a finite positive number, got {self.tolerance!r}"
            )
        object.__setattr__(self, "tolerance", tolerance)
        if isinstance(self.window, bool) or not isinstance(self.window, int):
            raise TypeError(
                f"window must be an int, got {type(self.window).__name__}"
            )
        if self.window < 1:
            raise InvalidConvergenceInputError(
                f"window must be at least 1, got {self.window!r}"
            )
        if self.max_iterations is not None:
            if isinstance(self.max_iterations, bool) or not isinstance(
                self.max_iterations, int
            ):
                raise TypeError(
                    "max_iterations must be an int or None, got"
                    f" {type(self.max_iterations).__name__}"
                )
            if self.max_iterations < 1:
                raise InvalidConvergenceInputError(
                    f"max_iterations must be at least 1, got {self.max_iterations!r}"
                )
            if len(self.iterations) > self.max_iterations:
                raise InvalidConvergenceInputError(
                    f"iteration series has {len(self.iterations)} entries but"
                    f" the declared budget is {self.max_iterations}: a series"
                    " cannot exceed its iteration budget"
                )


@dataclass(frozen=True)
class ConvergenceState:
    """The observed series and the drift measures convergence is decided from.

    The drift measures are pure functions of the :class:`ConvergenceInput`:
    ``final_drift`` is the absolute change at the last step;
    ``settling_drift`` is the largest absolute change over the trailing
    ``window`` steps (the window is clamped to the series when the series
    is shorter); ``max_drift`` is the largest absolute change anywhere in
    the series; ``prior_max_drift`` is the largest absolute change before
    the last step (None when the series has exactly two entries -- no
    earlier drift exists to compare against).

    Raises:
        TypeError: a field has the wrong type.
        InvalidConvergenceInputError: a measure is non-finite.
    """

    input: ConvergenceInput
    final_drift: float
    settling_drift: float
    max_drift: float
    prior_max_drift: float | None

    def __post_init__(self) -> None:
        if not isinstance(self.input, ConvergenceInput):
            raise TypeError(
                "ConvergenceState.input must be a ConvergenceInput, got"
                f" {type(self.input).__name__}"
            )
        _require_finite(self.final_drift, "ConvergenceState.final_drift", InvalidConvergenceInputError)
        _require_finite(self.settling_drift, "ConvergenceState.settling_drift", InvalidConvergenceInputError)
        _require_finite(self.max_drift, "ConvergenceState.max_drift", InvalidConvergenceInputError)
        if self.prior_max_drift is not None:
            _require_finite(
                self.prior_max_drift,
                "ConvergenceState.prior_max_drift",
                InvalidConvergenceInputError,
            )


@dataclass(frozen=True)
class ConvergenceRule:
    """One entry of the ordered convergence rule table."""

    rule_id: str
    description: str
    status: ConvergenceStatus
    predicate: Callable[[ConvergenceState], bool]


@dataclass(frozen=True)
class ConvergenceRuleDecision:
    """Record of one convergence-rule evaluation for a given state."""

    rule_id: str
    description: str
    status: ConvergenceStatus
    matched: bool


#: The ordered convergence rule table. First match wins; order is
#: normative. Predicates are pure functions of the
#: :class:`ConvergenceState` only, and ``R-CONV-C1`` is the total default
#: so every series is classified. The classification is a report of the
#: observable -- it decides nothing about acceptance (AC-03).
CONVERGENCE_RULES: tuple[ConvergenceRule, ...] = (
    ConvergenceRule(
        rule_id="R-CONV-D1",
        description=(
            "the per-iteration drift is not settling: the drift at the last"
            " iteration exceeds the frozen tolerance and every earlier"
            " drift -- the observable is moving away instead of settling"
            " (e.g. growing SCF oscillation or increasing MD drift)"
        ),
        status=ConvergenceStatus.DIVERGING,
        predicate=lambda s: (
            s.prior_max_drift is not None
            and s.final_drift > s.input.tolerance
            and s.final_drift > s.prior_max_drift
        ),
    ),
    ConvergenceRule(
        rule_id="R-CONV-N1",
        description=(
            "the trailing convergence window has not settled within the"
            " frozen tolerance when the iteration series ended: SCF/GCMC/"
            " MD-style iteration non-convergence (the run ended before the"
            " observable settled)"
        ),
        status=ConvergenceStatus.NOT_CONVERGED,
        predicate=lambda s: s.settling_drift > s.input.tolerance,
    ),
    ConvergenceRule(
        rule_id="R-CONV-C1",
        description=(
            "the trailing convergence window settled within the frozen"
            " tolerance (total default)"
        ),
        status=ConvergenceStatus.CONVERGED,
        predicate=lambda s: True,
    ),
)


@dataclass(frozen=True)
class ConvergenceAssessment:
    """Full, auditable result of one convergence validation.

    ``state`` is the exact series and drift measures the status was
    computed from; ``status`` is the classified :class:`ConvergenceStatus`;
    ``decisions`` records the outcome of every rule in the table (in
    evaluation order); ``matched_rule_id`` names the deciding rule (never
    ``None``: the trailing total default always matches);
    ``ruleset_version`` records the rule table version
    (``CONVERGENCE_RULESET_VERSION``). The assessment is pure data: it
    contains no parameters to change, and nothing here can restart or
    alter a calculation (AC-01).

    Raises:
        TypeError: a field has the wrong type.
        InvalidConvergenceInputError: ``matched_rule_id`` does not name a
            recorded decision, or the status does not match the matched
            decision.
    """

    ruleset_version: str
    state: ConvergenceState
    status: ConvergenceStatus
    decisions: tuple[ConvergenceRuleDecision, ...]
    matched_rule_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.ruleset_version, str):
            raise TypeError(
                "ConvergenceAssessment.ruleset_version must be a str, got"
                f" {type(self.ruleset_version).__name__}"
            )
        if not isinstance(self.state, ConvergenceState):
            raise TypeError(
                "ConvergenceAssessment.state must be a ConvergenceState, got"
                f" {type(self.state).__name__}"
            )
        if not isinstance(self.status, ConvergenceStatus):
            raise TypeError(
                "ConvergenceAssessment.status must be a ConvergenceStatus,"
                f" got {type(self.status).__name__}"
            )
        if not isinstance(self.decisions, tuple) or not all(
            isinstance(decision, ConvergenceRuleDecision)
            for decision in self.decisions
        ):
            raise TypeError(
                "ConvergenceAssessment.decisions must be a tuple of"
                " ConvergenceRuleDecision, got"
                f" {type(self.decisions).__name__}"
            )
        if not isinstance(self.matched_rule_id, str):
            raise TypeError(
                "ConvergenceAssessment.matched_rule_id must be a str, got"
                f" {type(self.matched_rule_id).__name__}"
            )
        matched = next(
            (d for d in self.decisions if d.rule_id == self.matched_rule_id),
            None,
        )
        if matched is None:
            raise InvalidConvergenceInputError(
                "ConvergenceAssessment.matched_rule_id must name one of the"
                f" recorded rule decisions, got {self.matched_rule_id!r}"
            )
        if not matched.matched or matched.status is not self.status:
            raise InvalidConvergenceInputError(
                "ConvergenceAssessment status must match the matched rule"
                f" decision ({self.matched_rule_id})"
            )

    @property
    def converged(self) -> bool:
        """True iff the scientific convergence succeeded (status CONVERGED)."""
        return self.status is ConvergenceStatus.CONVERGED

    @property
    def failure(self) -> bool:
        """True iff the run's scientific convergence failed (AC-01).

        True for ``NOT_CONVERGED`` (iteration non-convergence) and
        ``DIVERGING`` (drift not settling). This reports the state of the
        **observable**, never an acceptance decision (AC-03).
        """
        return self.status in (
            ConvergenceStatus.NOT_CONVERGED,
            ConvergenceStatus.DIVERGING,
        )

    @property
    def final_drift(self) -> float:
        """The absolute observable change at the last iteration."""
        return self.state.final_drift

    @property
    def settling_drift(self) -> float:
        """The largest absolute change over the trailing convergence window."""
        return self.state.settling_drift

    @property
    def iterations_used(self) -> int:
        """The number of iterations observed in the series."""
        return len(self.state.input.iterations)

    @property
    def budget_exhausted(self) -> bool:
        """True iff the series ran the full declared iteration budget."""
        max_iterations = self.state.input.max_iterations
        return (
            max_iterations is not None
            and len(self.state.input.iterations) == max_iterations
        )


def _drift_measures(
    input_: ConvergenceInput,
) -> tuple[float, float, float, float | None]:
    """The drift measures of a series (pure functions of the input).

    Returns ``(final_drift, settling_drift, max_drift, prior_max_drift)``
    per :class:`ConvergenceState`.
    """
    drifts = [
        abs(input_.iterations[index] - input_.iterations[index - 1])
        for index in range(1, len(input_.iterations))
    ]
    final_drift = drifts[-1]
    window = min(input_.window, len(drifts))
    settling_drift = max(drifts[-window:])
    max_drift = max(drifts)
    prior = drifts[:-1]
    prior_max_drift = max(prior) if prior else None
    return final_drift, settling_drift, max_drift, prior_max_drift


def evaluate_convergence(
    iterations: Sequence[float],
    tolerance: float,
    *,
    window: int = 1,
    max_iterations: int | None = None,
) -> ConvergenceAssessment:
    """Validate the convergence of an observed iteration series (AC-01).

    Pure and deterministic: the status is a pure function of the observed
    series and the **frozen** protocol inputs (``CONVERGENCE_RULES``,
    first match wins, trailing total default). The hook reports the
    scientific state of the run (``CONVERGED`` / ``NOT_CONVERGED`` /
    ``DIVERGING``) as a first-class typed result; it never changes
    parameters, never restarts or alters a calculation and has no side
    effect -- there is no parameter input to adjust (AC-01). Nothing is
    persisted and no acceptance is decided: the Supervisor consumes the
    assessment (AC-03).

    Args:
        iterations: the observed per-iteration observable of the run
            (energy/force per SCF/GCMC/MD-style step, drift per window),
            as a tuple or list of numbers (at least two).
        tolerance: the frozen convergence tolerance of the protocol (a
            finite positive number).
        window: the number of trailing per-iteration drift steps over
            which convergence must settle (default 1: the last-step
            criterion).
        max_iterations: the declared iteration budget of the run (None =
            no budget declared; a longer series is rejected as a
            contradictory input).

    Returns:
        The full :class:`ConvergenceAssessment` (status plus the
        auditable rule trace and drift measures).

    Raises:
        TypeError: an argument has the wrong type.
        InvalidConvergenceInputError: a value is degenerate (see
            :class:`ConvergenceInput`).
    """
    input_ = ConvergenceInput(
        iterations=iterations,
        tolerance=tolerance,
        window=window,
        max_iterations=max_iterations,
    )
    final_drift, settling_drift, max_drift, prior_max_drift = _drift_measures(input_)
    state = ConvergenceState(
        input=input_,
        final_drift=final_drift,
        settling_drift=settling_drift,
        max_drift=max_drift,
        prior_max_drift=prior_max_drift,
    )
    decisions: list[ConvergenceRuleDecision] = []
    matched_rule_id: str | None = None
    matched_status = ConvergenceStatus.CONVERGED  # unreachable default
    for rule in CONVERGENCE_RULES:
        hit = rule.predicate(state)
        decisions.append(
            ConvergenceRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                status=rule.status,
                matched=hit,
            )
        )
        if hit and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_status = rule.status
    # R-CONV-C1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return ConvergenceAssessment(
        ruleset_version=CONVERGENCE_RULESET_VERSION,
        state=state,
        status=matched_status,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# AC-02: Monte Carlo / sampling uncertainty (stdlib statistics only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SamplingInterval:
    """A confidence interval of the sampling uncertainty report.

    ``lower`` / ``upper`` are the endpoints of the normal-approximation
    interval ``mean +/- z * standard_error`` at the two-sided
    ``confidence_level``; the endpoints coincide when the standard error
    is zero (a constant sample series).

    Raises:
        TypeError: a field has the wrong type.
        InvalidSamplingInputError: a bound is non-finite, the confidence
            level is outside ``(0, 1)``, or ``upper < lower``.
    """

    lower: float
    upper: float
    confidence_level: float

    def __post_init__(self) -> None:
        _require_finite(self.lower, "SamplingInterval.lower", InvalidSamplingInputError)
        _require_finite(self.upper, "SamplingInterval.upper", InvalidSamplingInputError)
        level = _require_number(self.confidence_level, "SamplingInterval.confidence_level")
        if not math.isfinite(level) or not (0.0 < level < 1.0):
            raise InvalidSamplingInputError(
                "SamplingInterval.confidence_level must be in (0, 1), got"
                f" {self.confidence_level!r}"
            )
        if self.upper < self.lower:
            raise InvalidSamplingInputError(
                f"SamplingInterval.upper must not be below lower, got"
                f" ({self.lower}, {self.upper})"
            )


@dataclass(frozen=True)
class SamplingUncertaintyReport:
    """The sampling uncertainty of one sample series (AC-02).

    ``n`` is the sample count; ``mean`` the sample mean; ``minimum`` /
    ``maximum`` the sample extrema; ``spread`` their difference;
    ``standard_deviation`` the sample standard deviation;
    ``standard_error`` the standard error of the mean
    (``stdev / sqrt(n)``); ``interval`` the normal-approximation
    confidence interval at ``confidence_level``. All statistics come
    from the stdlib ``statistics`` module (no scipy, no numpy) and are
    pure, deterministic functions of the series. Frozen and hashable so
    "same samples -> same report" is directly testable.

    Raises:
        TypeError: a field has the wrong type.
        InvalidSamplingInputError: ``n`` is below 2, a measure is
            non-finite, or the interval's confidence level differs from
            the report's.
    """

    n: int
    mean: float
    minimum: float
    maximum: float
    spread: float
    standard_deviation: float
    standard_error: float
    confidence_level: float
    interval: SamplingInterval

    def __post_init__(self) -> None:
        if isinstance(self.n, bool) or not isinstance(self.n, int):
            raise TypeError(f"SamplingUncertaintyReport.n must be an int, got {type(self.n).__name__}")
        if self.n < 2:
            raise InvalidSamplingInputError(
                f"SamplingUncertaintyReport.n must be at least 2, got {self.n!r}"
            )
        for label in (
            "SamplingUncertaintyReport.mean",
            "SamplingUncertaintyReport.minimum",
            "SamplingUncertaintyReport.maximum",
            "SamplingUncertaintyReport.spread",
            "SamplingUncertaintyReport.standard_deviation",
            "SamplingUncertaintyReport.standard_error",
            "SamplingUncertaintyReport.confidence_level",
        ):
            _require_finite(getattr(self, label.rsplit(".", 1)[1]), label, InvalidSamplingInputError)
        if not isinstance(self.interval, SamplingInterval):
            raise TypeError(
                "SamplingUncertaintyReport.interval must be a SamplingInterval,"
                f" got {type(self.interval).__name__}"
            )
        if self.interval.confidence_level != self.confidence_level:
            raise InvalidSamplingInputError(
                "SamplingUncertaintyReport.confidence_level must match its"
                " interval"
            )

    @property
    def lower(self) -> float:
        """The confidence interval lower endpoint."""
        return self.interval.lower

    @property
    def upper(self) -> float:
        """The confidence interval upper endpoint."""
        return self.interval.upper


def _z_critical_value(confidence_level: float) -> float:
    """The two-sided standard-normal critical value of a level in (0, 1)."""
    return NormalDist().inv_cdf((1.0 + confidence_level) / 2.0)


def sampling_uncertainty(
    samples: Sequence[float],
    confidence_level: float = DEFAULT_SAMPLING_CONFIDENCE_LEVEL,
) -> SamplingUncertaintyReport:
    """Report the sampling uncertainty of a sample series (AC-02).

    Pure and deterministic: the report is a pure function of the sample
    series and the confidence level, computed with the stdlib
    ``statistics`` module only (no scipy, no numpy). Degenerate inputs
    (an empty series, a single sample, non-finite samples, a confidence
    level outside ``(0, 1)``) are rejected up front with stable
    :class:`InvalidSamplingInputError` messages -- the hook never
    silently degrades a report. Nothing is persisted and no acceptance is
    decided.

    Args:
        samples: the observed sample series (e.g. Monte Carlo block
            means, replicate results), as a tuple or list of numbers
            (at least two).
        confidence_level: the two-sided confidence level of the
            normal-approximation interval (default 0.95).

    Returns:
        The frozen :class:`SamplingUncertaintyReport`.

    Raises:
        TypeError: ``samples`` is not a tuple/list of numbers, or a
            sample is not a number.
        InvalidSamplingInputError: the series has fewer than two samples,
            a sample is non-finite, or ``confidence_level`` is outside
            ``(0, 1)``.
    """
    if not isinstance(samples, (tuple, list)):
        raise TypeError(
            f"samples must be a tuple or list of numbers, got {type(samples).__name__}"
        )
    if len(samples) < 2:
        raise InvalidSamplingInputError(
            "sampling_uncertainty requires at least two samples to compute a"
            f" standard error, got {len(samples)}"
        )
    series = tuple(
        _require_number(value, f"sample {index}")
        for index, value in enumerate(samples)
    )
    for index, value in enumerate(series):
        _require_finite(value, f"sample {index}", InvalidSamplingInputError)
    level = _require_number(confidence_level, "confidence_level")
    if not math.isfinite(level) or not (0.0 < level < 1.0):
        raise InvalidSamplingInputError(
            f"confidence_level must be in (0, 1), got {confidence_level!r}"
        )
    n = len(series)
    mean = fmean(series)
    minimum = min(series)
    maximum = max(series)
    spread = maximum - minimum
    sample_stdev = stdev(series)
    standard_error = sample_stdev / math.sqrt(n)
    half_width = _z_critical_value(level) * standard_error
    interval = SamplingInterval(
        lower=mean - half_width,
        upper=mean + half_width,
        confidence_level=level,
    )
    return SamplingUncertaintyReport(
        n=n,
        mean=mean,
        minimum=minimum,
        maximum=maximum,
        spread=spread,
        standard_deviation=sample_stdev,
        standard_error=standard_error,
        confidence_level=level,
        interval=interval,
    )


# ---------------------------------------------------------------------------
# AC-03: reporting hooks (exact ResultRecord shapes, DecisionMode.CONVERGENCE)
# ---------------------------------------------------------------------------


def convergence_metrics(
    metric_name: str, assessment: ConvergenceAssessment
) -> list[dict[str, Any]]:
    """Build the derived-metric entries of a convergence validation.

    The hook producing the ``metrics`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.metrics`` -- a list of
    ``{"metric": ..., "value": ...}`` dicts): the final drift, the
    settling drift, the maximum drift and the iteration count, in
    deterministic order.

    Raises:
        TypeError: ``metric_name`` is not a str, or ``assessment`` is not
            a ``ConvergenceAssessment``.
        InvalidConvergenceInputError: ``metric_name`` is empty/blank.
    """
    name = _require_metric_name(metric_name, InvalidConvergenceInputError)
    if not isinstance(assessment, ConvergenceAssessment):
        raise TypeError(
            f"assessment must be a ConvergenceAssessment, got {type(assessment).__name__}"
        )
    return [
        {"metric": name, "value": assessment.final_drift},
        {"metric": f"{name}_settling_drift", "value": assessment.settling_drift},
        {"metric": f"{name}_max_drift", "value": assessment.state.max_drift},
        {"metric": f"{name}_iterations", "value": assessment.iterations_used},
    ]


def convergence_findings(assessment: ConvergenceAssessment) -> list[str]:
    """Build the QC-finding lines of a convergence validation.

    The hook producing the ``qc_findings`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.qc_findings`` -- a list of strings):
    one stable one-line finding naming the deciding rule. The finding
    reports the scientific state of the observable (never PASS/FAIL for
    requirements -- AC-03).

    Raises:
        TypeError: ``assessment`` is not a ``ConvergenceAssessment``.
    """
    if not isinstance(assessment, ConvergenceAssessment):
        raise TypeError(
            f"assessment must be a ConvergenceAssessment, got {type(assessment).__name__}"
        )
    tolerance = assessment.state.input.tolerance
    if assessment.status is ConvergenceStatus.CONVERGED:
        line = (
            "convergence: the trailing-window drift"
            f" {assessment.settling_drift:.6g} is within the frozen tolerance"
            f" {tolerance:.6g} ({assessment.matched_rule_id})"
        )
    elif assessment.status is ConvergenceStatus.DIVERGING:
        line = (
            "convergence: the per-iteration drift is not settling: the final"
            f" drift {assessment.final_drift:.6g} exceeds every earlier drift"
            f" and the frozen tolerance {tolerance:.6g}"
            f" ({assessment.matched_rule_id})"
        )
    else:
        line = (
            "convergence: the trailing-window drift"
            f" {assessment.settling_drift:.6g} exceeds the frozen tolerance"
            f" {tolerance:.6g}; the iteration series ended without settling"
            f" ({assessment.matched_rule_id})"
        )
    return [line]


def sampling_metrics(
    metric_name: str, report: SamplingUncertaintyReport
) -> list[dict[str, Any]]:
    """Build the derived-metric entries of a sampling uncertainty report.

    The hook producing the ``metrics`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.metrics`` -- a list of
    ``{"metric": ..., "value": ...}`` dicts): the mean, the standard
    error of the mean and the spread, in deterministic order.

    Raises:
        TypeError: ``metric_name`` is not a str, or ``report`` is not a
            ``SamplingUncertaintyReport``.
        InvalidSamplingInputError: ``metric_name`` is empty/blank.
    """
    name = _require_metric_name(metric_name, InvalidSamplingInputError)
    if not isinstance(report, SamplingUncertaintyReport):
        raise TypeError(
            f"report must be a SamplingUncertaintyReport, got {type(report).__name__}"
        )
    return [
        {"metric": name, "value": report.mean},
        {"metric": f"{name}_standard_error", "value": report.standard_error},
        {"metric": f"{name}_spread", "value": report.spread},
    ]


def sampling_uncertainty_payload(report: SamplingUncertaintyReport) -> dict[str, Any]:
    """Build the uncertainty payload of a sampling report.

    The hook producing the ``uncertainty`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.uncertainty`` -- a dict): method,
    sample count, mean, standard deviation, standard error, extrema,
    spread, confidence level and the interval endpoints. Deterministic:
    a pure function of the report.

    Raises:
        TypeError: ``report`` is not a ``SamplingUncertaintyReport``.
    """
    if not isinstance(report, SamplingUncertaintyReport):
        raise TypeError(
            f"report must be a SamplingUncertaintyReport, got {type(report).__name__}"
        )
    return {
        "method": _SAMPLING_UNCERTAINTY_METHOD,
        "n": report.n,
        "mean": report.mean,
        "standard_deviation": report.standard_deviation,
        "standard_error": report.standard_error,
        "minimum": report.minimum,
        "maximum": report.maximum,
        "spread": report.spread,
        "confidence_level": report.confidence_level,
        "lower": report.lower,
        "upper": report.upper,
    }


def sampling_findings(
    metric_name: str, report: SamplingUncertaintyReport
) -> list[str]:
    """Build the QC-finding lines of a sampling uncertainty report.

    The hook producing the ``qc_findings`` shape the DEV-M9-G02 result
    records consume (``ResultRecord.qc_findings`` -- a list of strings):
    one stable one-line sampling summary per report.

    Raises:
        TypeError: ``metric_name`` is not a str, or ``report`` is not a
            ``SamplingUncertaintyReport``.
        InvalidSamplingInputError: ``metric_name`` is empty/blank.
    """
    name = _require_metric_name(metric_name, InvalidSamplingInputError)
    if not isinstance(report, SamplingUncertaintyReport):
        raise TypeError(
            f"report must be a SamplingUncertaintyReport, got {type(report).__name__}"
        )
    return [
        f"sampling of {name}: {report.n} samples, mean {report.mean:.6g},"
        f" standard error {report.standard_error:.6g}, spread {report.spread:.6g}"
    ]


def validate_acceptance_mode(acceptance: AcceptanceCriteria) -> None:
    """Reject acceptance records that cannot consume computational validation.

    AC-03: the computational validation output feeds Supervisor
    acceptance under the frozen ``DecisionMode.CONVERGENCE``
    (``VALIDATION_DECISION_MODE``) only. An acceptance record declaring
    any other decision mode is rejected with a stable
    :class:`UnsupportedDecisionModeError`; a matching record passes
    silently. The hook only checks the frozen mode -- it never decides
    acceptance itself.

    Raises:
        TypeError: ``acceptance`` is not an ``AcceptanceCriteria``.
        UnsupportedDecisionModeError: the record's ``decision_mode`` is
            not ``DecisionMode.CONVERGENCE``.
    """
    if not isinstance(acceptance, AcceptanceCriteria):
        raise TypeError(
            f"validate_acceptance_mode expects an AcceptanceCriteria, got"
            f" {type(acceptance).__name__}"
        )
    if acceptance.decision_mode is not VALIDATION_DECISION_MODE:
        raise UnsupportedDecisionModeError(
            "computational validation output feeds acceptance under"
            " DecisionMode.CONVERGENCE only; acceptance"
            f" {acceptance.acceptance_id!r} declares decision_mode"
            f" {acceptance.decision_mode.value!r} (AC-03)"
        )


@dataclass(frozen=True)
class ConvergenceCriterion:
    """The frozen convergence tolerance of an acceptance record.

    AC-03: the tolerance is an **input** from the registered, frozen
    Acceptance Criteria -- never inferred from the observed run. Frozen
    and hashable so "same record -> same tolerance" is directly testable.

    Raises:
        TypeError: ``tolerance`` is not a number.
        ConvergenceCriterionError: ``tolerance`` is not a finite positive
            number.
    """

    tolerance: float

    def __post_init__(self) -> None:
        value = _require_number(self.tolerance, "ConvergenceCriterion.tolerance")
        if not math.isfinite(value) or value <= 0:
            raise ConvergenceCriterionError(
                f"convergence tolerance must be a finite positive number, got"
                f" {self.tolerance!r}"
            )
        object.__setattr__(self, "tolerance", value)


def convergence_criterion_from_acceptance(
    acceptance: AcceptanceCriteria,
) -> ConvergenceCriterion:
    """Extract the convergence tolerance from a frozen acceptance record.

    AC-03: the convergence tolerance is an **input** from the registered,
    frozen ``AcceptanceCriteria`` record -- never inferred from the
    observed run. The record's ``criteria`` entries are scanned for a
    numeric ``tolerance`` (the frozen drift threshold, e.g.
    ``{"metric": "scf_energy", "tolerance": 1e-6}``); entries without a
    ``tolerance`` (e.g. replication-design criteria) are ignored. The
    function is a pure function of the record: it cannot see the run, and
    its return is byte-for-byte the stored tolerance. The decision-mode
    fit is checked separately by :func:`validate_acceptance_mode`.

    Raises:
        TypeError: ``acceptance`` is not an ``AcceptanceCriteria``.
        ConvergenceCriterionError: the record carries no numeric
            ``tolerance`` (a value that is not a positive finite number is
            rejected, and several **differing** tolerances are ambiguous
            and rejected).
    """
    if not isinstance(acceptance, AcceptanceCriteria):
        raise TypeError(
            "convergence_criterion_from_acceptance expects an"
            f" AcceptanceCriteria, got {type(acceptance).__name__}"
        )
    tolerances: set[float] = set()
    for entry in acceptance.criteria:
        if not isinstance(entry, dict) or "tolerance" not in entry:
            continue
        tolerance = entry["tolerance"]
        if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
            raise ConvergenceCriterionError(
                "convergence tolerance must be a finite positive number in"
                f" the frozen acceptance criteria, got {tolerance!r} (AC-03)"
            )
        value = float(tolerance)
        if not math.isfinite(value) or value <= 0:
            raise ConvergenceCriterionError(
                "convergence tolerance must be a finite positive number in"
                f" the frozen acceptance criteria, got {tolerance!r} (AC-03)"
            )
        tolerances.add(value)
    if not tolerances:
        raise ConvergenceCriterionError(
            "no convergence tolerance in the frozen acceptance criteria: a"
            " numeric 'tolerance' entry is required; the tolerance is a"
            " frozen input, never inferred from the run (AC-03)"
        )
    if len(tolerances) > 1:
        raise ConvergenceCriterionError(
            "ambiguous convergence tolerances in the frozen acceptance"
            f" criteria: {', '.join(str(t) for t in sorted(tolerances))};"
            " exactly one tolerance is required (AC-03)"
        )
    return ConvergenceCriterion(tolerance=tolerances.pop())
