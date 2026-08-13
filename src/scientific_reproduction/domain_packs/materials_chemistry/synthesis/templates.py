"""Materials-chemistry synthesis unit-process templates (DEV-M11-G01).

Implements the **synthesis Unit Process templates** deliverable of
DEV-M11-G01 for the materials-chemistry domain pack: parameterized,
frozen dataclass templates for ligand/material/MOF synthesis, thermal
activation and solvent exchange, plus independent-batch replication
defaults and deterministic protocol capture. Grounded in:

* ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS2 (v0.1 experimental
  capability families: ligand/precursor synthesis; MOF/material
  synthesis; solvent exchange/activation; sample handling and
  independent batch logic) and SS5 (domain acceptance examples are
  *templates*, never universal thresholds; "Missing critical column
  parameters enter Assumption Registry"; "Missing scientifically
  meaningful settings are A2 unless reliable method evidence supports an
  A1 classification");
* ``08-STRICT-RECOVERY-CLOSURE.md`` SS1 (tracks: ``STRICT_REPRODUCTION`` /
  ``RECOVERY`` / ``METHOD_REDESIGN``) and SS3 (the Assumption Registry:
  every non-explicit parameter is registered as
  ``A0_TECHNICAL_DEFAULT`` / ``A1_METHODOLOGICAL_DEFAULT`` /
  ``A2_SCIENTIFIC_ASSUMPTION``; A2 must not be silently used inside
  strict reproduction);
* ``07-STATISTICS-AND-ACCEPTANCE.md`` SS2 (experimental Goals require
  independent replicates by default with default floor ``n >= 3`` --
  ``analysis/replication.py`` ``DEFAULT_MIN_INDEPENDENT``);
* ``core/models.py`` -- the frozen vocabulary reused verbatim:
  ``GoalTrack`` (the strict/recovery track label), ``GoalReplication``
  (the independent-batch shape), ``RunType.INDEPENDENT_REPLICATE`` (the
  independent-Run label), ``Assumption`` / ``AssumptionClassification``
  (the Assumption Registry entry);
* ``core/rules/assumptions.py`` -- the EXISTING Assumption Registry
  evaluation API (``assumption_effect`` / ``evaluate_strict_label``):
  missing scientific parameters are routed through it, never through a
  parallel store;
* ``core/permissions.py`` (DEV-M6-G03) -- the role-action matrix:
  templates are proposed by Research/domain helpers and RECORD the
  strict/recovery label, but freezing is Supervisor-only; the freeze
  helper is gated by the matrix (``Action.PLAN_FREEZE``, granted only to
  the Supervisor), so nothing is ever silently frozen;
* ``17-FDM201-REFERENCE-CASE.md`` WP-10/WP-20 (ligand precursor
  synthesis, solvothermal MOF synthesis, solvent exchange, activation,
  independent batch synthesis Runs) -- the FDM-201 reference case the
  templates model. AC-03: FDM-201-specific chemistry may appear only as
  **instance data** inside template parameter values; the rule tables
  below are universal (no reagent names, no synthesis conditions).

Template model (determinism and boundaries)
-------------------------------------------
Every template is a frozen dataclass with strict ``__post_init__``
validation: ``TypeError`` at the type boundaries (template id, track,
replication defaults, parameters, ...), ``ValueError``-subclass stable
errors (``InvalidTemplateError`` and siblings) for value violations.
Construction enforces the **universal value rules** of the ordered,
versioned ``TEMPLATE_VALUE_RULES`` table over the parameters that ARE
present, and validates ids as safe single registry path segments (the
FND-M9-G02-01 lesson: no path separators, no glob metacharacters).
Missing scientific parameters are NOT a construction error: they are the
input to the Assumption Registry pathway (AC-02) -- the ordered
``TEMPLATE_PARAMETER_RULES`` table declares, per unit-process kind, the
required scientific parameter set, and
:func:`assumptions_for_missing_parameters` routes every missing required
parameter through the real ``core.models.Assumption`` record and the
real ``core.rules.assumptions`` evaluation API, returning the exact
assumptions, their recorded strict-status effects and the strict label,
with the assumption refs carried on the template.

The templates support strict/recovery labeling (AC-01): every template
carries a ``track`` drawn from the frozen ``GoalTrack`` vocabulary
(``STRICT_REPRODUCTION`` / ``RECOVERY`` / ``METHOD_REDESIGN``), the
independent-batch defaults mirror the frozen ``GoalReplication`` shape
with the frozen ``n >= 3`` floor family, and
:func:`plan_independent_batches` plans deterministic independent Runs
labeled with the frozen ``RunType.INDEPENDENT_REPLICATE``.

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

from scientific_reproduction.analysis.replication import DEFAULT_MIN_INDEPENDENT
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
    GoalReplication,
    GoalTrack,
    RunType,
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

__all__ = [
    "ACTIVATION_KIND",
    "BATCH_FLOOR_RULES",
    "BatchFloorAssessment",
    "BatchFloorDecision",
    "BatchFloorRule",
    "BatchPlan",
    "BatchReplicationDefaults",
    "CAPTURE_KEYS",
    "CONTROLLED_ATMOSPHERES",
    "INDEPENDENT_FLOOR",
    "InvalidBatchPlanError",
    "InvalidBatchReplicationError",
    "InvalidTemplateError",
    "LIGAND_KIND",
    "MATERIAL_KIND",
    "MOF_KIND",
    "MissingParameterRouting",
    "ParameterCompletenessAssessment",
    "ParameterCompletenessDecision",
    "SOLVENT_EXCHANGE_KIND",
    "SYNTHESIS_RULESET_VERSION",
    "SolventExchangeTemplate",
    "SynthesisTemplateBase",
    "SynthesisTemplateError",
    "SynthesisUnitProcessKind",
    "SynthesisUnitProcessTemplate",
    "ActivationTemplate",
    "TEMPLATE_PARAMETER_RULES",
    "TEMPLATE_VALUE_RULES",
    "TemplateParameterRule",
    "TemplateValueRule",
    "UnknownUnitProcessError",
    "ValueValidationAssessment",
    "ValueValidationDecision",
    "apply_assumption_routing",
    "assess_parameter_completeness",
    "assumptions_for_missing_parameters",
    "capture_protocol",
    "evaluate_batch_floor",
    "freeze_synthesis_template",
    "missing_parameters",
    "plan_independent_batches",
    "validate_synthesis_rulesets",
    "validate_template_values",
]

#: Version of the template rule tables. Bumped whenever a rule changes;
#: recorded in every assessment so old decisions stay interpretable.
SYNTHESIS_RULESET_VERSION: str = "1.0"

#: The frozen independent-n floor family of 07-STATISTICS-AND-ACCEPTANCE.md
#: SS2 (the same constant the analysis replication evaluator uses).
INDEPENDENT_FLOOR: int = DEFAULT_MIN_INDEPENDENT


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class SynthesisTemplateError(ValueError):
    """Base class for all synthesis-template errors."""


class InvalidTemplateError(SynthesisTemplateError):
    """Raised when a template violates a universal value rule or shape rule."""


class UnknownUnitProcessError(SynthesisTemplateError):
    """Raised when no rule declares a required parameter set for a kind."""


class InvalidBatchReplicationError(SynthesisTemplateError):
    """Raised when batch-replication defaults violate the frozen floor family."""


class InvalidBatchPlanError(SynthesisTemplateError):
    """Raised when an independent-batch plan request is invalid."""


# ---------------------------------------------------------------------------
# Unit-process kind vocabulary (the domain-pack capability families)
# ---------------------------------------------------------------------------


class SynthesisUnitProcessKind(StrEnum):
    """The unit-process kinds the templates parameterize.

    Values follow ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS2 family
    names (ligand/precursor synthesis; MOF/material synthesis; solvent
    exchange/activation). This is domain-pack vocabulary, distinct from
    the frozen core vocabulary -- the kinds are the keys of the universal
    ``TEMPLATE_PARAMETER_RULES`` table, never chemistry instances.
    """

    LIGAND_SYNTHESIS = "ligand_synthesis"
    MATERIAL_SYNTHESIS = "material_synthesis"
    MOF_SYNTHESIS = "mof_synthesis"
    ACTIVATION = "activation"
    SOLVENT_EXCHANGE = "solvent_exchange"


#: Convenience aliases for the five unit-process kinds.
LIGAND_KIND: SynthesisUnitProcessKind = SynthesisUnitProcessKind.LIGAND_SYNTHESIS
MATERIAL_KIND: SynthesisUnitProcessKind = SynthesisUnitProcessKind.MATERIAL_SYNTHESIS
MOF_KIND: SynthesisUnitProcessKind = SynthesisUnitProcessKind.MOF_SYNTHESIS
ACTIVATION_KIND: SynthesisUnitProcessKind = SynthesisUnitProcessKind.ACTIVATION
SOLVENT_EXCHANGE_KIND: SynthesisUnitProcessKind = (
    SynthesisUnitProcessKind.SOLVENT_EXCHANGE
)

#: The controlled atmosphere vocabulary of the universal value rules.
#: Universal handling vocabulary -- a template records which atmosphere a
#: synthesis/activation runs under; the value must be one of these names.
CONTROLLED_ATMOSPHERES: frozenset[str] = frozenset(
    {"air", "argon", "nitrogen", "vacuum", "sealed"}
)


# ---------------------------------------------------------------------------
# Independent-batch replication defaults (the frozen GoalReplication shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchReplicationDefaults:
    """Independent-batch defaults of a synthesis template (AC-01).

    Mirrors the frozen ``core.models.GoalReplication`` shape: independent
    batches by default, the frozen ``n >= 3`` floor family
    (``DEFAULT_MIN_INDEPENDENT``, 07-STATISTICS-AND-ACCEPTANCE.md SS2) as
    ``minimum_n``, and a planned-n policy string. An explicit floor may
    only be set to an integer ``>= 1`` (the same convention as
    ``analysis/replication.py``: an override can never weaken the floor
    below 1).

    Raises:
        TypeError: a field has the wrong type.
        InvalidBatchReplicationError: a value violation (empty policy,
            floor below 1, negative technical repeats).
    """

    independent_required: bool = True
    planned_n_policy: str = "independent_batches_then_dynamic_precision"
    minimum_n: int = INDEPENDENT_FLOOR
    technical_repeats: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.independent_required, bool):
            raise TypeError(
                "BatchReplicationDefaults.independent_required must be a"
                f" bool, got {type(self.independent_required).__name__}"
            )
        if not isinstance(self.planned_n_policy, str):
            raise TypeError(
                "BatchReplicationDefaults.planned_n_policy must be a str,"
                f" got {type(self.planned_n_policy).__name__}"
            )
        if not self.planned_n_policy.strip():
            raise InvalidBatchReplicationError(
                "BatchReplicationDefaults.planned_n_policy must be a"
                f" non-empty string, got {self.planned_n_policy!r}"
            )
        if not isinstance(self.minimum_n, int) or isinstance(self.minimum_n, bool):
            raise TypeError(
                "BatchReplicationDefaults.minimum_n must be an int, got"
                f" {type(self.minimum_n).__name__}"
            )
        if self.minimum_n < 1:
            raise InvalidBatchReplicationError(
                "BatchReplicationDefaults.minimum_n must be at least 1 (the"
                " floor can never be weakened below 1; the frozen default"
                f" is {INDEPENDENT_FLOOR}), got {self.minimum_n}"
            )
        if self.technical_repeats is not None:
            if not isinstance(self.technical_repeats, int) or isinstance(
                self.technical_repeats, bool
            ):
                raise TypeError(
                    "BatchReplicationDefaults.technical_repeats must be an"
                    " int or None, got"
                    f" {type(self.technical_repeats).__name__}"
                )
            if self.technical_repeats < 0:
                raise InvalidBatchReplicationError(
                    "BatchReplicationDefaults.technical_repeats must be"
                    f" non-negative, got {self.technical_repeats}"
                )

    def to_goal_replication(self) -> GoalReplication:
        """Map onto the frozen ``GoalReplication`` model (verbatim values)."""
        return GoalReplication(
            independent_required=self.independent_required,
            planned_n_policy=self.planned_n_policy,
            minimum_n=self.minimum_n,
            technical_repeats=self.technical_repeats,
        )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (protocol-capture shape)."""
        return {
            "independent_required": self.independent_required,
            "planned_n_policy": self.planned_n_policy,
            "minimum_n": self.minimum_n,
            "technical_repeats": self.technical_repeats,
        }


