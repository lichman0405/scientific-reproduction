"""Tests for human gate registration / resolution (v0.3.1 local).

Covers the registry surface added so evidence-interpretation ambiguities
(ambiguous digitized readings) no longer surface as blocking mid-run
questions: exactly-once registration with deterministic ids, rule-gated
resolution transitions, finalization blocked while a gate is OPEN, the
summary 「人工确认项」 section in both renderers, and model <-> schema
enum synchronization.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from inventory_helpers import init_project

from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import GateStatus, GateType, HumanGate
from scientific_reproduction.planning.finalize import check_finalization
from scientific_reproduction.planning.human_gates import (
    HUMAN_GATES_STATE_DIR,
    DuplicateHumanGateError,
    HumanGateNotFoundError,
    InvalidHumanGateTransitionError,
    list_human_gates,
    read_human_gate,
    register_human_gate,
    resolve_human_gate,
)
from scientific_reproduction.reporting.human_summary import (
    build_human_summary,
    build_human_summary_pdf,
)

TRIGGER = (
    "Fig. 5 marker sits on two candidate data points; the calibration"
    " reading must pick one to continue REQ-02"
)


def make_gate(**kw) -> dict:
    data = {
        "gate_type": GateType.EVIDENCE_INTERPRETATION_GATE.value,
        "status": GateStatus.OPEN.value,
        "trigger": TRIGGER,
        "affected_refs": ["REQ-02"],
        "default_safe_action": (
            "use both candidates as the reading interval; mark REQ-02"
            " uncertainty until confirmed"
        ),
    }
    data.update(kw)
    return data


def test_human_gate_registers_lists_and_reads(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    gate = register_human_gate(root, make_gate())
    assert is_valid_id(gate.gate_id, "gate")
    assert gate.status == GateStatus.OPEN
    listed = list_human_gates(root)
    assert [g.gate_id for g in listed] == [gate.gate_id]
    assert read_human_gate(root, gate.gate_id) == gate
    stored = json.loads(
        (root / HUMAN_GATES_STATE_DIR / f"{gate.gate_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["status"] == "OPEN"
    assert stored["gate_type"] == "EVIDENCE_INTERPRETATION_GATE"
    assert stored["default_safe_action"].startswith("use both candidates")
    # resolution_note is absent until resolved (to_dict omits None values)
    assert "resolution_note" not in stored


def test_human_gate_deterministic_id_duplicate_rejected(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_human_gate(root, make_gate())
    # the same ambiguity derives the same id and is rejected exactly-once
    with pytest.raises(DuplicateHumanGateError):
        register_human_gate(root, make_gate())


def test_human_gate_accepts_typed_model_and_explicit_id(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    gate_id = generate_id("gate", "typed", TRIGGER)
    gate = HumanGate(
        gate_id=gate_id,
        gate_type=GateType.SAFETY_GATE,
        status=GateStatus.OPEN,
        trigger="safety probe",
        affected_refs=["GOAL-EXE-01"],
    )
    reg = register_human_gate(root, gate)
    assert reg.gate_id == gate_id


def test_human_gate_resolve_transitions_persist_resolution(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    gate = register_human_gate(root, make_gate())
    resolved = resolve_human_gate(
        root, gate.gate_id, GateStatus.APPROVED,
        resolution_note="pick candidate B (0.43), marked on the REQ-02 rationale",
    )
    assert resolved.status == GateStatus.APPROVED
    assert resolved.resolution_note.startswith("pick candidate B")
    assert list_human_gates(root)[0].resolution_note == resolved.resolution_note
    # APPROVED is terminal: any further transition is an error, not a no-op
    with pytest.raises(InvalidHumanGateTransitionError):
        resolve_human_gate(root, gate.gate_id, GateStatus.CANCELLED)


def test_human_gate_resolve_rejects_unknown_gate(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    with pytest.raises(HumanGateNotFoundError):
        resolve_human_gate(
            root, generate_id("gate", "nope", "x"), GateStatus.CANCELLED
        )


def test_human_gate_open_blocks_finalization(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_human_gate(root, make_gate())
    check = check_finalization(root, language="en")
    assert len(check.open_human_gates) == 1
    gate = list_human_gates(root)[0]
    resolve_human_gate(root, gate.gate_id, GateStatus.APPROVED, resolution_note="ok")
    assert not check_finalization(root, language="en").open_human_gates


def test_human_gate_summary_section_render(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    md_empty = build_human_summary(
        root, generated_at="2026-09-05T00:00:00+00:00", language="zh"
    )
    assert "人工确认项" in md_empty
    assert "无。" in md_empty.split("## 人工确认项")[1]
    register_human_gate(root, make_gate())
    md = build_human_summary(
        root, generated_at="2026-09-05T00:00:00+00:00", language="zh"
    )
    section = md.split("## 人工确认项")[1]
    # the gate line carries the template labels in context: anchor the
    # assertion so a wrong-class label (e.g. "safe" for this gate) fails
    assert "（证据解读；待确认）" in section
    # the raw enum value must NOT leak: the template label is what renders
    assert "EVIDENCE_INTERPRETATION_GATE" not in section
    assert "待确认" in section
    assert "REQ-02" in section
    assert "默认动作" in section
    assert "受影响" in section
    pdf = build_human_summary_pdf(
        root, generated_at="2026-09-05T00:00:00+00:00", language="zh"
    )
    assert len(pdf) > 1000


def test_human_gate_summary_section_render_en(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_human_gate(root, make_gate())
    md = build_human_summary(
        root, generated_at="2026-09-05T00:00:00+00:00", language="en"
    )
    section = md.split("## Human confirmation items")[1]
    assert "evidence interpretation" in section
    assert "EVIDENCE_INTERPRETATION_GATE" not in section
    assert "affected" in section
    assert "受影响" not in section


def test_human_gate_schema_enum_matches_model(tmp_path: Path) -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "human-gate.schema.json")
        .read_text(encoding="utf-8")
    )
    assert "resolution_note" in schema["properties"]
    assert set(GateType) <= set(
        schema["properties"]["gate_type"]["enum"]
    )
