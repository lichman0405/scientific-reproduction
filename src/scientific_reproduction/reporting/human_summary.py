"""Human-readable reproduction summary (Markdown + PDF) derived from state.

v2 (2026-09-03, target-user review): restructured for a first-time reader
who does not know the paper or the skill:

* two-level verdicts are separated (requirement-level vs metric-level) and
  never mixed on one line or one count;
* a derived executive summary states what did NOT reproduce and why;
* the metric table is split into a core claim-vs-value table (metrics that
  carry a ``claim``, grouped by goal) and a context table (metrics without
  a claim, appendix, explicitly not adjudicating);
* core rows show 偏差 (value - claim) and 单位 columns;
* acceptance bands are listed verbatim from the frozen acceptance records;
* the requirement table gains a one-sentence "paper claim" column (from the
  evidence registry) and an A2 (synthetic-demo) badge when a requirement's
  goals reference an A2 assumption;
* findings are grouped by a leading rationale tag 【推翻声称】/【内部不一致】/
  【结构确认】/【观察】 (missing tag -> 观察);
* a "复核与重跑" section tells the reader how to verify the numbers.

Design rules (unchanged from v1, aligned with the locked architecture):

* **State-derived, never hand-written**: every figure is read from the
  registered state (requirements + closure outcomes, run result packages,
  evidence, decisions, assumptions, acceptance records, manifests). No
  project-specific data.
* **Metrics-first, finding-fallback**: a goal without structured metrics
  degrades to its ``finding`` text (context table, never guessed).
* **Findings section filtered**: governance/closure decisions never
  surface as paper findings (U7); findings use ``GOAL_REVIEW``.
* **Deterministic**: ``generated_at`` is injected; identical state renders
  byte-identical output (both formats).
* **Language**: template packs ``zh`` / ``en``; unknown keys fall back to
  ``en``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SUMMARY_FILENAME = "复现结果摘要.md"      # zh default; overridable per call
SUMMARY_PDF_FILENAME = "复现结果摘要.pdf"


class SummaryConsistencyError(ValueError):
    """The summary state cannot pair an internal-inconsistency annotation with
    its explanation.

    Evaluation rule: every paper-internal contradiction that is badged on a
    requirement row must ALSO be explained in the findings section, and every
    internal-inconsistency finding must be linked to the requirement it
    affects. A summary must never silently render half a pair -- fixing this
    belongs in the data (Supervisor), not in the renderer.
    """

# ---------------------------------------------------------------------------
# Template packs (zh / en). Keep minimal and stable.
# ---------------------------------------------------------------------------
_TEMPLATES: dict[str, dict[str, str]] = {
    "zh": {
        "title": "复现结果摘要",
        "paper_label": "论文",
        "generated_suffix": "生成于",
        "exec_title": "执行摘要",
        "conclusion_para": "一句话结论",
        "matrix_title": "产出覆盖矩阵（论文各项可复现声明）",
        "matrix_legend": "判定：✅ 复现 / ❓ 无法判定 / ❌ 未复现 = 需求级综合裁决（相对冻结验收带）。"
                         "说明列含代表数字；完整裁决理由见附录 A，口径与验收带见附录 B。",
        "matrix_col_req": "需求",
        "matrix_col_claim": "论文声称",
        "matrix_col_result": "结果",
        "matrix_col_note": "复现说明",
        "internal_badge": "（⚠️ 论文内部不一致，见『发现与确认』）",
        "synthetic_internal_title": "内部不一致裁决说明",
        "involves_label": "涉及",
        "source_label": "出处",
        "evidence_title": "关键数值证据",
        "appendix_a": "附录 A：需求裁决详情",
        "appendix_b": "附录 B：判定口径与验收带（详细）",
        "guide_title": "判定说明",
        "conclusion_title": "复现结论",
        "conclusion_scope": "指标级计数只覆盖核心对照表；上下文指标不参与判定。",
        "core_title": "核心对照表（与论文声称对照的指标）",
        "ctx_title": "上下文指标（不参与判定）",
        "ctx_note": "以下指标无论文声称值，属上下文/灵敏度信息，不进入判定计数；未提供中文 label 的条目按结果包中的 metric key 显示。",
        "table_col_metric": "指标",
        "table_col_claim": "论文声称",
        "table_col_value": "复现值",
        "table_col_delta": "偏差",
        "table_col_unit": "单位",
        "table_col_verdict": "判定",
        "req_legend": "判定：✅ 复现 / ❌ 未复现 = 需求级综合裁决（相对冻结验收带）。"
                      "下方“核心对照表”中 ⚠️/❌ 表示单个数值相对论文声称的偏离——"
                      "个别数值偏离声称而需求仍判 ✅，是验收带含数字化容差所致。",
        "see_finding": "见说明",
        "req_table_title": "需求级结果",
        "req_col_req": "需求",
        "req_col_claim": "论文声称（一句话）",
        "req_col_outcome": "结果",
        "req_col_reason": "裁决理由",
        "demo_badge": "（合成演示 A2）",
        "problems_title": "发现与确认",
        "problems_none": "未登记问题发现（无 decision 记录；无记录不等于无问题）。",
        "grp_claim": "影响声称的发现",
        "grp_internal": "论文内部不一致",
        "grp_struct": "结构确认",
        "grp_obs": "其他观察",
        "limits_title": "假设与边界",
        "limits_none": "无。",
        "gates_title": "人工确认项",
        "gates_note": "需要人工裁决、已记录在案的事项（通常为证据解读歧义）："
                      "OPEN 项按记录的默认动作继续执行，结题前须人工确认；"
                      "确认后在此保留最终裁决供审计。",
        "gates_none": "无。",
        "gate_type_resource": "资源",
        "gate_type_access": "访问",
        "gate_type_safety": "安全",
        "gate_type_scope": "范围",
        "gate_type_termination": "终止",
        "gate_type_external_contact": "外部联系",
        "gate_type_evidence_interpretation": "证据解读",
        "gate_status_open": "待确认",
        "gate_status_approved": "已批准",
        "gate_status_rejected": "已否决",
        "gate_status_resolved": "已解决",
        "gate_status_cancelled": "已取消",
        "gates_default_label": "默认动作",
        "gates_resolution_label": "答复",
        "gates_affected_label": "受影响",
        "uncertainty_header": "结果不确定度(来自结果包 uncertainty 字段):",
        "status_warning_header": "status 告警:",
        "verify_title": "复核与重跑",
        "deliverables_title": "交付物位置",
        "dlv_note": "路径均相对本摘要所在文件夹（除非注明绝对路径）。",
        "dlv_group_report": "报告",
        "dlv_group_compute": "计算结果",
        "dlv_group_evidence": "计算证据留档",
        "dlv_group_knowledge": "知识工件",
        "dlv_group_other": "其他",
        "qc_header": "**过程自检与记录项**（QC/流程指标，不构成论文数值对照；"
                     "✅ = 满足冻结的内部规则，❌ = 内部 QC 未达标但已记录为限制，"
                     "· = 已记录）：",
        "result_notes_header": "**各目标结果说明**（无结构化指标的目标）：",
        "qc_rule_label": "规则",
        "none_marker": "（无）",
        "footer": "本摘要由 Supervisor 基于已注册状态自动派生;审计版报告见 reproduction-report.pdf。",
    },
    "en": {
        "title": "Reproduction Summary",
        "paper_label": "Paper",
        "generated_suffix": "generated",
        "exec_title": "Executive summary",
        "conclusion_para": "One-sentence conclusion",
        "matrix_title": "Reproduction matrix (each formal claim of the paper)",
        "matrix_legend": "✅ reproduced / ❓ inconclusive / ❌ not reproduced = "
                         "requirement-level verdict "
                         "(against the frozen acceptance band). The note column carries "
                         "representative numbers; full rationales in Appendix A, verdict "
                         "semantics and bands in Appendix B.",
        "matrix_col_req": "Requirement",
        "matrix_col_claim": "Paper claim",
        "matrix_col_result": "Result",
        "matrix_col_note": "Reproduction note",
        "internal_badge": " (paper-internal inconsistency, see findings)",
        "synthetic_internal_title": "internal-inconsistency adjudication",
        "involves_label": "involves",
        "source_label": "source",
        "evidence_title": "Key numbers (evidence)",
        "appendix_a": "Appendix A: requirement adjudication details",
        "appendix_b": "Appendix B: verdict semantics and acceptance bands",
        "guide_title": "How to read the verdicts",
        "conclusion_title": "Conclusion",
        "conclusion_scope": "Metric-level counts cover the core claim table only; context metrics do not adjudicate.",
        "core_title": "Core claim-vs-value table (metrics compared to a paper claim)",
        "ctx_title": "Context metrics (not adjudicating)",
        "ctx_note": "These metrics carry no paper claim; they are context/sensitivity values and never counted in the verdicts.",
        "table_col_metric": "Metric",
        "table_col_claim": "Paper claim",
        "table_col_value": "Reproduced",
        "table_col_delta": "Delta",
        "table_col_unit": "Unit",
        "table_col_verdict": "Verdict",
        "req_legend": "✅ reproduced / ❌ not reproduced = requirement-level verdict "
                      "(against the frozen acceptance band). ⚠️/❌ in the core table "
                      "below are single-value deviations from the paper claim — a "
                      "value may deviate while its requirement stays ✅ because the "
                      "band includes digitization tolerance.",
        "see_finding": "see finding",
        "req_table_title": "Requirement-level results",
        "req_col_req": "Requirement",
        "req_col_claim": "Paper claim (one line)",
        "req_col_outcome": "Outcome",
        "req_col_reason": "Rationale",
        "demo_badge": " (synthetic demo A2)",
        "problems_title": "Findings and confirmations",
        "problems_none": "No decision records (absence of records is not absence of issues).",
        "grp_claim": "Findings that affect the claims",
        "grp_internal": "Paper internal inconsistencies",
        "grp_struct": "Structural confirmations",
        "grp_obs": "Other observations",
        "limits_title": "Assumptions and boundaries",
        "limits_none": "None.",
        "gates_title": "Human confirmation items",
        "gates_note": "Recorded items awaiting the human's decision (usually an "
                      "ambiguous digitized reading): OPEN gates continued "
                      "under the recorded default action and must be confirmed "
                      "before close-out; resolutions stay here for the audit "
                      "trail.",
        "gates_none": "None.",
        "gate_type_resource": "resource",
        "gate_type_access": "access",
        "gate_type_safety": "safety",
        "gate_type_scope": "scope",
        "gate_type_termination": "termination",
        "gate_type_external_contact": "external contact",
        "gate_type_evidence_interpretation": "evidence interpretation",
        "gate_status_open": "open",
        "gate_status_approved": "approved",
        "gate_status_rejected": "rejected",
        "gate_status_resolved": "resolved",
        "gate_status_cancelled": "cancelled",
        "gates_default_label": "default action",
        "gates_resolution_label": "resolution",
        "gates_affected_label": "affected",
        "uncertainty_header": "Result uncertainty (from result-package uncertainty fields):",
        "status_warning_header": "status warnings:",
        "verify_title": "Verification and rerun",
        "deliverables_title": "Deliverables",
        "dlv_note": "Paths are relative to this summary's folder unless absolute.",
        "dlv_group_report": "Reports",
        "dlv_group_compute": "Result packages",
        "dlv_group_evidence": "Digitization evidence",
        "dlv_group_knowledge": "Knowledge artifacts",
        "dlv_group_other": "Other",
        "qc_header": "**Process / QC checks** (process metrics, not a paper "
                     "value comparison; ✅ = frozen internal rule met, "
                     "❌ = internal QC failed but recorded as a limit, "
                     "· = recorded):",
        "result_notes_header": "**Per-goal result notes** (goals without "
                               "structured metrics):",
        "qc_rule_label": "rule",
        "none_marker": "(none)",
        "footer": "Derived automatically from registered state; the audit report is reproduction-report.pdf.",
    },
}


def _tpl(language: str, key: str) -> str:
    return _TEMPLATES.get(language, _TEMPLATES["en"])[key]


def _fmt(v: Any) -> str:
    """Format a metric value for display; strings pass through."""
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return f"{v:.8g}"  # keeps significant digits (no .2f truncation)
    return str(v)


_STATUS_ICONS = {
    "exact": "✅",
    "within_tolerance": "⚠️",
    "over_claim": "❌",
}

# Keyword-based group matching (zh + en), order matters: claim first, then
# internal, then structural; unmatched tags land in observations.  Keyword
# sets are chosen so the groups do not share discriminating terms.
_GROUP_KEYWORDS = {
    "grp_claim": ["推翻", "refut", "exceed", "over claim", "not reproduced",
                  "claim fails", "claim contradict", "falsif", "超声称", "超.*声称",
                  "缺乏", "不构成", "缺乏数据", "缺乏支撑"],
    "grp_internal": ["内部不一致", "inconsist", "mismatch", "internal contradict",
                     "self-contradict", "text.*fig", "vs.*fig", "正文与图注",
                     "矛盾", "笔误", "疑似笔误"],
    "grp_struct": ["结构确认", "confirm", "verified", "reproduc", "consistent",
                   "一致", "吻合", "支持图注", "确认"],
}
_GROUP_ORDER = ("grp_claim", "grp_internal", "grp_struct")
_DEFAULT_GROUP = "grp_obs"
import re as _re


def _strip_lead_tag(rationale: str) -> str:
    """Remove the 【...】 grouping tag from a rationale for display."""
    t = (rationale or "").strip()
    if t.startswith("【") and "】" in t:
        return t.split("】", 1)[1].strip()
    return t


def _group_key(rationale: str) -> str:
    """Deterministic finding group.

    Keyword matching (zh terms verbatim, lower-cased otherwise) runs on the
    lead tag 【...】 when present, and falls back to the first 160 chars of
    the rationale itself so untagged findings still group sensibly instead
    of silently landing in observations."""
    head = (rationale or "").strip()
    if head.startswith("【"):
        head = head.split("】", 1)[0].lstrip("【").strip().lower()
    else:
        head = " ".join(head.split()).lower()
    for group in _GROUP_ORDER:
        for kw in _GROUP_KEYWORDS[group]:
            if _re.search(kw, head):
                return group
    return _DEFAULT_GROUP


def _verdict_icon(verdict: str, status: str | None = None) -> str:
    """Map an acceptance verdict (+ optional metric status) to a glyph."""
    if status in _STATUS_ICONS:
        return _STATUS_ICONS[status]
    if status is not None:
        # Unknown status values degrade to a blank verdict; the caller
        # records a warning so the drift stays visible (G1).
        return ""
    return {"PASS": "✅", "FAIL": "❌"}.get(verdict, verdict)


def _verdict_label(language: str, outcome: str) -> str:
    """Map a requirement outcome onto a short human verdict."""
    zh = {
        "REPRODUCED": "✅ 复现",
        "REPRODUCED_WITH_RECOVERY": "⚠️ 基本复现(有修正)",
        "NOT_REPRODUCED": "❌ 未复现",
        "INCONCLUSIVE": "❓ 无法判定",
    }
    en = {
        "REPRODUCED": "reproduced",
        "REPRODUCED_WITH_RECOVERY": "reproduced w/ recovery",
        "NOT_REPRODUCED": "not reproduced",
        "INCONCLUSIVE": "inconclusive",
    }
    return (zh if language == "zh" else en).get(outcome, outcome)


# ---------------------------------------------------------------------------
# State readers (small, deterministic, defensive)
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _iter_goal_results(root: Path):
    """Yield (goal_id, result_dict) for every run result package found."""
    run_dirs = [p for p in (root / "runs").iterdir() if p.is_dir()]
    for d in sorted(run_dirs):
        for p in sorted(d.glob("GOAL-*.json")):
            rec = _read_json(p)
            if rec and rec.get("goal_id"):
                yield rec["goal_id"], rec


def _requirements(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "requirements").glob("*.json")):
        rec = _read_json(p)
        if rec:
            out.append(rec)
    return out


def _decisions(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "decisions").glob("*.json")):
        rec = _read_json(p)
        if rec:
            out.append(rec)
    return out


def _assumptions(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "assumptions").glob("*.json")):
        rec = _read_json(p)
        if rec:
            out.append(rec)
    return out


def _human_gates(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "human-gates").glob("*.json")):
        rec = _read_json(p)
        if rec:
            out.append(rec)
    return out


def _gate_type_label(language: str, gate_type: str) -> str:
    """Human label for a gate type; unknown types degrade to the raw value.

    The template keys use the bare classifier (``gate_type_resource``),
    while enum values carry the ``_GATE`` suffix
    (``RESOURCE_GATE``) -- strip it before the lookup.
    """
    pack = _TEMPLATES.get(language, _TEMPLATES["en"])
    return pack.get("gate_type_" + gate_type.lower().removesuffix("_gate"), gate_type)


def _gate_status_label(language: str, status: str) -> str:
    """Human label for a gate status; unknown statuses degrade to raw."""
    pack = _TEMPLATES.get(language, _TEMPLATES["en"])
    return pack.get("gate_status_" + status.lower(), status)


def _goals(root: Path, language: str = "zh") -> dict[str, dict]:
    """Goal records; zh renderings prefer the project-level label map
    ``<root>/goal-labels.zh.json`` (goal_id -> Chinese title) and fall back
    to the registered ``title`` when absent (v2/P19, language consistency)."""
    labels: dict[str, str] = {}
    if language == "zh":
        rec = _read_json(root / "goal-labels.zh.json")
        if rec:
            labels = {k: str(v) for k, v in rec.items() if not k.startswith("_")}
    out: dict[str, dict] = {}
    for p in sorted((root / "goals").glob("*.json")):
        rec = _read_json(p)
        if rec and rec.get("goal_id"):
            gid = rec["goal_id"]
            if gid in labels:
                rec = dict(rec, title=labels[gid], _zh_label=labels[gid])
            out[gid] = rec
    return out


def _acceptances(root: Path) -> list[dict]:
    out = []
    for p in sorted((root / "acceptance").glob("*.json")):
        rec = _read_json(p)
        if rec:
            out.append(rec)
    return out


# Governance / closure decision types are NOT paper findings (U7).
_NON_FINDING_TYPES = {
    "REQUIREMENT_CLOSURE", "PROJECT_OUTCOME", "PLAN_FREEZE",
    "GOAL_REVISION", "ACCEPTANCE_REVISION", "ANALYSIS_PROTOCOL_REVISION",
    "RESEARCH_REQUEST", "RECOVERY_ENTRY", "METHOD_REDESIGN_ENTRY",
    "HUMAN_GATE_OPEN",
}


_NO_SPLIT_BEFORE = ("Fig", "vs", "e.g", "i.e", "No", "pp", "et al")


def _first_sentence(text: str, limit: int = 160) -> str:
    """First sentence of a rationale/finding, robust to 'Fig. 5'-style
    abbreviations; extends to a second chunk when the first is too short."""
    t = (text or "").replace("\n", " ").strip()
    if not t:
        return ""
    stops = [i for i in (t.find("。"),) if i > 0]  # ";" never terminates a
    # sentence -- (10 samples, 3 replicates; ...) must not split mid-parens
    # '. ' breaks that are not abbreviations (skip 'Fig. ', 'vs. ', ...)
    idx = t.find(". ")
    while idx > 0:
        prev = t[max(0, idx - 8):idx].strip()
        if not any(prev.endswith(tok) or prev.endswith(tok + ".") for tok in _NO_SPLIT_BEFORE):
            stops.append(idx)
            break
        idx = t.find(". ", idx + 1)
    cut = min(stops or [len(t)])
    out = t[: cut + 1].strip()
    if len(out) < 20 and cut < len(t):          # too short -> second chunk
        out = t[: t.find(". ", cut + 1) + 1].strip() if t.find(". ", cut + 1) > 0 else out
    return out[:limit].rstrip()


def _evidence_claim_for(root: Path, rid: str) -> str:
    """One-sentence paper-claim text for a requirement (evidence-based)."""
    want = "CLAIM-" + rid.removeprefix("REQ-")
    best, fallback = None, None
    for p in sorted((root / "evidence").glob("*.json")):
        rec = _read_json(p)
        if not rec:
            continue
        used = rec.get("used_by") or []
        if rid not in used:
            continue
        finding = str(rec.get("finding") or "")
        snippet = _first_sentence(finding, 140)
        if rec.get("claim_id") == want:
            best = snippet
            break
        if fallback is None:
            fallback = snippet
    return (best or fallback or "").strip()


def _a2_flag_for(root: Path, req: dict, goals: dict[str, dict],
                 warnings: list | None = None) -> tuple[bool, list[str]]:
    """Whether the requirement's goals rest on a *synthetic-substitute* A2
    assumption (strict_status_effect == DISQUALIFIES_PURE_STRICT).

    Links come from the assumptions' own ``affected_goal_ids`` (the
    authored, precise mapping) intersected with the requirement's goals
    (inverse of goals[].requirement_ids; requirement.goal_ids as
    fallback).  A2 assumptions with effect STRICT_WITH_ASSUMPTIONS
    (e.g. integer-rounding reference values) are boundary notes, not
    synthetic demos, and never earn the badge.

    Conservative fallback (evaluation pass, 2026-09-05): an A2 assumption
    linked to a ``SIM-*`` goal that carries NO ``strict_status_effect`` is
    treated as disqualifying-synthetic and the inference is recorded in
    ``warnings`` -- the demo badge must not silently vanish when authors
    omit the field."""
    rid = req.get("requirement_id", "")
    all_a2 = [a for a in _assumptions(root)
              if a.get("classification") == "A2_SCIENTIFIC_ASSUMPTION"]
    linked: list[str] = []
    for g in goals.values():
        if rid in (g.get("requirement_ids") or []):
            linked.append(g.get("goal_id"))
    if not linked:
        linked = list(req.get("goal_ids") or [])
    flagged: list[str] = []
    for a in all_a2:
        if a.get("strict_status_effect") != "DISQUALIFIES_PURE_STRICT":
            continue
        affected = set(a.get("affected_goal_ids") or [])
        if affected & set(linked):
            flagged.append(a.get("assumption_id"))
    if not flagged:
        sim_goal = next((g for g in linked if "SIM-" in g), None)
        if sim_goal:
            for a in all_a2:
                affected = set(a.get("affected_goal_ids") or [])
                linked_match = (affected & set(linked)) or (not affected and not linked)
                if linked_match and a.get("strict_status_effect") is None:
                    flagged.append(a.get("assumption_id"))
                    if warnings is not None:
                        warnings.append(
                            f"{a.get('assumption_id')}: A2 assumption on synthetic"
                            f" goal {sim_goal} lacks strict_status_effect;"
                            " DISQUALIFIES_PURE_STRICT inferred from the goal name")
    return bool(flagged), sorted(flagged)


def _decision_labels(root: Path, language: str) -> dict[str, str]:
    """Optional project-level zh one-liners for decisions
    (``decision-labels.zh.json``, decision_id -> label) so zh deliverables
    can describe English-authored rationales in Chinese without touching
    the frozen decision schema (same pattern as ``goal-labels.zh.json``).

    English renderings ignore the file entirely."""
    if language != "zh":
        return {}
    rec = _read_json(root / "decision-labels.zh.json")
    if not rec:
        return {}
    return {str(k): str(v) for k, v in rec.items() if not k.startswith("_")}


def _sep_zh(language: str) -> str:
    """Hierarchy separator: fullwidth in zh, ASCII in en -- the EN render
    never emits fullwidth punctuation (evaluation P2-F)."""
    return "；" if language == "zh" else "; "


def _sep_col(language: str) -> str:
    return "：" if language == "zh" else ": "


def _paren(language: str, text: str) -> str:
    """Parenthesize for the render language."""
    return f"（{text}）" if language == "zh" else f"({text})"


def _rationale_for_requirement(rid: str, decisions: list[dict],
                               goal_ids: list | tuple = (),
                               labels: dict[str, str] | None = None,
                               language: str = "zh") -> str:
    """Closure rationale recorded for a requirement.

    Prefers the REQUIREMENT_CLOSURE decision whose affected_refs name the
    requirement.  When no such record exists the linked GOAL_REVIEW
    verdicts are used (their refs may name the requirement or its goals) --
    each contributes its authored ``summary`` one-liner when present, else
    the rationale's lead clause, joined with "；".  Returns "" when nothing
    was recorded; callers render the explicit placeholder.

    ``labels`` (zh) replaces an authored English text with the project's
    Chinese rendering of the same decision when one is registered."""
    for dec in decisions:
        if dec.get("decision_type") == "REQUIREMENT_CLOSURE":
            refs = dec.get("affected_refs") or []
            if rid in refs and str(dec.get("rationale") or "").strip():
                if labels and dec.get("decision_id") in labels:
                    return labels[str(dec.get("decision_id"))]
                return str(dec.get("rationale") or "")
    goals = set(goal_ids or [])
    clauses: list[str] = []
    for dec in decisions:
        if dec.get("decision_type") != "GOAL_REVIEW":
            continue
        refs = dec.get("affected_refs") or []
        if rid in refs or goals & set(refs):
            text = (labels.get(str(dec.get("decision_id"))) or ""
                    if labels is not None else "")
            if not text:
                text = str(dec.get("summary") or "").strip()
            if not text:
                text = _lead_clause(str(dec.get("rationale") or ""), 160)
            if text:
                clauses.append(text)
    return _sep_zh(language).join(clauses)


def _pairing_invariants(d: dict, warnings: list[str]) -> None:
    """标注-详述配对 invariant: annotation and explanation must be a PAIR.

    Hard rules (raise ``SummaryConsistencyError`` naming the offending
    record -- never silently render half a pair):

    * A rendered internal-inconsistency finding must carry at least one
      ``affected_refs`` entry, else the findings section explains a
      contradiction no requirement row annotates (no badge possible).
    * A requirement row that shows the internal-inconsistency badge must
      have its finding rendered in the findings section (guards the block
      above: synthetic entries must be spliced into the group too).

    Soft rule (``warnings``): an internal finding whose refs match no closed
    requirement is reported as a status warning -- it is still rendered (a
    paper problem stays a problem) but no row is badged for it."""
    rendered = {str(x.get("decision_id") or "") for x in d["problems_grouped"].get("grp_internal", [])}
    rendered |= {str(x.get("decision_id") or "") for x in d.get("cross_internal", []) or []}
    rendered.discard("")
    closed_ids = {r["requirement_id"] for r in d["closed_reqs"]}
    for x in d["problems_grouped"].get("grp_internal", []):
        did = str(x.get("decision_id") or "(unnamed)")
        refs = [str(v) for v in (x.get("affected_refs") or []) if v]
        if not refs:
            raise SummaryConsistencyError(
                f"internal-inconsistency finding {did} has no affected_refs: "
                "a paper-internal contradiction must be annotated on the "
                "requirement it affects (record the affected_refs)")
        if not any(ref in closed_ids for ref in refs):
            warnings.append(
                f"finding {did}: affected refs {refs} match no closed "
                "requirement -- the finding is rendered but no requirement "
                "row is badged for it (pairing warning)")
    for r in d["closed_reqs"]:
        for fnd in r.get("internal_findings") or []:
            did = str(fnd.get("decision_id") or "")
            if did and did not in rendered:
                raise SummaryConsistencyError(
                    f"{r['requirement_id']} renders the internal-inconsistency "
                    f"badge for {did}, but no explanation for it appears in "
                    "the findings section")


# ---------------------------------------------------------------------------
# Shared collection (rendering-neutral)
# ---------------------------------------------------------------------------
def _collect(root: Path, language: str) -> dict:
    """Collect all summary data from the registered state.

    Pure function of the state: both renderers (Markdown, PDF) consume this
    so the two formats always describe the same numbers.
    """
    project = _read_json(root / "project.yaml") or {}
    target = project.get("primary_target", {}) or {}
    title = target.get("title") or project.get("title") or "(unknown)"
    doi = target.get("doi")

    reqs = _requirements(root)
    closed = [r for r in reqs if r.get("outcome") not in (None, "OPEN")]
    outcomes: dict[str, int] = {}
    for r in closed:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    goals = _goals(root, language)

    status_warnings: list[str] = []
    uncertainties: list[str] = []
    core_rows: list[dict] = []       # metrics WITH a claim (adjudicating)
    ctx_rows: list[dict] = []        # metrics WITHOUT claim + finding-fallback
    core_counts = {"exact": 0, "within_tolerance": 0, "over_claim": 0}
    process_rows: list[dict] = []
    result_notes: list[dict] = []
    label_by_metric: dict[str, str] = {}
    for goal_id, result in sorted(_iter_goal_results(root)):
        unc = result.get("uncertainty") or {}
        if unc and isinstance(unc, dict):
            notes = [str(v) for v in unc.values() if isinstance(v, str) and v]
            if notes:
                uncertainties.append(f"**{goal_id}** — " +
                                     _sep_zh(language).join(notes[:2]))
        gtitle = goals.get(goal_id, {}).get("title") or goal_id
        gshort = goal_id.replace("GOAL-", "", 1)
        metrics = result.get("metrics") or []
        if not metrics:
            result_notes.append({"goal_id": goal_id, "goal_title": gtitle,
                                 "text": str(result.get("finding") or "")[:240]})
            continue
        for m in metrics:
            name = (m.get("label_zh") if language == "zh" and m.get("label_zh")
                    else (m.get("label") or m.get("metric", "")))
            if language == "en" and any("一" <= c <= "鿿" for c in name):
                name = m.get("metric") or name  # EN render never leaks CJK labels
            claim = m.get("claim")
            value = m.get("value")
            unit = m.get("unit") or ""
            status = m.get("status")
            verdict = str(m.get("verdict") or "")
            # v3.1: verdict-carrying rows without an explicit status (worker
            # packages commonly omit it) must not silently fall out of the
            # adjudicating table into the context dump: derive the status
            # from the frozen band comparison (mechanical verdict).
            status_derived = status is None
            if status_derived:
                status = {"in_band": "within_tolerance",
                          "out_of_band": "over_claim"}.get(verdict)
            icon = _verdict_icon(
                result.get("acceptance", {}).get("verdict", ""), status)
            if status is not None and status not in _STATUS_ICONS:
                icon = {"PASS": "✅", "FAIL": "❌"}.get(
                    result.get("acceptance", {}).get("verdict", ""), "")
                status_warnings.append(
                    f"{goal_id}/{name}: unrecognized status {status!r} "
                    "treated as exact")
            row = {
                "goal_id": goal_id, "goal_title": gtitle,
                "name": name, "unit": unit, "icon": icon,
                "status": status or "",
                "metric_key": m.get("metric", ""),
            }
            label_by_metric[m.get("metric", "")] = name
            kind = str(m.get("kind") or "")
            value_numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            # v3.1: percentage claims ("<15%") compared against a normalized
            # ratio (0..~2, e.g. 0.1699) are rescaled to percent so the row
            # reads 16.99 % vs <15 % with a real delta, not a unit clash.
            pct_claim: float | None = None
            if isinstance(claim, str):
                _pm = _re.match(r"^\s*[<>≤≥]?\s*([0-9]+(?:\.[0-9]+)?)\s*%\s*$", claim)
                if _pm and value_numeric and abs(float(value)) < 2:
                    pct_claim = float(_pm.group(1))
                    value = float(value) * 100.0
                    unit = unit if "%" in unit else "%"
            elif isinstance(claim, (int, float)) and not isinstance(claim, bool):
                pct_claim = float(claim)
            procedural = (kind == "procedural"
                          or (kind == "context" and not isinstance(value, (int, float)))
                          or (kind != "claim" and kind != "context"
                              and isinstance(value, (str, bool)))
                          or (kind == "" and claim is None and isinstance(value, (str, bool))))
            adjudicated = (kind in ("", "claim") and claim is not None
                           and status in _STATUS_ICONS
                           and (kind == "claim" or value_numeric))
            if adjudicated:
                row["claim"] = _fmt(claim)
                row["value"] = _fmt(value) if value is not None else ""
                # signed delta when both sides are numeric (percentage
                # claims compare against the parsed numeric bound)
                try:
                    target = pct_claim if pct_claim is not None else float(claim)
                    delta = float(value) - target
                    if abs(delta) < 5e-7:
                        row["delta"] = "0"
                    else:
                        sign = "+" if delta > 0 else ""
                        row["delta"] = sign + f"{delta:.6f}".rstrip("0").rstrip(".")
                    # exactness refinement (skill semantics: deviation <= the
                    # last printed digit of the claim counts as exact): apply
                    # only to statuses derived mechanically from in_band.
                    if status_derived and status == "within_tolerance":
                        numeric_claim = (pct_claim is not None) or (
                            isinstance(claim, (int, float)) and not isinstance(claim, bool))
                        if numeric_claim:
                            # ulp from the *displayed* claim precision
                            # (trimmed: 522 / 174 / 1, not 522.0 / 174.0)
                            sval = _fmt(pct_claim if pct_claim is not None else claim)
                            nd = len(sval.split(".")[1]) if "." in sval else 0
                            ulp = 0.5 * 10.0 ** (-nd)
                            if abs(delta) <= ulp:
                                status = "exact"
                                icon = _verdict_icon(
                                    result.get("acceptance", {}).get("verdict", ""), status)
                    row["status"] = status
                except (TypeError, ValueError):
                    row["delta"] = ""
                    row["status"] = status
                if status == "within_tolerance":
                    core_counts["within_tolerance"] += 1
                elif status == "over_claim":
                    core_counts["over_claim"] += 1
                elif status == "exact":
                    core_counts["exact"] += 1
                # icon/unit may have been refined after row creation
                # (exactness refinement, percentage rescaling)
                row["icon"] = icon
                row["unit"] = unit
                core_rows.append(row)
            elif procedural:
                # process/boolean records (delivered / recorded / yes-no /
                # QC self-checks against an internal rule): shown as a
                # checklist, never as a fake claim-vs-value comparison
                val = m.get("value")
                shown = ({"True": "是", "False": "否", "true": "是", "false": "否"}
                         if language == "zh" else
                         {"True": "Yes", "False": "No", "true": "Yes", "false": "No"}
                         ).get(str(val), str(val))
                band = m.get("band") or {}
                rule = str(band.get("rule_text") or "") if isinstance(band, dict) else ""
                note = str(m.get("note") or "")
                process_rows.append({
                    "goal_id": goal_id, "goal_short": gshort,
                    "name": name, "value": shown, "status": status or "",
                    "rule": rule, "note": note,
                })
            else:
                # reference-only target / non-adjudicated claim -> context
                row["claim"] = ""
                row["value"] = _fmt(value) if value is not None else ""
                row["delta"] = ""
                ctx_rows.append(row)

    # acceptance bands for goals appearing in the core table (verbatim rules)
    core_goal_ids = {r["goal_id"] for r in core_rows}
    band_lines: list[str] = []
    for acc in _acceptances(root):
        gid = str(acc.get("goal_id") or "")
        if gid not in core_goal_ids:
            continue
        for crit in acc.get("criteria") or []:
            mkey = str(crit.get("metric") or "")
            rule = str(crit.get("rule") or "")
            if not rule:
                continue
            shown = label_by_metric.get(mkey, mkey)
            band_lines.append(f"- `{acc.get('acceptance_id', gid)}` — {shown}: {rule}")

    decisions = _decisions(root)
    problems = [
        d for d in decisions
        if d.get("rationale") and d.get("decision_type") not in _NON_FINDING_TYPES
    ]
    grouped: dict[str, list[dict]] = {}
    for d in problems:
        grouped.setdefault(_group_key(str(d.get("rationale") or "")), []).append(d)
    # zh decision labels provide human-first titles/short renderings of
    # English-authored decisions (findings rows, rationale strings).
    decision_labels = _decision_labels(root, language)
    for d in problems:
        if not d.get("title") and decision_labels.get(str(d.get("decision_id"))):
            d["title"] = decision_labels[str(d.get("decision_id"))]

    # cross-group references into the internal-inconsistency group: a finding
    # whose affected requirement carries an internal-nature closure appears
    # under "论文内部不一致" as a "另见" pointer (P24e).
    internal_nature_reqs = {
        r.get("requirement_id") for r in closed
        if r.get("outcome") == "NOT_REPRODUCED"
        and _is_internal_nature(_rationale_for_requirement(
            r.get("requirement_id", ""), decisions, r.get("goal_ids"),
            language=language))
    }
    def _states_claim_conflict(dec) -> bool:
        t = _strip_lead_tag(str(dec.get("rationale") or "")).lower()
        return bool(_re.search(r"超.*声称|与.*声称|exceed.*claim|claim.*(refut|contradict|fail)",
                               t)) or _is_internal_nature(t)
    cross_internal = [
        d for d in problems
        if d not in grouped.get("grp_internal", [])
        and _states_claim_conflict(d)
        and any(rid in internal_nature_reqs for rid in (d.get("affected_refs") or []))
    ]

    assumptions = _assumptions(root)
    limits = [
        a for a in assumptions
        if a.get("classification") == "A2_SCIENTIFIC_ASSUMPTION"
    ]

    human_gates = []
    for g in _human_gates(root):
        human_gates.append({
            "gate_id": str(g.get("gate_id") or ""),
            "gate_type": str(g.get("gate_type") or ""),
            "status": str(g.get("status") or ""),
            "trigger": _clean_reason(g.get("trigger")),
            "affected_refs": ", ".join(
                str(x) for x in (g.get("affected_refs") or [])),
            "default_safe_action": str(g.get("default_safe_action") or ""),
            "resolution_note": str(g.get("resolution_note") or ""),
        })

    deliverables = []
    for p in sorted((root / "manifests").glob("*.json")):
        rec = _read_json(p)
        if rec and rec.get("uri"):
            uri = _rel(str(rec["uri"]).replace("\\", "/"), root)
            parts = uri.split("/")
            # converge deep compute trees to their goal/work dir
            if len(parts) > 2 and parts[0] == "compute" and parts[1] not in ("goals", "run"):
                uri = f"compute/{parts[1]}/"
            elif len(parts) > 2 and parts[0] == "compute" and parts[1] in ("goals", "run") and len(parts) > 3:
                uri = f"compute/{parts[1]}/{parts[2]}/"
            if uri not in deliverables:
                deliverables.append(uri)
    for name in ("reproduction-report.pdf", "reproduction-audit-package.json"):
        if (root / "reports" / name).exists():
            deliverables.append(f"reports/{name}")

    closed_reqs = []
    for r in closed:
        demo, asm_ids = _a2_flag_for(root, r, goals, warnings=status_warnings)
        rid = r.get("requirement_id", "?")
        internal_hits = [d for d in grouped.get("grp_internal", [])
                         if rid in (d.get("affected_refs") or [])]
        raw_rationale = _rationale_for_requirement(
            rid, decisions, r.get("goal_ids"), language=language)
        rationale = _rationale_for_requirement(
            rid, decisions, r.get("goal_ids"),
            decision_labels if language == "zh" else None,
            language=language)
        if not internal_hits and r.get("outcome") == "NOT_REPRODUCED"                 and _is_internal_nature(raw_rationale):
            # pairing guarantee: the badge and its explanation must both be
            # rendered -- the synthetic entry goes into the findings group,
            # not only behind the badge (annotation without explanation).
            internal_hits = [{
                "decision_id": rid,
                "title": _tpl(language, "synthetic_internal_title"),
                "rationale": rationale,
                "affected_refs": [rid],
            }]
            grouped.setdefault("grp_internal", []).append(internal_hits[0])
        closed_reqs.append({
            "requirement_id": rid,
            "outcome": r.get("outcome"),
            "method": r.get("method_reproducibility") or "",
            # rationale: zh-rendered when labels exist; rationale_full keeps
            # the authored record verbatim (appendix A stays audit-faithful)
            "rationale": rationale,
            "rationale_full": raw_rationale,
            "statement": _statement_for(r, language, root, rid),
            "statement_full": _statement_raw(r, language),
            "demo": demo, "demo_assumptions": asm_ids,
            "internal_findings": internal_hits,
        })

    d = dict(
        title=title, doi=doi, outcomes=outcomes,
        core_rows=core_rows, ctx_rows=ctx_rows, process_rows=process_rows,
        result_notes=result_notes,
        core_counts=core_counts, band_lines=band_lines,
        status_warnings=status_warnings, uncertainties=uncertainties,
        problems_grouped=grouped, problems=problems, cross_internal=cross_internal,
        limits=limits,
        deliverables=deliverables, closed_reqs=closed_reqs,
        human_gates=human_gates,
        decision_labels=decision_labels,
    )
    _pairing_invariants(d, status_warnings)
    return d


def _clean_reason(x) -> str:
    """Single-line rationale for table cells (no escape pitfalls)."""
    return str(x or "").replace(chr(10), " ").strip()


def _clip_heading(text: str, limit: int = 42, cjk_limit: int | None = None) -> str:
    """Clip a heading at a script-appropriate boundary: CJK text is cut at
    ``cjk_limit`` characters when given (heading cells pass 24; ``None``
    falls back to ``limit``), Latin text at the last word boundary inside
    the limit; an ellipsis marks every cut, so no mid-word truncation
    occurs for either script."""
    t = (text or "").strip()
    if not t:
        return ""
    if len(t) <= limit:
        return t
    if any("一" <= c <= "鿿" for c in t):
        return t[: (cjk_limit if cjk_limit else limit)] + "…"
    window = t[:limit]
    space = window.rfind(" ")
    if space > limit * 0.4:  # keep a meaningful prefix, not a stub
        rest = t[limit:]
        ns = rest.find(" ")
        if 0 <= ns <= 5:  # short trailing word completes the boundary
            return t[:limit + ns].rstrip() + "…"
        return window[:space].rstrip(".,:;—–") + "…"
    return window + "…"


def _clip_prose(text: str, limit: int) -> str:
    """Word-boundary clip for prose cells (Latin) / character clip (CJK);
    an ellipsis marks every cut -- identical boundary rules to
    ``_clip_heading`` but no CJK heading budget."""
    t = (text or "").strip()
    if not t or len(t) <= limit:
        return t
    if any("一" <= c <= "鿿" for c in t):
        return t[:limit] + "…"
    window = t[:limit]
    space = window.rfind(" ")
    if space > limit * 0.4:
        rest = t[limit:]
        ns = rest.find(" ")
        if 0 <= ns <= 5:
            return t[:limit + ns].rstrip() + "…"
        return window[:space].rstrip(".,:;—–") + "…"
    return window + "…"


def _item_title(rec: dict, fallback_text: str) -> str:
    """Human-first title: authored title, else the rationale's own lead
    text clipped at word boundaries, else the authored ``summary``
    one-liner (clipped); never the bare machine id as the heading."""
    t = (rec.get("title") or "").strip()
    if t:
        return t
    head = _clip_heading(_strip_lead_tag(str(rec.get("rationale") or "")),
                         cjk_limit=24)
    if head:
        return head
    summary = _clip_heading(str(rec.get("summary") or ""), cjk_limit=24)
    if summary:
        return summary
    return fallback_text


def _is_foreign(rationale: str) -> bool:
    """Heuristic: mostly non-CJK prose -> flag as not localized for zh."""
    t = (rationale or "").strip()
    if not t:
        return False
    n = max(1, len(t))
    cjk = sum(1 for ch in t if "一" <= ch <= "鿿")
    return cjk / n < 0.08 and any(ch.isalpha() for ch in t)


def _section_foreign_note(items: list[dict], language: str,
                          labels: dict | None = None) -> str:
    """Section-level localization note: when *every* item of a rendered
    group is shown in a foreign language, one line replaces the noisy
    per-item notes; a mixed group keeps the per-item marking. An item
    carrying a project zh label is rendered in Chinese -- it is not
    foreign, whatever language its authored rationale came in."""
    if language != "zh" or not items:
        return ""
    labels = labels or {}

    def _shown_foreign(it: dict) -> bool:
        if labels.get(str(it.get("decision_id") or "")):
            return False
        return _is_foreign(_strip_lead_tag(str(it.get("rationale") or "")))

    if all(_shown_foreign(it) for it in items):
        return "（注：本节条目均以英文撰写，未随交付语言本地化）"
    return ""


def _item_note(rationale: str, language: str) -> str:
    """Per-item localization note for a mixed (zh/en) group."""
    if language == "zh" and _is_foreign(rationale):
        return "（注：本条以英文撰写，未随交付语言本地化）"
    return ""


def _source_refs(dec: dict, *texts: str) -> list[str]:
    """Deterministic source pointers for a finding (原文出处).

    Author-provided ``page_refs`` (optional extra field on the decision
    record; the schema allows additional properties) wins. Otherwise the
    page pass (``p.4`` / ``page 4`` / ``第4页``) runs first, then the
    figure/table pass (``Fig. 5`` / ``图5`` / ``Table 1`` / ``表1`` over
    each text in order), then the next text; same-number duplicates keep
    the first-seen form, order is stable. Pass the zh label first so its
    own notation (``图5``) wins over the rationale's (``Fig. 5``)."""
    refs: list[str] = []
    for r in (dec.get("page_refs") or []):
        r = str(r).strip()
        if r and r not in refs:
            refs.append(r)
    if refs:
        return refs

    def _ref_key(ref: str) -> tuple:
        """Canonical (kind, number) so 图5 == Fig. 5 and 表1 == Table 1."""
        m = _re.match(r"\s*([A-Za-z一-鿿]+)\s*\.?\s*(\d+)", ref)
        if not m:
            return (str(ref).lower().strip(), 0)
        kind = {"图": "fig", "表": "table"}.get(m.group(1).lower(), m.group(1).lower())
        return (kind, int(m.group(2)))

    def _add(ref: str) -> None:
        # same number in another notation (图5 vs Fig. 5) is one reference
        same = next((x for x in refs if _ref_key(x) == _ref_key(ref)), None)
        if same is None:
            refs.append(ref)

    for t in (texts or ("",)):
        t = t or ""
        for m in _re.finditer(r"\bp\.\s*(\d+)\b|\bpage\s+(\d+)\b|第\s*(\d+)\s*页", t):
            _add(f"p.{m.group(1) or m.group(2) or m.group(3)}")
        for m in _re.finditer(r"\bfig(?:ure)?[. ]*(\d+)", t, flags=_re.I):
            _add(f"Fig. {m.group(1)}")
        for m in _re.finditer(r"图\s*(\d+)", t):
            _add(f"图{m.group(1)}")
        for m in _re.finditer(r"\btable\s*(\d+)", t, flags=_re.I):
            _add(f"Table {m.group(1)}")
        for m in _re.finditer(r"表\s*(\d+)", t):
            _add(f"表{m.group(1)}")
    return refs


