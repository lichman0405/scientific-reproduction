"""Tests for the project finalization gate (planning.finalize).

The gate is the completion counterpart of the freeze preconditions:
open requirements, evidence without used_by links, a failing audit
validation, or missing report/summary/audit-package files must block
``COMPLETED`` with the offending items named (``FinalizationProhibitedError``).
"""
import json
from pathlib import Path

import pytest

from scientific_reproduction.planning.finalize import (
    FinalizationProhibitedError,
    check_finalization,
    finalize_project,
)

CLAIMS = ["CLAIM-1", "CLAIM-2"]


def _write(root: Path, rel: str, data: dict) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_project(tmp_path: Path, *, open_req=False, no_used_by=False,
                 no_report=False, no_summary=False,
                 outcome_undetermined=False) -> Path:
    root = tmp_path / "proj"
    _write(root, "project.yaml", {
        "project_id": "sr_project_test_finalize",
        "project_phase": "REPORTING",
        "reproduction_outcome": "UNDETERMINED" if outcome_undetermined else "FULLY_REPRODUCED",
        "primary_target": {"title": "t", "doi": "10.1/x"},
        "title": "t",
    })
    _write(root, "requirements/REQ-1.json", {
        "requirement_id": "REQ-1", "statement": "s1", "criticality": "CRITICAL",
        "goal_ids": ["G1"], "inventory_items": ["INV-1"],
        "outcome": "OPEN" if open_req else "REPRODUCED",
        "method_reproducibility": "DIRECTLY_REPRODUCIBLE",
        # the zh finalization gate (v0.3.0) requires an author-supplied
        # statement_zh on closed requirements for zh deliverables
        "statement_zh": "声明(中文)",
    })
    if not open_req:
        # sanctioned closure: emits the requirement.outcome.updated event
        # that the finalization gate (U6) requires
        from scientific_reproduction.core.models import (
            MethodReproducibility,
            RequirementOutcome,
        )
        from scientific_reproduction.planning.inventory import close_requirement
        close_requirement(
            root, "REQ-1", RequirementOutcome.REPRODUCED,
            method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE,
            reason="test closure", actor="supervisor", at="2026-01-01T00:00:00Z",
        )
    _write(root, "evidence/EVID-1.json", {
        "evidence_id": "EVID-1", "source_id": "SRC-1", "claim_id": CLAIMS[0],
        "finding": "f1", "role": "protocol_definition",
        "assessment": {"authority": 4, "reliability": 2, "directness": 4,
                       "reliability_checklist_ref": "RCHK-1"},
        "used_by": [] if no_used_by else ["REQ-1"],
    })
    _write(root, "evidence/EVID-2.json", {
        "evidence_id": "EVID-2", "source_id": "SRC-1", "claim_id": CLAIMS[1],
        "finding": "f2", "role": "dataset",
        "assessment": {"authority": 4, "reliability": 2, "directness": 4,
                       "reliability_checklist_ref": "RCHK-2"},
        "used_by": ["REQ-1"],
    })
    _write(root, "runs/run-1/GOAL-G1.json", {
        "goal_id": "GOAL-G1", "acceptance": {"verdict": "PASS"},
        "metrics": [{"metric": "m1", "value": 1.0, "claim": 1.0}],
        "finding": "ok",
    })
    _write(root, "analysis/results/RES-1.json", {
        "result_id": "RES-1", "analysis_id": "ANP-1", "protocol_version": "v1",
        "run_ref": "RUN-1", "input_artifact_ids": ["ART-1"],
        "primary_or_exploratory": "PRIMARY", "requirement_refs": ["REQ-1"],
        "acceptance_ref": "ACC-1", "output_artifact_ids": [],
        "environment": {}, "qc_findings": [], "metrics": [], "uncertainty": {},
        "warnings": [], "scripts": [],
    })
    _write(root, "runs/RUN-1.json", {
        "run_id": "RUN-1", "goal_id": "GOAL-G1", "goal_version": "v1",
        "run_type": "independent_replicate", "lifecycle_state": "CLOSED",
        "scientific_review": "PASS", "artifacts": ["ART-1-RESULT"],
        "deviations": [], "engineering_retries": [],
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
    })
    def _real_manifest(aid, uri):
        import hashlib as _h
        p = root / uri
        data = p.read_bytes()
        return {
            "artifact_id": aid, "uri": uri,
            "sha256": _h.sha256(data).hexdigest(),
            "size_bytes": len(data), "created_at": "2026-01-01T00:00:00Z",
        }
    _write(root, "manifests/ART-1.json",
           _real_manifest("ART-1", "runs/run-1/GOAL-G1.json"))
    _write(root, "manifests/ART-1-RESULT.json",
           _real_manifest("ART-1-RESULT", "analysis/results/RES-1.json"))
    _write(root, "acceptance/ACC-1.json", {
        "acceptance_id": "ACC-1", "goal_id": "GOAL-G1", "version": "v1",
        "frozen": True, "decision_mode": "bounded_interval",
        "criteria": [{"metric": "m", "rule": "r", "tolerance": 0.1}],
    })
    _write(root, "reports/reproduction-report.pdf", {"bytes": "dummy"}) if False else None
    (root / "reports").mkdir(parents=True, exist_ok=True)
    if not no_report:
        (root / "reports" / "reproduction-report.pdf").write_bytes(b"%PDF-1.4 dummy")
    if not no_summary:
        # must carry a "生成于 <timestamp>" line: the finalization gate
        # checks summary freshness against the last adjudication event
        # (v0.3.0; a bare heading predates it and blocks COMPLETED)
        (root / "reports" / "复现结果摘要.md").write_text(
            "# 复现结果摘要\n\n*生成于 2026-01-02T00:00:00Z*\n",
            encoding="utf-8",
        )
    return root


