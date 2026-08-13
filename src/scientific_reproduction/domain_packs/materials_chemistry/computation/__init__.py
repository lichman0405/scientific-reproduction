"""Materials-chemistry computation metadata templates (DEV-M11-G04).

The computation template pack: frozen, parameterized templates for
structure preparation, DFT, GCMC and MD execution and post-processing
(analysis/validation) metadata, plus validated Slurm/Modules scheduler
metadata, routed through the frozen core vocabulary (``GoalTrack``, the
Assumption Registry pathway of ``core.rules.assumptions`` and the
DEV-M6-G03 role-action matrix of ``core.permissions``).

This package is the domain-pack wiring: ``domain_packs/__init__.py``
defines no registration mechanism, so the exports of this module (and
the sibling ``templates`` module) are the pack's public interface. No
core module is modified: the templates reuse the frozen core APIs.
"""

from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    templates,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation.templates import (
    ANALYSIS_STAGE,
    CAPTURE_KEYS,
    COMPUTATION_PARAMETER_RULES,
    COMPUTATION_RULESET_VERSION,
    COMPUTATION_VALUE_RULES,
    DFT_KIND,
    EXECUTION_STAGE,
    GCMC_KIND,
    MD_KIND,
    STRUCTURE_PREPARATION_KIND,
    ComputationKind,
    ComputationParameterRule,
    ComputationStage,
    ComputationTemplateBase,
    ComputationTemplateError,
    ComputationValueRule,
    DftTemplate,
    GcmcTemplate,
    InvalidComputationTemplateError,
    InvalidSchedulerOptionsError,
    MdTemplate,
    MissingParameterRouting,
    ParameterCompletenessAssessment,
    ParameterCompletenessDecision,
    SchedulerOptions,
    StructurePreparationTemplate,
    ValueValidationAssessment,
    ValueValidationDecision,
    apply_assumption_routing,
    assess_parameter_completeness,
    assumptions_for_missing_parameters,
    capture_protocol,
    freeze_computation_template,
    missing_parameters,
    validate_computation_rulesets,
    validate_template_values,
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
    "templates",
]
