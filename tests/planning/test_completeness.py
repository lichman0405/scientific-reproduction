"""Tests for the inventory completeness audit (DEV-M4-G03).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_completeness_ac01_*``: one intentionally unmapped item
    prevents freeze eligibility: the verdict is FAIL, ``freeze_eligible``
    is False, and the unmapped item's id is in the audit evidence.
  * AC-02 -- ``test_completeness_ac02_*``: 100% mapped and zero ambiguous
    items passes: full formal-item coverage with no ambiguity yields the
    PASS verdict, while any AMBIGUOUS item blocks the audit (an unresolved
    mapping cannot be decided by the rules).
  * AC-03 -- ``test_completeness_ac03_*``: audit evidence lists offending
    item IDs: the record names exactly the unmapped formal items and the
    ambiguous items, in deterministic sorted order.

Plus paradigm tests: verdict recomputed from state rather than stored
``mapping_status`` snapshots, purity / order-independence, empty-inventory
and non-formal-only edge cases, the versioned total rule table, frozen
records, the schema-compatible ``PlanInventoryAudit`` view, TypeError at
the public boundaries, and the registry path's initialized-project gating
and corrupt-record rejection.

Every test name contains "completeness" so the goal verification command
``python -m pytest -q tests/planning -k completeness`` selects the full
suite.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from inventory_helpers import (
    init_project,
    make_item,
    make_requirement,
)

from scientific_reproduction.core.models import (
    AuditStatus,
    MappingStatus,
    PlanInventoryAudit,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.audit import (
    AUDIT_RULES,
    AUDIT_RULESET_VERSION,
    AuditInput,
    CompletenessAudit,
    CompletenessAuditDecision,
    CompletenessAuditRule,
    audit_inventory_registry,
    evaluate_completeness_audit,
)
from scientific_reproduction.planning.init import ProjectNotInitializedError
from scientific_reproduction.planning.inventory import (
    INVENTORY_STATE_DIR,
    InventorySummary,
    load_inventory_registry,
    read_inventory_item,
    register_inventory_item,
    register_requirement,
)

# ---------------------------------------------------------------------------
# AC-01: one intentionally unmapped item prevents freeze eligibility
# ---------------------------------------------------------------------------


def test_completeness_ac01_single_unmapped_item_blocks_freeze_eligibility(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-UNMAPPED-001"))
    registry = load_inventory_registry(root)
    audit = evaluate_completeness_audit(registry.items, registry.requirements)
    assert isinstance(audit, CompletenessAudit)
    assert audit.verdict is AuditStatus.FAIL
    assert not audit.freeze_eligible
    assert audit.matched_rule_id == "R-AUD-U1"
    assert audit.summary.formally_reported_items == 1
    assert audit.summary.unmapped_items == 1
    assert audit.summary.mapped_items == 0
    assert audit.summary.coverage == pytest.approx(0.0)
    # AC-03: the offending id is named in the evidence.
    assert audit.unmapped_item_ids == ("INV-UNMAPPED-001",)
    assert audit.offending_item_ids == ("INV-UNMAPPED-001",)
    # The registry-path wrapper reaches the identical decision.
    assert audit_inventory_registry(root) == audit


def test_completeness_ac01_unmapped_item_blocks_even_with_other_mapped_items(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root, make_item("INV-MAPPED", requirement_ids=("REQ-1",))
    )
    register_inventory_item(root, make_item("INV-UNMAPPED"))
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("INV-MAPPED",),
            goal_ids=("GOAL-1",),
        ),
    )
    audit = audit_inventory_registry(root)
    # 100% coverage is necessary but not sufficient: one intentionally
    # unmapped item still prevents freeze eligibility (AC-01).
    assert audit.summary.coverage == pytest.approx(0.5)
    assert audit.summary.mapped_items == 1
    assert audit.verdict is AuditStatus.FAIL
    assert not audit.freeze_eligible
    assert audit.matched_rule_id == "R-AUD-U1"
    assert audit.offending_item_ids == ("INV-UNMAPPED",)


def test_completeness_ac01_multiple_unmapped_items_block_and_list_all_ids() -> None:
    items = (
        make_item("INV-UNMAPPED-A"),
        make_item("INV-UNMAPPED-B"),
        make_item("INV-UNMAPPED-C"),
    )
    audit = evaluate_completeness_audit(items, ())
    assert audit.verdict is AuditStatus.FAIL
    assert audit.summary.unmapped_items == 3
    # AC-03: every offending id is listed, in deterministic sorted order.
    assert audit.unmapped_item_ids == (
        "INV-UNMAPPED-A",
        "INV-UNMAPPED-B",
        "INV-UNMAPPED-C",
    )
    assert audit.offending_item_ids == (
        "INV-UNMAPPED-A",
        "INV-UNMAPPED-B",
        "INV-UNMAPPED-C",
    )


# ---------------------------------------------------------------------------
# AC-02: 100% mapped and zero ambiguous items passes
# ---------------------------------------------------------------------------


def test_completeness_ac02_full_mapping_zero_ambiguous_passes(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root, make_item("INV-1", requirement_ids=("REQ-1",))
    )
    register_inventory_item(
        root, make_item("INV-2", requirement_ids=("REQ-1", "REQ-2"))
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("INV-1", "INV-2"),
            goal_ids=("GOAL-1",),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-2",
            inventory_items=("INV-2",),
            goal_ids=("GOAL-2",),
        ),
    )
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.freeze_eligible
    assert audit.matched_rule_id == "R-AUD-P1"
    assert audit.summary.formally_reported_items == 2
    assert audit.summary.mapped_items == 2
    assert audit.summary.unmapped_items == 0
    assert audit.summary.ambiguous_items == 0
    assert audit.summary.coverage == pytest.approx(1.0)
    assert audit.unmapped_item_ids == ()
    assert audit.ambiguous_item_ids == ()
    assert audit.offending_item_ids == ()


def test_completeness_ac02_ambiguous_item_blocks_freeze_eligibility(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    # The item references a requirement the registry does not hold: the
    # mapping cannot be decided by the rules (R-MAP-A1).
    register_inventory_item(
        root, make_item("INV-AMB-1", requirement_ids=("REQ-GHOST",))
    )
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.FAIL
    assert not audit.freeze_eligible
    assert audit.matched_rule_id == "R-AUD-A1"
    assert audit.summary.ambiguous_items == 1
    assert audit.ambiguous_item_ids == ("INV-AMB-1",)
    assert audit.offending_item_ids == ("INV-AMB-1",)


def test_completeness_ac02_empty_inventory_passes_vacuously() -> None:
    # No formally reported items -> no coverage obligation: the audit
    # passes with zero counts and 0.0 coverage (the inventory.py
    # convention for an empty formal-item set).
    audit = evaluate_completeness_audit((), ())
    assert audit.verdict is AuditStatus.PASS
    assert audit.freeze_eligible
    assert audit.matched_rule_id == "R-AUD-P1"
    assert audit.summary.total_items == 0
    assert audit.summary.formally_reported_items == 0
    assert audit.summary.coverage == 0.0
    assert audit.offending_item_ids == ()


def test_completeness_empty_registry_passes_on_initialized_workspace(
    tmp_path: Path,
) -> None:
    # The registry path agrees: an initialized workspace with no registered
    # items has no formal-item coverage obligation and passes.
    root = init_project(tmp_path / "project")
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.freeze_eligible
    assert audit.summary.total_items == 0
    assert audit.offending_item_ids == ()


def test_completeness_ac02_nonformal_only_inventory_passes(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    # Non-formal items are excluded from the formal-item coverage
    # obligation (R-MAP-X1): they never fail the audit, even when they
    # carry requirement ids (which the mapping rules ignore for them).
    register_inventory_item(
        root,
        make_item(
            "INV-NF-1",
            formal_report=False,
            requirement_ids=("REQ-1",),
        ),
    )
    register_inventory_item(root, make_item("INV-NF-2", formal_report=False))
    audit = audit_inventory_registry(root)
    assert audit.verdict is AuditStatus.PASS
    assert audit.freeze_eligible
    assert audit.summary.formally_reported_items == 0
    assert audit.summary.excluded_nonformal_items == 2
    assert audit.summary.coverage == 0.0
    assert audit.offending_item_ids == ()
    assert audit.unmapped_item_ids == ()
    assert audit.ambiguous_item_ids == ()


# ---------------------------------------------------------------------------
# AC-03: audit evidence lists offending item IDs
# ---------------------------------------------------------------------------


def test_completeness_ac03_evidence_lists_offending_item_ids() -> None:
    items = (
        make_item("INV-MAPPED", requirement_ids=("REQ-1",)),
        make_item("INV-UNMAPPED"),
        make_item("INV-AMB", requirement_ids=("REQ-GHOST",)),
        make_item("INV-NF", formal_report=False, requirement_ids=("REQ-1",)),
    )
    requirements = (
        make_requirement(
            "REQ-1",
            inventory_items=("INV-MAPPED",),
            goal_ids=("GOAL-1",),
        ),
    )
    audit = evaluate_completeness_audit(items, requirements)
    assert audit.verdict is AuditStatus.FAIL
    # AC-03: the evidence names exactly the offending ids -- the unmapped
    # formal item and the ambiguous item -- and nothing else.
    assert audit.unmapped_item_ids == ("INV-UNMAPPED",)
    assert audit.ambiguous_item_ids == ("INV-AMB",)
    assert audit.offending_item_ids == ("INV-AMB", "INV-UNMAPPED")
    assert "INV-MAPPED" not in audit.offending_item_ids
    assert "INV-NF" not in audit.offending_item_ids


def test_completeness_ac03_evidence_is_deterministic_and_sorted() -> None:
    items_a = (
        make_item("INV-Z-UNMAPPED"),
        make_item("INV-A-AMB", requirement_ids=("REQ-GHOST",)),
        make_item("INV-M-UNMAPPED"),
    )
    items_b = tuple(reversed(items_a))
    first = evaluate_completeness_audit(items_a, ())
    second = evaluate_completeness_audit(items_b, ())
    # Evidence order is the sorted inventory_id order (which is also the
    # registry order of load_inventory_registry), independent of input
    # order, and stable across repeated calls.
    assert first == second
    assert first.offending_item_ids == (
        "INV-A-AMB",
        "INV-M-UNMAPPED",
        "INV-Z-UNMAPPED",
    )
    assert second.offending_item_ids == (
        "INV-A-AMB",
        "INV-M-UNMAPPED",
        "INV-Z-UNMAPPED",
    )


# ---------------------------------------------------------------------------
# Paradigm: purity, determinism, recomputation from state
# ---------------------------------------------------------------------------


def test_completeness_audit_is_pure_and_order_independent() -> None:
    items = (
        make_item("INV-1", requirement_ids=("REQ-1",)),
        make_item("INV-2"),
        make_item("INV-3", requirement_ids=("REQ-GHOST",)),
        make_item("INV-4", formal_report=False),
    )
    requirements = (
        make_requirement(
            "REQ-1",
            inventory_items=("INV-1",),
            goal_ids=("GOAL-1",),
        ),
    )
    first = evaluate_completeness_audit(items, requirements)
    # Equal state -> equal record, on every call and in any order.
    assert evaluate_completeness_audit(items, requirements) == first
    assert evaluate_completeness_audit(tuple(reversed(items)), requirements) == first
    assert evaluate_completeness_audit(items, tuple(reversed(requirements))) == first
    # The state matters: without the requirement the mapping collapses.
    assert evaluate_completeness_audit(items, ()) != first
    assert evaluate_completeness_audit(items, ()).verdict is AuditStatus.FAIL
    # The record exposes the deterministic InventorySummary counts.
    assert isinstance(first.summary, InventorySummary)
    assert first.summary.mapped_items == 1
    assert first.summary.unmapped_items == 1
    assert first.summary.ambiguous_items == 1
    assert first.summary.excluded_nonformal_items == 1


def test_completeness_verdict_recomputed_from_state_not_stored_snapshots(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    # Register the item first: its requirement reference is unresolved, so
    # the stored record snapshot says AMBIGUOUS and the audit fails.
    register_inventory_item(
        root,
        make_item("INV-ADS-001", requirement_ids=("REQ-ADS-001",)),
    )
    assert read_inventory_item(root, "INV-ADS-001").mapping_status is (
        MappingStatus.AMBIGUOUS
    )
    failing = audit_inventory_registry(root)
    assert failing.verdict is AuditStatus.FAIL
    assert failing.matched_rule_id == "R-AUD-A1"
    # Registering the requirement later resolves the mapping: the stored
    # snapshot is untouched (records are immutable), but the audit
    # recomputes every status from the fuller registered state.
    register_requirement(
        root,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    assert read_inventory_item(root, "INV-ADS-001").mapping_status is (
        MappingStatus.AMBIGUOUS
    )
    passing = audit_inventory_registry(root)
    assert passing.verdict is AuditStatus.PASS
    assert passing.freeze_eligible
    assert passing.summary.mapped_items == 1
    assert passing.summary.ambiguous_items == 0
    assert passing.offending_item_ids == ()


def test_completeness_stored_mapping_status_snapshot_not_trusted() -> None:
    # An input snapshot claiming MAPPED is not trusted: the rules recompute
    # from the given state, so the item's unresolved requirement reference
    # is AMBIGUOUS and blocks the audit.
    item = make_item(
        "INV-1",
        requirement_ids=("REQ-1",),
        mapping_status=MappingStatus.MAPPED,
    )
    audit = evaluate_completeness_audit((item,), ())
    assert audit.verdict is AuditStatus.FAIL
    assert audit.matched_rule_id == "R-AUD-A1"
    assert audit.ambiguous_item_ids == ("INV-1",)
    assert audit.offending_item_ids == ("INV-1",)
    # With the requirement registered, the same snapshot input passes:
    # only the given state decides.
    passing = evaluate_completeness_audit(
        (item,),
        (
            make_requirement(
                "REQ-1",
                inventory_items=("INV-1",),
                goal_ids=("GOAL-1",),
            ),
        ),
    )
    assert passing.verdict is AuditStatus.PASS


def test_completeness_freeze_eligible_matches_verdict() -> None:
    passing = evaluate_completeness_audit(
        (make_item("INV-1", requirement_ids=("REQ-1",)),),
        (
            make_requirement(
                "REQ-1",
                inventory_items=("INV-1",),
                goal_ids=("GOAL-1",),
            ),
        ),
    )
    assert passing.verdict is AuditStatus.PASS
    assert passing.freeze_eligible is True
    failing = evaluate_completeness_audit((make_item("INV-1"),), ())
    assert failing.verdict is AuditStatus.FAIL
    assert failing.freeze_eligible is False


# ---------------------------------------------------------------------------
# Rule table shape and verdict semantics
# ---------------------------------------------------------------------------


def test_completeness_ruleset_is_versioned_and_total() -> None:
    assert AUDIT_RULESET_VERSION == "1.0"
    rule_ids = [rule.rule_id for rule in AUDIT_RULES]
    assert len(rule_ids) == len(set(rule_ids)), "rule ids must be unique"
    assert len(AUDIT_RULES) == 3
    for rule in AUDIT_RULES:
        assert isinstance(rule, CompletenessAuditRule)
        assert isinstance(rule.verdict, AuditStatus)
        assert rule.description
    # The trailing default rule matches every audit state: the table is
    # total, so a verdict is always decided.
    assert AUDIT_RULES[-1].rule_id == "R-AUD-P1"
    empty = evaluate_completeness_audit((), ())
    assert AUDIT_RULES[-1].predicate(
        AuditInput(
            summary=empty.summary,
            unmapped_item_ids=empty.unmapped_item_ids,
            ambiguous_item_ids=empty.ambiguous_item_ids,
        )
    ) is True
    failing = evaluate_completeness_audit((make_item("INV-1"),), ())
    assert AUDIT_RULES[-1].predicate(
        AuditInput(
            summary=failing.summary,
            unmapped_item_ids=failing.unmapped_item_ids,
            ambiguous_item_ids=failing.ambiguous_item_ids,
        )
    ) is True


def test_completeness_first_match_wins_verdict_semantics() -> None:
    # R-AUD-U1 fires before R-AUD-A1: with both an unmapped and an
    # ambiguous item the verdict rule trace records every evaluation (both
    # blocker predicates match this state) and names the first blocker.
    items = (
        make_item("INV-UNMAPPED"),
        make_item("INV-AMB", requirement_ids=("REQ-GHOST",)),
    )
    audit = evaluate_completeness_audit(items, ())
    assert audit.verdict is AuditStatus.FAIL
    assert audit.matched_rule_id == "R-AUD-U1"
    assert [d.rule_id for d in audit.decisions] == [r.rule_id for r in AUDIT_RULES]
    assert all(isinstance(d, CompletenessAuditDecision) for d in audit.decisions)
    assert audit.decisions[0].matched is True
    assert audit.decisions[1].matched is True
    assert audit.decisions[-1].matched is True  # default rule fires
    # R-AUD-A1 is the first blocker when no item is unmapped.
    ambiguous_only = evaluate_completeness_audit(
        (make_item("INV-AMB", requirement_ids=("REQ-GHOST",)),), ()
    )
    assert ambiguous_only.matched_rule_id == "R-AUD-A1"
    # R-AUD-P1 is the default: reached only when nothing blocks.
    passing = evaluate_completeness_audit(
        (make_item("INV-1", requirement_ids=("REQ-1",)),),
        (
            make_requirement(
                "REQ-1",
                inventory_items=("INV-1",),
                goal_ids=("GOAL-1",),
            ),
        ),
    )
    assert passing.matched_rule_id == "R-AUD-P1"
    assert all(not d.matched for d in passing.decisions[:-1])


def test_completeness_rule_and_record_are_frozen_dataclasses() -> None:
    audit = evaluate_completeness_audit((make_item("INV-1"),), ())
    with pytest.raises(dataclasses.FrozenInstanceError):
        audit.verdict = AuditStatus.PASS  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        audit.offending_item_ids = ()  # type: ignore[misc]
    rule = AUDIT_RULES[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.rule_id = "changed"  # type: ignore[misc]
    decision = audit.decisions[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.matched = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The frozen PlanInventoryAudit view (schema compatibility)
# ---------------------------------------------------------------------------


def test_completeness_plan_inventory_audit_view_is_schema_compatible() -> None:
    items = (
        make_item("INV-MAPPED", requirement_ids=("REQ-1",)),
        make_item("INV-UNMAPPED"),
    )
    requirements = (
        make_requirement(
            "REQ-1",
            inventory_items=("INV-MAPPED",),
            goal_ids=("GOAL-1",),
        ),
    )
    failing = evaluate_completeness_audit(items, requirements)
    view = failing.plan_inventory_audit()
    assert isinstance(view, PlanInventoryAudit)
    assert view.status is AuditStatus.FAIL
    assert view.formally_reported_items == 2
    assert view.mapped_items == 1
    assert view.unmapped_items == 1
    assert view.ambiguous_items == 0
    assert view.coverage == pytest.approx(0.5)
    # The view round-trips and satisfies the frozen plan schema's
    # inventory_audit sub-object (schemas/plan.schema.yaml), the shape the
    # Plan freeze flow embeds into the plan record.
    assert PlanInventoryAudit.from_dict(view.to_dict()) == view
    validate_and_reject(
        "plan",
        {
            "plan_id": "PLAN-1",
            "version": "v1",
            "status": "DRAFT",
            "goal_ids": [],
            "requirement_ids": [],
            "inventory_audit": view.to_dict(),
        },
    )
    # The passing record carries the PASS status in the view.
    passing = evaluate_completeness_audit(
        (make_item("INV-MAPPED", requirement_ids=("REQ-1",)),),
        requirements,
    )
    assert passing.plan_inventory_audit().status is AuditStatus.PASS
    assert passing.plan_inventory_audit().coverage == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# API robustness
# ---------------------------------------------------------------------------


def test_completeness_wrong_types_raise_type_error(tmp_path: Path) -> None:
    item = make_item("INV-1")
    requirement = make_requirement(
        "REQ-1", inventory_items=("INV-1",), goal_ids=("GOAL-1",)
    )
    with pytest.raises(TypeError):
        evaluate_completeness_audit("items", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_completeness_audit((item,), "requirements")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_completeness_audit((requirement,), ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_completeness_audit((item,), (item,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        audit_inventory_registry(123)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        audit_inventory_registry(None)  # type: ignore[arg-type]


def test_completeness_registry_audit_requires_initialized_project(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProjectNotInitializedError, match="initialize the project first"):
        audit_inventory_registry(tmp_path / "bare")


def test_completeness_registry_audit_rejects_corrupt_record(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    (root / INVENTORY_STATE_DIR).mkdir(exist_ok=True)
    (root / INVENTORY_STATE_DIR / "INV-CORRUPT.json").write_text(
        "{not json", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt inventory record"):
        audit_inventory_registry(root)
    # A corrupt requirement record is rejected the same way.
    (root / INVENTORY_STATE_DIR / "INV-CORRUPT.json").unlink()
    (root / "requirements").mkdir(exist_ok=True)
    (root / "requirements" / "REQ-CORRUPT.json").write_text(
        "[]", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="corrupt requirement record"):
        audit_inventory_registry(root)
