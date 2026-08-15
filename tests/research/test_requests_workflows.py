"""Tests for the bootstrap research workflow contract (DEV-M5-G02).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_ac01_*``: the bootstrap workflow contract covers
    exactly the six categories (paper / SI / data / structure / citations
    / related methods): the ordered workflow table has one step per
    category in the goal's canonical order; every one of the frozen
    ``SourceType`` members (``schemas/source.schema.yaml``) maps to
    exactly one category (total, disjoint); every 09-RESEARCH-SUBSYSTEM.md
    section 2 acquisition bullet is covered by exactly one step; the
    contract is versioned and frozen;
  * the primary-target metadata registration obligation -- the W-BOOT-1
    primary paper step carries the bootstrap's first-class
    metadata-registration obligation: ``TARGET_METADATA_REGISTRATION``
    binds to the primary paper step, resolves to the real
    ``planning.init.register_target_metadata`` API, is frozen, and
    describes what is registered (the PDF-target DOI/title case).

Invariants: the category mapping is a pure deterministic function of the
frozen vocabulary (equal inputs -> equal outputs), the table is
immutable (frozen dataclasses in a tuple), and the public boundaries
raise ``TypeError`` for non-``SourceType`` / non-``BootstrapCategory``
arguments.
"""

from __future__ import annotations

import dataclasses

import pytest

from scientific_reproduction.core.models import SourceType
from scientific_reproduction.planning import init as planning_init
from scientific_reproduction.research.workflows import (
    BOOTSTRAP_CATEGORIES,
    BOOTSTRAP_WORKFLOW,
    BOOTSTRAP_WORKFLOW_VERSION,
    SPEC_ACQUISITION_ITEMS,
    TARGET_METADATA_REGISTRATION,
    BootstrapCategory,
    BootstrapStep,
    TargetMetadataRegistration,
    bootstrap_category_for_source_type,
    bootstrap_source_types,
)

# The goal's canonical category order (AC-01 wording: paper/SI/data/
# structure/citations/related methods).
GOAL_CATEGORY_ORDER = (
    BootstrapCategory.PAPER,
    BootstrapCategory.SI,
    BootstrapCategory.DATA,
    BootstrapCategory.STRUCTURE,
    BootstrapCategory.CITATIONS,
    BootstrapCategory.RELATED_METHODS,
)

# The normative mapping locked by the contract (module docstring): every
# frozen SourceType lands in exactly one category.
EXPECTED_CATEGORY_MAPPING: dict[BootstrapCategory, set[SourceType]] = {
    BootstrapCategory.PAPER: {
        SourceType.TARGET_PAPER,
        SourceType.PEER_REVIEWED_PAPER,
        SourceType.REVIEW,
        SourceType.THESIS,
        SourceType.PREPRINT,
    },
    BootstrapCategory.SI: {SourceType.SUPPLEMENTARY_INFORMATION},
    BootstrapCategory.DATA: {SourceType.DATASET, SourceType.DATABASE_RECORD},
    BootstrapCategory.STRUCTURE: {SourceType.STRUCTURE_DEPOSITION},
    BootstrapCategory.CITATIONS: {
        SourceType.STANDARD,
        SourceType.OFFICIAL_DOCUMENTATION,
        SourceType.VENDOR_NOTE,
    },
    BootstrapCategory.RELATED_METHODS: {SourceType.INFORMAL, SourceType.OTHER},
}


# ---------------------------------------------------------------------------
# AC-01: six-category coverage of the bootstrap workflow contract
# ---------------------------------------------------------------------------


def test_ac01_bootstrap_workflow_covers_six_categories() -> None:
    """The contract covers exactly the six goal categories, in order."""
    assert BOOTSTRAP_CATEGORIES == GOAL_CATEGORY_ORDER
    assert len(BOOTSTRAP_CATEGORIES) == 6
    assert {c.value for c in BOOTSTRAP_CATEGORIES} == {
        "paper",
        "si",
        "data",
        "structure",
        "citations",
        "related_methods",
    }
    # One step per category, in the same order, with sequential step ids.
    assert [step.category for step in BOOTSTRAP_WORKFLOW] == list(BOOTSTRAP_CATEGORIES)
    assert [step.step_id for step in BOOTSTRAP_WORKFLOW] == [
        f"W-BOOT-{i}" for i in range(1, 7)
    ]
    for step in BOOTSTRAP_WORKFLOW:
        assert isinstance(step, BootstrapStep)
        assert step.description


def test_ac01_bootstrap_workflow_links_frozen_source_vocabulary() -> None:
    """Every frozen SourceType maps to exactly one category (total/disjoint)."""
    for step in BOOTSTRAP_WORKFLOW:
        assert step.source_types == tuple(sorted(step.source_types))
    union = set()
    for step in BOOTSTRAP_WORKFLOW:
        assert not (union & set(step.source_types)), (
            f"step {step.step_id} reuses a source type"
        )
        union |= set(step.source_types)
    assert union == set(SourceType), "the workflow must cover all frozen SourceTypes"
    for category, expected in EXPECTED_CATEGORY_MAPPING.items():
        assert set(bootstrap_source_types(category)) == expected, (
            f"category {category.value!r} must map to the frozen vocabulary"
        )


