"""Materials-chemistry computation metadata templates (DEV-M11-G04).

Implements the **computational materials templates** deliverable of
DEV-M11-G04 for the materials-chemistry domain pack: frozen, parameterized
templates for structure preparation, DFT, GCMC and MD execution and
post-processing (analysis/validation) metadata, plus validated
Slurm/Modules scheduler metadata. Grounded in:

* ``11-COMPUTATION-SUBSYSTEM.md`` SS1 (HPC environments are Slurm +
  environment modules; resource discovery occurs at execution time
  rather than blocking scientific planning -- scheduler metadata is
  recorded when known and is never a scientific requirement), SS4
  (Computation Worker records software/version/environment and
  materializes the frozen scientific input) and SS7 (domain protocols
  define numerical convergence/sampling validation, never a single
  final scalar matching the paper);
* ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS3 (v0.1 computational
  capability families: structure model preparation; disorder
  resolution/model assumptions; DFT geometry/energy calculations;
  GCMC adsorption; MD/diffusion; convergence and stochastic error
  analysis) and SS4 (inventory extraction captures
  "calculation software/method/force field/charges/cutoffs",
  "structure models and disorder treatments") and SS5 (DFT: record
  complete model, disorder resolution, functional, dispersion,
  basis/pseudopotential, convergence and finite-size choices --
  missing scientifically meaningful settings are A2 unless reliable
  method evidence supports an A1 classification; GCMC: record force
  fields, charges, mixing rules, cutoffs/Ewald treatment,
  initialization/equilibration/production cycles, seeds and
  uncertainty analysis);
* ``08-STRICT-RECOVERY-CLOSURE.md`` SS1 and SS3 (the Assumption
  Registry: every non-explicit parameter is registered as
  ``A0_TECHNICAL_DEFAULT`` / ``A1_METHODOLOGICAL_DEFAULT`` /
  ``A2_SCIENTIFIC_ASSUMPTION``);
* ``core/models.py`` -- the frozen vocabulary reused verbatim:
  ``GoalTrack`` (the strict/recovery track label), ``Assumption`` /
  ``AssumptionClassification`` (the Assumption Registry entry);
* ``core/rules/assumptions.py`` -- the EXISTING Assumption Registry
  evaluation API (``assumption_effect`` / ``evaluate_strict_label``):
  missing scientific parameters are routed through it, never through a
  parallel store;
* ``core/permissions.py`` (DEV-M6-G03) -- the role-action matrix:
  templates are proposed by Research/domain helpers and RECORD the
  strict/recovery label, but freezing is Supervisor-only; the freeze
  helper is gated by the matrix (``Action.PLAN_FREEZE``, granted only
  to the Supervisor), so nothing is ever silently frozen.

The template model mirrors the merged DEV-M11-G01 synthesis templates
pattern exactly (frozen dataclasses with strict ``__post_init__``
validation, ordered first-match-wins rule tables with a trailing total
default, real Assumption Registry routing for missing parameters,
Supervisor-only freezing, deterministic protocol capture).

Template model (determinism and boundaries)
-------------------------------------------
Every template is a frozen dataclass with strict ``__post_init__``
validation: ``TypeError`` at the type boundaries (template id, stage,
kind, track, parameters, scheduler, ...), ``ValueError``-subclass stable
errors (``InvalidComputationTemplateError`` and siblings) for value
violations. Construction enforces the **universal value rules** of the
ordered, versioned ``COMPUTATION_VALUE_RULES`` table over the parameters
that ARE present, and validates ids as safe single registry path segments
(the FND-M9-G02-01 lesson: no path separators, no glob metacharacters).
Missing scientific parameters are NOT a construction error: they are the
input to the Assumption Registry pathway (AC-01) -- the ordered
``COMPUTATION_PARAMETER_RULES`` table declares, per (kind, stage) pair,
the required scientific parameter set, and
:func:`assumptions_for_missing_parameters` routes every missing required
parameter through the real ``core.models.Assumption`` record and the
real ``core.rules.assumptions`` evaluation API, returning the exact
assumptions, their recorded strict-status effects and the strict label,
with the assumption refs carried on the template.

Software, method, force-field, functional and convergence inputs are
explicitly captured as template parameters (AC-01) or routed to the real
Assumption Registry when absent (AC-01). Execution and post-processing
(analysis/validation) are separate metadata surfaces (AC-02): every
template class selects a ``ComputationStage`` (``EXECUTION`` or
``ANALYSIS``), the required-parameter rules differ per stage (execution
captures software/method/settings; analysis captures the property,
convergence metric/threshold, sampling and statistical-uncertainty
validation), and each surface carries its own ``frozen`` flag and is
frozen independently through :func:`freeze_computation_template`.
Slurm/Modules metadata (partition, account, QOS, nodes, tasks per node,
walltime, module loads) is representable as the validated
``SchedulerOptions`` section with its own value rules (AC-03); it is
infrastructure metadata recorded when known and never enters the
Assumption Registry (11-COMPUTATION-SUBSYSTEM.md SS1: resource discovery
happens at execution time).

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

__all__ = [
    "ANALYSIS_STAGE",
    "CAPTURE_KEYS",
    "COMPUTATION_PARAMETER_RULES",
    "COMPUTATION_RULESET_VERSION",
    "COMPUTATION_VALUE_RULES",
    "ComputationKind",
    "ComputationParameterRule",
    "ComputationStage",
    "ComputationTemplateBase",
    "ComputationTemplateError",
    "ComputationValueRule",
    "DFT_KIND",
    "DftTemplate",
    "EXECUTION_STAGE",
    "GCMC_KIND",
    "GcmcTemplate",
    "InvalidComputationTemplateError",
    "InvalidSchedulerOptionsError",
    "MD_KIND",
    "MdTemplate",
    "MissingParameterRouting",
    "ParameterCompletenessAssessment",
    "ParameterCompletenessDecision",
    "STRUCTURE_PREPARATION_KIND",
    "SchedulerOptions",
    "StructurePreparationTemplate",
    "ValueValidationAssessment",
    "ValueValidationDecision",
    "apply_assumption_routing",
    "assess_parameter_completeness",
    "assumptions_for_missing_parameters",
    "capture_protocol",
    "freeze_computation_template",
    "missing_parameters",
    "validate_computation_rulesets",
    "validate_template_values",
]

#: Version of the template rule tables. Bumped whenever a rule changes;
#: recorded in every assessment so old decisions stay interpretable.
COMPUTATION_RULESET_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ComputationTemplateError(ValueError):
    """Base class for all computation-template errors."""


class InvalidComputationTemplateError(ComputationTemplateError):
    """Raised when a template violates a universal value rule or shape rule."""


class InvalidSchedulerOptionsError(ComputationTemplateError):
    """Raised when a Slurm/Modules scheduler section violates its value rules."""


# ---------------------------------------------------------------------------
# Kind and stage vocabulary (the computation capability families)
# ---------------------------------------------------------------------------


class ComputationKind(StrEnum):
    """The computation kinds the templates parameterize.

    Values follow ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS3 family
    names (structure model preparation; DFT geometry/energy
    calculations; GCMC adsorption; MD/diffusion where present). This is
    domain-pack vocabulary, distinct from the frozen core vocabulary --
    the kinds are the keys of the universal ``COMPUTATION_PARAMETER_RULES``
    table, never software or chemistry instances.
    """

    STRUCTURE_PREPARATION = "structure_preparation"
    DFT = "dft"
    GCMC = "gcmc"
    MD = "md"


#: Convenience aliases for the four computation kinds.
STRUCTURE_PREPARATION_KIND: ComputationKind = (
    ComputationKind.STRUCTURE_PREPARATION
)
DFT_KIND: ComputationKind = ComputationKind.DFT
GCMC_KIND: ComputationKind = ComputationKind.GCMC
MD_KIND: ComputationKind = ComputationKind.MD


class ComputationStage(StrEnum):
    """The metadata surface a template captures (AC-02).

    ``EXECUTION`` captures the run itself -- software and versions,
    method, force field/functional, and the convergence inputs of the
    calculation; ``ANALYSIS`` captures post-processing/validation -- the
    computed property, the convergence metric and threshold, and the
    sampling/statistical-uncertainty validation
    (11-COMPUTATION-SUBSYSTEM.md SS7). The two surfaces are separate
    templates with disjoint required parameter sets, and each is frozen
    independently (AC-02).
    """

    EXECUTION = "execution"
    ANALYSIS = "analysis"


#: Convenience aliases for the two computation stages.
EXECUTION_STAGE: ComputationStage = ComputationStage.EXECUTION
ANALYSIS_STAGE: ComputationStage = ComputationStage.ANALYSIS


# ---------------------------------------------------------------------------
# Scheduler metadata (AC-03: Slurm/Modules compatibility)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerOptions:
    """Validated Slurm/Modules scheduler section of a template (AC-03).

    Records the scheduler metadata of 11-COMPUTATION-SUBSYSTEM.md SS1
    (partition, account, QOS) and SS3 (module loading mapping): the
    partition, account and QOS names, the requested node count and tasks
    per node, the walltime in hours and the environment-module loads.
    Every field is optional -- resource discovery happens at execution
    time (SS1), so an unset field is valid and never enters the
    Assumption Registry (scheduler metadata is infrastructure, not
    science). Values that ARE set satisfy the section's own value rules:
    non-empty names, positive integer node/task counts, a finite
    positive walltime, non-empty module names.

    Raises:
        TypeError: a field has the wrong type.
        InvalidSchedulerOptionsError: a value violation (empty name,
            node count below 1, non-positive or non-finite walltime,
            empty module name).
    """

    partition: str | None = None
    account: str | None = None
    qos: str | None = None
    nodes: int | None = None
    tasks_per_node: int | None = None
    walltime_hours: float | None = None
    modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("partition", "account", "qos"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError(
                    f"SchedulerOptions.{field_name} must be a str or None,"
                    f" got {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidSchedulerOptionsError(
                    f"SchedulerOptions.{field_name} must be a non-empty"
                    f" name when set, got {value!r}"
                )
        for field_name in ("nodes", "tasks_per_node"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"SchedulerOptions.{field_name} must be an int or"
                    f" None, got {type(value).__name__}"
                )
            if value < 1:
                raise InvalidSchedulerOptionsError(
                    f"SchedulerOptions.{field_name} must be at least 1"
                    f" when set, got {value}"
                )
        walltime = self.walltime_hours
        if walltime is not None:
            if not isinstance(walltime, (int, float)) or isinstance(
                walltime, bool
            ):
                raise TypeError(
                    "SchedulerOptions.walltime_hours must be a number or"
                    f" None, got {type(walltime).__name__}"
                )
            if not (math.isfinite(walltime) and walltime > 0):
                raise InvalidSchedulerOptionsError(
                    "SchedulerOptions.walltime_hours must be a finite"
                    " positive number of hours when set, got"
                    f" {walltime!r}"
                )
        if not isinstance(self.modules, tuple) or not all(
            isinstance(module, str) for module in self.modules
        ):
            raise TypeError(
                "SchedulerOptions.modules must be a tuple of module-name"
                " strings"
            )
        if not all(module.strip() for module in self.modules):
            raise InvalidSchedulerOptionsError(
                "SchedulerOptions.modules must contain only non-empty"
                " module names"
            )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (protocol-capture shape)."""
        return {
            "partition": self.partition,
            "account": self.account,
            "qos": self.qos,
            "nodes": self.nodes,
            "tasks_per_node": self.tasks_per_node,
            "walltime_hours": self.walltime_hours,
            "modules": list(self.modules),
        }