# ---------------------------------------------------------------------------
# Universal rule tables (AC-03: no material-specific chemistry anywhere)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateParameterRule:
    """One entry of the required-scientific-parameter rule table.

    Declares, per unit-process kind, the required scientific parameters a
    template of that kind must record (or route to the Assumption
    Registry pathway when missing). The parameter names are universal
    method-capture vocabulary -- no reagent names, no synthesis
    conditions (AC-03). The predicate is a pure function of the kind; the
    trailing total default always matches.
    """

    rule_id: str
    description: str
    required_parameters: tuple[str, ...]
    predicate: Callable[[SynthesisUnitProcessKind], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"TemplateParameterRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidTemplateError(
                    f"TemplateParameterRule.{field_name} must be a non-empty"
                    f" string, got {value!r}"
                )
        if not isinstance(self.required_parameters, tuple) or not all(
            isinstance(p, str) and p.strip() for p in self.required_parameters
        ):
            raise TypeError(
                "TemplateParameterRule.required_parameters must be a tuple"
                " of non-empty strings"
            )
        if not callable(self.predicate):
            raise TypeError(
                "TemplateParameterRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class TemplateValueRule:
    """One entry of the universal parameter-value rule table.

    Each rule validates the value of one named scientific parameter when
    the template records it. Predicates are pure functions of the value
    only; the message template is filled with the offending value. All
    rules are universal physics/handling rules -- thermodynamic
    temperatures positive, durations positive, pressures non-negative,
    stoichiometric ratios positive, a controlled atmosphere vocabulary, a
    non-empty solvent name (any solvent: no reagent-name table, AC-03).
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
                    f"TemplateValueRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidTemplateError(
                    f"TemplateValueRule.{field_name} must be a non-empty"
                    f" string, got {value!r}"
                )
        if not callable(self.predicate):
            raise TypeError(
                "TemplateValueRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


def _is_positive_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number strictly above zero."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _is_non_negative_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number >= 0."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _is_positive_integer(value: Any) -> bool:
    """True iff ``value`` is an int >= 1 (bool is not an int here)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_non_empty_string(value: Any) -> bool:
    """True iff ``value`` is a non-empty str."""
    return isinstance(value, str) and bool(value.strip())


def _is_controlled_atmosphere(value: Any) -> bool:
    """True iff ``value`` is a controlled-atmosphere name."""
    return isinstance(value, str) and value in CONTROLLED_ATMOSPHERES


def _kind_is(kind: SynthesisUnitProcessKind) -> Callable[[SynthesisUnitProcessKind], bool]:
    """A predicate matching exactly the given unit-process kind."""
    return lambda candidate: candidate is kind


#: The ordered, versioned universal value-rule table. Each named parameter
#: has exactly one rule (the table is a total function of parameter
#: names). Order is normative.
TEMPLATE_VALUE_RULES: tuple[TemplateValueRule, ...] = (
    TemplateValueRule(
        rule_id="R-TPL-V1",
        description=(
            "thermodynamic temperature is on the absolute scale: a"
            " recorded temperature must be a finite positive number of"
            " kelvin"
        ),
        parameter="temperature_K",
        predicate=_is_positive_number,
        message="temperature_K must be a finite positive number of kelvin,"
        " got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V2",
        description=(
            "a recorded synthesis duration must be a finite positive"
            " number of hours"
        ),
        parameter="duration_h",
        predicate=_is_positive_number,
        message="duration_h must be a finite positive number of hours, got"
        " {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V3",
        description=(
            "a recorded stoichiometric ratio must be a finite positive"
            " number"
        ),
        parameter="stoichiometry",
        predicate=_is_positive_number,
        message="stoichiometry must be a finite positive number, got"
        " {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V4",
        description=(
            "an activation temperature is on the absolute scale: it must"
            " be a finite positive number of kelvin"
        ),
        parameter="activation_temperature_K",
        predicate=_is_positive_number,
        message="activation_temperature_K must be a finite positive number"
        " of kelvin, got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V5",
        description=(
            "an activation duration must be a finite positive number of"
            " hours"
        ),
        parameter="activation_duration_h",
        predicate=_is_positive_number,
        message="activation_duration_h must be a finite positive number of"
        " hours, got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V6",
        description=(
            "a recorded atmosphere must be one of the controlled handling"
            " vocabulary"
        ),
        parameter="atmosphere",
        predicate=_is_controlled_atmosphere,
        message="atmosphere must be one of the controlled handling names"
        f" {sorted(CONTROLLED_ATMOSPHERES)}, got {{value!r}}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V7",
        description=(
            "a recorded pressure must be a finite non-negative number of"
            " mbar"
        ),
        parameter="pressure_mbar",
        predicate=_is_non_negative_number,
        message="pressure_mbar must be a finite non-negative number of"
        " mbar, got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V8",
        description=(
            "a recorded solvent exchange count must be an integer of at"
            " least one cycle"
        ),
        parameter="exchange_cycles",
        predicate=_is_positive_integer,
        message="exchange_cycles must be an integer >= 1, got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V9",
        description=(
            "a recorded soaking duration must be a finite positive number"
            " of hours"
        ),
        parameter="soaking_duration_h",
        predicate=_is_positive_number,
        message="soaking_duration_h must be a finite positive number of"
        " hours, got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V10",
        description=(
            "a recorded solvent must be a named solvent (any name; the"
            " rules never restrict which solvent -- AC-03)"
        ),
        parameter="solvent",
        predicate=_is_non_empty_string,
        message="solvent must be a non-empty solvent name, got {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V11",
        description=(
            "a recorded precursor must be a named precursor (any name; the"
            " rules never restrict which precursor -- AC-03)"
        ),
        parameter="precursor",
        predicate=_is_non_empty_string,
        message="precursor must be a non-empty precursor name, got"
        " {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V12",
        description=(
            "a recorded metal source must be a named metal source (any"
            " name; the rules never restrict which source -- AC-03)"
        ),
        parameter="metal_source",
        predicate=_is_non_empty_string,
        message="metal_source must be a non-empty metal-source name, got"
        " {value!r}",
    ),
    TemplateValueRule(
        rule_id="R-TPL-V13",
        description=(
            "a recorded organic linker must be a named linker (any name;"
            " the rules never restrict which linker -- AC-03)"
        ),
        parameter="organic_linker",
        predicate=_is_non_empty_string,
        message="organic_linker must be a non-empty linker name, got"
        " {value!r}",
    ),
)

#: The ordered required-scientific-parameter rule table, one rule per
#: unit-process kind, first match wins, trailing total default (AC-02:
#: missing required parameters route to the Assumption Registry pathway;
#: AC-03: the table is universal).
TEMPLATE_PARAMETER_RULES: tuple[TemplateParameterRule, ...] = (
    TemplateParameterRule(
        rule_id="R-TPL-P1",
        description=(
            "ligand synthesis records the precursor, the solvent, the"
            " reaction temperature, the duration and the stoichiometry"
        ),
        required_parameters=(
            "precursor",
            "solvent",
            "temperature_K",
            "duration_h",
            "stoichiometry",
        ),
        predicate=_kind_is(SynthesisUnitProcessKind.LIGAND_SYNTHESIS),
    ),
    TemplateParameterRule(
        rule_id="R-TPL-P2",
        description=(
            "material synthesis records the precursor, the solvent, the"
            " reaction temperature, the duration and the stoichiometry"
        ),
        required_parameters=(
            "precursor",
            "solvent",
            "temperature_K",
            "duration_h",
            "stoichiometry",
        ),
        predicate=_kind_is(SynthesisUnitProcessKind.MATERIAL_SYNTHESIS),
    ),
    TemplateParameterRule(
        rule_id="R-TPL-P3",
        description=(
            "MOF synthesis records the metal source, the organic linker,"
            " the solvent, the reaction temperature, the duration and the"
            " stoichiometry"
        ),
        required_parameters=(
            "metal_source",
            "organic_linker",
            "solvent",
            "temperature_K",
            "duration_h",
            "stoichiometry",
        ),
        predicate=_kind_is(SynthesisUnitProcessKind.MOF_SYNTHESIS),
    ),
    TemplateParameterRule(
        rule_id="R-TPL-P4",
        description=(
            "activation records the activation temperature, the"
            " activation duration, the atmosphere and the pressure"
        ),
        required_parameters=(
            "activation_temperature_K",
            "activation_duration_h",
            "atmosphere",
            "pressure_mbar",
        ),
        predicate=_kind_is(SynthesisUnitProcessKind.ACTIVATION),
    ),
    TemplateParameterRule(
        rule_id="R-TPL-P5",
        description=(
            "solvent exchange records the solvent, the exchange-cycle"
            " count, the temperature and the soaking duration"
        ),
        required_parameters=(
            "solvent",
            "exchange_cycles",
            "temperature_K",
            "soaking_duration_h",
        ),
        predicate=_kind_is(SynthesisUnitProcessKind.SOLVENT_EXCHANGE),
    ),
    TemplateParameterRule(
        rule_id="R-TPL-P0",
        description=(
            "no rule declares a required parameter set for this unit"
            " process kind (total default)"
        ),
        required_parameters=(),
        predicate=lambda kind: True,
    ),
)


def validate_synthesis_rulesets() -> tuple[str, ...]:
    """Validate the template rule tables' integrity; return the ids.

    A valid parameter table is non-empty, has unique rule ids, declares a
    rule for every unit-process kind (the evaluation is a total function
    of the kind), and its trailing rule matches every kind (the total
    default that guarantees first-match evaluation is total). The value
    table has unique rule ids and exactly one rule per parameter name.

    Raises:
        InvalidTemplateError: a table violates the frozen shape (stable
            messages).
    """
    parameter_ids = tuple(rule.rule_id for rule in TEMPLATE_PARAMETER_RULES)
    value_ids = tuple(rule.rule_id for rule in TEMPLATE_VALUE_RULES)
    for label, ids in (("parameter", parameter_ids), ("value", value_ids)):
        duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if duplicates:
            raise InvalidTemplateError(
                f"duplicate rule id(s) in the {label} rule table:"
                f" {', '.join(duplicates)}"
            )
    if not parameter_ids:
        raise InvalidTemplateError(
            "the required-parameter rule table must not be empty"
        )
    covered = {
        kind
        for rule in TEMPLATE_PARAMETER_RULES
        for kind in SynthesisUnitProcessKind
        if rule.predicate(kind)
    }
    if covered != set(SynthesisUnitProcessKind):
        missing = sorted(
            kind.value
            for kind in SynthesisUnitProcessKind
            if kind not in covered
        )
        raise InvalidTemplateError(
            "the required-parameter rule table must cover every unit"
            f" process kind, missing: {', '.join(missing)}"
        )
    default_rule = TEMPLATE_PARAMETER_RULES[-1]
    for kind in SynthesisUnitProcessKind:
        if not default_rule.predicate(kind):
            raise InvalidTemplateError(
                f"the trailing rule {default_rule.rule_id!r} is not a total"
                f" default: it does not match kind {kind.value!r}"
            )
    value_parameters = [rule.parameter for rule in TEMPLATE_VALUE_RULES]
    duplicated_parameters = sorted(
        {
            parameter
            for parameter in value_parameters
            if value_parameters.count(parameter) > 1
        }
    )
    if duplicated_parameters:
        raise InvalidTemplateError(
            "the value rule table declares more than one rule for"
            f" parameter(s): {', '.join(duplicated_parameters)}"
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
    the required scientific parameters of the kind that the template does
    not record -- the exact input of the Assumption Registry routing
    (AC-02).
    """

    template_id: str
    kind: SynthesisUnitProcessKind
    present_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...]
    decisions: tuple[ParameterCompletenessDecision, ...]
    matched_rule_id: str
    ruleset_version: str = SYNTHESIS_RULESET_VERSION


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
    ruleset_version: str = SYNTHESIS_RULESET_VERSION


# ---------------------------------------------------------------------------
# The templates (frozen dataclasses, strict __post_init__)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SynthesisTemplateBase:
    """Frozen base of every synthesis/activation/solvent-exchange template.

    Common shape: a safe ``template_id``, a title, the unit-process kind,
    the strict/recovery ``track`` label (frozen ``GoalTrack`` vocabulary,
    AC-01), the independent-batch replication defaults, the recorded
    scientific parameters (instance data -- material-specific values live
    here, never in the rule tables, AC-03), the Assumption Registry refs
    of routed missing parameters (AC-02) and the freeze flag.

    Construction enforces the universal value rules over the parameters
    that are present; required parameters may be missing -- they are the
    input of the Assumption Registry pathway, not a construction error.
    Nothing is ever frozen by construction: the only way to produce a
    frozen template is :func:`freeze_synthesis_template`, gated by the
    Supervisor-only permission (``core/permissions.py``).

    Raises:
        TypeError: a field has the wrong type.
        InvalidTemplateError: a value violation (unsafe template id,
            value-rule violation, unknown kind for the class).
    """

    template_id: str
    title: str
    unit_process_kind: SynthesisUnitProcessKind
    track: GoalTrack = GoalTrack.STRICT_REPRODUCTION
    replication: BatchReplicationDefaults = field(
        default_factory=BatchReplicationDefaults
    )
    parameters: dict[str, Any] = field(default_factory=dict)
    assumption_refs: tuple[str, ...] = ()
    frozen: bool = False
    notes: str | None = None

    #: The kinds this template class accepts (subclasses narrow this).
    _ALLOWED_KINDS: ClassVar[tuple[SynthesisUnitProcessKind, ...]] = tuple(
        SynthesisUnitProcessKind
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
            raise InvalidTemplateError(
                f"{type(self).__name__}.title must be a non-empty string,"
                f" got {self.title!r}"
            )
        if not isinstance(self.unit_process_kind, SynthesisUnitProcessKind):
            raise TypeError(
                f"{type(self).__name__}.unit_process_kind must be a"
                " SynthesisUnitProcessKind member, got"
                f" {type(self.unit_process_kind).__name__}"
            )
        _validate_template_id(type(self).__name__, self.template_id)
        if self.unit_process_kind not in type(self)._ALLOWED_KINDS:
            allowed = ", ".join(kind.value for kind in type(self)._ALLOWED_KINDS)
            raise InvalidTemplateError(
                f"{type(self).__name__} does not accept unit process kind"
                f" {self.unit_process_kind.value!r}; allowed kinds: {allowed}"
            )
        if not isinstance(self.track, GoalTrack):
            raise TypeError(
                f"{type(self).__name__}.track must be a GoalTrack member,"
                f" got {type(self.track).__name__}"
            )
        if not isinstance(self.replication, BatchReplicationDefaults):
            raise TypeError(
                f"{type(self).__name__}.replication must be a"
                " BatchReplicationDefaults, got"
                f" {type(self.replication).__name__}"
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
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError(
                f"{type(self).__name__}.notes must be a str or None, got"
                f" {type(self.notes).__name__}"
            )
        for parameter in self.parameters:
            if not isinstance(parameter, str):
                raise TypeError(
                    f"{type(self).__name__}.parameters keys must be strings,"
                    f" got {type(parameter).__name__}"
                )
        # Defensive copy: the frozen template owns its parameter table, so
        # mutating the caller's dict can never leak into the template.
        object.__setattr__(self, "parameters", dict(self.parameters))
        assessment = validate_template_values(self)
        if assessment.violations:
            details = "; ".join(assessment.violations)
            raise InvalidTemplateError(
                f"invalid {self.unit_process_kind.value} template"
                f" {self.template_id!r}: {details}"
            )


@dataclass(frozen=True)
class SynthesisUnitProcessTemplate(SynthesisTemplateBase):
    """Synthesis Unit Process template (ligand / material / MOF).

    The general synthesis template: ``unit_process_kind`` is one of the
    three synthesis kinds (``LIGAND_SYNTHESIS``, ``MATERIAL_SYNTHESIS``,
    ``MOF_SYNTHESIS``; default ``MATERIAL_SYNTHESIS``). Material-specific
    values (e.g. the FDM-201 reference chemistry) are instance data in
    ``parameters``; the validation rules are universal (AC-03).
    """

    _ALLOWED_KINDS: ClassVar[tuple[SynthesisUnitProcessKind, ...]] = (
        SynthesisUnitProcessKind.LIGAND_SYNTHESIS,
        SynthesisUnitProcessKind.MATERIAL_SYNTHESIS,
        SynthesisUnitProcessKind.MOF_SYNTHESIS,
    )

    unit_process_kind: SynthesisUnitProcessKind = (
        SynthesisUnitProcessKind.MATERIAL_SYNTHESIS
    )


@dataclass(frozen=True)
class ActivationTemplate(SynthesisTemplateBase):
    """Thermal activation Unit Process template.

    Fixed ``unit_process_kind`` ``ACTIVATION``; records the activation
    temperature, duration, atmosphere and pressure. Any remaining
    required parameters route to the Assumption Registry pathway.
    """

    _ALLOWED_KINDS: ClassVar[tuple[SynthesisUnitProcessKind, ...]] = (
        SynthesisUnitProcessKind.ACTIVATION,
    )

    unit_process_kind: SynthesisUnitProcessKind = (
        SynthesisUnitProcessKind.ACTIVATION
    )


@dataclass(frozen=True)
class SolventExchangeTemplate(SynthesisTemplateBase):
    """Solvent-exchange Unit Process template.

    Fixed ``unit_process_kind`` ``SOLVENT_EXCHANGE``; records the solvent,
    exchange-cycle count, temperature and soaking duration.
    """

    _ALLOWED_KINDS: ClassVar[tuple[SynthesisUnitProcessKind, ...]] = (
        SynthesisUnitProcessKind.SOLVENT_EXCHANGE,
    )

    unit_process_kind: SynthesisUnitProcessKind = (
        SynthesisUnitProcessKind.SOLVENT_EXCHANGE
    )


def _validate_template_id(class_name: str, value: str) -> None:
    """Reject template ids that escape registries or break glob listings.

    Safe single registry path segment (FND-M9-G02-01 lesson): no path
    separators, no glob metacharacters, not empty, not ``.``/``..``.
    """
    if not value.strip() or value in (".", ".."):
        raise InvalidTemplateError(
            f"{class_name}.template_id must be a non-empty safe registry"
            f" id, got {value!r}"
        )
    if "/" in value or "\\" in value:
        raise InvalidTemplateError(
            f"{class_name}.template_id must be a safe single path segment"
            f" (no '/', no '\\'), got {value!r}"
        )
    if any(char.isspace() for char in value):
        raise InvalidTemplateError(
            f"{class_name}.template_id must not contain whitespace, got"
            f" {value!r}"
        )
    if any(char in value for char in "*?[]"):
        raise InvalidTemplateError(
            f"{class_name}.template_id must not contain glob"
            f" metacharacters, got {value!r}"
        )


# ---------------------------------------------------------------------------
# Universal evaluation (pure and deterministic)
# ---------------------------------------------------------------------------


def _rule_for_kind(kind: SynthesisUnitProcessKind) -> TemplateParameterRule:
    """The required-parameter rule of a kind (first match wins)."""
    for rule in TEMPLATE_PARAMETER_RULES:
        if rule.predicate(kind):
            return rule
    # The trailing total default always matches (validate_synthesis_rulesets
    # guarantees it); this line is unreachable.
    return TEMPLATE_PARAMETER_RULES[-1]


def assess_parameter_completeness(
    template: SynthesisTemplateBase,
) -> ParameterCompletenessAssessment:
    """Evaluate a template's required-scientific-parameter completeness.

    Pure and deterministic: the assessment is a pure function of the
    template's kind and recorded parameter names, decided by the ordered
    ``TEMPLATE_PARAMETER_RULES`` table (first match wins; the trailing
    default rule always matches). The assessment records every rule
    decision, the matched rule id and the missing parameters.

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``.
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
            f" {type(template).__name__}"
        )
    recorded = set(template.parameters)
    decisions: list[ParameterCompletenessDecision] = []
    matched_rule_id: str | None = None
    matched_required: tuple[str, ...] = ()
    for rule in TEMPLATE_PARAMETER_RULES:
        matched = rule.predicate(template.unit_process_kind)
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
        kind=template.unit_process_kind,
        present_parameters=tuple(sorted(recorded)),
        missing_parameters=missing,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


def missing_parameters(template: SynthesisTemplateBase) -> tuple[str, ...]:
    """The required scientific parameters the template does not record.

    The exact input of the Assumption Registry routing (AC-02).

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``.
    """
    return assess_parameter_completeness(template).missing_parameters


def validate_template_values(
    template: SynthesisTemplateBase,
) -> ValueValidationAssessment:
    """Validate the template's present parameter values by the universal table.

    Pure and deterministic: every ``TEMPLATE_VALUE_RULES`` rule whose
    parameter the template records is applied; violations are collected
    as stable messages (``matched_rule_id`` names the first violated rule
    in table order). The template constructor enforces this assessment;
    the public hook makes the decision auditable.

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``.
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
            f" {type(template).__name__}"
        )
    violations: list[str] = []
    matched_rule_id: str | None = None
    decisions: list[ValueValidationDecision] = []
    for rule in TEMPLATE_VALUE_RULES:
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


def freeze_synthesis_template(
    template: SynthesisTemplateBase, *, role: Role
) -> SynthesisTemplateBase:
    """Freeze a template -- a Supervisor-only decision (DEV-M6-G03).

    Templates RECORD the strict/recovery label and may be proposed by
    Research/domain helpers, but freezing is gated by the frozen
    role-action matrix: the caller's role must be permitted the plan
    freeze action (``Action.PLAN_FREEZE``, granted only to the Supervisor
    by ``R-PRM-SUP1``). The pure function returns a frozen copy
    (``frozen`` True) of the template; nothing is ever frozen silently
    and the input template is never mutated.

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``, or
            ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    assessment = check_action_allowed(role, Action.PLAN_FREEZE)
    if not assessment.allowed:
        raise PermissionDeniedError(
            f"role {role.value!r} may not freeze synthesis template"
            f" {template.template_id!r}: freezing is a Supervisor-only"
            " decision (the plan-freeze action of the frozen role-action"
            " matrix)",
            assessment,
        )
    return replace(template, frozen=True)


# ---------------------------------------------------------------------------
# Assumption Registry routing (AC-02: the existing pathway, never a copy)
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
    (``SynthesisTemplateBase.assumption_refs``).
    """

    template_id: str
    kind: SynthesisUnitProcessKind
    missing_parameters: tuple[str, ...]
    assumptions: tuple[Assumption, ...]
    effects: tuple[AssumptionEffectDecision, ...]
    strict_label_assessment: StrictLabelAssessment
    assumption_refs: tuple[str, ...]


def assumptions_for_missing_parameters(
    template: SynthesisTemplateBase,
    *,
    classification: AssumptionClassification = AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
    rationale: str | None = None,
    source_refs: Sequence[str] = (),
    affected_goal_ids: Sequence[str] = (),
) -> MissingParameterRouting:
    """Route the template's missing scientific parameters through the real
    Assumption Registry pathway (AC-02).

    For every required scientific parameter the template does not record,
    a real ``core.models.Assumption`` registry entry is constructed
    (deterministic safe assumption id derived from the template id and
    the parameter, ``core.ids.generate_id``), its strict-status effect is
    decided by the real ``core.rules.assumptions.assumption_effect`` and
    recorded on the entry, and the real ``core.rules.assumptions.evaluate_strict_label``
    reads the whole set back into the strict label. The default
    classification for a missing scientific parameter is
    ``A2_SCIENTIFIC_ASSUMPTION`` (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md
    SS5: missing scientifically meaningful settings are A2 unless
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
        TypeError: ``template`` is not a ``SynthesisTemplateBase``,
            ``classification`` is not an ``AssumptionClassification``
            member, ``rationale`` is not a str or None, or a ref/affected
            goal id is not a str.
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
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
                    f"required scientific parameter {parameter!r} of"
                    f" {template.unit_process_kind.value} template"
                    f" {template.template_id!r} is not recorded by the"
                    " published protocol and enters the Assumption"
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
        kind=template.unit_process_kind,
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
    template: SynthesisTemplateBase, routing: MissingParameterRouting
) -> SynthesisTemplateBase:
    """Return the template carrying the routed assumption refs (AC-02).

    Pure: a frozen copy of the template with ``assumption_refs`` set to
    the routing's safe assumption ids; the input template and the routing
    are never mutated.

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``, or
            ``routing`` is not a ``MissingParameterRouting``.
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(routing, MissingParameterRouting):
        raise TypeError(
            "routing must be a MissingParameterRouting, got"
            f" {type(routing).__name__}"
        )
    return replace(template, assumption_refs=routing.assumption_refs)


# ---------------------------------------------------------------------------
# Independent-batch planning (AC-01: independent Runs, frozen floor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchFloorRule:
    """One entry of the ordered batch-floor rule table."""

    rule_id: str
    description: str
    sufficient: bool
    predicate: Callable[[int, int], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"BatchFloorRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidBatchPlanError(
                    f"BatchFloorRule.{field_name} must be a non-empty"
                    f" string, got {value!r}"
                )
        if not isinstance(self.sufficient, bool):
            raise TypeError(
                "BatchFloorRule.sufficient must be a bool, got"
                f" {type(self.sufficient).__name__}"
            )
        if not callable(self.predicate):
            raise TypeError(
                "BatchFloorRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class BatchFloorDecision:
    """Record of one batch-floor rule evaluation."""

    rule_id: str
    description: str
    sufficient: bool
    matched: bool


@dataclass(frozen=True)
class BatchFloorAssessment:
    """Full, auditable result of a batch-floor evaluation.

    ``floor`` is the template's ``minimum_n`` (the frozen ``n >= 3``
    default family unless the template records an explicit floor),
    ``sufficient`` True iff ``n >= floor``, and ``requested_batches`` the
    number of additional independent batches a below-floor plan must add
    to reach the floor (0 when sufficient).
    """

    n: int
    floor: int
    sufficient: bool
    requested_batches: int
    decisions: tuple[BatchFloorDecision, ...]
    matched_rule_id: str
    ruleset_version: str = SYNTHESIS_RULESET_VERSION


#: The ordered batch-floor rule table (07-STATISTICS-AND-ACCEPTANCE.md
#: SS2: independent batch floor; first match wins, trailing total
#: default).
BATCH_FLOOR_RULES: tuple[BatchFloorRule, ...] = (
    BatchFloorRule(
        rule_id="R-BF-1",
        description=(
            "the planned independent batch count meets the frozen floor:"
            " floor satisfied"
        ),
        sufficient=True,
        predicate=lambda n, floor: n >= floor,
    ),
    BatchFloorRule(
        rule_id="R-BF-2",
        description=(
            "the planned independent batch count is below the frozen"
            " floor: additional independent batches are requested to reach"
            " it (default)"
        ),
        sufficient=False,
        predicate=lambda n, floor: True,
    ),
)


def evaluate_batch_floor(
    n: int, floor: int = INDEPENDENT_FLOOR
) -> BatchFloorAssessment:
    """Evaluate an independent-batch count against the frozen floor.

    Pure and deterministic: sufficient iff ``n >= floor``, decided by the
    ordered ``BATCH_FLOOR_RULES`` table (first match wins; the trailing
    default is total). Requests exactly the batches needed to reach the
    floor.

    Raises:
        TypeError: ``n`` is not an int, or ``floor`` is not an int.
        InvalidBatchPlanError: ``n`` is below 1 (a plan plans at least
            one batch).
    """
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"n must be an int, got {type(n).__name__}")
    if not isinstance(floor, int) or isinstance(floor, bool):
        raise TypeError(f"floor must be an int, got {type(floor).__name__}")
    if n < 1:
        raise InvalidBatchPlanError(
            f"an independent-batch plan must plan at least one batch, got {n}"
        )
    decisions: list[BatchFloorDecision] = []
    matched_rule_id: str | None = None
    matched_sufficient = False  # unreachable default
    for rule in BATCH_FLOOR_RULES:
        matched = rule.predicate(n, floor)
        decisions.append(
            BatchFloorDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                sufficient=rule.sufficient,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_sufficient = rule.sufficient
    # R-BF-2 (the total default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return BatchFloorAssessment(
        n=n,
        floor=floor,
        sufficient=matched_sufficient,
        requested_batches=max(0, floor - n),
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


@dataclass(frozen=True)
class BatchPlan:
    """A deterministic independent-batch plan of a template (AC-01).

    ``batch_run_ids`` holds one deterministic safe run id per planned
    batch (derived from the template id and the batch index,
    ``core.ids.generate_id``); every planned batch is labeled with the
    frozen ``RunType.INDEPENDENT_REPLICATE`` vocabulary; ``track`` carries
    the template's strict/recovery label; ``floor_assessment`` records the
    frozen-floor decision (below-floor plans request the batches needed to
    reach the floor).
    """

    template_id: str
    track: GoalTrack
    n: int
    run_type: RunType
    batch_run_ids: tuple[str, ...]
    floor_assessment: BatchFloorAssessment


def plan_independent_batches(
    template: SynthesisTemplateBase, n: int = INDEPENDENT_FLOOR
) -> BatchPlan:
    """Plan ``n`` independent synthesis batches for a template (AC-01).

    Pure and deterministic: the plan is a pure function of the template
    and ``n`` -- every batch run id is derived with ``core.ids.generate_id``
    (no randomness, no wall clock), every batch is labeled with the frozen
    ``RunType.INDEPENDENT_REPLICATE``, the track label is the template's
    strict/recovery label, and the frozen ``n >= 3`` floor family is
    evaluated and recorded (a below-floor plan requests the batches needed
    to reach the floor).

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``, or
            ``n`` is not an int.
        InvalidBatchPlanError: ``n`` is below 1.
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
            f" {type(template).__name__}"
        )
    floor_assessment = evaluate_batch_floor(n, template.replication.minimum_n)
    batch_run_ids = tuple(
        generate_id("run", template.template_id, f"batch-{index:03d}")
        for index in range(1, n + 1)
    )
    return BatchPlan(
        template_id=template.template_id,
        track=template.track,
        n=n,
        run_type=RunType.INDEPENDENT_REPLICATE,
        batch_run_ids=batch_run_ids,
        floor_assessment=floor_assessment,
    )


