"""Reproduction Inventory registration and mapping rules (DEV-M4-G02).

Implements the three deliverables of DEV-M4-G02 over the frozen models
``ReproductionInventoryItem`` (``schemas/inventory-item.schema.yaml`` /
``core/models.py``) and ``ReproductionRequirement``
(``schemas/requirement.schema.yaml`` / ``core/models.py``):

* **inventory registry** -- ``register_inventory_item`` registers items into
  the workspace ``inventory/`` directory
  (``templates/PROJECT-TREE.template.txt``) as canonical JSON
  (``14-STATE-GIT-ARTIFACTS.md`` SS2/SS3) through
  ``core.atomic.atomic_write``, gated by ``core.schema_validation``
  ``validate_and_reject`` before persistence. The registry keys on
  ``inventory_id`` (the frozen model's identity; the FDM example uses
  curated ids like ``INV-MAIN-ADS-001``,
  ``examples/fdm-201/inventory.example.yaml``): a duplicate registration is
  rejected deterministically with a stable ``DuplicateInventoryItemError``.
  When the caller does not supply an ``inventory_id``, one is derived
  deterministically from the canonical fields
  (``core.ids.generate_id``), so registering the same item twice produces
  the same id and is rejected the same way.
* **inventory-to-requirement mapping** -- ``register_requirement`` registers
  ``ReproductionRequirement`` records into ``requirements/``. Per
  ``05-GOAL-RUN-SCHEMA.md`` SS2 a Requirement "represents one formally
  reported result/procedure obligation from the target paper inventory" and
  contains "source inventory item(s)"; the requirement record's
  ``inventory_items`` field is exactly that edge. Registration rejects
  requirements that reference unregistered items
  (``UnresolvedItemReferenceError``), so the authoring order is
  deterministic: inventory items first, then the requirements that map them.
* **requirement-to-goal mapping** -- the requirement record's ``goal_ids``
  field is the frozen edge of the mapping ("It may map to one or more Goals",
  ``05-GOAL-RUN-SCHEMA.md`` SS2). ``resolve_goal_mappings`` /
  ``mapped_goal_ids`` resolve the transitive item -> requirement -> goal
  mapping: a formal item can therefore map to one or more Goals through one
  or more Requirements (AC-01), and every mapping edge carries the item's
  ``source_id`` / ``source_location`` provenance (AC-03).

Mapping rules (AC-02: deterministic counting)
---------------------------------------------
The item-level mapping status is decided by a versioned, ordered rule table
(``MAPPING_RULES``; the frozen rule-engine paradigm of ``core/rules/`` and
``research/dedupe.py``: pure deterministic predicates, first match wins,
every rule evaluation recorded in an auditable assessment):

1. ``R-MAP-X1``  the item is not formally reported
                  (``formal_report`` is False)                     -> EXCLUDED_NONFORMAL
2. ``R-MAP-A1``  the formal item references requirement id(s) that
                 are not registered; the mapping cannot be decided
                 by the rules                                      -> AMBIGUOUS
3. ``R-MAP-M1``  the formal item carries requirement id(s), every
                 referenced requirement is registered              -> MAPPED
4. ``R-MAP-U1``  no requirement mapping is recorded (default)       -> UNMAPPED

"Formally reported" means all formally reported experiments, controls and
computations of the main paper, SI and linked public data (``00-README.md``;
``01-PRODUCT-REQUIREMENTS.md`` SS5: the Supervisor "creates a complete
Reproduction Inventory" and the inventory "is audited for 100% coverage of
formally reported items"). Ambiguities are explicit rather than silently
omitted (``23-DEVELOPMENT-GOAL-CATALOG.md`` DEV-M12-G02): an item whose
``requirement_ids`` reference requirements the registry does not hold is
``AMBIGUOUS`` and receives a stable ``ambiguity_notes`` string naming the
unresolved ids.

Counting (``summarize_inventory``) is a pure function of the registered
state: statuses are recomputed from the state at summary time -- never
trusted from stored snapshots -- so the mapped/unmapped/ambiguous counts are
deterministic and order-independent (AC-02). Registration persists the
rule-computed status as the item's ``mapping_status`` snapshot (the schema
requires the field); if a referenced requirement is registered later, the
stored snapshot stays untouched but every summary recomputes the status from
the fuller state. The summary's count fields mirror the frozen
``PlanInventoryAudit`` shape (``core/models.py``) that the completeness
audit (DEV-M4-G03) builds on.

Registration is a state-authoring operation (atomic JSON writes, no wall
clock anywhere: the inventory models carry no timestamp fields); the Git
"Inventory audit passed" checkpoint commit belongs to the completeness audit
(``14-STATE-GIT-ARTIFACTS.md`` SS5, DEV-M4-G03). Every registry id is
validated as a single path segment so registry files can never escape their
directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import AbstractSet, Any, Callable, Mapping, Sequence, TypeAlias

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    InventoryItemType,
    MappingStatus,
    ReproductionInventoryItem,
    ReproductionRequirement,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    PlanningError,
    ProjectNotInitializedError,
)

__all__ = [
    "INVENTORY_STATE_DIR",
    "MAPPING_RULESET_VERSION",
    "REQUIREMENTS_STATE_DIR",
    "DuplicateInventoryItemError",
    "DuplicateRequirementError",
    "InventoryError",
    "InventoryItemInput",
    "InventoryItemNotFoundError",
    "InventoryRegistry",
    "InventorySummary",
    "InvalidRegistryIdError",
    "ItemGoalMapping",
    "ItemMappingAssessment",
    "ItemMappingInput",
    "ItemMappingRule",
    "ItemMappingRuleDecision",
    "MAPPING_RULES",
    "RequirementInput",
    "RequirementNotFoundError",
    "UnresolvedItemReferenceError",
    "evaluate_item_mapping",
    "list_inventory_items",
    "list_requirements",
    "load_inventory_registry",
    "mapped_goal_ids",
    "read_inventory_item",
    "read_requirement",
    "register_inventory_item",
    "register_requirement",
    "resolve_goal_mappings",
    "summarize_inventory",
    "unresolved_requirement_ids",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InventoryError(PlanningError):
    """Base class for all reproduction inventory errors."""


class DuplicateInventoryItemError(InventoryError, ValueError):
    """Raised when an ``inventory_id`` is registered a second time."""


class DuplicateRequirementError(InventoryError, ValueError):
    """Raised when a ``requirement_id`` is registered a second time."""


class InventoryItemNotFoundError(InventoryError, ValueError):
    """Raised when reading an inventory item id that is not registered."""


class RequirementNotFoundError(InventoryError, ValueError):
    """Raised when reading a requirement id that is not registered."""


class UnresolvedItemReferenceError(InventoryError, ValueError):
    """Raised when a requirement references unregistered inventory items."""


class InvalidRegistryIdError(InventoryError, ValueError):
    """Raised when an id is not a safe single registry path segment."""


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Workspace directory holding the inventory item records
#: (``templates/PROJECT-TREE.template.txt``).
INVENTORY_STATE_DIR: str = "inventory"

#: Workspace directory holding the requirement records
#: (``templates/PROJECT-TREE.template.txt``).
REQUIREMENTS_STATE_DIR: str = "requirements"

#: Version of the item mapping rule table. Bumped whenever a rule changes;
#: recorded in every assessment so old decisions stay interpretable.
MAPPING_RULESET_VERSION: str = "1.0"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: A user-supplied inventory item: the typed model or a schema-shaped dict.
InventoryItemInput: TypeAlias = ReproductionInventoryItem | Mapping[str, Any]

#: A user-supplied requirement: the typed model or a schema-shaped dict.
RequirementInput: TypeAlias = ReproductionRequirement | Mapping[str, Any]

# ---------------------------------------------------------------------------
# Item mapping rules (AC-01 / AC-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemMappingInput:
    """The state a mapping decision is a pure function of.

    Frozen and hashable so "same state -> same mapping status" is directly
    testable and the exact input is preserved in every assessment. The
    registered requirement set is order-free (a ``frozenset``): counting
    must not depend on the runtime order of the registry (AC-02).
    """

    item: ReproductionInventoryItem
    registered_requirement_ids: frozenset[str]


@dataclass(frozen=True)
class ItemMappingRule:
    """One entry of the ordered item mapping rule table."""

    rule_id: str
    description: str
    status: MappingStatus
    predicate: Callable[[ItemMappingInput], bool]


@dataclass(frozen=True)
class ItemMappingRuleDecision:
    """Record of one rule evaluation for a given state (auditability)."""

    rule_id: str
    description: str
    status: MappingStatus
    matched: bool


def unresolved_requirement_ids(
    item: ReproductionInventoryItem,
    registered_requirement_ids: Sequence[str] | AbstractSet[str],
) -> tuple[str, ...]:
    """Return the item's requirement references missing from the registry.

    Deterministic: the result is a pure function of the item and the
    registered id set, in the item's ``requirement_ids`` order.

    Raises:
        TypeError: ``item`` is not a ``ReproductionInventoryItem``, or
            ``registered_requirement_ids`` is not a sequence/set of str.
    """
    if not isinstance(item, ReproductionInventoryItem):
        raise TypeError(
            "unresolved_requirement_ids expects a ReproductionInventoryItem,"
            f" got {type(item).__name__}"
        )
    if isinstance(registered_requirement_ids, (str, bytes)) or not isinstance(
        registered_requirement_ids, (Sequence, AbstractSet)
    ):
        raise TypeError(
            "registered_requirement_ids must be a sequence or set of str, got"
            f" {type(registered_requirement_ids).__name__}"
        )
    for rid in registered_requirement_ids:
        if not isinstance(rid, str):
            raise TypeError(
                "registered_requirement_ids must contain only str, got"
                f" {type(rid).__name__}"
            )
    registered = frozenset(registered_requirement_ids)
    return tuple(rid for rid in item.requirement_ids if rid not in registered)


#: The ordered rule table deciding an item's mapping status. First match
#: wins; order is normative (see the module docstring). Predicates are pure
#: functions of the :class:`ItemMappingInput` only.
MAPPING_RULES: tuple[ItemMappingRule, ...] = (
    ItemMappingRule(
        rule_id="R-MAP-X1",
        description=(
            "the item is not formally reported (formal_report is False); "
            "non-formal items are excluded from the formal-item mapping "
            "obligation"
        ),
        status=MappingStatus.EXCLUDED_NONFORMAL,
        predicate=lambda i: not i.item.formal_report,
    ),
    ItemMappingRule(
        rule_id="R-MAP-A1",
        description=(
            "the formal item references requirement id(s) that are not "
            "registered; the mapping cannot be decided by the rules"
        ),
        status=MappingStatus.AMBIGUOUS,
        predicate=lambda i: bool(
            unresolved_requirement_ids(i.item, i.registered_requirement_ids)
        ),
    ),
    ItemMappingRule(
        rule_id="R-MAP-M1",
        description=(
            "the formal item carries requirement id(s) and every referenced "
            "requirement is registered"
        ),
        status=MappingStatus.MAPPED,
        predicate=lambda i: bool(i.item.requirement_ids),
    ),
    ItemMappingRule(
        rule_id="R-MAP-U1",
        description="no requirement mapping is recorded for the formal item (default)",
        status=MappingStatus.UNMAPPED,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class ItemMappingAssessment:
    """Full, auditable result of an item mapping decision (AC-02/AC-03).

    ``input`` is the exact state the decision was computed from;
    ``decisions`` records the outcome of every rule in the table (in
    evaluation order); ``matched_rule_id`` names the deciding rule (``None``
    is impossible: the final default rule always matches);
    ``ambiguity_notes`` is the stable note naming the unresolved requirement
    ids when the decision is ``AMBIGUOUS`` (``None`` otherwise).
    """

    input: ItemMappingInput
    mapping_status: MappingStatus
    decisions: tuple[ItemMappingRuleDecision, ...]
    matched_rule_id: str
    ambiguity_notes: str | None


def evaluate_item_mapping(
    item: ReproductionInventoryItem,
    registered_requirement_ids: Sequence[str] | AbstractSet[str],
) -> ItemMappingAssessment:
    """Decide an item's mapping status with the ordered rule table.

    Pure and deterministic: the status is a pure function of the item and
    the registered requirement id set (AC-02), and the returned
    :class:`ItemMappingAssessment` records the exact input and every rule
    decision for auditability (AC-03).

    Raises:
        TypeError: ``item`` is not a ``ReproductionInventoryItem``, or
            ``registered_requirement_ids`` is not a sequence/set of str.
    """
    if not isinstance(item, ReproductionInventoryItem):
        raise TypeError(
            "evaluate_item_mapping expects a ReproductionInventoryItem, got"
            f" {type(item).__name__}"
        )
    if isinstance(registered_requirement_ids, (str, bytes)) or not isinstance(
        registered_requirement_ids, (Sequence, AbstractSet)
    ):
        raise TypeError(
            "registered_requirement_ids must be a sequence or set of str, got"
            f" {type(registered_requirement_ids).__name__}"
        )
    ids_tuple = tuple(registered_requirement_ids)
    for rid in ids_tuple:
        if not isinstance(rid, str):
            raise TypeError(
                "registered_requirement_ids must contain only str, got"
                f" {type(rid).__name__}"
            )
    mapping_input = ItemMappingInput(
        item=item, registered_requirement_ids=frozenset(ids_tuple)
    )
    decisions: list[ItemMappingRuleDecision] = []
    matched_rule_id: str | None = None
    matched_status = MappingStatus.UNMAPPED  # unreachable default
    for rule in MAPPING_RULES:
        matched = rule.predicate(mapping_input)
        decisions.append(
            ItemMappingRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                status=rule.status,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_status = rule.status
    # R-MAP-U1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    ambiguity_notes: str | None = None
    if matched_status is MappingStatus.AMBIGUOUS:
        unresolved = unresolved_requirement_ids(item, ids_tuple)
        ambiguity_notes = (
            f"unresolved requirement reference(s): {', '.join(unresolved)}"
        )
    return ItemMappingAssessment(
        input=mapping_input,
        mapping_status=matched_status,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
        ambiguity_notes=ambiguity_notes,
    )


# ---------------------------------------------------------------------------
# Deterministic counting (AC-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventorySummary:
    """Deterministic counts over a registered inventory state (AC-02).

    The count fields mirror the frozen ``PlanInventoryAudit``
    (``core/models.py``) that the completeness audit (DEV-M4-G03) builds on:
    ``formally_reported_items`` counts only formally reported items
    (``00-README.md``), ``excluded_nonformal_items`` the rest, and
    ``coverage`` is ``mapped / formally_reported`` (0.0 when no formal item
    is registered).
    """

    total_items: int
    formally_reported_items: int
    mapped_items: int
    unmapped_items: int
    ambiguous_items: int
    excluded_nonformal_items: int
    coverage: float


def summarize_inventory(
    items: Sequence[ReproductionInventoryItem],
    requirements: Sequence[ReproductionRequirement],
) -> InventorySummary:
    """Count the registered inventory state deterministically (AC-02).

    The counts are a pure function of the state: every item's status is
    recomputed from the given requirement set by the rule table (stored
    ``mapping_status`` snapshots are never trusted), the result does not
    depend on the order of ``items`` or ``requirements``, and no wall clock,
    randomness or runtime state participates.

    Raises:
        TypeError: ``items`` or ``requirements`` is not a sequence (a
            ``str``/``bytes`` is rejected explicitly), or an element is not
            a ``ReproductionInventoryItem`` / ``ReproductionRequirement``.
    """
    items_tuple = _coerce_item_sequence(items, "items")
    requirements_tuple = _coerce_requirement_sequence(requirements, "requirements")
    registered_ids = frozenset(r.requirement_id for r in requirements_tuple)

    total = len(items_tuple)
    formally_reported = 0
    mapped = 0
    unmapped = 0
    ambiguous = 0
    for item in items_tuple:
        if not item.formal_report:
            continue
        formally_reported += 1
        status = evaluate_item_mapping(item, registered_ids).mapping_status
        if status is MappingStatus.MAPPED:
            mapped += 1
        elif status is MappingStatus.UNMAPPED:
            unmapped += 1
        elif status is MappingStatus.AMBIGUOUS:
            ambiguous += 1
    coverage = (mapped / formally_reported) if formally_reported else 0.0
    return InventorySummary(
        total_items=total,
        formally_reported_items=formally_reported,
        mapped_items=mapped,
        unmapped_items=unmapped,
        ambiguous_items=ambiguous,
        excluded_nonformal_items=total - formally_reported,
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Goal mapping resolution (AC-01 / AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemGoalMapping:
    """One item -> requirement -> goal mapping edge (AC-01, AC-03).

    Every edge preserves the item's provenance: ``inventory_id``,
    ``source_id`` and ``source_location`` (the item's source
    location/provenance reference, ``schemas/inventory-item.schema.yaml``)
    plus the requirement link that carries the edge.
    """

    inventory_id: str
    source_id: str
    source_location: str | None
    requirement_id: str
    goal_id: str


def resolve_goal_mappings(
    item: ReproductionInventoryItem,
    requirements: Sequence[ReproductionRequirement],
) -> tuple[ItemGoalMapping, ...]:
    """Resolve the goal mappings of one item (AC-01, AC-03).

    Walks the item's ``requirement_ids`` in record order and each
    requirement's ``goal_ids`` in record order, emitting one
    :class:`ItemGoalMapping` edge per (item, requirement, goal) triple with
    the item's source provenance preserved. A formally reported item whose
    requirements are all registered therefore resolves to one or more Goals
    (AC-01); requirement references that do not resolve in the given
    requirement set contribute no edge -- the rule table
    (``evaluate_item_mapping``) is the authority on such ambiguous states.
    Deterministic: a pure function of ``item`` and ``requirements``.

    Raises:
        TypeError: ``item`` is not a ``ReproductionInventoryItem``, or
            ``requirements`` is not a sequence of
            ``ReproductionRequirement``.
    """
    if not isinstance(item, ReproductionInventoryItem):
        raise TypeError(
            "resolve_goal_mappings expects a ReproductionInventoryItem, got"
            f" {type(item).__name__}"
        )
    requirements_tuple = _coerce_requirement_sequence(requirements, "requirements")
    by_id = {r.requirement_id: r for r in requirements_tuple}
    edges: list[ItemGoalMapping] = []
    for requirement_id in item.requirement_ids:
        requirement = by_id.get(requirement_id)
        if requirement is None:
            continue
        for goal_id in requirement.goal_ids:
            edges.append(
                ItemGoalMapping(
                    inventory_id=item.inventory_id,
                    source_id=item.source_id,
                    source_location=item.source_location,
                    requirement_id=requirement.requirement_id,
                    goal_id=goal_id,
                )
            )
    return tuple(edges)


def mapped_goal_ids(
    item: ReproductionInventoryItem,
    requirements: Sequence[ReproductionRequirement],
) -> tuple[str, ...]:
    """Return the distinct goal ids an item maps to (AC-01).

    First-seen order over ``resolve_goal_mappings``; a goal reached through
    several requirements appears once, so the result is the item's goal set.
    """
    seen: list[str] = []
    for mapping in resolve_goal_mappings(item, requirements):
        if mapping.goal_id not in seen:
            seen.append(mapping.goal_id)
    return tuple(seen)


# ---------------------------------------------------------------------------
# Registry: registration and reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventoryRegistry:
    """A typed snapshot of the registered inventory state.

    ``items`` and ``requirements`` are sorted by their ids, so the snapshot
    is deterministic for a given workspace; it is the input of the pure
    mapping functions (``summarize_inventory``, ``resolve_goal_mappings``).
    """

    items: tuple[ReproductionInventoryItem, ...]
    requirements: tuple[ReproductionRequirement, ...]


def register_inventory_item(
    root: str | Path, item: InventoryItemInput
) -> ReproductionInventoryItem:
    """Register one inventory item in the workspace ``inventory/`` directory.

    The item is schema-validated (``validate_and_reject``) and persisted as
    canonical JSON (``core.atomic.atomic_write``). Its ``mapping_status``
    and ``ambiguity_notes`` are always computed by the rule table from the
    currently registered requirements -- any values in the input are
    replaced -- so the stored record is fully determined by the input fields
    and the registered requirement set. A missing ``inventory_id`` is
    derived deterministically from the canonical fields (``source_id``,
    ``item_type``, ``description``) via ``core.ids.generate_id``.

    Args:
        root: the initialized workspace root.
        item: the item as a typed ``ReproductionInventoryItem`` or a
            schema-shaped mapping.

    Returns:
        The registered, rule-computed item record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``item`` is neither a
            ``ReproductionInventoryItem`` nor a mapping.
        ValueError: the item is schema-invalid (subclass
            ``SchemaValidationError``), its ``item_type`` is not a frozen
            enum value, or a required field is missing.
        InvalidRegistryIdError: the ``inventory_id`` is not a safe single
            path segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateInventoryItemError: an item with the same ``inventory_id``
            is already registered (stable message).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    item_model = _coerce_inventory_item(item)
    _validate_registry_id("inventory item", item_model.inventory_id)
    state_path = _item_path(project_root, item_model.inventory_id)
    if state_path.is_file():
        raise DuplicateInventoryItemError(
            f"inventory item {item_model.inventory_id!r} is already registered;"
            " an inventory_id is unique per item and duplicate registration is"
            " rejected"
        )
    assessment = evaluate_item_mapping(item_model, _registered_requirement_ids(project_root))
    final = replace(
        item_model,
        mapping_status=assessment.mapping_status,
        ambiguity_notes=assessment.ambiguity_notes,
    )
    validate_and_reject("inventory-item", final.to_dict())
    atomic_write(state_path, _canonical_json(final.to_dict()))
    return final


