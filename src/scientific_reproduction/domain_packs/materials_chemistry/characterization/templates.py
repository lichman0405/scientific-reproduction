"""Materials-chemistry characterization planning/analysis templates (DEV-M11-G02).

Implements the **characterization planning/analysis metadata templates**
deliverable of DEV-M11-G02 for the materials-chemistry domain pack: frozen,
parameterized templates for PXRD, SCXRD, TGA and spectroscopy
characterization capture, freezable analysis protocol/acceptance plans and
PXRD identity/quality checks as auditable decision records. Grounded in:

* ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS2 (v0.1 experimental
  capability families: PXRD; SCXRD and structure verification; TGA/thermal
  analysis; spectroscopy/basic identity characterization) and SS5 (domain
  acceptance examples are *templates*, never universal thresholds: PXRD
  analysis "may include peak-position agreement, phase identification,
  intensity-pattern comparison with caution for preferred orientation, and
  batch consistency"; "Missing critical column parameters enter Assumption
  Registry"; missing scientifically meaningful settings are A2 unless
  reliable method evidence supports an A1 classification);
* ``08-STRICT-RECOVERY-CLOSURE.md`` SS1/SS3 (the Assumption Registry:
  every non-explicit parameter is registered as
  ``A0_TECHNICAL_DEFAULT`` / ``A1_METHODOLOGICAL_DEFAULT`` /
  ``A2_SCIENTIFIC_ASSUMPTION``; A2 must not be silently used inside strict
  reproduction);
* ``core/models.py`` -- the frozen vocabulary reused verbatim:
  ``GoalTrack`` (the strict/recovery track label) and ``Assumption`` /
  ``AssumptionClassification`` (the Assumption Registry entry);
* ``core/rules/assumptions.py`` -- the EXISTING Assumption Registry
  evaluation API (``assumption_effect`` / ``evaluate_strict_label``):
  missing raw data/instrument metadata and missing acceptance measurements
  are routed through it, never through a parallel store;
* ``core/permissions.py`` (DEV-M6-G03) -- the role-action matrix: analysis
  plans and templates are proposed by Research/domain helpers, but freezing
  is Supervisor-only (``Action.PLAN_FREEZE``); the freeze helpers are gated
  by the matrix, so an analysis protocol and its acceptance criteria are
  frozen separately from execution and nothing is ever silently frozen;
* ``17-FDM201-REFERENCE-CASE.md`` WP-30 (PXRD/SCXRD/TGA/FTIR
  characterization of the FDM-201 reference case) -- the workflows the
  templates model. AC-03: FDM-201-specific chemistry may appear only as
  **instance data** inside template parameters and analysis-plan acceptance
  thresholds; the rule tables below are universal (no reagent names, no
  instrument models, no condition values).

Template model (determinism and boundaries)
-------------------------------------------
Every template is a frozen dataclass with strict ``__post_init__``
validation: ``TypeError`` at the type boundaries (template id, kind, track,
parameters, analysis plan, ...), ``ValueError``-subclass stable errors
(``InvalidCharacterizationTemplateError`` and siblings) for value
violations. Construction enforces the **universal metadata value rules** of
the ordered, versioned ``CHARACTERIZATION_VALUE_RULES`` table over the
parameters that ARE present, and validates ids as safe single registry path
segments (the FND-M9-G02-01 lesson: no path separators, no glob
metacharacters). Missing required raw data/instrument metadata is NOT a
construction error (AC-01): it is the input to the Assumption Registry
pathway -- the ordered ``CHARACTERIZATION_REQUIREMENT_RULES`` table
declares, per characterization kind, the required raw data + instrument
metadata parameter set, and
:func:`assumptions_for_missing_metadata` routes every missing required
parameter through the real ``core.models.Assumption`` record and the real
``core.rules.assumptions`` evaluation API.

Analysis protocol and acceptance (AC-02)
----------------------------------------
Every template carries an :class:`AnalysisPlan`: the analysis protocol
description, ordered protocol steps and the instance-data acceptance
threshold parameters (e.g. a recorded peak-position tolerance -- the
threshold VALUES are instance data, never universal rules). The plan is a
frozen metadata record that can be frozen by
:func:`freeze_analysis_plan` -- a Supervisor-only decision through the real
``Action.PLAN_FREEZE`` permission, requiring NO execution artifacts: the
analysis protocol and acceptance criteria are frozen separately from
execution. :func:`evaluate_acceptance` is a pure function of the recorded
measurements and the plan's thresholds: every criterion of the ordered,
universal ``ANALYSIS_ACCEPTANCE_RULES`` table is applied as an explicit
contract, and every decision is recorded (``AcceptanceAssessment``).

PXRD identity/quality checks (AC-03)
------------------------------------
PXRD identity/quality checks are REPRESENTABLE metadata, never a worker
self-decision: the four checks of ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md``
SS5 (phase identification, peak-position agreement, intensity-pattern
comparison with caution for preferred orientation, and batch consistency)
are the ordered, universal ``PXRD_IDENTITY_CHECKS`` table. The worker
records measurement FACTS (e.g. the largest peak-position deviation);
:func:`evaluate_identity_checks` decides PASS/FAIL/PENDING through the
frozen check predicates and the plan's recorded thresholds, returning an
:class:`IdentityCheckAssessment` -- a full decision record (every check
decision, the deciding check id, the outcome rule id) with no worker
outcome input anywhere. A check whose measurement is not recorded is
PENDING, never silently skipped: it routes to the Assumption Registry
pathway (:func:`assumptions_for_missing_measurements`).

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
    "ACCEPTANCE_PARAMETER_RULES",
    "ACCEPTANCE_PARAMETERS",
    "ANALYSIS_ACCEPTANCE_RULES",
    "AcceptanceAssessment",
    "AcceptanceDecision",
    "AcceptanceOutcomeRule",
    "UniversalValueRule",
    "AnalysisPlan",
    "CAPTURE_KEYS",
    "CHARACTERIZATION_REQUIREMENT_RULES",
    "CHARACTERIZATION_RULESET_VERSION",
    "CHARACTERIZATION_VALUE_RULES",
    "CHECK_OUTCOME_RULES",
    "CheckOutcome",
    "CharacterizationKind",
    "CharacterizationTemplateBase",
    "CharacterizationTemplateError",
    "IdentityCheckAssessment",
    "IdentityCheckDecision",
    "InvalidAcceptanceCriteriaError",
    "InvalidAnalysisPlanError",
    "InvalidCharacterizationTemplateError",
    "MetadataCompletenessAssessment",
    "MetadataCompletenessDecision",
    "MetadataValueAssessment",
    "MetadataValueDecision",
    "MissingMeasurementRouting",
    "MissingMetadataRouting",
    "PXRDCharacterizationTemplate",
    "PXRD_IDENTITY_CHECKS",
    "PXRD_KIND",
    "SCXRDCharacterizationTemplate",
    "SCXRD_KIND",
    "SPECTROSCOPY_KIND",
    "SpectroscopyCharacterizationTemplate",
    "TGACharacterizationTemplate",
    "TGA_KIND",
    "TemplateRequirementRule",
    "UnknownCharacterizationKindError",
    "apply_assumption_routing",
    "assess_metadata_completeness",
    "assumptions_for_missing_measurements",
    "assumptions_for_missing_metadata",
    "capture_characterization",
    "evaluate_acceptance",
    "evaluate_identity_checks",
    "freeze_analysis_plan",
    "freeze_characterization_template",
    "missing_metadata",
    "validate_characterization_rulesets",
    "validate_metadata_values",
]

#: Version of the characterization rule tables. Bumped whenever a rule
#: changes; recorded in every assessment so old decisions stay
#: interpretable.
CHARACTERIZATION_RULESET_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class CharacterizationTemplateError(ValueError):
    """Base class for all characterization-template errors."""


class InvalidCharacterizationTemplateError(CharacterizationTemplateError):
    """Raised when a template violates a value rule or a shape rule."""


class UnknownCharacterizationKindError(CharacterizationTemplateError):
    """Raised when no rule declares a required metadata set for a kind."""


class InvalidAnalysisPlanError(CharacterizationTemplateError):
    """Raised when an analysis plan violates the frozen plan shape."""


class InvalidAcceptanceCriteriaError(CharacterizationTemplateError):
    """Raised when acceptance criteria violate the universal rules."""


# ---------------------------------------------------------------------------
# Characterization kind vocabulary (the domain-pack capability families)
# ---------------------------------------------------------------------------


class CharacterizationKind(StrEnum):
    """The characterization kinds the templates parameterize.

    Values follow ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS2 family
    names (PXRD; SCXRD and structure verification; TGA/thermal analysis;
    spectroscopy/basic identity characterization). This is domain-pack
    vocabulary, distinct from the frozen core vocabulary -- the kinds are
    the keys of the universal ``CHARACTERIZATION_REQUIREMENT_RULES`` and
    ``ANALYSIS_ACCEPTANCE_RULES`` tables, never chemistry instances.
    """

    PXRD = "pxrd"
    SCXRD = "scxrd"
    TGA = "tga"
    SPECTROSCOPY = "spectroscopy"


#: Convenience aliases for the four characterization kinds.
PXRD_KIND: CharacterizationKind = CharacterizationKind.PXRD
SCXRD_KIND: CharacterizationKind = CharacterizationKind.SCXRD
TGA_KIND: CharacterizationKind = CharacterizationKind.TGA
SPECTROSCOPY_KIND: CharacterizationKind = CharacterizationKind.SPECTROSCOPY


# ---------------------------------------------------------------------------
# Universal rule tables (AC-01/AC-03: no material-specific chemistry)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniversalValueRule:
    """One entry of a universal named-value rule table.

    The shared shape of the two universal value-rule tables: the metadata
    value rules (``CHARACTERIZATION_VALUE_RULES``, one universal rule per
    raw data / instrument metadata parameter name) and the acceptance
    parameter rules (``ACCEPTANCE_PARAMETER_RULES`` -- acceptance
    thresholds are instance data on the analysis plan,
    ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS5: templates, never
    universal thresholds; each NAMED threshold parameter has exactly one
    universal shape rule, e.g. a normalized score is a number in
    ``[0, 1]``, a tolerance is a finite positive number).
    """

    rule_id: str
    parameter: str
    description: str
    predicate: Callable[[Any], bool]
    message: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("parameter", self.parameter),
            ("description", self.description),
            ("message", self.message),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"UniversalValueRule.{field_name} must be a str,"
                    f" got {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidAcceptanceCriteriaError(
                    f"UniversalValueRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not callable(self.predicate):
            raise TypeError(
                "UniversalValueRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class AnalysisAcceptanceRule:
    """One entry of the universal analysis-acceptance rule table.

    Each rule is an explicit acceptance contract between one instance-data
    threshold parameter of the analysis plan and one recorded measurement:
    the predicate is a pure function of ``(threshold, measurements)`` -- a
    worker records measurement FACTS and the frozen rule decides, so no
    worker self-decision ever enters an acceptance or identity/quality
    outcome (AC-03). ``kinds`` declares which characterization kinds the
    rule applies to; ``measurement`` is the fact key the rule consumes;
    ``acceptance_parameter`` is the plan threshold key (``None`` when the
    rule needs no threshold).
    """

    rule_id: str
    description: str
    kinds: tuple[CharacterizationKind, ...]
    acceptance_parameter: str | None
    measurement: str
    predicate: Callable[[float | None, dict[str, Any]], bool]
    message: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("measurement", self.measurement),
            ("message", self.message),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"AnalysisAcceptanceRule.{field_name} must be a str,"
                    f" got {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidAcceptanceCriteriaError(
                    f"AnalysisAcceptanceRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not isinstance(self.kinds, tuple) or not self.kinds:
            raise TypeError(
                "AnalysisAcceptanceRule.kinds must be a non-empty tuple of"
                " CharacterizationKind members"
            )
        if not all(isinstance(kind, CharacterizationKind) for kind in self.kinds):
            raise TypeError(
                "AnalysisAcceptanceRule.kinds entries must be"
                " CharacterizationKind members"
            )
        if self.acceptance_parameter is not None and not isinstance(
            self.acceptance_parameter, str
        ):
            raise TypeError(
                "AnalysisAcceptanceRule.acceptance_parameter must be a str"
                " or None, got"
                f" {type(self.acceptance_parameter).__name__}"
            )
        if not callable(self.predicate):
            raise TypeError(
                "AnalysisAcceptanceRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class AcceptanceOutcomeRule:
    """One entry of the ordered check-outcome rule table.

    The aggregate outcome of a set of per-item check outcomes: first match
    wins; the trailing total default always matches.
    """

    rule_id: str
    description: str
    outcome: CheckOutcome
    predicate: Callable[[tuple[CheckOutcome, ...]], bool]

    def __post_init__(self) -> None:
        for field_name, value in (("rule_id", self.rule_id), ("description", self.description)):
            if not isinstance(value, str):
                raise TypeError(
                    f"AcceptanceOutcomeRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidAcceptanceCriteriaError(
                    f"AcceptanceOutcomeRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not isinstance(self.outcome, CheckOutcome):
            raise TypeError(
                "AcceptanceOutcomeRule.outcome must be a CheckOutcome"
                " member, got"
                f" {type(self.outcome).__name__}"
            )
        if not callable(self.predicate):
            raise TypeError(
                "AcceptanceOutcomeRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


class CheckOutcome(StrEnum):
    """The frozen outcome vocabulary of an acceptance or identity check.

    ``PASS`` / ``FAIL`` are decided by the frozen contract (a pure rule
    predicate over recorded measurement facts and plan thresholds), never
    by a worker; ``PENDING`` marks a check whose required measurement is
    not recorded -- the exact input of the Assumption Registry pathway,
    never a silent skip.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    PENDING = "PENDING"


