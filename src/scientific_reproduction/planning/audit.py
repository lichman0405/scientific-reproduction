"""Inventory completeness audit: 100% formal-item coverage gate (DEV-M4-G03).

Implements the **inventory completeness evaluator** and the **audit result
record** deliverables of DEV-M4-G03 over the frozen models and the
DEV-M4-G02 mapping rules (``planning/inventory.py``), grounded in:

* ``01-PRODUCT-REQUIREMENTS.md`` SS5: the Supervisor "creates a complete
  Reproduction Inventory" and the inventory "is audited for 100% coverage
  of formally reported items";
* ``14-STATE-GIT-ARTIFACTS.md`` SS5: the "Inventory audit passed" git
  checkpoint -- the audit gate is what blocks the Plan v1 freeze;
* ``core/models.py``: ``MappingStatus`` / ``ReproductionInventoryItem`` /
  ``ReproductionRequirement`` as the data, ``PlanInventoryAudit`` as the
  frozen counts shape, ``AuditStatus`` (PASS/FAIL) as the verdict
  vocabulary (the exact enum of ``schemas/plan.schema.yaml``
  ``inventory_audit.status``);
* ``planning/inventory.py`` (DEV-M4-G02): ``evaluate_item_mapping`` (the
  ordered rule table R-MAP-X1/A1/M1/U1 -> EXCLUDED_NONFORMAL / AMBIGUOUS /
  MAPPED / UNMAPPED), ``summarize_inventory`` (``InventorySummary``
  counts) and ``load_inventory_registry`` (the typed registry snapshot).

Verdict semantics (normative)
-----------------------------
The freeze-eligibility verdict is a pure function of the registered
inventory state, decided by a versioned ordered rule table
(``AUDIT_RULES``; the frozen rule-engine paradigm of ``core/rules/``,
``research/dedupe.py`` and the M4-G02 mapping table: pure deterministic
predicates, first match wins, every evaluation recorded):

1. ``R-AUD-U1``  at least one formally reported item is UNMAPPED
   (R-MAP-U1)                                      -> FAIL  (AC-01)
2. ``R-AUD-A1``  at least one item is AMBIGUOUS (R-MAP-A1); an
   unresolved mapping cannot be decided by the rules -> FAIL  (AC-02)
3. ``R-AUD-P1``  every formally reported item is MAPPED (R-MAP-M1)
   and no item is AMBIGUOUS (default)               -> PASS  (AC-02)

So one intentionally unmapped item prevents freeze eligibility (AC-01);
100% mapped with zero ambiguous items passes (AC-02); and an AMBIGUOUS
item always blocks (it is an unresolved mapping). Items ruled
EXCLUDED_NONFORMAL (R-MAP-X1) are outside the formal-item coverage
obligation and never fail the audit; ``R-MAP-A1`` only fires for formally
reported items, so AMBIGUOUS items are always formal.

An empty inventory (zero items, or only non-formal items) **passes
vacuously**: the coverage obligation ranges over formally reported items,
and with zero formal items there is no obligation -- consistent with the
``inventory.py`` convention that ``coverage`` is 0.0 when no formal item
is registered. An audit is a coverage gate, not an existence check; the
workflow phases (``ProjectPhase``) govern whether an empty inventory is
even reachable at audit time.

Statuses are always recomputed from the given state by the mapping rules
-- stored ``mapping_status`` snapshots are never trusted, matching the
M4-G02 counting convention -- so the verdict is deterministic and
order-independent.

The audit result record
-----------------------
``evaluate_completeness_audit`` returns the frozen :class:`CompletenessAudit`
record carrying:

* ``verdict`` -- the freeze-eligibility decision as the frozen
  ``AuditStatus`` (PASS/FAIL; the exact enum values of the frozen plan
  schema, ``schemas/plan.schema.yaml`` ``inventory_audit.status``);
* ``summary`` -- the ``InventorySummary`` counts (the vocabulary of the
  frozen ``PlanInventoryAudit`` / ``InventorySummary``: total /
  formally_reported / mapped / unmapped / ambiguous / excluded_nonformal
  items and coverage);
* the evidence tuples (AC-03): ``unmapped_item_ids`` and
  ``ambiguous_item_ids``, and ``offending_item_ids`` -- their union, in
  deterministic order (unique, **sorted by inventory_id**, which equals
  the registry order of ``load_inventory_registry``);
* the verdict rule trace (``decisions`` / ``matched_rule_id``) for
  auditability.

The frozen ``PlanInventoryAudit`` itself carries counts + an optional
``status`` but **no evidence list**, so it cannot be the record: AC-03
requires the offending item ids. The record therefore exposes
``plan_inventory_audit()``, producing the schema-compatible
``PlanInventoryAudit`` view (counts + ``status`` = verdict) that the Plan
freeze flow (DEV-M4-G04) embeds into the ``plan`` record -- the only
frozen place the audit result is persisted
(``schemas/plan.schema.yaml`` ``inventory_audit`` sub-object).

Persistence and git boundary (normative reading)
------------------------------------------------
This module is a **pure evaluator over in-memory state**: it persists
nothing and performs no Git operations. Rationale: ``schemas/`` has no
standalone audit schema (``core.schema_validation`` knows 21 object
types; ``plan.schema.yaml`` defines ``inventory_audit`` only as a nested
sub-object of the Plan), ``templates/PROJECT-TREE.template.txt`` has no
``audit/`` directory, and the inventory models carry no timestamp fields
-- a standalone persisted audit record would require inventing both a
schema and a tree entry, which are frozen. Persistence is the caller's
job: the Plan freeze flow (DEV-M4-G04) embeds the
``plan_inventory_audit()`` view into the Plan record, and the git
"Inventory audit passed" checkpoint (``audit/git.py`` checkpoint kind
``inventory.audit.passed``, ``14-STATE-GIT-ARTIFACTS.md`` SS5) is a
state-flow operation owned by the supervisor flow, not by this module.

Pure deterministic functions, no randomness, no wall-clock, no LLM;
``TypeError`` at the public boundaries; errors of the registry path
(``ProjectNotInitializedError``, corrupt-record ``ValueError``) are
propagated unchanged from ``planning/inventory.py`` with their stable
messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from scientific_reproduction.core.models import (
    AuditStatus,
    MappingStatus,
    PlanInventoryAudit,
    ReproductionInventoryItem,
    ReproductionRequirement,
)
from scientific_reproduction.planning.inventory import (
    InventorySummary,
    evaluate_item_mapping,
    load_inventory_registry,
    summarize_inventory,
)

__all__ = [
    "AUDIT_RULESET_VERSION",
    "AUDIT_RULES",
    "AuditInput",
    "CompletenessAudit",
    "CompletenessAuditDecision",
    "CompletenessAuditRule",
    "audit_inventory_registry",
    "evaluate_completeness_audit",
]

#: Version of the completeness audit rule table. Bumped whenever a rule
#: changes; recorded in every audit so old verdicts stay interpretable.
AUDIT_RULESET_VERSION: str = "1.0"


@dataclass(frozen=True)
class AuditInput:
    """The state a completeness verdict is a pure function of.

    Frozen and hashable so "same state -> same verdict" is directly
    testable and the exact input is preserved in every audit. The evidence
    id tuples are already deterministically sorted by ``inventory_id``;
    the summary counts are computed by ``summarize_inventory``.
    """

    summary: InventorySummary
    unmapped_item_ids: tuple[str, ...]
    ambiguous_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompletenessAuditRule:
    """One entry of the ordered completeness verdict rule table."""

    rule_id: str
    description: str
    verdict: AuditStatus
    predicate: Callable[[AuditInput], bool]


@dataclass(frozen=True)
class CompletenessAuditDecision:
    """Record of one verdict rule evaluation for a given state (auditability)."""

    rule_id: str
    description: str
    verdict: AuditStatus
    matched: bool


#: The ordered verdict rule table. First match wins; order is normative
#: (see the module docstring). Predicates are pure functions of the
#: :class:`AuditInput` only.
AUDIT_RULES: tuple[CompletenessAuditRule, ...] = (
    CompletenessAuditRule(
        rule_id="R-AUD-U1",
        description=(
            "at least one formally reported item is UNMAPPED (R-MAP-U1); "
            "an unmapped formal item prevents freeze eligibility (AC-01)"
        ),
        verdict=AuditStatus.FAIL,
        predicate=lambda i: bool(i.unmapped_item_ids),
    ),
    CompletenessAuditRule(
        rule_id="R-AUD-A1",
        description=(
            "at least one item is AMBIGUOUS (R-MAP-A1); an unresolved "
            "mapping cannot be decided by the rules and blocks the audit "
            "(AC-02: zero ambiguous items required)"
        ),
        verdict=AuditStatus.FAIL,
        predicate=lambda i: bool(i.ambiguous_item_ids),
    ),
    CompletenessAuditRule(
        rule_id="R-AUD-P1",
        description=(
            "every formally reported item is MAPPED (R-MAP-M1) and no item "
            "is AMBIGUOUS: 100% formal-item coverage (default, AC-02)"
        ),
        verdict=AuditStatus.PASS,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class CompletenessAudit:
    """The audit result record: verdict, counts and evidence (AC-01/02/03).

    ``verdict`` is the freeze-eligibility decision as the frozen
    ``AuditStatus`` (PASS/FAIL). The counts live in ``summary`` (an
    ``InventorySummary``, the vocabulary of the frozen
    ``PlanInventoryAudit``). ``unmapped_item_ids`` / ``ambiguous_item_ids``
    name the offending items of each class and ``offending_item_ids`` is
    their union -- unique, in deterministic sorted-by-``inventory_id``
    order (AC-03), which equals the registry order of
    ``load_inventory_registry``. ``decisions`` / ``matched_rule_id``
    record the verdict rule trace.
    """

    verdict: AuditStatus
    summary: InventorySummary
    unmapped_item_ids: tuple[str, ...]
    ambiguous_item_ids: tuple[str, ...]
    offending_item_ids: tuple[str, ...]
    decisions: tuple[CompletenessAuditDecision, ...]
    matched_rule_id: str

    @property
    def freeze_eligible(self) -> bool:
        """True iff the verdict is PASS (the Plan freeze gate of DEV-M4-G04)."""
        return self.verdict is AuditStatus.PASS

    def plan_inventory_audit(self) -> PlanInventoryAudit:
        """Return the frozen ``PlanInventoryAudit`` view of this record.

        Schema-compatible with the ``inventory_audit`` sub-object of
        ``schemas/plan.schema.yaml`` (``core/models.py``): the counts are
        the summary's and ``status`` is the verdict. The Plan freeze flow
        (DEV-M4-G04) embeds this view into the Plan record -- the only
        frozen place the audit result is persisted (see the module
        docstring).
        """
        return PlanInventoryAudit(
            formally_reported_items=self.summary.formally_reported_items,
            mapped_items=self.summary.mapped_items,
            unmapped_items=self.summary.unmapped_items,
            ambiguous_items=self.summary.ambiguous_items,
            coverage=self.summary.coverage,
            status=self.verdict,
        )


def evaluate_completeness_audit(
    items: Sequence[ReproductionInventoryItem],
    requirements: Sequence[ReproductionRequirement],
) -> CompletenessAudit:
    """Evaluate the completeness audit over an in-memory inventory state.

    Pure and deterministic (AC-02): the verdict is a pure function of the
    given items and requirements. Every formal item's status is recomputed
    from the given requirement set by the mapping rule table -- stored
    ``mapping_status`` snapshots are never trusted -- and non-formal
    items never fail the audit (R-MAP-X1). The result does not depend on
    the order of ``items`` or ``requirements``, and no wall clock,
    randomness or runtime state participates. Nothing is persisted and no
    Git operation is performed (see the module docstring).

    Args:
        items: the inventory items (typed ``ReproductionInventoryItem``).
        requirements: the registered requirements (typed
            ``ReproductionRequirement``).

    Returns:
        The frozen :class:`CompletenessAudit` record (verdict + counts +
        evidence + rule trace).

    Raises:
        TypeError: ``items`` or ``requirements`` is not a sequence (a
            ``str``/``bytes`` is rejected explicitly), or an element is
            not a ``ReproductionInventoryItem`` /
            ``ReproductionRequirement``.
    """
    items_tuple = _coerce_item_sequence(items, "items")
    requirements_tuple = _coerce_requirement_sequence(
        requirements, "requirements"
    )
    registered_ids = frozenset(
        r.requirement_id for r in requirements_tuple
    )
    unmapped_ids: list[str] = []
    ambiguous_ids: list[str] = []
    for item in items_tuple:
        if not item.formal_report:
            continue
        status = evaluate_item_mapping(item, registered_ids).mapping_status
        if status is MappingStatus.UNMAPPED:
            unmapped_ids.append(item.inventory_id)
        elif status is MappingStatus.AMBIGUOUS:
            ambiguous_ids.append(item.inventory_id)
    # Deterministic evidence: unique ids, sorted (registry order of
    # load_inventory_registry), independent of the input order (AC-03).
    unmapped = tuple(sorted(set(unmapped_ids)))
    ambiguous = tuple(sorted(set(ambiguous_ids)))
    audit_input = AuditInput(
        summary=summarize_inventory(items_tuple, requirements_tuple),
        unmapped_item_ids=unmapped,
        ambiguous_item_ids=ambiguous,
    )
    decisions: list[CompletenessAuditDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = AuditStatus.PASS  # unreachable default
    for rule in AUDIT_RULES:
        matched = rule.predicate(audit_input)
        decisions.append(
            CompletenessAuditDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
    # R-AUD-P1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return CompletenessAudit(
        verdict=matched_verdict,
        summary=audit_input.summary,
        unmapped_item_ids=unmapped,
        ambiguous_item_ids=ambiguous,
        offending_item_ids=tuple(sorted(set((*unmapped, *ambiguous)))),
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


def audit_inventory_registry(root: str | Path) -> CompletenessAudit:
    """Audit the registered inventory state at an initialized workspace root.

    Convenience composition of ``load_inventory_registry`` and
    ``evaluate_completeness_audit``: the verdict is a pure function of the
    registered state (items and requirements, each sorted by id). No
    record is persisted and no Git operation is performed (see the module
    docstring).

    Args:
        root: the initialized workspace root.

    Returns:
        The frozen :class:`CompletenessAudit` record for the registered
        state.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored inventory or requirement record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    registry = load_inventory_registry(root)
    return evaluate_completeness_audit(registry.items, registry.requirements)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
