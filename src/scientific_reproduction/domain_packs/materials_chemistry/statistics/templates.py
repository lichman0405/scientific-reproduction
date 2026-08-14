"""Materials-chemistry statistics/acceptance proposal rules (DEV-M11-G05).

Implements the **domain statistical proposal hooks** deliverable of
DEV-M11-G05 for the materials-chemistry domain pack: proposal helpers
for replicate design, measurement uncertainty metadata and acceptance
construction -- every default is a PROPOSAL the Supervisor can override
or enrich with evidence, never a universal fixed percent margin rule.
Grounded in:

* ``07-STATISTICS-AND-ACCEPTANCE.md`` SS2 (independent replication by
  default; the default floor is ``n >= 3`` and the Supervisor
  dynamically determines the final sample size from variability,
  precision, equivalence margin, power logic, cost, prior evidence and
  domain guidance), SS3 (preferred acceptance logic: confidence
  intervals, effect sizes, equivalence tests, bootstrap intervals,
  hierarchical models, robust methods, uncertainty propagation,
  measurement uncertainty), SS8 (every numeric margin or decision
  threshold records its basis -- target-paper error, independent
  reproduction literature, standard method/instrument uncertainty,
  domain-specific accepted threshold, or an explicit documented
  scientific judgment; *no global fixed percent rule is allowed*) and
  SS9 (freeze target metrics, equivalence margin, replication design,
  primary statistical method, alpha level and outlier rules BEFORE data
  generation; changes after data are observed require a versioned
  Supervisor decision);
* ``20-ARCHITECTURE-DECISIONS.md`` item 9 (independent experimental
  replication mandatory; default floor ``n >= 3``, final ``n``
  dynamically designed) and item 17 (evidence is Source x Claim
  specific -- the evidence references below are exactly
  (source, claim) pairs);
* ``03-ROLE-AND-PERMISSION-SPEC.md`` SS2 and ``core/permissions.py``
  (DEV-M6-G03) -- the role-action matrix: research/domain helpers
  PROPOSE, but freezing is Supervisor-only (``Action.PLAN_FREEZE``,
  granted only to the Supervisor by ``R-PRM-SUP1``); the freeze helper
  is gated by the matrix, so an acceptance is never silently frozen;
* ``core/models.py`` -- the frozen vocabulary reused verbatim:
  ``DecisionMode`` (the acceptance decision-mode vocabulary, e.g.
  ``EQUIVALENCE`` / ``BOUNDED_INTERVAL``) and ``GoalReplication``;
* ``analysis/replication.py`` -- the EXISTING frozen default floor
  family ``DEFAULT_MIN_INDEPENDENT`` (the ``n >= 3`` default of 07-SS2
  and 20-AD item 9) is reused by reference, never redefined: the
  statistics pack proposes it, the synthesis pack records it, the
  analysis evaluator consumes it -- one frozen constant;
* ``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`` SS5 (domain acceptance
  examples are templates, never universal thresholds).

Proposal model (determinism and boundaries)
-------------------------------------------
Every proposal is a frozen dataclass with strict ``__post_init__``
validation: ``TypeError`` at the type boundaries, ``ValueError``-subclass
stable errors (``StatisticsProposalError`` and siblings) for value
violations. Ids follow the house ``core.ids.generate_id`` discipline:
deterministic pure functions of the canonical fields (no randomness, no
wall clock); ids are safe single registry path segments (FND-M9-G02-01
lesson: no path separators, no glob metacharacters). External evidence
identifiers (citations, method sources, e.g. DOIs) are recorded as
given -- they are references to external sources, never path segments.

AC-02 (replicate design is PROPOSED, never a rule): the default
independent-replicate floor ``n >= 3`` is proposed by
:func:`default_replicate_design_proposal` as a :class:`ReplicateDesignProposal`
record carrying the proposed floor, the ``is_default`` flag, the
auditable rationale and the Supervisor override field; the floor can be
overridden by :func:`set_replicate_override` and the effective floor is
read by :func:`effective_replicate_floor`.

Measurement uncertainty (objective): :func:`propose_measurement_uncertainty`
proposes measurement uncertainty metadata (uncertainty kind, variance,
reporting form) for a measurement/run as a :class:`MeasurementUncertaintyProposal`
record with rationale -- values are explicit, never fabricated defaults.

AC-01 (no universal fixed percent margin): :func:`construct_acceptance_proposal`
and :func:`default_acceptance_proposal` construct acceptance proposals
in which a numeric equivalence margin can ONLY appear through explicit
evidence-grounded arguments -- construction refuses a margin without an
attached evidence reference supporting it (``EvidenceClaim.EQUIVALENCE_MARGIN``),
and the default path proposes no numeric tolerance at all. No percent
margin constant exists anywhere in this module.

AC-03 (evidence before freezing): :func:`attach_evidence` attaches
literature/method evidence references (source identifiers with the
evidence claims they support) to an acceptance proposal BEFORE it is
frozen and refuses to mutate a frozen acceptance. :func:`freeze_acceptance_proposal`
freezes the acceptance -- a Supervisor-only decision through the frozen
role-action matrix -- after :func:`assess_freeze_eligibility` decides
eligibility through the ordered ``FREEZE_ELIGIBILITY_RULES`` table: an
acceptance carrying a numeric tolerance whose margin evidence is missing
is refused (``FrozenAcceptanceError`` naming the missing claims), and a
frozen acceptance carries exactly the evidence that justified it.

Pure deterministic module: no randomness, no wall clock, no network, no
I/O anywhere; same inputs -> same proposals, assessments and freeze
records on every call and platform. ``from __future__ import annotations``;
``__all__``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Sequence

from scientific_reproduction.analysis.replication import DEFAULT_MIN_INDEPENDENT
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import DecisionMode
from scientific_reproduction.core.permissions import (
    Action,
    PermissionDeniedError,
    Role,
    check_action_allowed,
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
    "validate_statistics_rulesets",
]

#: Version of the statistics rule tables. Bumped whenever a rule changes;
#: recorded in every assessment so old decisions stay interpretable.
STATISTICS_RULESET_VERSION: str = "1.0"

#: The proposed default independent-replicate floor family
#: (07-STATISTICS-AND-ACCEPTANCE.md SS2: "Default floor: n >= 3";
#: 20-ARCHITECTURE-DECISIONS.md item 9). Reused by reference from the
#: frozen analysis replication evaluator -- the statistics pack PROPOSES
#: this floor, it never redefines it.
DEFAULT_REPLICATE_FLOOR: int = DEFAULT_MIN_INDEPENDENT


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class StatisticsProposalError(ValueError):
    """Base class for all statistics-proposal errors."""


class InvalidReplicateProposalError(StatisticsProposalError):
    """Raised when a replicate-design proposal violates the proposal shape."""


class InvalidUncertaintyProposalError(StatisticsProposalError):
    """Raised when an uncertainty proposal violates the proposal shape."""


class InvalidAcceptanceProposalError(StatisticsProposalError):
    """Raised when an acceptance proposal violates the proposal shape.

    Also raised by :func:`construct_acceptance_proposal` when a numeric
    tolerance is requested without the explicit evidence that grounds it
    (AC-01: a numeric margin is never a universal default).
    """


class FrozenAcceptanceError(StatisticsProposalError):
    """Raised when a frozen acceptance is mutated or freezing is refused.

    ``assessment`` carries the full freeze-eligibility decision record so
    the caller can persist the audit trail (which rules matched and which
    evidence claims are missing).
    """

    def __init__(self, message: str, assessment: FreezeEligibilityAssessment) -> None:
        super().__init__(message)
        self.assessment: FreezeEligibilityAssessment = assessment


# ---------------------------------------------------------------------------
# Evidence vocabulary (AC-03: evidence is Source x Claim specific,
# 20-ARCHITECTURE-DECISIONS.md item 17)
# ---------------------------------------------------------------------------


class EvidenceClaim(StrEnum):
    """The frozen claim vocabulary of the acceptance evidence references.

    Every evidence reference is a (source, claim) pair: the claim names
    which element of the acceptance the source establishes. The claim
    vocabulary is universal method vocabulary -- no paper, no method
    instance (AC-03).
    """

    REPLICATE_FLOOR = "replicate_floor"
    UNCERTAINTY_METHOD = "uncertainty_method"
    EQUIVALENCE_MARGIN = "equivalence_margin"
    ACCEPTANCE_METHOD = "acceptance_method"


@dataclass(frozen=True)
class EvidenceReference:
    """One literature/method evidence reference of an acceptance (AC-03).

    A (source, claim) pair in the house evidence vocabulary
    (20-ARCHITECTURE-DECISIONS.md item 17: evidence is Source x Claim
    specific): ``source_id`` is the citation/method-source identifier
    (e.g. a DOI or method designation -- an external reference, recorded
    as given, never a path segment), ``claim`` is the frozen
    ``EvidenceClaim`` the source supports, and ``claim_text`` optionally
    states what the source establishes. ``evidence_id`` is a
    deterministic id derived from the source and the claim
    (``core.ids.generate_id``), so the same (source, claim) pair always
    names the same evidence.

    Raises:
        TypeError: a field has the wrong type.
        InvalidAcceptanceProposalError: an empty source id or claim text.
    """

    evidence_id: str
    source_id: str
    claim: EvidenceClaim
    claim_text: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str):
            raise TypeError(
                "EvidenceReference.evidence_id must be a str, got"
                f" {type(self.evidence_id).__name__}"
            )
        if not self.evidence_id.strip():
            raise InvalidAcceptanceProposalError(
                "EvidenceReference.evidence_id must be a non-empty string,"
                f" got {self.evidence_id!r}"
            )
        if not isinstance(self.source_id, str):
            raise TypeError(
                "EvidenceReference.source_id must be a str, got"
                f" {type(self.source_id).__name__}"
            )
        if not self.source_id.strip():
            raise InvalidAcceptanceProposalError(
                "EvidenceReference.source_id must be a non-empty source"
                f" identifier, got {self.source_id!r}"
            )
        if not isinstance(self.claim, EvidenceClaim):
            raise TypeError(
                "EvidenceReference.claim must be an EvidenceClaim member,"
                f" got {type(self.claim).__name__}"
            )
        if self.claim_text is not None and not isinstance(self.claim_text, str):
            raise TypeError(
                "EvidenceReference.claim_text must be a str or None, got"
                f" {type(self.claim_text).__name__}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (proposal-capture shape)."""
        return {
            "evidence_id": self.evidence_id,
            "source_id": self.source_id,
            "claim": self.claim.value,
            "claim_text": self.claim_text,
        }