def _goal_display(g: dict, language: str) -> str:
    """Group heading: zh prefers the authored label file, then any title
    that is already Chinese, and only then falls back to the goal id
    (short) -- never an unlocalized English title by default."""
    if g.get("_zh_label"):
        return g["_zh_label"]
    short = g.get("goal_id", "").replace("GOAL-", "", 1) or g.get("goal_id", "")
    title = g.get("goal_title") or ""
    if language == "zh":
        if title and any("一" <= c <= "鿿" for c in title):
            return title
        return f"目标 {short}" if short else (title or short)
    return title or short


def _bands_md(d: dict, language: str) -> list[str]:
    zh = ["## 附录：验收带（机器原文，仅列出出现在核心对照表中的目标）", ""]
    en = ["## Appendix: acceptance bands (verbatim; goals present in the core table)", ""]
    head = zh if language == "zh" else en
    if not d["band_lines"]:
        return head + [_tpl(language, "none_marker"), ""]
    return head + [b for b in d["band_lines"]] + [""]


def _statement_raw(r: dict, language: str) -> str:
    """Full unclipped requirement claim text (zh preference) -- kept verbatim
    for appendix A; the matrix cell uses the clipped ``_statement_for``."""
    if language == "zh" and r.get("statement_zh"):
        return str(r.get("statement_zh"))
    return str(r.get("statement") or "")


