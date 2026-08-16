"""Final reproduction report PDF renderer tests (issue #107).

The deterministic PDF report is assembled from persisted records only
(no agent prose): the audit package, the outcome summary, the planning /
inventory / analysis registries, the evidence registry, supervisor
decisions, the event log and the artifact manifests. These tests pin the
issue's design bar:

* deterministic, stdlib-only PDF output -- ``build_pdf_report`` takes an
  injected ``generated_at`` (the renderer never consults the clock) and
  repeated builds from the same state are byte-identical;
* review-ready document -- a table of contents with page numbers, styled
  tables, color-coded verdict callouts (PASS green / FAIL red /
  INCONCLUSIVE amber) and the required sections (executive summary,
  target paper identity and scope, pipeline summary, requirement
  outcomes, core findings per CRITICAL requirement with evidence trail,
  governance exercised, audit trail with artifact SHA-256 checksums,
  simulation/real-data labeling);
* machine-auditable registration -- the renderer writes
  ``reproduction-report.pdf`` plus a canonical JSON sidecar carrying the
  PDF's SHA-256, so the audit package can register the report files
  (issue requirement: report files must be registered with checksums).

``generated_at`` is a required keyword argument -- the renderer has no
other source of time, so the same state always renders the same bytes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from reporting_helpers import (
    ACCEPTANCE_ID,
    ANALYSIS_ID,
    ARTIFACT_ID,
    CLAIM_ID,
    EVIDENCE_ID,
    GOAL_ID,
    PROTOCOL_VERSION,
    REQUIREMENT_ID,
    RESULT_ID,
    RUN_ID,
    TIMESTAMP,
    install_chain_with_failed_run,
    install_valid_chain,
    make_requirement,
    make_result_record,
)

from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.models import (
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    DecisionType,
    InventoryItemType,
    MappingStatus,
    ProjectEvent,
    ReproductionInventoryItem,
    RequirementOutcome,
    SupervisorDecision,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.inventory import register_inventory_item
from scientific_reproduction.planning.plan import register_closure_contract
from scientific_reproduction.reporting.pdf_report import (
    PdfReportCorruptError,
    PdfReportNotInitializedError,
    build_pdf_report,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

GENERATED_AT = "2026-08-15T00:00:00Z"

#: Section titles in render order (the issue's section list).
SECTION_TITLES = [
    "Executive summary",
    "Target paper identity and reproduction scope",
    "Pipeline summary",
    "Requirement outcomes",
    "Core findings",
    "Governance exercised",
    "Audit trail",
    "Simulation and real-data labeling",
]


def _build(
    root: Path,
    *,
    evidence: EvidenceRegistry | None = None,
    key_claims: list[str] = [CLAIM_ID],
    generated_at: str = GENERATED_AT,
    language: str = "en",
    out_dir: Path | None = None,
):
    return build_pdf_report(
        root,
        evidence,
        key_claims,
        generated_at=generated_at,
        language=language,
        out_dir=out_dir,
    )


# ---------------------------------------------------------------------------
# deterministic render + machine-auditable files
# ---------------------------------------------------------------------------


def test_pdf_report_renders_valid_pdf_and_writes_files(tmp_path: Path) -> None:
    """The report renders a PDF 1.4 document and writes the PDF plus a
    canonical JSON sidecar (with the PDF checksum) to the out dir."""
    evidence = install_valid_chain(tmp_path)
    out_dir = tmp_path / "reports"
    report = _build(tmp_path, evidence=evidence, out_dir=out_dir)

    assert report.pdf_bytes.startswith(b"%PDF-1.4\n")
    assert report.pdf_bytes.rstrip().endswith(b"%%EOF")
    assert report.pages >= 1
    pdf_path = out_dir / "reproduction-report.pdf"
    json_path = out_dir / "reproduction-report.json"
    assert pdf_path.exists()
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["project_id"] == report.project_id
    assert data["language"] == "en"
    assert data["pdf_sha256"] == compute_sha256(pdf_path)
    assert data["pdf_size_bytes"] == len(report.pdf_bytes)


def test_pdf_report_is_byte_identical_across_builds(tmp_path: Path) -> None:
    """Repeated builds from the same state yield byte-identical PDF and
    canonical JSON (the generated_at injection is the only time source)."""
    evidence = install_chain_with_failed_run(tmp_path)[0]
    first = _build(tmp_path, evidence=evidence)
    second = _build(tmp_path, evidence=evidence)

    assert first.pdf_bytes == second.pdf_bytes
    assert first.to_canonical_json() == second.to_canonical_json()


def test_pdf_report_requires_injected_generated_at(tmp_path: Path) -> None:
    """``generated_at`` is required and must be a string: the renderer
    never consults the wall clock."""
    evidence = install_valid_chain(tmp_path)

    with pytest.raises(TypeError, match="generated_at"):
        build_pdf_report(tmp_path, evidence, [CLAIM_ID])  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="generated_at"):
        build_pdf_report(
            tmp_path,
            evidence,
            [CLAIM_ID],
            generated_at=20260815,  # type: ignore[arg-type]
        )


def test_pdf_report_no_wall_clock_keys_in_bytes(tmp_path: Path) -> None:
    """The rendered PDF carries no CreationDate/ModDate/ID keys."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)

    assert b"/CreationDate" not in report.pdf_bytes
    assert b"/ModDate" not in report.pdf_bytes
    assert b"/ID" not in report.pdf_bytes


