"""Materials-chemistry gas adsorption/separation templates (DEV-M11-G03).

Implements the **gas adsorption/separation Unit Process templates**
deliverable of DEV-M11-G03 for the materials-chemistry domain pack: frozen,
parameterized templates for BET, single-component gas adsorption, IAST
selectivity, Qst, dynamic breakthrough and cycling/stability, with analysis
as a separate, independently freezable surface from raw execution and with
breakthrough raw/result metadata that maps deterministically to formal paper
figures/results. Grounded in:

* ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS2 (v0.1 experimental
  capability families: N2 adsorption / BET / pore analysis;
  single-component gas adsorption; multi-component selectivity
  calculation; Qst calculation; dynamic breakthrough; cycling/reusability;
  stability testing) and SS5 (BET: "Use a frozen fitting/selection
  protocol. Batch-to-batch sample activation quality must be treated as a
  process variable; one attractive BET result is not sufficient"; gas
  adsorption: "Validate data quality, equilibration, units, temperature,
  pressure basis and sample activation. Use independent material batches
  where scientifically feasible"; breakthrough: "Record flow rates, gas
  composition, column dimensions, packing mass/density, dead volume,
  detector calibration and temperature. Missing critical column parameters
  enter Assumption Registry"; missing scientifically meaningful settings
  are A2 unless reliable method evidence supports an A1 classification);
* ``08-STRICT-RECOVERY-CLOSURE.md`` SS1/SS3 (the Assumption Registry:
  every non-explicit parameter is registered as ``A0_TECHNICAL_DEFAULT`` /
  ``A1_METHODOLOGICAL_DEFAULT`` / ``A2_SCIENTIFIC_ASSUMPTION``; A2 must
  not be silently used inside strict reproduction);
* ``core/models.py`` -- the frozen vocabulary reused verbatim:
  ``GoalTrack`` (the strict/recovery track label) and ``Assumption`` /
  ``AssumptionClassification`` (the Assumption Registry entry);
* ``core/rules/assumptions.py`` -- the EXISTING Assumption Registry
  evaluation API (``assumption_effect`` / ``evaluate_strict_label``):
  missing temperature/pressure/composition conditions and missing
  breakthrough column parameters are routed through it, never through a
  parallel store;
* ``core/permissions.py`` (DEV-M6-G03) -- the role-action matrix:
  templates are proposed by Research/domain helpers and RECORD the
  strict/recovery label, but freezing is Supervisor-only; the freeze
  helper is gated by the matrix (``Action.PLAN_FREEZE``, granted only to
  the Supervisor), so nothing is ever silently frozen.

The gas/condition vocabulary is universal (AC-03): the rule tables name no
gas, no temperature/pressure/composition value and no reported number --
specific chemistry and reported values appear only as **instance data** in
template parameters and breakthrough results-table entries (the workflows
of the v0.1 capability families, including the reference-case protocols
themselves, are parameterized by the templates, never hardcoded).

Template model (determinism and boundaries)
-------------------------------------------
Every template is a frozen dataclass with strict ``__post_init__``
validation: ``TypeError`` at the type boundaries (template id, stage,
kind, track, parameters, results table, ...), ``ValueError``-subclass
stable errors (``InvalidAdsorptionTemplateError`` and siblings) for value
violations. Construction enforces the **universal value rules** of the
ordered, versioned ``ADSORPTION_VALUE_RULES`` table over the parameters
that ARE present, and validates ids as safe single registry path segments
(the FND-M9-G02-01 lesson: no path separators, no glob metacharacters).
Missing scientific parameters are NOT a construction error: they are the
input to the Assumption Registry pathway (AC-01) -- the ordered
``ADSORPTION_PARAMETER_RULES`` table declares, per (kind, stage) pair, the
required scientific parameter set (temperature, pressure and composition
are explicit condition inputs per kind), and
:func:`assumptions_for_missing_parameters` routes every missing required
parameter through the real ``core.models.Assumption`` record and the real
``core.rules.assumptions`` evaluation API, returning the exact
assumptions, their recorded strict-status effects and the strict label,
with the assumption refs carried on the template.

Execution and analysis are separate metadata surfaces (AC-02): every
template class selects an ``AdsorptionStage`` (``EXECUTION`` or
``ANALYSIS``), the required-parameter rules differ per stage (execution
captures the raw measurement conditions -- gas identity, temperature,
pressure, composition, flow, column geometry; analysis captures the
fitting/selection inputs -- the BET relative-pressure range, the IAST
adsorbate pair and composition, the Qst temperature pairs and reference
loading), and each surface carries its own ``frozen`` flag and is frozen
independently through :func:`freeze_adsorption_template`. The two pairs
without a raw execution in this pack (IAST execution, Qst execution) are
decided by the trailing total default: their raw execution surface is the
single-component isotherm template.

Breakthrough raw/result metadata (AC-03): a breakthrough template records
its raw measurement conditions (AC-01) and may carry a
:class:`BreakthroughResultTable` -- representable metadata mapping raw/
result keys to formal paper figure/result references (instance data,
never a worker judgment). :func:`capture_protocol` records the mapping
deterministically (:func:`paper_mapping`), so breakthrough raw/result
metadata maps to the paper's figures/results byte-identically across
calls.

The templates support strict/recovery labeling (AC-01): every template
carries a ``track`` drawn from the frozen ``GoalTrack`` vocabulary
(``STRICT_REPRODUCTION`` / ``RECOVERY`` / ``METHOD_REDESIGN``).

Pure deterministic module: no randomness, no wall clock, no network, no
I/O anywhere; same inputs -> same templates, assessments and captures on
every call and platform. ``from __future__ import annotations``;
``__all__``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, ClassVar, Sequence

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
    GoalTrack,
)
from scientific_reproduction.core.permissions import (
    Action,
    PermissionDeniedError,
    Role,
    check_action_allowed,
)
from scientific_reproduction.core.rules.assumptions import (
    AssumptionEffectDecision,
    StrictLabelAssessment,
    assumption_effect,
    evaluate_strict_label,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    CONTROLLED_ATMOSPHERES,
)

__all__ = [
    "ADSORPTION_PARAMETER_RULES",
    "ADSORPTION_RULESET_VERSION",
    "ADSORPTION_VALUE_RULES",
    "ANALYSIS_STAGE",
    "AdsorptionKind",
    "AdsorptionParameterRule",
    "AdsorptionStage",
    "AdsorptionTemplateBase",
    "AdsorptionTemplateError",
    "AdsorptionValueRule",
    "BET_KIND",
    "BREAKTHROUGH_KIND",
    "BetTemplate",
    "BreakthroughResultTable",
    "BreakthroughTemplate",
    "CAPTURE_KEYS",
    "CYCLING_STABILITY_KIND",
    "CyclingStabilityTemplate",
    "EXECUTION_STAGE",
    "IAST_KIND",
    "IastTemplate",
    "InvalidAdsorptionTemplateError",
    "InvalidBreakthroughResultError",
    "MissingParameterRouting",
    "PaperResultEntry",
    "ParameterCompletenessAssessment",
    "ParameterCompletenessDecision",
    "QST_KIND",
    "QstTemplate",
    "SINGLE_COMPONENT_KIND",
    "SingleComponentTemplate",
    "ValueValidationAssessment",
    "ValueValidationDecision",
    "apply_assumption_routing",
    "assess_parameter_completeness",
    "assumptions_for_missing_parameters",
    "capture_protocol",
    "freeze_adsorption_template",
    "missing_parameters",
    "paper_mapping",
    "validate_adsorption_rulesets",
    "validate_template_values",
]

#: Version of the template rule tables. Bumped whenever a rule changes;
#: recorded in every assessment so old decisions stay interpretable.
ADSORPTION_RULESET_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class AdsorptionTemplateError(ValueError):
    """Base class for all adsorption-template errors."""


class InvalidAdsorptionTemplateError(AdsorptionTemplateError):
    """Raised when a template violates a universal value rule or shape rule."""


class InvalidBreakthroughResultError(AdsorptionTemplateError):
    """Raised when a breakthrough results table violates the mapping shape."""


# ---------------------------------------------------------------------------
# Kind and stage vocabulary (the adsorption capability families)
# ---------------------------------------------------------------------------


class AdsorptionKind(StrEnum):
    """The gas adsorption/separation kinds the templates parameterize.

    Values follow ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS2 family
    names (N2 adsorption / BET / pore analysis; single-component gas
    adsorption; multi-component selectivity calculation; Qst calculation;
    dynamic breakthrough; cycling/reusability and stability testing). This
    is domain-pack vocabulary, distinct from the frozen core vocabulary --
    the kinds are the keys of the universal ``ADSORPTION_PARAMETER_RULES``
    table, never gas or chemistry instances.
    """

    BET = "bet"
    SINGLE_COMPONENT = "single_component"
    IAST = "iast"
    QST = "qst"
    BREAKTHROUGH = "breakthrough"
    CYCLING_STABILITY = "cycling_stability"


#: Convenience aliases for the six adsorption kinds.
BET_KIND: AdsorptionKind = AdsorptionKind.BET
SINGLE_COMPONENT_KIND: AdsorptionKind = AdsorptionKind.SINGLE_COMPONENT
IAST_KIND: AdsorptionKind = AdsorptionKind.IAST
QST_KIND: AdsorptionKind = AdsorptionKind.QST
BREAKTHROUGH_KIND: AdsorptionKind = AdsorptionKind.BREAKTHROUGH
CYCLING_STABILITY_KIND: AdsorptionKind = AdsorptionKind.CYCLING_STABILITY


class AdsorptionStage(StrEnum):
    """The metadata surface a template captures (AC-02).

    ``EXECUTION`` captures the raw measurement itself -- the gas identity,
    temperature, pressure, composition, flow and column conditions of the
    Unit Process; ``ANALYSIS`` captures the derived analysis -- the
    fitting/selection inputs (BET relative-pressure range, IAST adsorbate
    pair and composition, Qst temperature pairs and reference loading,
    breakthrough criteria) and its validation. The two surfaces are
    separate templates with their own required parameter sets, and each is
    frozen independently (AC-02: BET/IAST/Qst analysis remains separate
    from raw execution).
    """

    EXECUTION = "execution"
    ANALYSIS = "analysis"


#: Convenience aliases for the two adsorption stages.
EXECUTION_STAGE: AdsorptionStage = AdsorptionStage.EXECUTION
ANALYSIS_STAGE: AdsorptionStage = AdsorptionStage.ANALYSIS


# ---------------------------------------------------------------------------
# Universal rule tables (AC-01: conditions captured, never instances)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdsorptionParameterRule:
    """One entry of the required-scientific-parameter rule table.

    Declares, per (kind, stage) pair, the required scientific parameters
    a template of that pair must record (or route to the Assumption
    Registry pathway when missing). The parameter names are universal
    method-capture vocabulary -- no gas names, no chemistry instances, no
    condition values (AC-03). The predicate is a pure function of the
    kind and the stage; the trailing total default always matches.
    """

    rule_id: str
    description: str
    required_parameters: tuple[str, ...]
    predicate: Callable[[AdsorptionKind, AdsorptionStage], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"AdsorptionParameterRule.{field_name} must be a str,"
                    f" got {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidAdsorptionTemplateError(
                    f"AdsorptionParameterRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not isinstance(self.required_parameters, tuple) or not all(
            isinstance(parameter, str) and parameter.strip()
            for parameter in self.required_parameters
        ):
            raise TypeError(
                "AdsorptionParameterRule.required_parameters must be a"
                " tuple of non-empty strings"
            )
        if not callable(self.predicate):
            raise TypeError(
                "AdsorptionParameterRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class AdsorptionValueRule:
    """One entry of the universal parameter-value rule table.

    Each rule validates the value of one named scientific parameter when
    the template records it. Predicates are pure functions of the value
    only; the message template is filled with the offending value. All
    rules are universal physical/handling rules -- gas and column names
    are non-empty names (any name: no gas-name table, AC-03),
    temperatures on the absolute scale, pressures non-negative, a mole
    fraction a normalized score, a relative pressure a strict fraction
    below one, masses and column dimensions positive, a dead volume
    non-negative, a controlled-atmosphere vocabulary, cycle counts
    positive integers, and analysis inputs (fitting model, property,
    criterion, metrics) non-empty names.
    """

    rule_id: str
    description: str
    parameter: str
    predicate: Callable[[Any], bool]
    message: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("parameter", self.parameter),
            ("message", self.message),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"AdsorptionValueRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidAdsorptionTemplateError(
                    f"AdsorptionValueRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not callable(self.predicate):
            raise TypeError(
                "AdsorptionValueRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


def _is_finite_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number of any sign."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_positive_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number strictly above zero."""
    return _is_finite_number(value) and value > 0