def _statement_for(r: dict, language: str, root, rid: str) -> str:
    """One-line requirement content for the matrix cell: zh prefers the
    optional authored statement_zh, then the registered statement, then
    evidence -- clipped at a word boundary with an ellipsis, never a
    mid-word cut."""
    raw = (r.get("statement_zh") if language == "zh" else "") or r.get("statement") or ""
    first = _first_sentence(str(raw), 400)
    return _clip_prose(first, 140) or _evidence_claim_for(root, rid)


def _lead_clause(text: str, limit: int = 220) -> str:
    """First natural clause of a rationale (cut at 。/；/newline, or EN
    ./;), capped by `limit`; never leaves a dangling ellipsis.
    Falls back to a hard clip with an explicit ellipsis marker."""
    t = (text or "").strip()
    if not t:
        return ""
    for sep in ("。", "；", "\n"):
        i = t.find(sep)
        if 0 < i <= limit:
            return t[:i]
    for sep in (". ", "; "):
        i = t.find(sep)
        if 0 < i <= limit:
            return t[:i]
    return t[:limit] if len(t) <= limit else t[: limit - 1] + "…"


def _note_cell(r: dict, language: str) -> str:
    """Rationale cell of the reproduction matrix.

    An ❌/❓ row must open on the actual verdict content, not on a benign
    first clause ("Field readings fully reproduced" before the failure):
    those rows use the full rationale clipped at a word/sentence boundary
    (evaluation pass, 2026-09-05). Reproduced rows keep the natural first
    clause -- the good news IS the verdict. Missing rationale uses the
    explicit fallback."""
    raw = (r.get("rationale") or "").replace("\n", " ").strip()
    if not raw:
        return _reason_fallback(language)
    if r["outcome"] in ("NOT_REPRODUCED", "INCONCLUSIVE"):
        return _clip_prose(raw, 180)
    return _lead_clause(raw, 180)


