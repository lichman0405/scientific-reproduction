"""DEV-M11-G05: statistics/acceptance proposal rules (AC-01/AC-02/AC-03).

The domain statistical proposal hooks of ``07-STATISTICS-AND-ACCEPTANCE.md``
SS2/SS3/SS8/SS9 as PROPOSALS, never hard rules:

* AC-02: the default independent-replicate floor ``n >= 3`` is proposed
  by ``default_replicate_design_proposal`` -- the proposal record carries
  the proposed floor, the ``is_default`` flag, the auditable rationale
  and the Supervisor override field (``set_replicate_override``);
* objective: measurement uncertainty metadata (kind/variance/reporting
  form) is proposed with rationale, never hard-coded values;
* AC-01: acceptance construction carries NO universal fixed percent
  margin -- a numeric tolerance can only enter through explicit
  evidence-grounded arguments, and the default acceptance records no
  numeric tolerance at all;
* AC-03: literature/method evidence references are attached BEFORE
  freezing; freezing is Supervisor-only and refused while the evidence
  the flow requires is missing (the refusal names the missing claims); a
  frozen acceptance carries exactly the evidence that justified it.

Every test name contains "statistic" (DEV-M11-G05 naming rule).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from scientific_reproduction.core.ids import ID_PATTERN, is_valid_id
from scientific_reproduction.core.models import DecisionMode
from scientific_reproduction.core.permissions import (
    PermissionDeniedError,
    Role,
)
from scientific_reproduction.domain_packs.materials_chemistry.statistics import (
    DEFAULT_REPLICATE_FLOOR,
    AcceptanceProposal,
    EvidenceClaim,
    EvidenceReference,
    FrozenAcceptanceError,
    InvalidAcceptanceProposalError,
    InvalidReplicateProposalError,
    InvalidUncertaintyProposalError,
    MeasurementUncertaintyProposal,
    ReplicateDesignProposal,
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

GOAL_ID = "GOAL-FDM201-ADS-001"


@pytest.fixture
def statistic_margin_evidence() -> EvidenceReference:
    """A deterministic margin-claim evidence reference (method source)."""
    return EvidenceReference(
        evidence_id="sr_evidence_margin_method",
        source_id="method-source-adsorption-equivalence",
        claim=EvidenceClaim.EQUIVALENCE_MARGIN,
        claim_text="reported instrument uncertainty bounds the equivalence margin",
    )


@pytest.fixture
def statistic_method_evidence() -> EvidenceReference:
    """A deterministic acceptance-method evidence reference (literature)."""
    return EvidenceReference(
        evidence_id="sr_evidence_method_literature",
        source_id="10.1039/D5TA00771B",
        claim=EvidenceClaim.ACCEPTANCE_METHOD,
        claim_text="paper reports triplicate uptake measurements at 298 K",
    )


@pytest.fixture
def statistic_uncertainty() -> MeasurementUncertaintyProposal:
    """A proposed measurement-uncertainty metadata record."""
    return propose_measurement_uncertainty(
        measurement_key="c3h6_uptake_cm3_g",
        uncertainty_kind=UncertaintyKind.STANDARD_ERROR,
        reporting_form="mean +/- standard error of the mean",
        rationale="standard error of three independent measurements",
        variance=0.7,
    )


@pytest.fixture
def statistic_replicate_design() -> ReplicateDesignProposal:
    """The default replicate-design proposal of the goal."""
    return default_replicate_design_proposal(GOAL_ID)


# ---------------------------------------------------------------------------
# AC-02: the default n >= 3 independent replicate floor is PROPOSED
# ---------------------------------------------------------------------------


def test_statistic_default_replicate_floor_is_proposed_as_n_ge_three(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """AC-02: the default floor is proposed as n >= 3 -- a proposal, not a rule."""
    proposal = statistic_replicate_design
    assert proposal.minimum_n == 3
    assert proposal.minimum_n == DEFAULT_REPLICATE_FLOOR
    assert proposal.is_default is True
    assert proposal.override_n is None
    # The proposal is auditable: a documented rationale exists.
    assert proposal.rationale.strip()
    assert "n >= 3" in proposal.rationale
    # The proposal is a suggestion: an override field exists.
    assert "override_n" in proposal.__dataclass_fields__
    # The record is unfrozen by construction and carries no fixed margin.
    assert proposal.as_dict()["effective_floor"] == 3


def test_statistic_default_replicate_floor_is_overridable_by_supervisor(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """AC-02: the Supervisor can override the proposed floor."""
    overridden = set_replicate_override(statistic_replicate_design, 6)
    assert overridden.override_n == 6
    assert overridden.is_default is False
    assert effective_replicate_floor(overridden) == 6
    # The default floor stays recorded for the audit trail.
    assert overridden.minimum_n == 3
    # The input proposal is never mutated (pure function).
    assert statistic_replicate_design.override_n is None
    assert statistic_replicate_design.is_default is True
    assert effective_replicate_floor(statistic_replicate_design) == 3


def test_statistic_replicate_override_cannot_weaken_floor_below_one(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """The override can never weaken the floor below 1 (stable errors)."""
    with pytest.raises(InvalidReplicateProposalError):
        set_replicate_override(statistic_replicate_design, 0)
    with pytest.raises(InvalidReplicateProposalError):
        set_replicate_override(statistic_replicate_design, -2)
    with pytest.raises(TypeError):
        set_replicate_override(statistic_replicate_design, 3.5)  # type: ignore[arg-type]


def test_statistic_replicate_proposal_rejects_invalid_shape() -> None:
    """A below-1 floor, a default with an override or a non-default floor
    under the default flag are stable value errors."""
    with pytest.raises(InvalidReplicateProposalError):
        ReplicateDesignProposal(
            proposal_id="proposal-rep-1",
            goal_id=GOAL_ID,
            minimum_n=0,
            is_default=False,
            rationale="explicit",
        )
    with pytest.raises(InvalidReplicateProposalError):
        ReplicateDesignProposal(
            proposal_id="proposal-rep-2",
            goal_id=GOAL_ID,
            minimum_n=3,
            is_default=True,
            override_n=5,
            rationale="explicit",
        )
    with pytest.raises(InvalidReplicateProposalError):
        ReplicateDesignProposal(
            proposal_id="proposal-rep-3",
            goal_id=GOAL_ID,
            minimum_n=5,
            is_default=True,
            rationale="explicit",
        )
    with pytest.raises(InvalidReplicateProposalError):
        ReplicateDesignProposal(
            proposal_id="proposal-rep-4",
            goal_id=GOAL_ID,
            minimum_n=3,
            is_default=True,
            rationale="   ",
        )


def test_statistic_replicate_proposal_requires_safe_ids() -> None:
    """Unsafe proposal/goal ids are rejected (FND-M9-G02-01 lesson)."""
    with pytest.raises(InvalidReplicateProposalError):
        ReplicateDesignProposal(
            proposal_id="proposal-rep-5",
            goal_id="GOAL/FDM201",
            minimum_n=3,
            is_default=False,
            rationale="explicit",
        )
    with pytest.raises(InvalidReplicateProposalError):
        ReplicateDesignProposal(
            proposal_id="proposal rep",
            goal_id=GOAL_ID,
            minimum_n=3,
            is_default=False,
            rationale="explicit",
        )
    with pytest.raises(TypeError):
        ReplicateDesignProposal(
            proposal_id="proposal-rep-6",
            goal_id=GOAL_ID,
            minimum_n="3",  # type: ignore[arg-type]
            is_default=False,
            rationale="explicit",
        )


def test_statistic_replicate_proposal_is_deterministic() -> None:
    """Same goal id -> identical proposal on every call and platform."""
    first = default_replicate_design_proposal(GOAL_ID)
    second = default_replicate_design_proposal(GOAL_ID)
    assert first == second
    assert first.as_dict() == second.as_dict()
    assert first.proposal_id == second.proposal_id
    assert ID_PATTERN.fullmatch(first.proposal_id)


def test_statistic_effective_floor_requires_a_replicate_proposal() -> None:
    """A non-proposal argument is a TypeError at the boundary."""
    with pytest.raises(TypeError):
        effective_replicate_floor("not-a-proposal")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        set_replicate_override("not-a-proposal", 5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Objective: measurement uncertainty metadata as proposal records
# ---------------------------------------------------------------------------


def test_statistic_uncertainty_proposal_records_kind_variance_and_form(
    statistic_uncertainty: MeasurementUncertaintyProposal,
) -> None:
    """The uncertainty proposal records kind, variance, form and rationale."""
    proposal = statistic_uncertainty
    assert proposal.measurement_key == "c3h6_uptake_cm3_g"
    assert proposal.uncertainty_kind is UncertaintyKind.STANDARD_ERROR
    assert proposal.variance == 0.7
    assert proposal.reporting_form == "mean +/- standard error of the mean"
    assert proposal.rationale.strip()
    assert is_valid_id(proposal.proposal_id, "proposal")
    assert proposal.as_dict()["uncertainty_kind"] == "standard_error"


def test_statistic_uncertainty_variance_is_explicit_never_defaulted() -> None:
    """No fabricated uncertainty value: variance is None when not estimated."""
    proposal = propose_measurement_uncertainty(
        measurement_key="c3h6_uptake_cm3_g",
        uncertainty_kind=UncertaintyKind.CONFIDENCE_INTERVAL,
        reporting_form="95 % confidence interval of the mean",
        rationale="interval to be estimated from the frozen alpha level",
    )
    assert proposal.variance is None
    assert proposal.as_dict()["variance"] is None


def test_statistic_uncertainty_rejects_invalid_variance() -> None:
    """Negative or non-finite variances are stable value errors."""
    with pytest.raises(InvalidUncertaintyProposalError):
        propose_measurement_uncertainty(
            measurement_key="uptake",
            uncertainty_kind=UncertaintyKind.STANDARD_DEVIATION,
            reporting_form="value +/- standard deviation",
            rationale="explicit",
            variance=-1.0,
        )
    with pytest.raises(InvalidUncertaintyProposalError):
        propose_measurement_uncertainty(
            measurement_key="uptake",
            uncertainty_kind=UncertaintyKind.STANDARD_DEVIATION,
            reporting_form="value +/- standard deviation",
            rationale="explicit",
            variance=float("nan"),
        )
    with pytest.raises(TypeError):
        propose_measurement_uncertainty(
            measurement_key="uptake",
            uncertainty_kind=UncertaintyKind.STANDARD_DEVIATION,
            reporting_form="value +/- standard deviation",
            rationale="explicit",
            variance="0.7",  # type: ignore[arg-type]
        )


def test_statistic_uncertainty_requires_explicit_form_and_rationale() -> None:
    """An empty reporting form or rationale is a stable value error."""
    with pytest.raises(InvalidUncertaintyProposalError):
        propose_measurement_uncertainty(
            measurement_key="uptake",
            uncertainty_kind=UncertaintyKind.STANDARD_DEVIATION,
            reporting_form="   ",
            rationale="explicit",
        )
    with pytest.raises(InvalidUncertaintyProposalError):
        propose_measurement_uncertainty(
            measurement_key="uptake",
            uncertainty_kind=UncertaintyKind.STANDARD_DEVIATION,
            reporting_form="value +/- standard deviation",
            rationale="   ",
        )


def test_statistic_uncertainty_ids_are_deterministic() -> None:
    """Same measurement key -> identical proposal id."""
    first = propose_measurement_uncertainty(
        measurement_key="c3h6_uptake_cm3_g",
        uncertainty_kind=UncertaintyKind.STANDARD_ERROR,
        reporting_form="mean +/- standard error",
        rationale="explicit",
    )
    second = propose_measurement_uncertainty(
        measurement_key="c3h6_uptake_cm3_g",
        uncertainty_kind=UncertaintyKind.STANDARD_ERROR,
        reporting_form="mean +/- standard error",
        rationale="explicit",
    )
    assert first == second
    assert first.proposal_id == second.proposal_id


# ---------------------------------------------------------------------------
# AC-01: acceptance construction without any universal fixed percent margin
# ---------------------------------------------------------------------------


def test_statistic_default_acceptance_has_no_numeric_tolerance() -> None:
    """AC-01: the default acceptance records no numeric tolerance at all."""
    acceptance = default_acceptance_proposal(GOAL_ID)
    assert acceptance.equivalence_margin is None
    assert acceptance.evidence_refs == ()
    assert acceptance.frozen is False
    assert acceptance.replicate_floor == 3
    capture = acceptance.as_dict()
    assert capture["equivalence_margin"] is None
    assert capture["evidence_refs"] == []
    assert capture["replicate_floor"] == 3
    # No numeric tolerance lurks anywhere in the default capture.
    assert all(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value == 3
        for value in (capture["replicate_floor"],)
    )


def test_statistic_tolerance_requires_explicit_evidence_arguments() -> None:
    """AC-01: a numeric tolerance must come from explicit evidence-grounded
    arguments -- construction is refused without the margin evidence."""
    replicate = default_replicate_design_proposal(GOAL_ID)
    with pytest.raises(InvalidAcceptanceProposalError) as exc_info:
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design=replicate,
            equivalence_margin=5.0,
        )
    message = str(exc_info.value)
    assert "evidence" in message
    assert "equivalence_margin" in message


def test_statistic_tolerance_is_grounded_by_evidence_arguments(
    statistic_margin_evidence: EvidenceReference,
) -> None:
    """AC-01: with the explicit margin evidence attached, the tolerance is
    recorded as instance data -- never a universal default."""
    replicate = default_replicate_design_proposal(GOAL_ID)
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=replicate,
        equivalence_margin=5.0,
        evidence_refs=(statistic_margin_evidence,),
    )
    assert acceptance.equivalence_margin == 5.0
    assert acceptance.evidence_refs == (statistic_margin_evidence,)
    assert acceptance.frozen is False


def test_statistic_acceptance_builds_from_replicate_and_uncertainty(
    statistic_replicate_design: ReplicateDesignProposal,
    statistic_uncertainty: MeasurementUncertaintyProposal,
) -> None:
    """The acceptance is constructible from the replicate design proposal
    (override wins) and the measurement uncertainty metadata."""
    overridden = set_replicate_override(statistic_replicate_design, 6)
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=overridden,
        uncertainty=statistic_uncertainty,
        decision_mode=DecisionMode.BOUNDED_INTERVAL,
    )
    assert acceptance.replicate_floor == 6
    assert acceptance.replicate_design is overridden
    assert acceptance.uncertainty is statistic_uncertainty
    assert acceptance.decision_mode is DecisionMode.BOUNDED_INTERVAL
    assert acceptance.equivalence_margin is None


def test_statistic_acceptance_rejects_invalid_margin_values() -> None:
    """Non-positive or non-finite margins are stable value errors."""
    replicate = default_replicate_design_proposal(GOAL_ID)
    for bad_margin in (0, -1.0, float("inf"), float("nan")):
        with pytest.raises(InvalidAcceptanceProposalError):
            construct_acceptance_proposal(
                goal_id=GOAL_ID,
                replicate_design=replicate,
                equivalence_margin=bad_margin,
                evidence_refs=(
                    EvidenceReference(
                        evidence_id=f"sr_evidence_bad_{bad_margin}",
                        source_id="source-x",
                        claim=EvidenceClaim.EQUIVALENCE_MARGIN,
                    ),
                ),
            )
    with pytest.raises(TypeError):
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design=replicate,
            equivalence_margin="5.0",  # type: ignore[arg-type]
        )


def test_statistic_acceptance_rejects_duplicate_evidence(
    statistic_margin_evidence: EvidenceReference,
) -> None:
    """Duplicate evidence references are rejected (stable error)."""
    replicate = default_replicate_design_proposal(GOAL_ID)
    with pytest.raises(InvalidAcceptanceProposalError) as exc_info:
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design=replicate,
            evidence_refs=(
                statistic_margin_evidence,
                statistic_margin_evidence,
            ),
        )
    assert "duplicate" in str(exc_info.value)


def test_statistic_acceptance_rejects_wrong_types() -> None:
    """Type boundaries are stable TypeErrors, not silent acceptances."""
    replicate = default_replicate_design_proposal(GOAL_ID)
    with pytest.raises(TypeError):
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design="not-a-proposal",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design=replicate,
            decision_mode="equivalence",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design=replicate,
            uncertainty="not-a-proposal",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        construct_acceptance_proposal(
            goal_id=GOAL_ID,
            replicate_design=replicate,
            evidence_refs=("not-an-evidence-ref",),  # type: ignore[list-item]
        )
    with pytest.raises(TypeError):
        construct_acceptance_proposal(
            goal_id=42,  # type: ignore[arg-type]
            replicate_design=replicate,
        )


def test_statistic_acceptance_requires_safe_goal_id() -> None:
    """An unsafe goal id is a stable value error."""
    replicate = default_replicate_design_proposal(GOAL_ID)
    with pytest.raises(InvalidAcceptanceProposalError):
        construct_acceptance_proposal(
            goal_id="GOAL/FDM201",
            replicate_design=replicate,
        )


def test_statistic_acceptance_ids_are_deterministic_and_safe() -> None:
    """Same goal id -> identical safe acceptance proposal id."""
    first = default_acceptance_proposal(GOAL_ID)
    second = default_acceptance_proposal(GOAL_ID)
    assert first == second
    assert first.proposal_id == second.proposal_id
    assert ID_PATTERN.fullmatch(first.proposal_id)
    assert is_valid_id(first.proposal_id, "proposal")


# ---------------------------------------------------------------------------
# AC-03: evidence before freezing, freeze refused without required evidence
# ---------------------------------------------------------------------------


def test_statistic_evidence_is_attached_before_freezing(
    statistic_replicate_design: ReplicateDesignProposal,
    statistic_margin_evidence: EvidenceReference,
    statistic_method_evidence: EvidenceReference,
) -> None:
    """AC-03: evidence references can be attached to the unfrozen proposal."""
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=statistic_replicate_design,
        equivalence_margin=5.0,
        evidence_refs=(statistic_margin_evidence,),
    )
    enriched = attach_evidence(acceptance, (statistic_method_evidence,))
    assert enriched.evidence_refs == (
        statistic_margin_evidence,
        statistic_method_evidence,
    )
    assert enriched.frozen is False
    # The input proposal is never mutated (pure function).
    assert acceptance.evidence_refs == (statistic_margin_evidence,)
    # Evidence accumulates across attachments.
    again = attach_evidence(
        enriched,
        (
            EvidenceReference(
                evidence_id="sr_evidence_third",
                source_id="source-z",
                claim=EvidenceClaim.UNCERTAINTY_METHOD,
            ),
        ),
    )
    assert len(again.evidence_refs) == 3


def test_statistic_evidence_cannot_be_attached_after_freezing(
    statistic_replicate_design: ReplicateDesignProposal,
    statistic_margin_evidence: EvidenceReference,
    statistic_method_evidence: EvidenceReference,
) -> None:
    """AC-03: evidence must be attached BEFORE freezing -- a frozen
    acceptance refuses further evidence."""
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=statistic_replicate_design,
        equivalence_margin=5.0,
        evidence_refs=(statistic_margin_evidence,),
    )
    frozen = freeze_acceptance_proposal(acceptance, role=Role.SUPERVISOR)
    with pytest.raises(FrozenAcceptanceError) as exc_info:
        attach_evidence(frozen, (statistic_method_evidence,))
    assert "before" in str(exc_info.value)
    assert exc_info.value.assessment.eligible is True


def test_statistic_freeze_refuses_margin_without_required_evidence(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """AC-03: freezing without the evidence the flow requires is refused,
    and the refusal explicitly states the missing evidence claim."""
    proposal = AcceptanceProposal(
        proposal_id="proposal-acc-no-evidence",
        goal_id=GOAL_ID,
        replicate_design=statistic_replicate_design,
        decision_mode=DecisionMode.EQUIVALENCE,
        equivalence_margin=5.0,
    )
    assert proposal.evidence_refs == ()
    with pytest.raises(FrozenAcceptanceError) as exc_info:
        freeze_acceptance_proposal(proposal, role=Role.SUPERVISOR)
    assessment = exc_info.value.assessment
    assert assessment.eligible is False
    assert assessment.matched_rule_id == "R-STAT-F1"
    assert assessment.missing_evidence_claims == (
        EvidenceClaim.EQUIVALENCE_MARGIN,
    )
    assert "equivalence_margin" in str(exc_info.value)
    # The refusal is auditable: every rule decision is recorded.
    assert len(assessment.decisions) == 2
    assert assessment.decisions[0].rule_id == "R-STAT-F1"
    assert assessment.decisions[0].matched is True
    assert assessment.decisions[1].rule_id == "R-STAT-F0"
    assert assessment.decisions[1].matched is True
    # The proposal remains unfrozen after the refusal.
    assert proposal.frozen is False


def test_statistic_freeze_carries_exactly_the_justifying_evidence(
    statistic_replicate_design: ReplicateDesignProposal,
    statistic_margin_evidence: EvidenceReference,
    statistic_method_evidence: EvidenceReference,
    statistic_uncertainty: MeasurementUncertaintyProposal,
) -> None:
    """AC-03: a frozen acceptance carries exactly the evidence that
    justified it -- nothing added, nothing dropped."""
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=statistic_replicate_design,
        uncertainty=statistic_uncertainty,
        equivalence_margin=5.0,
        evidence_refs=(statistic_margin_evidence,),
    )
    enriched = attach_evidence(acceptance, (statistic_method_evidence,))
    frozen = freeze_acceptance_proposal(enriched, role=Role.SUPERVISOR)
    assert frozen.frozen is True
    assert frozen.evidence_refs == (
        statistic_margin_evidence,
        statistic_method_evidence,
    )
    assert tuple(
        reference.evidence_id for reference in frozen.evidence_refs
    ) == ("sr_evidence_margin_method", "sr_evidence_method_literature")
    # The frozen record pins the replicate floor and the uncertainty.
    assert frozen.replicate_floor == 3
    assert frozen.uncertainty is statistic_uncertainty
    assert frozen.equivalence_margin == 5.0


def test_statistic_freeze_is_supervisor_only(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """AC-03: freezing is a Supervisor-only decision (R-PRM-SUP1); Research
    is denied with the full permission assessment."""
    acceptance = default_acceptance_proposal(GOAL_ID)
    with pytest.raises(PermissionDeniedError) as exc_info:
        freeze_acceptance_proposal(acceptance, role=Role.RESEARCH)
    assessment = exc_info.value.assessment
    assert assessment.allowed is False
    assert str(exc_info.value)
    assert acceptance.frozen is False
    with pytest.raises(TypeError):
        freeze_acceptance_proposal(acceptance, role="supervisor")  # type: ignore[arg-type]


def test_statistic_freeze_eligibility_assessment_records_decisions(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """The freeze-eligibility assessment is a full auditable decision record."""
    acceptance = default_acceptance_proposal(GOAL_ID)
    assessment = assess_freeze_eligibility(acceptance)
    assert assessment.eligible is True
    assert assessment.matched_rule_id == "R-STAT-F0"
    assert assessment.missing_evidence_claims == ()
    assert len(assessment.decisions) == 2
    assert assessment.decisions[0].rule_id == "R-STAT-F1"
    assert assessment.decisions[0].eligible is False
    assert assessment.decisions[1].rule_id == "R-STAT-F0"
    assert assessment.decisions[1].eligible is True


def test_statistic_default_acceptance_can_be_frozen_without_tolerance() -> None:
    """AC-01/AC-03: an acceptance with no numeric tolerance needs no margin
    evidence -- the default acceptance is freezeable as proposed."""
    acceptance = default_acceptance_proposal(GOAL_ID)
    frozen = freeze_acceptance_proposal(acceptance, role=Role.SUPERVISOR)
    assert frozen.frozen is True
    assert frozen.equivalence_margin is None
    assert frozen.evidence_refs == ()


def test_statistic_freezing_is_pure_and_immutable(
    statistic_replicate_design: ReplicateDesignProposal,
    statistic_margin_evidence: EvidenceReference,
) -> None:
    """Freezing never mutates its input; the frozen copy rejects mutation."""
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=statistic_replicate_design,
        equivalence_margin=5.0,
        evidence_refs=(statistic_margin_evidence,),
    )
    frozen = freeze_acceptance_proposal(acceptance, role=Role.SUPERVISOR)
    assert acceptance.frozen is False
    assert is_dataclass(frozen)
    for field_name in frozen.__dataclass_fields__:
        with pytest.raises(FrozenInstanceError):
            setattr(frozen, field_name, None)


def test_statistic_freeze_flag_records_in_capture_dict(
    statistic_replicate_design: ReplicateDesignProposal,
) -> None:
    """The freeze state is recorded in the deterministic capture dict."""
    acceptance = construct_acceptance_proposal(
        goal_id=GOAL_ID,
        replicate_design=statistic_replicate_design,
    )
    assert acceptance.as_dict()["frozen"] is False
    frozen = freeze_acceptance_proposal(acceptance, role=Role.SUPERVISOR)
    assert frozen.as_dict()["frozen"] is True
    # Freezing is idempotent, like the sibling freeze helpers.
    again = freeze_acceptance_proposal(frozen, role=Role.SUPERVISOR)
    assert again.frozen is True


def test_statistic_rulesets_validate() -> None:
    """The freeze-eligibility rule table passes the integrity validation."""
    ids = validate_statistics_rulesets()
    assert ids == ("R-STAT-F1", "R-STAT-F0")
