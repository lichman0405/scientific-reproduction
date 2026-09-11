"""Tests for the human-readable summary generator (reporting.human_summary).

Covers the two design contracts that matter most:

* metrics-first / finding-fallback: a run result package with structured
  ``metrics`` feeds the claim-vs-value table; one without degrades to its
  ``finding`` text -- never guessed, never regex-parsed;
* determinism: identical state + identical ``generated_at`` yields
  byte-identical output (both zh and en render).
"""
import json

import pytest

from scientific_reproduction.reporting.human_summary import (
    build_human_summary,
    build_human_summary_pdf,
    SummaryConsistencyError,
    write_human_summary,
    write_human_summary_pdf,
    write_human_summary_both,
)

GOAL_WITH_METRICS = {
    "goal_id": "GOAL-TEST-A",
    # adjudicated metric (claim + status) -> core table; a metric with a
    # claim but no status is reference-only and lands in the context table.
    "metrics": [
        {"metric": "slope", "label": "校准方程斜率", "value": 0.0928, "claim": 0.0927,
         "unit": "mg/L per mV", "status": "exact"},
    ],
    "acceptance": {"verdict": "PASS"},
    "finding": "OLS fit: y = 0.0928x - 1.2552, R2 = 0.9787.",
}
GOAL_FINDING_ONLY = {
    "goal_id": "GOAL-TEST-B",
    "acceptance": {"verdict": "PASS"},
    "finding": "Digitized 9 markers; slope matches published 0.0927.",
}