def _matrix_note_cells(d: dict, language: str) -> list[str]:
    """One matrix note per closed requirement; identical rationales are
    preserved on the first row and tagged "同 <REQ> 的裁决理由" on later rows
    (the audit must not hide that a rationale is shared by two verdicts)."""
    fb = _reason_fallback(language)
    seen: dict[str, str] = {}
    out: list[str] = []
    for r in d["closed_reqs"]:
        base = _note_cell(r, language)
        if base in seen and base != fb:
            dup_of = seen[base]
            out.append((f"同 {dup_of} 的裁决理由；" if language == "zh"
                        else f"same rationale as {dup_of}; ") + base)
        else:
            if base not in seen:
                seen[base] = r["requirement_id"]
            out.append(base)
    return out


def _claim_text(r: dict, language: str) -> str:
    """Requirement claim text: Chinese rendering preferred when
    the deliverable language is zh (records may carry a
    `statement_zh` translation alongside the canonical English
    `statement`)."""
    if language == "zh" and r.get("statement_zh"):
        return str(r.get("statement_zh"))
    return str(r.get("statement") or "")


def _cut(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars with an ellipsis when longer."""
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"



def _esc_md(s) -> str:
    """Escape markdown table pipes inside cell text."""
    return str(s).replace("|", chr(92) + "|")



def _conclusion_text(d: dict, language: str, root) -> str:
    """One-sentence conclusion (v3/v4): derived by default; an optional
    project-level ``summary-override.json`` with conclusion_zh/conclusion_en
    replaces the whole sentence.  The derived text stays short: counts
    (reproduced / not reproduced / inconclusive), one clause per open-ended
    requirement, and a pointer to the findings."""
    rec = _read_json(Path(root) / "summary-override.json") or {}
    key = "conclusion_zh" if language == "zh" else "conclusion_en"
    override = (rec.get(key) or "").strip()
    if override:
        return override
    total = sum(d["outcomes"].values())
    ok = d["outcomes"].get("REPRODUCED", 0) + d["outcomes"].get(
        "REPRODUCED_WITH_RECOVERY", 0)
    no = d["outcomes"].get("NOT_REPRODUCED", 0)
    inc = d["outcomes"].get("INCONCLUSIVE", 0)
    bad = [r for r in d["closed_reqs"] if r["outcome"] == "NOT_REPRODUCED"]
    unknown = [r for r in d["closed_reqs"] if r["outcome"] == "INCONCLUSIVE"]
    if language == "zh":
        head = (f"论文 {total} 项可复现声明中 {ok} 项成立、{no} 项不成立"
                f"（{', '.join(r['requirement_id'] for r in bad) or '无'}）")
        if inc:
            head += (f"、{inc} 项无法判定"
                     f"（{', '.join(r['requirement_id'] for r in unknown) or '无'}）")
        pieces = [head]
        for r in bad:
            reason = (r["rationale"] or "").replace("\n", " ")
            reason = reason if reason.strip() else _reason_fallback(language)
            pieces.append(f"{r['requirement_id']}：{_clip_prose(reason, 220)}")
        for r in unknown:
            reason = (r["rationale"] or "").replace("\n", " ")
            reason = reason if reason.strip() else _reason_fallback(language)
            pieces.append(f"{r['requirement_id']}：{_clip_prose(reason, 220)}")
        flagged = []
        for group in ("grp_claim", "grp_internal"):
            flagged += d["problems_grouped"].get(group, [])
        if flagged:
            _append_pointer(language, pieces, flagged)
    else:
        head = (f"Of the paper's {total} reproducible claims, {ok} hold and "
                f"{no} do not ({', '.join(r['requirement_id'] for r in bad) or 'none'})")
        if inc:
            head += (f", with {inc} inconclusive "
                     f"({', '.join(r['requirement_id'] for r in unknown) or 'none'})")
        pieces = [head]
        for r in bad:
            reason = (r["rationale"] or "").replace("\n", " ")
            pieces.append(f"{r['requirement_id']}: "
                          f"{_clip_prose(reason or _reason_fallback(language), 220)}")
        for r in unknown:
            reason = (r["rationale"] or "").replace("\n", " ")
            pieces.append(f"{r['requirement_id']}: "
                          f"{_clip_prose(reason or _reason_fallback(language), 220)}")
        flagged = []
        for group in ("grp_claim", "grp_internal"):
            flagged += d["problems_grouped"].get(group, [])
        if flagged:
            _append_pointer(language, pieces, flagged)
    demo = [r for r in d["closed_reqs"] if r["demo"]]
    if demo:
        ids = ", ".join(sorted(r["requirement_id"] for r in demo))
        if language == "zh":
            pieces.append("其中 " + ids + " 为合成演示（A2，非数据级复现）")
        else:
            verb = "is a synthetic demo (A2, not data-level)" \
                if len(demo) == 1 else "are synthetic demos (A2, not data-level)"
            pieces.append(f"{ids} {verb}")
    # strip sentence-final punctuation from each piece so the join never
    # yields the "。；" artifact; EN renders with Western punctuation
    # (evaluation pass, 2026-09-05)
    join_sep, tail = ("；", "。") if language == "zh" else ("; ", ".")
    return join_sep.join(p.rstrip("。.") for p in pieces) + tail


def _append_pointer(language: str, pieces: list[str], flagged: list[dict]) -> None:
    """Add the '此外 / Also' pointer to the finding section, unless the
    finding's lead phrase is already covered by a requirement clause above
    (the pointer must not duplicate a verdict already named in the reason
    clauses)."""
    title = _item_title(flagged[0], "发现" if language == "zh" else "finding")
    base = title.replace("…", "").replace("...", "").rstrip(".,;:—– ").strip()
    rest = " ".join(pieces[1:])
    # a lead phrase too short to be informative is never a duplicate signal
    if len(base) >= 10 and base in rest:
        return
    did = str(flagged[0].get("decision_id") or "")
    if len(did) >= 4 and did in rest:
        return
    flag = "此外，" if language == "zh" else "Also, "
    tail = ("（详见『发现与确认』）" if language == "zh"
            else " (see 'Findings and confirmations')")
    pieces.append(flag + title + tail)



def _is_internal_nature(text) -> bool:
    """Whether a closure rationale declares the mismatch as the paper's own
    (claim contradicted by the paper itself), driving the internal badge.
    Authors writing the closure in English must get the same badge: the
    English phrases match the respective zh terms ("internal inconsistency"
    / "own published data" / "self-contradictory / self-contradiction")."""
    t = (text or "")
    return any(k in t for k in (
        "论文内部不一致", "自身公布数据", "自身数据", "自相矛盾",
        "internal inconsistency", "internal contradict",
        "self-contradict", "own published data", "own data"))


def _reason_fallback(language: str) -> str:
    return ("未记录裁决理由；相关发现见『发现与确认』。"
            if language == "zh"
            else "No rationale recorded; see 'Findings and confirmations'.")


def _rel(uri: str, root: Path) -> str:
    """Prefer folder-relative paths for deliverables under the workspace."""
    rp = str(root).replace("\\", "/")
    if uri.startswith(rp):
        return uri[len(rp):].lstrip("/")
    return uri


def _deliverable_groups(deliverables, language):
    """Group deliverables by uri prefix into labelled buckets (H2)."""
    order = [
        ("reports/", _tpl(language, "dlv_group_report")),
        ("compute/output/", _tpl(language, "dlv_group_compute")),
        ("knowledge/", _tpl(language, "dlv_group_knowledge")),
        ("compute/", _tpl(language, "dlv_group_evidence")),
    ]
    remaining = list(deliverables)
    groups = []
    for prefix, label in order:
        bucket = [x for x in remaining if x.startswith(prefix)]
        remaining = [x for x in remaining if x not in bucket]
        if bucket:
            groups.append((label, bucket))
    if remaining:
        groups.append((_tpl(language, "dlv_group_other"), remaining))
    return groups


# ---------------------------------------------------------------------------
# Finding bullets (shared md / pdf)
# ---------------------------------------------------------------------------
def _finding_meta(dec: dict, language: str, d: dict, group_key: str) -> tuple:
    """Per-finding bullet pieces: (zh label body or None, rationale text,
    parenthetical). The parenthetical is ``（DEC-x；出处：p.4；图5；涉及：REQ-y）``
    in zh and ``(DEC-x; source: p.4; Fig. 5; involves: REQ-y)`` in en --
    every pointer of a finding sits next to its id, so the reader does not
    open an appendix to learn the provenance of the finding or which frozen
    requirements it badges."""
    did = str(dec.get("decision_id") or "")
    base = str(dec.get("rationale") or "")
    rat = _strip_lead_tag(base)
    lbl = d["decision_labels"].get(did) if language == "zh" else None
    # source extraction prefers the zh label (written from the paper in
    # hand), then the authored rationale -- 图5 and Fig. 5 dedupe by number
    src = _source_refs(dec, *((lbl, base) if lbl else (base,)))
    parts = [did]
    if src:
        parts.append(_tpl(language, "source_label") + _sep_col(language)
                     + _sep_zh(language).join(src))
    if group_key == "grp_internal":
        reqs = [str(x) for x in (dec.get("affected_refs") or [])
                if str(x) in {r["requirement_id"] for r in d["closed_reqs"]}]
        if reqs:
            parts.append(_tpl(language, "involves_label") + _sep_col(language)
                         + _sep_zh(language).join(reqs))
    return lbl, rat, _paren(language, _sep_zh(language).join(parts))


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------
def _guide_lines(d: dict, language: str) -> list[str]:
    """Two-level verdict explanation placed before any table (zh/en)."""
    zh = [
        "**两级判定，先分清口径再读数：**",
        "",
        "1. **指标级**（核心对照表）— 相对**论文声称**："
        "✅ 精确（偏差 ≤ 声称值末位舍入半径）、⚠️ 容差内偏离声称、❌ 超出冻结验收带；",
        "2. **需求级**（产出覆盖矩阵“结果”列）— 相对**冻结验收带**的终裁：✅ 复现 / ❓ 无法判定 / ❌ 未复现。",
        "两级可以不同（例：某指标相对声称仅偏离 0.1% 属 ⚠️，但整条需求在带内仍判 ✅）。",
        "**冻结验收带**：逐条机器原文如下（仅列核心对照表涉及的目标）。",
    ]
    en = [
        "**Two verdict levels — read the right one first:**",
        "",
        "1. **Metric level** (core claim table) — against the **paper claim**: "
        "✅ exact (deviation within the claim's last printed digit), "
        "⚠️ within tolerance of the claim, ❌ outside the frozen band;",
        "2. **Requirement level** (the matrix “Result” column) — the final adjudication "
        "against the **frozen acceptance band**: ✅ reproduced / ❓ inconclusive / ❌ not reproduced.",
        "The two levels may differ (e.g. a metric 0.1% off the claim is ⚠️ "
        "while its requirement stays ✅ inside the band).",
        "**Frozen acceptance bands** (verbatim; goals present in the core table only):",
    ]
    tail_zh = [
        "**显示位数** = 结果包中值的精度；判定为精确的指标显示到能区分舍入的位数。",
        "**核心表收录规则** = 只收录论文有声称**且本运行已裁决（有 status）**的指标；"
        "仅对照未裁决、或目标值并非论文声称的数值一律放『上下文指标』表。",
        "**不一致的两种性质**：①论文内部不一致——声称与论文自身的另一处表述或其公布的数据"
        "相矛盾，属于论文自身的问题；②复现偏差——本复现数值对声称的偏离，量级小，"
        "源于印刷舍入与数字化精度，不构成对论文的否定。判定 ❌ 的需求，"
        "其根源性质会在裁决理由中点明。",
    ]
    tail_en = [
        "**Display precision** = the precision of the value in the result "
        "package; exactly-reproduced metrics show enough digits to resolve "
        "rounding.",
        "**Core-table rule** = only metrics that carry a paper claim **and** "
        "were adjudicated (have a status) enter the core table; reference-only "
        "values, or values whose target is not a paper claim, go to the "
        "context table.",
        "**Two kinds of mismatch**: ① paper-internal inconsistency — a claim "
        "contradicts another statement of the paper or data the paper itself "
        "published (a problem of the paper); ② reproduction deviation — the "
        "reproduced value differs from the claim at a small scale, from "
        "printing rounding and digitization precision, and does not refute "
        "the paper. For ❌ requirements the underlying kind is named in the "
        "rationale.",
    ]
    head, tail = (zh, tail_zh) if language == "zh" else (en, tail_en)
    return head + tail


def core_status_terms(d: dict, language: str = "zh") -> str:
    c = d["core_counts"]
    n = len(d["core_rows"])
    tail = f"（共 {n} 项）" if language == "zh" else f" ({n} total)"
    return f"{c['exact']} ✅ / {c['within_tolerance']} ⚠️ / {c['over_claim']} ❌{tail}"


def build_human_summary(
    root: str | Path,
    *,
    generated_at: str,
    language: str = "zh",
) -> str:
    """Render the human-readable reproduction summary as Markdown (v3).

    User-first order: one-sentence conclusion, reproduction matrix of the
    paper's formal claims, the paper's own problems, boundaries, then the
    numeric evidence; full rationales and verdict semantics in appendices."""
    root_path = Path(root)
    d = _collect(root_path, language)
    L = _tpl

    lines: list[str] = []
    lines.append(f"# {L(language, 'title')}")
    lines.append("")
    lines.append(f"*{L(language, 'generated_suffix')} {generated_at}*")
    lines.append("")
    lines.append(f"**{L(language, 'paper_label')}**: {d['title']}")
    if d["doi"]:
        lines.append(f"DOI: {d['doi']}")
    lines.append("")

    # ---- one-sentence conclusion -------------------------------------------
    lines.append(f"## {L(language, 'conclusion_para')}")
    lines.append("")
    lines.append(_conclusion_text(d, language, root_path))
    lines.append("")

    # ---- reproduction matrix -----------------------------------------------
    lines.append(f"## {L(language, 'matrix_title')}")
    lines.append("")
    lines.append(f"*{L(language, 'matrix_legend')}*")
    lines.append("")
    lines.append(
        f"| {L(language, 'matrix_col_req')} | {L(language, 'matrix_col_claim')} | "
        f"{L(language, 'matrix_col_result')} | {L(language, 'matrix_col_note')} |")
    lines.append("|---|---|---|---|")
    for r, note in zip(d["closed_reqs"], _matrix_note_cells(d, language)):
        outcome = _verdict_label(language, r["outcome"])
        if r["demo"]:
            outcome += L(language, "demo_badge")
        if r["internal_findings"]:
            outcome += L(language, "internal_badge")
        lines.append(
            f"| {r['requirement_id']} | {_esc_md(_claim_text(r, language))} | {outcome} | "
            f"{_esc_md(note)} |")
    lines.append("")

    # ---- the paper's own problems ------------------------------------------
    lines.append(f"## {L(language, 'problems_title')}")
    lines.append("")
    if d["problems"]:
        for group_key in ("grp_claim", "grp_internal", "grp_struct", "grp_obs"):
            items = d["problems_grouped"].get(group_key)
            if not items:
                continue
            lines.append(f"**{L(language, group_key)}**")
            group_note = _section_foreign_note(items, language, d["decision_labels"])
            if group_note:
                lines.append(group_note)
            for dec in items:
                lbl, rat, paren = _finding_meta(dec, language, d, group_key)
                note = "" if lbl else (
                    _item_note(rat, language) if not group_note else "")
                if lbl:
                    # zh-first: the authored one-paragraph zh description IS
                    # the bullet body; the verbatim record stays in appendix A
                    line = f"- **{lbl}**{paren}"
                else:
                    title = _item_title(dec, "发现" if language == "zh" else "finding")
                    line = (f"- **{title}**{paren}"
                            f"{'— ' if language == 'zh' else ' — '}{rat}{note}")
                lines.append(line)
            if group_key == "grp_internal" and d.get("cross_internal"):
                for dec in d["cross_internal"]:
                    title = _item_title(dec, "发现" if language == "zh" else "finding")
                    did = dec.get("decision_id", "")
                    src_grp = _group_key(str(dec.get("rationale") or ""))
                    src_label = L(language, src_grp) if src_grp in _TEMPLATES.get(
                        language, _TEMPLATES["en"]) else src_grp
                    lines.append(
                        (f"- 另见（性质同属论文内部不一致，详见『{src_label}』）："
                         f"**{title}**（{did}）" if language == "zh" else
                         f"- See also (same paper-internal inconsistency, in "
                         f"'{src_label}'): **{title}** ({did})"))
            lines.append("")
    else:
        lines.append(L(language, "problems_none"))
        lines.append("")

    # ---- assumptions & boundaries ------------------------------------------
    lines.append(f"## {L(language, 'limits_title')}")
    lines.append("")
    limits_note = _section_foreign_note(d["limits"], language)
    if limits_note:
        lines.append(limits_note)
    if d["limits"]:
        for a in d["limits"]:
            rat = str(a.get("rationale") or "")
            note = (_item_note(rat, language) if not limits_note else "")
            title = _item_title(a, "假设" if language == "zh" else "assumption")
            lines.append(
                f"- **{title}**{_paren(language, a.get('assumption_id', ''))}— "
                f"{rat}{note}")
    else:
        lines.append(L(language, "limits_none"))
    lines.append("")

    # ---- human confirmation gates ------------------------------------------
    lines.append(f"## {L(language, 'gates_title')}")
    lines.append("")
    lines.append(f"*{L(language, 'gates_note')}*")
    lines.append("")
    if d["human_gates"]:
        for g in d["human_gates"]:
            gsep = "— " if language == "zh" else " — "
            line = (f"- **{g['gate_id']}**"
                    f"{_paren(language, _gate_type_label(language, g['gate_type'])
                              + _sep_zh(language)
                              + _gate_status_label(language, g['status']))}"
                    f"{gsep}{g['trigger']}")
            if g["affected_refs"]:
                line += (f"{_sep_zh(language)}{L(language, 'gates_affected_label')}"
                         f"{_sep_col(language)}{g['affected_refs']}")
            line += (f"{_sep_zh(language)}{L(language, 'gates_default_label')}"
                     f"{_sep_col(language)}{g['default_safe_action'] or '—'}")
            if g["resolution_note"]:
                line += (f"{_sep_zh(language)}{L(language, 'gates_resolution_label')}"
                         f"{_sep_col(language)}{g['resolution_note']}")
            lines.append(line)
        lines.append("")
    else:
        lines.append(L(language, "gates_none"))
    lines.append("")

    # ---- key numbers (evidence) --------------------------------------------
    lines.append(f"## {L(language, 'evidence_title')}")
    lines.append("")
    lines.append(f"**{L(language, 'core_title')}**")
    lines.append("")
    hdr = (f"| {L(language, 'table_col_metric')} | {L(language, 'table_col_claim')} | "
           f"{L(language, 'table_col_value')} | {L(language, 'table_col_delta')} | "
           f"{L(language, 'table_col_unit')} | {L(language, 'table_col_verdict')} |")
    sep = "|---|---|---|---|---|---|"
    prev_goal = None
    for r in d["core_rows"]:
        if r["goal_id"] != prev_goal:
            if prev_goal is not None:
                lines.append("")
            lines.append(f"**{_goal_display(r, language)}**")
            lines.append("")
            lines.append(hdr)
            lines.append(sep)
            prev_goal = r["goal_id"]
        lines.append(
            f"| {_esc_md(r['name'])} | {_esc_md(r['claim'])} | {_esc_md(r['value'])} | {_esc_md(r['delta'])} | "
            f"{_esc_md(r['unit'])} | {_esc_md(r['icon'])} |")
    if not d["core_rows"]:
        lines.append(L(language, "none_marker"))
    lines.append("")
    lines.append(f"**{L(language, 'ctx_title')}**")
    lines.append("")
    lines.append(f"*{L(language, 'ctx_note')}*")
    lines.append("")
    if d["ctx_rows"]:
        lines.append(f"| {L(language, 'table_col_metric')} | "
                     f"{L(language, 'table_col_value')} | "
                     f"{L(language, 'table_col_unit')} |")
        lines.append("|---|---|---|")
        for r in d["ctx_rows"]:
            lines.append(
                f"| {_esc_md(r['name'])} ({_esc_md(_goal_display(r, language))}) | "
                f"{_esc_md(r['value'])} | {_esc_md(r['unit'])} |")
    else:
        lines.append(L(language, "none_marker"))
    if d["process_rows"]:
        lines.append("")
        lines.append(L(language, "qc_header"))
        for r in d["process_rows"]:
            icon = {"exact": "✅", "within_tolerance": "✅",
                    "over_claim": "❌"}.get(r["status"], "·")
            extra = (f"{_sep_zh(language)}{L(language, 'qc_rule_label')}"
                     f"{_sep_col(language)}{r['rule']}" if r.get("rule")
                     else "")
            if r.get("note"):
                extra += _paren(language, _clip_prose(r['note'], 118))
            lines.append(
                f"- {icon} {r['name']}{_paren(language, _goal_display(r, language))}"
                f"{_sep_col(language)}{r['value']}{extra}")
    if d["result_notes"]:
        lines.append("")
        lines.append(L(language, "result_notes_header"))
        for r in d["result_notes"]:
            lines.append(f"- {_goal_display(r, language)} — {r['text']}")
    if d["uncertainties"]:
        lines.append("")
        lines.append(f"**{L(language, 'uncertainty_header')}**")
        for u in d["uncertainties"]:
            lines.append(f"- {u}")
    if d["status_warnings"]:
        lines.append("")
        lines.append(f"**{L(language, 'status_warning_header')}**")
        for w in d["status_warnings"]:
            lines.append(f"- {w}")
    lines.append("")

    # ---- verification & rerun ----------------------------------------------
    lines.append(f"## {L(language, 'verify_title')}")
    lines.append("")
    lines.extend(_verify_lines(language))
    lines.append("")

    # ---- deliverables (converged list) -------------------------------------
    lines.append(f"## {L(language, 'deliverables_title')}")
    lines.append("")
    lines.append(f"*{L(language, 'dlv_note')}*")
    lines.append("")
    groups = _deliverable_groups(d["deliverables"], language)
    for label, bucket in groups:
        lines.append(f"**{label}**")
        for item in bucket:
            lines.append(f"- `{item}`")
    lines.append("")

    # ---- appendix A: full rationales ---------------------------------------
    lines.append(f"## {L(language, 'appendix_a')}")
    lines.append("")
    a_note = _section_foreign_note(
        [dict(it, rationale=it.get("rationale_full") or it.get("rationale"))
         for it in d["closed_reqs"]], language)
    if a_note:
        lines.append(a_note)
        lines.append("")
    for r in d["closed_reqs"]:
        # appendix A keeps the authored record verbatim (rationale_full);
        # the zh-rendered label serves the matrix/conclusion.
        reason = (r["rationale_full"] or r["rationale"] or "").replace("\n", " ").strip()
        reason = reason if reason else _reason_fallback(language)
        lines.append(f"**{r['requirement_id']}** — {r['statement_full']}")
        lines.append("")
        note = _item_note(
            r["rationale_full"] or r["rationale"], language) if not a_note else ""
        lines.append(reason + note)
        lines.append("")

    # ---- appendix B: verdict semantics + bands -----------------------------
    lines.append(f"## {L(language, 'appendix_b')}")
    lines.append("")
    lines.extend(_guide_lines(d, language))
    lines.append("")
    lines.extend([b for b in d["band_lines"]])
    lines.append("")
    lines.append(f"*{L(language, 'footer')}*")
    lines.append("")
    return "\n".join(lines)



def _conclusion_req_line(d: dict, language: str) -> str:
    parts = [
        f"{_verdict_label(language, k)} {v}"
        for k, v in sorted(d["outcomes"].items())
    ]
    if not parts:
        return ("（无已关闭需求）" if language == "zh"
                else "(no closed requirements)")
    return "、".join(parts) if language == "zh" else ", ".join(parts)


def _verify_lines(language: str) -> list[str]:
    zh = [
        "- 审计包 `reports/reproduction-audit-package.json`：逐声明证据链 + 工件校验和（validate PASS 为交付前提）。",
        "- 结果包与重跑脚本：位于 `compute/` 各目标目录（`GOAL-*/` 或 `compute/goals/out/`，布局随项目而定），每包确定性输出，脚本可重跑复算。",
        "- 运行记录 `runs/`：每个 run 一个目录（结果包）+ 记录文件（CLOSED/PASS）。",
        "- 数字化证据：`compute/run/assets/` 或各目标目录内的渲染图、OCR 表、像素仲裁件（支持逐字符复核）。",
        "- 跨版本可比性：不同时间线（如 20260904c 与本次）的数值口径可能不同（OLS 重拟、误差定义、验收带评估名义值），以本摘要附录 B 的判定口径为准；"
          "跨版本对账请以机器审计包 `reproduction-audit-package.json` 为准。",
    ]
    en = [
        "- Audit package `reports/reproduction-audit-package.json`: per-claim evidence chain + artifact checksums (validate PASS is the delivery gate).",
        "- Result packages and rerun scripts live under `compute/` goal directories (`GOAL-*/` or `compute/goals/out/`, layout depends on the project); deterministic per-goal outputs, scripts recomputable.",
        "- Run records under `runs/`: one directory per run (result package) plus a CLOSED/PASS record file.",
        "- Digitization evidence: renders, OCR tables and pixel-arbitration artifacts under `compute/run/assets/` or per-goal directories (character-level verifiable).",
        "- Cross-version comparability: numeric conventions (OLS refit, error definition, "
          "band evaluation at the nominal value) may differ between timelines; Appendix B of "
          "this summary states the semantics used here — for cross-version reconciliation use "
          "the machine audit package `reports/reproduction-audit-package.json`.",
    ]
    return zh if language == "zh" else en


# ---------------------------------------------------------------------------
# PDF renderer (deterministic; shared rendering stack)
# ---------------------------------------------------------------------------
_PDF_EMOJI_TEXT = {
    "✅": "[OK]",
    "⚠️": "[within-tol]",
    "❌": "[over-claim]",
    "❓": "[undetermined]",
    "…": "...",
}


def _pdf_text(s: str) -> str:
    """Map glyphs the PDF font stack cannot render (emoji verdict icons)
    onto plain ASCII markers; the Markdown renderer keeps the icons."""
    for emoji, text in _PDF_EMOJI_TEXT.items():
        s = s.replace(emoji, text)
    return s


def _all_render_texts(d: dict, language: str, generated_at: str) -> list[str]:
    """Every string the PDF will draw — content probe for the backend."""
    out: list[str] = []
    for key in _TEMPLATES.get(language, _TEMPLATES["en"]):
        out.append(_tpl(language, key))
    out.extend([d["title"], d["doi"] or ""])
    out.append(_pdf_text(_tpl(language, "generated_suffix") + " " + generated_at))
    out.extend(_verify_lines(language))
    out.extend(_guide_lines(d, language))
    for r in d["closed_reqs"]:
        out.extend((r["requirement_id"], r["statement"], r["statement_full"],
                    r["rationale"], r["rationale_full"]))
    for r in d["core_rows"]:
        out.extend((r["name"], r["claim"], r["value"], r["delta"],
                    r["unit"], r["icon"]))
    for r in d["ctx_rows"]:
        out.extend((r["name"], r["value"], r["unit"]))
    for r in d["process_rows"]:
        out.extend((r["name"], r["value"], r.get("rule") or "", r.get("note") or ""))
    for w in d["status_warnings"]:
        out.append(w)
    for u in d["uncertainties"]:
        out.append(u)
    for dec in d["problems"]:
        out.append(str(dec.get("decision_id") or ""))
        out.append(str(dec.get("rationale") or ""))
    for a in d["limits"]:
        out.append(str(a.get("assumption_id") or ""))
        out.append(str(a.get("rationale") or ""))
    for g in d["human_gates"]:
        out.extend((g["gate_id"], _gate_type_label(language, g["gate_type"]),
                    _gate_status_label(language, g["status"]), g["trigger"],
                    g["affected_refs"], g["default_safe_action"],
                    g["resolution_note"]))
    out.extend(d["deliverables"])
    return out


def build_human_summary_pdf(
    root: str | Path,
    *,
    generated_at: str,
    language: str = "zh",
) -> bytes:
    """Render the human-readable reproduction summary as a deterministic PDF.

    Same state and ordering as the Markdown renderer (v3 user-first)."""
    from scientific_reproduction.rendering.layout import FlowLayout
    from scientific_reproduction.rendering.pdf import PdfDocument
    from scientific_reproduction.rendering.style import measure_backend

    root_path = Path(root)
    d = _collect(root_path, language)
    d = dict(d)
    d["title"] = _pdf_text(d["title"])
    for r in d["core_rows"]:
        r["icon"] = _pdf_text(r["icon"])
        r["value"] = _pdf_text(r["value"])
    for r in d["ctx_rows"]:
        r["value"] = _pdf_text(r["value"])
    for r in d["process_rows"]:
        r["name"] = _pdf_text(r["name"])
        r["value"] = _pdf_text(r["value"])
    for r in d["result_notes"]:
        r["text"] = _pdf_text(r["text"])
    d["problems"] = [
        dict(p, rationale=_pdf_text(str(p.get("rationale") or "")))
        for p in d["problems"]
    ]
    d["limits"] = [
        dict(a, rationale=_pdf_text(str(a.get("rationale") or "")))
        for a in d["limits"]
    ]
    d["uncertainties"] = [_pdf_text(u) for u in d["uncertainties"]]
    d["status_warnings"] = [_pdf_text(w) for w in d["status_warnings"]]
    d["deliverables"] = [_pdf_text(x) for x in d["deliverables"]]
    d["closed_reqs"] = [
        dict(r, rationale=_pdf_text(str(r.get("rationale") or "")),
             rationale_full=_pdf_text(str(r.get("rationale_full") or "")),
             statement=_pdf_text(str(r.get("statement") or "")),
             statement_full=_pdf_text(str(r.get("statement_full") or "")))
        for r in d["closed_reqs"]
    ]

    texts = _all_render_texts(d, language, generated_at)
    needs_unicode = any(ord(ch) > 127 for s in texts for ch in s)
    if needs_unicode:
        from scientific_reproduction.rendering.fonts import (
            FontConfig,
            TrueTypeBackend,
        )
        backend = TrueTypeBackend(FontConfig.default())
    else:
        from scientific_reproduction.rendering.fonts import Base14Backend
        backend = Base14Backend(strict=True)

    with measure_backend(backend):
        doc = PdfDocument(title=_pdf_text(_tpl(language, "title")), font_backend=backend)
        layout = FlowLayout(doc)
        L = lambda lang, key: _pdf_text(_tpl(lang, key))

        layout.heading(L(language, "title"), level=1)
        layout.paragraph(_pdf_text(_tpl(language, "generated_suffix")) +
                         " " + generated_at)
        layout.paragraph(f"{L(language, 'paper_label')}: {d['title']}")
        if d["doi"]:
            layout.paragraph(f"DOI: {d['doi']}")
        layout.spacer(4)

        # one-sentence conclusion
        layout.heading(L(language, "conclusion_para"), level=2)
        layout.paragraph(_pdf_text(_conclusion_text(d, language, root_path)))

        # reproduction matrix
        layout.heading(L(language, "matrix_title"), level=2)
        layout.paragraph(L(language, "matrix_legend"))
        mhdr = [L(language, "matrix_col_req"), L(language, "matrix_col_claim"),
                L(language, "matrix_col_result"), L(language, "matrix_col_note")]
        for r, note in zip(d["closed_reqs"], _matrix_note_cells(d, language)):
            outcome = _pdf_text(_verdict_label(language, r["outcome"]))
            if r["demo"]:
                outcome += L(language, "demo_badge")
            if r["internal_findings"]:
                outcome += L(language, "internal_badge")
            layout.table(
                mhdr,
                [[r["requirement_id"], _pdf_text(_claim_text(r, language)),
                  outcome, _pdf_text(note)]],
                widths=[f * layout.content_width for f in
                        (0.10, 0.30, 0.13, 0.47)])

        # the paper's own problems
        layout.heading(L(language, "problems_title"), level=2)
        if d["problems"]:
            for group_key in ("grp_claim", "grp_internal", "grp_struct", "grp_obs"):
                items = d["problems_grouped"].get(group_key)
                if not items:
                    continue
                layout.paragraph(L(language, group_key))
                group_note = _section_foreign_note(items, language, d["decision_labels"])
                if group_note:
                    layout.paragraph(group_note)
                for dec in items:
                    lbl, rat, paren = _finding_meta(dec, language, d, group_key)
                    note = "" if lbl else (
                        _item_note(rat, language) if not group_note else "")
                    if lbl:
                        # zh-first body; the verbatim record stays in appendix A
                        text = f"{lbl}{paren}"
                    else:
                        title = _item_title(dec, "finding")
                        text = (f"{title}{paren}"
                                f"{'— ' if language == 'zh' else ' — '}{rat}{note}")
                    layout.paragraph(text)
                if group_key == "grp_internal" and d.get("cross_internal"):
                    for dec in d["cross_internal"]:
                        title = _item_title(dec, "finding")
                        did = dec.get("decision_id", "")
                        layout.paragraph(
                            "另见（性质同属论文内部不一致，详见『" +
                            L(language, _group_key(str(dec.get("rationale") or ""))) +
                            "』）：" + f"{title} ({did})"
                            if language == "zh" else
                            "See also (same internal-inconsistency nature, in " +
                            L(language, _group_key(str(dec.get("rationale") or ""))) +
                            "): " + f"{title} ({did})")
        else:
            layout.paragraph(L(language, "problems_none"))

        # assumptions & boundaries
        layout.heading(L(language, "limits_title"), level=2)
        if d["limits"]:
            limits_note = _section_foreign_note(d["limits"], language)
            if limits_note:
                layout.paragraph(limits_note)
            for a in d["limits"]:
                rat = str(a.get("rationale") or "")
                note = (_item_note(rat, language) if not limits_note else "")
                title = _item_title(a, "assumption")
                layout.paragraph(
                    f"{title} ({a.get('assumption_id', '')}) — {rat}{note}")
        else:
            layout.paragraph(L(language, "limits_none"))

        # human confirmation gates
        layout.heading(L(language, "gates_title"), level=2)
        layout.paragraph(L(language, "gates_note"))
        if d["human_gates"]:
            for g in d["human_gates"]:
                gsep = "— " if language == "zh" else " — "
                text = (f"{g['gate_id']}"
                        f"{_paren(language, _gate_type_label(language, g['gate_type'])
                                  + _sep_zh(language)
                                  + _gate_status_label(language, g['status']))}"
                        f"{gsep}{g['trigger']}")
                if g["affected_refs"]:
                    text += (f"{_sep_zh(language)}{L(language, 'gates_affected_label')}"
                             f"{_sep_col(language)}{g['affected_refs']}")
                text += (f"{_sep_zh(language)}{L(language, 'gates_default_label')}"
                         f"{_sep_col(language)}{g['default_safe_action'] or '—'}")
                if g["resolution_note"]:
                    text += (f"{_sep_zh(language)}{L(language, 'gates_resolution_label')}"
                             f"{_sep_col(language)}{g['resolution_note']}")
                layout.paragraph(_pdf_text(text))
        else:
            layout.paragraph(L(language, "gates_none"))

        # key numbers (evidence)
        layout.heading(L(language, "evidence_title"), level=2)
        layout.paragraph(L(language, "core_title"))
        hdr = [L(language, "table_col_metric"), L(language, "table_col_claim"),
               L(language, "table_col_value"), L(language, "table_col_delta"),
               L(language, "table_col_unit"), L(language, "table_col_verdict")]
        widths = [f * layout.content_width for f in
                  (0.30, 0.13, 0.15, 0.10, 0.14, 0.18)]
        prev_goal = None
        for r in d["core_rows"]:
            if r["goal_id"] != prev_goal:
                if prev_goal is not None:
                    layout.spacer(3)
                layout.paragraph(_pdf_text(_goal_display(r, language)))
                prev_goal = r["goal_id"]
            layout.table(hdr, [[r["name"], r["claim"], r["value"], r["delta"],
                                r["unit"], r["icon"]]], widths=widths)
        if not d["core_rows"]:
            layout.paragraph(L(language, "none_marker"))
        layout.paragraph(L(language, "ctx_title"))
        layout.paragraph(L(language, "ctx_note"))
        if d["ctx_rows"]:
            for r in d["ctx_rows"]:
                layout.paragraph(
                    f"{r['name']} ({_pdf_text(_goal_display(r, language))}) — "
                    f"{r['value']} {r['unit']}".rstrip())
        if d["process_rows"]:
            layout.paragraph("Process / QC checks:" if language != "zh"
                             else "过程自检与记录项（QC/流程指标，不构成论文数值对照）：")
            for r in d["process_rows"]:
                icon = _pdf_text({"exact": "✅", "within_tolerance": "✅",
                                  "over_claim": "❌"}.get(r["status"], "·"))
                extra = f"; rule: {r['rule']}" if r.get("rule") else ""
                if r.get("note"):
                    extra += f" ({_pdf_text(_clip_prose(r['note'], 118))})"
                layout.paragraph(
                    f"- {icon} {r['name']} ({_pdf_text(_goal_display(r, language))}): "
                    f"{r['value']}{extra}")
        if d["result_notes"]:
            layout.paragraph("Goal result notes:" if language != "zh"
                             else "各目标结果说明（无结构化指标的目标）：")
            for r in d["result_notes"]:
                layout.paragraph(
                    f"- {_pdf_text(_goal_display(r, language))} — {r['text']}")
        if d["uncertainties"]:
            layout.paragraph(L(language, "uncertainty_header"))
            for u in d["uncertainties"]:
                layout.paragraph(u)
        if d["status_warnings"]:
            layout.paragraph(L(language, "status_warning_header"))
            for w in d["status_warnings"]:
                layout.paragraph(w)

        # verification & rerun
        layout.heading(L(language, "verify_title"), level=2)
        for line in _verify_lines(language):
            layout.paragraph(line)

        # deliverables
        layout.heading(L(language, "deliverables_title"), level=2)
        layout.paragraph(L(language, "dlv_note"))
        groups = _deliverable_groups(d["deliverables"], language)
        for label, bucket in groups:
            layout.paragraph(label)
            for item in bucket:
                layout.paragraph(item)

        # appendix A: full rationales (authored record kept verbatim)
        layout.heading(L(language, "appendix_a"), level=2)
        a_note = _section_foreign_note(
            [dict(it, rationale=it.get("rationale_full") or it.get("rationale"))
             for it in d["closed_reqs"]], language)
        if a_note:
            layout.paragraph(a_note)
        for r in d["closed_reqs"]:
            reason = (r["rationale_full"] or r["rationale"] or "").replace(
                chr(10), " ").strip()
            if not reason:
                reason = _reason_fallback(language)
            note = _item_note(
                r["rationale_full"] or r["rationale"], language) if not a_note else ""
            layout.paragraph(
                f"{r['requirement_id']} — {r['statement_full']}")
            layout.paragraph(reason[:500] + note)

        # appendix B: verdict semantics + bands
        layout.heading(L(language, "appendix_b"), level=2)
        for line in _guide_lines(d, language):
            layout.paragraph(_pdf_text(line))
        for b in d["band_lines"]:
            layout.paragraph(b)

        layout.spacer(6)
        layout.paragraph(L(language, "footer"))
        return doc.render()



def write_human_summary(
    root: str | Path,
    *,
    generated_at: str,
    language: str = "zh",
    out_path: str | Path | None = None,
) -> Path:
    """Render and persist the human-readable summary (Markdown)."""
    root_path = Path(root)
    if out_path is None:
        out_path = root_path / "reports" / SUMMARY_FILENAME
    text = build_human_summary(root_path, generated_at=generated_at, language=language)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return out_path


def write_human_summary_pdf(
    root: str | Path,
    *,
    generated_at: str,
    language: str = "zh",
    out_path: str | Path | None = None,
) -> Path:
    """Render and persist the human-readable summary as a PDF."""
    root_path = Path(root)
    if out_path is None:
        out_path = root_path / "reports" / SUMMARY_PDF_FILENAME
    data = build_human_summary_pdf(
        root_path, generated_at=generated_at, language=language
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    return out_path


def write_human_summary_both(
    root: str | Path,
    *,
    generated_at: str,
    language: str = "zh",
) -> tuple[Path, Path]:
    """Persist both summary formats (Markdown + PDF).

    Convenience entry used by the finalization flow so every project ships
    both a diff-able Markdown and a formal PDF summary.

    Returns:
        ``(md_path, pdf_path)``.
    """
    md_path = write_human_summary(root, generated_at=generated_at, language=language)
    pdf_path = write_human_summary_pdf(
        root, generated_at=generated_at, language=language
    )
    return md_path, pdf_path
