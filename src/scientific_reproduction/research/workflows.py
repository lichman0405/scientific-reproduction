"""Bootstrap research workflow contract: the six acquisition categories
(DEV-M5-G02).

Implements the **bootstrap research workflow contract** deliverable:
the frozen, versioned, ordered workflow table that a Research Agent
follows for the mandatory systematic acquisition *before Plan v1*. The
frozen spec grounds this module:

* ``09-RESEARCH-SUBSYSTEM.md`` section 2 ("Mandatory bootstrap
  research"): *Before Plan v1, Research must systematically acquire and
  index* -- the twelve acquisition bullets (primary paper, supplementary
  information, CIF/CCDC/structure files, linked data repository files,
  data-availability statements, key method references cited by the paper,
  relevant same-material papers, closely related materials/methods,
  same-author prior/subsequent methods, independent reproduction work if
  available, public database records, computational method sources).
* ``agent-contracts/RESEARCH.md``: the Research Agent's *startup
  obligation* is to *perform systematic bootstrap research before Plan
  v1*.
* ``CLAUDE-CODE-HANDOFF.md`` (M5): the *bootstrap-research workflow
  contract* is a milestone deliverable.

The contract (AC-01)
--------------------
The twelve acquisition bullets are organized into **exactly six
categories**, in the goal's canonical order: ``paper`` / ``si`` /
``data`` / ``structure`` / ``citations`` / ``related methods``. The
contract is the ordered workflow table :data:`BOOTSTRAP_WORKFLOW`: one
:class:`BootstrapStep` per category, each step carrying the spec bullets
it covers (``spec_items``) and the frozen ``SourceType`` members that
realize it (``source_types``). Every one of the frozen source categories
in ``core/models.SourceType`` (``schemas/source.schema.yaml``) maps to
exactly one category, so the contract is **total**: any source a Research
Agent acquires during bootstrap lands in exactly one step. The mapping is
deterministic (a pure function of the frozen vocabulary), versioned
(:data:`BOOTSTRAP_WORKFLOW_VERSION`), and immutable (frozen dataclasses
in a frozen tuple).

Category <-> frozen source vocabulary (normative mapping)
---------------------------------------------------------
* ``paper`` -- the primary paper and relevant same-material papers:
  ``TARGET_PAPER``, ``PEER_REVIEWED_PAPER``, ``REVIEW``, ``THESIS``,
  ``PREPRINT``;
* ``si`` -- supplementary information: ``SUPPLEMENTARY_INFORMATION``;
* ``data`` -- linked data repository files, data-availability
  statements and public database records: ``DATASET``,
  ``DATABASE_RECORD``;
* ``structure`` -- CIF/CCDC/structure files:
  ``STRUCTURE_DEPOSITION``;
* ``citations`` -- key method references cited by the paper
  (standards/manuals are method references per 09 section 4, *public
  standards/manuals where legally accessible*): ``STANDARD``,
  ``OFFICIAL_DOCUMENTATION``, ``VENDOR_NOTE``;
* ``related_methods`` -- closely related materials/methods, same-author
  prior/subsequent methods, independent reproduction work and
  computational method sources: ``INFORMAL`` (independent/negative
  reproduction reports are typically informal records, 09 section 6
  family 10) and ``OTHER`` (the catch-all so the mapping stays total).

The mapping is exhaustive and disjoint over all 14 frozen ``SourceType``
members (locked by the tests).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from scientific_reproduction.core.models import SourceType

__all__ = [
    "BOOTSTRAP_WORKFLOW_VERSION",
    "BootstrapCategory",
    "BootstrapStep",
    "BOOTSTRAP_CATEGORIES",
    "BOOTSTRAP_WORKFLOW",
    "SPEC_ACQUISITION_ITEMS",
    "bootstrap_category_for_source_type",
    "bootstrap_source_types",
]

#: Version of the bootstrap workflow contract. Bumped whenever a rule of
#: the table changes; the version travels with the contract so a Research
#: Agent's executed plan stays interpretable (auditability).
BOOTSTRAP_WORKFLOW_VERSION: str = "1.0"


class BootstrapCategory(StrEnum):
    """One of the six bootstrap acquisition categories (AC-01).

    The member values are the goal's own vocabulary (paper/SI/data/
    structure/citations/related methods), so category coverage is directly
    assertable against the acceptance criterion.
    """

    PAPER = "paper"
    SI = "si"
    DATA = "data"
    STRUCTURE = "structure"
    CITATIONS = "citations"
    RELATED_METHODS = "related_methods"


#: The six categories in the goal's canonical order (AC-01 wording).
BOOTSTRAP_CATEGORIES: tuple[BootstrapCategory, ...] = (
    BootstrapCategory.PAPER,
    BootstrapCategory.SI,
    BootstrapCategory.DATA,
    BootstrapCategory.STRUCTURE,
    BootstrapCategory.CITATIONS,
    BootstrapCategory.RELATED_METHODS,
)


@dataclass(frozen=True)
class BootstrapStep:
    """One entry of the ordered bootstrap workflow table.

    ``step_id`` is the stable step identifier (``W-BOOT-1`` .. ``W-BOOT-6``
    in table order); ``category`` names the acquisition category;
    ``description`` states what is acquired; ``spec_items`` lists the
    09-RESEARCH-SUBSYSTEM.md section 2 bullets the step covers; and
    ``source_types`` are the frozen ``SourceType`` members that realize
    the category in the source vocabulary.
    """

    step_id: str
    category: BootstrapCategory
    description: str
    spec_items: tuple[str, ...]
    source_types: tuple[SourceType, ...]


#: The ordered bootstrap workflow table. One step per category, in
#: ``BOOTSTRAP_CATEGORIES`` order; a Research Agent executes the steps in
#: this order. The union of ``source_types`` over all steps is the full
#: frozen ``SourceType`` vocabulary (total, disjoint -- locked by tests).
BOOTSTRAP_WORKFLOW: tuple[BootstrapStep, ...] = (
    BootstrapStep(
        step_id="W-BOOT-1",
        category=BootstrapCategory.PAPER,
        description=(
            "acquire the primary paper and relevant same-material papers "
            "(the target work and its scholarly corpus)"
        ),
        spec_items=("primary paper", "relevant same-material papers"),
        source_types=(
            SourceType.PEER_REVIEWED_PAPER,
            SourceType.PREPRINT,
            SourceType.REVIEW,
            SourceType.TARGET_PAPER,
            SourceType.THESIS,
        ),
    ),
    BootstrapStep(
        step_id="W-BOOT-2",
        category=BootstrapCategory.SI,
        description=(
            "acquire the supplementary information shipped with the "
            "primary paper"
        ),
        spec_items=("supplementary information",),
        source_types=(SourceType.SUPPLEMENTARY_INFORMATION,),
    ),
    BootstrapStep(
        step_id="W-BOOT-3",
        category=BootstrapCategory.DATA,
        description=(
            "acquire linked data repository files, data-availability "
            "statements and public database records"
        ),
        spec_items=(
            "linked data repository files",
            "data-availability statements",
            "public database records",
        ),
        source_types=(SourceType.DATABASE_RECORD, SourceType.DATASET),
    ),
    BootstrapStep(
        step_id="W-BOOT-4",
        category=BootstrapCategory.STRUCTURE,
        description=(
            "acquire CIF/CCDC/structure files for the target material"
        ),
        spec_items=("CIF/CCDC/structure files",),
        source_types=(SourceType.STRUCTURE_DEPOSITION,),
    ),
    BootstrapStep(
        step_id="W-BOOT-5",
        category=BootstrapCategory.CITATIONS,
        description=(
            "acquire the key method references cited by the paper "
            "(standards, official documentation, vendor notes)"
        ),
        spec_items=("key method references cited by the paper",),
        source_types=(
            SourceType.OFFICIAL_DOCUMENTATION,
            SourceType.STANDARD,
            SourceType.VENDOR_NOTE,
        ),
    ),
    BootstrapStep(
        step_id="W-BOOT-6",
        category=BootstrapCategory.RELATED_METHODS,
        description=(
            "acquire closely related materials/methods, same-author "
            "prior/subsequent methods, independent reproduction work and "
            "computational method sources"
        ),
        spec_items=(
            "closely related materials/methods",
            "same-author prior/subsequent methods",
            "independent reproduction work if available",
            "computational method sources",
        ),
        source_types=(SourceType.INFORMAL, SourceType.OTHER),
    ),
)

#: The 09-RESEARCH-SUBSYSTEM.md section 2 acquisition bullets; used by the
#: tests to prove every bullet is covered by exactly one step.
SPEC_ACQUISITION_ITEMS: tuple[str, ...] = (
    "primary paper",
    "supplementary information",
    "CIF/CCDC/structure files",
    "linked data repository files",
    "data-availability statements",
    "key method references cited by the paper",
    "relevant same-material papers",
    "closely related materials/methods",
    "same-author prior/subsequent methods",
    "independent reproduction work if available",
    "public database records",
    "computational method sources",
)


def bootstrap_category_for_source_type(source_type: SourceType) -> BootstrapCategory:
    """Return the single bootstrap category that acquires ``source_type``.

    Total and deterministic: every frozen ``SourceType`` member maps to
    exactly one category, so any source acquired during bootstrap lands in
    exactly one workflow step (AC-01).

    Raises:
        TypeError: ``source_type`` is not a ``SourceType``.
    """
    if not isinstance(source_type, SourceType):
        raise TypeError(
            "bootstrap_category_for_source_type expects a SourceType, got"
            f" {type(source_type).__name__}"
        )
    for step in BOOTSTRAP_WORKFLOW:
        if source_type in step.source_types:
            return step.category
    raise AssertionError(
        "bootstrap workflow is not total over SourceType: "
        f"{source_type.value!r} maps to no category"
    )


def bootstrap_source_types(category: BootstrapCategory) -> tuple[SourceType, ...]:
    """Return the frozen source types acquired by ``category``.

    The result is the ``source_types`` of the unique step whose category
    is ``category`` (in table order).

    Raises:
        TypeError: ``category`` is not a ``BootstrapCategory``.
    """
    if not isinstance(category, BootstrapCategory):
        raise TypeError(
            "bootstrap_source_types expects a BootstrapCategory, got"
            f" {type(category).__name__}"
        )
    for step in BOOTSTRAP_WORKFLOW:
        if step.category == category:
            return step.source_types
    raise AssertionError(
        "bootstrap workflow is not total over BootstrapCategory: "
        f"{category.value!r} has no step"
    )