# ---------------------------------------------------------------------------
# Protocol capture (deterministic, pure)
# ---------------------------------------------------------------------------

#: The shape a captured protocol dict must carry (protocol capture
#: deliverable; consumed by downstream execution-package builders).
CAPTURE_KEYS: tuple[str, ...] = (
    "template_id",
    "title",
    "unit_process_kind",
    "track",
    "frozen",
    "replication",
    "parameter_table",
    "assumption_refs",
    "notes",
)


def capture_protocol(template: SynthesisTemplateBase) -> dict[str, Any]:
    """Capture the template as a deterministic protocol dict.

    Pure: the capture is a pure function of the template -- sorted
    parameter table, the frozen replication defaults, the strict/recovery
    track label, the freeze state and the assumption refs of the routed
    missing parameters. Same template -> identical capture on every call
    and platform.

    Raises:
        TypeError: ``template`` is not a ``SynthesisTemplateBase``.
    """
    if not isinstance(template, SynthesisTemplateBase):
        raise TypeError(
            "template must be a SynthesisTemplateBase, got"
            f" {type(template).__name__}"
        )
    return {
        "template_id": template.template_id,
        "title": template.title,
        "unit_process_kind": template.unit_process_kind.value,
        "track": template.track.value,
        "frozen": template.frozen,
        "replication": template.replication.as_dict(),
        "parameter_table": [
            {"parameter": parameter, "value": template.parameters[parameter]}
            for parameter in sorted(template.parameters)
        ],
        "assumption_refs": list(template.assumption_refs),
        "notes": template.notes,
    }