def _is_non_negative_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number >= 0."""
    return _is_finite_number(value) and value >= 0


def _is_positive_integer(value: Any) -> bool:
    """True iff ``value`` is an int >= 1 (bool is not an int here)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_unit_score(value: Any) -> bool:
    """True iff ``value`` is a finite number in the normalized range [0, 1]."""
    return _is_finite_number(value) and 0 <= value <= 1


def _is_relative_pressure(value: Any) -> bool:
    """True iff ``value`` is a strict pressure fraction in the open (0, 1).

    A relative pressure p/p0 is a dimensionless ratio strictly between
    zero and one: the range endpoints of a BET fitting window are
    instance data on the analysis template, never universal thresholds.
    """
    return _is_finite_number(value) and 0 < value < 1


def _is_non_empty_string(value: Any) -> bool:
    """True iff ``value`` is a non-empty str."""
    return isinstance(value, str) and bool(value.strip())


def _is_controlled_atmosphere(value: Any) -> bool:
    """True iff ``value`` is a controlled-atmosphere name."""
    return isinstance(value, str) and value in CONTROLLED_ATMOSPHERES


def _kind_stage_is(
    kind: AdsorptionKind, stage: AdsorptionStage
) -> Callable[[AdsorptionKind, AdsorptionStage], bool]:
    """A predicate matching exactly the given (kind, stage) pair."""
    return lambda candidate_kind, candidate_stage: (
        candidate_kind is kind and candidate_stage is stage
    )