def _is_finite_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number."""
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


def _is_non_empty_string(value: Any) -> bool:
    """True iff ``value`` is a non-empty str."""
    return isinstance(value, str) and bool(value.strip())


def _is_controlled_atmosphere(value: Any) -> bool:
    """True iff ``value`` is a controlled-atmosphere name."""
    return isinstance(value, str) and value in CONTROLLED_ATMOSPHERES


def _kind_in(*kinds: CharacterizationKind) -> Callable[[CharacterizationKind], bool]:
    """A predicate matching exactly the given characterization kinds."""
    return lambda candidate: candidate in kinds


def _score_meets(threshold: float | None, value: Any) -> bool:
    """True iff a normalized measurement score meets the recorded threshold."""
    return _is_unit_score(value) and (threshold is None or value >= threshold)


def _deviation_within(threshold: float | None, value: Any) -> bool:
    """True iff a non-negative deviation is at most the recorded tolerance."""
    return _is_non_negative_number(value) and (threshold is None or value <= threshold)


def _r_factor_within(threshold: float | None, value: Any) -> bool:
    """True iff a non-negative agreement factor is at most the recorded maximum."""
    return _is_non_negative_number(value) and (threshold is None or value <= threshold)


def _mass_loss_within(threshold: float | None, measurements: dict[str, Any]) -> bool:
    """True iff the observed mass loss deviates from the reference within the window.

    Pure contract of the recorded facts: ``|observed - reference| <=
    threshold`` with the window recorded on the plan -- the worker records
    the observed value and the reference value, the frozen rule decides.
    """
    observed = measurements["observed_mass_loss_pct"]
    reference = measurements["reference_mass_loss_pct"]
    if not _is_finite_number(observed) or not _is_finite_number(reference):
        return False
    return threshold is not None and abs(observed - reference) <= threshold


#: The ordered, versioned universal metadata value-rule table. Each named
#: raw data / instrument metadata parameter has exactly one rule (the table
#: is a total function of parameter names). Order is normative.
CHARACTERIZATION_VALUE_RULES: tuple[UniversalValueRule, ...] = (
    UniversalValueRule(
        rule_id="R-CHA-V1",
        parameter="wavelength_A",
        description=(
            "a recorded radiation wavelength must be a finite positive"
            " number of angstrom"
        ),
        predicate=_is_positive_number,
        message="wavelength_A must be a finite positive number of angstrom,"
        " got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V2",
        parameter="radiation_type",
        description=(
            "a recorded radiation must be a named radiation (any name; the"
            " rules never restrict which radiation -- AC-03)"
        ),
        predicate=_is_non_empty_string,
        message="radiation_type must be a non-empty radiation name, got"
        " {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V3",
        parameter="two_theta_min_deg",
        description=(
            "a recorded scan-range start must be a finite non-negative"
            " number of degrees two-theta"
        ),
        predicate=_is_non_negative_number,
        message="two_theta_min_deg must be a finite non-negative number of"
        " degrees, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V4",
        parameter="two_theta_max_deg",
        description=(
            "a recorded scan-range end must be a finite positive number of"
            " degrees two-theta"
        ),
        predicate=_is_positive_number,
        message="two_theta_max_deg must be a finite positive number of"
        " degrees, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V5",
        parameter="step_size_deg",
        description=(
            "a recorded scan step size must be a finite positive number of"
            " degrees"
        ),
        predicate=_is_positive_number,
        message="step_size_deg must be a finite positive number of degrees,"
        " got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V6",
        parameter="scan_temperature_K",
        description=(
            "a recorded scan temperature is on the absolute scale: it must"
            " be a finite positive number of kelvin"
        ),
        predicate=_is_positive_number,
        message="scan_temperature_K must be a finite positive number of"
        " kelvin, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V7",
        parameter="collection_temperature_K",
        description=(
            "a recorded data-collection temperature is on the absolute"
            " scale: it must be a finite positive number of kelvin"
        ),
        predicate=_is_positive_number,
        message="collection_temperature_K must be a finite positive number"
        " of kelvin, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V8",
        parameter="resolution_limit_A",
        description=(
            "a recorded structure resolution limit must be a finite"
            " positive number of angstrom"
        ),
        predicate=_is_positive_number,
        message="resolution_limit_A must be a finite positive number of"
        " angstrom, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V9",
        parameter="sample_mass_mg",
        description=(
            "a recorded sample mass must be a finite positive number of"
            " milligrams"
        ),
        predicate=_is_positive_number,
        message="sample_mass_mg must be a finite positive number of"
        " milligrams, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V10",
        parameter="heating_rate_K_min",
        description=(
            "a recorded heating rate must be a finite positive number of"
            " kelvin per minute"
        ),
        predicate=_is_positive_number,
        message="heating_rate_K_min must be a finite positive number of"
        " kelvin per minute, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V11",
        parameter="final_temperature_K",
        description=(
            "a recorded final temperature is on the absolute scale: it"
            " must be a finite positive number of kelvin"
        ),
        predicate=_is_positive_number,
        message="final_temperature_K must be a finite positive number of"
        " kelvin, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V12",
        parameter="scan_duration_h",
        description=(
            "a recorded analysis duration must be a finite positive number"
            " of hours"
        ),
        predicate=_is_positive_number,
        message="scan_duration_h must be a finite positive number of hours,"
        " got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V13",
        parameter="gas_flow_ml_min",
        description=(
            "a recorded purge gas flow must be a finite non-negative number"
            " of millilitres per minute"
        ),
        predicate=_is_non_negative_number,
        message="gas_flow_ml_min must be a finite non-negative number of"
        " millilitres per minute, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V14",
        parameter="atmosphere",
        description=(
            "a recorded atmosphere must be one of the controlled handling"
            " vocabulary"
        ),
        predicate=_is_controlled_atmosphere,
        message="atmosphere must be one of the controlled handling names"
        f" {sorted(CONTROLLED_ATMOSPHERES)}, got {{value!r}}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V15",
        parameter="detector",
        description=(
            "a recorded detector must be a named detector (any name; the"
            " rules never restrict which detector -- AC-03)"
        ),
        predicate=_is_non_empty_string,
        message="detector must be a non-empty detector name, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V16",
        parameter="wavenumber_min_cm_1",
        description=(
            "a recorded spectral-range start must be a finite non-negative"
            " number of reciprocal centimetres"
        ),
        predicate=_is_non_negative_number,
        message="wavenumber_min_cm_1 must be a finite non-negative number"
        " of reciprocal centimetres, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V17",
        parameter="wavenumber_max_cm_1",
        description=(
            "a recorded spectral-range end must be a finite positive"
            " number of reciprocal centimetres"
        ),
        predicate=_is_positive_number,
        message="wavenumber_max_cm_1 must be a finite positive number of"
        " reciprocal centimetres, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V18",
        parameter="resolution_cm_1",
        description=(
            "a recorded spectral resolution must be a finite positive"
            " number of reciprocal centimetres"
        ),
        predicate=_is_positive_number,
        message="resolution_cm_1 must be a finite positive number of"
        " reciprocal centimetres, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V19",
        parameter="number_of_scans",
        description=(
            "a recorded scan count must be an integer of at least one scan"
        ),
        predicate=_is_positive_integer,
        message="number_of_scans must be an integer >= 1, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V20",
        parameter="technique",
        description=(
            "a recorded spectroscopic technique must be a named technique"
            " (any name; the rules never restrict which technique --"
            " AC-03)"
        ),
        predicate=_is_non_empty_string,
        message="technique must be a non-empty technique name, got"
        " {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-V21",
        parameter="instrument",
        description=(
            "a recorded instrument must be a named instrument (any name;"
            " the rules never restrict which instrument -- AC-03)"
        ),
        predicate=_is_non_empty_string,
        message="instrument must be a non-empty instrument name, got"
        " {value!r}",
    ),
)


#: The acceptance threshold parameters an analysis plan may record -- one
#: universal shape rule per parameter name (instance-data thresholds are
#: bound by universal rules; the VALUES are never hardcoded, AC-03).
ACCEPTANCE_PARAMETER_RULES: tuple[UniversalValueRule, ...] = (
    UniversalValueRule(
        rule_id="R-CHA-AP1",
        parameter="pxrd_phase_score_min",
        description=(
            "a recorded phase-identification score floor is a normalized"
            " score: a finite number in [0, 1]"
        ),
        predicate=_is_unit_score,
        message="pxrd_phase_score_min must be a finite number in [0, 1],"
        " got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-AP2",
        parameter="pxrd_peak_tolerance_deg",
        description=(
            "a recorded peak-position tolerance is a finite positive number"
            " of degrees two-theta"
        ),
        predicate=_is_positive_number,
        message="pxrd_peak_tolerance_deg must be a finite positive number"
        " of degrees, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-AP3",
        parameter="pxrd_intensity_score_min",
        description=(
            "a recorded intensity-pattern score floor is a normalized"
            " score: a finite number in [0, 1]"
        ),
        predicate=_is_unit_score,
        message="pxrd_intensity_score_min must be a finite number in"
        " [0, 1], got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-AP4",
        parameter="pxrd_batch_consistency_min",
        description=(
            "a recorded batch-consistency score floor is a normalized"
            " score: a finite number in [0, 1]"
        ),
        predicate=_is_unit_score,
        message="pxrd_batch_consistency_min must be a finite number in"
        " [0, 1], got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-AP5",
        parameter="scxrd_r_factor_max",
        description=(
            "a recorded structure agreement-factor ceiling is a finite"
            " positive number"
        ),
        predicate=_is_positive_number,
        message="scxrd_r_factor_max must be a finite positive number, got"
        " {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-AP6",
        parameter="tga_mass_loss_tolerance_pct",
        description=(
            "a recorded mass-loss window is a finite positive number of"
            " percent"
        ),
        predicate=_is_positive_number,
        message="tga_mass_loss_tolerance_pct must be a finite positive"
        " number of percent, got {value!r}",
    ),
    UniversalValueRule(
        rule_id="R-CHA-AP7",
        parameter="spectroscopy_band_tolerance_cm_1",
        description=(
            "a recorded identity-band tolerance is a finite positive"
            " number of reciprocal centimetres"
        ),
        predicate=_is_positive_number,
        message="spectroscopy_band_tolerance_cm_1 must be a finite positive"
        " number of reciprocal centimetres, got {value!r}",
    ),
)

#: The acceptance threshold parameters an analysis plan may record
#: (frozenset view of the universal acceptance-parameter rule table).
ACCEPTANCE_PARAMETERS: frozenset[str] = frozenset(
    rule.parameter for rule in ACCEPTANCE_PARAMETER_RULES
)


#: The PXRD identity/quality checks (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md
#: SS5 PXRD: peak-position agreement, phase identification, intensity-pattern
#: comparison with caution for preferred orientation, and batch consistency)
#: as an ordered, universal contract table (AC-03): every check is a pure
#: predicate over recorded measurement facts and plan thresholds -- the
#: outcome is decided by the frozen rule, never by a worker self-decision.
PXRD_IDENTITY_CHECKS: tuple[AnalysisAcceptanceRule, ...] = (
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A1",
        description=(
            "PXRD phase identification: the recorded reference-phase"
            " match score must meet the recorded phase-identification floor"
        ),
        kinds=(CharacterizationKind.PXRD,),
        acceptance_parameter="pxrd_phase_score_min",
        measurement="reference_phase_score",
        predicate=lambda threshold, measurements: _score_meets(
            threshold, measurements["reference_phase_score"]
        ),
        message=(
            "reference_phase_score {reference_phase_score!r} does not meet"
            " the recorded phase-identification floor"
            " {pxrd_phase_score_min!r}"
        ),
    ),
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A2",
        description=(
            "PXRD peak-position agreement: the recorded largest"
            " peak-position deviation must be at most the recorded"
            " tolerance"
        ),
        kinds=(CharacterizationKind.PXRD,),
        acceptance_parameter="pxrd_peak_tolerance_deg",
        measurement="max_peak_position_deviation_deg",
        predicate=lambda threshold, measurements: _deviation_within(
            threshold, measurements["max_peak_position_deviation_deg"]
        ),
        message=(
            "max_peak_position_deviation_deg"
            " {max_peak_position_deviation_deg!r} exceeds the recorded"
            " tolerance {pxrd_peak_tolerance_deg!r}"
        ),
    ),
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A3",
        description=(
            "PXRD intensity-pattern comparison (with caution for"
            " preferred orientation): the recorded intensity-pattern score"
            " must meet the recorded floor"
        ),
        kinds=(CharacterizationKind.PXRD,),
        acceptance_parameter="pxrd_intensity_score_min",
        measurement="intensity_pattern_score",
        predicate=lambda threshold, measurements: _score_meets(
            threshold, measurements["intensity_pattern_score"]
        ),
        message=(
            "intensity_pattern_score {intensity_pattern_score!r} does not"
            " meet the recorded floor {pxrd_intensity_score_min!r}"
        ),
    ),
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A4",
        description=(
            "PXRD batch consistency: the recorded batch-consistency score"
            " must meet the recorded floor"
        ),
        kinds=(CharacterizationKind.PXRD,),
        acceptance_parameter="pxrd_batch_consistency_min",
        measurement="batch_consistency_score",
        predicate=lambda threshold, measurements: _score_meets(
            threshold, measurements["batch_consistency_score"]
        ),
        message=(
            "batch_consistency_score {batch_consistency_score!r} does not"
            " meet the recorded floor {pxrd_batch_consistency_min!r}"
        ),
    ),
)

#: The ordered, universal analysis-acceptance rule table: the PXRD
#: identity/quality checks plus the SCXRD, TGA and spectroscopy acceptance
#: contracts. Every characterization kind is covered by at least one rule
#: (``validate_characterization_rulesets`` proves it); the worker records
#: measurement facts and the frozen rules decide (AC-03).
ANALYSIS_ACCEPTANCE_RULES: tuple[AnalysisAcceptanceRule, ...] = PXRD_IDENTITY_CHECKS + (
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A5",
        description=(
            "SCXRD structure verification: the recorded reported agreement"
            " factor must be at most the recorded ceiling"
        ),
        kinds=(CharacterizationKind.SCXRD,),
        acceptance_parameter="scxrd_r_factor_max",
        measurement="reported_r_factor",
        predicate=lambda threshold, measurements: _r_factor_within(
            threshold, measurements["reported_r_factor"]
        ),
        message=(
            "reported_r_factor {reported_r_factor!r} exceeds the recorded"
            " ceiling {scxrd_r_factor_max!r}"
        ),
    ),
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A6",
        description=(
            "TGA mass-loss agreement: the recorded observed mass loss must"
            " deviate from the recorded reference by at most the recorded"
            " window"
        ),
        kinds=(CharacterizationKind.TGA,),
        acceptance_parameter="tga_mass_loss_tolerance_pct",
        measurement="observed_mass_loss_pct",
        predicate=_mass_loss_within,
        message=(
            "observed_mass_loss_pct {observed_mass_loss_pct!r} deviates"
            " from the reference {reference_mass_loss_pct!r} by more than"
            " the recorded window {tga_mass_loss_tolerance_pct!r}"
        ),
    ),
    AnalysisAcceptanceRule(
        rule_id="R-CHA-A7",
        description=(
            "spectroscopy identity-band agreement: the recorded largest"
            " band-position deviation must be at most the recorded"
            " tolerance"
        ),
        kinds=(CharacterizationKind.SPECTROSCOPY,),
        acceptance_parameter="spectroscopy_band_tolerance_cm_1",
        measurement="max_band_position_deviation_cm_1",
        predicate=lambda threshold, measurements: _deviation_within(
            threshold, measurements["max_band_position_deviation_cm_1"]
        ),
        message=(
            "max_band_position_deviation_cm_1"
            " {max_band_position_deviation_cm_1!r} exceeds the recorded"
            " tolerance {spectroscopy_band_tolerance_cm_1!r}"
        ),
    ),
)

#: The ordered check-outcome rule table (first match wins; the trailing
#: total default always matches): any FAIL decides FAIL, else any PENDING
#: decides PENDING, else PASS.
CHECK_OUTCOME_RULES: tuple[AcceptanceOutcomeRule, ...] = (
    AcceptanceOutcomeRule(
        rule_id="R-CHA-O1",
        description=(
            "at least one check failed: the aggregate outcome is FAIL"
        ),
        outcome=CheckOutcome.FAIL,
        predicate=lambda outcomes: any(outcome is CheckOutcome.FAIL for outcome in outcomes),
    ),
    AcceptanceOutcomeRule(
        rule_id="R-CHA-O2",
        description=(
            "no check failed but at least one is pending: the aggregate"
            " outcome is PENDING (the missing measurements enter the"
            " Assumption Registry pathway)"
        ),
        outcome=CheckOutcome.PENDING,
        predicate=lambda outcomes: any(
            outcome is CheckOutcome.PENDING for outcome in outcomes
        ),
    ),
    AcceptanceOutcomeRule(
        rule_id="R-CHA-O3",
        description=(
            "every applicable check passed (default): the aggregate"
            " outcome is PASS"
        ),
        outcome=CheckOutcome.PASS,
        predicate=lambda outcomes: True,
    ),
)


@dataclass(frozen=True)
class TemplateRequirementRule:
    """One entry of the required raw data / instrument metadata rule table.

    Declares, per characterization kind, the required raw data + instrument
    metadata parameters a template of that kind must record (AC-01) or
    route to the Assumption Registry pathway when missing. The parameter
    names are universal method-capture vocabulary -- no reagent names, no
    instrument models (AC-03). The predicate is a pure function of the
    kind; the trailing total default always matches.
    """

    rule_id: str
    description: str
    required_parameters: tuple[str, ...]
    predicate: Callable[[CharacterizationKind], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"TemplateRequirementRule.{field_name} must be a str,"
                    f" got {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidCharacterizationTemplateError(
                    f"TemplateRequirementRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not isinstance(self.required_parameters, tuple) or not all(
            isinstance(parameter, str) and parameter.strip()
            for parameter in self.required_parameters
        ):
            raise TypeError(
                "TemplateRequirementRule.required_parameters must be a"
                " tuple of non-empty strings"
            )
        if not callable(self.predicate):
            raise TypeError(
                "TemplateRequirementRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


#: The ordered required raw data / instrument metadata rule table, one rule
#: per characterization kind, first match wins, trailing total default
#: (AC-01: templates define the required raw data + instrument metadata;
#: AC-02: missing required metadata routes to the Assumption Registry
#: pathway; AC-03: the table is universal).
CHARACTERIZATION_REQUIREMENT_RULES: tuple[TemplateRequirementRule, ...] = (
    TemplateRequirementRule(
        rule_id="R-CHA-P1",
        description=(
            "PXRD records the instrument, the radiation type and the"
            " radiation wavelength (instrument metadata), the scan range"
            " and step size (raw data capture metadata), and the scan"
            " temperature"
        ),
        required_parameters=(
            "instrument",
            "radiation_type",
            "wavelength_A",
            "two_theta_min_deg",
            "two_theta_max_deg",
            "step_size_deg",
            "scan_temperature_K",
        ),
        predicate=_kind_in(CharacterizationKind.PXRD),
    ),
    TemplateRequirementRule(
        rule_id="R-CHA-P2",
        description=(
            "SCXRD records the instrument, the radiation type, the"
            " radiation wavelength, the detector (instrument metadata), the"
            " data-collection temperature and the structure resolution"
            " limit (raw data metadata)"
        ),
        required_parameters=(
            "instrument",
            "radiation_type",
            "wavelength_A",
            "collection_temperature_K",
            "resolution_limit_A",
            "detector",
        ),
        predicate=_kind_in(CharacterizationKind.SCXRD),
    ),
    TemplateRequirementRule(
        rule_id="R-CHA-P3",
        description=(
            "TGA records the instrument, the atmosphere and the purge gas"
            " flow (instrument metadata), the heating rate, the final"
            " temperature, the sample mass and the analysis duration (raw"
            " data metadata)"
        ),
        required_parameters=(
            "instrument",
            "atmosphere",
            "heating_rate_K_min",
            "final_temperature_K",
            "sample_mass_mg",
            "gas_flow_ml_min",
            "scan_duration_h",
        ),
        predicate=_kind_in(CharacterizationKind.TGA),
    ),
    TemplateRequirementRule(
        rule_id="R-CHA-P4",
        description=(
            "spectroscopy records the instrument, the technique and the"
            " spectral resolution (instrument metadata), the spectral range"
            " and the scan count (raw data metadata)"
        ),
        required_parameters=(
            "instrument",
            "technique",
            "wavenumber_min_cm_1",
            "wavenumber_max_cm_1",
            "resolution_cm_1",
            "number_of_scans",
        ),
        predicate=_kind_in(CharacterizationKind.SPECTROSCOPY),
    ),
    TemplateRequirementRule(
        rule_id="R-CHA-P0",
        description=(
            "no rule declares a required metadata set for this"
            " characterization kind (total default)"
        ),
        required_parameters=(),
        predicate=lambda kind: True,
    ),
)


def validate_characterization_rulesets() -> tuple[str, ...]:
    """Validate the characterization rule tables' integrity; return the ids.

    A valid requirement table is non-empty, has unique rule ids, declares a
    rule for every characterization kind (the evaluation is a total
    function of the kind), and its trailing rule matches every kind (the
    total default that guarantees first-match evaluation is total). The
    metadata value table has unique rule ids and exactly one rule per
    parameter name. The acceptance-parameter table covers exactly the
    acceptance-parameter vocabulary with one rule per name, and every name
    the analysis-acceptance rules reference exists in that vocabulary. The
    analysis-acceptance table has unique rule ids and covers every
    characterization kind with at least one rule. The check-outcome table
    is non-empty with a trailing total default.

    Raises:
        CharacterizationTemplateError: a table violates the frozen shape
            (stable messages).
    """
    tables = (
        ("requirement", CHARACTERIZATION_REQUIREMENT_RULES),
        ("metadata value", CHARACTERIZATION_VALUE_RULES),
        ("acceptance parameter", ACCEPTANCE_PARAMETER_RULES),
        ("analysis acceptance", ANALYSIS_ACCEPTANCE_RULES),
        ("check outcome", CHECK_OUTCOME_RULES),
    )
    all_ids: list[str] = []
    for label, table in tables:
        ids = tuple(rule.rule_id for rule in table)
        duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if duplicates:
            raise CharacterizationTemplateError(
                f"duplicate rule id(s) in the {label} rule table:"
                f" {', '.join(duplicates)}"
            )
        if not ids:
            raise CharacterizationTemplateError(
                f"the {label} rule table must not be empty"
            )
        all_ids.extend(ids)
    requirement_ids = tuple(rule.rule_id for rule in CHARACTERIZATION_REQUIREMENT_RULES)
    covered = {
        kind
        for rule in CHARACTERIZATION_REQUIREMENT_RULES
        for kind in CharacterizationKind
        if rule.predicate(kind)
    }
    if covered != set(CharacterizationKind):
        missing = sorted(
            kind.value
            for kind in CharacterizationKind
            if kind not in covered
        )
        raise CharacterizationTemplateError(
            "the requirement rule table must cover every characterization"
            f" kind, missing: {', '.join(missing)}"
        )
    default_rule = CHARACTERIZATION_REQUIREMENT_RULES[-1]
    for kind in CharacterizationKind:
        if not default_rule.predicate(kind):
            raise CharacterizationTemplateError(
                f"the trailing rule {default_rule.rule_id!r} is not a total"
                f" default: it does not match kind {kind.value!r}"
            )
    if requirement_ids[-1] != "R-CHA-P0":
        raise CharacterizationTemplateError(
            "the requirement rule table's trailing rule must be the total"
            f" default R-CHA-P0, got {requirement_ids[-1]!r}"
        )
    value_parameters = [rule.parameter for rule in CHARACTERIZATION_VALUE_RULES]
    duplicated_parameters = sorted(
        {
            parameter
            for parameter in value_parameters
            if value_parameters.count(parameter) > 1
        }
    )
    if duplicated_parameters:
        raise CharacterizationTemplateError(
            "the metadata value rule table declares more than one rule for"
            f" parameter(s): {', '.join(duplicated_parameters)}"
        )
    acceptance_parameters = [rule.parameter for rule in ACCEPTANCE_PARAMETER_RULES]
    duplicated_acceptance = sorted(
        {
            parameter
            for parameter in acceptance_parameters
            if acceptance_parameters.count(parameter) > 1
        }
    )
    if duplicated_acceptance:
        raise CharacterizationTemplateError(
            "the acceptance parameter rule table declares more than one"
            " rule for parameter(s):"
            f" {', '.join(duplicated_acceptance)}"
        )
    if set(acceptance_parameters) != ACCEPTANCE_PARAMETERS:
        raise CharacterizationTemplateError(
            "the acceptance parameter rule table does not declare exactly"
            " the acceptance-parameter vocabulary"
        )
    referenced = {
        rule.acceptance_parameter
        for rule in ANALYSIS_ACCEPTANCE_RULES
        if rule.acceptance_parameter is not None
    }
    undeclared = sorted(referenced - ACCEPTANCE_PARAMETERS)
    if undeclared:
        raise CharacterizationTemplateError(
            "the analysis acceptance rules reference undeclared acceptance"
            f" parameter(s): {', '.join(undeclared)}"
        )
    acceptance_kinds_covered: set[CharacterizationKind] = set()
    for rule in ANALYSIS_ACCEPTANCE_RULES:
        acceptance_kinds_covered.update(rule.kinds)
    if acceptance_kinds_covered != set(CharacterizationKind):
        missing_kinds = sorted(
            kind.value
            for kind in CharacterizationKind
            if kind not in acceptance_kinds_covered
        )
        raise CharacterizationTemplateError(
            "the analysis acceptance rule table must cover every"
            " characterization kind, missing:"
            f" {', '.join(missing_kinds)}"
        )
    for kind in CharacterizationKind:
        measurements = [
            rule.measurement
            for rule in ANALYSIS_ACCEPTANCE_RULES
            if kind in rule.kinds
        ]
        duplicated_measurements = sorted(
            {
                measurement
                for measurement in measurements
                if measurements.count(measurement) > 1
            }
        )
        if duplicated_measurements:
            raise CharacterizationTemplateError(
                "the analysis acceptance rule table declares more than one"
                f" rule for kind {kind.value!r} measurement(s):"
                f" {', '.join(duplicated_measurements)}"
            )
    trailing_outcome = CHECK_OUTCOME_RULES[-1]
    if not all(
        trailing_outcome.predicate(outcomes)
        for outcomes in (
            (),
            (CheckOutcome.FAIL,),
            (CheckOutcome.PENDING,),
            (CheckOutcome.PASS,),
        )
    ):
        raise CharacterizationTemplateError(
            f"the trailing check-outcome rule {trailing_outcome.rule_id!r}"
            " must be a total default"
        )
    return tuple(all_ids)


# ---------------------------------------------------------------------------
# Metadata assessments (recorded rule decisions, AC-01)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetadataCompletenessDecision:
    """Record of one requirement-rule evaluation for a template."""

    rule_id: str
    description: str
    matched: bool
    required_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...]


@dataclass(frozen=True)
class MetadataCompletenessAssessment:
    """Full, auditable result of a template's metadata-completeness check.

    ``matched_rule_id`` names the deciding rule (``None`` is impossible:
    the trailing default rule always matches); ``missing_parameters`` are
    the required raw data / instrument metadata parameters of the kind
    that the template does not record -- the exact input of the Assumption
    Registry routing (AC-01, AC-02).
    """

    template_id: str
    kind: CharacterizationKind
    present_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...]
    decisions: tuple[MetadataCompletenessDecision, ...]
    matched_rule_id: str
    ruleset_version: str = CHARACTERIZATION_RULESET_VERSION


@dataclass(frozen=True)
class MetadataValueDecision:
    """Record of one universal metadata value-rule evaluation."""

    rule_id: str
    description: str
    parameter: str
    applied: bool
    valid: bool
    violation: str | None


@dataclass(frozen=True)
class MetadataValueAssessment:
    """Full, auditable result of a template's metadata value validation.

    ``violations`` carries the stable messages of every violated rule
    (empty when the template's present parameters all satisfy the
    universal metadata value rules). ``matched_rule_id`` is the id of the
    first violation in table order (``None`` when no rule is violated).
    """

    template_id: str
    violations: tuple[str, ...]
    decisions: tuple[MetadataValueDecision, ...]
    matched_rule_id: str | None
    ruleset_version: str = CHARACTERIZATION_RULESET_VERSION


# ---------------------------------------------------------------------------
# The analysis plan (AC-02: freezable separately from execution)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnalysisPlan:
    """The freezable analysis protocol and acceptance criteria (AC-02).

    A pure metadata record: the analysis protocol description, the ordered
    protocol steps and the instance-data acceptance threshold parameters
    (e.g. a recorded peak-position tolerance -- the VALUES are instance
    data, ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS5 templates, never
    universal thresholds). The plan is frozen by
    :func:`freeze_analysis_plan` -- a Supervisor-only decision that needs
    NO execution artifacts: analysis protocol and acceptance criteria are
    frozen separately from execution. Acceptance threshold names must come
    from the universal ``ACCEPTANCE_PARAMETERS`` vocabulary and every
    threshold value must satisfy its universal shape rule.

    Raises:
        TypeError: a field has the wrong type.
        InvalidAnalysisPlanError: a value violation (empty protocol, empty
            step, no steps).
        InvalidAcceptanceCriteriaError: an acceptance parameter is unknown
            or its value violates its universal shape rule.
    """

    protocol: str
    protocol_steps: tuple[str, ...]
    acceptance_parameters: dict[str, float] = field(default_factory=dict)
    frozen: bool = False
    notes: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.protocol, str):
            raise TypeError(
                f"AnalysisPlan.protocol must be a str, got"
                f" {type(self.protocol).__name__}"
            )
        if not self.protocol.strip():
            raise InvalidAnalysisPlanError(
                "AnalysisPlan.protocol must be a non-empty string, got"
                f" {self.protocol!r}"
            )
        if not isinstance(self.protocol_steps, tuple):
            raise TypeError(
                "AnalysisPlan.protocol_steps must be a tuple, got"
                f" {type(self.protocol_steps).__name__}"
            )
        if not self.protocol_steps:
            raise InvalidAnalysisPlanError(
                "AnalysisPlan.protocol_steps must be a non-empty tuple of"
                " non-empty strings"
            )
        for step in self.protocol_steps:
            if not isinstance(step, str):
                raise TypeError(
                    "AnalysisPlan.protocol_steps must contain only strings,"
                    f" got {type(step).__name__}"
                )
            if not step.strip():
                raise InvalidAnalysisPlanError(
                    "AnalysisPlan.protocol_steps must be a non-empty tuple"
                    " of non-empty strings"
                )
        if not isinstance(self.acceptance_parameters, dict):
            raise TypeError(
                "AnalysisPlan.acceptance_parameters must be a dict, got"
                f" {type(self.acceptance_parameters).__name__}"
            )
        for parameter, threshold in self.acceptance_parameters.items():
            if not isinstance(parameter, str):
                raise TypeError(
                    "AnalysisPlan.acceptance_parameters keys must be"
                    f" strings, got {type(parameter).__name__}"
                )
            if parameter not in ACCEPTANCE_PARAMETERS:
                known = ", ".join(sorted(ACCEPTANCE_PARAMETERS))
                raise InvalidAcceptanceCriteriaError(
                    f"unknown acceptance parameter {parameter!r}; the"
                    f" universal acceptance-parameter vocabulary is:"
                    f" {known}"
                )
            for rule in ACCEPTANCE_PARAMETER_RULES:
                if rule.parameter != parameter:
                    continue
                if not rule.predicate(threshold):
                    raise InvalidAcceptanceCriteriaError(
                        rule.message.format(value=threshold)
                    )
        if not isinstance(self.frozen, bool):
            raise TypeError(
                f"AnalysisPlan.frozen must be a bool, got"
                f" {type(self.frozen).__name__}"
            )
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError(
                f"AnalysisPlan.notes must be a str or None, got"
                f" {type(self.notes).__name__}"
            )
        # Defensive copy: the frozen plan owns its threshold table.
        object.__setattr__(
            self, "acceptance_parameters", dict(self.acceptance_parameters)
        )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (protocol-capture shape)."""
        return {
            "protocol": self.protocol,
            "protocol_steps": list(self.protocol_steps),
            "acceptance_parameters": dict(
                sorted(self.acceptance_parameters.items())
            ),
            "frozen": self.frozen,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Acceptance / identity-check assessments (recorded decision contracts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceDecision:
    """Record of one analysis-acceptance rule evaluation.

    ``applied`` means the rule matches the template's kind and every input
    it needs is present; ``pending`` means the rule matches the kind but a
    required input (a plan threshold or a recorded measurement) is missing
    -- the missing measurements are the exact input of the Assumption
    Registry pathway; ``passed`` means the frozen contract's predicate
    decided the check holds.
    """

    rule_id: str
    description: str
    applied: bool
    passed: bool
    pending: bool
    detail: str | None


@dataclass(frozen=True)
class AcceptanceAssessment:
    """Full, auditable result of an analysis-acceptance evaluation.

    ``outcome`` aggregates the per-rule decisions through the ordered
    ``CHECK_OUTCOME_RULES`` table (first match wins, trailing total
    default): FAIL when any applied rule failed, PENDING when none failed
    but some inputs are missing, else PASS. ``matched_rule_id`` names the
    deciding outcome rule (never ``None``); ``matched_item_id`` names the
    first deciding item (the first failed or first pending rule, else
    ``None``); ``pending_measurements`` are the recorded-fact keys the
    applied rules require but the measurements do not carry.
    """

    template_id: str
    kind: CharacterizationKind
    plan_frozen: bool
    decisions: tuple[AcceptanceDecision, ...]
    outcome: CheckOutcome
    matched_rule_id: str
    matched_item_id: str | None
    pending_measurements: tuple[str, ...]
    ruleset_version: str = CHARACTERIZATION_RULESET_VERSION


@dataclass(frozen=True)
class IdentityCheckDecision:
    """Record of one PXRD identity/quality check evaluation (AC-03).

    The check outcome is decided by the frozen contract -- a pure rule
    predicate over recorded measurement facts and the plan's recorded
    thresholds -- never by a worker self-decision.
    """

    check_id: str
    description: str
    applied: bool
    passed: bool
    outcome: CheckOutcome
    detail: str | None


@dataclass(frozen=True)
class IdentityCheckAssessment:
    """Full, auditable result of a PXRD identity/quality check evaluation.

    The PXRD identity/quality decision record (AC-03): every check
    decision of the ordered ``PXRD_IDENTITY_CHECKS`` table, the aggregate
    ``outcome`` decided by the ordered ``CHECK_OUTCOME_RULES`` table, the
    deciding outcome rule id and the deciding check id. ``PENDING`` checks
    (measurement facts not recorded) are never silently skipped: they are
    the exact input of the Assumption Registry pathway.
    """

    template_id: str
    kind: CharacterizationKind
    plan_frozen: bool
    checks: tuple[IdentityCheckDecision, ...]
    outcome: CheckOutcome
    matched_rule_id: str
    matched_check_id: str | None
    pending_measurements: tuple[str, ...]
    ruleset_version: str = CHARACTERIZATION_RULESET_VERSION


# ---------------------------------------------------------------------------
# The templates (frozen dataclasses, strict __post_init__)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CharacterizationTemplateBase:
    """Frozen base of every PXRD/SCXRD/TGA/spectroscopy template.

    Common shape: a safe ``template_id``, a title, the characterization
    kind, the strict/recovery ``track`` label (frozen ``GoalTrack``
    vocabulary), the recorded raw data / instrument metadata parameters
    (instance data -- material-specific values live here, never in the
    rule tables, AC-03), the freezable analysis plan (AC-02; ``None`` when
    no analysis plan is recorded yet -- an absent plan is a real planning
    state, never a placeholder), the Assumption Registry refs of routed
    missing metadata (AC-02) and the freeze flag.

    Construction enforces the universal metadata value rules over the
    parameters that are present; required metadata may be missing -- they
    are the input of the Assumption Registry pathway (AC-01), not a
    construction error. Nothing is ever frozen by construction: the only
    ways to produce a frozen analysis plan or a frozen template are
    :func:`freeze_analysis_plan` and :func:`freeze_characterization_template`,
    both gated by the Supervisor-only plan-freeze permission
    (``core/permissions.py``).

    Raises:
        TypeError: a field has the wrong type.
        InvalidCharacterizationTemplateError: a value violation (unsafe
            template id, metadata value-rule violation, unknown kind for
            the class).
    """

    template_id: str
    title: str
    characterization_kind: CharacterizationKind
    track: GoalTrack = GoalTrack.STRICT_REPRODUCTION
    parameters: dict[str, Any] = field(default_factory=dict)
    assumption_refs: tuple[str, ...] = ()
    frozen: bool = False
    analysis: AnalysisPlan | None = None
    notes: str | None = None

    #: The kinds this template class accepts (subclasses narrow this).
    _ALLOWED_KINDS: ClassVar[tuple[CharacterizationKind, ...]] = tuple(
        CharacterizationKind
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
            raise InvalidCharacterizationTemplateError(
                f"{type(self).__name__}.title must be a non-empty string,"
                f" got {self.title!r}"
            )
        if not isinstance(self.characterization_kind, CharacterizationKind):
            raise TypeError(
                f"{type(self).__name__}.characterization_kind must be a"
                " CharacterizationKind member, got"
                f" {type(self.characterization_kind).__name__}"
            )
        _validate_template_id(type(self).__name__, self.template_id)
        if self.characterization_kind not in type(self)._ALLOWED_KINDS:
            allowed = ", ".join(kind.value for kind in type(self)._ALLOWED_KINDS)
            raise InvalidCharacterizationTemplateError(
                f"{type(self).__name__} does not accept characterization"
                f" kind {self.characterization_kind.value!r}; allowed"
                f" kinds: {allowed}"
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
        if self.analysis is not None and not isinstance(self.analysis, AnalysisPlan):
            raise TypeError(
                f"{type(self).__name__}.analysis must be an AnalysisPlan or"
                f" None, got {type(self.analysis).__name__}"
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
        assessment = validate_metadata_values(self)
        if assessment.violations:
            details = "; ".join(assessment.violations)
            raise InvalidCharacterizationTemplateError(
                f"invalid {self.characterization_kind.value} template"
                f" {self.template_id!r}: {details}"
            )


@dataclass(frozen=True)
class PXRDCharacterizationTemplate(CharacterizationTemplateBase):
    """PXRD characterization template (AC-01, AC-03).

    Fixed ``characterization_kind`` ``PXRD``; records the raw data +
    instrument metadata of a powder X-ray diffraction measurement. The PXRD
    identity/quality checks are decided by :func:`evaluate_identity_checks`
    through the frozen ``PXRD_IDENTITY_CHECKS`` contract.
    """

    _ALLOWED_KINDS: ClassVar[tuple[CharacterizationKind, ...]] = (
        CharacterizationKind.PXRD,
    )

    characterization_kind: CharacterizationKind = CharacterizationKind.PXRD


@dataclass(frozen=True)
class SCXRDCharacterizationTemplate(CharacterizationTemplateBase):
    """SCXRD characterization template (AC-01).

    Fixed ``characterization_kind`` ``SCXRD``; records the raw data +
    instrument metadata of a single-crystal X-ray diffraction / structure
    verification measurement.
    """

    _ALLOWED_KINDS: ClassVar[tuple[CharacterizationKind, ...]] = (
        CharacterizationKind.SCXRD,
    )

    characterization_kind: CharacterizationKind = CharacterizationKind.SCXRD


@dataclass(frozen=True)
class TGACharacterizationTemplate(CharacterizationTemplateBase):
    """TGA / thermal-analysis characterization template (AC-01).

    Fixed ``characterization_kind`` ``TGA``; records the raw data +
    instrument metadata of a thermogravimetric measurement.
    """

    _ALLOWED_KINDS: ClassVar[tuple[CharacterizationKind, ...]] = (
        CharacterizationKind.TGA,
    )

    characterization_kind: CharacterizationKind = CharacterizationKind.TGA


@dataclass(frozen=True)
class SpectroscopyCharacterizationTemplate(CharacterizationTemplateBase):
    """Spectroscopy / basic identity characterization template (AC-01).

    Fixed ``characterization_kind`` ``SPECTROSCOPY``; records the raw data
    + instrument metadata of a basic identity spectroscopy measurement
    (e.g. IR/FTIR band-position identity checks).
    """

    _ALLOWED_KINDS: ClassVar[tuple[CharacterizationKind, ...]] = (
        CharacterizationKind.SPECTROSCOPY,
    )

    characterization_kind: CharacterizationKind = CharacterizationKind.SPECTROSCOPY


def _validate_template_id(class_name: str, value: str) -> None:
    """Reject template ids that escape registries or break glob listings.

    Safe single registry path segment (FND-M9-G02-01 lesson): no path
    separators, no glob metacharacters, not empty, not ``.``/``..``.
    """
    if not value.strip() or value in (".", ".."):
        raise InvalidCharacterizationTemplateError(
            f"{class_name}.template_id must be a non-empty safe registry"
            f" id, got {value!r}"
        )
    if "/" in value or "\\" in value:
        raise InvalidCharacterizationTemplateError(
            f"{class_name}.template_id must be a safe single path segment"
            f" (no '/', no '\\'), got {value!r}"
        )
    if any(char.isspace() for char in value):
        raise InvalidCharacterizationTemplateError(
            f"{class_name}.template_id must not contain whitespace, got"
            f" {value!r}"
        )
    if any(char in value for char in "*?[]"):
        raise InvalidCharacterizationTemplateError(
            f"{class_name}.template_id must not contain glob"
            f" metacharacters, got {value!r}"
        )


# ---------------------------------------------------------------------------
# Universal evaluation (pure and deterministic)
# ---------------------------------------------------------------------------


def _rule_for_kind(kind: CharacterizationKind) -> TemplateRequirementRule:
    """The required-metadata rule of a kind (first match wins)."""
    for rule in CHARACTERIZATION_REQUIREMENT_RULES:
        if rule.predicate(kind):
            return rule
    # The trailing total default always matches (validate_characterization_rulesets
    # guarantees it); this line is unreachable.
    return CHARACTERIZATION_REQUIREMENT_RULES[-1]


def assess_metadata_completeness(
    template: CharacterizationTemplateBase,
) -> MetadataCompletenessAssessment:
    """Evaluate a template's required raw data / instrument metadata (AC-01).

    Pure and deterministic: the assessment is a pure function of the
    template's kind and recorded parameter names, decided by the ordered
    ``CHARACTERIZATION_REQUIREMENT_RULES`` table (first match wins; the
    trailing default rule always matches). The assessment records every
    rule decision, the matched rule id and the missing metadata.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    recorded = set(template.parameters)
    decisions: list[MetadataCompletenessDecision] = []
    matched_rule_id: str | None = None
    matched_required: tuple[str, ...] = ()
    for rule in CHARACTERIZATION_REQUIREMENT_RULES:
        matched = rule.predicate(template.characterization_kind)
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
            MetadataCompletenessDecision(
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
    return MetadataCompletenessAssessment(
        template_id=template.template_id,
        kind=template.characterization_kind,
        present_parameters=tuple(sorted(recorded)),
        missing_parameters=missing,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


def missing_metadata(template: CharacterizationTemplateBase) -> tuple[str, ...]:
    """The required raw data / instrument metadata the template does not record.

    The exact input of the Assumption Registry routing (AC-01, AC-02).

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``.
    """
    return assess_metadata_completeness(template).missing_parameters


def validate_metadata_values(
    template: CharacterizationTemplateBase,
) -> MetadataValueAssessment:
    """Validate the template's present metadata by the universal table.

    Pure and deterministic: every ``CHARACTERIZATION_VALUE_RULES`` rule
    whose parameter the template records is applied; violations are
    collected as stable messages (``matched_rule_id`` names the first
    violated rule in table order). The template constructor enforces this
    assessment; the public hook makes the decision auditable.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    violations: list[str] = []
    matched_rule_id: str | None = None
    decisions: list[MetadataValueDecision] = []
    for rule in CHARACTERIZATION_VALUE_RULES:
        if rule.parameter not in template.parameters:
            decisions.append(
                MetadataValueDecision(
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
            MetadataValueDecision(
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
    return MetadataValueAssessment(
        template_id=template.template_id,
        violations=tuple(violations),
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Acceptance and identity-check evaluation (AC-02/AC-03: contract decides)
# ---------------------------------------------------------------------------


def _evaluate_check_outcome(
    outcomes: tuple[CheckOutcome, ...],
) -> tuple[CheckOutcome, str]:
    """The aggregate outcome of a set of item outcomes (first match wins).

    The ordered ``CHECK_OUTCOME_RULES`` table decides: any FAIL decides
    FAIL, else any PENDING decides PENDING, else the trailing total default
    decides PASS. Returns the outcome and the deciding rule id (never
    ``None``: the trailing default always matches).
    """
    matched_rule_id: str | None = None
    matched_outcome = CheckOutcome.PASS  # unreachable default
    for rule in CHECK_OUTCOME_RULES:
        if rule.predicate(outcomes):
            matched_rule_id = rule.rule_id
            matched_outcome = rule.outcome
            break
    # The trailing total default always matches, so this can never be None.
    assert matched_rule_id is not None
    return matched_outcome, matched_rule_id


def _require_measurements_dict(measurements: object) -> dict[str, Any]:
    """Type gate for recorded measurement facts (stable TypeError)."""
    if not isinstance(measurements, dict):
        raise TypeError(
            "measurements must be a dict, got"
            f" {type(measurements).__name__}"
        )
    for key in measurements:
        if not isinstance(key, str):
            raise TypeError(
                f"measurements keys must be strings, got {type(key).__name__}"
            )
    return measurements


def evaluate_acceptance(
    template: CharacterizationTemplateBase,
    measurements: dict[str, Any],
) -> AcceptanceAssessment:
    """Evaluate the template's acceptance criteria over recorded measurements.

    Pure and deterministic (AC-02): every ``ANALYSIS_ACCEPTANCE_RULES``
    rule whose kind matches the template is applied to the recorded
    measurement facts and the plan's recorded thresholds; the frozen
    contract decides every criterion (AC-03: the worker records facts, the
    rule table decides). The assessment records every rule decision, the
    aggregate outcome (``CHECK_OUTCOME_RULES``), the deciding rule and
    item ids, and the pending measurement facts (the input of the
    Assumption Registry pathway).

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            or ``measurements`` is not a dict of string keys.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    measurements = _require_measurements_dict(measurements)
    # The stable messages of the acceptance rules may name both the
    # recorded measurement facts and the plan's recorded thresholds.
    plan = template.analysis
    format_values = (
        {**plan.acceptance_parameters, **measurements} if plan is not None else {}
    )
    decisions: list[AcceptanceDecision] = []
    pending_measurements: list[str] = []
    item_outcomes: list[CheckOutcome] = []
    for rule in ANALYSIS_ACCEPTANCE_RULES:
        if template.characterization_kind not in rule.kinds:
            decisions.append(
                AcceptanceDecision(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    pending=False,
                    detail=None,
                )
            )
            continue
        if plan is None:
            pending_measurements.append(rule.measurement)
            decisions.append(
                AcceptanceDecision(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    pending=True,
                    detail=(
                        "no analysis plan is recorded on the template"
                    ),
                )
            )
            item_outcomes.append(CheckOutcome.PENDING)
            continue
        if (
            rule.acceptance_parameter is not None
            and rule.acceptance_parameter not in plan.acceptance_parameters
        ):
            decisions.append(
                AcceptanceDecision(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    pending=True,
                    detail=(
                        f"acceptance parameter {rule.acceptance_parameter!r}"
                        " is not recorded on the analysis plan"
                    ),
                )
            )
            item_outcomes.append(CheckOutcome.PENDING)
            continue
        assert plan is not None  # unreachable: handled above
        threshold: float | None = (
            None
            if rule.acceptance_parameter is None
            else plan.acceptance_parameters[rule.acceptance_parameter]
        )
        if rule.measurement not in measurements:
            pending_measurements.append(rule.measurement)
            decisions.append(
                AcceptanceDecision(
                    rule_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    pending=True,
                    detail=(
                        f"measurement {rule.measurement!r} is not recorded"
                    ),
                )
            )
            item_outcomes.append(CheckOutcome.PENDING)
            continue
        passed = rule.predicate(threshold, measurements)
        detail = None if passed else rule.message.format(**format_values)
        decisions.append(
            AcceptanceDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                applied=True,
                passed=passed,
                pending=False,
                detail=detail,
            )
        )
        item_outcomes.append(CheckOutcome.PASS if passed else CheckOutcome.FAIL)
    outcome, matched_rule_id = _evaluate_check_outcome(tuple(item_outcomes))
    matched_item_id: str | None = None
    if outcome is CheckOutcome.FAIL or outcome is CheckOutcome.PENDING:
        for decision in decisions:
            if outcome is CheckOutcome.FAIL and decision.applied and not decision.passed:
                matched_item_id = decision.rule_id
                break
            if outcome is CheckOutcome.PENDING and decision.pending:
                matched_item_id = decision.rule_id
                break
    return AcceptanceAssessment(
        template_id=template.template_id,
        kind=template.characterization_kind,
        plan_frozen=plan is not None and plan.frozen,
        decisions=tuple(decisions),
        outcome=outcome,
        matched_rule_id=matched_rule_id,
        matched_item_id=matched_item_id,
        pending_measurements=tuple(sorted(pending_measurements)),
        ruleset_version=CHARACTERIZATION_RULESET_VERSION,
    )


def evaluate_identity_checks(
    template: CharacterizationTemplateBase,
    measurements: dict[str, Any],
) -> IdentityCheckAssessment:
    """Evaluate the PXRD identity/quality checks as a decision record (AC-03).

    Pure and deterministic: the PXRD identity/quality checks of
    ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS5 (phase identification,
    peak-position agreement, intensity-pattern comparison with caution for
    preferred orientation, and batch consistency) are REPRESENTED by the
    ordered ``PXRD_IDENTITY_CHECKS`` contract and evaluated by it -- the
    worker records measurement FACTS, the frozen check predicates and the
    plan's recorded thresholds decide PASS/FAIL/PENDING. The result is a
    full decision record: every check decision, the aggregate outcome
    (``CHECK_OUTCOME_RULES``), the deciding outcome rule and check ids and
    the pending measurement facts. There is no worker outcome input
    anywhere in the signature: the checks never rely on a worker
    self-decision.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            or ``measurements`` is not a dict of string keys.
        InvalidCharacterizationTemplateError: the template is not a PXRD
            template (identity/quality checks are a PXRD contract).
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    if template.characterization_kind is not CharacterizationKind.PXRD:
        raise InvalidCharacterizationTemplateError(
            "identity/quality checks are a PXRD contract, but template"
            f" {template.template_id!r} has characterization kind"
            f" {template.characterization_kind.value!r}"
        )
    measurements = _require_measurements_dict(measurements)
    # The stable messages of the identity checks may name both the
    # recorded measurement facts and the plan's recorded thresholds.
    plan = template.analysis
    format_values = (
        {**plan.acceptance_parameters, **measurements} if plan is not None else {}
    )
    checks: list[IdentityCheckDecision] = []
    pending_measurements: list[str] = []
    item_outcomes: list[CheckOutcome] = []
    for rule in PXRD_IDENTITY_CHECKS:
        if plan is None:
            pending_measurements.append(rule.measurement)
            checks.append(
                IdentityCheckDecision(
                    check_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    outcome=CheckOutcome.PENDING,
                    detail="no analysis plan is recorded on the template",
                )
            )
            item_outcomes.append(CheckOutcome.PENDING)
            continue
        if (
            rule.acceptance_parameter is not None
            and rule.acceptance_parameter not in plan.acceptance_parameters
        ):
            checks.append(
                IdentityCheckDecision(
                    check_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    outcome=CheckOutcome.PENDING,
                    detail=(
                        f"acceptance parameter {rule.acceptance_parameter!r}"
                        " is not recorded on the analysis plan"
                    ),
                )
            )
            item_outcomes.append(CheckOutcome.PENDING)
            continue
        assert plan is not None  # unreachable: handled above
        threshold: float | None = (
            None
            if rule.acceptance_parameter is None
            else plan.acceptance_parameters[rule.acceptance_parameter]
        )
        if rule.measurement not in measurements:
            pending_measurements.append(rule.measurement)
            checks.append(
                IdentityCheckDecision(
                    check_id=rule.rule_id,
                    description=rule.description,
                    applied=False,
                    passed=False,
                    outcome=CheckOutcome.PENDING,
                    detail=f"measurement {rule.measurement!r} is not recorded",
                )
            )
            item_outcomes.append(CheckOutcome.PENDING)
            continue
        passed = rule.predicate(threshold, measurements)
        outcome = CheckOutcome.PASS if passed else CheckOutcome.FAIL
        checks.append(
            IdentityCheckDecision(
                check_id=rule.rule_id,
                description=rule.description,
                applied=True,
                passed=passed,
                outcome=outcome,
                detail=(
                    None
                    if passed
                    else rule.message.format(**format_values)
                ),
            )
        )
        item_outcomes.append(outcome)
    outcome, matched_rule_id = _evaluate_check_outcome(tuple(item_outcomes))
    matched_check_id: str | None = None
    if outcome is CheckOutcome.FAIL or outcome is CheckOutcome.PENDING:
        for check in checks:
            if outcome is CheckOutcome.FAIL and check.applied and not check.passed:
                matched_check_id = check.check_id
                break
            if outcome is CheckOutcome.PENDING and check.outcome is CheckOutcome.PENDING:
                matched_check_id = check.check_id
                break
    return IdentityCheckAssessment(
        template_id=template.template_id,
        kind=template.characterization_kind,
        plan_frozen=plan is not None and plan.frozen,
        checks=tuple(checks),
        outcome=outcome,
        matched_rule_id=matched_rule_id,
        matched_check_id=matched_check_id,
        pending_measurements=tuple(sorted(pending_measurements)),
        ruleset_version=CHARACTERIZATION_RULESET_VERSION,
    )


# ---------------------------------------------------------------------------
# Freeze helpers (AC-02: frozen separately from execution, Supervisor-only)
# ---------------------------------------------------------------------------


def _check_plan_freeze_permission(role: Role, template: CharacterizationTemplateBase) -> None:
    """Gate a freeze request by the frozen role-action matrix (DEV-M6-G03).

    Raises:
        TypeError: ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    assessment = check_action_allowed(role, Action.PLAN_FREEZE)
    if not assessment.allowed:
        raise PermissionDeniedError(
            f"role {role.value!r} may not freeze the analysis plan of"
            f" characterization template {template.template_id!r}: freezing"
            " is a Supervisor-only decision (the plan-freeze action of the"
            " frozen role-action matrix)",
            assessment,
        )


def freeze_analysis_plan(
    template: CharacterizationTemplateBase, *, role: Role
) -> CharacterizationTemplateBase:
    """Freeze the analysis protocol and acceptance criteria (AC-02).

    Freezes the template's :class:`AnalysisPlan` (protocol + acceptance
    thresholds) -- a Supervisor-only decision through the frozen role-action
    matrix (``Action.PLAN_FREEZE``, granted only to the Supervisor by
    ``R-PRM-SUP1``). The pure function requires NO execution artifacts: the
    analysis protocol and acceptance criteria are frozen separately from
    execution, and the input template is never mutated.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            or ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
        InvalidAnalysisPlanError: the template records no analysis plan
            yet (an absent plan is a real planning state -- it must be
            recorded before it can be frozen).
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    if template.analysis is None:
        raise InvalidAnalysisPlanError(
            f"the analysis plan of characterization template"
            f" {template.template_id!r} is not recorded; a plan must be"
            " recorded before it can be frozen"
        )
    _check_plan_freeze_permission(role, template)
    return replace(template, analysis=replace(template.analysis, frozen=True))


def freeze_characterization_template(
    template: CharacterizationTemplateBase, *, role: Role
) -> CharacterizationTemplateBase:
    """Freeze a characterization template -- a Supervisor-only decision.

    Mirrors the synthesis freeze: the pure function returns a frozen copy
    (``frozen`` True) of the template, gated by the same plan-freeze
    action of the frozen role-action matrix; the input template is never
    mutated.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            or ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    _check_plan_freeze_permission(role, template)
    return replace(template, frozen=True)


# ---------------------------------------------------------------------------
# Assumption Registry routing (AC-01/AC-02: the existing pathway, never a copy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MissingMetadataRouting:
    """The Assumption Registry routing of a template's missing metadata.

    ``assumptions`` holds one real ``core.models.Assumption`` record per
    missing required raw data / instrument metadata parameter; ``effects``
    records, per assumption, the strict-status effect decided by the real
    ``core.rules.assumptions.assumption_effect`` API;
    ``strict_label_assessment`` is the real ``evaluate_strict_label``
    result over the routed assumption set; ``assumption_refs`` are the
    safe assumption ids the template carries
    (``CharacterizationTemplateBase.assumption_refs``).
    """

    template_id: str
    kind: CharacterizationKind
    missing_parameters: tuple[str, ...]
    assumptions: tuple[Assumption, ...]
    effects: tuple[AssumptionEffectDecision, ...]
    strict_label_assessment: StrictLabelAssessment
    assumption_refs: tuple[str, ...]


@dataclass(frozen=True)
class MissingMeasurementRouting:
    """The Assumption Registry routing of a plan's missing measurements.

    ``missing_measurements`` are the recorded-fact keys the acceptance
    rules of the template's kind require but the measurements do not carry
    (the ``PENDING`` checks of an acceptance/identity-check evaluation);
    ``assumptions`` holds one real ``core.models.Assumption`` record per
    missing measurement, with the same real-API effect/label readback as
    :class:`MissingMetadataRouting` (AC-02: pending checks enter the
    Assumption Registry, never a silent skip).
    """

    template_id: str
    kind: CharacterizationKind
    missing_measurements: tuple[str, ...]
    assumptions: tuple[Assumption, ...]
    effects: tuple[AssumptionEffectDecision, ...]
    strict_label_assessment: StrictLabelAssessment
    assumption_refs: tuple[str, ...]


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


def _route_assumptions(
    template: CharacterizationTemplateBase,
    missing_items: tuple[str, ...],
    *,
    classification: AssumptionClassification,
    rationale: str | None,
    source_refs: Sequence[str],
    affected_goal_ids: Sequence[str],
) -> tuple[tuple[Assumption, ...], tuple[AssumptionEffectDecision, ...], StrictLabelAssessment, tuple[str, ...]]:
    """Route missing items through the real Assumption Registry pathway.

    Shared core of the metadata/measurement routing: one real
    ``core.models.Assumption`` record per missing item (deterministic safe
    assumption id derived from the template id and the item), the real
    ``assumption_effect`` decision recorded on the entry, the real
    ``evaluate_strict_label`` readback over the set and the safe
    assumption refs.
    """
    assumptions: list[Assumption] = []
    for item in missing_items:
        assumption_id = generate_id("assumption", template.template_id, item)
        entry = Assumption(
            assumption_id=assumption_id,
            parameter=item,
            classification=classification,
            rationale=(
                rationale
                if rationale is not None
                else (
                    f"required {template.characterization_kind.value}"
                    f" metadata parameter {item!r} of characterization"
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
    return (
        routed,
        tuple(assumption_effect(assumption) for assumption in routed),
        evaluate_strict_label(routed),
        tuple(assumption.assumption_id for assumption in routed),
    )


def assumptions_for_missing_metadata(
    template: CharacterizationTemplateBase,
    *,
    classification: AssumptionClassification = AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
    rationale: str | None = None,
    source_refs: Sequence[str] = (),
    affected_goal_ids: Sequence[str] = (),
) -> MissingMetadataRouting:
    """Route the template's missing raw data / instrument metadata through the
    real Assumption Registry pathway (AC-01, AC-02).

    For every required raw data / instrument metadata parameter the
    template does not record, a real ``core.models.Assumption`` registry
    entry is constructed (deterministic safe assumption id derived from
    the template id and the parameter, ``core.ids.generate_id``), its
    strict-status effect is decided by the real
    ``core.rules.assumptions.assumption_effect`` and recorded on the
    entry, and the real ``core.rules.assumptions.evaluate_strict_label``
    reads the whole set back into the strict label. The default
    classification for a missing scientific parameter is
    ``A2_SCIENTIFIC_ASSUMPTION`` (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md
    SS5: missing scientifically meaningful settings are A2 unless reliable
    method evidence supports an A1 classification); an explicit
    classification is accepted verbatim.

    Args:
        template: the template whose missing metadata is routed.
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
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            ``classification`` is not an ``AssumptionClassification``
            member, ``rationale`` is not a str or None, or a ref/affected
            goal id is not a str.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
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
    missing = missing_metadata(template)
    routed, effects, label_assessment, refs = _route_assumptions(
        template,
        missing,
        classification=classification,
        rationale=rationale,
        source_refs=source_refs,
        affected_goal_ids=affected_goal_ids,
    )
    return MissingMetadataRouting(
        template_id=template.template_id,
        kind=template.characterization_kind,
        missing_parameters=missing,
        assumptions=routed,
        effects=effects,
        strict_label_assessment=label_assessment,
        assumption_refs=refs,
    )


def assumptions_for_missing_measurements(
    template: CharacterizationTemplateBase,
    measurements: dict[str, Any],
    *,
    classification: AssumptionClassification = AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
    rationale: str | None = None,
    source_refs: Sequence[str] = (),
    affected_goal_ids: Sequence[str] = (),
) -> MissingMeasurementRouting:
    """Route a template's pending acceptance measurements through the real
    Assumption Registry pathway (AC-02, AC-03).

    The recorded-fact keys the analysis-acceptance rules of the template's
    kind require but the measurements do not carry become real
    ``core.models.Assumption`` registry entries, with the same real
    ``assumption_effect`` / ``evaluate_strict_label`` readback as the
    metadata routing. A PENDING check is therefore never silently skipped
    by a worker: it is registered as an assumption.

    Args:
        template: the template whose acceptance measurements are routed.
        measurements: the recorded measurement facts (a dict of string
            keys).
        classification: the frozen assumption classification of every
            routed measurement (default A2).
        rationale: the assumption rationale (a stable default names the
            template and the measurement when omitted).
        source_refs: source identifiers backing the assumption.
        affected_goal_ids: goal ids the assumption affects (optional).

    Returns:
        The full routing: the missing measurement keys, the real
        assumption records, their real effects, the real strict label and
        the safe assumption refs.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            ``measurements`` is not a dict of string keys,
            ``classification`` is not an ``AssumptionClassification``
            member, ``rationale`` is not a str or None, or a ref/affected
            goal id is not a str.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    measurements = _require_measurements_dict(measurements)
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
    missing: list[str] = []
    for rule in ANALYSIS_ACCEPTANCE_RULES:
        if (
            template.characterization_kind in rule.kinds
            and rule.measurement not in measurements
            and rule.measurement not in missing
        ):
            missing.append(rule.measurement)
    # The same stable (sorted) order the acceptance assessment records:
    # the routing is byte-identical to the evaluation's pending list.
    missing_items = tuple(sorted(missing))
    routed, effects, label_assessment, refs = _route_assumptions(
        template,
        missing_items,
        classification=classification,
        rationale=rationale,
        source_refs=source_refs,
        affected_goal_ids=affected_goal_ids,
    )
    return MissingMeasurementRouting(
        template_id=template.template_id,
        kind=template.characterization_kind,
        missing_measurements=missing_items,
        assumptions=routed,
        effects=effects,
        strict_label_assessment=label_assessment,
        assumption_refs=refs,
    )


def apply_assumption_routing(
    template: CharacterizationTemplateBase,
    routing: MissingMetadataRouting | MissingMeasurementRouting,
) -> CharacterizationTemplateBase:
    """Return the template carrying the routed assumption refs (AC-02).

    Pure: a frozen copy of the template with ``assumption_refs`` set to
    the routing's safe assumption ids; the input template and the routing
    are never mutated.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``,
            or ``routing`` is not a metadata/measurement routing record.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(routing, (MissingMetadataRouting, MissingMeasurementRouting)):
        raise TypeError(
            "routing must be a MissingMetadataRouting or"
            f" MissingMeasurementRouting, got {type(routing).__name__}"
        )
    return replace(template, assumption_refs=routing.assumption_refs)


# ---------------------------------------------------------------------------
# Protocol capture (deterministic, pure)
# ---------------------------------------------------------------------------

#: The shape a captured characterization record must carry (protocol
#: capture deliverable; consumed by downstream execution-package builders).
CAPTURE_KEYS: tuple[str, ...] = (
    "template_id",
    "title",
    "characterization_kind",
    "track",
    "frozen",
    "analysis",
    "parameter_table",
    "assumption_refs",
    "notes",
)


def capture_characterization(template: CharacterizationTemplateBase) -> dict[str, Any]:
    """Capture the template as a deterministic protocol dict.

    Pure: the capture is a pure function of the template -- sorted
    parameter table, the analysis plan (protocol steps, acceptance
    thresholds, freeze state), the strict/recovery track label, the freeze
    state and the assumption refs of the routed missing metadata. Same
    template -> identical capture on every call and platform.

    Raises:
        TypeError: ``template`` is not a ``CharacterizationTemplateBase``.
    """
    if not isinstance(template, CharacterizationTemplateBase):
        raise TypeError(
            "template must be a CharacterizationTemplateBase, got"
            f" {type(template).__name__}"
        )
    return {
        "template_id": template.template_id,
        "title": template.title,
        "characterization_kind": template.characterization_kind.value,
        "track": template.track.value,
        "frozen": template.frozen,
        "analysis": None if template.analysis is None else template.analysis.as_dict(),
        "parameter_table": [
            {"parameter": parameter, "value": template.parameters[parameter]}
            for parameter in sorted(template.parameters)
        ],
        "assumption_refs": list(template.assumption_refs),
        "notes": template.notes,
    }