def register_requirement(
    root: str | Path, requirement: RequirementInput
) -> ReproductionRequirement:
    """Register one requirement and its item/goal mapping edges.

    Persists the requirement record into ``requirements/`` (canonical JSON,
    schema-validated). The record carries both mapping edges: its
    ``inventory_items`` (inventory-to-requirement) and its ``goal_ids``
    (requirement-to-goal, ``05-GOAL-RUN-SCHEMA.md`` SS2). Every
    ``inventory_items`` reference must already be registered, otherwise the
    registration is rejected with ``UnresolvedItemReferenceError`` -- the
    authoring order is items first, then the requirements that map them.
    Goal references are not required to exist yet: the frozen schema is
    bidirectional (a Goal also lists its ``requirement_ids``), and Goal
    records are created by the planning freeze flow (DEV-M4-G04). A missing
    ``requirement_id`` is derived deterministically from the statement.

    Args:
        root: the initialized workspace root.
        requirement: the requirement as a typed ``ReproductionRequirement``
            or a schema-shaped mapping.

    Returns:
        The registered requirement record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``requirement`` is neither
            a ``ReproductionRequirement`` nor a mapping.
        ValueError: the requirement is schema-invalid, or a required field
            is missing.
        InvalidRegistryIdError: the ``requirement_id`` is not a safe single
            path segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateRequirementError: a requirement with the same
            ``requirement_id`` is already registered (stable message).
        UnresolvedItemReferenceError: an ``inventory_items`` reference is
            not a registered inventory item (stable message).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    requirement_model = _coerce_requirement(requirement)
    _validate_registry_id("requirement", requirement_model.requirement_id)
    state_path = _requirement_path(project_root, requirement_model.requirement_id)
    if state_path.is_file():
        raise DuplicateRequirementError(
            f"requirement {requirement_model.requirement_id!r} is already"
            " registered; a requirement_id is unique per requirement and"
            " duplicate registration is rejected"
        )
    registered_items = _registered_inventory_ids(project_root)
    missing = [
        iid for iid in requirement_model.inventory_items if iid not in registered_items
    ]
    if missing:
        raise UnresolvedItemReferenceError(
            f"requirement {requirement_model.requirement_id!r} references"
            f" unregistered inventory item(s): {', '.join(missing)}; register"
            " the inventory items before the requirements that map them"
        )
    validate_and_reject("requirement", requirement_model.to_dict())
    atomic_write(state_path, _canonical_json(requirement_model.to_dict()))
    return requirement_model


def read_inventory_item(
    root: str | Path, inventory_id: str
) -> ReproductionInventoryItem:
    """Read one registered inventory item record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``inventory_id`` is not a
            str.
        InvalidRegistryIdError: ``inventory_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InventoryItemNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt (unparseable or not an
            object).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(inventory_id, str):
        raise TypeError(
            f"inventory_id must be a str, got {type(inventory_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_registry_id("inventory item", inventory_id)
    state_path = _item_path(project_root, inventory_id)
    if not state_path.is_file():
        raise InventoryItemNotFoundError(
            f"no inventory item with id {inventory_id!r} is registered at"
            f" {project_root}"
        )
    return _read_item_record(state_path)


def read_requirement(root: str | Path, requirement_id: str) -> ReproductionRequirement:
    """Read one registered requirement record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``requirement_id`` is not
            a str.
        InvalidRegistryIdError: ``requirement_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        RequirementNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt (unparseable or not an
            object).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(requirement_id, str):
        raise TypeError(
            f"requirement_id must be a str, got {type(requirement_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_registry_id("requirement", requirement_id)
    state_path = _requirement_path(project_root, requirement_id)
    if not state_path.is_file():
        raise RequirementNotFoundError(
            f"no requirement with id {requirement_id!r} is registered at"
            f" {project_root}"
        )
    return _read_requirement_record(state_path)


def list_inventory_items(
    root: str | Path,
) -> tuple[ReproductionInventoryItem, ...]:
    """List every registered inventory item, sorted by id (deterministic).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    directory = project_root / INVENTORY_STATE_DIR
    if not directory.is_dir():
        return ()
    records: list[ReproductionInventoryItem] = []
    for path in sorted(directory.glob("*.json")):
        records.append(_read_item_record(path))
    return tuple(records)


def list_requirements(root: str | Path) -> tuple[ReproductionRequirement, ...]:
    """List every registered requirement, sorted by id (deterministic).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    directory = project_root / REQUIREMENTS_STATE_DIR
    if not directory.is_dir():
        return ()
    records: list[ReproductionRequirement] = []
    for path in sorted(directory.glob("*.json")):
        records.append(_read_requirement_record(path))
    return tuple(records)


def load_inventory_registry(root: str | Path) -> InventoryRegistry:
    """Load a typed snapshot of the registered inventory state.

    Convenience composition of ``list_inventory_items`` and
    ``list_requirements``: the snapshot is the input of the pure mapping
    functions (``summarize_inventory``, ``resolve_goal_mappings``).
    """
    return InventoryRegistry(
        items=list_inventory_items(root),
        requirements=list_requirements(root),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _validate_registry_id(kind: str, value: str) -> None:
    """Reject ids that would escape the registry directory as file names.

    The registry maps ids to ``<id>.json`` files; ids containing path
    separators or the ``.``/``..`` segments could address files outside the
    registry directory, so they are rejected with a stable error.
    """
    if value in ("", ".", "..") or "/" in value or "\\" in value:
        raise InvalidRegistryIdError(
            f"invalid {kind} id {value!r}: ids must be non-empty single path"
            " segments (no '/', no '\\', not '.' or '..')"
        )


def _coerce_inventory_item(item: InventoryItemInput) -> ReproductionInventoryItem:
    """Return a typed item from either input form."""
    if isinstance(item, ReproductionInventoryItem):
        return item
    if isinstance(item, Mapping):
        data = dict(item)
        if "inventory_id" not in data:
            generated = _generated_inventory_id(data)
            if generated is not None:
                data["inventory_id"] = generated
        # The schema requires mapping_status; registration recomputes it
        # anyway (the rules decide), so absent input defaults safely here.
        data.setdefault("mapping_status", MappingStatus.UNMAPPED.value)
        return ReproductionInventoryItem.from_dict(data)
    raise TypeError(
        "inventory item must be a ReproductionInventoryItem or a mapping, got"
        f" {type(item).__name__}"
    )


def _generated_inventory_id(data: Mapping[str, Any]) -> str | None:
    """Deterministic id from the canonical fields, or None when incomplete."""
    source_id = data.get("source_id")
    raw_type = data.get("item_type")
    description = data.get("description")
    if not (
        isinstance(source_id, str)
        and isinstance(raw_type, str)
        and isinstance(description, str)
    ):
        return None
    try:
        item_type = InventoryItemType(raw_type)
    except ValueError:
        return None
    return generate_id("inventory", source_id, item_type.value, description)


def _coerce_requirement(requirement: RequirementInput) -> ReproductionRequirement:
    """Return a typed requirement from either input form."""
    if isinstance(requirement, ReproductionRequirement):
        return requirement
    if isinstance(requirement, Mapping):
        data = dict(requirement)
        if "requirement_id" not in data and isinstance(data.get("statement"), str):
            data["requirement_id"] = generate_id("requirement", data["statement"])
        return ReproductionRequirement.from_dict(data)
    raise TypeError(
        "requirement must be a ReproductionRequirement or a mapping, got"
        f" {type(requirement).__name__}"
    )


def _coerce_item_sequence(
    items: Sequence[ReproductionInventoryItem], name: str
) -> tuple[ReproductionInventoryItem, ...]:
    """Type-check a sequence of inventory items (str/bytes rejected)."""
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise TypeError(
            f"{name} must be a sequence of ReproductionInventoryItem, got"
            f" {type(items).__name__}"
        )
    result = tuple(items)
    for item in result:
        if not isinstance(item, ReproductionInventoryItem):
            raise TypeError(
                f"{name} must contain only ReproductionInventoryItem, got"
                f" {type(item).__name__}"
            )
    return result


def _coerce_requirement_sequence(
    requirements: Sequence[ReproductionRequirement], name: str
) -> tuple[ReproductionRequirement, ...]:
    """Type-check a sequence of requirements (str/bytes rejected)."""
    if isinstance(requirements, (str, bytes)) or not isinstance(
        requirements, Sequence
    ):
        raise TypeError(
            f"{name} must be a sequence of ReproductionRequirement, got"
            f" {type(requirements).__name__}"
        )
    result = tuple(requirements)
    for requirement in result:
        if not isinstance(requirement, ReproductionRequirement):
            raise TypeError(
                f"{name} must contain only ReproductionRequirement, got"
                f" {type(requirement).__name__}"
            )
    return result


def _registered_requirement_ids(root: Path) -> tuple[str, ...]:
    """Ids of the registered requirement records (from file names)."""
    directory = root / REQUIREMENTS_STATE_DIR
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(path.stem for path in directory.glob("*.json"))
    )


def _registered_inventory_ids(root: Path) -> frozenset[str]:
    """Ids of the registered inventory item records (from file names)."""
    directory = root / INVENTORY_STATE_DIR
    if not directory.is_dir():
        return frozenset()
    return frozenset(path.stem for path in directory.glob("*.json"))


def _item_path(root: Path, inventory_id: str) -> Path:
    return root / INVENTORY_STATE_DIR / f"{inventory_id}.json"


def _requirement_path(root: Path, requirement_id: str) -> Path:
    return root / REQUIREMENTS_STATE_DIR / f"{requirement_id}.json"


def _canonical_json(data: dict[str, object]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _read_item_record(path: Path) -> ReproductionInventoryItem:
    """Load and type an inventory item record, rejecting corrupt state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt inventory record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt inventory record at {path}: expected a JSON object"
        )
    return ReproductionInventoryItem.from_dict(raw)


def _read_requirement_record(path: Path) -> ReproductionRequirement:
    """Load and type a requirement record, rejecting corrupt state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt requirement record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt requirement record at {path}: expected a JSON object"
        )
    return ReproductionRequirement.from_dict(raw)