def test_ac01_bootstrap_workflow_covers_all_spec_acquisition_items() -> None:
    """Every 09 section 2 acquisition bullet is covered by exactly one step."""
    covered: list[str] = []
    for step in BOOTSTRAP_WORKFLOW:
        assert step.spec_items, f"step {step.step_id} must cite spec items"
        for item in step.spec_items:
            assert item in SPEC_ACQUISITION_ITEMS, (
                f"step {step.step_id} cites an unknown spec item {item!r}"
            )
        covered.extend(step.spec_items)
    assert len(covered) == len(set(covered)), "spec items must not be repeated"
    assert set(covered) == set(SPEC_ACQUISITION_ITEMS), (
        "the workflow must cover every mandatory bootstrap acquisition bullet"
    )
    assert len(SPEC_ACQUISITION_ITEMS) == 12


def test_ac01_category_mapping_is_total_and_deterministic() -> None:
    """category_for_source_type is a total pure function of SourceType."""
    for source_type in SourceType:
        first = bootstrap_category_for_source_type(source_type)
        second = bootstrap_category_for_source_type(source_type)
        assert first is second, "equal inputs must yield equal outputs"
        assert first in BOOTSTRAP_CATEGORIES
        assert source_type in bootstrap_source_types(first), (
            "the mapping must be the inverse of the step source_types"
        )


def test_ac01_bootstrap_workflow_contract_is_versioned_and_frozen() -> None:
    """The contract is versioned and immutable (frozen steps, tuples)."""
    assert BOOTSTRAP_WORKFLOW_VERSION == "1.0"
    step = BOOTSTRAP_WORKFLOW[0]
    assert isinstance(step, BootstrapStep)
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.description = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        step.source_types += (SourceType.OTHER,)  # type: ignore[misc]
    # The tables themselves are tuples (immutable containers).
    assert isinstance(BOOTSTRAP_WORKFLOW, tuple)
    assert isinstance(BOOTSTRAP_CATEGORIES, tuple)
    assert step.source_types == step.source_types
    # No mutation occurred despite the attempted writes.
    assert "changed" not in {s.description for s in BOOTSTRAP_WORKFLOW}


# ---------------------------------------------------------------------------
# Primary-target metadata registration (the bootstrap's first-class
# metadata-registration obligation)
# ---------------------------------------------------------------------------


def test_target_metadata_registration_binds_to_primary_paper_step() -> None:
    """The obligation rides the W-BOOT-1 primary paper step."""
    assert isinstance(TARGET_METADATA_REGISTRATION, TargetMetadataRegistration)
    assert TARGET_METADATA_REGISTRATION.step_id == "W-BOOT-1"
    step = next(
        step
        for step in BOOTSTRAP_WORKFLOW
        if step.step_id == TARGET_METADATA_REGISTRATION.step_id
    )
    assert step.category == BootstrapCategory.PAPER
    # The primary paper step indeed covers the target paper acquisition.
    assert SourceType.TARGET_PAPER in step.source_types


def test_target_metadata_registration_api_resolves_in_planning_init() -> None:
    """The declared API is the real runtime registration function."""
    api = TARGET_METADATA_REGISTRATION.api
    assert api == "planning.init.register_target_metadata"
    parts = api.split(".")
    assert parts[:2] == ["planning", "init"]
    assert callable(getattr(planning_init, parts[-1]))


def test_target_metadata_registration_is_frozen() -> None:
    """The obligation is a frozen, immutable contract entry."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        TARGET_METADATA_REGISTRATION.description = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        TARGET_METADATA_REGISTRATION.step_id = "W-BOOT-7"  # type: ignore[misc]
    assert TARGET_METADATA_REGISTRATION.description != "changed"
    assert TARGET_METADATA_REGISTRATION.step_id == "W-BOOT-1"


def test_target_metadata_registration_describes_obligation() -> None:
    """The entry states what is registered, in the goal's vocabulary."""
    assert TARGET_METADATA_REGISTRATION.description
    assert "DOI" in TARGET_METADATA_REGISTRATION.description
    assert "title" in TARGET_METADATA_REGISTRATION.description


# ---------------------------------------------------------------------------
# Boundary type checks (public functions, TypeError at the boundary)
# ---------------------------------------------------------------------------


def test_bootstrap_category_for_source_type_rejects_non_source_type() -> None:
    with pytest.raises(TypeError, match="expects a SourceType"):
        bootstrap_category_for_source_type("target_paper")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expects a SourceType"):
        bootstrap_category_for_source_type(None)  # type: ignore[arg-type]


def test_bootstrap_source_types_rejects_non_category() -> None:
    with pytest.raises(TypeError, match="expects a BootstrapCategory"):
        bootstrap_source_types("paper")  # type: ignore[arg-type]


def test_bootstrap_workflow_steps_carry_unique_ids() -> None:
    step_ids = [step.step_id for step in BOOTSTRAP_WORKFLOW]
    assert len(step_ids) == len(set(step_ids))