def _make_project(tmp_path) -> str:
    """Build a minimal registered-state project for summary rendering."""
    root = tmp_path / "project"
    for sub in ("requirements", "runs", "runs/run-1", "decisions", "assumptions", "manifests", "reports"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(json.dumps({
        "primary_target": {
            "title": "Test paper title",
            "doi": "10.1234/test.2026",
        },
        "title": "Test paper title",
    }), encoding="utf-8")
    (root / "requirements" / "REQ-A.json").write_text(json.dumps({
        "requirement_id": "REQ-A", "statement": "reproduce slope",
        "criticality": "CRITICAL", "goal_ids": ["GOAL-TEST-A"],
        "outcome": "REPRODUCED",
    }), encoding="utf-8")
    (root / "requirements" / "REQ-B.json").write_text(json.dumps({
        "requirement_id": "REQ-B", "statement": "reproduce markers",
        "criticality": "REQUIRED", "goal_ids": ["GOAL-TEST-B"],
        "outcome": "REPRODUCED_WITH_RECOVERY",
    }), encoding="utf-8")
    (root / "requirements" / "REQ-C.json").write_text(json.dumps({
        "requirement_id": "REQ-C", "statement": "field accuracy claim",
        "criticality": "CRITICAL", "goal_ids": ["GOAL-TEST-A"],
        "outcome": "NOT_REPRODUCED",
    }), encoding="utf-8")
    (root / "decisions" / "DEC-CLOSE-C.json").write_text(json.dumps({
        "decision_id": "DEC-CLOSE-C", "decision_type": "REQUIREMENT_CLOSURE",
        "affected_refs": ["REQ-C"],
        "rationale": "claim contradicted by the paper's own published data - "
                     "internal inconsistency, not a reproduction deviation.",
    }), encoding="utf-8")
    (root / "decisions" / "DEC-2.json").write_text(json.dumps({
        "decision_id": "DEC-2", "decision_type": "GOAL_REVIEW",
        "affected_refs": ["REQ-C"],
        "rationale": "max error 16.99% exceeds the 15% claim of the paper.",
    }), encoding="utf-8")
    (root / "runs" / "run-1" / "GOAL-TEST-A.json").write_text(
        json.dumps(GOAL_WITH_METRICS), encoding="utf-8")
    (root / "runs" / "run-1" / "GOAL-TEST-B.json").write_text(
        json.dumps(GOAL_FINDING_ONLY), encoding="utf-8")
    (root / "decisions" / "DEC-1.json").write_text(json.dumps({
        "decision_id": "DEC-1", "decision_type": "GOAL_REVIEW",
        "affected_refs": ["REQ-A"],
        "rationale": "text vs figure intercept inconsistent; figure variant wins.",
    }), encoding="utf-8")
    # governance-type decisions must NOT surface as paper findings (U7)
    (root / "decisions" / "DEC-CLOSE.json").write_text(json.dumps({
        "decision_id": "DEC-CLOSE", "decision_type": "REQUIREMENT_CLOSURE",
        "rationale": "see OUTCOMES placeholder",
    }), encoding="utf-8")
    (root / "assumptions" / "ASM-1.json").write_text(json.dumps({
        "assumption_id": "ASM-1", "classification": "A2_SCIENTIFIC_ASSUMPTION",
        "rationale": "raw spectra not published; synthetic spectra used.",
    }), encoding="utf-8")
    (root / "manifests" / "ART-1.json").write_text(json.dumps({
        "artifact_id": "ART-1", "uri": "knowledge/data.csv",
        "sha256": "0" * 64, "size_bytes": 1, "created_at": "2026-01-01T00:00:00Z",
    }), encoding="utf-8")
    return str(root)


def test_metrics_first_renders_structured_values(tmp_path):
    md = build_human_summary(_make_project(tmp_path), generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "校准方程斜率" in md
    assert "0.0928" in md          # reproduced value from metrics
    assert "0.0927" in md          # claimed value from metrics
    assert "✅" in md               # PASS verdict icon


def test_finding_fallback_never_guesses(tmp_path):
    md = build_human_summary(_make_project(tmp_path), generated_at="2026-01-01T00:00:00Z", language="zh")
    # GOAL-TEST-B has no metrics -> its finding text is surfaced, not parsed
    assert "目标 TEST-B" in md     # goal id shown under the zh goal label
    assert "Digitized 9 markers" in md
    assert "各目标结果说明" in md  # finding fallback renders as a result note


def test_tri_state_status_icon(tmp_path):
    root = _make_project(tmp_path)
    # 给 GOAL-TEST-A 的 metric 加 within_tolerance status
    import json as _j
    p = root + "/runs/run-1/GOAL-TEST-A.json"
    rec = _j.loads(open(p, encoding="utf-8").read())
    rec["metrics"][0]["status"] = "within_tolerance"
    open(p, "w", encoding="utf-8").write(_j.dumps(rec))
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "⚠️" in md          # within_tolerance metric row icon in the evidence
    ev = md.split("## 关键数值证据")[1] if "## 关键数值证据" in md else md
    assert "⚠️" in ev


def test_unknown_status_degrades_with_warning(tmp_path):
    root = _make_project(tmp_path)
    import json as _j
    p = root + "/runs/run-1/GOAL-TEST-A.json"
    rec = _j.loads(open(p, encoding="utf-8").read())
    rec["metrics"][0]["status"] = "totally_made_up"
    open(p, "w", encoding="utf-8").write(_j.dumps(rec))
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "status 告警" in md
    assert "totally_made_up" in md


def test_conclusion_counts_tolerance_rows(tmp_path):
    root = _make_project(tmp_path)
    import json as _j
    p = root + "/runs/run-1/GOAL-TEST-A.json"
    rec = _j.loads(open(p, encoding="utf-8").read())
    rec["metrics"][0]["status"] = "within_tolerance"
    open(p, "w", encoding="utf-8").write(_j.dumps(rec))
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "⚠️" in md.split("## 关键数值证据")[1]


def test_uncertainty_section_renders(tmp_path):
    root = _make_project(tmp_path)
    import json as _j
    p = root + "/runs/run-1/GOAL-TEST-A.json"
    rec = _j.loads(open(p, encoding="utf-8").read())
    rec["uncertainty"] = {"digitization": "pixel/OCR uncertainty on extraction"}
    open(p, "w", encoding="utf-8").write(_j.dumps(rec))
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "结果不确定度" in md
    assert "pixel/OCR uncertainty" in md


def test_governance_decisions_not_shown_as_findings(tmp_path):
    md = build_human_summary(_make_project(tmp_path), generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "DEC-1" in md            # GOAL_REVIEW finding shown (grouped)
    assert "DEC-CLOSE" not in md    # REQUIREMENT_CLOSURE filtered out
    assert "## 发现与确认" in md  # v2 findings title


def test_no_decisions_is_stated_as_unrecorded(tmp_path):
    root = _make_project(tmp_path)
    import shutil, os as _os
    for f in _os.listdir(root + "/decisions"):
        _os.remove(root + "/decisions/" + f)
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "未登记问题发现" in md
    assert "## 发现与确认" in md
    assert "未发现论文内部不一致" not in md


def test_problems_and_limits_sections(tmp_path):
    md = build_human_summary(_make_project(tmp_path), generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "DEC-1" in md
    assert "ASM-1" in md
    assert "## 假设与边界" in md   # v2 limits title


def test_determinism_same_state_same_bytes(tmp_path):
    root = _make_project(tmp_path)
    a = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    b = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert a == b


def test_language_packs_both_render(tmp_path):
    root = _make_project(tmp_path)
    zh = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="en")
    assert zh and en and zh != en
    assert "Reproduction Summary" in en


def _pdf_text_content(data: bytes) -> str:
    """Extract decoded text from deterministic PDF bytes via pypdf."""
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


def test_pdf_deterministic_and_contains_table_data(tmp_path):
    root = _make_project(tmp_path)
    a = build_human_summary_pdf(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    b = build_human_summary_pdf(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert a == b
    assert a[:8] == b"%PDF-1.4"
    assert "0.0928" in _pdf_text_content(a)  # claim-vs-value table data rendered
    assert "复现结果摘要" in _pdf_text_content(a)


def test_pdf_emoji_icons_mapped_to_ascii(tmp_path):
    root = _make_project(tmp_path)
    data = build_human_summary_pdf(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    # emoji glyphs would render as NUL in the font stack; they must be
    # replaced by ASCII markers. Check the decoded text layer (raw PDF
    # bytes legitimately contain format NULs).
    text = _pdf_text_content(data)
    assert "\x00" not in text and "�" not in text
    assert "[OK]" in text or "[within-tol]" in text


def test_write_pdf_and_both(tmp_path):
    root = _make_project(tmp_path)
    pdf = write_human_summary_pdf(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert pdf.exists() and pdf.read_bytes()[:8] == b"%PDF-1.4"
    md, pdf2 = write_human_summary_both(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert md.exists() and pdf2.exists()


def test_write_human_summary_persists(tmp_path):
    root = _make_project(tmp_path)
    out = tmp_path / "out" / "summary.md"
    written = write_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh", out_path=out)
    assert written == out
    assert out.exists()
    assert "复现结果摘要" in out.read_text(encoding="utf-8")


# --- v2/P21 generalizability guards ----------------------------------------

def test_legend_bilingual_and_no_project_examples(tmp_path):
    """Matrix legend + appendix structure (v4); no project examples leak."""
    root = _make_project(tmp_path)
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="en")
    zh = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    cjk = lambda s: any("一" <= c <= "鿿" for c in s)
    assert "## 一句话结论" in zh
    assert "## 产出覆盖矩阵" in zh
    i_zh = zh.index("## 产出覆盖矩阵")
    assert "需求级综合裁决" in zh[i_zh:i_zh + 300]
    i_en = en.index("## Reproduction matrix")
    assert "requirement-level verdict" in en[i_en:i_en + 400]
    assert "## 附录 A：需求裁决详情" in zh
    assert "## 附录 B：判定口径与验收带（详细）" in zh
    assert "## Appendix B: verdict semantics and acceptance bands" in en
    app_b_zh = zh.split("## 附录 B：判定口径与验收带（详细）")[1]
    assert "Fig.10" not in zh and "Fig.10" not in en and "截距偏差" not in app_b_zh


def test_finding_group_keywords_zh_and_en(tmp_path):
    """Grouping matches keywords in both languages; untagged stays in
    observations (P21)."""
    from scientific_reproduction.reporting.human_summary import _group_key
    assert _group_key("【claim refuted】max err exceeds claim") == "grp_claim"
    assert _group_key("【推翻声称】实测超出声称") == "grp_claim"
    assert _group_key("【internal inconsistency】text vs figure") == "grp_internal"
    assert _group_key("【内部不一致】正文与图注") == "grp_internal"
    assert _group_key("【structure confirmed】markers match") == "grp_struct"
    assert _group_key("【结构确认】散点一致") == "grp_struct"
    assert _group_key("【misc note】untagged-like") == "grp_obs"
    assert _group_key("no lead tag at all") == "grp_obs"
    assert _group_key("PLS R=0.9999 缺乏预测力证据（无验证/交叉验证）") == "grp_claim"
    assert _group_key("蓝光 LED 激发声称缺乏数据支撑") == "grp_claim"




def test_internal_inconsistency_badge_on_requirement_row(tmp_path):
    """A grp_internal finding that names the requirement surfaces as a
    paper-internal-inconsistency badge on that requirement row."""
    md = build_human_summary(_make_project(tmp_path),
                             generated_at="2026-01-01T00:00:00Z", language="zh")
    rows = [l for l in md.split(chr(10)) if l.startswith('| REQ-A ')]
    assert rows and "论文内部不一致" in rows[0]


def test_internal_badge_on_not_reproduced_by_self_data(tmp_path):
    """A NOT_REPRODUCED requirement whose rationale declares the paper's own
    inconsistency carries the internal badge (v4/C)."""
    md = build_human_summary(_make_project(tmp_path),
                             generated_at="2026-01-01T00:00:00Z", language="zh")
    rows = [l for l in md.split(chr(10)) if l.startswith("| REQ-C ")]
    assert rows and "论文内部不一致" in rows[0]


def test_internal_group_carries_cross_reference(tmp_path):
    """A claim-group finding whose requirement has an internal-nature closure
    is cross-referenced under 论文内部不一致 (P24e)."""
    md = build_human_summary(_make_project(tmp_path),
                             generated_at="2026-01-01T00:00:00Z", language="zh")
    internal_sec = md.split("**论文内部不一致**")[1].split("**其他观察**")[0]
    assert "另见" in internal_sec and "DEC-2" in internal_sec


def test_internal_badge_pairs_with_synthetic_explanation(tmp_path):
    """标注-详述配对 invariant: a NOT_REPRODUCED requirement whose rationale
    declares the paper's own inconsistency gets BOTH the matrix badge AND a
    rendered explanation entry in the findings section (the synthetic entry
    must not stay behind the badge)."""
    root = _make_project(tmp_path)
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    internal_sec = md.split("**论文内部不一致**")[1].split("**其他观察**")[0]
    # REQ-C has no grp_internal finding; the synthetic entry names it
    assert "内部不一致裁决说明" in internal_sec
    assert "**内部不一致裁决说明**（REQ-C" in internal_sec
    assert "；涉及：REQ-C" in internal_sec
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="en")
    assert "**Paper internal inconsistencies**" in en
    assert "internal-inconsistency adjudication" in en
    assert "; involves: REQ-C" in en
    assert "裁决性质声明" not in en   # old hardcoded zh title gone


def test_internal_findings_bullets_name_their_requirements(tmp_path):
    """Every internal-inconsistency bullet states which requirement it
    annotates, so the reader can pair badge <-> explanation without hunting
    (evaluation rule: annotate AND explain, explicitly)."""
    md = build_human_summary(_make_project(tmp_path),
                             generated_at="2026-01-01T00:00:00Z", language="zh")
    internal_sec = md.split("**论文内部不一致**")[1].split("**其他观察**")[0]
    assert "DEC-1" in internal_sec and "；涉及：REQ-A" in internal_sec
    en = build_human_summary(_make_project(tmp_path),
                             generated_at="2026-01-01T00:00:00Z", language="en")
    assert "DEC-1" in en and "(DEC-1; involves: REQ-A)" in en
    assert "involves: REQ-A" in en   # en bullet names its requirement too


def test_zh_label_body_with_source_refs(tmp_path):
    """A project ``decision-labels.zh.json`` turns a finding into a Chinese
    one-paragraph bullet in the zh render, with prose provenance (出处:
    page/figure refs) in the id parenthetical -- while the verbatim English
    rationale stays untouched in the en render (project data is audit-original)."""
    root = _make_project(tmp_path)
    with open(root + "/decisions/DEC-1.json", "w", encoding="utf-8") as fh:
        json.dump({
            "decision_id": "DEC-1", "decision_type": "GOAL_REVIEW",
            "affected_refs": ["REQ-A"],
            "rationale": "text vs figure intercept inconsistent (Fig. 5, p.4); "
                         "figure variant wins.",
        }, fh)
    with open(root + "/decision-labels.zh.json", "w", encoding="utf-8") as fh:
        json.dump({
            "DEC-1": "校准方程正文与图5不一致：正文 p.4 引用 y=0.0927x-1.1268，"
                     "图5 印刷为 y=0.0927x-1.2168；独立重拟支持图5（正文 p.4 视为笔误）。",
        }, fh)
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    assert "**校准方程正文与图5不一致" in md
    assert "（DEC-1；出处：p.4；图5；涉及：REQ-A）" in md
    internal_sec = md.split("**论文内部不一致**")[1].split("**其他观察**")[0]
    assert "figure variant wins" not in internal_sec  # no English tail
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="en")
    assert "(DEC-1; source: p.4; Fig. 5; involves: REQ-A)" in en
    assert "figure variant wins" in en  # authored record intact for en


def test_internal_finding_without_affected_refs_raises(tmp_path):
    """A rendered internal-inconsistency finding that names no affected
    requirement can never be badged -- the builder must refuse rather than
    silently render half a pair (whole-consistency guarantee)."""
    root = _make_project(tmp_path)
    with open(root + "/decisions/DEC-1.json", "w", encoding="utf-8") as fh:
        json.dump({
            "decision_id": "DEC-1", "decision_type": "GOAL_REVIEW",
            "affected_refs": [],   # finding without a target
            "rationale": "text vs figure intercept inconsistent; figure variant wins.",
        }, fh)
    with pytest.raises(SummaryConsistencyError) as ei:
        build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                            language="zh")
    assert "DEC-1" in str(ei.value) and "affected_refs" in str(ei.value)


def test_heading_clipped_at_word_boundary():
    """Derived headings are never sliced mid-word: Latin clips at word
    boundaries with an ellipsis, CJK at the char limit (G-fix v0.3.1)."""
    from scientific_reproduction.reporting.human_summary import _clip_heading, _item_title
    long = ("Calibration equation: the equation printed INSIDE Fig. 5 reads "
            "y = 0.0927x - 1.2168 with R2 = 0.9787 (OCR conf 0.97); the main "
            "text (p.4) quotes y = 0.0927x - 1.1268.")
    title = _item_title({"rationale": long}, "发现")
    assert title == "Calibration equation: the equation printed…"
    # authored summary is the last-resort source when rationale is empty
    summary_t = _item_title({"rationale": "", "summary": long}, "发现")
    assert summary_t.startswith("Calibration equation: the equation printed")
    assert summary_t.endswith("…")
    # single long Latin word degrades to a hard clip, never a crash
    assert _clip_heading("x" * 50) == "x" * 42 + "…"
    # CJK headings are shorter and char-clipped
    assert (_clip_heading("这是一个用于测试中文标题截断逻辑的句子句子", 12)
            == "这是一个用于测试中文标题…")


def test_requirement_rationale_falls_back_to_goal_review(tmp_path):
    """A requirement without a REQUIREMENT_CLOSURE record is justified from
    its linked GOAL_REVIEW verdicts (summary, else rationale lead clause)
    instead of the unrecorded placeholder (G-fix v0.3.1)."""
    import os as _os
    root = _make_project(tmp_path)
    _os.remove(root + "/decisions/DEC-CLOSE-C.json")
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    appendix = md.split("## 附录 A：需求裁决详情")[1]
    assert "max error 16.99% exceeds the 15% claim" in appendix
    row = [l for l in md.split(chr(10)) if l.startswith("| REQ-C ")]
    assert row and "max error 16.99%" in row[0]
    # REQ-B has no decision at all -> the explicit placeholder stays
    assert "未记录裁决理由" in md


def test_foreign_note_section_level_when_all_english(tmp_path):
    """All-foreign sections emit one section-level localization note; the
    per-item note is gone inside that section (G-fix v0.3.1). Mixed groups
    elsewhere (e.g. appendix A) keep per-item notes (evaluation P2)."""
    md = build_human_summary(_make_project(tmp_path),
                             generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "（注：本节条目均以英文撰写，未随交付语言本地化）" in md
    findings = md.split("## 发现与确认")[1].split("## 假设与边界")[0]
    assert "（注：本条以英文撰写，未随交付语言本地化）" not in findings


def test_foreign_note_per_item_when_mixed(tmp_path):
    """A mixed zh/en group keeps the per-item note and no section note."""
    import json as _j
    from pathlib import Path
    root = _make_project(tmp_path)
    (Path(root) / "assumptions" / "ASM-2.json").write_text(_j.dumps({
        "assumption_id": "ASM-2", "classification": "A2_SCIENTIFIC_ASSUMPTION",
        "rationale": "合成光谱仅用于演示建模路径，不构成数据级复现。",
    }), encoding="utf-8")
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    limits_sec = md.split("## 假设与边界")[1].split("##")[0]
    assert "（注：本条以英文撰写，未随交付语言本地化）" in limits_sec
    assert "本节条目均以英文撰写" not in limits_sec


def test_conclusion_counts_inconclusive(tmp_path):
    """INCONCLUSIVE requirements are counted and named in the conclusion so
    the latest report no longer reads '6 claims: 5 hold, 0 fail' (G-fix
    v0.3.1)."""
    import json as _j
    from pathlib import Path
    root = _make_project(tmp_path)
    (Path(root) / "requirements" / "REQ-D.json").write_text(_j.dumps({
        "requirement_id": "REQ-D", "statement": "field error claims",
        "criticality": "CRITICAL", "goal_ids": [],
        "outcome": "INCONCLUSIVE",
    }), encoding="utf-8")
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    concl = md.split("## 产出覆盖矩阵")[0]
    assert "、1 项无法判定（REQ-D）" in concl
    assert "REQ-D：" in concl
    # no inconclusive -> the previous head shape is preserved
    md2 = build_human_summary(_make_project(Path(tmp_path) / "plain"),
                              generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "项无法判定" not in md2.split("## 产出覆盖矩阵")[0]


def test_note_cell_does_not_open_on_good_news():
    """A ❌/❓ row rationale must not be cut at a benign first clause
    ('Field readings fully reproduced ...'); the full rationale carries the
    verdict content, while reproduced rows keep the natural lead clause
    (evaluation P0)."""
    from scientific_reproduction.reporting.human_summary import _note_cell
    bad = {"outcome": "NOT_REPRODUCED",
           "rationale": "Field readings fully reproduced as described; however "
                        "the claimed accuracy band is exceeded (16.99% vs 15%)."}
    note = _note_cell(bad, "en")
    assert note.startswith("Field readings fully reproduced as described")
    assert "however" in note
    ok = {"outcome": "REPRODUCED",
          "rationale": "Field readings fully reproduced as described; mean "
                       "error 0.8% within band."}
    lead = _note_cell(ok, "en")
    assert lead.startswith("Field readings fully reproduced")
    assert "mean error" not in lead
    # no rationale recorded -> explicit fallback, never an empty cell
    assert _note_cell({"outcome": "INCONCLUSIVE", "rationale": ""}, "en") \
        .startswith("No rationale recorded")


def test_matrix_duplicate_rationale_tagged(tmp_path):
    """Two requirements justified by the same decision keep one full
    rationale and a '同 REQ-x 的裁决理由' pointer on the second row
    (evaluation P3)."""
    import json as _j
    import os as _os
    from pathlib import Path
    root = _make_project(tmp_path)
    _os.remove(root + "/decisions/DEC-CLOSE-C.json")  # REQ-C joins the fallback
    (Path(root) / "requirements" / "REQ-E.json").write_text(_j.dumps({
        "requirement_id": "REQ-E", "statement": "repeat REQ-C",
        "criticality": "REQUIRED", "goal_ids": [],
        "outcome": "NOT_REPRODUCED",
    }), encoding="utf-8")
    dec2 = _j.loads((Path(root) / "decisions" / "DEC-2.json").read_text(encoding="utf-8"))
    dec2["affected_refs"] = ["REQ-C", "REQ-E"]
    (Path(root) / "decisions" / "DEC-2.json").write_text(_j.dumps(dec2), encoding="utf-8")
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    rows = [l for l in md.split(chr(10)) if l.startswith("| REQ-" )]
    assert len([r for r in rows if "同 REQ-C 的裁决理由" in r]) == 1
    # the first row keeps the plain full rationale, no pointer
    c_row = [r for r in rows if r.startswith("| REQ-C ")][0]
    assert "max error 16.99%" in c_row and "同 REQ" not in c_row


def test_conclusion_pointer_skips_already_named_decision(tmp_path):
    """The '此外 / Also' pointer is dropped when the finding's lead phrase is
    already covered by a requirement clause (evaluation P1); it stays when
    the finding is not otherwise named."""
    import os as _os
    from pathlib import Path
    root = _make_project(tmp_path)
    _os.remove(root + "/decisions/DEC-CLOSE-C.json")
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    concl = md.split("## 产出覆盖矩阵")[0]
    assert "此外，" not in concl
    md2 = build_human_summary(_make_project(Path(tmp_path) / "plain"),
                              generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "此外，" in md2.split("## 产出覆盖矩阵")[0]


def test_conclusion_pointer_dropped_when_decision_named(tmp_path):
    """The pointer is also dropped when the reason clause quotes the finding's
    *decision id* even if its title phrase is not repeated (evaluation P1)."""
    import json as _j
    import os as _os
    from pathlib import Path
    root = _make_project(tmp_path)
    _os.remove(root + "/decisions/DEC-CLOSE-C.json")
    dec2 = _j.loads((Path(root) / "decisions" / "DEC-2.json").read_text(encoding="utf-8"))
    dec2["summary"] = ("Field readings fully reproduced; error claims fail - "
                       "requirement rated INCONCLUSIVE (DEC-2)")
    (Path(root) / "decisions" / "DEC-2.json").write_text(_j.dumps(dec2), encoding="utf-8")
    md = build_human_summary(root, generated_at="2026-01-01T00:00:00Z", language="zh")
    assert "此外，" not in md.split("## 产出覆盖矩阵")[0]


def test_conclusion_demo_clause_exact_zh_and_en(tmp_path):
    """The synthetic-demo clause agrees in number: zh keeps its form, a
    single EN demo reads 'is a synthetic demo' (evaluation P1 wording)."""
    import json as _j
    from pathlib import Path
    root = _make_project(tmp_path)
    (Path(root) / "assumptions" / "ASM-2.json").write_text(_j.dumps({
        "assumption_id": "ASM-2", "classification": "A2_SCIENTIFIC_ASSUMPTION",
        "strict_status_effect": "DISQUALIFIES_PURE_STRICT",
        "affected_goal_ids": ["GOAL-TEST-B"],
    }), encoding="utf-8")
    zh = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    assert "其中 REQ-B 为合成演示（A2，非数据级复现）" in zh
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="en")
    assert "REQ-B is a synthetic demo" in en


def test_decision_zh_labels_preferred_zh_only(tmp_path):
    """decision-labels.zh.json (same pattern as goal-labels.zh.json) feeds
    zh titles/rationales; appendix A keeps the authored record verbatim and
    the EN render ignores the file (evaluation P2-G, iteration 2)."""
    import json as _j
    import os as _os
    from pathlib import Path
    root = _make_project(tmp_path)
    _os.remove(root + "/decisions/DEC-CLOSE-C.json")  # REQ-C falls back to DEC-2
    (Path(root) / "decision-labels.zh.json").write_text(_j.dumps({
        "_note": "zh one-liners",
        "DEC-2": "最大误差 16.99% 超出 15% 声称，限于名义值无法判定。",
    }), encoding="utf-8")
    zh = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    concl = zh.split("## 产出覆盖矩阵")[0]
    assert "超出 15% 声称" in concl          # zh label replaces the EN clause
    matrix = zh.split("## 产出覆盖矩阵")[1]
    assert "超出 15% 声称" in matrix          # matrix note is zh too
    appendix = zh.split("## 附录 A：需求裁决详情")[1]
    assert "max error 16.99%" in appendix    # verbatim record kept for audit
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="en")
    assert "超出 15% 声称" not in en
    assert "max error 16.99%" in en


def test_first_sentence_ignores_paren_semicolons():
    """A parenthesized ';' (10 samples, 3 replicates; ...) never terminates
    the sentence -- no more interior cut without an ellipsis (P2-E)."""
    from scientific_reproduction.reporting.human_summary import _first_sentence
    t = ("Reproduce the bench calibration curve (Fig. 5) of the optical "
         "sensor (10 samples, 3 replicates; 10 calibration levels) and "
         "verify the printed equation.")
    s = _first_sentence(t, 400)
    assert s == t
    assert "replicates;" in s
    assert "10 calibration levels" in s


def test_en_render_never_leaks_cjk_labels(tmp_path):
    """EN rendering falls back to the metric key for CJK-only labels and
    uses EN markers ('(none)', EN QC/result-notes headers) (P2-F)."""
    root = _make_project(tmp_path)
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="en")
    assert "校准方程斜率" not in en
    assert "slope" in en                   # metric key fallback
    assert "（无）" not in en
    assert "(none)" in en              # empty ctx table -> EN none marker (P2-F)
    assert "Process / QC checks" not in en  # fixture has no QC rows either
    assert "**Per-goal result notes**" in en


def test_en_render_ascii_only_for_qc_gates_findings(tmp_path):
    """Renderer-generated punctuation is ASCII in EN while zh keeps
    fullwidth separators: QC rows (Yes/No, '; rule:', ASCII parens), human
    gates ('; affected:', ': '), finding/assumption id parens.  Data-level
    CJK (authored rationales) is out of scope; this fixture is EN-authored."""
    import json as _j
    import os as _os
    from pathlib import Path
    root = _make_project(tmp_path)
    # strictly EN-authored closure (the stock fixture carries a zh fragment)
    _os.remove(root + "/decisions/DEC-CLOSE-C.json")
    (Path(root) / "decisions" / "DEC-CLOSE-C.json").write_text(_j.dumps({
        "decision_id": "DEC-CLOSE-C", "decision_type": "REQUIREMENT_CLOSURE",
        "affected_refs": ["REQ-C"],
        "rationale": "claim contradicted by its own data; not a reproduction bias.",
    }), encoding="utf-8")
    # QC / procedural row on GOAL-TEST-B (no claim -> not the core table)
    p = root + "/runs/run-1/GOAL-TEST-B.json"
    rec = _j.load(open(p, encoding="utf-8"))
    rec.setdefault("metrics", []).append({
        "metric": "bands_verbatim", "kind": "procedural", "value": "True",
        "band": {"rule_text": "table text equals the paper's Table 1 rows"},
    })
    (Path(root) / "runs" / "run-1" / "GOAL-TEST-B.json").write_text(
        _j.dumps(rec), encoding="utf-8")
    # a human gate (EVIDENCE_INTERPRETATION_GATE)
    (Path(root) / "human-gates").mkdir(exist_ok=True)
    (Path(root) / "human-gates" / "GATE-1.json").write_text(_j.dumps({
        "gate_id": "GATE-1", "gate_type": "EVIDENCE_INTERPRETATION_GATE",
        "status": "OPEN", "trigger": "figure 5 marker labeling",
        "affected_refs": ["REQ-A"], "default_safe_action": "use printed rule",
    }), encoding="utf-8")
    en = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="en")
    # renderer fragments must be ASCII
    assert "Yes" in en                                  # boolean QC value
    assert "是" not in en                               # zh boolean not used
    assert "; rule: table text equals" in en            # rule separator
    assert "- **GATE-1**(" in en                        # ASCII parens after id
    assert "affected: REQ-A" in en                      # ASCII sep + EN label
    assert "；" not in en and "（" not in en and "）" not in en and "：" not in en
    # zh keeps fullwidth in the same render
    zh = build_human_summary(root, generated_at="2026-01-01T00:00:00Z",
                             language="zh")
    assert "；" in zh
    assert "GATE-1**（" in zh                              # fullwidth parens
