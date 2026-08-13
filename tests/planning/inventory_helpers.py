"""Shared test helpers for the planning/inventory tests (DEV-M4-G02).

``IDENTITY`` / ``TIMESTAMP`` pin every deterministic input the backing
``initialize_project`` call takes, so each test exercises the deterministic
path. The inventory records themselves carry no timestamp fields
(``schemas/inventory-item.schema.yaml``, ``schemas/requirement.schema.yaml``),
so state-content assertions compare exact bytes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    Criticality,
    InventoryItemType,
    MappingStatus,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
)
from scientific_reproduction.planning.init import initialize_project

#: Deterministic author/committer identity used by every init behind the
#: inventory tests.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects
#: (``17-FDM201-REFERENCE-CASE.md``).
DOI = "10.1039/D5TA00771B"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
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