#: The ordered, versioned universal value-rule table. Each named parameter
#: has exactly one rule (the table is a total function of parameter
#: names). Order is normative.
ADSORPTION_VALUE_RULES: tuple[AdsorptionValueRule, ...] = (
    AdsorptionValueRule(
        rule_id="R-ADS-V1",
        description=(
            "a recorded adsorbate must be a named gas (any name; the rules"
            " never restrict which gas -- instance data lives in template"
            " parameters)"
        ),
        parameter="adsorbate",
        predicate=_is_non_empty_string,
        message="adsorbate must be a non-empty gas name, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V2",
        description=(
            "a recorded co-adsorbate must be a named gas (any name; the"
            " rules never restrict which gas -- AC-03)"
        ),
        parameter="co_adsorbate",
        predicate=_is_non_empty_string,
        message="co_adsorbate must be a non-empty gas name, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V3",
        description=(
            "a thermodynamic temperature is on the absolute scale: a"
            " recorded temperature must be a finite positive number of"
            " kelvin"
        ),
        parameter="temperature_K",
        predicate=_is_positive_number,
        message="temperature_K must be a finite positive number of kelvin,"
        " got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V4",
        description=(
            "a recorded low temperature of an isosteric pair is on the"
            " absolute scale: it must be a finite positive number of kelvin"
        ),
        parameter="temperature_low_K",
        predicate=_is_positive_number,
        message="temperature_low_K must be a finite positive number of"
        " kelvin, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V5",
        description=(
            "a recorded high temperature of an isosteric pair is on the"
            " absolute scale: it must be a finite positive number of kelvin"
        ),
        parameter="temperature_high_K",
        predicate=_is_positive_number,
        message="temperature_high_K must be a finite positive number of"
        " kelvin, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V6",
        description=(
            "a recorded pressure must be a finite non-negative number of"
            " kilopascal"
        ),
        parameter="pressure_kPa",
        predicate=_is_non_negative_number,
        message="pressure_kPa must be a finite non-negative number of"
        " kilopascal, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V7",
        description=(
            "a recorded mixture composition must be a mole fraction: a"
            " finite number in [0, 1]"
        ),
        parameter="composition_fraction",
        predicate=_is_unit_score,
        message="composition_fraction must be a finite number in [0, 1],"
        " got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V8",
        description=(
            "a recorded relative-pressure range start is a pressure"
            " fraction: a finite number strictly between 0 and 1"
        ),
        parameter="relative_pressure_min",
        predicate=_is_relative_pressure,
        message="relative_pressure_min must be a finite number strictly"
        " between 0 and 1, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V9",
        description=(
            "a recorded relative-pressure range end is a pressure"
            " fraction: a finite number strictly between 0 and 1"
        ),
        parameter="relative_pressure_max",
        predicate=_is_relative_pressure,
        message="relative_pressure_max must be a finite number strictly"
        " between 0 and 1, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V10",
        description=(
            "a recorded sample mass must be a finite positive number of"
            " milligrams"
        ),
        parameter="sample_mass_mg",
        predicate=_is_positive_number,
        message="sample_mass_mg must be a finite positive number of"
        " milligrams, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V11",
        description=(
            "a recorded adsorbent mass must be a finite positive number of"
            " milligrams"
        ),
        parameter="adsorbent_mass_mg",
        predicate=_is_positive_number,
        message="adsorbent_mass_mg must be a finite positive number of"
        " milligrams, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V12",
        description=(
            "a recorded flow rate must be a finite positive number of"
            " millilitres per minute"
        ),
        parameter="flow_rate_ml_min",
        predicate=_is_positive_number,
        message="flow_rate_ml_min must be a finite positive number of"
        " millilitres per minute, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V13",
        description=(
            "a recorded column length must be a finite positive number of"
            " millimetres"
        ),
        parameter="column_length_mm",
        predicate=_is_positive_number,
        message="column_length_mm must be a finite positive number of"
        " millimetres, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V14",
        description=(
            "a recorded column diameter must be a finite positive number"
            " of millimetres"
        ),
        parameter="column_diameter_mm",
        predicate=_is_positive_number,
        message="column_diameter_mm must be a finite positive number of"
        " millimetres, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V15",
        description=(
            "a recorded dead volume must be a finite non-negative number"
            " of millilitres"
        ),
        parameter="dead_volume_ml",
        predicate=_is_non_negative_number,
        message="dead_volume_ml must be a finite non-negative number of"
        " millilitres, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V16",
        description=(
            "a recorded detector must be a named detector (any name; the"
            " rules never restrict which detector -- AC-03)"
        ),
        parameter="detector",
        predicate=_is_non_empty_string,
        message="detector must be a non-empty detector name, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V17",
        description=(
            "a recorded regeneration protocol must be a non-empty protocol"
            " description"
        ),
        parameter="regeneration_protocol",
        predicate=_is_non_empty_string,
        message="regeneration_protocol must be a non-empty protocol, got"
        " {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V18",
        description=(
            "a recorded cycle count must be an integer of at least one"
            " cycle"
        ),
        parameter="cycle_count",
        predicate=_is_positive_integer,
        message="cycle_count must be an integer >= 1, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V19",
        description=(
            "a recorded atmosphere must be one of the controlled handling"
            " vocabulary"
        ),
        parameter="atmosphere",
        predicate=_is_controlled_atmosphere,
        message="atmosphere must be one of the controlled handling names"
        f" {sorted(CONTROLLED_ATMOSPHERES)}, got {{value!r}}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V20",
        description=(
            "a recorded fitting model must be a non-empty model name (any"
            " model -- the rules never restrict which; the frozen"
            " fitting/selection protocol is instance data on the analysis"
            " template)"
        ),
        parameter="model",
        predicate=_is_non_empty_string,
        message="model must be a non-empty model name, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V21",
        description=(
            "a recorded computed property must be a non-empty property"
            " name (any property -- the rules never restrict which)"
        ),
        parameter="property",
        predicate=_is_non_empty_string,
        message="property must be a non-empty property name, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V22",
        description=(
            "a recorded criterion must be a non-empty criterion name (e.g."
            " a breakthrough detection criterion)"
        ),
        parameter="criterion",
        predicate=_is_non_empty_string,
        message="criterion must be a non-empty criterion name, got"
        " {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V23",
        description=(
            "a recorded convergence metric must be a non-empty metric name"
        ),
        parameter="convergence_metric",
        predicate=_is_non_empty_string,
        message="convergence_metric must be a non-empty metric name, got"
        " {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V24",
        description=(
            "a recorded convergence threshold must be a finite positive"
            " number"
        ),
        parameter="convergence_threshold",
        predicate=_is_positive_number,
        message="convergence_threshold must be a finite positive number,"
        " got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V25",
        description=(
            "a recorded statistical-uncertainty metric must be a non-empty"
            " metric name"
        ),
        parameter="statistical_uncertainty_metric",
        predicate=_is_non_empty_string,
        message="statistical_uncertainty_metric must be a non-empty metric"
        " name, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V26",
        description=(
            "a recorded sampling validation must be a non-empty validation"
            " description"
        ),
        parameter="sampling_validation",
        predicate=_is_non_empty_string,
        message="sampling_validation must be a non-empty description, got"
        " {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V27",
        description=(
            "a recorded reference loading must be a finite positive number"
            " of moles per kilogram of adsorbent"
        ),
        parameter="reference_loading_mol_kg",
        predicate=_is_positive_number,
        message="reference_loading_mol_kg must be a finite positive number"
        " of moles per kilogram, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V28",
        description=(
            "a recorded reference value must be a finite number of any"
            " sign (uptake differences and retention losses may be"
            " negative)"
        ),
        parameter="reference_value",
        predicate=_is_finite_number,
        message="reference_value must be a finite number, got {value!r}",
    ),
    AdsorptionValueRule(
        rule_id="R-ADS-V29",
        description=(
            "a recorded comparison tolerance must be a finite positive"
            " number"
        ),
        parameter="tolerance",
        predicate=_is_positive_number,
        message="tolerance must be a finite positive number, got {value!r}",
    ),
)