def test_check_passes_on_finalizable_project(tmp_path):
    root = make_project(tmp_path)
    check = check_finalization(root)
    assert check.passed, check.missing_items()


def test_check_blocks_open_requirements(tmp_path):
    root = make_project(tmp_path, open_req=True)
    check = check_finalization(root)
    assert not check.passed
    assert "REQ-1" in check.open_requirements


def test_check_blocks_evidence_without_used_by(tmp_path):
    root = make_project(tmp_path, no_used_by=True)
    check = check_finalization(root)
    assert not check.passed
    assert "EVID-1" in check.evidence_without_used_by


def test_check_blocks_missing_report(tmp_path):
    root = make_project(tmp_path, no_report=True)
    check = check_finalization(root)
    assert not check.passed
    assert check.report_pdf_missing


def test_check_blocks_artifact_sha_drift(tmp_path):
    root = make_project(tmp_path)
    # overwrite a registered artifact -> manifest SHA no longer matches
    (root / "runs/run-1/GOAL-G1.json").write_text("tampered", encoding="utf-8")
    check = check_finalization(root)
    assert not check.passed
    assert any("SHA drift" in item for item in check.artifact_content_mismatches)


def test_check_blocks_unrecorded_adjudication(tmp_path):
    root = make_project(tmp_path)
    # remove the closure event (bypassing the sanctioned API)
    for ev in (root / "events").glob("sr_event_*.json"):
        if "requirement.outcome.updated" in ev.read_text(encoding="utf-8"):
            ev.unlink()
    check = check_finalization(root)
    assert not check.passed
    assert "REQ-1" in check.unrecorded_adjudications


def test_check_blocks_undetermined_outcome(tmp_path):
    root = make_project(tmp_path, outcome_undetermined=True)
    check = check_finalization(root)
    assert not check.passed
    assert check.outcome_undetermined
    with pytest.raises(FinalizationProhibitedError) as exc:
        finalize_project(root, generated_at="2026-01-01T00:00:00Z",
                         language="zh", actor="supervisor")
    assert "UNDETERMINED" in str(exc.value)


def test_check_blocks_missing_summary(tmp_path):
    root = make_project(tmp_path, no_summary=True)
    check = check_finalization(root)
    assert not check.passed
    assert check.summary_missing


def test_finalize_writes_artifacts_and_completes(tmp_path):
    root = make_project(tmp_path)
    audit_path = finalize_project(
        root, generated_at="2026-01-01T00:00:00Z", language="zh",
        actor="supervisor", reason="done",
    )
    assert audit_path.name == "reproduction-audit-package.json"
    assert audit_path.exists()
    project = json.loads((root / "project.yaml").read_text(encoding="utf-8"))
    assert project["project_phase"] == "COMPLETED"
    assert (root / "reports" / "复现结果摘要.md").exists()
    # append-only event recorded
    events = list((root / "events").glob("sr_event_*.json"))
    assert any("finalized" in e.read_text(encoding="utf-8") for e in events)


def test_check_blocks_dangling_used_by(tmp_path):
    root = make_project(tmp_path)
    # 注入一条引用未注册对象的证据
    _write(root, "evidence/EVID-DANGLING.json", {
        "evidence_id": "EVID-DANGLING", "source_id": "SRC-1", "claim_id": "CLAIM-X",
        "finding": "fx", "role": "dataset",
        "assessment": {"authority": 4, "reliability": 2, "directness": 4,
                       "reliability_checklist_ref": "RCHK-X"},
        "used_by": ["REQ-DOES-NOT-EXIST"],
    })
    check = check_finalization(root)
    assert not check.passed
    assert any("EVID-DANGLING" in item for item in check.evidence_without_used_by)


def test_finalize_raises_on_open_requirements_without_writing(tmp_path):
    root = make_project(tmp_path, open_req=True)
    with pytest.raises(FinalizationProhibitedError) as exc:
        finalize_project(root, generated_at="2026-01-01T00:00:00Z",
                         language="zh", actor="supervisor")
    assert "REQ-1" in str(exc.value)
    project = json.loads((root / "project.yaml").read_text(encoding="utf-8"))
    assert project["project_phase"] != "COMPLETED"
