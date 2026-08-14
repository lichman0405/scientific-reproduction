"""Materials-chemistry statistics/acceptance proposal rules (DEV-M11-G05).

The domain statistical proposal hooks: proposal helpers for replicate
design (the default ``n >= 3`` independent-replicate floor is PROPOSED,
never a hard rule -- Supervisor-overridable, with the auditable
rationale and a default flag, AC-02), measurement uncertainty metadata
(uncertainty kind/variance/reporting form as proposal records with
rationale, never hard-coded values) and acceptance construction WITHOUT
any universal fixed percent margin (a numeric tolerance can only enter
through explicit evidence-grounded arguments; the default acceptance
records no numeric tolerance at all, AC-01). Literature/method evidence
references (source identifiers with the evidence claims they support)
are attached BEFORE the acceptance is frozen (AC-03): freezing is a
Supervisor-only decision gated by the frozen role-action matrix of
``core.permissions`` (``Action.PLAN_FREEZE``), the flow refuses the
freeze of an acceptance whose recorded tolerance lacks the evidence that
grounds it, and a frozen acceptance carries exactly the evidence that
justified it. Grounded in ``07-STATISTICS-AND-ACCEPTANCE.md``
(SS2/SS3/SS8/SS9), ``20-ARCHITECTURE-DECISIONS.md`` items 9 and 17, and
the frozen core vocabulary (``DecisionMode`` of ``core.models``; the
``n >= 3`` default floor family ``DEFAULT_MIN_INDEPENDENT`` of
``analysis.replication``).

This package is the domain-pack wiring: ``domain_packs/__init__.py``
defines no registration mechanism, so the exports of this module (and
the sibling ``templates`` module) are the pack's public interface. No
core module is modified: the proposal helpers reuse the frozen core
APIs.
"""

from scientific_reproduction.domain_packs.materials_chemistry.statistics import (
    templates,
)
from scientific_reproduction.domain_packs.materials_chemistry.statistics.templates import (
    DEFAULT_REPLICATE_FLOOR,
    DEFAULT_REPLICATE_RATIONALE,
    FREEZE_ELIGIBILITY_RULES,
    STATISTICS_RULESET_VERSION,
    AcceptanceProposal,
    EvidenceClaim,
    EvidenceReference,
    FreezeEligibilityAssessment,
    FreezeEligibilityDecision,
    FreezeEligibilityRule,
    FrozenAcceptanceError,
    InvalidAcceptanceProposalError,
    InvalidReplicateProposalError,
    InvalidUncertaintyProposalError,
    MeasurementUncertaintyProposal,
    ReplicateDesignProposal,
    StatisticsProposalError,
    UncertaintyKind,
    assess_freeze_eligibility,
    attach_evidence,
    construct_acceptance_proposal,
    default_acceptance_proposal,
    default_replicate_design_proposal,
    effective_replicate_floor,
    freeze_acceptance_proposal,
    propose_measurement_uncertainty,
    set_replicate_override,
    validate_statistics_rulesets,
)

__all__ = [
    "DEFAULT_REPLICATE_FLOOR",
    "DEFAULT_REPLICATE_RATIONALE",
    "EvidenceClaim",
    "EvidenceReference",
    "FREEZE_ELIGIBILITY_RULES",
    "FrozenAcceptanceError",
    "InvalidAcceptanceProposalError",
    "InvalidReplicateProposalError",
    "InvalidUncertaintyProposalError",
    "MeasurementUncertaintyProposal",
    "ReplicateDesignProposal",
    "STATISTICS_RULESET_VERSION",
    "StatisticsProposalError",
    "UncertaintyKind",
    "AcceptanceProposal",
    "FreezeEligibilityAssessment",
    "FreezeEligibilityDecision",
    "FreezeEligibilityRule",
    "assess_freeze_eligibility",
    "attach_evidence",
    "construct_acceptance_proposal",
    "default_acceptance_proposal",
    "default_replicate_design_proposal",
    "effective_replicate_floor",
    "freeze_acceptance_proposal",
    "propose_measurement_uncertainty",
    "set_replicate_override",
    "templates",
    "validate_statistics_rulesets",
]