# ---------------------------------------------------------------------------
# Universal rule tables (AC-01: method/settings capture, never instances)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputationParameterRule:
    """One entry of the required-scientific-parameter rule table.

    Declares, per (kind, stage) pair, the required scientific parameters
    a template of that pair must record (or route to the Assumption
    Registry pathway when missing). The parameter names are universal
    method-capture vocabulary -- no software, force-field or chemistry
    instances. The predicate is a pure function of the kind and the
    stage; the trailing total default always matches.
    """

    rule_id: str
    description: str
    required_parameters: tuple[str, ...]
    predicate: Callable[[ComputationKind, ComputationStage], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"ComputationParameterRule.{field_name} must be a str,"
                    f" got {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidComputationTemplateError(
                    f"ComputationParameterRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not isinstance(self.required_parameters, tuple) or not all(
            isinstance(parameter, str) and parameter.strip()
            for parameter in self.required_parameters
        ):
            raise TypeError(
                "ComputationParameterRule.required_parameters must be a"
                " tuple of non-empty strings"
            )
        if not callable(self.predicate):
            raise TypeError(
                "ComputationParameterRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class ComputationValueRule:
    """One entry of the universal parameter-value rule table.

    Each rule validates the value of one named scientific parameter when
    the template records it. Predicates are pure functions of the value
    only; the message template is filled with the offending value. All
    rules are universal computational-physics rules -- named software,
    method, force field, functional, charges, mixing rules, ensembles
    and thermostats, positive cutoffs/convergences/temperatures,
    positive integer step/cycle/seed counts, a 3-vector positive
    k-point mesh, and a finite reference value of any sign (binding
    energies may be negative).
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
                    f"ComputationValueRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise InvalidComputationTemplateError(
                    f"ComputationValueRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not callable(self.predicate):
            raise TypeError(
                "ComputationValueRule.predicate must be callable, got"
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


def _is_finite_number(value: Any) -> bool:
    """True iff ``value`` is a finite non-bool number of any sign."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_positive_integer(value: Any) -> bool:
    """True iff ``value`` is an int >= 1 (bool is not an int here)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_non_empty_string(value: Any) -> bool:
    """True iff ``value`` is a non-empty str."""
    return isinstance(value, str) and bool(value.strip())


def _is_kpoint_mesh(value: Any) -> bool:
    """True iff ``value`` is a 3-vector of positive integers."""
    return (
        isinstance(value, (tuple, list))
        and len(value) == 3
        and all(
            isinstance(entry, int)
            and not isinstance(entry, bool)
            and entry >= 1
            for entry in value
        )
    )


def _kind_stage_is(
    kind: ComputationKind, stage: ComputationStage
) -> Callable[[ComputationKind, ComputationStage], bool]:
    """A predicate matching exactly the given (kind, stage) pair."""
    return lambda candidate_kind, candidate_stage: (
        candidate_kind is kind and candidate_stage is stage
    )


#: The ordered, versioned universal value-rule table. Each named parameter
#: has exactly one rule (the table is a total function of parameter
#: names). Order is normative.
COMPUTATION_VALUE_RULES: tuple[ComputationValueRule, ...] = (
    ComputationValueRule(
        rule_id="R-COM-V1",
        description=(
            "a recorded software name must be a named software (any name;"
            " the rules never restrict which software -- instance data"
            " lives in template parameters)"
        ),
        parameter="software",
        predicate=_is_non_empty_string,
        message="software must be a non-empty software name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V2",
        description=(
            "a recorded software version must be a non-empty version"
            " string"
        ),
        parameter="software_version",
        predicate=_is_non_empty_string,
        message="software_version must be a non-empty version string, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V3",
        description=(
            "a recorded method name must be a non-empty method string (e.g."
            " a method family; any method -- the rules never restrict"
            " which)"
        ),
        parameter="method",
        predicate=_is_non_empty_string,
        message="method must be a non-empty method name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V4",
        description=(
            "a recorded functional must be a non-empty functional name"
            " (any functional -- the rules never restrict which)"
        ),
        parameter="functional",
        predicate=_is_non_empty_string,
        message="functional must be a non-empty functional name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V5",
        description=(
            "a recorded dispersion correction must be a non-empty name"
        ),
        parameter="dispersion_correction",
        predicate=_is_non_empty_string,
        message="dispersion_correction must be a non-empty name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V6",
        description=(
            "a recorded basis set must be a non-empty basis-set name"
        ),
        parameter="basis_set",
        predicate=_is_non_empty_string,
        message="basis_set must be a non-empty basis-set name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V7",
        description=(
            "a recorded pseudopotential must be a non-empty"
            " pseudopotential name"
        ),
        parameter="pseudopotential",
        predicate=_is_non_empty_string,
        message="pseudopotential must be a non-empty pseudopotential name,"
        " got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V8",
        description=(
            "a recorded force field must be a non-empty force-field name"
            " (any force field -- the rules never restrict which)"
        ),
        parameter="force_field",
        predicate=_is_non_empty_string,
        message="force_field must be a non-empty force-field name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V9",
        description=(
            "a recorded charge assignment must be a non-empty charge-model"
            " name"
        ),
        parameter="charges",
        predicate=_is_non_empty_string,
        message="charges must be a non-empty charge-model name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V10",
        description=(
            "a recorded mixing rule must be a non-empty mixing-rules name"
        ),
        parameter="mixing_rules",
        predicate=_is_non_empty_string,
        message="mixing_rules must be a non-empty mixing-rules name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V11",
        description=(
            "a recorded ensemble must be a non-empty ensemble name (any"
            " ensemble -- the rules never restrict which)"
        ),
        parameter="ensemble",
        predicate=_is_non_empty_string,
        message="ensemble must be a non-empty ensemble name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V12",
        description=(
            "a recorded thermostat must be a non-empty thermostat name"
        ),
        parameter="thermostat",
        predicate=_is_non_empty_string,
        message="thermostat must be a non-empty thermostat name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V13",
        description=(
            "a recorded barostat must be a non-empty barostat name"
        ),
        parameter="barostat",
        predicate=_is_non_empty_string,
        message="barostat must be a non-empty barostat name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V14",
        description=(
            "a recorded k-point mesh must be a 3-vector of positive"
            " integers"
        ),
        parameter="kpoint_mesh",
        predicate=_is_kpoint_mesh,
        message="kpoint_mesh must be a 3-vector of positive integers, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V15",
        description=(
            "a recorded plane-wave energy cutoff must be a finite positive"
            " number of eV"
        ),
        parameter="energy_cutoff_ev",
        predicate=_is_positive_number,
        message="energy_cutoff_ev must be a finite positive number of eV,"
        " got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V16",
        description=(
            "a recorded interaction cutoff must be a finite positive"
            " number of angstrom"
        ),
        parameter="cutoff_angstrom",
        predicate=_is_positive_number,
        message="cutoff_angstrom must be a finite positive number of"
        " angstrom, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V17",
        description=(
            "a recorded convergence tolerance must be a finite positive"
            " number"
        ),
        parameter="convergence_tolerance",
        predicate=_is_positive_number,
        message="convergence_tolerance must be a finite positive number,"
        " got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V18",
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
    ComputationValueRule(
        rule_id="R-COM-V19",
        description=(
            "a recorded pressure must be a finite non-negative number of"
            " bar"
        ),
        parameter="pressure_bar",
        predicate=_is_non_negative_number,
        message="pressure_bar must be a finite non-negative number of bar,"
        " got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V20",
        description=(
            "a recorded integration timestep must be a finite positive"
            " number of femtoseconds"
        ),
        parameter="timestep_fs",
        predicate=_is_positive_number,
        message="timestep_fs must be a finite positive number of"
        " femtoseconds, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V21",
        description=(
            "a recorded step count must be an integer of at least one step"
        ),
        parameter="n_steps",
        predicate=_is_positive_integer,
        message="n_steps must be an integer >= 1, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V22",
        description=(
            "a recorded random seed must be an integer of at least one"
            " (seeds are positive integers)"
        ),
        parameter="seed",
        predicate=_is_positive_integer,
        message="seed must be an integer >= 1, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V23",
        description=(
            "a recorded equilibration cycle count must be an integer of at"
            " least one cycle"
        ),
        parameter="equilibration_cycles",
        predicate=_is_positive_integer,
        message="equilibration_cycles must be an integer >= 1, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V24",
        description=(
            "a recorded production cycle count must be an integer of at"
            " least one cycle"
        ),
        parameter="production_cycles",
        predicate=_is_positive_integer,
        message="production_cycles must be an integer >= 1, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V25",
        description=(
            "a recorded structure source must be a non-empty source name"
            " (any source -- the rules never restrict which)"
        ),
        parameter="structure_source",
        predicate=_is_non_empty_string,
        message="structure_source must be a non-empty structure source, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V26",
        description=(
            "a recorded disorder treatment must be a non-empty treatment"
            " name"
        ),
        parameter="disorder_treatment",
        predicate=_is_non_empty_string,
        message="disorder_treatment must be a non-empty treatment name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V27",
        description=(
            "a recorded computed property must be a non-empty property"
            " name (any property -- the rules never restrict which)"
        ),
        parameter="property",
        predicate=_is_non_empty_string,
        message="property must be a non-empty property name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V28",
        description=(
            "a recorded convergence metric must be a non-empty metric name"
        ),
        parameter="convergence_metric",
        predicate=_is_non_empty_string,
        message="convergence_metric must be a non-empty metric name, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V29",
        description=(
            "a recorded convergence threshold must be a finite positive"
            " number"
        ),
        parameter="convergence_threshold",
        predicate=_is_positive_number,
        message="convergence_threshold must be a finite positive number,"
        " got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V30",
        description=(
            "a recorded statistical-uncertainty metric must be a non-empty"
            " metric name"
        ),
        parameter="statistical_uncertainty_metric",
        predicate=_is_non_empty_string,
        message="statistical_uncertainty_metric must be a non-empty metric"
        " name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V31",
        description=(
            "a recorded sampling validation must be a non-empty validation"
            " description"
        ),
        parameter="sampling_validation",
        predicate=_is_non_empty_string,
        message="sampling_validation must be a non-empty description, got"
        " {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V32",
        description=(
            "a recorded finite-size correction must be a non-empty"
            " correction name"
        ),
        parameter="finite_size_correction",
        predicate=_is_non_empty_string,
        message="finite_size_correction must be a non-empty correction"
        " name, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V33",
        description=(
            "a recorded reference value must be a finite number of any"
            " sign (binding energies may be negative)"
        ),
        parameter="reference_value",
        predicate=_is_finite_number,
        message="reference_value must be a finite number, got {value!r}",
    ),
    ComputationValueRule(
        rule_id="R-COM-V34",
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
#: missing required parameters route to the Assumption Registry pathway;
#: AC-02: execution and analysis declare disjoint required sets).
COMPUTATION_PARAMETER_RULES: tuple[ComputationParameterRule, ...] = (
    ComputationParameterRule(
        rule_id="R-COM-P1",
        description=(
            "structure preparation execution records the structure source,"
            " the disorder treatment, the method and the software name and"
            " version"
        ),
        required_parameters=(
            "structure_source",
            "disorder_treatment",
            "method",
            "software",
            "software_version",
        ),
        predicate=_kind_stage_is(
            ComputationKind.STRUCTURE_PREPARATION, ComputationStage.EXECUTION
        ),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P2",
        description=(
            "DFT execution records the software name and version, the"
            " method, the functional, the dispersion correction, the basis"
            " set, the pseudopotential, the k-point mesh, the energy"
            " cutoff and the convergence tolerance (16-...DOMAIN-PACK SS5"
            " DFT)"
        ),
        required_parameters=(
            "software",
            "software_version",
            "method",
            "functional",
            "dispersion_correction",
            "basis_set",
            "pseudopotential",
            "kpoint_mesh",
            "energy_cutoff_ev",
            "convergence_tolerance",
        ),
        predicate=_kind_stage_is(ComputationKind.DFT, ComputationStage.EXECUTION),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P3",
        description=(
            "GCMC execution records the software name and version, the"
            " force field, the charge model, the mixing rules, the"
            " interaction cutoff, the temperature, the pressure, the"
            " equilibration and production cycle counts and the random"
            " seed (16-...DOMAIN-PACK SS5 GCMC)"
        ),
        required_parameters=(
            "software",
            "software_version",
            "force_field",
            "charges",
            "mixing_rules",
            "cutoff_angstrom",
            "temperature_K",
            "pressure_bar",
            "equilibration_cycles",
            "production_cycles",
            "seed",
        ),
        predicate=_kind_stage_is(ComputationKind.GCMC, ComputationStage.EXECUTION),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P4",
        description=(
            "MD execution records the software name and version, the force"
            " field, the charge model, the ensemble, the thermostat, the"
            " barostat, the temperature, the pressure, the timestep, the"
            " step count, the interaction cutoff and the random seed"
        ),
        required_parameters=(
            "software",
            "software_version",
            "force_field",
            "charges",
            "ensemble",
            "thermostat",
            "barostat",
            "temperature_K",
            "pressure_bar",
            "timestep_fs",
            "n_steps",
            "cutoff_angstrom",
            "seed",
        ),
        predicate=_kind_stage_is(ComputationKind.MD, ComputationStage.EXECUTION),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P5",
        description=(
            "structure preparation analysis records the validated property,"
            " the convergence metric and threshold and the reference value"
            " and comparison tolerance (structure validation against the"
            " reference structure)"
        ),
        required_parameters=(
            "property",
            "convergence_metric",
            "convergence_threshold",
            "reference_value",
            "tolerance",
        ),
        predicate=_kind_stage_is(
            ComputationKind.STRUCTURE_PREPARATION, ComputationStage.ANALYSIS
        ),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P6",
        description=(
            "DFT analysis records the computed property, the convergence"
            " metric and threshold, the statistical-uncertainty metric,"
            " the sampling validation and the finite-size correction"
            " (11-COMPUTATION-SUBSYSTEM SS7)"
        ),
        required_parameters=(
            "property",
            "convergence_metric",
            "convergence_threshold",
            "statistical_uncertainty_metric",
            "sampling_validation",
            "finite_size_correction",
        ),
        predicate=_kind_stage_is(ComputationKind.DFT, ComputationStage.ANALYSIS),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P7",
        description=(
            "GCMC analysis records the computed property, the convergence"
            " metric and threshold, the statistical-uncertainty metric and"
            " the sampling validation"
        ),
        required_parameters=(
            "property",
            "convergence_metric",
            "convergence_threshold",
            "statistical_uncertainty_metric",
            "sampling_validation",
        ),
        predicate=_kind_stage_is(ComputationKind.GCMC, ComputationStage.ANALYSIS),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P8",
        description=(
            "MD analysis records the computed property, the convergence"
            " metric and threshold, the statistical-uncertainty metric and"
            " the sampling validation"
        ),
        required_parameters=(
            "property",
            "convergence_metric",
            "convergence_threshold",
            "statistical_uncertainty_metric",
            "sampling_validation",
        ),
        predicate=_kind_stage_is(ComputationKind.MD, ComputationStage.ANALYSIS),
    ),
    ComputationParameterRule(
        rule_id="R-COM-P0",
        description=(
            "no rule declares a required parameter set for this (kind,"
            " stage) pair (total default)"
        ),
        required_parameters=(),
        predicate=lambda kind, stage: True,
    ),
)


def validate_computation_rulesets() -> tuple[str, ...]:
    """Validate the template rule tables' integrity; return the ids.

    A valid parameter table is non-empty, has unique rule ids, declares a
    rule for every (kind, stage) pair (the evaluation is a total function
    of the pair), and its trailing rule matches every pair (the total
    default that guarantees first-match evaluation is total). The value
    table has unique rule ids, exactly one rule per parameter name, and a
    rule for every required parameter name of the parameter table.

    Raises:
        InvalidComputationTemplateError: a table violates the frozen
            shape (stable messages).
    """
    parameter_ids = tuple(rule.rule_id for rule in COMPUTATION_PARAMETER_RULES)
    value_ids = tuple(rule.rule_id for rule in COMPUTATION_VALUE_RULES)
    for label, ids in (("parameter", parameter_ids), ("value", value_ids)):
        duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
        if duplicates:
            raise InvalidComputationTemplateError(
                f"duplicate rule id(s) in the {label} rule table:"
                f" {', '.join(duplicates)}"
            )
    if not parameter_ids:
        raise InvalidComputationTemplateError(
            "the required-parameter rule table must not be empty"
        )
    pairs = [
        (kind, stage)
        for kind in ComputationKind
        for stage in ComputationStage
    ]
    covered = {
        (kind, stage)
        for rule in COMPUTATION_PARAMETER_RULES
        for kind, stage in pairs
        if rule.predicate(kind, stage)
    }
    if covered != set(pairs):
        missing = sorted(
            f"{kind.value}/{stage.value}" for kind, stage in pairs
            if (kind, stage) not in covered
        )
        raise InvalidComputationTemplateError(
            "the required-parameter rule table must cover every (kind,"
            f" stage) pair, missing: {', '.join(missing)}"
        )
    default_rule = COMPUTATION_PARAMETER_RULES[-1]
    for kind, stage in pairs:
        if not default_rule.predicate(kind, stage):
            raise InvalidComputationTemplateError(
                f"the trailing rule {default_rule.rule_id!r} is not a total"
                f" default: it does not match kind {kind.value!r} stage"
                f" {stage.value!r}"
            )
    value_parameters = [rule.parameter for rule in COMPUTATION_VALUE_RULES]
    duplicated_parameters = sorted(
        {
            parameter
            for parameter in value_parameters
            if value_parameters.count(parameter) > 1
        }
    )
    if duplicated_parameters:
        raise InvalidComputationTemplateError(
            "the value rule table declares more than one rule for"
            f" parameter(s): {', '.join(duplicated_parameters)}"
        )
    value_ruled = set(value_parameters)
    unrouted = sorted(
        {
            parameter
            for rule in COMPUTATION_PARAMETER_RULES
            for parameter in rule.required_parameters
            if parameter not in value_ruled
        }
    )
    if unrouted:
        raise InvalidComputationTemplateError(
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
    kind: ComputationKind
    stage: ComputationStage
    present_parameters: tuple[str, ...]
    missing_parameters: tuple[str, ...]
    decisions: tuple[ParameterCompletenessDecision, ...]
    matched_rule_id: str
    ruleset_version: str = COMPUTATION_RULESET_VERSION


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
    ruleset_version: str = COMPUTATION_RULESET_VERSION


# ---------------------------------------------------------------------------
# The templates (frozen dataclasses, strict __post_init__)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComputationTemplateBase:
    """Frozen base of every computation template (AC-01, AC-02, AC-03).

    Common shape: a safe ``template_id``, a title, the
    ``ComputationStage`` (``EXECUTION`` or ``ANALYSIS`` -- the separate
    metadata surfaces of AC-02), the computation ``kind``, the
    strict/recovery ``track`` label (frozen ``GoalTrack`` vocabulary,
    AC-01), the recorded scientific parameters (instance data --
    software/force-field/functional/convergence values live here, never
    in the rule tables), the Assumption Registry refs of routed missing
    parameters (AC-01), the freeze flag and the validated Slurm/Modules
    ``scheduler`` section (AC-03).

    Construction enforces the universal value rules over the parameters
    that are present; required parameters may be missing -- they are the
    input of the Assumption Registry pathway, not a construction error.
    Nothing is ever frozen by construction: the only way to produce a
    frozen template is :func:`freeze_computation_template`, gated by the
    Supervisor-only permission (``core/permissions.py``).

    The ``stage`` field precedes ``kind`` in the declaration order so
    subclasses can fix ``kind`` with a default (frozen-dataclass field
    ordering: a defaulted field must not precede a defaultless one).

    Raises:
        TypeError: a field has the wrong type.
        InvalidComputationTemplateError: a value violation (unsafe
            template id, value-rule violation, unknown kind for the
            class).
    """

    template_id: str
    title: str
    stage: ComputationStage
    kind: ComputationKind
    track: GoalTrack = GoalTrack.STRICT_REPRODUCTION
    parameters: dict[str, Any] = field(default_factory=dict)
    assumption_refs: tuple[str, ...] = ()
    frozen: bool = False
    scheduler: SchedulerOptions | None = None
    notes: str | None = None

    #: The kinds this template class accepts (subclasses narrow this).
    _ALLOWED_KINDS: ClassVar[tuple[ComputationKind, ...]] = tuple(
        ComputationKind
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
            raise InvalidComputationTemplateError(
                f"{type(self).__name__}.title must be a non-empty string,"
                f" got {self.title!r}"
            )
        if not isinstance(self.stage, ComputationStage):
            raise TypeError(
                f"{type(self).__name__}.stage must be a ComputationStage"
                f" member, got {type(self.stage).__name__}"
            )
        if not isinstance(self.kind, ComputationKind):
            raise TypeError(
                f"{type(self).__name__}.kind must be a ComputationKind"
                f" member, got {type(self.kind).__name__}"
            )
        _validate_template_id(type(self).__name__, self.template_id)
        if self.kind not in type(self)._ALLOWED_KINDS:
            allowed = ", ".join(kind.value for kind in type(self)._ALLOWED_KINDS)
            raise InvalidComputationTemplateError(
                f"{type(self).__name__} does not accept computation kind"
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
        if self.scheduler is not None and not isinstance(
            self.scheduler, SchedulerOptions
        ):
            raise TypeError(
                f"{type(self).__name__}.scheduler must be a"
                " SchedulerOptions or None, got"
                f" {type(self.scheduler).__name__}"
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
            raise InvalidComputationTemplateError(
                f"invalid {self.stage.value} {self.kind.value} template"
                f" {self.template_id!r}: {details}"
            )


@dataclass(frozen=True)
class StructurePreparationTemplate(ComputationTemplateBase):
    """Structure preparation template (16-...DOMAIN-PACK SS3).

    Fixed ``kind`` ``STRUCTURE_PREPARATION``; the ``stage`` selects the
    execution surface (structure source, disorder treatment, method,
    software) or the analysis surface (property, convergence metric and
    threshold, reference value and tolerance).
    """

    _ALLOWED_KINDS: ClassVar[tuple[ComputationKind, ...]] = (
        ComputationKind.STRUCTURE_PREPARATION,
    )

    kind: ComputationKind = ComputationKind.STRUCTURE_PREPARATION


@dataclass(frozen=True)
class DftTemplate(ComputationTemplateBase):
    """DFT geometry/energy calculation template (16-...DOMAIN-PACK SS5).

    Fixed ``kind`` ``DFT``; the ``stage`` selects the execution surface
    (software, method, functional, dispersion, basis set, pseudopotential,
    k-point mesh, energy cutoff, convergence tolerance) or the analysis
    surface (property, convergence metric and threshold, uncertainty and
    sampling validation, finite-size correction).
    """

    _ALLOWED_KINDS: ClassVar[tuple[ComputationKind, ...]] = (
        ComputationKind.DFT,
    )

    kind: ComputationKind = ComputationKind.DFT


@dataclass(frozen=True)
class GcmcTemplate(ComputationTemplateBase):
    """GCMC adsorption calculation template (16-...DOMAIN-PACK SS5).

    Fixed ``kind`` ``GCMC``; the ``stage`` selects the execution surface
    (software, force field, charges, mixing rules, cutoff, temperature,
    pressure, equilibration/production cycles, seed) or the analysis
    surface (property, convergence metric and threshold, uncertainty and
    sampling validation).
    """

    _ALLOWED_KINDS: ClassVar[tuple[ComputationKind, ...]] = (
        ComputationKind.GCMC,
    )

    kind: ComputationKind = ComputationKind.GCMC


@dataclass(frozen=True)
class MdTemplate(ComputationTemplateBase):
    """MD/diffusion simulation template (16-...DOMAIN-PACK SS3).

    Fixed ``kind`` ``MD``; the ``stage`` selects the execution surface
    (software, force field, charges, ensemble, thermostat, barostat,
    temperature, pressure, timestep, step count, cutoff, seed) or the
    analysis surface (property, convergence metric and threshold,
    uncertainty and sampling validation).
    """

    _ALLOWED_KINDS: ClassVar[tuple[ComputationKind, ...]] = (
        ComputationKind.MD,
    )

    kind: ComputationKind = ComputationKind.MD


def _validate_template_id(class_name: str, value: str) -> None:
    """Reject template ids that escape registries or break glob listings.

    Safe single registry path segment (FND-M9-G02-01 lesson): no path
    separators, no glob metacharacters, not empty, not ``.``/``..``.
    """
    if not value.strip() or value in (".", ".."):
        raise InvalidComputationTemplateError(
            f"{class_name}.template_id must be a non-empty safe registry"
            f" id, got {value!r}"
        )
    if "/" in value or "\\" in value:
        raise InvalidComputationTemplateError(
            f"{class_name}.template_id must be a safe single path segment"
            f" (no '/', no '\\'), got {value!r}"
        )
    if any(char.isspace() for char in value):
        raise InvalidComputationTemplateError(
            f"{class_name}.template_id must not contain whitespace, got"
            f" {value!r}"
        )
    if any(char in value for char in "*?[]"):
        raise InvalidComputationTemplateError(
            f"{class_name}.template_id must not contain glob"
            f" metacharacters, got {value!r}"
        )


# ---------------------------------------------------------------------------
# Universal evaluation (pure and deterministic)
# ---------------------------------------------------------------------------


def _rule_for_kind_stage(
    kind: ComputationKind, stage: ComputationStage
) -> ComputationParameterRule:
    """The required-parameter rule of a (kind, stage) pair (first match)."""
    for rule in COMPUTATION_PARAMETER_RULES:
        if rule.predicate(kind, stage):
            return rule
    # The trailing total default always matches (validate_computation_rulesets
    # guarantees it); this line is unreachable.
    return COMPUTATION_PARAMETER_RULES[-1]


def assess_parameter_completeness(
    template: ComputationTemplateBase,
) -> ParameterCompletenessAssessment:
    """Evaluate a template's required-scientific-parameter completeness.

    Pure and deterministic: the assessment is a pure function of the
    template's kind, stage and recorded parameter names, decided by the
    ordered ``COMPUTATION_PARAMETER_RULES`` table (first match wins; the
    trailing default rule always matches). The assessment records every
    rule decision, the matched rule id and the missing parameters.

    Raises:
        TypeError: ``template`` is not a ``ComputationTemplateBase``.
    """
    if not isinstance(template, ComputationTemplateBase):
        raise TypeError(
            "template must be a ComputationTemplateBase, got"
            f" {type(template).__name__}"
        )
    recorded = set(template.parameters)
    decisions: list[ParameterCompletenessDecision] = []
    matched_rule_id: str | None = None
    matched_required: tuple[str, ...] = ()
    for rule in COMPUTATION_PARAMETER_RULES:
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


def missing_parameters(template: ComputationTemplateBase) -> tuple[str, ...]:
    """The required scientific parameters the template does not record.

    The exact input of the Assumption Registry routing (AC-01).

    Raises:
        TypeError: ``template`` is not a ``ComputationTemplateBase``.
    """
    return assess_parameter_completeness(template).missing_parameters


def validate_template_values(
    template: ComputationTemplateBase,
) -> ValueValidationAssessment:
    """Validate the template's present parameter values by the universal table.

    Pure and deterministic: every ``COMPUTATION_VALUE_RULES`` rule whose
    parameter the template records is applied; violations are collected
    as stable messages (``matched_rule_id`` names the first violated rule
    in table order). The template constructor enforces this assessment;
    the public hook makes the decision auditable.

    Raises:
        TypeError: ``template`` is not a ``ComputationTemplateBase``.
    """
    if not isinstance(template, ComputationTemplateBase):
        raise TypeError(
            "template must be a ComputationTemplateBase, got"
            f" {type(template).__name__}"
        )
    violations: list[str] = []
    matched_rule_id: str | None = None
    decisions: list[ValueValidationDecision] = []
    for rule in COMPUTATION_VALUE_RULES:
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


def freeze_computation_template(
    template: ComputationTemplateBase, *, role: Role
) -> ComputationTemplateBase:
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
        TypeError: ``template`` is not a ``ComputationTemplateBase``, or
            ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
    """
    if not isinstance(template, ComputationTemplateBase):
        raise TypeError(
            "template must be a ComputationTemplateBase, got"
            f" {type(template).__name__}"
        )
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    assessment = check_action_allowed(role, Action.PLAN_FREEZE)
    if not assessment.allowed:
        raise PermissionDeniedError(
            f"role {role.value!r} may not freeze computation template"
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
    (``ComputationTemplateBase.assumption_refs``).
    """

    template_id: str
    kind: ComputationKind
    stage: ComputationStage
    missing_parameters: tuple[str, ...]
    assumptions: tuple[Assumption, ...]
    effects: tuple[AssumptionEffectDecision, ...]
    strict_label_assessment: StrictLabelAssessment
    assumption_refs: tuple[str, ...]


def assumptions_for_missing_parameters(
    template: ComputationTemplateBase,
    *,
    classification: AssumptionClassification = AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
    rationale: str | None = None,
    source_refs: Sequence[str] = (),
    affected_goal_ids: Sequence[str] = (),
) -> MissingParameterRouting:
    """Route the template's missing scientific parameters through the real
    Assumption Registry pathway (AC-01).

    For every required scientific parameter the template does not record,
    a real ``core.models.Assumption`` registry entry is constructed
    (deterministic safe assumption id derived from the template id and
    the parameter, ``core.ids.generate_id``), its strict-status effect is
    decided by the real ``core.rules.assumptions.assumption_effect`` and
    recorded on the entry, and the real ``core.rules.assumptions.evaluate_strict_label``
    reads the whole set back into the strict label. The default
    classification for a missing scientific parameter is
    ``A2_SCIENTIFIC_ASSUMPTION`` (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md
    SS5: missing scientifically meaningful settings such as a functional
    or a convergence input are A2 unless reliable method evidence
    supports an A1 classification); an explicit classification is
    accepted verbatim.

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
        TypeError: ``template`` is not a ``ComputationTemplateBase``,
            ``classification`` is not an ``AssumptionClassification``
            member, ``rationale`` is not a str or None, or a ref/affected
            goal id is not a str.
    """
    if not isinstance(template, ComputationTemplateBase):
        raise TypeError(
            "template must be a ComputationTemplateBase, got"
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
                    f" {parameter!r} of {template.kind.value} computation"
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
    template: ComputationTemplateBase, routing: MissingParameterRouting
) -> ComputationTemplateBase:
    """Return the template carrying the routed assumption refs (AC-01).

    Pure: a frozen copy of the template with ``assumption_refs`` set to
    the routing's safe assumption ids; the input template and the routing
    are never mutated.

    Raises:
        TypeError: ``template`` is not a ``ComputationTemplateBase``, or
            ``routing`` is not a ``MissingParameterRouting``.
    """
    if not isinstance(template, ComputationTemplateBase):
        raise TypeError(
            "template must be a ComputationTemplateBase, got"
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
    "scheduler",
    "parameter_table",
    "assumption_refs",
    "notes",
)


def capture_protocol(template: ComputationTemplateBase) -> dict[str, Any]:
    """Capture the template as a deterministic protocol dict.

    Pure: the capture is a pure function of the template -- sorted
    parameter table, the validated scheduler section (or ``None``), the
    strict/recovery track label, the stage and kind, the freeze state and
    the assumption refs of the routed missing parameters. Same template
    -> identical capture on every call and platform.

    Raises:
        TypeError: ``template`` is not a ``ComputationTemplateBase``.
    """
    if not isinstance(template, ComputationTemplateBase):
        raise TypeError(
            "template must be a ComputationTemplateBase, got"
            f" {type(template).__name__}"
        )
    return {
        "template_id": template.template_id,
        "title": template.title,
        "stage": template.stage.value,
        "kind": template.kind.value,
        "track": template.track.value,
        "frozen": template.frozen,
        "scheduler": (
            template.scheduler.as_dict() if template.scheduler is not None else None
        ),
        "parameter_table": [
            {"parameter": parameter, "value": template.parameters[parameter]}
            for parameter in sorted(template.parameters)
        ],
        "assumption_refs": list(template.assumption_refs),
        "notes": template.notes,
    }