#: The ordered required-scientific-parameter rule table, one rule per
#: (kind, stage) pair, first match wins, trailing total default (AC-01:
#: temperature/pressure/composition are explicit Unit Process condition
#: inputs per kind; missing required parameters route to the Assumption
#: Registry pathway; AC-02: execution and analysis declare separate
#: surfaces; AC-03: the table is universal).
ADSORPTION_PARAMETER_RULES: tuple[AdsorptionParameterRule, ...] = (
    AdsorptionParameterRule(
        rule_id="R-ADS-P1",
        description=(
            "BET execution records the adsorbate, the measurement"
            " temperature and the sample mass (the raw isotherm"
            " acquisition of WP-40; activation quality is a batch process"
            " variable, 16-...DOMAIN-PACK SS5)"
        ),
        required_parameters=(
            "adsorbate",
            "temperature_K",
            "sample_mass_mg",
        ),
        predicate=_kind_stage_is(AdsorptionKind.BET, AdsorptionStage.EXECUTION),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P2",
        description=(
            "BET analysis records the computed property, the frozen"
            " fitting model and the relative-pressure fitting range (the"
            " frozen fitting/selection protocol of 16-...DOMAIN-PACK SS5)"
        ),
        required_parameters=(
            "property",
            "model",
            "relative_pressure_min",
            "relative_pressure_max",
        ),
        predicate=_kind_stage_is(AdsorptionKind.BET, AdsorptionStage.ANALYSIS),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P3",
        description=(
            "single-component adsorption execution records the adsorbate,"
            " the temperature and the pressure (AC-01: explicit"
            " temperature/pressure conditions of the isotherm point)"
        ),
        required_parameters=(
            "adsorbate",
            "temperature_K",
            "pressure_kPa",
        ),
        predicate=_kind_stage_is(
            AdsorptionKind.SINGLE_COMPONENT, AdsorptionStage.EXECUTION
        ),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P4",
        description=(
            "single-component adsorption analysis records the computed"
            " property, the fitting model, the convergence metric and"
            " threshold, the statistical-uncertainty metric and the"
            " sampling validation (16-...DOMAIN-PACK SS5: validate data"
            " quality, equilibration, units, temperature, pressure basis"
            " and sample activation)"
        ),
        required_parameters=(
            "property",
            "model",
            "convergence_metric",
            "convergence_threshold",
            "statistical_uncertainty_metric",
            "sampling_validation",
        ),
        predicate=_kind_stage_is(
            AdsorptionKind.SINGLE_COMPONENT, AdsorptionStage.ANALYSIS
        ),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P5",
        description=(
            "IAST analysis records the computed property, the adsorbate"
            " pair, the mixture composition, the temperature and the"
            " pressure (AC-01: explicit composition/temperature/pressure"
            " conditions of the selectivity calculation), plus the fitting"
            " model and the sampling validation"
        ),
        required_parameters=(
            "property",
            "adsorbate",
            "co_adsorbate",
            "composition_fraction",
            "temperature_K",
            "pressure_kPa",
            "model",
            "sampling_validation",
        ),
        predicate=_kind_stage_is(AdsorptionKind.IAST, AdsorptionStage.ANALYSIS),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P6",
        description=(
            "Qst analysis records the computed property, the adsorbate,"
            " the low and high temperatures of the isosteric pair and the"
            " reference loading at which the isosteric heat is evaluated"
        ),
        required_parameters=(
            "property",
            "adsorbate",
            "temperature_low_K",
            "temperature_high_K",
            "reference_loading_mol_kg",
        ),
        predicate=_kind_stage_is(AdsorptionKind.QST, AdsorptionStage.ANALYSIS),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P7",
        description=(
            "breakthrough execution records the gas pair and mixture"
            " composition, the temperature and the pressure (AC-01), the"
            " flow rate, the adsorbent mass, the column length and"
            " diameter, the dead volume, the detector and the regeneration"
            " and cycling protocols (16-...DOMAIN-PACK SS5 breakthrough"
            " record and pre-execution inventory)"
            " inventory)"
        ),
        required_parameters=(
            "adsorbate",
            "co_adsorbate",
            "composition_fraction",
            "temperature_K",
            "pressure_kPa",
            "flow_rate_ml_min",
            "adsorbent_mass_mg",
            "column_length_mm",
            "column_diameter_mm",
            "dead_volume_ml",
            "detector",
            "regeneration_protocol",
            "cycle_count",
        ),
        predicate=_kind_stage_is(
            AdsorptionKind.BREAKTHROUGH, AdsorptionStage.EXECUTION
        ),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P8",
        description=(
            "breakthrough analysis records the computed property, the"
            " breakthrough criterion and the sampling validation; the"
            " raw/result to paper figure/result mapping is recorded as the"
            " results table (AC-03)"
        ),
        required_parameters=(
            "property",
            "criterion",
            "sampling_validation",
        ),
        predicate=_kind_stage_is(
            AdsorptionKind.BREAKTHROUGH, AdsorptionStage.ANALYSIS
        ),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P9",
        description=(
            "cycling/stability execution records the adsorbate, the"
            " temperature and the pressure (AC-01), the cycle count, the"
            " regeneration protocol and the atmosphere (cycling/"
            " reusability and stability testing conditions)"
        ),
        required_parameters=(
            "adsorbate",
            "temperature_K",
            "pressure_kPa",
            "cycle_count",
            "regeneration_protocol",
            "atmosphere",
        ),
        predicate=_kind_stage_is(
            AdsorptionKind.CYCLING_STABILITY, AdsorptionStage.EXECUTION
        ),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P10",
        description=(
            "cycling/stability analysis records the computed property,"
            " the retention criterion, the reference value and the"
            " comparison tolerance (e.g. capacity retention versus the"
            " reference capacity)"
        ),
        required_parameters=(
            "property",
            "criterion",
            "reference_value",
            "tolerance",
        ),
        predicate=_kind_stage_is(
            AdsorptionKind.CYCLING_STABILITY, AdsorptionStage.ANALYSIS
        ),
    ),
    AdsorptionParameterRule(
        rule_id="R-ADS-P0",
        description=(
            "no rule declares a required parameter set for this (kind,"
            " stage) pair (total default; covers the pairs without a raw"
            " execution in this pack -- IAST execution and Qst execution"
            " use the single-component isotherm execution surface)"
        ),
        required_parameters=(),
        predicate=lambda kind, stage: True,
    ),
)


def validate_adsorption_rulesets() -> tuple[str, ...]:
    """Validate the template rule tables' integrity; return the ids.

    A valid parameter table is non-empty, has unique rule ids, declares a
    rule for every (kind, stage) pair (the evaluation is a total function
    of the pair), and its trailing rule matches every pair (the total
    default that guarantees first-match evaluation is total). The value
    table has unique rule ids, exactly one rule per parameter name, and a
    rule for every required parameter name of the parameter table.

    Raises:
        InvalidAdsorptionTemplateError: a table violates the frozen shape
            (stable messages).
    """
    parameter_ids = tuple(rule.rule_id for rule in ADSORPTION_PARAMETER_RULES)
    value_ids = tuple(rule.rule_id for rule in ADSORPTION_VALUE_RULES)
    for label, ids in (("parameter", parameter_ids), ("value", value_ids)):
        duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if duplicates:
            raise InvalidAdsorptionTemplateError(
                f"duplicate rule id(s) in the {label} rule table:"
                f" {', '.join(duplicates)}"
            )
    if not parameter_ids:
        raise InvalidAdsorptionTemplateError(
            "the required-parameter rule table must not be empty"
        )
    pairs = [
        (kind, stage)
        for kind in AdsorptionKind
        for stage in AdsorptionStage
    ]
    covered = {
        (kind, stage)
        for rule in ADSORPTION_PARAMETER_RULES
        for kind, stage in pairs
        if rule.predicate(kind, stage)
    }
    if covered != set(pairs):
        missing = sorted(
            f"{kind.value}/{stage.value}" for kind, stage in pairs
            if (kind, stage) not in covered
        )
        raise InvalidAdsorptionTemplateError(
            "the required-parameter rule table must cover every (kind,"
            f" stage) pair, missing: {', '.join(missing)}"
        )
    default_rule = ADSORPTION_PARAMETER_RULES[-1]
    for kind, stage in pairs:
        if not default_rule.predicate(kind, stage):
            raise InvalidAdsorptionTemplateError(
                f"the trailing rule {default_rule.rule_id!r} is not a total"
                f" default: it does not match kind {kind.value!r} stage"
                f" {stage.value!r}"
            )
    value_parameters = [rule.parameter for rule in ADSORPTION_VALUE_RULES]
    duplicated_parameters = sorted(
        {
            parameter
            for parameter in value_parameters
            if value_parameters.count(parameter) > 1
        }
    )
    if duplicated_parameters:
        raise InvalidAdsorptionTemplateError(
            "the value rule table declares more than one rule for"
            f" parameter(s): {', '.join(duplicated_parameters)}"
        )
    value_ruled = set(value_parameters)
    unrouted = sorted(
        {
            parameter
            for rule in ADSORPTION_PARAMETER_RULES
            for parameter in rule.required_parameters
            if parameter not in value_ruled
        }
    )
    if unrouted:
        raise InvalidAdsorptionTemplateError(
            "the value rule table must declare a rule for every required"
            f" parameter, missing: {', '.join(unrouted)}"
        )
    return (*parameter_ids, *value_ids)


