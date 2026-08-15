"""Tests for the research role-facing state authoring helpers (issue #92).

The helpers (``research/state_helpers.py``) give the Research agent the
official authoring facade over the existing primitives: source and
evidence records, research-request registration and lifecycle moves and
result linkage, with deterministic event ids and idempotency keys (the
monitoring pattern) instead of hand-rolled plumbing. Coverage maps to
the issue's observed hand-rolled layer:

* source writes -- ``test_register_source_*``: canonical-JSON records
  at ``sources/source/<id>.json``, canonical mirror identity derived at
  authoring time (malformed DOIs surfaced loudly), mirror collisions
  rejected (06-EVIDENCE-SYSTEM.md section 7), exactly-once with
  crash-window convergence;
* evidence writes -- ``test_register_evidence_*``: records at
  ``evidence/evidence/<id>.json`` validated against the frozen evidence
  shape (the same checks the in-memory ``EvidenceRegistry`` applies),
  exactly-once;
* request lifecycle -- ``test_register_request_*`` /
  ``test_advance_request_*`` / ``test_link_result_*``: persistence of
  the issued (OPEN) request, lifecycle moves through the normative rule
  table (no-op transitions rejected), result linkage through the
  linkage rule table, and one deterministic event per operation;
* event appends -- every operation audits through the real
  ``ProjectEventLog`` under deterministic idempotency keys;
  crash-window convergence and steady-state re-runs resolve to the
  single original event (``replayed=True``, sequence never advances
  twice);
* reads -- ``test_reads_*``: typed reads and sorted listings with
  stable not-found and corrupt-record errors.

The deterministic path follows the house suites: every fixture pins the
identity/timestamp of ``initialize_project``, and all timestamps and
actors are injected (no wall clock anywhere).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    EvidenceAssessment,
    ResearchRequest,
    ResearchRequestStatus,
    ResearchSource,
    SourceType,
)
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    initialize_project,
)
from scientific_reproduction.research.evidence import (
    EvidenceDuplicateError,
    EvidenceRegistrationError,
)
from scientific_reproduction.research.requests import (
    IllegalRequestTransitionError,
    RequestIssuanceError,
    RequestLinkageError,
)
from scientific_reproduction.research.sources import SourceNormalizationError
from scientific_reproduction.research.state_helpers import (
    EVENTS_STATE_DIR,
    EVIDENCE_RECORDED_EVENT_TYPE,
    EVIDENCE_STATE_DIR,
    REQUEST_PREDECESSOR_STATUS,
    RESEARCH_REQUEST_RECORDED_EVENT_TYPE,
    RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE,
    RESEARCH_REQUEST_STATE_DIR,
    RESEARCH_REQUEST_TRANSITION_EVENT_TYPE,
    SOURCE_RECORDED_EVENT_TYPE,
    SOURCE_STATE_DIR,
    DuplicateRequestError,
    DuplicateSourceError,
    EvidenceNotFoundError,
    RequestNotFoundError,
    SourceNotFoundError,
    StateHelperError,
    advance_research_request,
    link_result_to_request,
    list_evidence,
    list_research_requests,
    list_sources,
    read_evidence,
    read_research_request,
    read_source,
    register_evidence,
    register_research_request,
    register_source,
)

# ---------------------------------------------------------------------------
# Fixtures (deterministic: pinned identity/timestamp, injected stamps)
# ---------------------------------------------------------------------------

#: Deterministic author/committer identity for the initialized project.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects.
DOI = "10.1039/D5TA00771B"

#: Injected actor and recording stamps (no wall clock anywhere).
ACTOR = "research"
RECORDED_AT = "2026-01-02T00:00:00Z"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def event_log(root: Path) -> ProjectEventLog:
    """The workspace event log bound to the ``events/`` directory."""
    return ProjectEventLog(root / EVENTS_STATE_DIR)


def make_source(
    source_id: str = "SRC-1",
    *,
    source_type: SourceType = SourceType.PEER_REVIEWED_PAPER,
    doi: str | None = DOI,
    title: str | None = None,
) -> ResearchSource:
    """Build a schema-valid source record with compact defaults."""
    return ResearchSource(
        source_id=source_id,
        source_type=source_type,
        title=title or "Batch adsorption of the FDM-201 reference case",
        provenance="acquired from the publisher during bootstrap research",
        doi=doi,
    )


def make_evidence(
    evidence_id: str = "EVID-1",
    *,
    source_id: str = "SRC-1",
    claim_id: str = "CLAIM-1",
) -> ClaimSpecificEvidence:
    """Build a schema-valid claim-specific evidence record."""
    return ClaimSpecificEvidence(
        evidence_id=evidence_id,
        source_id=source_id,
        claim_id=claim_id,
        finding="The paper reports batch-level uptake within tolerance",
        assessment=EvidenceAssessment(
            authority=3,
            reliability=4,
            directness=4,
            reliability_checklist_ref="RCHK-001",
        ),
        used_by=["GOAL-1"],
    )


def make_request(
    request_id: str = "REQ-1",
    *,
    status: ResearchRequestStatus = ResearchRequestStatus.OPEN,
) -> ResearchRequest:
    """Build the schema-valid issued research request."""
    return ResearchRequest(
        request_id=request_id,
        requested_by="supervisor",
        question="Does the FDM-201 batch-level uptake reproduce?",
        origin_refs=["GOAL-1"],
        status=status,
    )


# ---------------------------------------------------------------------------
# register_source
# ---------------------------------------------------------------------------


def test_register_source_persists_canonical_record_and_identity(tmp_path):
    root = init_project(tmp_path)
    registration = register_source(
        root, make_source(), actor=ACTOR, recorded_at=RECORDED_AT
    )
    assert registration.source == make_source()
    assert registration.identity.key == f"doi:{DOI.lower()}"
    assert registration.identity.normalized_doi == DOI.lower()
    assert registration.replayed is False
    assert registration.event_record is None
    stored = json.loads(
        (root / SOURCE_STATE_DIR / "source" / "SRC-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["source_id"] == "SRC-1"
    assert stored["doi"] == DOI
    assert list_sources(root) == (make_source(),)


def test_register_source_audits_one_recorded_event(tmp_path):
    root = init_project(tmp_path)
    registration = register_source(
        root,
        make_source(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=event_log(root),
    )
    record = registration.event_record
    assert record is not None and record.replayed is False
    event = record.event
    assert event.event_type == SOURCE_RECORDED_EVENT_TYPE
    assert event.object_id == "SRC-1"
    assert event.actor == ACTOR
    assert event.timestamp == RECORDED_AT
    assert event_log(root).get(event.event_id) is not None


def test_register_source_duplicate_rejected_and_bytes_untouched(tmp_path):
    root = init_project(tmp_path)
    register_source(root, make_source(), actor=ACTOR, recorded_at=RECORDED_AT)
    path = root / SOURCE_STATE_DIR / "source" / "SRC-1.json"
    original = path.read_text(encoding="utf-8")
    with pytest.raises(DuplicateSourceError, match="already registered"):
        register_source(
            root,
            make_source(title="different content"),
            actor=ACTOR,
            recorded_at=RECORDED_AT,
        )
    assert path.read_text(encoding="utf-8") == original


def test_register_source_rejects_mirror_collision(tmp_path):
    root = init_project(tmp_path)
    register_source(root, make_source("SRC-1"), actor=ACTOR, recorded_at=RECORDED_AT)
    mirror = make_source(
        "SRC-2",
        source_type=SourceType.PREPRINT,
        doi="doi:10.1039/d5ta00771b",  # same work, wrapper-prefixed, uppercase
    )
    with pytest.raises(
        DuplicateSourceError, match="mirrors registered source 'SRC-1'"
    ):
        register_source(root, mirror, actor=ACTOR, recorded_at=RECORDED_AT)
    assert read_source(root, "SRC-1") == make_source("SRC-1")


def test_register_source_record_scoped_type_shares_doi_without_collision(tmp_path):
    root = init_project(tmp_path)
    register_source(root, make_source("SRC-1"), actor=ACTOR, recorded_at=RECORDED_AT)
    si = make_source(
        "SRC-2", source_type=SourceType.SUPPLEMENTARY_INFORMATION, doi=DOI
    )
    registration = register_source(root, si, actor=ACTOR, recorded_at=RECORDED_AT)
    # SI/dataset/structure records are record-scoped (AC-02 of sources.py):
    # they keep their own address and never collapse on the parent DOI.
    assert registration.identity.key == "record:SRC-2"
    assert sorted(s.source_id for s in list_sources(root)) == ["SRC-1", "SRC-2"]


def test_register_source_surfaces_malformed_doi(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(SourceNormalizationError, match="malformed DOI"):
        register_source(
            root,
            make_source(doi="10.1039"),
            actor=ACTOR,
            recorded_at=RECORDED_AT,
        )
    assert list_sources(root) == ()


def test_register_source_requires_initialized_project(tmp_path):
    with pytest.raises(ProjectNotInitializedError):
        register_source(tmp_path, make_source(), actor=ACTOR, recorded_at=RECORDED_AT)


def test_register_source_type_errors(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        register_source(root, "SRC-1", actor=ACTOR, recorded_at=RECORDED_AT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_source(root, make_source(), actor=1, recorded_at=RECORDED_AT)  # type: ignore[arg-type]
    with pytest.raises(StateHelperError):
        register_source(root, make_source(), actor="", recorded_at=RECORDED_AT)


def test_register_source_crash_window_converges_exactly_once(tmp_path):
    root = init_project(tmp_path)
    log = event_log(root)
    # First call: the record write lands, the event append does not (a
    # crash between the two steps). Simulated by writing the record
    # through the raw state backend, exactly the hand-rolled layer's
    # interruption point.
    (root / SOURCE_STATE_DIR / "source" / "SRC-1.json").parent.mkdir(
        parents=True, exist_ok=True
    )
    (root / SOURCE_STATE_DIR / "source" / "SRC-1.json").write_text(
        json.dumps(make_source().to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    # Re-run converges: the missing deterministic event is appended and
    # the original record is reported (replayed).
    registration = register_source(
        root,
        make_source(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=log,
    )
    assert registration.replayed is True
    assert registration.event_record is not None
    assert registration.event_record.replayed is False
    # A third call finds record and event both present: a true duplicate.
    with pytest.raises(DuplicateSourceError, match="already registered"):
        register_source(
            root,
            make_source(),
            actor=ACTOR,
            recorded_at=RECORDED_AT,
            event_log=log,
        )
    assert len(log.list_events()) == 1


# ---------------------------------------------------------------------------
# register_evidence
# ---------------------------------------------------------------------------


def test_register_evidence_persists_and_audits(tmp_path):
    root = init_project(tmp_path)
    registration = register_evidence(
        root,
        make_evidence(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=event_log(root),
    )
    assert registration.evidence == make_evidence()
    assert registration.replayed is False
    assert registration.event_record is not None
    assert registration.event_record.event.event_type == EVIDENCE_RECORDED_EVENT_TYPE
    stored = json.loads(
        (root / EVIDENCE_STATE_DIR / "evidence" / "EVID-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["evidence_id"] == "EVID-1"
    assert read_evidence(root, "EVID-1") == make_evidence()
    assert list_evidence(root) == (make_evidence(),)


def test_register_evidence_rejects_invalid_records(tmp_path):
    root = init_project(tmp_path)
    out_of_range = ClaimSpecificEvidence(
        evidence_id="EVID-1",
        source_id="SRC-1",
        claim_id="CLAIM-1",
        finding="The paper reports batch-level uptake within tolerance",
        assessment=EvidenceAssessment(
            authority=3, reliability=4, directness=9,
            reliability_checklist_ref="RCHK-001",
        ),
        used_by=["GOAL-1"],
    )
    with pytest.raises(EvidenceRegistrationError, match="0-4"):
        register_evidence(root, out_of_range, actor=ACTOR, recorded_at=RECORDED_AT)
    no_checklist = ClaimSpecificEvidence(
        evidence_id="EVID-1",
        source_id="SRC-1",
        claim_id="CLAIM-1",
        finding="The paper reports batch-level uptake within tolerance",
        assessment=EvidenceAssessment(
            authority=3, reliability=4, directness=4,
            reliability_checklist_ref="",
        ),
        used_by=["GOAL-1"],
    )
    with pytest.raises(EvidenceRegistrationError, match="reliability_checklist_ref"):
        register_evidence(root, no_checklist, actor=ACTOR, recorded_at=RECORDED_AT)
    assert list_evidence(root) == ()


def test_register_evidence_duplicate_and_convergence(tmp_path):
    root = init_project(tmp_path)
    log = event_log(root)
    register_evidence(
        root,
        make_evidence(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=log,
    )
    with pytest.raises(EvidenceDuplicateError, match="already registered"):
        register_evidence(
            root,
            make_evidence(),
            actor=ACTOR,
            recorded_at=RECORDED_AT,
            event_log=log,
        )
    # Crash-window convergence on a fresh project: record present, event
    # absent -> the missing event is appended, replayed.
    other_root = init_project(tmp_path / "other")
    (other_root / EVIDENCE_STATE_DIR / "evidence" / "EVID-1.json").parent.mkdir(
        parents=True, exist_ok=True
    )
    (other_root / EVIDENCE_STATE_DIR / "evidence" / "EVID-1.json").write_text(
        json.dumps(make_evidence().to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    replayed = register_evidence(
        other_root,
        make_evidence(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=event_log(other_root),
    )
    assert replayed.replayed is True
    assert len(event_log(other_root).list_events()) == 1


# ---------------------------------------------------------------------------
# register_research_request
# ---------------------------------------------------------------------------


def test_register_request_persists_issued_state_and_audits(tmp_path):
    root = init_project(tmp_path)
    registration = register_research_request(
        root,
        make_request(),
        actor="supervisor",
        recorded_at=RECORDED_AT,
        event_log=event_log(root),
    )
    assert registration.request == make_request()
    assert registration.event_record is not None
    assert (
        registration.event_record.event.event_type
        == RESEARCH_REQUEST_RECORDED_EVENT_TYPE
    )
    stored = json.loads(
        (
            root / RESEARCH_REQUEST_STATE_DIR / "research-request" / "REQ-1.json"
        ).read_text(encoding="utf-8")
    )
    assert stored["status"] == "OPEN"
    assert read_research_request(root, "REQ-1") == make_request()
    assert list_research_requests(root) == (make_request(),)


def test_register_request_rejects_non_issued_states(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(RequestIssuanceError, match="only OPEN"):
        register_research_request(
            root,
            make_request(status=ResearchRequestStatus.COMPLETE),
            actor="supervisor",
            recorded_at=RECORDED_AT,
        )
    forged = make_request()
    forged = forged.__class__(
        **{**forged.to_dict(), "requested_by": "research"}
    )
    with pytest.raises(RequestIssuanceError, match="requested_by"):
        register_research_request(
            root, forged, actor="supervisor", recorded_at=RECORDED_AT
        )
    assert list_research_requests(root) == ()


def test_register_request_duplicate(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    with pytest.raises(DuplicateRequestError, match="already registered"):
        register_research_request(
            root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
        )


# ---------------------------------------------------------------------------
# advance_research_request
# ---------------------------------------------------------------------------


def test_advance_request_legal_transitions_persist_and_audit(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    log = event_log(root)
    advancement = advance_research_request(
        root, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-03T00:00:00Z",
        event_log=log,
    )
    assert advancement.replayed is False
    assert advancement.request.status is ResearchRequestStatus.SEARCHING
    event = advancement.event_record.event
    assert event.event_type == RESEARCH_REQUEST_TRANSITION_EVENT_TYPE
    assert event.from_ == "OPEN"
    assert event.to == "SEARCHING"
    assert event.reason == "search started"
    assert event.actor == ACTOR
    # The full legal lifecycle: SEARCHING -> COMPLETE and -> EXHAUSTED.
    completed = advance_research_request(
        root, "REQ-1", ResearchRequestStatus.COMPLETE,
        actor=ACTOR, reason="findings linked", at="2026-01-04T00:00:00Z",
        event_log=log,
    )
    assert completed.request.status is ResearchRequestStatus.COMPLETE
    assert completed.event_record.event.from_ == "SEARCHING"
    assert completed.event_record.event.to == "COMPLETE"
    exhausted_root = init_project(tmp_path / "exhausted")
    register_research_request(
        exhausted_root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    advance_research_request(
        exhausted_root, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-03T00:00:00Z",
        event_log=event_log(exhausted_root),
    )
    exhausted = advance_research_request(
        exhausted_root, "REQ-1", ResearchRequestStatus.EXHAUSTED,
        actor=ACTOR, reason="search space exhausted", at="2026-01-04T00:00:00Z",
        event_log=event_log(exhausted_root),
    )
    assert exhausted.request.status is ResearchRequestStatus.EXHAUSTED
    assert exhausted.event_record.event.to == "EXHAUSTED"


def test_advance_request_rejects_illegal_pairs(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    # OPEN -> COMPLETE is not in the rule table.
    with pytest.raises(IllegalRequestTransitionError, match="OPEN.*COMPLETE"):
        advance_research_request(
            root, "REQ-1", ResearchRequestStatus.COMPLETE,
            actor=ACTOR, reason="skip", at="2026-01-03T00:00:00Z",
        )
    # A no-op (OPEN -> OPEN) is never legal (R-REQ-D1) and never enters
    # the audit record, with or without an event log.
    with pytest.raises(IllegalRequestTransitionError, match="OPEN.*OPEN"):
        advance_research_request(
            root, "REQ-1", ResearchRequestStatus.OPEN,
            actor=ACTOR, reason="noop", at="2026-01-03T00:00:00Z",
        )
    with pytest.raises(IllegalRequestTransitionError, match="OPEN.*OPEN"):
        advance_research_request(
            root, "REQ-1", ResearchRequestStatus.OPEN,
            actor=ACTOR, reason="noop", at="2026-01-03T00:00:00Z",
            event_log=event_log(root),
        )
    assert event_log(root).list_events() == []
    assert read_research_request(root, "REQ-1").status is ResearchRequestStatus.OPEN


def test_advance_request_steady_state_and_crash_window_converge(tmp_path):
    root = init_project(tmp_path)
    log = event_log(root)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT,
        event_log=log,
    )
    first = advance_research_request(
        root, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-03T00:00:00Z",
        event_log=log,
    )
    # Steady-state re-submission of the same move resolves to the single
    # original event (replayed): the sequence never advances twice.
    second = advance_research_request(
        root, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-05T00:00:00Z",
        event_log=log,
    )
    assert second.replayed is True
    assert second.event_record.event.event_id == first.event_record.event.event_id
    assert second.event_record.sequence == first.event_record.sequence
    assert len(log.list_events()) == 2  # recorded + transition, nothing new
    assert read_research_request(root, "REQ-1").status is ResearchRequestStatus.SEARCHING
    # Crash-window convergence: the record is already at SEARCHING but
    # the transition event was lost (raw write, no event) -- the unique
    # arc OPEN -> SEARCHING is appended and the call reports replayed.
    other = init_project(tmp_path / "crash")
    register_research_request(
        other, make_request(), actor="supervisor", recorded_at=RECORDED_AT,
        event_log=event_log(other),
    )
    path = other / RESEARCH_REQUEST_STATE_DIR / "research-request" / "REQ-1.json"
    advanced = make_request(status=ResearchRequestStatus.SEARCHING)
    path.write_text(
        json.dumps(advanced.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    converged = advance_research_request(
        other, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-06T00:00:00Z",
        event_log=event_log(other),
    )
    assert converged.replayed is True
    assert converged.event_record.event.from_ == "OPEN"
    assert converged.event_record.event.to == "SEARCHING"
    assert len(event_log(other).list_events()) == 2  # recorded + converged arc


def test_advance_request_unknown_request(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(RequestNotFoundError, match="REQ-9"):
        advance_research_request(
            root, "REQ-9", ResearchRequestStatus.SEARCHING,
            actor=ACTOR, reason="search", at=RECORDED_AT,
        )


def test_advance_request_type_errors(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    with pytest.raises(TypeError):
        advance_research_request(
            root, "REQ-1", "SEARCHING",  # type: ignore[arg-type]
            actor=ACTOR, reason="search", at=RECORDED_AT,
        )
    with pytest.raises(StateHelperError):
        advance_research_request(
            root, "REQ-1", ResearchRequestStatus.SEARCHING,
            actor=ACTOR, reason="", at=RECORDED_AT,
        )


# ---------------------------------------------------------------------------
# link_result_to_request
# ---------------------------------------------------------------------------


def test_link_result_to_request_while_searching(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    register_evidence(root, make_evidence(), actor=ACTOR, recorded_at=RECORDED_AT)
    log = event_log(root)
    advance_research_request(
        root, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-03T00:00:00Z",
        event_log=log,
    )
    result = link_result_to_request(
        root, "REQ-1", "EVID-1",
        linked_by=ACTOR, linked_at="2026-01-04T00:00:00Z",
        event_log=log,
    )
    assert result.replayed is False
    assert result.request.result_evidence_ids == ["EVID-1"]
    assert result.linkage.evidence_id == "EVID-1"
    assert result.linkage.request_status is ResearchRequestStatus.SEARCHING
    event = result.event_record.event
    assert event.event_type == RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE
    assert event.from_ == "SEARCHING"
    assert event.to == "EVID-1"
    assert event.actor == ACTOR
    assert read_research_request(root, "REQ-1").result_evidence_ids == ["EVID-1"]


def test_link_result_rejects_when_not_searching(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    with pytest.raises(RequestLinkageError, match="R-LINK-S1"):
        link_result_to_request(
            root, "REQ-1", "EVID-1",
            linked_by=ACTOR, linked_at=RECORDED_AT,
        )


def test_link_result_duplicate_replays_with_log_and_rejects_without(tmp_path):
    root = init_project(tmp_path)
    register_research_request(
        root, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    log = event_log(root)
    advance_research_request(
        root, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-03T00:00:00Z",
        event_log=log,
    )
    first = link_result_to_request(
        root, "REQ-1", "EVID-1",
        linked_by=ACTOR, linked_at="2026-01-04T00:00:00Z",
        event_log=log,
    )
    replayed = link_result_to_request(
        root, "REQ-1", "EVID-1",
        linked_by=ACTOR, linked_at="2026-01-05T00:00:00Z",
        event_log=log,
    )
    assert replayed.replayed is True
    assert replayed.event_record.event.event_id == first.event_record.event.event_id
    assert replayed.linkage.linked_at == first.linkage.linked_at
    assert read_research_request(root, "REQ-1").result_evidence_ids == ["EVID-1"]
    # Without an event log the duplicate is rejected by the rule table
    # (R-LINK-D1: an evidence id may only be linked once).
    other = init_project(tmp_path / "nolog")
    register_research_request(
        other, make_request(), actor="supervisor", recorded_at=RECORDED_AT
    )
    register_evidence(other, make_evidence(), actor=ACTOR, recorded_at=RECORDED_AT)
    advance_research_request(
        other, "REQ-1", ResearchRequestStatus.SEARCHING,
        actor=ACTOR, reason="search started", at="2026-01-03T00:00:00Z",
        event_log=event_log(other),
    )
    link_result_to_request(
        other, "REQ-1", "EVID-1",
        linked_by=ACTOR, linked_at="2026-01-04T00:00:00Z",
        event_log=event_log(other),
    )
    # Advance to COMPLETE first so R-LINK-S1 cannot reject the re-link.
    advance_research_request(
        other, "REQ-1", ResearchRequestStatus.COMPLETE,
        actor=ACTOR, reason="findings linked", at="2026-01-05T00:00:00Z",
        event_log=event_log(other),
    )
    with pytest.raises(RequestLinkageError, match="R-LINK-D1"):
        link_result_to_request(
            other, "REQ-1", "EVID-1",
            linked_by=ACTOR, linked_at="2026-01-06T00:00:00Z",
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_reads_not_found_and_corrupt_state(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(SourceNotFoundError, match="SRC-9"):
        read_source(root, "SRC-9")
    with pytest.raises(EvidenceNotFoundError, match="EVID-9"):
        read_evidence(root, "EVID-9")
    with pytest.raises(RequestNotFoundError, match="REQ-9"):
        read_research_request(root, "REQ-9")
    path = root / SOURCE_STATE_DIR / "source" / "SRC-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="is corrupt"):
        read_source(root, "SRC-1")


def test_reads_sorted_and_typed(tmp_path):
    root = init_project(tmp_path)
    register_source(
        root,
        make_source("SRC-2", doi="10.1039/D5TA00772B"),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )
    register_source(
        root, make_source("SRC-1"), actor=ACTOR, recorded_at=RECORDED_AT
    )
    register_evidence(
        root, make_evidence("EVID-2"), actor=ACTOR, recorded_at=RECORDED_AT
    )
    register_evidence(
        root, make_evidence("EVID-1"), actor=ACTOR, recorded_at=RECORDED_AT
    )
    register_research_request(
        root, make_request("REQ-2"), actor="supervisor", recorded_at=RECORDED_AT
    )
    register_research_request(
        root, make_request("REQ-1"), actor="supervisor", recorded_at=RECORDED_AT
    )
    assert [s.source_id for s in list_sources(root)] == ["SRC-1", "SRC-2"]
    assert [e.evidence_id for e in list_evidence(root)] == ["EVID-1", "EVID-2"]
    assert [r.request_id for r in list_research_requests(root)] == ["REQ-1", "REQ-2"]
    assert all(
        isinstance(record, ResearchSource) for record in list_sources(root)
    )
    assert all(
        isinstance(record, ClaimSpecificEvidence) for record in list_evidence(root)
    )
    assert all(
        isinstance(record, ResearchRequest)
        for record in list_research_requests(root)
    )


# ---------------------------------------------------------------------------
# Contract locks
# ---------------------------------------------------------------------------


def test_unique_predecessor_table_matches_rule_table():
    # Every non-initial status is reached through exactly one normative
    # arc, which is what makes crash-window convergence deterministic.
    assert REQUEST_PREDECESSOR_STATUS == {
        ResearchRequestStatus.OPEN: None,
        ResearchRequestStatus.SEARCHING: ResearchRequestStatus.OPEN,
        ResearchRequestStatus.COMPLETE: ResearchRequestStatus.SEARCHING,
        ResearchRequestStatus.EXHAUSTED: ResearchRequestStatus.SEARCHING,
    }