# ---------------------------------------------------------------------------
# language packs: explicit language input (issue #122)
# ---------------------------------------------------------------------------

#: The zh section titles (mirror of the zh pack's ``section_titles``).
ZH_SECTION_TITLES = [
    "执行摘要",
    "目标论文身份与复现范围",
    "流水线摘要",
    "需求结果",
    "核心发现",
    "治理行使",
    "审计追踪",
    "模拟与真实数据标注",
]


def test_pdf_report_language_default_is_english_byte_identical(tmp_path):
    # ``language="en"`` is the explicit default and renders byte-identical
    # to the implicit default (the pre-pack renderer).
    evidence = install_valid_chain(tmp_path)
    default = _build(tmp_path, evidence=evidence)
    explicit = _build(tmp_path, evidence=evidence, language="en")
    assert default.pdf_bytes == explicit.pdf_bytes
    assert default.to_canonical_json() == explicit.to_canonical_json()


def test_pdf_report_language_zh_renders_chinese_sections(tmp_path):
    # The zh pack renders the section titles in Chinese on the structured
    # surface and in the JSON sidecar. (The deterministic PDF writer
    # encodes WinAnsi, so CJK glyphs in the rendered bytes fall back to
    # "?" -- the documented writer limitation; the language pack's
    # machine-readable projection carries the Chinese titles verbatim.)
    evidence = install_valid_chain(tmp_path)
    out_dir = tmp_path / "reports"
    report = _build(tmp_path, evidence=evidence, language="zh", out_dir=out_dir)

    assert report.language == "zh"
    assert [section.title for section in report.sections] == ZH_SECTION_TITLES
    data = json.loads(
        (out_dir / "reproduction-report.json").read_text(encoding="utf-8")
    )
    assert data["language"] == "zh"
    # The sidecar mirrors the structured surface (page numbers include
    # the leading TOC pages).
    assert [entry["title"] for entry in data["sections"]] == ZH_SECTION_TITLES
    assert data["sections"] == [
        {"title": entry["title"], "page_number": entry["page_number"]}
        for entry in data["sections"]
    ]
    assert all(entry["page_number"] >= 1 for entry in data["sections"])
    # The zh document renders deterministically and differs from the en
    # document (labels are pack strings, not data).
    zh_twice = _build(tmp_path, evidence=evidence, language="zh")
    assert zh_twice.pdf_bytes == report.pdf_bytes
    assert zh_twice.to_canonical_json() == report.to_canonical_json()
    en = _build(tmp_path, evidence=evidence)
    assert report.pdf_bytes != en.pdf_bytes


