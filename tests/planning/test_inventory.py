"""Tests for Reproduction Inventory registration and mapping (DEV-M4-G02).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_inventory_ac01_*``: formal reported items can map to one
    or more Goals: the transitive item -> requirement -> goal mapping
    resolves a formal item through one or more Requirements to its goal set,
    the mapping structure is many-to-many-able, and non-formal / unmapped /
    ambiguous items produce no goal mappings.
  * AC-02 -- ``test_inventory_ac02_*``: unmapped/ambiguous counts are
    deterministic: ``summarize_inventory`` is a pure function of the
    registered state (order-independent, stable across repeated calls,
    recomputed from state rather than stored snapshots) and exposes
    mapped/unmapped/ambiguous counts plus coverage.
  * AC-03 -- ``test_inventory_ac03_*``: mappings preserve source
    location/provenance references: every item -> requirement -> goal
    mapping edge carries the item's ``source_id`` / ``source_location``, the
    registry records preserve them, and the mapping assessments record the
    full rule trace.

Plus registry behavior (duplicate registration rejection, initialized
project gating, requirement registration validation, deterministic reads and
state bytes) and the rule-table shape invariants.

Every test name contains "inventory" so the goal verification command
``python -m pytest -q tests/planning -k inventory`` selects the full suite.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from inventory_helpers import (
    init_project,
    make_item,
    make_requirement,
)

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    Criticality,
    InventoryItemType,
    MappingStatus,
    ReproductionInventoryItem,
    ReproductionRequirement,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import ProjectNotInitializedError
from scientific_reproduction.planning.inventory import (
    INVENTORY_STATE_DIR,
    MAPPING_RULES,
    MAPPING_RULESET_VERSION,
    REQUIREMENTS_STATE_DIR,
    DuplicateInventoryItemError,
    DuplicateRequirementError,
    InvalidRegistryIdError,
    InventoryError,
    InventoryItemNotFoundError,
    InventoryRegistry,
    ItemGoalMapping,
    ItemMappingAssessment,
    ItemMappingRule,
    ItemMappingRuleDecision,
    RequirementNotFoundError,
    UnresolvedItemReferenceError,
    evaluate_item_mapping,
    list_inventory_items,
    list_requirements,
    load_inventory_registry,
    mapped_goal_ids,
    read_inventory_item,
    read_requirement,
    register_inventory_item,
    register_requirement,
    resolve_goal_mappings,
    summarize_inventory,
    unresolved_requirement_ids,
)

# ---------------------------------------------------------------------------
# Registry: registration and persistence
# ---------------------------------------------------------------------------


def test_inventory_registers_item_with_rule_computed_status(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    # Authoring order is items first: the requirement maps an existing item.
    register_inventory_item(root, make_item("INV-ADS-001"))
    register_requirement(
        root,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    stored = register_inventory_item(
        root,
        make_item("INV-ADS-002", requirement_ids=("REQ-ADS-001",)),
    )
    # The rule table decided the status: MAPPED, not whatever the input said.
    assert stored.mapping_status is MappingStatus.MAPPED
    assert stored.ambiguity_notes is None
    # The record is persisted under the registry directory and round-trips.
    state_path = root / INVENTORY_STATE_DIR / "INV-ADS-002.json"
    assert state_path.is_file()
    assert read_inventory_item(root, "INV-ADS-002") == stored
    assert list_inventory_items(root) == (
        make_item("INV-ADS-001"),
        stored,
    )
    # The input's explicit mapping_status was replaced by the computed one.
    assert stored.to_dict()["mapping_status"] == "MAPPED"


def test_inventory_registers_ambiguous_item_with_stable_notes(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    stored = register_inventory_item(
        root,
        make_item(
            "INV-AMB-001",
            requirement_ids=("REQ-NOT-YET-REGISTERED", "REQ-ALSO-MISSING"),
        ),
    )
    assert stored.mapping_status is MappingStatus.AMBIGUOUS
    assert stored.ambiguity_notes == (
        "unresolved requirement reference(s): REQ-NOT-YET-REGISTERED,"
        " REQ-ALSO-MISSING"
    )
    # The same input produces the identical stored record (determinism).
    root2 = init_project(tmp_path / "project-2")
    stored2 = register_inventory_item(
        root2,
        make_item(
            "INV-AMB-001",
            requirement_ids=("REQ-NOT-YET-REGISTERED", "REQ-ALSO-MISSING"),
        ),
    )
    assert stored2 == stored
    assert stored2.ambiguity_notes == stored.ambiguity_notes


def test_inventory_registers_nonformal_item_as_excluded(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    stored = register_inventory_item(
        root,
        make_item(
            "INV-NF-001",
            item_type=InventoryItemType.SUPPLEMENTARY_RESULT,
            formal_report=False,
            description="informal lab note",
        ),
    )
    assert stored.mapping_status is MappingStatus.EXCLUDED_NONFORMAL
    # Non-formal items never map, even when requirement ids are present.
    excluded = register_inventory_item(
        root,
        make_item(
            "INV-NF-002",
            formal_report=False,
            description="informal note with a stray reference",
            requirement_ids=("REQ-ADS-001",),
        ),
    )
    assert excluded.mapping_status is MappingStatus.EXCLUDED_NONFORMAL


def test_inventory_duplicate_registration_rejected_with_stable_error(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    item = make_item("INV-ADS-001", requirement_ids=("REQ-ADS-001",))
    register_inventory_item(root, item)
    with pytest.raises(DuplicateInventoryItemError) as excinfo:
        register_inventory_item(root, item)
    message = str(excinfo.value)
    assert "INV-ADS-001" in message
    assert "already registered" in message
    # Stable error: the same attempt on another initialized root yields the
    # identical message.
    root2 = init_project(tmp_path / "project-2")
    register_inventory_item(root2, item)
    with pytest.raises(DuplicateInventoryItemError) as excinfo2:
        register_inventory_item(root2, item)
    assert str(excinfo2.value) == message
    # The rejection leaves the registered record untouched.
    assert read_inventory_item(root, "INV-ADS-001").description == item.description


def test_inventory_duplicate_detection_is_deterministic_for_generated_ids(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    item_dict = {
        "source_id": "SRC-TARGET-PAPER",
        "item_type": "experiment",
        "formal_report": True,
        "description": "Deterministic isotherm item.",
    }
    expected_id = generate_id(
        "inventory", "SRC-TARGET-PAPER", "experiment", "Deterministic isotherm item."
    )
    first = register_inventory_item(root, item_dict)
    assert first.inventory_id == expected_id
    # The same canonical fields yield the same id, so the second
    # registration is the same item and is rejected deterministically.
    with pytest.raises(DuplicateInventoryItemError, match="already registered"):
        register_inventory_item(root, item_dict)


def test_inventory_registration_requires_initialized_project(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotInitializedError) as excinfo:
        register_inventory_item(tmp_path / "bare", make_item("INV-001"))
    assert "initialize the project first" in str(excinfo.value)
    with pytest.raises(ProjectNotInitializedError, match="initialize the project first"):
        register_requirement(
            tmp_path / "bare",
            make_requirement("REQ-1", inventory_items=("INV-1",), goal_ids=("G",)),
        )


def test_inventory_rejects_ids_with_path_separators(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    for bad_id in ("a/b", "a\\b", "..", ".", ""):
        with pytest.raises(InvalidRegistryIdError):
            register_inventory_item(root, make_item(bad_id))
    register_inventory_item(root, make_item("INV-OK"))
    for bad_id in ("a/b", ".."):
        with pytest.raises(InvalidRegistryIdError):
            read_inventory_item(root, bad_id)
    with pytest.raises(InvalidRegistryIdError):
        register_requirement(
            root,
            make_requirement("REQ/bad", inventory_items=("INV-OK",), goal_ids=("G",)),
        )


def test_inventory_register_requirement_with_goal_mapping_roundtrips(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-ADS-001"))
    requirement = make_requirement(
        "REQ-ADS-001",
        statement="Reproduce the reported isotherm.",
        inventory_items=("INV-ADS-001",),
        goal_ids=("GOAL-ADS-001", "GOAL-ADS-002"),
        criticality=Criticality.CRITICAL,
    )
    stored = register_requirement(root, requirement)
    assert stored == requirement
    state_path = root / REQUIREMENTS_STATE_DIR / "REQ-ADS-001.json"
    assert state_path.is_file()
    assert read_requirement(root, "REQ-ADS-001") == requirement
    assert list_requirements(root) == (requirement,)


def test_inventory_register_requirement_rejects_unregistered_items(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    with pytest.raises(UnresolvedItemReferenceError) as excinfo:
        register_requirement(
            root,
            make_requirement(
                "REQ-ORPHAN",
                inventory_items=("INV-MISSING",),
                goal_ids=("GOAL-ADS-001",),
            ),
        )
    message = str(excinfo.value)
    assert "REQ-ORPHAN" in message
    assert "INV-MISSING" in message
    assert "unregistered inventory item" in message
    # Stable error for the same attempt on another root.
    root2 = init_project(tmp_path / "project-2")
    with pytest.raises(UnresolvedItemReferenceError) as excinfo2:
        register_requirement(
            root2,
            make_requirement(
                "REQ-ORPHAN",
                inventory_items=("INV-MISSING",),
                goal_ids=("GOAL-ADS-001",),
            ),
        )
    assert str(excinfo2.value) == message
    # Nothing was written by the rejected attempt.
    assert list_requirements(root) == ()


def test_inventory_duplicate_requirement_rejected_with_stable_error(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-ADS-001"))
    requirement = make_requirement(
        "REQ-ADS-001",
        inventory_items=("INV-ADS-001",),
        goal_ids=("GOAL-ADS-001",),
    )
    register_requirement(root, requirement)
    with pytest.raises(DuplicateRequirementError) as excinfo:
        register_requirement(root, requirement)
    assert "REQ-ADS-001" in str(excinfo.value)
    assert "already registered" in str(excinfo.value)


def test_inventory_requirement_id_generated_deterministically(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-ADS-001"))
    requirement_dict = {
        "statement": "Reproduce the reported uptake capacity.",
        "inventory_items": ["INV-ADS-001"],
        "criticality": "REQUIRED",
        "goal_ids": ["GOAL-ADS-001"],
        "outcome": "OPEN",
    }
    expected_id = generate_id("requirement", "Reproduce the reported uptake capacity.")
    stored = register_requirement(root, requirement_dict)
    assert stored.requirement_id == expected_id


def test_inventory_registry_snapshot_typed_and_sorted(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-B"))
    register_inventory_item(root, make_item("INV-A"))
    register_inventory_item(root, make_item("INV-C"))
    register_requirement(
        root,
        make_requirement(
            "REQ-B", inventory_items=("INV-B",), goal_ids=("GOAL-1",)
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-A", inventory_items=("INV-A",), goal_ids=("GOAL-1",)
        ),
    )
    registry = load_inventory_registry(root)
    assert isinstance(registry, InventoryRegistry)
    assert [item.inventory_id for item in registry.items] == ["INV-A", "INV-B", "INV-C"]
    assert [r.requirement_id for r in registry.requirements] == ["REQ-A", "REQ-B"]
    # Missing records raise the dedicated not-found errors.
    with pytest.raises(InventoryItemNotFoundError):
        read_inventory_item(root, "INV-MISSING")
    with pytest.raises(RequirementNotFoundError):
        read_requirement(root, "REQ-MISSING")


def test_inventory_registered_records_validate_against_frozen_schema(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root,
        make_item("INV-ADS-001", requirement_ids=("REQ-ADS-001",)),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    stored_item = read_inventory_item(root, "INV-ADS-001")
    validate_and_reject("inventory-item", stored_item.to_dict())
    stored_req = read_requirement(root, "REQ-ADS-001")
    validate_and_reject("requirement", stored_req.to_dict())


def test_inventory_state_bytes_identical_across_registrations(
    tmp_path: Path,
) -> None:
    root1 = init_project(tmp_path / "a")
    root2 = init_project(tmp_path / "b")
    register_inventory_item(root1, make_item("INV-ADS-001"))
    register_inventory_item(root2, make_item("INV-ADS-001"))
    register_requirement(
        root1,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    register_requirement(
        root2,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    # No timestamps anywhere in the inventory records: identical inputs
    # produce byte-identical state.
    assert (
        root1 / INVENTORY_STATE_DIR / "INV-ADS-001.json"
    ).read_bytes() == (
        root2 / INVENTORY_STATE_DIR / "INV-ADS-001.json"
    ).read_bytes()
    assert (
        root1 / REQUIREMENTS_STATE_DIR / "REQ-ADS-001.json"
    ).read_bytes() == (
        root2 / REQUIREMENTS_STATE_DIR / "REQ-ADS-001.json"
    ).read_bytes()


# ---------------------------------------------------------------------------
# AC-01: formal reported items can map to one or more Goals
# ---------------------------------------------------------------------------


def test_inventory_ac01_formal_item_maps_to_multiple_goals_via_one_requirement(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root,
        make_item("INV-ADS-001", requirement_ids=("REQ-ADS-001",)),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001", "GOAL-ADS-002"),
        ),
    )
    item = read_inventory_item(root, "INV-ADS-001")
    # The registered state classifies the item MAPPED (AC-02 counting path).
    registry = load_inventory_registry(root)
    assert summarize_inventory(registry.items, registry.requirements).mapped_items == 1
    # One formal item -> one requirement -> two Goals (AC-01).
    assert mapped_goal_ids(item, list_requirements(root)) == (
        "GOAL-ADS-001",
        "GOAL-ADS-002",
    )
    edges = resolve_goal_mappings(item, list_requirements(root))
    assert [edge.goal_id for edge in edges] == ["GOAL-ADS-001", "GOAL-ADS-002"]


def test_inventory_ac01_formal_item_maps_to_multiple_goals_via_multiple_requirements(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root,
        make_item(
            "INV-ADS-001",
            requirement_ids=("REQ-ADS-001", "REQ-ADS-002"),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-ADS-001",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-ADS-002",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-002", "GOAL-ADS-003"),
        ),
    )
    item = read_inventory_item(root, "INV-ADS-001")
    # The registered state classifies the item MAPPED (AC-02 counting path).
    registry = load_inventory_registry(root)
    assert summarize_inventory(registry.items, registry.requirements).mapped_items == 1
    # One formal item -> two Requirements -> three Goals (AC-01).
    assert mapped_goal_ids(item, list_requirements(root)) == (
        "GOAL-ADS-001",
        "GOAL-ADS-002",
        "GOAL-ADS-003",
    )
    edges = resolve_goal_mappings(item, list_requirements(root))
    assert len(edges) == 3
    assert [(e.requirement_id, e.goal_id) for e in edges] == [
        ("REQ-ADS-001", "GOAL-ADS-001"),
        ("REQ-ADS-002", "GOAL-ADS-002"),
        ("REQ-ADS-002", "GOAL-ADS-003"),
    ]


def test_inventory_ac01_many_to_many_mapping_with_deduped_goal_ids(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root,
        make_item(
            "INV-ITEM-1",
            requirement_ids=("REQ-1", "REQ-2"),
        ),
    )
    register_inventory_item(
        root,
        make_item(
            "INV-ITEM-2",
            requirement_ids=("REQ-2",),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("INV-ITEM-1",),
            goal_ids=("GOAL-SHARED",),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-2",
            inventory_items=("INV-ITEM-1", "INV-ITEM-2"),
            goal_ids=("GOAL-SHARED", "GOAL-2"),
        ),
    )
    requirements = list_requirements(root)
    # Item 1 reaches GOAL-SHARED through two requirements: the goal set is
    # deduplicated (first-seen order), the edge list keeps both provenance
    # records.
    assert mapped_goal_ids(read_inventory_item(root, "INV-ITEM-1"), requirements) == (
        "GOAL-SHARED",
        "GOAL-2",
    )
    edges = resolve_goal_mappings(read_inventory_item(root, "INV-ITEM-1"), requirements)
    assert [(e.requirement_id, e.goal_id) for e in edges] == [
        ("REQ-1", "GOAL-SHARED"),
        ("REQ-2", "GOAL-SHARED"),
        ("REQ-2", "GOAL-2"),
    ]
    # Item 2 maps to the shared goal and its own, through the same REQ-2.
    assert mapped_goal_ids(read_inventory_item(root, "INV-ITEM-2"), requirements) == (
        "GOAL-SHARED",
        "GOAL-2",
    )
    # Both items are MAPPED in the registered state (AC-02 counting path).
    registry = load_inventory_registry(root)
    assert summarize_inventory(registry.items, registry.requirements).mapped_items == 2


def test_inventory_ac01_nonformal_items_never_map_to_goals(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root,
        make_item(
            "INV-NF-001",
            formal_report=False,
            description="informal lab note",
        ),
    )
    register_inventory_item(root, make_item("INV-ADS-001", requirement_ids=("REQ-1",)))
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-ADS-001",),
        ),
    )
    nonformal = read_inventory_item(root, "INV-NF-001")
    assert nonformal.mapping_status is MappingStatus.EXCLUDED_NONFORMAL
    assert mapped_goal_ids(nonformal, list_requirements(root)) == ()
    assert resolve_goal_mappings(nonformal, list_requirements(root)) == ()


def test_inventory_ac01_unmapped_and_ambiguous_items_produce_no_goal_mappings(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-UNMAPPED-001"))
    register_inventory_item(
        root,
        make_item("INV-AMB-001", requirement_ids=("REQ-GHOST",)),
    )
    requirements = list_requirements(root)
    unmapped = read_inventory_item(root, "INV-UNMAPPED-001")
    assert unmapped.mapping_status is MappingStatus.UNMAPPED
    assert mapped_goal_ids(unmapped, requirements) == ()
    ambiguous = read_inventory_item(root, "INV-AMB-001")
    assert ambiguous.mapping_status is MappingStatus.AMBIGUOUS
    assert mapped_goal_ids(ambiguous, requirements) == ()
    assert resolve_goal_mappings(ambiguous, requirements) == ()


# ---------------------------------------------------------------------------
# AC-02: unmapped/ambiguous counts are deterministic
# ---------------------------------------------------------------------------


def test_inventory_ac02_summary_counts_mapped_unmapped_ambiguous_excluded() -> None:
    items = (
        make_item("INV-MAPPED-1", requirement_ids=("REQ-1",)),
        make_item("INV-MAPPED-2", requirement_ids=("REQ-1",)),
        make_item("INV-UNMAPPED-1"),
        make_item("INV-UNMAPPED-2"),
        make_item("INV-AMB-1", requirement_ids=("REQ-GHOST",)),
        make_item("INV-AMB-2", requirement_ids=("REQ-1", "REQ-GHOST")),
        make_item("INV-NF-1", formal_report=False),
        make_item("INV-NF-2", formal_report=False),
    )
    requirements = (
        make_requirement(
            "REQ-1",
            inventory_items=("INV-MAPPED-1",),
            goal_ids=("GOAL-1",),
        ),
    )
    summary = summarize_inventory(items, requirements)
    assert summary.total_items == 8
    assert summary.formally_reported_items == 6
    assert summary.mapped_items == 2
    assert summary.unmapped_items == 2
    assert summary.ambiguous_items == 2
    assert summary.excluded_nonformal_items == 2
    assert summary.coverage == pytest.approx(2 / 6)


def test_inventory_ac02_summary_is_pure_function_of_registered_state() -> None:
    items = (
        make_item("INV-MAPPED", requirement_ids=("REQ-1",)),
        make_item("INV-UNMAPPED"),
        make_item("INV-AMB", requirement_ids=("REQ-GHOST",)),
        make_item("INV-NF", formal_report=False),
    )
    requirements = (
        make_requirement(
            "REQ-1",
            inventory_items=("INV-MAPPED",),
            goal_ids=("GOAL-1",),
        ),
    )
    first = summarize_inventory(items, requirements)
    # Repeated calls on equal state are stable.
    assert summarize_inventory(items, requirements) == first
    assert summarize_inventory(tuple(reversed(items)), requirements) == first
    assert summarize_inventory(items, ()) != first
    # 1 of 3 formal items is mapped (one is unmapped, one ambiguous).
    assert summarize_inventory(items, requirements).coverage == pytest.approx(1 / 3)


def test_inventory_ac02_summary_is_order_independent() -> None:
    items_a = (
        make_item("INV-1", requirement_ids=("REQ-1",)),
        make_item("INV-2"),
        make_item("INV-3", formal_report=False),
    )
    items_b = tuple(reversed(items_a))
    requirements = (
        make_requirement(
            "REQ-1",
            inventory_items=("INV-1",),
            goal_ids=("GOAL-1",),
        ),
    )
    # Registration order of requirements cannot change the counts either.
    assert summarize_inventory(items_a, requirements) == summarize_inventory(
        items_b, requirements
    )
    assert summarize_inventory(items_a, requirements) == summarize_inventory(
        items_a, tuple(reversed(requirements))
    )


def test_inventory_ac02_coverage_deterministic() -> None:
    # No formal items: coverage is 0.0, not a division error.
    empty = summarize_inventory((make_item("INV-NF", formal_report=False),), ())
    assert empty.formally_reported_items == 0
    assert empty.mapped_items == 0
    assert empty.coverage == 0.0
    # Fully mapped formal registry: coverage 1.0.
    full = summarize_inventory(
        (make_item("INV-1", requirement_ids=("REQ-1",)),),
        (
            make_requirement(
                "REQ-1",
                inventory_items=("INV-1",),
                goal_ids=("GOAL-1",),
            ),
        ),
    )
    assert full.coverage == pytest.approx(1.0)


def test_inventory_ac02_counts_recomputed_from_state_not_stored_snapshots(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    # Register the item first: its requirement reference is unresolved, so
    # the stored snapshot says AMBIGUOUS.
    register_inventory_item(
        root,
        make_item("INV-ADS-001", requirement_ids=("REQ-ADS-001",)),
    )
    assert read_inventory_item(root, "INV-ADS-001").mapping_status is (
        MappingStatus.AMBIGUOUS
    )
    # Registering the requirement later resolves the mapping: the stored
    # snapshot is untouched (records are immutable), but every summary
    # recomputes the status from the fuller registered state.
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
    registry = load_inventory_registry(root)
    summary = summarize_inventory(registry.items, registry.requirements)
    assert summary.formally_reported_items == 1
    assert summary.mapped_items == 1
    assert summary.ambiguous_items == 0
    assert summary.unmapped_items == 0
    assert summary.coverage == pytest.approx(1.0)


def test_inventory_ac02_ambiguous_count_is_stable_and_exposed(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(
        root,
        make_item("INV-AMB-1", requirement_ids=("REQ-GHOST",)),
    )
    register_inventory_item(
        root,
        make_item("INV-AMB-2", requirement_ids=("REQ-GHOST-2",)),
    )
    first = summarize_inventory(*_registry_views(root))
    second = summarize_inventory(*_registry_views(root))
    assert first == second
    assert first.ambiguous_items == 2
    assert first.unmapped_items == 0
    # The notes naming the unresolved references are stable and exposed.
    notes = read_inventory_item(root, "INV-AMB-1").ambiguity_notes
    assert notes == "unresolved requirement reference(s): REQ-GHOST"


def _registry_views(root: Path) -> tuple[tuple[ReproductionInventoryItem, ...], tuple[ReproductionRequirement, ...]]:
    registry = load_inventory_registry(root)
    return registry.items, registry.requirements


# ---------------------------------------------------------------------------
# AC-03: mappings preserve source location/provenance references
# ---------------------------------------------------------------------------


def test_inventory_ac03_goal_mapping_preserves_source_provenance(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    source_location = "main adsorption figure, 'Adsorption isotherms' section"
    register_inventory_item(
        root,
        make_item(
            "INV-ADS-001",
            source_id="SRC-TARGET-PAPER",
            source_location=source_location,
            requirement_ids=("REQ-1", "REQ-2"),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-1",),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-2",
            inventory_items=("INV-ADS-001",),
            goal_ids=("GOAL-2",),
        ),
    )
    edges = resolve_goal_mappings(read_inventory_item(root, "INV-ADS-001"), list_requirements(root))
    assert len(edges) == 2
    for edge in edges:
        assert isinstance(edge, ItemGoalMapping)
        assert edge.inventory_id == "INV-ADS-001"
        assert edge.source_id == "SRC-TARGET-PAPER"
        assert edge.source_location == source_location
        assert edge.goal_id in ("GOAL-1", "GOAL-2")
    assert {edge.requirement_id for edge in edges} == {"REQ-1", "REQ-2"}


def test_inventory_ac03_source_location_persisted_in_registry(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    source_location = "Figure 2, panel (b); SI section 1.3"
    register_inventory_item(
        root,
        make_item(
            "INV-ADS-001",
            source_id="SRC-TARGET-PAPER",
            source_location=source_location,
        ),
    )
    state = json.loads(
        (root / INVENTORY_STATE_DIR / "INV-ADS-001.json").read_text(encoding="utf-8")
    )
    assert state["source_id"] == "SRC-TARGET-PAPER"
    assert state["source_location"] == source_location
    read_back = read_inventory_item(root, "INV-ADS-001")
    assert read_back.source_id == "SRC-TARGET-PAPER"
    assert read_back.source_location == source_location


def test_inventory_ac03_source_location_optional_none_preserved(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    item = make_item("INV-NO-LOC", source_location=None)
    stored = register_inventory_item(root, item)
    assert stored.source_location is None
    read_back = read_inventory_item(root, "INV-NO-LOC")
    assert read_back.source_location is None
    assert read_back == stored


def test_inventory_ac03_requirement_mapping_preserves_requirement_links(
    tmp_path: Path,
) -> None:
    root = init_project(tmp_path / "project")
    register_inventory_item(root, make_item("INV-1"))
    register_inventory_item(root, make_item("INV-2"))
    requirement = make_requirement(
        "REQ-1",
        statement="One formally reported result/procedure obligation.",
        inventory_items=("INV-1", "INV-2"),
        goal_ids=("GOAL-1", "GOAL-2", "GOAL-3"),
    )
    stored = register_requirement(root, requirement)
    read_back = read_requirement(root, "REQ-1")
    assert read_back == requirement == stored
    # The requirement record is the frozen inventory-to-requirement and
    # requirement-to-goal mapping edge (05-GOAL-RUN-SCHEMA.md SS2).
    assert read_back.inventory_items == ["INV-1", "INV-2"]
    assert read_back.goal_ids == ["GOAL-1", "GOAL-2", "GOAL-3"]


def test_inventory_ac03_assessment_records_full_rule_trace_and_version() -> None:
    item = make_item("INV-1", requirement_ids=("REQ-1",))
    assessment = evaluate_item_mapping(item, ("REQ-1",))
    assert isinstance(assessment, ItemMappingAssessment)
    assert assessment.mapping_status is MappingStatus.MAPPED
    assert assessment.matched_rule_id == "R-MAP-M1"
    assert assessment.ambiguity_notes is None
    # Every rule evaluation is recorded, in table order, with the version.
    assert [d.rule_id for d in assessment.decisions] == [
        rule.rule_id for rule in MAPPING_RULES
    ]
    assert all(isinstance(d, ItemMappingRuleDecision) for d in assessment.decisions)
    assert assessment.decisions[-1].matched is True  # default rule fires
    assert MAPPING_RULESET_VERSION == "1.0"
    # Same state -> identical assessment (pure and deterministic).
    assert evaluate_item_mapping(item, ("REQ-1",)) == assessment
    # The exact input state is preserved in the assessment.
    assert assessment.input.item == item
    assert assessment.input.registered_requirement_ids == frozenset({"REQ-1"})


# ---------------------------------------------------------------------------
# Rule table shape and semantics
# ---------------------------------------------------------------------------


def test_inventory_mapping_ruleset_is_versioned_and_total() -> None:
    assert MAPPING_RULESET_VERSION == "1.0"
    rule_ids = [rule.rule_id for rule in MAPPING_RULES]
    assert len(rule_ids) == len(set(rule_ids)), "rule ids must be unique"
    assert len(MAPPING_RULES) == 4
    for rule in MAPPING_RULES:
        assert isinstance(rule, ItemMappingRule)
        assert isinstance(rule.status, MappingStatus)
        assert rule.description
    # The trailing default rule matches every state, so the table is total.
    assert MAPPING_RULES[-1].rule_id == "R-MAP-U1"
    assert MAPPING_RULES[-1].predicate(
        evaluate_item_mapping(make_item("INV-1"), ()).input
    ) is True


def test_inventory_mapping_rules_are_frozen_dataclasses() -> None:
    rule = MAPPING_RULES[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.rule_id = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.predicate = lambda i: False  # type: ignore[misc]


def test_inventory_mapping_first_match_wins_semantics() -> None:
    # R-MAP-X1 fires before any requirement rule: a non-formal item with
    # requirement ids is EXCLUDED, never MAPPED.
    excluded = evaluate_item_mapping(
        make_item("INV-NF", formal_report=False, requirement_ids=("REQ-1",)), ("REQ-1",)
    )
    assert excluded.mapping_status is MappingStatus.EXCLUDED_NONFORMAL
    assert excluded.matched_rule_id == "R-MAP-X1"
    # R-MAP-A1 fires before R-MAP-M1: a formal item with an unresolved
    # reference is AMBIGUOUS, never MAPPED.
    ambiguous = evaluate_item_mapping(
        make_item("INV-1", requirement_ids=("REQ-1", "REQ-GHOST")), ("REQ-1",)
    )
    assert ambiguous.mapping_status is MappingStatus.AMBIGUOUS
    assert ambiguous.matched_rule_id == "R-MAP-A1"
    assert ambiguous.ambiguity_notes == "unresolved requirement reference(s): REQ-GHOST"
    # R-MAP-M1: every reference resolves.
    mapped = evaluate_item_mapping(
        make_item("INV-1", requirement_ids=("REQ-1",)), ("REQ-1",)
    )
    assert mapped.mapping_status is MappingStatus.MAPPED
    assert mapped.matched_rule_id == "R-MAP-M1"
    # R-MAP-U1 (default): formal item without any requirement reference.
    unmapped = evaluate_item_mapping(make_item("INV-1"), ())
    assert unmapped.mapping_status is MappingStatus.UNMAPPED
    assert unmapped.matched_rule_id == "R-MAP-U1"


def test_inventory_mapping_bi_implication_grid() -> None:
    # For formal items: MAPPED iff (non-empty requirement ids and all
    # registered); AMBIGUOUS iff (non-empty and some unresolved); UNMAPPED
    # iff empty. Asserted over the full small grid.
    for refs in ((), ("REQ-A",), ("REQ-A", "REQ-B")):
        for registered in ((), ("REQ-A",), ("REQ-A", "REQ-B"), ("REQ-B", "REQ-A")):
            assessment = evaluate_item_mapping(
                make_item("INV-1", requirement_ids=refs), registered
            )
            if not refs:
                expected = MappingStatus.UNMAPPED
            elif all(rid in registered for rid in refs):
                expected = MappingStatus.MAPPED
            else:
                expected = MappingStatus.AMBIGUOUS
            assert assessment.mapping_status is expected, (refs, registered)


# ---------------------------------------------------------------------------
# API robustness
# ---------------------------------------------------------------------------


def test_inventory_wrong_types_raise_type_error(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    item = make_item("INV-1")
    requirement = make_requirement(
        "REQ-1", inventory_items=("INV-1",), goal_ids=("GOAL-1",)
    )
    with pytest.raises(TypeError):
        register_inventory_item(123, item)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_inventory_item(root, "INV-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_requirement(root, 42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        read_inventory_item(root, 7)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        read_requirement(root, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_item_mapping("INV-1", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_item_mapping(item, "REQ-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_item_mapping(item, (7,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        summarize_inventory("items", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        summarize_inventory((item,), "requirements")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        summarize_inventory((requirement,), ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        resolve_goal_mappings(item, (item,))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        unresolved_requirement_ids(item, (1,))  # type: ignore[arg-type]


def test_inventory_errors_are_planning_value_errors() -> None:
    for exc_type in (
        DuplicateInventoryItemError,
        DuplicateRequirementError,
        InventoryItemNotFoundError,
        RequirementNotFoundError,
        UnresolvedItemReferenceError,
        InvalidRegistryIdError,
    ):
        assert issubclass(exc_type, InventoryError)
        assert issubclass(exc_type, ValueError)


def test_inventory_input_dict_forms_are_accepted(tmp_path: Path) -> None:
    root = init_project(tmp_path / "project")
    # Schema-shaped dict input with the fields of the FDM-201 example
    # (examples/fdm-201/inventory.example.yaml), minus the bookkeeping.
    stored = register_inventory_item(
        root,
        {
            "inventory_id": "INV-MAIN-ADS-001",
            "source_id": "SRC-TARGET-PAPER",
            "source_location": "main adsorption figure/section to be verified",
            "item_type": "experiment",
            "formal_report": True,
            "description": "Single-component C3H6 adsorption isotherm.",
            "conditions": {"adsorbate": "C3H6", "temperature_K": 298},
            "requirement_ids": ["REQ-ADS-001"],
        },
    )
    assert stored.inventory_id == "INV-MAIN-ADS-001"
    assert stored.conditions == {"adsorbate": "C3H6", "temperature_K": 298}
    assert stored.mapping_status is MappingStatus.AMBIGUOUS  # REQ not registered yet
    register_requirement(
        root,
        {
            "requirement_id": "REQ-ADS-001",
            "statement": "Reproduce the reported isotherm.",
            "inventory_items": ["INV-MAIN-ADS-001"],
            "criticality": "REQUIRED",
            "goal_ids": ["GOAL-ADS-001"],
            "outcome": "OPEN",
        },
    )
    registry = load_inventory_registry(root)
    summary = summarize_inventory(registry.items, registry.requirements)
    assert summary.mapped_items == 1
    assert summary.ambiguous_items == 0
    # The dict-form requirement round-trips.
    assert read_requirement(root, "REQ-ADS-001").statement == (
        "Reproduce the reported isotherm."
    )