# ---------------------------------------------------------------------------
# Template assessments (recorded rule decisions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterCompletenessDecision:
    """Record of one required-parameter rule evaluation for a template."""

    rule_id: str
    description: str
    matched: bool
    required_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...]


@dataclass(frozen=True)
class ParameterCompletenessAssessment:
    """Full, auditable result of a template's parameter-completeness check.

    ``matched_rule_id`` names the deciding rule (``None`` is impossible:
    the trailing default rule always matches); ``missing_parameters`` are
    the required scientific parameters of the (kind, stage) pair that the
    template does not record -- the exact input of the Assumption
    Registry routing (AC-01).
    """

    template_id: str
    kind: AdsorptionKind
    stage: AdsorptionStage
    present_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...]
    decisions: tuple[ParameterCompletenessDecision, ...]
    matched_rule_id: str
    ruleset_version: str = ADSORPTION_RULESET_VERSION


@dataclass(frozen=True)
class ValueValidationDecision:
    """Record of one universal value-rule evaluation for a template."""

    rule_id: str
    description: str
    parameter: str
    applied: bool
    valid: bool
    violation: str | None


@dataclass(frozen=True)
class ValueValidationAssessment:
    """Full, auditable result of a template's value validation.

    ``violations`` carries the stable messages of every violated rule
    (empty when the template's present parameters all satisfy the
    universal value rules). ``matched_rule_id`` is the id of the first
    violation in table order (``None`` when no rule is violated).
    """

    template_id: str
    violations: tuple[str, ...]
    decisions: tuple[ValueValidationDecision, ...]
    matched_rule_id: str | None
    ruleset_version: str = ADSORPTION_RULESET_VERSION


# ---------------------------------------------------------------------------
# Breakthrough results table (AC-03: raw/result -> paper figure mapping)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaperResultEntry:
    """One raw/result to paper figure/result mapping entry (AC-03).

    ``result_key`` is the raw/result metadata key recorded on the
    template (a safe single registry path segment -- the same safe-id
    discipline as template ids, FND-M9-G02-01); ``figure_ref`` names the
    formal paper figure/result the key maps to (instance data: the exact
    reference is inventoried from the paper/SI); ``description`` states
    what the key records. The mapping is representable metadata, never a
    worker judgment.

    Raises:
        TypeError: a field has the wrong type.
        InvalidBreakthroughResultError: a value violation (empty name,
            unsafe result key).
    """

    result_key: str
    figure_ref: str
    description: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("result_key", self.result_key),
            ("figure_ref", self.figure_ref),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"PaperResultEntry.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidBreakthroughResultError(
                    f"PaperResultEntry.{field_name} must be a non-empty"
                    f" string, got {value!r}"
                )
        _validate_result_key(self.result_key)

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (protocol-capture shape)."""
        return {
            "result_key": self.result_key,
            "figure_ref": self.figure_ref,
            "description": self.description,
        }


@dataclass(frozen=True)
class BreakthroughResultTable:
    """The raw/result to paper figure/result mapping of a breakthrough (AC-03).

    ``entries`` holds one :class:`PaperResultEntry` per mapped raw/result
    metadata key; the table must be non-empty and the result keys unique
    (the mapping is one-to-one). The table is carried on the breakthrough
    template and captured deterministically by :func:`capture_protocol`
    (:func:`paper_mapping` is the pure key -> figure reference view).

    Raises:
        TypeError: ``entries`` is not a tuple of ``PaperResultEntry``.
        InvalidBreakthroughResultError: the table is empty or the result
            keys are not unique.
    """

    entries: tuple[PaperResultEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.entries, tuple) or not self.entries:
            raise TypeError(
                "BreakthroughResultTable.entries must be a non-empty tuple"
                " of PaperResultEntry records"
            )
        if not all(isinstance(entry, PaperResultEntry) for entry in self.entries):
            raise TypeError(
                "BreakthroughResultTable.entries entries must be"
                " PaperResultEntry records"
            )
        keys = [entry.result_key for entry in self.entries]
        duplicated = sorted({key for key in keys if keys.count(key) > 1})
        if duplicated:
            raise InvalidBreakthroughResultError(
                "BreakthroughResultTable result keys must be unique,"
                f" duplicated: {', '.join(duplicated)}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (protocol-capture shape)."""
        return {
            "entries": [
                entry.as_dict()
                for entry in sorted(
                    self.entries, key=lambda entry: entry.result_key
                )
            ]
        }


def paper_mapping(table: BreakthroughResultTable) -> dict[str, str]:
    """The deterministic raw/result key -> paper figure reference mapping.

    Pure: the mapping is a pure function of the results table, keyed by
    result key in sorted order -- the same view
    :func:`capture_protocol` records, so breakthrough raw/result metadata
    maps to formal paper figures/results byte-identically across calls.

    Raises:
        TypeError: ``table`` is not a ``BreakthroughResultTable``.
    """
    if not isinstance(table, BreakthroughResultTable):
        raise TypeError(
            "table must be a BreakthroughResultTable, got"
            f" {type(table).__name__}"
        )
    return dict(
        sorted(
            (entry.result_key, entry.figure_ref) for entry in table.entries
        )
    )


def _validate_result_key(value: str) -> None:
    """Reject result keys that escape registries or break glob listings.

    Safe single registry path segment (FND-M9-G02-01 lesson): no path
    separators, no glob metacharacters, not empty, not ``.``/``..``.
    """
    if value in (".", ".."):
        raise InvalidBreakthroughResultError(
            f"PaperResultEntry.result_key must be a non-empty safe"
            f" registry id, got {value!r}"
        )
    if "/" in value or "\\" in value:
        raise InvalidBreakthroughResultError(
            "PaperResultEntry.result_key must be a safe single path"
            f" segment (no '/', no '\\'), got {value!r}"
        )
    if any(char.isspace() for char in value):
        raise InvalidBreakthroughResultError(
            "PaperResultEntry.result_key must not contain whitespace, got"
            f" {value!r}"
        )
    if any(char in value for char in "*?[]"):
        raise InvalidBreakthroughResultError(
            "PaperResultEntry.result_key must not contain glob"
            f" metacharacters, got {value!r}"
        )