def test_pdf_report_language_unknown_raises_stable_error(tmp_path):
    # Unknown languages and non-string inputs raise the stable boundary
    # errors of ``resolve_pack`` (never silently fall back).
    evidence = install_valid_chain(tmp_path)
    with pytest.raises(ValueError, match="available languages: en, zh"):
        build_pdf_report(
            tmp_path, evidence, [CLAIM_ID],
            generated_at=GENERATED_AT, language="fr",
        )
    with pytest.raises(TypeError, match="language must be a non-empty string"):
        build_pdf_report(
            tmp_path, evidence, [CLAIM_ID],
            generated_at=GENERATED_AT, language=123,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# document structure -- TOC, sections, bookmarks
# ---------------------------------------------------------------------------


def test_pdf_report_sections_cover_issue_list_in_order(
    tmp_path: Path,
) -> None:
    """The report renders every required section with a page number and
    a table of contents entry."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)

    assert [section.title for section in report.sections] == SECTION_TITLES
    for section in report.sections:
        assert section.page_number >= 1
        assert section.page_number <= report.pages
    assert report.pdf_bytes.count(b"/Title (") >= len(SECTION_TITLES) + 1
    # TOC entries carry the section titles.
    for title in SECTION_TITLES:
        assert title.encode() in report.pdf_bytes


# ---------------------------------------------------------------------------
# executive summary -- verdict callout, finding, headline metric
# ---------------------------------------------------------------------------


def test_pdf_report_executive_summary_verdict_callout_passes(
    tmp_path: Path,
) -> None:
    """A fully reproduced chain renders a PASS callout (green) with the
    outcome verbatim and the matched ruleset/rule."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"0.15 0.52 0.26" in data  # PASS foreground
    assert b"0.91 0.97 0.92" in data  # PASS background
    assert b"FULLY_REPRODUCED" in data


def test_pdf_report_executive_summary_headline_metric_with_band(
    tmp_path: Path,
) -> None:
    """The single most important number renders with its confidence
    interval and the frozen acceptance band."""
    evidence = install_valid_chain(
        tmp_path,
        result=make_result_record(
            metrics=[
                {"metric": "batch_level_uptake", "value": 12.4},
                {"metric": "batch_level_uptake_ci_lower", "value": 11.9},
                {"metric": "batch_level_uptake_ci_upper", "value": 12.9},
            ],
            uncertainty={
                "method": "confidence_interval",
                "confidence_level": 0.95,
                "lower": 11.9,
                "upper": 12.9,
            },
        ),
    )
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"batch_level_uptake" in data
    assert b"12.40" in data
    assert b"11.90" in data
    assert b"12.90" in data
    assert b"0.05" in data  # frozen tolerance band


def test_pdf_report_executive_summary_finding_assembled_from_records(
    tmp_path: Path,
) -> None:
    """The finding paragraph is assembled from the recorded requirement
    outcomes and run statistics, never free prose."""
    evidence = install_chain_with_failed_run(tmp_path)[0]
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"REQ-001" in data
    assert b"REPRODUCED" in data
    assert b"runs" in data
    assert RUN_ID.encode() in data


# ---------------------------------------------------------------------------
# scope and pipeline -- counts from registries
# ---------------------------------------------------------------------------


def test_pdf_report_scope_and_pipeline_counts_from_registries(
    tmp_path: Path,
) -> None:
    """Target identity, plan version and pipeline counts come from the
    real registered records."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"10.1039/D5TA00771B" in data
    assert GOAL_ID.encode() in data
    assert ANALYSIS_ID.encode() in data
    assert PROTOCOL_VERSION.encode() in data
    assert ACCEPTANCE_ID.encode() in data
    assert ARTIFACT_ID.encode() in data


# ---------------------------------------------------------------------------
# requirement outcomes -- table + core findings with evidence trail
# ---------------------------------------------------------------------------


def test_pdf_report_requirement_outcome_table(tmp_path: Path) -> None:
    """The requirement table renders criticality, outcome and method
    reproducibility for every requirement."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"CRITICAL" in data
    assert b"REPRODUCED" in data
    assert b"REQ-001" in data


def test_pdf_report_core_findings_evidence_trail_per_critical(
    tmp_path: Path,
) -> None:
    """Core findings render, per CRITICAL requirement, the analysis
    results, evidence records, decisions and closure status that trace
    to it."""
    evidence = install_valid_chain(
        tmp_path,
        result=make_result_record(
            metrics=[{"metric": "batch_level_uptake", "value": 12.4}]
        ),
    )
    FilesystemStateBackend(tmp_path).write(
        "decision",
        "DEC-001",
        SupervisorDecision(
            decision_id="DEC-001",
            decision_type=DecisionType.REQUIREMENT_CLOSURE,
            actor="supervisor",
            timestamp=TIMESTAMP.isoformat(),
            affected_refs=[REQUIREMENT_ID],
            rationale="Closed after acceptance criteria met",
        ).to_dict(),
    )
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert RESULT_ID.encode() in data
    assert EVIDENCE_ID.encode() in data
    assert b"DEC-001" in data
    assert b"REQUIREMENT_CLOSURE" in data
    assert PROTOCOL_VERSION.encode() in data


# ---------------------------------------------------------------------------
# governance -- decisions, reconciliations, recovery ladder
# ---------------------------------------------------------------------------


def test_pdf_report_governance_renders_reconciliations_and_recovery(
    tmp_path: Path,
) -> None:
    """Governance renders the monitor reconciliation events and the
    recorded recovery/closure state."""
    evidence = install_valid_chain(tmp_path)
    ProjectEventLog(tmp_path).append(
        ProjectEvent(
            event_id="EVT-001",
            timestamp=TIMESTAMP.isoformat(),
            actor="execution-monitor",
            event_type="external_status_change",
            object_id=RUN_ID,
            run_id=RUN_ID,
            from_="RUNNING_EXTERNAL",
            to="RESULT_AVAILABLE",
            reason="monitor reconciliation",
        )
    )
    register_closure_contract(
        tmp_path,
        ClosureContract(
            closure_id="CLOS-001",
            frozen=True,
            statistical_sufficiency={"power": 0.8},
            execution_validity={"valid": True},
            diagnosis={"cause": "drift"},
            recovery=ClosureRecovery(
                eligible_hypotheses_total=3,
                tested_or_ruled_out=1,
                remaining=2,
            ),
            literature=ClosureLiterature(),
        ),
    )
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"EVT-001" in data
    assert b"external_status_change" in data
    assert b"CLOS-001" in data
    assert b"eligible" in data
    assert b"tested" in data


def test_pdf_report_governance_no_recorded_decisions_states_so(
    tmp_path: Path,
) -> None:
    """With no recorded decisions or reconciliations the governance
    section states that explicitly instead of hiding it."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"no recorded" in data


# ---------------------------------------------------------------------------
# audit trail -- artifacts with checksums, git refs
# ---------------------------------------------------------------------------


def test_pdf_report_audit_trail_artifacts_with_checksums(tmp_path: Path) -> None:
    """The audit trail lists artifact manifests with their SHA-256
    checksums (the ``a``-filled fixture sha renders verbatim)."""
    evidence = install_valid_chain(tmp_path)
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert ARTIFACT_ID.encode() in data
    assert b"a" * 16 in data  # sha256 prefix of the fixture manifest
    assert b"1024" in data  # size_bytes


# ---------------------------------------------------------------------------
# simulation / real-data labeling
# ---------------------------------------------------------------------------


def test_pdf_report_simulation_labeling_from_inventory(tmp_path: Path) -> None:
    """The labeling derives from the recorded inventory item types: a
    computation item makes the label mixed, not silently real-data."""
    evidence = install_valid_chain(tmp_path)
    register_inventory_item(
        tmp_path,
        ReproductionInventoryItem(
            inventory_id="INV-002",
            source_id="SRC-001",
            item_type=InventoryItemType.COMPUTATION,
            formal_report=True,
            description="Simulation of the FDM-201 batch",
            mapping_status=MappingStatus.MAPPED,
        ),
    )
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"mixed" in data
    assert b"computation" in data
    assert b"INV-002" in data


# ---------------------------------------------------------------------------
# NOT_REPRODUCED with closure -- FAIL callout (issue acceptance scenario)
# ---------------------------------------------------------------------------


def test_pdf_report_not_reproduced_with_closure_renders_fail(
    tmp_path: Path,
) -> None:
    """A NOT_REPRODUCED requirement with a recorded closure decision
    renders a FAIL callout (red) with the closure contract verbatim."""
    evidence = install_valid_chain(
        tmp_path,
        requirement=make_requirement(
            outcome=RequirementOutcome.NOT_REPRODUCED
        ),
    )
    register_closure_contract(
        tmp_path,
        ClosureContract(
            closure_id="CLOS-002",
            frozen=True,
            statistical_sufficiency={"power": 0.8},
            execution_validity={"valid": True},
            diagnosis={"cause": "drift"},
            recovery=ClosureRecovery(
                eligible_hypotheses_total=4,
                tested_or_ruled_out=2,
                remaining=2,
            ),
            literature=ClosureLiterature(),
            closure_allowed=True,
        ),
    )
    report = _build(tmp_path, evidence=evidence)
    data = report.pdf_bytes

    assert b"0.72 0.16 0.14" in data  # FAIL foreground
    assert b"0.98 0.92 0.91" in data  # FAIL background
    assert b"NOT_REPRODUCED" in data
    assert b"CLOS-002" in data


# ---------------------------------------------------------------------------
# boundaries -- structural failures raise
# ---------------------------------------------------------------------------


def test_pdf_report_uninitialized_workspace_raises(tmp_path: Path) -> None:
    """Rendering without a project state record raises a stable error."""
    with pytest.raises(PdfReportNotInitializedError, match="project state"):
        _build(tmp_path)


def test_pdf_report_corrupt_run_record_raises(tmp_path: Path) -> None:
    """A corrupt stored run record surfaces as PdfReportCorruptError."""
    evidence = install_valid_chain(tmp_path)
    run_path = tmp_path / "runs" / f"{RUN_ID}.json"
    run_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(PdfReportCorruptError, match="run"):
        _build(tmp_path, evidence=evidence)


def test_pdf_report_type_errors(tmp_path: Path) -> None:
    """Wrong argument types raise TypeError at the boundary."""
    with pytest.raises(TypeError, match="root"):
        build_pdf_report(42, None, [], generated_at=GENERATED_AT)
    with pytest.raises(TypeError, match="evidence"):
        build_pdf_report(
            tmp_path, "not-a-registry", [], generated_at=GENERATED_AT  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="key_claims"):
        build_pdf_report(
            tmp_path, EvidenceRegistry(), "CLAIM-001", generated_at=GENERATED_AT
        )