def _evidence_reference(source_id: str, claim: EvidenceClaim) -> EvidenceReference:
    """A deterministic evidence reference for a (source, claim) pair."""
    return EvidenceReference(
        evidence_id=generate_id("evidence", source_id, claim.value),
        source_id=source_id,
        claim=claim,
    )


# ---------------------------------------------------------------------------
# Uncertainty vocabulary (07-STATISTICS-AND-ACCEPTANCE.md SS3)
# ---------------------------------------------------------------------------


class UncertaintyKind(StrEnum):
    """The frozen uncertainty-kind vocabulary of the proposals.

    Universal statistical vocabulary (07-SS3 method family names) -- the
    variance VALUES are instance data, never defaults: a proposal records
    which uncertainty quantity the measurement carries and its reporting
    form; the estimate itself is explicit or explicitly absent.
    """

    STANDARD_DEVIATION = "standard_deviation"
    STANDARD_ERROR = "standard_error"
    CONFIDENCE_INTERVAL = "confidence_interval"
    ROBUST_SPREAD = "robust_spread"


@dataclass(frozen=True)
class MeasurementUncertaintyProposal:
    """Proposed measurement uncertainty metadata of one measurement/run.

    A proposal record (never a hard-coded value): the measurement/run the
    metadata attaches to, the uncertainty kind, the proposed variance
    estimate (``None`` when not yet estimated -- an honest planning
    state) and the reporting form (e.g. how the uncertainty is reported
    with the value), each with the auditable rationale. Every field is
    explicit: the helper never fabricates a default variance or a default
    reporting form.

    Raises:
        TypeError: a field has the wrong type.
        InvalidUncertaintyProposalError: a value violation (empty
            measurement key or rationale, non-finite or negative
            variance).
    """

    proposal_id: str
    measurement_key: str
    uncertainty_kind: UncertaintyKind
    reporting_form: str
    rationale: str
    variance: float | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _validate_safe_segment(
            type(self).__name__,
            "proposal_id",
            self.proposal_id,
            InvalidUncertaintyProposalError,
        )
        _validate_safe_segment(
            type(self).__name__,
            "measurement_key",
            self.measurement_key,
            InvalidUncertaintyProposalError,
        )
        if not isinstance(self.uncertainty_kind, UncertaintyKind):
            raise TypeError(
                "MeasurementUncertaintyProposal.uncertainty_kind must be an"
                " UncertaintyKind member, got"
                f" {type(self.uncertainty_kind).__name__}"
            )
        if not isinstance(self.reporting_form, str):
            raise TypeError(
                "MeasurementUncertaintyProposal.reporting_form must be a"
                f" str, got {type(self.reporting_form).__name__}"
            )
        if not self.reporting_form.strip():
            raise InvalidUncertaintyProposalError(
                "MeasurementUncertaintyProposal.reporting_form must be a"
                f" non-empty string, got {self.reporting_form!r}"
            )
        if not isinstance(self.rationale, str):
            raise TypeError(
                "MeasurementUncertaintyProposal.rationale must be a str, got"
                f" {type(self.rationale).__name__}"
            )
        if not self.rationale.strip():
            raise InvalidUncertaintyProposalError(
                "MeasurementUncertaintyProposal.rationale must be a"
                f" non-empty string, got {self.rationale!r}"
            )
        variance = self.variance
        if variance is not None:
            if isinstance(variance, bool) or not isinstance(variance, (int, float)):
                raise TypeError(
                    "MeasurementUncertaintyProposal.variance must be a"
                    f" number or None, got {type(variance).__name__}"
                )
            if not math.isfinite(variance) or variance < 0:
                raise InvalidUncertaintyProposalError(
                    "MeasurementUncertaintyProposal.variance must be a"
                    f" finite non-negative number, got {variance!r}"
                )
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError(
                "MeasurementUncertaintyProposal.notes must be a str or"
                f" None, got {type(self.notes).__name__}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (proposal-capture shape)."""
        return {
            "proposal_id": self.proposal_id,
            "measurement_key": self.measurement_key,
            "uncertainty_kind": self.uncertainty_kind.value,
            "variance": self.variance,
            "reporting_form": self.reporting_form,
            "rationale": self.rationale,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Replicate design proposal (AC-02: proposed, never a hard rule)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplicateDesignProposal:
    """The proposed independent-replicate design of a goal (AC-02).

    A PROPOSAL record -- a suggestion the Supervisor can override, never
    a hard rule: ``minimum_n`` is the proposed independent-replicate
    floor, ``is_default`` records whether this proposal IS the frozen
    default (the ``n >= 3`` family of 07-STATISTICS-AND-ACCEPTANCE.md
    SS2 / 20-ARCHITECTURE-DECISIONS.md item 9, carried by reference as
    ``DEFAULT_REPLICATE_FLOOR``), ``rationale`` is the auditable
    reasoning and ``override_n`` is the field the Supervisor can set to
    override the floor (:func:`set_replicate_override` -- the default
    itself is never weakened below 1). The effective floor is
    :func:`effective_replicate_floor`: the override when set, else the
    proposed floor.

    Raises:
        TypeError: a field has the wrong type.
        InvalidReplicateProposalError: a value violation (floor below 1,
            an override below 1, a default flag on a non-default floor,
            a default flag combined with an override, empty rationale).
    """

    proposal_id: str
    goal_id: str
    minimum_n: int
    is_default: bool
    rationale: str
    override_n: int | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _validate_safe_segment(
            type(self).__name__,
            "proposal_id",
            self.proposal_id,
            InvalidReplicateProposalError,
        )
        _validate_safe_segment(
            type(self).__name__,
            "goal_id",
            self.goal_id,
            InvalidReplicateProposalError,
        )
        if not isinstance(self.minimum_n, int) or isinstance(self.minimum_n, bool):
            raise TypeError(
                "ReplicateDesignProposal.minimum_n must be an int, got"
                f" {type(self.minimum_n).__name__}"
            )
        if self.minimum_n < 1:
            raise InvalidReplicateProposalError(
                "ReplicateDesignProposal.minimum_n must be at least 1 (the"
                " floor can never be weakened below 1; the frozen default"
                f" proposal is {DEFAULT_REPLICATE_FLOOR}), got"
                f" {self.minimum_n}"
            )
        if not isinstance(self.is_default, bool):
            raise TypeError(
                "ReplicateDesignProposal.is_default must be a bool, got"
                f" {type(self.is_default).__name__}"
            )
        if not isinstance(self.rationale, str):
            raise TypeError(
                "ReplicateDesignProposal.rationale must be a str, got"
                f" {type(self.rationale).__name__}"
            )
        if not self.rationale.strip():
            raise InvalidReplicateProposalError(
                "ReplicateDesignProposal.rationale must be a non-empty"
                f" string, got {self.rationale!r}"
            )
        override = self.override_n
        if override is not None:
            if not isinstance(override, int) or isinstance(override, bool):
                raise TypeError(
                    "ReplicateDesignProposal.override_n must be an int or"
                    f" None, got {type(override).__name__}"
                )
            if override < 1:
                raise InvalidReplicateProposalError(
                    "ReplicateDesignProposal.override_n must be at least 1"
                    " when set (the floor can never be weakened below 1),"
                    f" got {override}"
                )
        if self.is_default:
            if self.minimum_n != DEFAULT_REPLICATE_FLOOR:
                raise InvalidReplicateProposalError(
                    "a default replicate-design proposal proposes the"
                    f" frozen default floor {DEFAULT_REPLICATE_FLOOR}, got"
                    f" minimum_n {self.minimum_n}"
                )
            if override is not None:
                raise InvalidReplicateProposalError(
                    "a default replicate-design proposal carries no"
                    " override: the default and the override are distinct"
                    " proposal states"
                )
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError(
                "ReplicateDesignProposal.notes must be a str or None, got"
                f" {type(self.notes).__name__}"
            )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (proposal-capture shape)."""
        return {
            "proposal_id": self.proposal_id,
            "goal_id": self.goal_id,
            "minimum_n": self.minimum_n,
            "is_default": self.is_default,
            "override_n": self.override_n,
            "effective_floor": effective_replicate_floor(self),
            "rationale": self.rationale,
            "notes": self.notes,
        }


#: The auditable rationale of the default replicate-design proposal
#: (07-STATISTICS-AND-ACCEPTANCE.md SS2 and 20-ARCHITECTURE-DECISIONS.md
#: item 9). The final sample size is dynamically designed by the
#: Supervisor from variability, precision, equivalence margin, power
#: logic, cost, prior evidence and domain guidance; the proposal is the
#: floor, not the final count.
DEFAULT_REPLICATE_RATIONALE: str = (
    "independent experimental replication is the default for experimental"
    " goals (07-STATISTICS-AND-ACCEPTANCE.md SS2): the proposed floor is"
    " the frozen n >= 3 default family (20-ARCHITECTURE-DECISIONS.md item"
    " 9). The floor is a proposal, not a rule: the Supervisor determines"
    " the final sample size from expected variability, required"
    " confidence/precision, the equivalence margin, power/sample-size"
    " logic, experimental cost/feasibility, prior evidence and"
    " domain-specific guidance, and may override the floor"
    " (set_replicate_override); technical replicates and instrument"
    " repeats are additional evidence and never replace independent"
    " replication."
)


def default_replicate_design_proposal(goal_id: str) -> ReplicateDesignProposal:
    """Propose the default independent-replicate design of a goal (AC-02).

    The default replicate floor ``n >= 3`` is PROPOSED as a
    :class:`ReplicateDesignProposal` -- a suggestion the Supervisor can
    override, not a hard rule: the proposal carries the proposed floor
    (``minimum_n`` = ``DEFAULT_REPLICATE_FLOOR``, the frozen default
    family of 07-STATISTICS-AND-ACCEPTANCE.md SS2 / 20-ARCHITECTURE-DECISIONS.md
    item 9, reused by reference from ``analysis.replication``), the
    ``is_default`` flag, the auditable rationale and the override field.
    Pure and deterministic: same goal id -> identical proposal on every
    call and platform.

    Raises:
        TypeError: ``goal_id`` is not a str.
        InvalidReplicateProposalError: ``goal_id`` is not a safe segment.
    """
    if not isinstance(goal_id, str):
        raise TypeError(f"goal_id must be a str, got {type(goal_id).__name__}")
    return ReplicateDesignProposal(
        proposal_id=generate_id("proposal", "replicate-design", goal_id),
        goal_id=goal_id,
        minimum_n=DEFAULT_REPLICATE_FLOOR,
        is_default=True,
        rationale=DEFAULT_REPLICATE_RATIONALE,
        override_n=None,
    )


def effective_replicate_floor(proposal: ReplicateDesignProposal) -> int:
    """The proposed replicate floor an acceptance uses (AC-02).

    The Supervisor override when set, else the proposed ``minimum_n``.
    Pure and deterministic.

    Raises:
        TypeError: ``proposal`` is not a ``ReplicateDesignProposal``.
    """
    if not isinstance(proposal, ReplicateDesignProposal):
        raise TypeError(
            "proposal must be a ReplicateDesignProposal, got"
            f" {type(proposal).__name__}"
        )
    if proposal.override_n is not None:
        return proposal.override_n
    return proposal.minimum_n


def set_replicate_override(
    proposal: ReplicateDesignProposal, override_n: int
) -> ReplicateDesignProposal:
    """Set the Supervisor override of a replicate-design proposal (AC-02).

    Pure: returns a copy of the proposal with ``override_n`` set and
    ``is_default`` False (an overridden proposal is no longer the default
    proposal; the default floor itself stays recorded for the audit
    trail). The input proposal is never mutated. The override can never
    weaken the floor below 1.

    Raises:
        TypeError: ``proposal`` is not a ``ReplicateDesignProposal``, or
            ``override_n`` is not an int.
        InvalidReplicateProposalError: ``override_n`` is below 1.
    """
    if not isinstance(proposal, ReplicateDesignProposal):
        raise TypeError(
            "proposal must be a ReplicateDesignProposal, got"
            f" {type(proposal).__name__}"
        )
    if not isinstance(override_n, int) or isinstance(override_n, bool):
        raise TypeError(
            f"override_n must be an int, got {type(override_n).__name__}"
        )
    if override_n < 1:
        raise InvalidReplicateProposalError(
            "an override can never weaken the replicate floor below 1, got"
            f" {override_n}"
        )
    return replace(proposal, override_n=override_n, is_default=False)


def propose_measurement_uncertainty(
    *,
    measurement_key: str,
    uncertainty_kind: UncertaintyKind,
    reporting_form: str,
    rationale: str,
    variance: float | None = None,
    notes: str | None = None,
) -> MeasurementUncertaintyProposal:
    """Propose measurement uncertainty metadata of one measurement/run.

    A proposal record (objective: proposal helpers for measurement
    uncertainty metadata, with rationale, never hard-coded values): the
    uncertainty kind, the variance estimate and the reporting form are
    EXPLICIT arguments -- the helper never fabricates a default variance,
    a default kind or a default reporting form. ``variance`` is ``None``
    when no estimate exists yet (an honest planning state).

    Raises:
        TypeError: a field has the wrong type.
        InvalidUncertaintyProposalError: a value violation (empty
            measurement key or rationale, non-finite or negative
            variance).
    """
    if not isinstance(measurement_key, str):
        raise TypeError(
            "measurement_key must be a str, got"
            f" {type(measurement_key).__name__}"
        )
    return MeasurementUncertaintyProposal(
        proposal_id=generate_id("proposal", "uncertainty", measurement_key),
        measurement_key=measurement_key,
        uncertainty_kind=uncertainty_kind,
        reporting_form=reporting_form,
        rationale=rationale,
        variance=variance,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Acceptance proposal (AC-01: never a universal fixed percent margin)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceProposal:
    """The proposed acceptance criteria of a goal (AC-01/AC-02/AC-03).

    A proposal record constructed by :func:`construct_acceptance_proposal`
    or :func:`default_acceptance_proposal` from the replicate-design
    proposal, the measurement uncertainty metadata and the
    literature/method evidence references. The acceptance is proposed
    UNFROZEN (``frozen`` False by construction; the only sanctioned ways
    to produce a frozen acceptance are the evidence-before-freezing flow
    and :func:`freeze_acceptance_proposal`, a Supervisor-only decision
    gated by the frozen role-action matrix).

    AC-01: ``equivalence_margin`` is instance data -- a numeric tolerance
    can ONLY enter through explicit evidence-grounded arguments (an
    attached evidence reference supporting ``EvidenceClaim.EQUIVALENCE_MARGIN``);
    there is no universal fixed percent margin anywhere and the default
    proposal records no numeric tolerance at all. ``replicate_floor`` is
    the effective replicate floor of the recorded replicate-design
    proposal (the Supervisor override when set, else the proposed floor),
    derived at construction and pinned on the record.

    AC-03: ``evidence_refs`` carries the literature/method evidence
    references attached BEFORE freezing; a frozen acceptance carries
    exactly the evidence that justified it, and freezing is refused when
    the recorded tolerance lacks the evidence the flow requires.

    Raises:
        TypeError: a field has the wrong type.
        InvalidAcceptanceProposalError: a value violation (unsafe
            proposal/goal id, duplicate evidence references, non-finite
            or non-positive margin).
        FrozenAcceptanceError: never raised here -- frozen-state
            mutations are refused by :func:`attach_evidence` and the
            freeze flow, not by construction.
    """

    proposal_id: str
    goal_id: str
    replicate_design: ReplicateDesignProposal
    decision_mode: DecisionMode
    evidence_refs: tuple[EvidenceReference, ...] = ()
    uncertainty: MeasurementUncertaintyProposal | None = None
    equivalence_margin: float | None = None
    rationale: str | None = None
    notes: str | None = None
    frozen: bool = False
    #: The effective replicate floor of the recorded replicate-design
    #: proposal, derived at construction (never passed by callers; the
    #: placeholder default is always overwritten by ``__post_init__``).
    replicate_floor: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        _validate_safe_segment(
            type(self).__name__,
            "proposal_id",
            self.proposal_id,
            InvalidAcceptanceProposalError,
        )
        _validate_safe_segment(
            type(self).__name__,
            "goal_id",
            self.goal_id,
            InvalidAcceptanceProposalError,
        )
        if not isinstance(self.replicate_design, ReplicateDesignProposal):
            raise TypeError(
                "AcceptanceProposal.replicate_design must be a"
                " ReplicateDesignProposal, got"
                f" {type(self.replicate_design).__name__}"
            )
        if not isinstance(self.decision_mode, DecisionMode):
            raise TypeError(
                "AcceptanceProposal.decision_mode must be a DecisionMode"
                f" member, got {type(self.decision_mode).__name__}"
            )
        if not isinstance(self.evidence_refs, tuple) or not all(
            isinstance(reference, EvidenceReference)
            for reference in self.evidence_refs
        ):
            raise TypeError(
                "AcceptanceProposal.evidence_refs must be a tuple of"
                " EvidenceReference records"
            )
        _reject_duplicate_evidence(self.evidence_refs)
        if self.uncertainty is not None and not isinstance(
            self.uncertainty, MeasurementUncertaintyProposal
        ):
            raise TypeError(
                "AcceptanceProposal.uncertainty must be a"
                " MeasurementUncertaintyProposal or None, got"
                f" {type(self.uncertainty).__name__}"
            )
        margin = self.equivalence_margin
        if margin is not None:
            if isinstance(margin, bool) or not isinstance(margin, (int, float)):
                raise TypeError(
                    "AcceptanceProposal.equivalence_margin must be a number"
                    f" or None, got {type(margin).__name__}"
                )
            if not math.isfinite(margin) or margin <= 0:
                raise InvalidAcceptanceProposalError(
                    "AcceptanceProposal.equivalence_margin must be a finite"
                    f" positive number when recorded, got {margin!r}"
                )
        if self.rationale is not None and not isinstance(self.rationale, str):
            raise TypeError(
                "AcceptanceProposal.rationale must be a str or None, got"
                f" {type(self.rationale).__name__}"
            )
        if self.notes is not None and not isinstance(self.notes, str):
            raise TypeError(
                "AcceptanceProposal.notes must be a str or None, got"
                f" {type(self.notes).__name__}"
            )
        if not isinstance(self.frozen, bool):
            raise TypeError(
                "AcceptanceProposal.frozen must be a bool, got"
                f" {type(self.frozen).__name__}"
            )
        # Defensive copy: the frozen proposal owns its evidence table.
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        # The replicate floor is derived at construction from the recorded
        # replicate-design proposal (the override wins over the proposal).
        object.__setattr__(
            self, "replicate_floor", effective_replicate_floor(self.replicate_design)
        )

    def as_dict(self) -> dict[str, Any]:
        """Deterministic plain-dict view (proposal-capture shape)."""
        return {
            "proposal_id": self.proposal_id,
            "goal_id": self.goal_id,
            "replicate_floor": self.replicate_floor,
            "replicate_design": self.replicate_design.as_dict(),
            "decision_mode": self.decision_mode.value,
            "equivalence_margin": self.equivalence_margin,
            "evidence_refs": [reference.as_dict() for reference in self.evidence_refs],
            "uncertainty": None if self.uncertainty is None else self.uncertainty.as_dict(),
            "rationale": self.rationale,
            "notes": self.notes,
            "frozen": self.frozen,
        }


def _reject_duplicate_evidence(
    references: tuple[EvidenceReference, ...],
) -> None:
    """Reject duplicate evidence references with a stable error."""
    evidence_ids = [reference.evidence_id for reference in references]
    duplicates = sorted(
        {
            evidence_id
            for evidence_id in evidence_ids
            if evidence_ids.count(evidence_id) > 1
        }
    )
    if duplicates:
        raise InvalidAcceptanceProposalError(
            "duplicate evidence reference(s):"
            f" {', '.join(duplicates)}"
        )


def _has_margin_evidence(proposal: AcceptanceProposal) -> bool:
    """True iff the proposal carries an evidence reference supporting its
    recorded numeric tolerance (07-STATISTICS-AND-ACCEPTANCE.md SS8)."""
    return any(
        reference.claim is EvidenceClaim.EQUIVALENCE_MARGIN
        for reference in proposal.evidence_refs
    )


def _require_evidence_sequence(
    values: Sequence[EvidenceReference], name: str
) -> None:
    """Reject non-EvidenceReference entries of a ref sequence."""
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError(
            f"{name} must be a sequence of EvidenceReference records, got"
            f" {type(values).__name__}"
        )
    for value in values:
        if not isinstance(value, EvidenceReference):
            raise TypeError(
                f"{name} entries must be EvidenceReference records, got"
                f" {type(value).__name__}"
            )


def construct_acceptance_proposal(
    *,
    goal_id: str,
    replicate_design: ReplicateDesignProposal,
    uncertainty: MeasurementUncertaintyProposal | None = None,
    decision_mode: DecisionMode = DecisionMode.EQUIVALENCE,
    equivalence_margin: float | None = None,
    evidence_refs: Sequence[EvidenceReference] = (),
    rationale: str | None = None,
    notes: str | None = None,
) -> AcceptanceProposal:
    """Construct an acceptance proposal for a goal (AC-01/AC-02/AC-03).

    The acceptance is constructed from the replicate-design proposal
    (the proposed floor -- Supervisor override wins), the measurement
    uncertainty metadata and the literature/method evidence references.
    The proposal is UNFROZEN: the Supervisor may attach further evidence
    before freezing (:func:`attach_evidence`) and freeze the acceptance
    (:func:`freeze_acceptance_proposal`).

    AC-01: a numeric ``equivalence_margin`` is NEVER a universal default.
    If a tolerance is requested it must come from explicit
    evidence-grounded arguments: at least one attached evidence reference
    must support ``EvidenceClaim.EQUIVALENCE_MARGIN``, else construction
    is refused with :class:`InvalidAcceptanceProposalError`. The
    no-margin path (the default) never fabricates a fixed percent margin.

    Raises:
        TypeError: a field has the wrong type (goal id not a str, a
            non-proposal replicate design, a non-proposal uncertainty
            record, a non-``DecisionMode`` mode, a non-number margin, a
            ref sequence with non-reference entries).
        InvalidAcceptanceProposalError: a value violation (unsafe goal
            id, duplicate evidence references, a non-positive or
            non-finite margin, a margin without margin evidence).
    """
    if not isinstance(goal_id, str):
        raise TypeError(f"goal_id must be a str, got {type(goal_id).__name__}")
    if not isinstance(replicate_design, ReplicateDesignProposal):
        raise TypeError(
            "replicate_design must be a ReplicateDesignProposal, got"
            f" {type(replicate_design).__name__}"
        )
    if uncertainty is not None and not isinstance(
        uncertainty, MeasurementUncertaintyProposal
    ):
        raise TypeError(
            "uncertainty must be a MeasurementUncertaintyProposal or None,"
            f" got {type(uncertainty).__name__}"
        )
    if not isinstance(decision_mode, DecisionMode):
        raise TypeError(
            "decision_mode must be a DecisionMode member, got"
            f" {type(decision_mode).__name__}"
        )
    if equivalence_margin is not None:
        if isinstance(equivalence_margin, bool) or not isinstance(
            equivalence_margin, (int, float)
        ):
            raise TypeError(
                "equivalence_margin must be a number or None, got"
                f" {type(equivalence_margin).__name__}"
            )
        if not math.isfinite(equivalence_margin) or equivalence_margin <= 0:
            raise InvalidAcceptanceProposalError(
                "equivalence_margin must be a finite positive number when"
                f" recorded, got {equivalence_margin!r}"
            )
    if rationale is not None and not isinstance(rationale, str):
        raise TypeError(
            f"rationale must be a str or None, got {type(rationale).__name__}"
        )
    if notes is not None and not isinstance(notes, str):
        raise TypeError(f"notes must be a str or None, got {type(notes).__name__}")
    _require_evidence_sequence(evidence_refs, "evidence_refs")
    refs = tuple(evidence_refs)
    _reject_duplicate_evidence(refs)
    if equivalence_margin is not None and not any(
        reference.claim is EvidenceClaim.EQUIVALENCE_MARGIN
        for reference in refs
    ):
        raise InvalidAcceptanceProposalError(
            "a numeric acceptance tolerance must come from explicit"
            " evidence-grounded arguments (07-STATISTICS-AND-ACCEPTANCE.md"
            " SS8: every numeric margin records its basis): attach at least"
            " one evidence reference whose claim is"
            f" {EvidenceClaim.EQUIVALENCE_MARGIN.value!r} to ground the"
            f" recorded equivalence_margin {equivalence_margin!r}"
        )
    return AcceptanceProposal(
        proposal_id=generate_id("proposal", "acceptance", goal_id),
        goal_id=goal_id,
        replicate_design=replicate_design,
        decision_mode=decision_mode,
        evidence_refs=refs,
        uncertainty=uncertainty,
        equivalence_margin=equivalence_margin,
        rationale=rationale,
        notes=notes,
    )


def default_acceptance_proposal(goal_id: str) -> AcceptanceProposal:
    """The default acceptance proposal of a goal (AC-01/AC-02).

    The no-argument default path: the acceptance is constructed from the
    default replicate-design proposal (the proposed ``n >= 3`` floor) and
    carries NO numeric tolerance at all -- ``equivalence_margin`` is
    ``None`` and no universal fixed percent margin exists anywhere. The
    proposal is unfrozen and records no fabricated evidence: the
    Supervisor enriches it with uncertainty metadata and literature/method
    evidence before freezing (AC-03).

    Raises:
        TypeError: ``goal_id`` is not a str.
        InvalidReplicateProposalError / InvalidAcceptanceProposalError:
            ``goal_id`` is not a safe segment.
    """
    return construct_acceptance_proposal(
        goal_id=goal_id,
        replicate_design=default_replicate_design_proposal(goal_id),
        rationale=(
            "default acceptance proposal: no numeric tolerance is proposed"
            " (07-STATISTICS-AND-ACCEPTANCE.md SS8: no universal fixed"
            " percent margin is allowed); acceptance rests on the proposed"
            " replicate design (SS2) and may be enriched with measurement"
            " uncertainty metadata and literature/method evidence before"
            " freezing (SS9, AC-03)."
        ),
    )


def attach_evidence(
    proposal: AcceptanceProposal,
    evidence_refs: Sequence[EvidenceReference],
) -> AcceptanceProposal:
    """Attach literature/method evidence to an acceptance (AC-03).

    Evidence is attached BEFORE the acceptance is frozen: the pure
    function returns a copy of the proposal whose ``evidence_refs``
    extend the recorded references (duplicates refused), and the input
    proposal is never mutated. A frozen acceptance refuses further
    evidence -- a frozen acceptance carries exactly the evidence that
    justified it, so evidence must be incorporated before freezing.

    Raises:
        TypeError: ``proposal`` is not an ``AcceptanceProposal``, or
            ``evidence_refs`` is not a sequence of ``EvidenceReference``
            records.
        InvalidAcceptanceProposalError: duplicate evidence references.
        FrozenAcceptanceError: the proposal is already frozen (evidence
            must be attached before freezing).
    """
    if not isinstance(proposal, AcceptanceProposal):
        raise TypeError(
            "proposal must be an AcceptanceProposal, got"
            f" {type(proposal).__name__}"
        )
    if proposal.frozen:
        raise FrozenAcceptanceError(
            f"evidence must be attached before the acceptance"
            f" {proposal.proposal_id!r} is frozen: a frozen acceptance"
            " carries exactly the evidence that justified it and can no"
            " longer be changed",
            assess_freeze_eligibility(proposal),
        )
    _require_evidence_sequence(evidence_refs, "evidence_refs")
    combined = proposal.evidence_refs + tuple(evidence_refs)
    _reject_duplicate_evidence(combined)
    return replace(proposal, evidence_refs=combined)


# ---------------------------------------------------------------------------
# Freeze eligibility (AC-03: the frozen contract decides, first match wins)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FreezeEligibilityRule:
    """One entry of the ordered freeze-eligibility rule table.

    Each rule declares whether an acceptance matching its predicate is
    eligible to be frozen; the first matching rule decides, the trailing
    total default always matches.
    """

    rule_id: str
    description: str
    eligible: bool
    predicate: Callable[[AcceptanceProposal], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"FreezeEligibilityRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise StatisticsProposalError(
                    f"FreezeEligibilityRule.{field_name} must be a"
                    f" non-empty string, got {value!r}"
                )
        if not isinstance(self.eligible, bool):
            raise TypeError(
                "FreezeEligibilityRule.eligible must be a bool, got"
                f" {type(self.eligible).__name__}"
            )
        if not callable(self.predicate):
            raise TypeError(
                "FreezeEligibilityRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class FreezeEligibilityDecision:
    """Record of one freeze-eligibility rule evaluation."""

    rule_id: str
    description: str
    eligible: bool
    matched: bool


@dataclass(frozen=True)
class FreezeEligibilityAssessment:
    """Full, auditable result of a freeze-eligibility evaluation.

    ``eligible`` is decided by the ordered ``FREEZE_ELIGIBILITY_RULES``
    table (first match wins; the trailing total default always matches);
    ``matched_rule_id`` names the deciding rule (never ``None``);
    ``missing_evidence_claims`` explicitly lists the evidence claims the
    flow requires for the proposal's recorded configuration but no
    attached evidence supports (``07-STATISTICS-AND-ACCEPTANCE.md`` SS8:
    every numeric margin records its basis).
    """

    proposal_id: str
    eligible: bool
    matched_rule_id: str
    decisions: tuple[FreezeEligibilityDecision, ...]
    missing_evidence_claims: tuple[EvidenceClaim, ...]
    ruleset_version: str = STATISTICS_RULESET_VERSION


#: The ordered freeze-eligibility rule table (AC-03: evidence before
#: freezing; 07-STATISTICS-AND-ACCEPTANCE.md SS8/SS9; first match wins,
#: trailing total default). ``R-STAT-F1`` refuses the freeze of an
#: acceptance that records a numeric tolerance without the evidence that
#: grounds it; the trailing default ``R-STAT-F0`` allows freezing every
#: other acceptance (an acceptance with no numeric tolerance needs no
#: margin evidence -- the no-tolerance path never fabricates a margin).
FREEZE_ELIGIBILITY_RULES: tuple[FreezeEligibilityRule, ...] = (
    FreezeEligibilityRule(
        rule_id="R-STAT-F1",
        description=(
            "the acceptance records a numeric equivalence margin but no"
            " attached evidence reference supports that margin: freezing"
            " is refused until the margin evidence is attached"
        ),
        eligible=False,
        predicate=lambda proposal: (
            proposal.equivalence_margin is not None
            and not _has_margin_evidence(proposal)
        ),
    ),
    FreezeEligibilityRule(
        rule_id="R-STAT-F0",
        description=(
            "the acceptance records no numeric tolerance, or its recorded"
            " tolerance carries the evidence that grounds it: the"
            " acceptance is eligible to freeze (total default)"
        ),
        eligible=True,
        predicate=lambda proposal: True,
    ),
)


def _required_evidence_claims(proposal: AcceptanceProposal) -> tuple[EvidenceClaim, ...]:
    """The evidence claims the flow requires for the recorded configuration.

    An acceptance recording a numeric equivalence margin requires the
    evidence that grounds it (07-SS8); an acceptance with no numeric
    tolerance requires no margin evidence (AC-01: no default margin).
    """
    if proposal.equivalence_margin is None:
        return ()
    return (EvidenceClaim.EQUIVALENCE_MARGIN,)


def assess_freeze_eligibility(
    proposal: AcceptanceProposal,
) -> FreezeEligibilityAssessment:
    """Evaluate whether an acceptance may be frozen (AC-03).

    Pure and deterministic: the ordered ``FREEZE_ELIGIBILITY_RULES``
    table decides (first match wins; the trailing total default always
    matches). The assessment records every rule decision, the deciding
    rule id, and the missing evidence claims (the claims the flow
    requires for the proposal's recorded configuration but no attached
    evidence supports).

    Raises:
        TypeError: ``proposal`` is not an ``AcceptanceProposal``.
    """
    if not isinstance(proposal, AcceptanceProposal):
        raise TypeError(
            "proposal must be an AcceptanceProposal, got"
            f" {type(proposal).__name__}"
        )
    decisions: list[FreezeEligibilityDecision] = []
    matched_rule_id: str | None = None
    matched_eligible = False  # unreachable default
    for rule in FREEZE_ELIGIBILITY_RULES:
        matched = rule.predicate(proposal)
        decisions.append(
            FreezeEligibilityDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                eligible=rule.eligible,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_eligible = rule.eligible
    # The trailing total default always matches, so this can never be None.
    assert matched_rule_id is not None
    required = _required_evidence_claims(proposal)
    present = {reference.claim for reference in proposal.evidence_refs}
    missing = tuple(claim for claim in required if claim not in present)
    return FreezeEligibilityAssessment(
        proposal_id=proposal.proposal_id,
        eligible=matched_eligible,
        matched_rule_id=matched_rule_id,
        decisions=tuple(decisions),
        missing_evidence_claims=missing,
    )


def _check_acceptance_freeze_permission(role: Role, proposal: AcceptanceProposal) -> None:
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
            f"role {role.value!r} may not freeze the acceptance proposal"
            f" {proposal.proposal_id!r}: freezing is a Supervisor-only"
            " decision (the plan-freeze action of the frozen role-action"
            " matrix)",
            assessment,
        )


def freeze_acceptance_proposal(
    proposal: AcceptanceProposal, *, role: Role
) -> AcceptanceProposal:
    """Freeze an acceptance proposal -- a Supervisor-only decision (AC-03).

    Freezes the acceptance only after the ordered
    ``FREEZE_ELIGIBILITY_RULES`` table confirms the evidence the flow
    requires is attached: an acceptance recording a numeric tolerance
    whose margin evidence is missing is REFUSED with
    :class:`FrozenAcceptanceError` (the error and its assessment
    explicitly state the missing evidence claims). The pure function is
    gated by the frozen role-action matrix (``Action.PLAN_FREEZE``,
    granted only to the Supervisor by ``R-PRM-SUP1``); the input proposal
    is never mutated and the frozen copy carries exactly the evidence
    that justified it (AC-03).

    Raises:
        TypeError: ``proposal`` is not an ``AcceptanceProposal``, or
            ``role`` is not a ``Role`` member.
        PermissionDeniedError: the role may not freeze (carries the full
            permission assessment for the audit trail).
        FrozenAcceptanceError: the flow requires evidence the proposal
            does not carry (the error and its assessment state the
            missing evidence claims).
    """
    if not isinstance(proposal, AcceptanceProposal):
        raise TypeError(
            "proposal must be an AcceptanceProposal, got"
            f" {type(proposal).__name__}"
        )
    _check_acceptance_freeze_permission(role, proposal)
    assessment = assess_freeze_eligibility(proposal)
    if not assessment.eligible:
        missing = ", ".join(
            claim.value for claim in assessment.missing_evidence_claims
        )
        raise FrozenAcceptanceError(
            f"acceptance proposal {proposal.proposal_id!r} records a"
            " numeric tolerance without the evidence the flow requires:"
            f" freezing is refused until the evidence supporting"
            f" claim(s) {missing} is attached before freezing"
            f" (deciding rule {assessment.matched_rule_id!r})",
            assessment,
        )
    return replace(proposal, frozen=True)


# ---------------------------------------------------------------------------
# Ruleset integrity (the house validation discipline)
# ---------------------------------------------------------------------------


def validate_statistics_rulesets() -> tuple[str, ...]:
    """Validate the statistics rule tables' integrity; return the ids.

    The freeze-eligibility table is non-empty, has unique rule ids, and
    its trailing rule is a total default (matches every acceptance
    proposal, including margin-bearing and margin-less ones).

    Raises:
        StatisticsProposalError: a table violates the frozen shape
            (stable messages).
    """
    ids = tuple(rule.rule_id for rule in FREEZE_ELIGIBILITY_RULES)
    duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicates:
        raise StatisticsProposalError(
            "duplicate rule id(s) in the freeze-eligibility rule table:"
            f" {', '.join(duplicates)}"
        )
    if not ids:
        raise StatisticsProposalError(
            "the freeze-eligibility rule table must not be empty"
        )
    sample_proposals = (
        default_acceptance_proposal("goal-sample-no-margin"),
        AcceptanceProposal(
            proposal_id=generate_id(
                "proposal", "acceptance", "goal-sample-with-margin"
            ),
            goal_id="goal-sample-with-margin",
            replicate_design=default_replicate_design_proposal(
                "goal-sample-with-margin"
            ),
            decision_mode=DecisionMode.EQUIVALENCE,
            equivalence_margin=5.0,
            evidence_refs=(
                _evidence_reference(
                    "method-source-sample", EvidenceClaim.EQUIVALENCE_MARGIN
                ),
            ),
        ),
        AcceptanceProposal(
            proposal_id=generate_id(
                "proposal", "acceptance", "goal-sample-missing-evidence"
            ),
            goal_id="goal-sample-missing-evidence",
            replicate_design=default_replicate_design_proposal(
                "goal-sample-missing-evidence"
            ),
            decision_mode=DecisionMode.EQUIVALENCE,
            equivalence_margin=5.0,
        ),
    )
    trailing_rule = FREEZE_ELIGIBILITY_RULES[-1]
    if not all(trailing_rule.predicate(proposal) for proposal in sample_proposals):
        raise StatisticsProposalError(
            f"the trailing freeze-eligibility rule {trailing_rule.rule_id!r}"
            " must be a total default"
        )
    return tuple(ids)


# ---------------------------------------------------------------------------
# Shared validation helpers (deterministic, stable errors)
# ---------------------------------------------------------------------------


def _validate_safe_segment(
    class_name: str,
    field_name: str,
    value: str,
    error_type: type[StatisticsProposalError],
) -> None:
    """Reject ids that escape registries or break glob listings.

    Safe single registry path segment (FND-M9-G02-01 lesson): no path
    separators, no glob metacharacters, not empty, not ``.``/``..``.
    Value violations raise ``error_type`` (the record's stable error
    class); type violations raise ``TypeError``.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"{class_name}.{field_name} must be a str, got"
            f" {type(value).__name__}"
        )
    if not value.strip() or value in (".", ".."):
        raise error_type(
            f"{class_name}.{field_name} must be a non-empty safe registry"
            f" id, got {value!r}"
        )
    if "/" in value or "\\" in value:
        raise error_type(
            f"{class_name}.{field_name} must be a safe single path segment"
            f" (no '/', no '\\'), got {value!r}"
        )
    if any(char.isspace() for char in value):
        raise error_type(
            f"{class_name}.{field_name} must not contain whitespace, got"
            f" {value!r}"
        )
    if any(char in value for char in "*?[]"):
        raise error_type(
            f"{class_name}.{field_name} must not contain glob"
            f" metacharacters, got {value!r}"
        )
