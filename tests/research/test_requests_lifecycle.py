"""Tests for the Research Request lifecycle: Supervisor-only issuance,
transition rules and auditable result linkage (DEV-M5-G02).

Acceptance coverage (exact AC test names below):

  * AC-02 -- ``test_ac02_*``: Research Requests can only be formally
    issued through the Supervisor-facing API: ``issue_research_request``
    is the sole entry point that creates a formally issued request (state
    ``OPEN`` with request id and the schema's ``requested_by`` constant
    ``"supervisor"``); the issued record's constructor is gated by a
    module-private token so no public construction path bypasses
    issuance, no parameter accepts another issuer identity, and the
    private factory re-checks the schema invariants.
  * AC-03 -- ``test_ac03_*``: request/result linkage is auditable: the
    linkage record carries request id, evidence id, the (injected)
    linkage timestamp, the linkage actor and the request status at
    linkage time, so the audit trail is reconstructible from the records
    alone; linkage is validated by the ordered rule table (SEARCHING
    only, no duplicate attachment) with stable rejection messages.

Invariants: the lifecycle rule table is versioned and total with a
default (``R-REQ-D1`` -> ILLEGAL), no-op transitions are never legal,
``COMPLETE``/``EXHAUSTED`` are terminal, every assessment records the
full rule-decision trace, and all public boundaries raise ``TypeError``
for wrong argument types. All timestamps are fixed injected values
(determinism: no wall clock).
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any, Sequence

import pytest

from scientific_reproduction.core.models import ResearchRequest, ResearchRequestStatus
from scientific_reproduction.research.requests import (
    REQUEST_LIFECYCLE_RULESET_VERSION,
    REQUEST_TRANSITION_RULES,
    RESULT_LINKAGE_RULES,
    RESULT_LINKAGE_RULESET_VERSION,
    IllegalRequestTransitionError,
    IssuedResearchRequest,
    LinkageVerdict,
    RequestIssuanceError,
    RequestLinkageError,
    RequestTransitionAssessment,
    ResearchRequestError,
    ResultLinkageRecord,
    TransitionVerdict,
    apply_request_transition,
    attach_result_to_request,
    evaluate_request_transition,
    evaluate_result_linkage,
    is_legal_request_transition,
    issue_research_request,
)
from scientific_reproduction.research.workflows import BootstrapCategory

ISSUED_AT = "2026-01-15T10:00:00Z"
LINKED_AT = "2026-01-16T11:30:00Z"
LINKED_BY = "research_agent"
QUESTION = (
    "Is the adsorption capacity of the FDM-201 framework reported in the "
    "primary paper reproducible with the stated activation procedure?"
)
ORIGIN_REFS = ["goal:DEV-M5-G01", "decision:RESEARCH_REQUEST-01"]


def _issue(
    *,
    request_id: str = "REQ-001",
    question: str = QUESTION,
    origin_refs: Sequence[str] = ORIGIN_REFS,
    issued_at: str = ISSUED_AT,
    category: BootstrapCategory | None = BootstrapCategory.PAPER,
    **kwargs: Any,
) -> IssuedResearchRequest:
    """Issue a request through the Supervisor-facing API (compact defaults)."""
    return issue_research_request(
        request_id=request_id,
        question=question,
        origin_refs=list(origin_refs),
        issued_at=issued_at,
        category=category,
        **kwargs,
    )


def _searching(request: ResearchRequest) -> ResearchRequest:
    """Advance a request to SEARCHING via the rule table."""
    return apply_request_transition(request, ResearchRequestStatus.SEARCHING)


# ---------------------------------------------------------------------------
# AC-02: Supervisor-only issuance
# ---------------------------------------------------------------------------


def test_ac02_issuance_creates_formally_issued_request() -> None:
    """The Supervisor-facing API issues state OPEN with id and identity."""
    issuance = _issue(
        request_id="REQ-001",
        required_search_families=["exact target material/name/identifier"],
        minimum_reliability=3,
        minimum_directness=2,
    )
    issued = issuance.request
    # The formally issued state in the frozen vocabulary is OPEN.
    assert issued.status is ResearchRequestStatus.OPEN
    assert issued.request_id == "REQ-001"
    # The schema const: requested_by is the supervisor.
    assert issued.requested_by == "supervisor"
    assert issuance.issued_by == "supervisor"
    assert issuance.issued_at == ISSUED_AT
    assert issuance.category is BootstrapCategory.PAPER
    assert issued.question == QUESTION
    assert issued.origin_refs == list(ORIGIN_REFS)
    assert issued.required_search_families == [
        "exact target material/name/identifier"
    ]
    assert issued.minimum_reliability == 3
    assert issued.minimum_directness == 2
    assert issued.result_evidence_ids == []
    assert isinstance(issuance, IssuedResearchRequest)


def test_ac02_issuance_is_supervisor_only_by_construction() -> None:
    """No public constructor path can bypass the Supervisor-facing API."""
    with pytest.raises(ResearchRequestError, match="issue_research_request"):
        IssuedResearchRequest(
            request=_issue().request,
            issued_at=ISSUED_AT,
            issued_by="supervisor",
        )
    # The gate fires before any identity value can matter (the Literal
    # mismatch is deliberate; mypy checks only src/).
    with pytest.raises(ResearchRequestError, match="issue_research_request"):
        IssuedResearchRequest(  # type: ignore[arg-type]
            request=_issue().request,
            issued_at=ISSUED_AT,
            issued_by="worker",
        )
    # Even with a category set, direct construction is refused.
    with pytest.raises(ResearchRequestError, match="issue_research_request"):
        IssuedResearchRequest(
            request=_issue().request,
            issued_at=ISSUED_AT,
            issued_by="supervisor",
            category=BootstrapCategory.DATA,
        )


def test_ac02_private_factory_refuses_forged_records() -> None:
    """A forged record can never enter the system through issuance."""
    # A hand-built model with a non-supervisor issuer (the frozen model
    # does not validate the Literal at construction time, so the request
    # layer must) is refused by the private factory.
    forged = ResearchRequest(
        request_id="REQ-FORGED",
        requested_by="research_agent",
        question=QUESTION,
        origin_refs=list(ORIGIN_REFS),
        status=ResearchRequestStatus.OPEN,
    )
    with pytest.raises(RequestIssuanceError, match="requested_by must be 'supervisor'"):
        IssuedResearchRequest._from_issuance(forged, ISSUED_AT, None)
    # A non-OPEN request is not a formally issued request.
    searching = _searching(_issue().request)
    with pytest.raises(RequestIssuanceError, match="state OPEN"):
        IssuedResearchRequest._from_issuance(searching, ISSUED_AT, None)


def test_ac02_issuance_api_has_no_issuer_parameter() -> None:
    """The issuing API exposes no way to record any issuer but supervisor.

    Structural check: the Supervisor-facing signature has no actor/issuer/
    requested_by parameter, so issuance by any other identity is not
    expressible.
    """
    parameters = inspect.signature(issue_research_request).parameters
    actor_like = {
        name for name in parameters if name in ("actor", "issuer", "issued_by", "requested_by")
    }
    assert actor_like == set(), f"unexpected issuer parameters: {sorted(actor_like)}"


def test_ac02_issuance_validates_request_fields() -> None:
    """Invalid issuance content raises RequestIssuanceError (stable)."""
    with pytest.raises(RequestIssuanceError, match="request_id must not be empty"):
        _issue(request_id="")
    with pytest.raises(RequestIssuanceError, match="question must not be empty"):
        _issue(question="")
    with pytest.raises(RequestIssuanceError, match="origin_refs must not be empty"):
        _issue(origin_refs=[])
    with pytest.raises(RequestIssuanceError, match="origin_refs members"):
        _issue(origin_refs=["goal:DEV-M5-G01", ""])
    with pytest.raises(RequestIssuanceError, match="required_search_families members"):
        _issue(required_search_families=["", "exact target material"])
    with pytest.raises(RequestIssuanceError, match="minimum_reliability must be between"):
        _issue(minimum_reliability=5)
    with pytest.raises(RequestIssuanceError, match="minimum_reliability must be between"):
        _issue(minimum_reliability=-1)
    with pytest.raises(RequestIssuanceError, match="minimum_directness must be between"):
        _issue(minimum_directness=5)
    with pytest.raises(RequestIssuanceError, match="issued_at must not be empty"):
        _issue(issued_at="")
    # The boundary values 0 and 4 are valid per the schema.
    issued = _issue(minimum_reliability=0, minimum_directness=4)
    assert issued.request.minimum_reliability == 0
    assert issued.request.minimum_directness == 4


def test_ac02_issuance_rejects_wrong_argument_types() -> None:
    """Non-conforming argument types raise TypeError at the boundary."""
    with pytest.raises(TypeError, match="expects a str request_id"):
        _issue(request_id=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a str question"):
        _issue(question=None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a sequence of str origin_refs"):
        issue_research_request(  # direct call: the helper would coerce
            request_id="REQ-001",
            question=QUESTION,
            origin_refs="goal:DEV-M5-G01",  # type: ignore[arg-type]
            issued_at=ISSUED_AT,
        )
    with pytest.raises(TypeError, match="expects str origin_refs members"):
        _issue(origin_refs=["goal:DEV-M5-G01", 7])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="expects a sequence of str"):
        _issue(required_search_families="exact target material")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects str required_search_families"):
        _issue(required_search_families=[1])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="int or None minimum_reliability"):
        _issue(minimum_reliability=2.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int or None minimum_reliability"):
        _issue(minimum_reliability=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="int or None minimum_directness"):
        _issue(minimum_directness=2.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a str issued_at"):
        _issue(issued_at=123)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a BootstrapCategory or None"):
        _issue(category="paper")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Lifecycle rule table (ordered, versioned, total with a default)
# ---------------------------------------------------------------------------


def test_request_lifecycle_ruleset_is_versioned_and_total() -> None:
    assert REQUEST_LIFECYCLE_RULESET_VERSION == "1.0"
    assert RESULT_LINKAGE_RULESET_VERSION == "1.0"
    rule_ids = [rule.rule_id for rule in REQUEST_TRANSITION_RULES]
    assert len(rule_ids) == len(set(rule_ids)), "rule ids must be unique"
    assert rule_ids == ["R-REQ-S0", "R-REQ-C1", "R-REQ-E1", "R-REQ-D1"]
    # The trailing default rule matches every pair, so the table is total:
    # every status pair gets exactly one verdict.
    default_rule = REQUEST_TRANSITION_RULES[-1]
    assert default_rule.rule_id == "R-REQ-D1"
    assert default_rule.verdict is TransitionVerdict.ILLEGAL
    for old in ResearchRequestStatus:
        for new in ResearchRequestStatus:
            assessment = evaluate_request_transition(old, new)
            assert assessment.matched_rule_id is not None
            assert len(assessment.decisions) == len(REQUEST_TRANSITION_RULES)
    assert default_rule.predicate(
        ResearchRequestStatus.OPEN, ResearchRequestStatus.COMPLETE
    )


def test_request_lifecycle_legal_forward_chain() -> None:
    """OPEN -> SEARCHING -> COMPLETE is the legal mainline."""
    issued = _issue().request
    assert is_legal_request_transition(
        ResearchRequestStatus.OPEN, ResearchRequestStatus.SEARCHING
    )
    assert is_legal_request_transition(
        ResearchRequestStatus.SEARCHING, ResearchRequestStatus.COMPLETE
    )
    searching = apply_request_transition(issued, ResearchRequestStatus.SEARCHING)
    assert searching.status is ResearchRequestStatus.SEARCHING
    assert issued.status is ResearchRequestStatus.OPEN, "input must not be mutated"
    complete = apply_request_transition(searching, ResearchRequestStatus.COMPLETE)
    assert complete.status is ResearchRequestStatus.COMPLETE
    assert complete.request_id == "REQ-001"


def test_request_lifecycle_exhausted_arc() -> None:
    """OPEN -> SEARCHING -> EXHAUSTED is legal; EXHAUSTED is terminal."""
    searching = _searching(_issue().request)
    exhausted = apply_request_transition(searching, ResearchRequestStatus.EXHAUSTED)
    assert exhausted.status is ResearchRequestStatus.EXHAUSTED
    for status in ResearchRequestStatus:
        assert not is_legal_request_transition(
            ResearchRequestStatus.EXHAUSTED, status
        )


def test_request_lifecycle_illegal_transitions_raise_stable() -> None:
    """Illegal pairs raise IllegalRequestTransitionError with a stable message."""
    illegal_pairs = [
        (ResearchRequestStatus.OPEN, ResearchRequestStatus.OPEN),
        (ResearchRequestStatus.OPEN, ResearchRequestStatus.COMPLETE),
        (ResearchRequestStatus.OPEN, ResearchRequestStatus.EXHAUSTED),
        (ResearchRequestStatus.SEARCHING, ResearchRequestStatus.OPEN),
        (ResearchRequestStatus.SEARCHING, ResearchRequestStatus.SEARCHING),
        (ResearchRequestStatus.COMPLETE, ResearchRequestStatus.OPEN),
        (ResearchRequestStatus.COMPLETE, ResearchRequestStatus.SEARCHING),
        (ResearchRequestStatus.COMPLETE, ResearchRequestStatus.COMPLETE),
        (ResearchRequestStatus.COMPLETE, ResearchRequestStatus.EXHAUSTED),
        (ResearchRequestStatus.EXHAUSTED, ResearchRequestStatus.SEARCHING),
        (ResearchRequestStatus.EXHAUSTED, ResearchRequestStatus.COMPLETE),
    ]
    issued = _issue().request
    for old, new in illegal_pairs:
        assert not is_legal_request_transition(old, new)
        with pytest.raises(IllegalRequestTransitionError) as exc_info:
            apply_request_transition(_in_status(issued, old), new)
        message = str(exc_info.value)
        assert "illegal research-request transition" in message
        assert old.value in message and new.value in message
        assert isinstance(exc_info.value, ResearchRequestError)


def _in_status(request: ResearchRequest, status: ResearchRequestStatus) -> ResearchRequest:
    """Return a request in the given status (legal path only)."""
    if request.status is status:
        return request
    if request.status is ResearchRequestStatus.OPEN:
        if status is ResearchRequestStatus.SEARCHING:
            return apply_request_transition(request, status)
        if status in (
            ResearchRequestStatus.COMPLETE,
            ResearchRequestStatus.EXHAUSTED,
        ):
            return apply_request_transition(
                apply_request_transition(request, ResearchRequestStatus.SEARCHING),
                status,
            )
    if request.status is ResearchRequestStatus.SEARCHING and status in (
        ResearchRequestStatus.COMPLETE,
        ResearchRequestStatus.EXHAUSTED,
    ):
        return apply_request_transition(request, status)
    raise AssertionError(
        f"no legal path to {status.value!r} from {request.status.value!r}"
    )


def test_request_lifecycle_terminal_states_have_no_outgoing_transitions() -> None:
    for terminal in (ResearchRequestStatus.COMPLETE, ResearchRequestStatus.EXHAUSTED):
        assert all(
            not is_legal_request_transition(terminal, other)
            for other in ResearchRequestStatus
        )


def test_request_lifecycle_assessment_records_full_rule_trace() -> None:
    legal = evaluate_request_transition(
        ResearchRequestStatus.OPEN, ResearchRequestStatus.SEARCHING
    )
    assert isinstance(legal, RequestTransitionAssessment)
    assert legal.verdict is TransitionVerdict.LEGAL
    assert legal.matched_rule_id == "R-REQ-S0"
    assert [d.rule_id for d in legal.decisions] == [
        "R-REQ-S0",
        "R-REQ-C1",
        "R-REQ-E1",
        "R-REQ-D1",
    ]
    assert legal.decisions[0].matched is True
    # The default rule matches every pair (that is the total default); the
    # first match wins, so the matched rule is still R-REQ-S0.
    assert legal.decisions[3].matched is True
    illegal = evaluate_request_transition(
        ResearchRequestStatus.OPEN, ResearchRequestStatus.COMPLETE
    )
    assert illegal.verdict is TransitionVerdict.ILLEGAL
    assert illegal.matched_rule_id == "R-REQ-D1"


def test_request_lifecycle_boundary_type_errors() -> None:
    with pytest.raises(TypeError, match="expects a ResearchRequestStatus"):
        evaluate_request_transition("OPEN", ResearchRequestStatus.SEARCHING)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a ResearchRequestStatus"):
        evaluate_request_transition(ResearchRequestStatus.OPEN, "SEARCHING")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a ResearchRequestStatus"):
        is_legal_request_transition(None, ResearchRequestStatus.SEARCHING)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a ResearchRequest"):
        apply_request_transition("REQ-001", ResearchRequestStatus.SEARCHING)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a ResearchRequestStatus"):
        apply_request_transition(_issue().request, "SEARCHING")  # type: ignore[arg-type]


def test_request_apply_transition_never_mutates_input() -> None:
    issued = _issue().request
    searching = apply_request_transition(issued, ResearchRequestStatus.SEARCHING)
    complete = apply_request_transition(searching, ResearchRequestStatus.COMPLETE)
    assert issued.status is ResearchRequestStatus.OPEN
    assert searching.status is ResearchRequestStatus.SEARCHING
    assert complete.status is ResearchRequestStatus.COMPLETE


def test_request_frozen_model_round_trip() -> None:
    """The issued record serializes through the frozen model machinery."""
    issued = _issue().request
    assert ResearchRequest.from_dict(issued.to_dict()) == issued


# ---------------------------------------------------------------------------
# AC-03: auditable request/result linkage
# ---------------------------------------------------------------------------


def test_ac03_linkage_record_carries_full_audit_trail() -> None:
    """The linkage record reconstructs the audit trail from records alone."""
    searching = _searching(_issue(request_id="REQ-010").request)
    linked, record = attach_result_to_request(
        searching, "EVID-100", linked_by=LINKED_BY, linked_at=LINKED_AT
    )
    assert linked.result_evidence_ids == ["EVID-100"]
    assert isinstance(record, ResultLinkageRecord)
    # Everything needed to reconstruct the linkage lives on the record:
    # request id, evidence id, timestamp, actor, request state.
    assert record.request_id == "REQ-010"
    assert record.evidence_id == "EVID-100"
    assert record.linked_at == LINKED_AT
    assert record.linked_by == LINKED_BY
    assert record.request_status is ResearchRequestStatus.SEARCHING
    # The request's evidence list must equal the record trail (assertable
    # from the records alone).
    assert linked.result_evidence_ids == [record.evidence_id]


def test_ac03_linkage_requires_searching_state() -> None:
    """Linkage is only legal while the request is SEARCHING (R-LINK-S1)."""
    issued = _issue().request
    with pytest.raises(RequestLinkageError, match="R-LINK-S1"):
        attach_result_to_request(issued, "EVID-100", LINKED_BY, LINKED_AT)
    searching = _searching(issued)
    complete = apply_request_transition(searching, ResearchRequestStatus.COMPLETE)
    with pytest.raises(RequestLinkageError, match="R-LINK-S1"):
        attach_result_to_request(complete, "EVID-100", LINKED_BY, LINKED_AT)
    exhausted = apply_request_transition(
        _searching(_issue().request), ResearchRequestStatus.EXHAUSTED
    )
    with pytest.raises(RequestLinkageError, match="R-LINK-S1"):
        attach_result_to_request(exhausted, "EVID-100", LINKED_BY, LINKED_AT)
    # The failed request never changed.
    assert issued.result_evidence_ids == []


def test_ac03_linkage_rejects_duplicate_evidence() -> None:
    """An evidence id may only be linked once (R-LINK-D1)."""
    searching = _searching(_issue().request)
    linked, _ = attach_result_to_request(searching, "EVID-200", LINKED_BY, LINKED_AT)
    with pytest.raises(RequestLinkageError, match="R-LINK-D1"):
        attach_result_to_request(linked, "EVID-200", LINKED_BY, LINKED_AT)
    assert linked.result_evidence_ids == ["EVID-200"], "no duplicate may enter"


def test_ac03_linkage_accumulates_in_order_and_reconstructs() -> None:
    """Multiple links accumulate in order; the trail reconstructs."""
    searching = _searching(_issue().request)
    first, rec1 = attach_result_to_request(
        searching, "EVID-A", linked_by=LINKED_BY, linked_at="2026-01-16T09:00:00Z"
    )
    second, rec2 = attach_result_to_request(
        first, "EVID-B", linked_by=LINKED_BY, linked_at="2026-01-16T10:00:00Z"
    )
    third, rec3 = attach_result_to_request(
        second, "EVID-C", linked_by="research_agent_b", linked_at="2026-01-16T11:00:00Z"
    )
    assert third.result_evidence_ids == ["EVID-A", "EVID-B", "EVID-C"]
    records = (rec1, rec2, rec3)
    # Reconstruct the audit trail from the records alone.
    assert [(r.evidence_id, r.linked_at, r.linked_by) for r in records] == [
        ("EVID-A", "2026-01-16T09:00:00Z", LINKED_BY),
        ("EVID-B", "2026-01-16T10:00:00Z", LINKED_BY),
        ("EVID-C", "2026-01-16T11:00:00Z", "research_agent_b"),
    ]
    assert {r.request_id for r in records} == {"REQ-001"}
    assert all(r.request_status is ResearchRequestStatus.SEARCHING for r in records)
    # The evidence id list must equal the record trail (assertable from
    # the records alone).
    assert third.result_evidence_ids == [r.evidence_id for r in records]


def test_ac03_linkage_is_deterministic() -> None:
    """Same inputs -> identical outputs (no wall clock, no randomness)."""
    left = attach_result_to_request(
        _searching(_issue().request), "EVID-300", LINKED_BY, LINKED_AT
    )
    right = attach_result_to_request(
        _searching(_issue().request), "EVID-300", LINKED_BY, LINKED_AT
    )
    assert left[0] == right[0]
    assert left[1] == right[1]
    with pytest.raises(dataclasses.FrozenInstanceError):
        left[1].linked_at = "2026-01-17T00:00:00Z"  # type: ignore[misc]


def test_ac03_linkage_assessment_records_rule_trace() -> None:
    searching = _searching(_issue().request)
    assessment = evaluate_result_linkage(searching, "EVID-400")
    assert assessment.verdict is LinkageVerdict.LINKED
    assert assessment.rejecting_rule_id is None
    assert [d.rule_id for d in assessment.decisions] == [
        r.rule_id for r in RESULT_LINKAGE_RULES
    ]
    assert all(d.passed for d in assessment.decisions)
    # A duplicate attachment is rejected by R-LINK-D1.
    linked, _ = attach_result_to_request(searching, "EVID-400", LINKED_BY, LINKED_AT)
    rejected = evaluate_result_linkage(linked, "EVID-400")
    assert rejected.verdict is LinkageVerdict.REJECTED
    assert rejected.rejecting_rule_id == "R-LINK-D1"
    assert not rejected.decisions[0].passed or not rejected.decisions[1].passed


def test_ac03_linkage_boundary_type_errors() -> None:
    searching = _searching(_issue().request)
    with pytest.raises(TypeError, match="expects a ResearchRequest"):
        attach_result_to_request("REQ-001", "EVID-1", LINKED_BY, LINKED_AT)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a str evidence_id"):
        attach_result_to_request(searching, 1, LINKED_BY, LINKED_AT)  # type: ignore[arg-type]
    with pytest.raises(RequestLinkageError, match="evidence_id must not be empty"):
        attach_result_to_request(searching, "", LINKED_BY, LINKED_AT)
    with pytest.raises(TypeError, match="expects a str linked_by"):
        attach_result_to_request(searching, "EVID-1", None, LINKED_AT)  # type: ignore[arg-type]
    with pytest.raises(RequestLinkageError, match="linked_by must not be empty"):
        attach_result_to_request(searching, "EVID-1", "", LINKED_AT)
    with pytest.raises(TypeError, match="expects a str linked_at"):
        attach_result_to_request(searching, "EVID-1", LINKED_BY, 123)  # type: ignore[arg-type]
    with pytest.raises(RequestLinkageError, match="linked_at must not be empty"):
        attach_result_to_request(searching, "EVID-1", LINKED_BY, "")
    # evaluate_result_linkage has the same boundaries.
    with pytest.raises(TypeError, match="expects a ResearchRequest"):
        evaluate_result_linkage(None, "EVID-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a str evidence_id"):
        evaluate_result_linkage(searching, 7)  # type: ignore[arg-type]
