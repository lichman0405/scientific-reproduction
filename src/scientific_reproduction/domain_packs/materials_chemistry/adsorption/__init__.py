"""Materials-chemistry gas adsorption/separation templates (DEV-M11-G03).

The gas adsorption/separation template pack: frozen, parameterized
templates for BET, single-component gas adsorption, IAST selectivity,
isosteric heat (Qst), dynamic breakthrough and cycling/stability --
execution and analysis as separate metadata surfaces (AC-02), with
temperature/pressure/composition as explicit condition inputs routed
through the Assumption Registry pathway when missing (AC-01), and with
breakthrough raw/result metadata mapped deterministically to formal paper
figures/results (AC-03) -- grounded in the frozen core vocabulary
(``GoalTrack``, the Assumption Registry pathway of
``core.rules.assumptions`` and the DEV-M6-G03 role-action matrix of
``core.permissions``).

This package is the domain-pack wiring: ``domain_packs/__init__.py``
defines no registration mechanism, so the exports of this module (and
the sibling ``templates`` module) are the pack's public interface. No
core module is modified: the templates reuse the frozen core APIs.
"""

from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    templates,
)
from scientific_reproduction.domain_packs.materials_chemistry.adsorption.templates import (
    ADSORPTION_PARAMETER_RULES,
    ADSORPTION_RULESET_VERSION,
    ADSORPTION_VALUE_RULES,
    ANALYSIS_STAGE,
    BET_KIND,
    BREAKTHROUGH_KIND,
    CAPTURE_KEYS,
    CYCLING_STABILITY_KIND,
    EXECUTION_STAGE,
    IAST_KIND,
    QST_KIND,
    SINGLE_COMPONENT_KIND,
    AdsorptionKind,
    AdsorptionParameterRule,
    AdsorptionStage,
    AdsorptionTemplateBase,
    AdsorptionTemplateError,
    AdsorptionValueRule,
    BetTemplate,
    BreakthroughResultTable,
    BreakthroughTemplate,
    CyclingStabilityTemplate,
    IastTemplate,
    InvalidAdsorptionTemplateError,
    InvalidBreakthroughResultError,
    MissingParameterRouting,
    PaperResultEntry,
    ParameterCompletenessAssessment,
    ParameterCompletenessDecision,
    QstTemplate,
    SingleComponentTemplate,
    ValueValidationAssessment,
    ValueValidationDecision,
    apply_assumption_routing,
    assess_parameter_completeness,
    assumptions_for_missing_parameters,
    capture_protocol,
    freeze_adsorption_template,
    missing_parameters,
    paper_mapping,
    validate_adsorption_rulesets,
    validate_template_values,
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
    "templates",
    "validate_adsorption_rulesets",
    "validate_template_values",
]
