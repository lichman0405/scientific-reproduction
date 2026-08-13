"""Materials-chemistry synthesis unit-process templates (DEV-M11-G01).

The synthesis Unit Process template pack: frozen, parameterized templates
for ligand/material/MOF synthesis, thermal activation and solvent
exchange, independent-batch replication defaults and deterministic
protocol capture, routed through the frozen core vocabulary
(``GoalTrack``, ``GoalReplication``, ``RunType``, the Assumption Registry
pathway of ``core.rules.assumptions`` and the DEV-M6-G03 role-action
matrix of ``core.permissions``).

This package is the domain-pack wiring: ``domain_packs/__init__.py``
defines no registration mechanism, so the exports of this module (and
the sibling ``templates`` module) are the pack's public interface. No
core module is modified: the templates reuse the frozen core APIs.
"""

from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    templates,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis.templates import (
    ACTIVATION_KIND,
    BATCH_FLOOR_RULES,
    CAPTURE_KEYS,
    CONTROLLED_ATMOSPHERES,
    INDEPENDENT_FLOOR,
    LIGAND_KIND,
    MATERIAL_KIND,
    MOF_KIND,
    SOLVENT_EXCHANGE_KIND,
    SYNTHESIS_RULESET_VERSION,
    TEMPLATE_PARAMETER_RULES,
    TEMPLATE_VALUE_RULES,
    ActivationTemplate,
    BatchFloorAssessment,
    BatchFloorDecision,
    BatchFloorRule,
    BatchPlan,
    BatchReplicationDefaults,
    InvalidBatchPlanError,
    InvalidBatchReplicationError,
    InvalidTemplateError,
    MissingParameterRouting,
    ParameterCompletenessAssessment,
    ParameterCompletenessDecision,
    SolventExchangeTemplate,
    SynthesisTemplateBase,
    SynthesisTemplateError,
    SynthesisUnitProcessKind,
    SynthesisUnitProcessTemplate,
    TemplateParameterRule,
    TemplateValueRule,
    UnknownUnitProcessError,
    ValueValidationAssessment,
    ValueValidationDecision,
    apply_assumption_routing,
    assess_parameter_completeness,
    assumptions_for_missing_parameters,
    capture_protocol,
    evaluate_batch_floor,
    freeze_synthesis_template,
    missing_parameters,
    plan_independent_batches,
    validate_synthesis_rulesets,
    validate_template_values,
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
    "ActivationTemplate",
    "SolventExchangeTemplate",
    "SynthesisTemplateBase",
    "SynthesisTemplateError",
    "SynthesisUnitProcessKind",
    "SynthesisUnitProcessTemplate",
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
    "templates",
]
