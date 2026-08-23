"""Research-process narrative section tests (issue #135).

The final reproduction report narrates the research process from
research state alone: per-category acquisition summaries through the
frozen ``BOOTSTRAP_WORKFLOW`` mapping, registered vs obtained vs
unavailable sources (the #134 acquisition-status field), research
request transitions and saturation conclusions, and a decision timeline
of the exclusion decisions -- every item citing source / evidence /
decision ids. These tests pin the issue's design bar:

* the section is present in every shipped language pack (en/zh);
* the section renders from state files only (``sources/``,
  ``research-requests/``, decision records, the event log);
* an empty research state renders an explicit "no research recorded"
  statement -- never a crash, never a silent omission;
* a populated state renders the per-category summary with citations;
* repeated builds over identical state are byte-identical (the
  injected-``generated_at`` discipline).
"""
from __future__ import annotations

from pathlib import Path

from reporting_helpers import (
    DOI,
    EVIDENCE_ID,
    IDENTITY,
    TIMESTAMP,
    install_valid_chain,
    make_evidence,
)

from scientific_reproduction.core.models import (
    AcquisitionStatus,
    DecisionType,
    ResearchRequest,
    ResearchRequestStatus,
    ResearchSource,
    SourceType,
    SupervisorDecision,
    UnavailabilityReason,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import initialize_project
from scientific_reproduction.reporting.pdf_report import build_pdf_report
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.research.state_helpers import (
    advance_research_request,
    link_result_to_request,
    register_evidence,
    register_research_request,
    register_source,
)

GENERATED_AT = "2026-08-15T00:00:00Z"

#: Fixed deterministic research-record timestamps (injected everywhere).
RECORDED_AT = "2026-01-02T00:00:00Z"
REQUEST_AT = "2026-01-03T00:00:00Z"
SEARCHING_AT = "2026-01-04T00:00:00Z"
COMPLETE_AT = "2026-01-05T00:00:00Z"

#: Fixed ids of the populated research state.
STRUCTURE_SOURCE_ID = "SRC-002"
DATASET_SOURCE_ID = "SRC-003"
REQUEST_ID = "RR-001"
DECISION_ID = "DEC-002"


def _build(
    root: Path,
    *,
    evidence: EvidenceRegistry | None = None,
    generated_at: str = GENERATED_AT,
    language: str = "en",
):
    return build_pdf_report(
        root,
        evidence,
        [],
        generated_at=generated_at,
        language=language,
    )


def _install_populated_research_state(root: Path) -> EvidenceRegistry:
    """Install a populated research state on top of the valid chain.

    Through the real registration APIs: two extra sources across
    bootstrap categories (an obtained structure deposition in W-BOOT-4,
    an unavailable dataset in W-BOOT-3), one research request advanced
    OPEN -> SEARCHING -> COMPLETE with one linked evidence result, and
    one exclusion decision citing the excluded source and the request.
    The valid chain's ``SRC-001`` (target paper) covers W-BOOT-1.
    """
    evidence = install_valid_chain(root)
    register_source(
        root,
        ResearchSource(
            source_id=STRUCTURE_SOURCE_ID,
            source_type=SourceType.STRUCTURE_DEPOSITION,
            title="FDM-201 CIF deposition",
            provenance="test fixture",
            stable_identifier="CCDC-2026123",
            acquisition_status=AcquisitionStatus.OBTAINED,
            acquired_at=RECORDED_AT,
            local_artifact_id="ART-CIF-001",
        ),
        actor="research",
        recorded_at=RECORDED_AT,
    )
    register_source(
        root,
        ResearchSource(
            source_id=DATASET_SOURCE_ID,
            source_type=SourceType.DATASET,
            title="FDM-201 raw adsorption isotherms (paywalled mirror)",
            provenance="test fixture",
            doi="10.5281/zenodo.2026123",
            acquisition_status=AcquisitionStatus.UNAVAILABLE,
            unavailability_reason=UnavailabilityReason.PAYWALL,
            unavailability_detail="subscription required",
        ),
        actor="research",
        recorded_at=RECORDED_AT,
    )
    register_research_request(
        root,
        ResearchRequest(
            request_id=REQUEST_ID,
            requested_by="supervisor",
            question="Locate the raw adsorption isotherm dataset of FDM-201",
            origin_refs=["REQ-001"],
            status=ResearchRequestStatus.OPEN,
            required_search_families=["datasets", "structure_files"],
        ),
        actor="supervisor",
        recorded_at=REQUEST_AT,
    )
    advance_research_request(
        root,
        REQUEST_ID,
        ResearchRequestStatus.SEARCHING,
        actor="research",
        reason="search started",
        at=SEARCHING_AT,
    )
    register_evidence(
        root,
        make_evidence(),
        actor="research",
        recorded_at=SEARCHING_AT,
    )
    link_result_to_request(
        root,
        REQUEST_ID,
        EVIDENCE_ID,
        linked_by="research",
        linked_at=SEARCHING_AT,
    )
    advance_research_request(
        root,
        REQUEST_ID,
        ResearchRequestStatus.COMPLETE,
        actor="research",
        reason="saturation: two consecutive zero-novelty cycles",
        at=COMPLETE_AT,
    )
    FilesystemStateBackend(root).write(
        "decision",
        DECISION_ID,
        SupervisorDecision(
            decision_id=DECISION_ID,
            decision_type=DecisionType.RESEARCH_REQUEST,
            actor="supervisor",
            timestamp=COMPLETE_AT,
            affected_refs=[DATASET_SOURCE_ID, REQUEST_ID],
            rationale=(
                "Excluded SRC-003: paywalled mirror of an already"
                " registered dataset"
            ),
        ).to_dict(),
    )
    return evidence


# ---------------------------------------------------------------------------
# the section is present in every shipped language pack
# ---------------------------------------------------------------------------


def test_research_section_present_in_every_language_pack(
    tmp_path: Path,
) -> None:
    """The report renders the research-process section in every shipped
    language pack (en and zh), in pipeline order."""
    evidence = install_valid_chain(tmp_path)
    en = _build(tmp_path, evidence=evidence)
    zh = _build(tmp_path, evidence=evidence, language="zh")

    en_titles = [section.title for section in en.sections]
    zh_titles = [section.title for section in zh.sections]
    assert en_titles[3] == "Research process"
    assert zh_titles[3] == "研究过程"
    assert b"Research process" in en.pdf_bytes


# ---------------------------------------------------------------------------
# empty research state renders the explicit statement
# ---------------------------------------------------------------------------


def test_research_section_empty_state_renders_explicit_line(
    tmp_path: Path,
) -> None:
    """A workspace without any research state renders the explicit
    "no research recorded" statement instead of crashing or silently
    dropping the section."""
    initialize_project(tmp_path, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    report = _build(tmp_path)

    assert [section.title for section in report.sections][3] == "Research process"
    assert b"No research recorded" in report.pdf_bytes
    assert b"no registered sources" in report.pdf_bytes


# ---------------------------------------------------------------------------
# populated state renders the per-category summary with citations
# ---------------------------------------------------------------------------


def test_research_section_populated_renders_per_category_summary(
    tmp_path: Path,
) -> None:
    """A populated research state renders the per-category acquisition
    summary (BOOTSTRAP_WORKFLOW steps), the acquisition-status counts,
    the unavailable-source failure story, the request transitions with
    the saturation conclusion and the exclusion decision timeline --
    every item citing source / evidence / decision ids."""
    evidence = _install_populated_research_state(tmp_path)
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    # Acquisition summary counts (registered vs obtained vs unavailable).
    assert b"3 recorded sources" in data
    assert b"1 obtained" in data
    assert b"1 identity-only registered" in data
    assert b"1 unavailable" in data
    # Per-category summary through the frozen BOOTSTRAP_WORKFLOW mapping:
    # every source lands in exactly one step, cited by id and status.
    assert b"W-BOOT-1" in data
    assert b"paper" in data
    assert b"W-BOOT-3" in data
    assert b"data" in data
    assert b"W-BOOT-4" in data
    assert b"structure" in data
    # Every source is cited by id together with its acquisition status
    # (the PDF writer octal-escapes the cell parentheses).
    assert b"SRC-001" in data
    assert b"REGISTERED" in data
    assert b"SRC-002" in data
    assert b"OBTAINED" in data
    assert b"SRC-003" in data
    assert b"UNAVAILABLE" in data
    # The failure story: the unavailable source cites its reason/detail.
    assert b"paywall" in data
    assert b"subscription required" in data
    # Research-request transitions and the saturation conclusion (the
    # transition reasons are rendered verbatim; the cell may wrap).
    assert b"OPEN -> SEARCHING" in data
    assert b"SEARCHING -> COMPLETE" in data
    assert b"saturation:" in data
    assert b"zero-novelty cycles" in data
    assert b"concluded COMPLETE" in data
    # Evidence citations of the request's linked results (the cell may
    # wrap between the label and the cited id).
    assert b"linked evidence" in data
    assert b"EVID-001" in data
    # The exclusion decision timeline cites the decision, the excluded
    # source, the request and the rationale.
    assert b"DEC-002" in data
    assert b"RR-001" in data
    assert b"Excluded SRC-003" in data


# ---------------------------------------------------------------------------
# determinism -- identical state, identical output
# ---------------------------------------------------------------------------


def test_research_section_deterministic_re_render(tmp_path: Path) -> None:
    """Repeated builds over identical (populated) research state yield
    byte-identical PDFs and canonical JSON."""
    evidence = _install_populated_research_state(tmp_path)
    first = _build(tmp_path, evidence=evidence)
    second = _build(tmp_path, evidence=evidence)

    assert first.pdf_bytes == second.pdf_bytes
    assert first.to_canonical_json() == second.to_canonical_json()
