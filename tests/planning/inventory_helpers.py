"""Shared test helpers for the planning/inventory tests (DEV-M4-G02).

``IDENTITY`` / ``TIMESTAMP`` pin every deterministic input the backing
``initialize_project`` call takes, so each test exercises the deterministic
path. The inventory records themselves carry no timestamp fields
(``schemas/inventory-item.schema.json``, ``schemas/requirement.schema.json``),
so state-content assertions compare exact bytes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import (
    Criticality,
    InventoryItemType,
    MappingStatus,
    ProjectPhase,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    ResearchSource,
    SourceType,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    initialize_project,
    read_project_state,
)
from scientific_reproduction.research.state_helpers import register_source

#: Deterministic author/committer identity used by every init behind the
#: inventory tests.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects
#: (``17-FDM201-REFERENCE-CASE.md``).
DOI = "10.1039/D5TA00771B"

#: Injected actor and recording stamps for the source registration
#: (no wall clock anywhere; mirrors the research suite).
ACTOR = "research"
RECORDED_AT = "2026-01-02T00:00:00Z"

#: The default provenance source id every helper-built item references.
SOURCE_ID = "SRC-TARGET-PAPER"


def register_default_source(root: Path) -> Path:
    """Register the default provenance source of the helper-built items.

    ``register_inventory_item`` resolves every item's ``source_id``
    against the workspace source registry, so the deterministic
    ``SRC-TARGET-PAPER`` record must be registered before the first item
    (items first, sources earlier -- the real authoring order).
    """
    register_source(
        root,
        ResearchSource(
            source_id=SOURCE_ID,
            source_type=SourceType.TARGET_PAPER,
            title="FDM-201 target paper (deterministic test provenance source)",
            provenance="test fixture",
            doi=DOI,
        ),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )
    return root


def author_project_phase(root: Path, phase: ProjectPhase) -> None:
    """Author ``phase`` on the registered project state record.

    The registry has no phase-authoring API (phase transitions are a
    supervisor-flow operation, out of scope), so tests author the phase
    by writing the project record in place with the registry's canonical
    serialization. ``updated_at`` is preserved: deterministic fixtures
    pin every timestamp.
    """
    project = read_project_state(root)
    updated = replace(project, project_phase=phase)
    validate_and_reject("project", updated.to_dict())
    atomic_write(
        root / PROJECT_STATE_FILENAME,
        json.dumps(updated.to_dict(), indent=2, sort_keys=True) + "\n",
    )


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it.

    The workspace is authored at ``REPRODUCTION_INVENTORY``: the freeze
    gate (issue #137) requires the registered phase to have reached the
    inventory phase, and the planning suite exercises that gate.
    """
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    register_default_source(root)
    author_project_phase(root, ProjectPhase.REPRODUCTION_INVENTORY)
    return root


def make_item(
    inventory_id: str,
    *,
    source_id: str = "SRC-TARGET-PAPER",
    item_type: InventoryItemType = InventoryItemType.EXPERIMENT,
    formal_report: bool = True,
    description: str = "Single-component C3H6 adsorption isotherm for FDM-201 at 298 K.",
    source_location: str | None = "main adsorption figure, 'Adsorption isotherms' section",
    requirement_ids: tuple[str, ...] = (),
    mapping_status: MappingStatus = MappingStatus.UNMAPPED,
    ambiguity_notes: str | None = None,
    **kwargs: Any,
) -> ReproductionInventoryItem:
    """Build a frozen ReproductionInventoryItem with compact defaults."""
    return ReproductionInventoryItem(
        inventory_id=inventory_id,
        source_id=source_id,
        item_type=item_type,
        formal_report=formal_report,
        description=description,
        mapping_status=mapping_status,
        source_location=source_location,
        requirement_ids=list(requirement_ids),
        ambiguity_notes=ambiguity_notes,
        **kwargs,
    )


def make_requirement(
    requirement_id: str,
    *,
    statement: str = "Reproduce the reported single-component adsorption isotherm.",
    inventory_items: tuple[str, ...] = (),
    goal_ids: tuple[str, ...] = (),
    criticality: Criticality = Criticality.REQUIRED,
    outcome: RequirementOutcome = RequirementOutcome.OPEN,
    **kwargs: Any,
) -> ReproductionRequirement:
    """Build a frozen ReproductionRequirement with compact defaults."""
    return ReproductionRequirement(
        requirement_id=requirement_id,
        statement=statement,
        inventory_items=list(inventory_items),
        criticality=criticality,
        goal_ids=list(goal_ids),
        outcome=outcome,
        **kwargs,
    )