# ---------------------------------------------------------------------------
# The templates (frozen dataclasses, strict __post_init__)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdsorptionTemplateBase:
    """Frozen base of every BET/adsorption/IAST/Qst/breakthrough/cycling template.

    Common shape: a safe ``template_id``, a title, the ``AdsorptionStage``
    (``EXECUTION`` or ``ANALYSIS`` -- the separate metadata surfaces of
    AC-02), the adsorption ``kind``, the strict/recovery ``track`` label
    (frozen ``GoalTrack`` vocabulary, AC-01), the recorded scientific
    parameters (instance data -- gas names, temperatures, pressures,
    compositions, flows and column conditions live here, never in the
    rule tables, AC-03), the Assumption Registry refs of routed missing
    parameters (AC-01), the freeze flag, the breakthrough raw/result to
    paper figure/result mapping table (AC-03; ``None`` when not recorded,
    and only breakthrough templates may carry one) and the notes.

    Construction enforces the universal value rules over the parameters
    that are present; required parameters may be missing -- they are the
    input of the Assumption Registry pathway, not a construction error.
    Nothing is ever frozen by construction: the only way to produce a
    frozen template is :func:`freeze_adsorption_template`, gated by the
    Supervisor-only permission (``core/permissions.py``).

    The ``stage`` field precedes ``kind`` in the declaration order so
    subclasses can fix ``kind`` with a default (frozen-dataclass field
    ordering: a defaulted field must not precede a defaultless one).

    Raises:
        TypeError: a field has the wrong type.
        InvalidAdsorptionTemplateError: a value violation (unsafe
            template id, value-rule violation, unknown kind for the
            class, results table on a non-breakthrough template).
    """

    template_id: str
    title: str
    stage: AdsorptionStage
    kind: AdsorptionKind
    track: GoalTrack = GoalTrack.STRICT_REPRODUCTION
    parameters: dict[str, Any] = field(default_factory=dict)
    assumption_refs: tuple[str, ...] = ()
    frozen: bool = False
    results: BreakthroughResultTable | None = None
    notes: str | None = None

    #: The kinds this template class accepts (subclasses narrow this).
    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = tuple(
        AdsorptionKind
    )

    def __post_init__(self) -> None:
        if not isinstance(self.template_id, str):
            raise TypeError(
                f"{type(self).__name__}.template_id must be a str, got"
                f" {type(self.template_id).__name__}"
            )
        if not isinstance(self.title, str):
            raise TypeError(
                f"{type(self).__name__}.title must be a str, got"
                f" {type(self.title).__name__}"
            )
        if not self.title.strip():
            raise InvalidAdsorptionTemplateError(
                f"{type(self).__name__}.title must be a non-empty string,"
                f" got {self.title!r}"
            )
        if not isinstance(self.stage, AdsorptionStage):
            raise TypeError(
                f"{type(self).__name__}.stage must be an AdsorptionStage"
                f" member, got {type(self.stage).__name__}"
            )
        if not isinstance(self.kind, AdsorptionKind):
            raise TypeError(
                f"{type(self).__name__}.kind must be an AdsorptionKind"
                f" member, got {type(self.kind).__name__}"
            )
        _validate_template_id(type(self).__name__, self.template_id)
        if self.kind not in type(self)._ALLOWED_KINDS:
            allowed = ", ".join(kind.value for kind in type(self)._ALLOWED_KINDS)
            raise InvalidAdsorptionTemplateError(
                f"{type(self).__name__} does not accept adsorption kind"
                f" {self.kind.value!r}; allowed kinds: {allowed}"
            )
        if not isinstance(self.track, GoalTrack):
            raise TypeError(
                f"{type(self).__name__}.track must be a GoalTrack member,"
                f" got {type(self.track).__name__}"
            )
        if not isinstance(self.parameters, dict):
            raise TypeError(
                f"{type(self).__name__}.parameters must be a dict, got"
                f" {type(self.parameters).__name__}"
            )
        if not isinstance(self.assumption_refs, tuple) or not all(
            isinstance(ref, str) for ref in self.assumption_refs
        ):
            raise TypeError(
                f"{type(self).__name__}.assumption_refs must be a tuple of"
                " strings"
            )
        if not isinstance(self.frozen, bool):
            raise TypeError(
                f"{type(self).__name__}.frozen must be a bool, got"
                f" {type(self.frozen).__name__}"
            )
        if self.results is not None and not isinstance(
            self.results, BreakthroughResultTable
        ):
            raise TypeError(
                f"{type(self).__name__}.results must be a"
                " BreakthroughResultTable or None, got"
                f" {type(self.results).__name__}"
            )
        if (
            self.results is not None
            and self.kind is not AdsorptionKind.BREAKTHROUGH
        ):
            raise InvalidAdsorptionTemplateError(
                "the raw/result to paper figure mapping table is a"
                " breakthrough contract, but template"
                f" {self.template_id!r} has adsorption kind"
                f" {self.kind.value!r}"
            )
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError(
                f"{type(self).__name__}.notes must be a str or None, got"
                f" {type(self.notes).__name__}"
            )
        for parameter in self.parameters:
            if not isinstance(parameter, str):
                raise TypeError(
                    f"{type(self).__name__}.parameters keys must be"
                    f" strings, got {type(parameter).__name__}"
                )
        # Defensive copy: the frozen template owns its parameter table, so
        # mutating the caller's dict can never leak into the template.
        object.__setattr__(self, "parameters", dict(self.parameters))
        assessment = validate_template_values(self)
        if assessment.violations:
            details = "; ".join(assessment.violations)
            raise InvalidAdsorptionTemplateError(
                f"invalid {self.stage.value} {self.kind.value} template"
                f" {self.template_id!r}: {details}"
            )


@dataclass(frozen=True)
class BetTemplate(AdsorptionTemplateBase):
    """BET surface-area analysis template (16-...DOMAIN-PACK SS5 BET).

    Fixed ``kind`` ``BET``; the ``stage`` selects the raw isotherm
    acquisition surface (adsorbate, temperature, sample mass) or the
    analysis surface (property, fitting model, relative-pressure range).
    The analysis surface is frozen independently of the raw execution
    surface (AC-02).
    """

    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = (
        AdsorptionKind.BET,
    )

    kind: AdsorptionKind = AdsorptionKind.BET


@dataclass(frozen=True)
class SingleComponentTemplate(AdsorptionTemplateBase):
    """Single-component gas adsorption template (16-...DOMAIN-PACK SS5).

    Fixed ``kind`` ``SINGLE_COMPONENT``; the ``stage`` selects the raw
    isotherm-point execution surface (adsorbate, temperature, pressure --
    AC-01) or the analysis surface (property, fitting model, convergence
    and uncertainty validation).
    """

    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = (
        AdsorptionKind.SINGLE_COMPONENT,
    )

    kind: AdsorptionKind = AdsorptionKind.SINGLE_COMPONENT


@dataclass(frozen=True)
class IastTemplate(AdsorptionTemplateBase):
    """IAST multi-component selectivity analysis template (AC-02).

    Fixed ``kind`` ``IAST``; the analysis surface records the adsorbate
    pair, the mixture composition, the temperature and the pressure
    (AC-01). The raw execution surface of IAST is the single-component
    isotherm execution template of this pack (R-ADS-P0).
    """

    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = (
        AdsorptionKind.IAST,
    )

    kind: AdsorptionKind = AdsorptionKind.IAST


@dataclass(frozen=True)
class QstTemplate(AdsorptionTemplateBase):
    """Isosteric heat (Qst) analysis template (AC-02).

    Fixed ``kind`` ``QST``; the analysis surface records the adsorbate,
    the low/high temperatures of the isosteric pair and the reference
    loading. The raw execution surface of Qst is the single-component
    isotherm execution template of this pack (R-ADS-P0).
    """

    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = (
        AdsorptionKind.QST,
    )

    kind: AdsorptionKind = AdsorptionKind.QST


