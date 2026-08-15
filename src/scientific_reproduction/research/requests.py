"""Research Request lifecycle: Supervisor-only issuance, transition rules
and auditable result linkage (DEV-M5-G02).

Implements the **Research Request lifecycle** deliverable over the frozen
``ResearchRequest`` model (``core/models.py``,
``schemas/research-request.schema.yaml``). The frozen spec grounds this
module:

* ``09-RESEARCH-SUBSYSTEM.md`` section 3 ("Research Requests"): *Only
  Supervisor may issue formal Research Requests. Workers report anomalies
  to Supervisor; Supervisor decides whether a research question is
  warranted*, and the request should carry the request ID, the
  originating Goal/decision, the scientific question, required search
  families and minimum directness/reliability.
* ``agent-contracts/RESEARCH.md``: during execution the Research Agent
  must *respond only to formal Supervisor Research Requests*.
* ``schemas/research-request.schema.yaml``: the frozen request vocabulary
  -- ``requested_by`` is the constant ``"supervisor"`` and ``status`` is
  one of ``OPEN`` / ``SEARCHING`` / ``COMPLETE`` / ``EXHAUSTED``.
* Frozen acceptance: *Research Request objects* are a milestone
  deliverable of the research subsystem.

Frozen request vocabulary (normative reading)
---------------------------------------------
The frozen schema is authoritative for the lifecycle states; this module
does not invent states. The four frozen statuses are the lifecycle:
issuance formally issues a request **in ``OPEN``**; a Research Agent
moves it to ``SEARCHING`` while working; ``COMPLETE`` and ``EXHAUSTED``
are the two terminal outcomes (finished with findings / search space
exhausted without sufficient findings). The prompt-level labels
ISSUED / IN_PROGRESS / RESOLVED are realized as OPEN / SEARCHING /
COMPLETE respectively, and the frozen ``EXHAUSTED`` terminal has no
counterpart in the generic vocabulary -- the frozen schema wins.

Supervisor-only issuance (AC-02)
--------------------------------
:func:`issue_research_request` is the **sole** entry point that creates a
formally issued request. It is the Supervisor-facing API (09 section 3);
it hard-codes the schema's ``requested_by`` constant ``"supervisor"`` and
the issued state ``OPEN``, and it is the only function that can pass the
module-private construction token :data:`_ISSUANCE_TOKEN`. The issued
record :class:`IssuedResearchRequest` is a frozen dataclass whose
constructor is gated by that token: any direct construction outside
:func:`issue_research_request` raises (no public constructor bypasses
issuance). Workers and research agents have no path that creates a
formally issued request, and no path exists to record any issuer other
than the supervisor.

Lifecycle transitions (ordered rule table)
------------------------------------------
Transitions are validated by the versioned, ordered rule table
:data:`REQUEST_TRANSITION_RULES` in the rule-paradigm style of
``core/rules/`` and ``research/dedupe.py`` (first match wins, every rule
evaluation recorded in an auditable assessment, ``TypeError`` at the
public boundaries, pure deterministic predicates, a total default):

1. ``R-REQ-S0``  ``OPEN -> SEARCHING`` (a Research Agent starts
   working)                                                    -> LEGAL
2. ``R-REQ-C1``  ``SEARCHING -> COMPLETE`` (search finished with
   findings)                                                   -> LEGAL
3. ``R-REQ-E1``  ``SEARCHING -> EXHAUSTED`` (search space exhausted
   without sufficient findings)                                -> LEGAL
4. ``R-REQ-D1``  any other pair (default; includes all no-op
   transitions and anything leaving a terminal state)          -> ILLEGAL

``COMPLETE`` and ``EXHAUSTED`` are terminal: no outgoing transitions.
No-op transitions are never legal (a transition records a change, so a
no-op must not enter the audit record).

Auditable request/result linkage (AC-03)
----------------------------------------
When a result (evidence) is attached to a request, :func:`attach_
result_to_request` validates it against the versioned, ordered linkage
rule table :data:`RESULT_LINKAGE_RULES` and returns the updated request
plus one :class:`ResultLinkageRecord`. The linkage record carries the
request id, the evidence id, the (injected, deterministic) linkage
timestamp, the linkage actor and the request status at linkage time, so
the full audit trail is reconstructible from the records alone.
Linkage rules:

1. ``R-LINK-S1`` results may only be linked while the request is
   ``SEARCHING`` (before ``SEARCHING`` nothing has been found; the
   terminal states are closed);
2. ``R-LINK-D1`` an evidence id may only be linked once (no duplicate
   attachment).

The frozen ``ResearchRequest`` model carries no timestamp field, so the
linkage (and issuance) timestamps live on the audit records; they are
injected by callers and never read from a clock (determinism).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Literal, Self, Sequence

from scientific_reproduction.core.models import ResearchRequest, ResearchRequestStatus
from scientific_reproduction.research.workflows import BootstrapCategory

__all__ = [
    "REQUEST_LIFECYCLE_RULESET_VERSION",
    "RESULT_LINKAGE_RULESET_VERSION",
    "ResearchRequestError",
    "RequestIssuanceError",
    "IllegalRequestTransitionError",
    "RequestLinkageError",
    "TransitionVerdict",
    "RequestTransitionRule",
    "REQUEST_TRANSITION_RULES",
    "RequestTransitionDecision",
    "RequestTransitionAssessment",
    "evaluate_request_transition",
    "is_legal_request_transition",
    "apply_request_transition",
    "LinkageVerdict",
    "ResultLinkageRule",
    "RESULT_LINKAGE_RULES",
    "ResultLinkageRuleDecision",
    "ResultLinkageAssessment",
    "evaluate_result_linkage",
    "ResultLinkageRecord",
    "attach_result_to_request",
    "IssuedResearchRequest",
    "issue_research_request",
]

#: Version of the request lifecycle rule table. Bumped whenever a rule
#: changes; recorded in every assessment so old decisions stay
#: interpretable (auditability).
REQUEST_LIFECYCLE_RULESET_VERSION: str = "1.0"

#: Version of the result-linkage rule table (same convention).
RESULT_LINKAGE_RULESET_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Errors -- stable messages, ValueError subclasses (rule-paradigm)
# ---------------------------------------------------------------------------


class ResearchRequestError(ValueError):
    """Base class for all research-request domain errors."""


class RequestIssuanceError(ResearchRequestError):
    """Raised when a request cannot be formally issued (invalid content).

    Stable messages: every message names the offending field and the
    reason, so callers and tests can rely on them.
    """


class IllegalRequestTransitionError(ResearchRequestError):
    """Raised when a requested status transition is not in the rule table."""

    def __init__(
        self, from_status: ResearchRequestStatus, to_status: ResearchRequestStatus
    ) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"illegal research-request transition: {from_status.value!r} -> "
            f"{to_status.value!r} is not in the normative rule table "
            "(see scientific_reproduction.research.requests)"
        )


class RequestLinkageError(ResearchRequestError):
    """Raised when a result cannot be linked to a request (rule rejection)."""


# ---------------------------------------------------------------------------
# Lifecycle transition rules (ordered table with a total default)
# ---------------------------------------------------------------------------


class TransitionVerdict(StrEnum):
    """Pair-level verdict of the request lifecycle rules."""

    LEGAL = "LEGAL"
    ILLEGAL = "ILLEGAL"


@dataclass(frozen=True)
class RequestTransitionRule:
    """One entry of the ordered request lifecycle rule table."""

    rule_id: str
    description: str
    verdict: TransitionVerdict
    predicate: Callable[[ResearchRequestStatus, ResearchRequestStatus], bool]


@dataclass(frozen=True)
class RequestTransitionDecision:
    """Record of one rule evaluation for a given status pair (auditability)."""

    rule_id: str
    description: str
    verdict: TransitionVerdict
    matched: bool


#: The ordered lifecycle rule table. First match wins; order is normative
#: (see the module docstring). The trailing default rule matches every
#: pair, so the table is total: every status pair gets exactly one verdict.
REQUEST_TRANSITION_RULES: tuple[RequestTransitionRule, ...] = (
    RequestTransitionRule(
        rule_id="R-REQ-S0",
        description=(
            "OPEN -> SEARCHING: a Research Agent starts working the "
            "formally issued request"
        ),
        verdict=TransitionVerdict.LEGAL,
        predicate=lambda old, new: (
            old is ResearchRequestStatus.OPEN
            and new is ResearchRequestStatus.SEARCHING
        ),
    ),
    RequestTransitionRule(
        rule_id="R-REQ-C1",
        description=(
            "SEARCHING -> COMPLETE: the search finished with findings "
            "linked to the request"
        ),
        verdict=TransitionVerdict.LEGAL,
        predicate=lambda old, new: (
            old is ResearchRequestStatus.SEARCHING
            and new is ResearchRequestStatus.COMPLETE
        ),
    ),
    RequestTransitionRule(
        rule_id="R-REQ-E1",
        description=(
            "SEARCHING -> EXHAUSTED: the search space was exhausted "
            "without sufficient findings"
        ),
        verdict=TransitionVerdict.LEGAL,
        predicate=lambda old, new: (
            old is ResearchRequestStatus.SEARCHING
            and new is ResearchRequestStatus.EXHAUSTED
        ),
    ),
    RequestTransitionRule(
        rule_id="R-REQ-D1",
        description=(
            "any other pair is an illegal transition (default): no-op "
            "transitions and anything leaving a terminal state"
        ),
        verdict=TransitionVerdict.ILLEGAL,
        predicate=lambda old, new: True,
    ),
)


@dataclass(frozen=True)
class RequestTransitionAssessment:
    """Full, auditable verdict for one status pair (rule trace)."""

    from_status: ResearchRequestStatus
    to_status: ResearchRequestStatus
    verdict: TransitionVerdict
    matched_rule_id: str
    decisions: tuple[RequestTransitionDecision, ...]


def evaluate_request_transition(
    old: ResearchRequestStatus, new: ResearchRequestStatus
) -> RequestTransitionAssessment:
    """Evaluate the lifecycle rule table over one status pair.

    Pure and deterministic: the verdict is a pure function of the two
    statuses. The returned assessment records the pair, the verdict, the
    matched rule and every rule evaluation.

    Raises:
        TypeError: ``old`` or ``new`` is not a ``ResearchRequestStatus``.
    """
    if not isinstance(old, ResearchRequestStatus):
        raise TypeError(
            "evaluate_request_transition expects a ResearchRequestStatus, got"
            f" {type(old).__name__}"
        )
    if not isinstance(new, ResearchRequestStatus):
        raise TypeError(
            "evaluate_request_transition expects a ResearchRequestStatus, got"
            f" {type(new).__name__}"
        )
    decisions: list[RequestTransitionDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = TransitionVerdict.ILLEGAL  # unreachable default
    for rule in REQUEST_TRANSITION_RULES:
        matched = rule.predicate(old, new)
        decisions.append(
            RequestTransitionDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
    # R-REQ-D1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return RequestTransitionAssessment(
        from_status=old,
        to_status=new,
        verdict=matched_verdict,
        matched_rule_id=matched_rule_id,
        decisions=tuple(decisions),
    )


def is_legal_request_transition(
    old: ResearchRequestStatus, new: ResearchRequestStatus
) -> bool:
    """Return whether ``old -> new`` is a normative lifecycle transition."""
    return evaluate_request_transition(old, new).verdict is TransitionVerdict.LEGAL


def apply_request_transition(
    request: ResearchRequest, to_status: ResearchRequestStatus
) -> ResearchRequest:
    """Return a new request advanced to ``to_status`` if the rule table
    allows the transition, else raise.

    The input request is never mutated (frozen model): a fresh
    ``ResearchRequest`` with the new status is returned.

    Raises:
        TypeError: ``request`` is not a ``ResearchRequest``, or
            ``to_status`` is not a ``ResearchRequestStatus``.
        IllegalRequestTransitionError: the pair is not in the rule table
            (including no-op transitions).
    """
    if not isinstance(request, ResearchRequest):
        raise TypeError(
            "apply_request_transition expects a ResearchRequest, got"
            f" {type(request).__name__}"
        )
    if not isinstance(to_status, ResearchRequestStatus):
        raise TypeError(
            "apply_request_transition expects a ResearchRequestStatus, got"
            f" {type(to_status).__name__}"
        )
    if request.status == to_status:
        raise IllegalRequestTransitionError(request.status, to_status)
    if not is_legal_request_transition(request.status, to_status):
        raise IllegalRequestTransitionError(request.status, to_status)
    return dataclasses.replace(request, status=to_status)


# ---------------------------------------------------------------------------
# Result linkage rules (ordered table; all preconditions must pass)
# ---------------------------------------------------------------------------


class LinkageVerdict(StrEnum):
    """Verdict of the result-linkage rule table."""

    LINKED = "LINKED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ResultLinkageRule:
    """One precondition of the ordered result-linkage rule table."""

    rule_id: str
    description: str
    predicate: Callable[[ResearchRequest, str], bool]


@dataclass(frozen=True)
class ResultLinkageRuleDecision:
    """Record of one linkage-rule evaluation (auditability)."""

    rule_id: str
    description: str
    passed: bool


@dataclass(frozen=True)
class ResultLinkageAssessment:
    """Full verdict of the linkage rule table for one (request, evidence)."""

    request_id: str
    evidence_id: str
    verdict: LinkageVerdict
    decisions: tuple[ResultLinkageRuleDecision, ...]
    rejecting_rule_id: str | None = None


#: The ordered linkage rule table. Unlike the transition table (first
#: match wins) every rule is a precondition that must pass; the first
#: failing rule names the rejection.
RESULT_LINKAGE_RULES: tuple[ResultLinkageRule, ...] = (
    ResultLinkageRule(
        rule_id="R-LINK-S1",
        description=(
            "results may only be linked while the request is SEARCHING "
            "(before SEARCHING nothing has been found; COMPLETE and "
            "EXHAUSTED are terminal)"
        ),
        predicate=lambda request, evidence_id: request.status
        is ResearchRequestStatus.SEARCHING,
    ),
    ResultLinkageRule(
        rule_id="R-LINK-D1",
        description=(
            "an evidence id may only be linked once (no duplicate "
            "attachment)"
        ),
        predicate=lambda request, evidence_id: evidence_id
        not in request.result_evidence_ids,
    ),
)


def evaluate_result_linkage(
    request: ResearchRequest, evidence_id: str
) -> ResultLinkageAssessment:
    """Evaluate the linkage rule table over one (request, evidence) pair.

    Pure and deterministic. ``LINKED`` means every rule passed;
    ``REJECTED`` records the first failing rule id.

    Raises:
        TypeError: ``request`` is not a ``ResearchRequest``, or
            ``evidence_id`` is not a ``str``.
    """
    if not isinstance(request, ResearchRequest):
        raise TypeError(
            "evaluate_result_linkage expects a ResearchRequest, got"
            f" {type(request).__name__}"
        )
    if not isinstance(evidence_id, str):
        raise TypeError(
            "evaluate_result_linkage expects a str evidence_id, got"
            f" {type(evidence_id).__name__}"
        )
    decisions: list[ResultLinkageRuleDecision] = []
    rejecting: str | None = None
    for rule in RESULT_LINKAGE_RULES:
        passed = rule.predicate(request, evidence_id)
        decisions.append(
            ResultLinkageRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                passed=passed,
            )
        )
        if not passed and rejecting is None:
            rejecting = rule.rule_id
    return ResultLinkageAssessment(
        request_id=request.request_id,
        evidence_id=evidence_id,
        verdict=LinkageVerdict.LINKED if rejecting is None else LinkageVerdict.REJECTED,
        decisions=tuple(decisions),
        rejecting_rule_id=rejecting,
    )


@dataclass(frozen=True)
class ResultLinkageRecord:
    """Auditable link between one Research Request and one result (AC-03).

    Carries the request id, the linked evidence (result) id, the linkage
    timestamp, the linkage actor and the request status at linkage time.
    The timestamp is injected by the caller (determinism; the frozen
    ``ResearchRequest`` model has no timestamp field, so the audit record
    carries it). The audit trail is reconstructible from these records
    alone: request id, evidence id, when and by whom, and in which state.
    """

    request_id: str
    evidence_id: str
    linked_at: str
    linked_by: str
    request_status: ResearchRequestStatus


def attach_result_to_request(
    request: ResearchRequest,
    evidence_id: str,
    linked_by: str,
    linked_at: str,
) -> tuple[ResearchRequest, ResultLinkageRecord]:
    """Link one result (evidence) to a request and return the audit record.

    Pure and deterministic: the input request is never mutated; a fresh
    ``ResearchRequest`` with ``evidence_id`` appended to
    ``result_evidence_ids`` is returned together with the linkage record.

    Raises:
        TypeError: ``request`` is not a ``ResearchRequest``, or
            ``evidence_id`` / ``linked_by`` / ``linked_at`` is not a
            ``str``.
        RequestLinkageError: an empty string was passed, or the linkage
            rule table rejected the linkage (stable message naming the
            rejecting rule).
    """
    if not isinstance(request, ResearchRequest):
        raise TypeError(
            "attach_result_to_request expects a ResearchRequest, got"
            f" {type(request).__name__}"
        )
    if not isinstance(evidence_id, str):
        raise TypeError(
            "attach_result_to_request expects a str evidence_id, got"
            f" {type(evidence_id).__name__}"
        )
    if not isinstance(linked_by, str):
        raise TypeError(
            "attach_result_to_request expects a str linked_by, got"
            f" {type(linked_by).__name__}"
        )
    if not isinstance(linked_at, str):
        raise TypeError(
            "attach_result_to_request expects a str linked_at, got"
            f" {type(linked_at).__name__}"
        )
    if not evidence_id:
        raise RequestLinkageError(
            "attach_result_to_request: evidence_id must not be empty"
        )
    if not linked_by:
        raise RequestLinkageError(
            "attach_result_to_request: linked_by must not be empty"
        )
    if not linked_at:
        raise RequestLinkageError(
            "attach_result_to_request: linked_at must not be empty"
        )
    assessment = evaluate_result_linkage(request, evidence_id)
    if assessment.verdict is not LinkageVerdict.LINKED:
        rejected = next(
            rule for rule in RESULT_LINKAGE_RULES if rule.rule_id == assessment.rejecting_rule_id
        )
        raise RequestLinkageError(
            f"attach_result_to_request: cannot link evidence {evidence_id!r} "
            f"to request {request.request_id!r}: rule {rejected.rule_id} "
            f"rejected the linkage ({rejected.description})"
        )
    linked = dataclasses.replace(
        request, result_evidence_ids=[*request.result_evidence_ids, evidence_id]
    )
    record = ResultLinkageRecord(
        request_id=request.request_id,
        evidence_id=evidence_id,
        linked_at=linked_at,
        linked_by=linked_by,
        request_status=request.status,
    )
    return linked, record


# ---------------------------------------------------------------------------
# Supervisor-only issuance (AC-02)
# ---------------------------------------------------------------------------

#: Module-private token that gates the ``IssuedResearchRequest``
#: constructor. Only :func:`issue_research_request` (via the private
#: ``_from_issuance`` factory) passes this token; any direct construction
#: fails, so there is no public constructor path that bypasses issuance.
_ISSUANCE_TOKEN: object = object()


@dataclass(frozen=True)
class IssuedResearchRequest:
    """A formally issued Research Request record (AC-02).

    Instances exist **only** through the Supervisor-facing API
    :func:`issue_research_request`; the constructor is gated by the
    module-private :data:`_ISSUANCE_TOKEN`, so no public construction
    path can create a formally issued request (no worker/research-agent
    path exists). ``request`` is the frozen ``ResearchRequest`` in the
    issued state ``OPEN`` with the schema's ``requested_by`` constant
    ``"supervisor"``; ``issued_at`` is the injected issuance timestamp;
    ``issued_by`` is always ``"supervisor"``; ``category`` optionally
    scopes the request to one bootstrap workflow category
    (``research.workflows.BootstrapCategory``).
    """

    request: ResearchRequest
    issued_at: str
    issued_by: Literal["supervisor"] = "supervisor"
    category: BootstrapCategory | None = None
    _issuance_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._issuance_token is not _ISSUANCE_TOKEN:
            raise ResearchRequestError(
                "IssuedResearchRequest: a formally issued request can only "
                "be created through issue_research_request (the "
                "Supervisor-facing API); direct construction is not a "
                "valid issuance path"
            )

    @classmethod
    def _from_issuance(
        cls,
        request: ResearchRequest,
        issued_at: str,
        category: BootstrapCategory | None,
    ) -> Self:
        """Private factory; only :func:`issue_research_request` calls it.

        Re-checks the schema invariants of a formally issued request so a
        forged record can never enter the system.
        """
        if request.requested_by != "supervisor":
            raise RequestIssuanceError(
                "issue_research_request: requested_by must be 'supervisor' "
                f"(schema const), got {request.requested_by!r}"
            )
        if request.status is not ResearchRequestStatus.OPEN:
            raise RequestIssuanceError(
                "issue_research_request: a formally issued request must be "
                f"in state OPEN, got {request.status.value!r}"
            )
        return cls(
            request=request,
            issued_at=issued_at,
            issued_by="supervisor",
            category=category,
            _issuance_token=_ISSUANCE_TOKEN,
        )


def issue_research_request(
    *,
    request_id: str,
    question: str,
    origin_refs: Sequence[str],
    required_search_families: Sequence[str] = (),
    minimum_reliability: int | None = None,
    minimum_directness: int | None = None,
    issued_at: str,
    category: BootstrapCategory | None = None,
) -> IssuedResearchRequest:
    """Formally issue a Research Request (Supervisor-facing API, AC-02).

    The **sole** entry point that creates a formally issued request
    (09-RESEARCH-SUBSYSTEM.md section 3: only Supervisor may issue
    formal Research Requests). The issued record is in state ``OPEN``
    with ``requested_by == "supervisor"`` (the schema's constant);
    ``issued_by`` is always ``"supervisor"`` and no parameter accepts
    another issuer identity. ``issued_at`` is the injected deterministic
    timestamp. ``category`` optionally scopes the request to one
    bootstrap workflow category.

    Raises:
        TypeError: any argument has the wrong type (strings must be
            ``str``, sequences must be sequences of non-empty ``str``,
            reliability/directness must be ``int`` or ``None``, category
            must be a ``BootstrapCategory`` or ``None``).
        RequestIssuanceError: a value is invalid (empty ``request_id``,
            empty ``question``, empty ``origin_refs``, out-of-range
            ``minimum_reliability`` / ``minimum_directness`` (0..4 per
            ``schemas/research-request.schema.yaml``), empty
            ``issued_at``). Stable messages.
    """
    if not isinstance(request_id, str):
        raise TypeError(
            "issue_research_request expects a str request_id, got"
            f" {type(request_id).__name__}"
        )
    if not request_id:
        raise RequestIssuanceError("issue_research_request: request_id must not be empty")
    if not isinstance(question, str):
        raise TypeError(
            "issue_research_request expects a str question, got"
            f" {type(question).__name__}"
        )
    if not question:
        raise RequestIssuanceError("issue_research_request: question must not be empty")
    if isinstance(origin_refs, (str, bytes)) or not isinstance(origin_refs, Sequence):
        raise TypeError(
            "issue_research_request expects a sequence of str origin_refs, got"
            f" {type(origin_refs).__name__}"
        )
    if not origin_refs:
        raise RequestIssuanceError(
            "issue_research_request: origin_refs must not be empty (the "
            "request must cite its originating Goal/decision)"
        )
    for ref in origin_refs:
        if not isinstance(ref, str):
            raise TypeError(
                "issue_research_request expects str origin_refs members, got"
                f" {type(ref).__name__}"
            )
        if not ref:
            raise RequestIssuanceError(
                "issue_research_request: origin_refs members must not be empty"
            )
    if isinstance(required_search_families, (str, bytes)) or not isinstance(
        required_search_families, Sequence
    ):
        raise TypeError(
            "issue_research_request expects a sequence of str "
            f"required_search_families, got {type(required_search_families).__name__}"
        )
    for family in required_search_families:
        if not isinstance(family, str):
            raise TypeError(
                "issue_research_request expects str required_search_families "
                f"members, got {type(family).__name__}"
            )
        if not family:
            raise RequestIssuanceError(
                "issue_research_request: required_search_families members "
                "must not be empty"
            )
    if minimum_reliability is not None:
        if isinstance(minimum_reliability, bool) or not isinstance(
            minimum_reliability, int
        ):
            raise TypeError(
                "issue_research_request expects an int or None "
                f"minimum_reliability, got {type(minimum_reliability).__name__}"
            )
        if not 0 <= minimum_reliability <= 4:
            raise RequestIssuanceError(
                "issue_research_request: minimum_reliability must be between "
                f"0 and 4 (research-request.schema.yaml), got {minimum_reliability}"
            )
    if minimum_directness is not None:
        if isinstance(minimum_directness, bool) or not isinstance(
            minimum_directness, int
        ):
            raise TypeError(
                "issue_research_request expects an int or None "
                f"minimum_directness, got {type(minimum_directness).__name__}"
            )
        if not 0 <= minimum_directness <= 4:
            raise RequestIssuanceError(
                "issue_research_request: minimum_directness must be between "
                f"0 and 4 (research-request.schema.yaml), got {minimum_directness}"
            )
    if not isinstance(issued_at, str):
        raise TypeError(
            "issue_research_request expects a str issued_at, got"
            f" {type(issued_at).__name__}"
        )
    if not issued_at:
        raise RequestIssuanceError("issue_research_request: issued_at must not be empty")
    if category is not None and not isinstance(category, BootstrapCategory):
        raise TypeError(
            "issue_research_request expects a BootstrapCategory or None "
            f"category, got {type(category).__name__}"
        )
    request = ResearchRequest(
        request_id=request_id,
        requested_by="supervisor",
        question=question,
        origin_refs=list(origin_refs),
        status=ResearchRequestStatus.OPEN,
        required_search_families=list(required_search_families),
        minimum_reliability=minimum_reliability,
        minimum_directness=minimum_directness,
    )
    return IssuedResearchRequest._from_issuance(request, issued_at, category)