@dataclass(frozen=True)
class BreakthroughTemplate(AdsorptionTemplateBase):
    """Dynamic breakthrough template (16-...DOMAIN-PACK SS5, AC-01, AC-03).

    Fixed ``kind`` ``BREAKTHROUGH``; the execution surface records the
    gas pair and composition, the temperature and the pressure, the flow
    rate, the column geometry, the adsorbent mass, the dead volume, the
    detector and the regeneration/cycling protocols; the analysis surface
    records the property, criterion and sampling validation and may carry
    the raw/result to paper figure/result mapping table
    (:class:`BreakthroughResultTable`, AC-03).
    """

    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = (
        AdsorptionKind.BREAKTHROUGH,
    )

    kind: AdsorptionKind = AdsorptionKind.BREAKTHROUGH


@dataclass(frozen=True)
class CyclingStabilityTemplate(AdsorptionTemplateBase):
    """Cycling/reusability and stability testing template (AC-01).

    Fixed ``kind`` ``CYCLING_STABILITY``; the execution surface records
    the adsorbate, the temperature and the pressure, the cycle count, the
    regeneration protocol and the atmosphere; the analysis surface
    records the property, the retention criterion, the reference value
    and the comparison tolerance.
    """

    _ALLOWED_KINDS: ClassVar[tuple[AdsorptionKind, ...]] = (
        AdsorptionKind.CYCLING_STABILITY,
    )

    kind: AdsorptionKind = AdsorptionKind.CYCLING_STABILITY


def _validate_template_id(class_name: str, value: str) -> None:
    """Reject template ids that escape registries or break glob listings.

    Safe single registry path segment (FND-M9-G02-01 lesson): no path
    separators, no glob metacharacters, not empty, not ``.``/``..``.
    """
    if not value.strip() or value in (".", ".."):
        raise InvalidAdsorptionTemplateError(
            f"{class_name}.template_id must be a non-empty safe registry"
            f" id, got {value!r}"
        )
    if "/" in value or "\\" in value:
        raise InvalidAdsorptionTemplateError(
            f"{class_name}.template_id must be a safe single path segment"
            f" (no '/', no '\\'), got {value!r}"
        )
    if any(char.isspace() for char in value):
        raise InvalidAdsorptionTemplateError(
            f"{class_name}.template_id must not contain whitespace, got"
            f" {value!r}"
        )
    if any(char in value for char in "*?[]"):
        raise InvalidAdsorptionTemplateError(
            f"{class_name}.template_id must not contain glob"
            f" metacharacters, got {value!r}"
        )


# ---------------------------------------------------------------------------
# Universal evaluation (pure and deterministic)
# ---------------------------------------------------------------------------


def _rule_for_kind_stage(
    kind: AdsorptionKind, stage: AdsorptionStage
) -> AdsorptionParameterRule:
    """The required-parameter rule of a (kind, stage) pair (first match)."""
    for rule in ADSORPTION_PARAMETER_RULES:
        if rule.predicate(kind, stage):
            return rule
    # The trailing total default always matches (validate_adsorption_rulesets
    # guarantees it); this line is unreachable.
    return ADSORPTION_PARAMETER_RULES[-1]


def assess_parameter_completeness(
    template: AdsorptionTemplateBase,
) -> ParameterCompletenessAssessment:
    """Evaluate a template's required-scientific-parameter completeness.

    Pure and deterministic: the assessment is a pure function of the
    template's kind, stage and recorded parameter names, decided by the
    ordered ``ADSORPTION_PARAMETER_RULES`` table (first match wins; the
    trailing default rule always matches). The assessment records every
    rule decision, the matched rule id and the missing parameters.

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``.
    """
    if not isinstance(template, AdsorptionTemplateBase):
        raise TypeError(
            "template must be an AdsorptionTemplateBase, got"
            f" {type(template).__name__}"
        )
    recorded = set(template.parameters)
    decisions: list[ParameterCompletenessDecision] = []
    matched_rule_id: str | None = None
    matched_required: tuple[str, ...] = ()
    for rule in ADSORPTION_PARAMETER_RULES:
        matched = rule.predicate(template.kind, template.stage)
        candidate_missing = (
            tuple(
                parameter
                for parameter in rule.required_parameters
                if parameter not in recorded
            )
            if matched
            else ()
        )
        decisions.append(
            ParameterCompletenessDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                matched=matched,
                required_parameters=rule.required_parameters,
                missing_parameters=candidate_missing,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_required = rule.required_parameters
    # The trailing default rule always matches, so this can never be None.
    assert matched_rule_id is not None
    missing = tuple(
        parameter for parameter in matched_required if parameter not in recorded
    )
    return ParameterCompletenessAssessment(
        template_id=template.template_id,
        kind=template.kind,
        stage=template.stage,
        present_parameters=tuple(sorted(recorded)),
        missing_parameters=missing,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


def missing_parameters(template: AdsorptionTemplateBase) -> tuple[str, ...]:
    """The required scientific parameters the template does not record.

    The exact input of the Assumption Registry routing (AC-01).

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``.
    """
    return assess_parameter_completeness(template).missing_parameters


def validate_template_values(
    template: AdsorptionTemplateBase,
) -> ValueValidationAssessment:
    """Validate the template's present parameter values by the universal table.

    Pure and deterministic: every ``ADSORPTION_VALUE_RULES`` rule whose
    parameter the template records is applied; violations are collected
    as stable messages (``matched_rule_id`` names the first violated rule
    in table order). The template constructor enforces this assessment;
    the public hook makes the decision auditable.

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``.
    """
    if not isinstance(template, AdsorptionTemplateBase):
        raise TypeError(
            "template must be an AdsorptionTemplateBase, got"
            f" {type(template).__name__}"
        )
    violations: list[str] = []
    matched_rule_id: str | None = None
    decisions: list[ValueValidationDecision] = []
    for rule in ADSORPTION_VALUE_RULES:
        if rule.parameter not in template.parameters:
            decisions.append(
                ValueValidationDecision(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    parameter=rule.parameter,
                    applied=False,
                    valid=True,
                    violation=None,
                )
            )
            continue
        valid = rule.predicate(template.parameters[rule.parameter])
        message = rule.message.format(value=template.parameters[rule.parameter])
        decisions.append(
            ValueValidationDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                parameter=rule.parameter,
                applied=True,
                valid=valid,
                violation=None if valid else message,
            )
        )
        if not valid:
            violations.append(f"{rule.parameter} ({rule.rule_id}): {message}")
            if matched_rule_id is None:
                matched_rule_id = rule.rule_id
    return ValueValidationAssessment(
        template_id=template.template_id,
        violations=tuple(violations),
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


def freeze_adsorption_template(
    template: AdsorptionTemplateBase, *, role: Role
) -> AdsorptionTemplateBase:
    """Freeze a template -- a Supervisor-only decision (DEV-M6-G03).

    Templates RECORD the strict/recovery label and may be proposed by
    Research/domain helpers, but freezing is gated by the frozen
    role-action matrix: the caller's role must be permitted the plan
    freeze action (``Action.PLAN_FREEZE``, granted only to the Supervisor
    by ``R-PRM-SUP1``). The pure function returns a frozen copy
    (``frozen`` True) of the template; nothing is ever frozen silently
    and the input template is never mutated. Because execution and
    analysis templates are separate instances, each surface is frozen
    independently (AC-02).

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``, or
            ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
    """
    if not isinstance(template, AdsorptionTemplateBase):
        raise TypeError(
            "template must be an AdsorptionTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    assessment = check_action_allowed(role, Action.PLAN_FREEZE)
    if not assessment.allowed:
        raise PermissionDeniedError(
            f"role {role.value!r} may not freeze adsorption template"
            f" {template.template_id!r}: freezing is a Supervisor-only"
            " decision (the plan-freeze action of the frozen role-action"
            " matrix)",
            assessment,
        )
    return replace(template, frozen=True)


# ---------------------------------------------------------------------------
# Assumption Registry routing (AC-01: the existing pathway, never a copy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingParameterRouting:
    """The Assumption Registry routing of a template's missing parameters.

    ``assumptions`` holds one real ``core.models.Assumption`` record per
    missing required scientific parameter; ``effects`` records, per
    assumption, the strict-status effect decided by the real
    ``core.rules.assumptions.assumption_effect`` API;
    ``strict_label_assessment`` is the real ``evaluate_strict_label``
    result over the routed assumption set; ``assumption_refs`` are the
    safe assumption ids the template carries
    (``AdsorptionTemplateBase.assumption_refs``).
    """

    template_id: str
    kind: AdsorptionKind
    stage: AdsorptionStage
    missing_parameters: tuple[str, ...]
    assumptions: tuple[Assumption, ...]
    effects: tuple[AssumptionEffectDecision, ...]
    strict_label_assessment: StrictLabelAssessment
    assumption_refs: tuple[str, ...]


def assumptions_for_missing_parameters(
    template: AdsorptionTemplateBase,
    *,
    classification: AssumptionClassification = AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
    rationale: str | None = None,
    source_refs: Sequence[str] = (),
    affected_goal_ids: Sequence[str] = (),
) -> MissingParameterRouting:
    """Route the template's missing scientific parameters through the real
    Assumption Registry pathway (AC-01).

    For every required scientific parameter the template does not record
    (a missing temperature, pressure, composition, flow or breakthrough
    column parameter), a real ``core.models.Assumption`` registry entry is
    constructed (deterministic safe assumption id derived from the
    template id and the parameter, ``core.ids.generate_id``), its
    strict-status effect is decided by the real
    ``core.rules.assumptions.assumption_effect`` and recorded on the
    entry, and the real ``core.rules.assumptions.evaluate_strict_label``
    reads the whole set back into the strict label. The default
    classification for a missing scientific parameter is
    ``A2_SCIENTIFIC_ASSUMPTION`` (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md
    SS5: missing critical column parameters enter the Assumption
    Registry; missing scientifically meaningful settings are A2 unless
    reliable method evidence supports an A1 classification); an explicit
    classification is accepted verbatim.

    Args:
        template: the template whose missing parameters are routed.
        classification: the frozen assumption classification of every
            routed parameter (default A2).
        rationale: the assumption rationale (a stable default names the
            template and the parameter when omitted).
        source_refs: source identifiers backing the assumption (A1
            classifications require reliable method evidence).
        affected_goal_ids: goal ids the assumption affects (optional).

    Returns:
        The full routing: the real assumption records, their real
        effects, the real strict label and the safe assumption refs.

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``,
            ``classification`` is not an ``AssumptionClassification``
            member, ``rationale`` is not a str or None, or a ref/affected
            goal id is not a str.
    """
    if not isinstance(template, AdsorptionTemplateBase):
        raise TypeError(
            "template must be an AdsorptionTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(classification, AssumptionClassification):
        raise TypeError(
            "classification must be an AssumptionClassification member, got"
            f" {type(classification).__name__}"
        )
    if rationale is not None and not isinstance(rationale, str):
        raise TypeError(
            f"rationale must be a str or None, got {type(rationale).__name__}"
        )
    _require_str_sequence(source_refs, "source_refs")
    _require_str_sequence(affected_goal_ids, "affected_goal_ids")

    missing = missing_parameters(template)
    assumptions: list[Assumption] = []
    for parameter in missing:
        assumption_id = generate_id("assumption", template.template_id, parameter)
        entry = Assumption(
            assumption_id=assumption_id,
            parameter=parameter,
            classification=classification,
            rationale=(
                rationale
                if rationale is not None
                else (
                    f"required {template.stage.value} scientific parameter"
                    f" {parameter!r} of {template.kind.value} adsorption"
                    f" template {template.template_id!r} is not recorded by"
                    " the published protocol and enters the Assumption"
                    " Registry"
                )
            ),
            source_refs=list(source_refs),
            affected_goal_ids=list(affected_goal_ids),
        )
        effect = assumption_effect(entry)
        assumptions.append(replace(entry, strict_status_effect=effect.effect))
    routed = tuple(assumptions)
    label_assessment = evaluate_strict_label(routed)
    return MissingParameterRouting(
        template_id=template.template_id,
        kind=template.kind,
        stage=template.stage,
        missing_parameters=missing,
        assumptions=routed,
        effects=tuple(assumption_effect(assumption) for assumption in routed),
        strict_label_assessment=label_assessment,
        assumption_refs=tuple(assumption.assumption_id for assumption in routed),
    )


def _require_str_sequence(values: Sequence[str], name: str) -> None:
    """Reject non-string entries of a ref sequence with a stable TypeError."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError(
            f"{name} must be a sequence of strings, got {type(values).__name__}"
        )
    for value in values:
        if not isinstance(value, str):
            raise TypeError(
                f"{name} entries must be strings, got {type(value).__name__}"
            )


def apply_assumption_routing(
    template: AdsorptionTemplateBase, routing: MissingParameterRouting
) -> AdsorptionTemplateBase:
    """Return the template carrying the routed assumption refs (AC-01).

    Pure: a frozen copy of the template with ``assumption_refs`` set to
    the routing's safe assumption ids; the input template and the routing
    are never mutated.

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``, or
            ``routing`` is not a ``MissingParameterRouting``.
    """
    if not isinstance(template, AdsorptionTemplateBase):
        raise TypeError(
            "template must be an AdsorptionTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(routing, MissingParameterRouting):
        raise TypeError(
            "routing must be a MissingParameterRouting, got"
            f" {type(routing).__name__}"
        )
    return replace(template, assumption_refs=routing.assumption_refs)


# ---------------------------------------------------------------------------
# Protocol capture (deterministic, pure)
# ---------------------------------------------------------------------------

#: The shape a captured protocol dict must carry (protocol capture
#: deliverable; consumed by downstream execution-package builders).
CAPTURE_KEYS: tuple[str, ...] = (
    "template_id",
    "title",
    "stage",
    "kind",
    "track",
    "frozen",
    "parameter_table",
    "assumption_refs",
    "results",
    "notes",
)


def capture_protocol(template: AdsorptionTemplateBase) -> dict[str, Any]:
    """Capture the template as a deterministic protocol dict.

    Pure: the capture is a pure function of the template -- sorted
    parameter table, the strict/recovery track label, the stage and kind,
    the freeze state, the assumption refs of the routed missing
    parameters and the breakthrough raw/result to paper figure/result
    mapping table (AC-03: the mapping is recorded here deterministically,
    so breakthrough raw/result metadata maps to the paper's figures/
    results byte-identically across calls). Same template -> identical
    capture on every call and platform.

    Raises:
        TypeError: ``template`` is not an ``AdsorptionTemplateBase``.
    """
    if not isinstance(template, AdsorptionTemplateBase):
        raise TypeError(
            "template must be an AdsorptionTemplateBase, got"
            f" {type(template).__name__}"
        )
    return {
        "template_id": template.template_id,
        "title": template.title,
        "stage": template.stage.value,
        "kind": template.kind.value,
        "track": template.track.value,
        "frozen": template.frozen,
        "parameter_table": [
            {"parameter": parameter, "value": template.parameters[parameter]}
            for parameter in sorted(template.parameters)
        ],
        "assumption_refs": list(template.assumption_refs),
        "results": (
            None if template.results is None else template.results.as_dict()
        ),
        "notes": template.notes,
    }
