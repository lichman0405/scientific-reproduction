"""Source deduplication rules: DOI/mirror collapse without losing versions
(DEV-M5-G01).

Implements the **deduplication rules** deliverable over the frozen
``ResearchSource`` model (``schemas/source.schema.yaml`` /
``core/models.py``), grounded in ``06-EVIDENCE-SYSTEM.md`` section 7
(*source identity using DOI/identifier/hash*; mirrors must not be treated
as independent evidence) and the frozen acceptance rule (*duplicate DOI
mirrors collapse to one source*). The engine follows the
frozen rule-engine paradigm of ``core/rules/`` (ordered rule table with
version constant, first-match-wins, every rule evaluation recorded in an
auditable assessment, ``TypeError`` at public boundaries, pure
deterministic rules -- no randomness, no wall-clock).

The ordered rule table
----------------------
Every rule is a pure predicate over a :class:`SourcePairInput` (two
records plus their canonical identities from ``sources.py``); the first
rule whose predicate matches decides the pair outcome and *all* rule
evaluations are recorded in the assessment:

1. ``R-DEDUPE-S0``  the two records are the same record (identical
   ``source_id``)                                        -> SAME_SOURCE
2. ``R-DEDUPE-S1``  either record is an SI/dataset/structure-deposition
   record; such records keep their own address (AC-02)   -> DISTINCT_SOURCES
3. ``R-DEDUPE-C1``  both records carry a DOI and their normalized DOIs
   are equal (AC-01)                                     -> SAME_SOURCE
4. ``R-DEDUPE-C2``  both records carry a stable identifier, neither
   carries a DOI, and the normalized identifiers are equal
                                                         -> SAME_SOURCE
5. ``R-DEDUPE-C3``  both records carry an http(s) URL, neither carries a
   DOI or stable identifier, and the normalized URLs are equal
                                                         -> SAME_SOURCE
6. ``R-DEDUPE-D1``  no identity dimension matches (default)
                                                         -> DISTINCT_SOURCES

Rule table bi-implication (normative)
-------------------------------------
For any two records, the pair outcome is ``SAME_SOURCE`` **if and only if**
their canonical identity keys (``canonical_identity`` in ``sources.py``)
are equal. Rule order is the proof: DOI outranks other identifiers, the
C2/C3 predicates require the identity dimensions they claim, and S1 fires
before any identity rule, matching the record-scoped keys of
SI/dataset/structure records. The tests assert the bi-implication over an
exhaustive variant grid, so clustering by key (``deduplicate_sources``) is
sound: every member of a cluster pairwise collapses with every other.

A shared DOI identifies the same *registered work*, so two collapsible
records with equal normalized DOIs collapse regardless of their exact
source types (a ``target_paper`` and a ``peer_reviewed_paper`` sharing a
DOI are mirrors of one work). Distinct versions are never merged here:
different versions carry different DOIs, and the record-scoped types keep
their own addresses (AC-02).

Collapse semantics (AC-03: provenance retention)
------------------------------------------------
``deduplicate_sources`` groups records by canonical key (first-seen order,
one group per key, duplicate ``source_id`` occurrences collapsed) and
merges each group into one :class:`CanonicalSource`:

* ``R-MERGE-1`` the canonical record is the **first member** in input
  order: ``source_id``, ``source_type``, ``title`` and ``provenance`` come
  from it unchanged (the earliest address wins);
* ``R-MERGE-2`` every optional field (``doi``, ``stable_identifier``,
  ``url_or_locator``, ``publication_year``, ``acquired_at``,
  ``local_artifact_id``, ``access_class``) is filled with the **first
  non-None** value among the members, in input order (the canonical
  ``doi`` keeps the first mirror's recorded casing);
* ``R-MERGE-3`` **all** distinct ``provenance`` strings of every member
  are retained in ``mirror_provenances`` (input order), and all distinct
  ``url_or_locator`` values in ``mirror_urls`` -- nothing a mirror
  contributed is discarded (AC-03).

For a multi-member group the assessment's decision trace is the evaluation
of the first two members; for a singleton group it is the self-evaluation
of the single member (``R-DEDUPE-S0`` matches: the record is its own
canonical source), and ``collapsed`` is False.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Sequence

from scientific_reproduction.core.models import ResearchSource
from scientific_reproduction.research.sources import (
    SourceIdentity,
    canonical_identity,
    is_mirror_collapsible,
)

__all__ = [
    "DEDUPE_RULESET_VERSION",
    "DedupeOutcome",
    "SourcePairInput",
    "SourceDedupeRule",
    "SourceDedupeDecision",
    "SOURCE_DEDUPE_RULES",
    "SourcePairAssessment",
    "evaluate_source_pair",
    "CanonicalSource",
    "SourceDedupeAssessment",
    "deduplicate_sources",
]

#: Version of the dedupe rule table. Bumped whenever a rule changes;
#: recorded in every assessment so old decisions stay interpretable.
DEDUPE_RULESET_VERSION: str = "1.0"


class DedupeOutcome(StrEnum):
    """Pair-level verdict of the dedupe rules."""

    SAME_SOURCE = "SAME_SOURCE"
    DISTINCT_SOURCES = "DISTINCT_SOURCES"


@dataclass(frozen=True)
class SourcePairInput:
    """The two records under comparison plus their canonical identities.

    Frozen and hashable so "same input -> same verdict" is directly
    testable and the exact input is preserved in every assessment.
    """

    left: ResearchSource
    right: ResearchSource
    left_identity: SourceIdentity
    right_identity: SourceIdentity


@dataclass(frozen=True)
class SourceDedupeRule:
    """One entry of the ordered source dedupe rule table."""

    rule_id: str
    description: str
    outcome: DedupeOutcome
    predicate: Callable[[SourcePairInput], bool]


@dataclass(frozen=True)
class SourceDedupeDecision:
    """Record of one rule evaluation for a given pair (auditability)."""

    rule_id: str
    description: str
    outcome: DedupeOutcome
    matched: bool


#: The ordered rule table. First match wins; order is normative (see the
#: module docstring). Predicates are pure functions of the pair input only.
SOURCE_DEDUPE_RULES: tuple[SourceDedupeRule, ...] = (
    SourceDedupeRule(
        rule_id="R-DEDUPE-S0",
        description=(
            "the two records are the same record (identical source_id)"
        ),
        outcome=DedupeOutcome.SAME_SOURCE,
        predicate=lambda i: i.left.source_id == i.right.source_id,
    ),
    SourceDedupeRule(
        rule_id="R-DEDUPE-S1",
        description=(
            "either record is an SI/dataset/structure-deposition record; "
            "such records keep their own address and never collapse (AC-02)"
        ),
        outcome=DedupeOutcome.DISTINCT_SOURCES,
        predicate=lambda i: (
            not is_mirror_collapsible(i.left.source_type)
            or not is_mirror_collapsible(i.right.source_type)
        ),
    ),
    SourceDedupeRule(
        rule_id="R-DEDUPE-C1",
        description=(
            "both records carry a DOI and their normalized DOIs are equal "
            "(AC-01: duplicate DOI mirrors collapse)"
        ),
        outcome=DedupeOutcome.SAME_SOURCE,
        predicate=lambda i: (
            i.left_identity.normalized_doi is not None
            and i.left_identity.normalized_doi == i.right_identity.normalized_doi
        ),
    ),
    SourceDedupeRule(
        rule_id="R-DEDUPE-C2",
        description=(
            "both records carry a stable identifier, neither carries a "
            "DOI, and the normalized identifiers are equal"
        ),
        outcome=DedupeOutcome.SAME_SOURCE,
        predicate=lambda i: (
            i.left_identity.normalized_stable_identifier is not None
            and i.left_identity.normalized_stable_identifier
            == i.right_identity.normalized_stable_identifier
        ),
    ),
    SourceDedupeRule(
        rule_id="R-DEDUPE-C3",
        description=(
            "both records carry an http(s) URL, neither carries a DOI or "
            "stable identifier, and the normalized URLs are equal"
        ),
        outcome=DedupeOutcome.SAME_SOURCE,
        predicate=lambda i: (
            i.left_identity.normalized_url is not None
            and i.left_identity.normalized_url == i.right_identity.normalized_url
        ),
    ),
    SourceDedupeRule(
        rule_id="R-DEDUPE-D1",
        description="no identity dimension matches (default)",
        outcome=DedupeOutcome.DISTINCT_SOURCES,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class SourcePairAssessment:
    """Full, auditable verdict for one source pair (AC-03 trace)."""

    input: SourcePairInput
    outcome: DedupeOutcome
    decisions: tuple[SourceDedupeDecision, ...]
    matched_rule_id: str


def evaluate_source_pair(
    left: ResearchSource, right: ResearchSource
) -> SourcePairAssessment:
    """Evaluate the dedupe rule table over one pair of source records.

    Pure and deterministic: the verdict is a pure function of the two
    records (equal records -> equal verdict). The returned
    :class:`SourcePairAssessment` records the exact pair, both canonical
    identities and every rule decision.

    Raises:
        TypeError: ``left`` or ``right`` is not a ``ResearchSource``.
        SourceNormalizationError: either record carries a malformed DOI
            (propagated from :func:`canonical_identity`).
    """
    if not isinstance(left, ResearchSource):
        raise TypeError(
            "evaluate_source_pair expects a ResearchSource, got"
            f" {type(left).__name__}"
        )
    if not isinstance(right, ResearchSource):
        raise TypeError(
            "evaluate_source_pair expects a ResearchSource, got"
            f" {type(right).__name__}"
        )
    pair_input = SourcePairInput(
        left=left,
        right=right,
        left_identity=canonical_identity(left),
        right_identity=canonical_identity(right),
    )
    decisions: list[SourceDedupeDecision] = []
    matched_rule_id: str | None = None
    matched_outcome = DedupeOutcome.DISTINCT_SOURCES  # unreachable default
    for rule in SOURCE_DEDUPE_RULES:
        matched = rule.predicate(pair_input)
        decisions.append(
            SourceDedupeDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                outcome=rule.outcome,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_outcome = rule.outcome
    # R-DEDUPE-D1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return SourcePairAssessment(
        input=pair_input,
        outcome=matched_outcome,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


@dataclass(frozen=True)
class CanonicalSource:
    """One collapsed cluster: the canonical record plus everything retained.

    ``canonical`` is the merged record (merge rules R-MERGE-1/R-MERGE-2,
    see the module docstring); ``members`` holds every source in the
    cluster in input order; ``mirror_provenances`` and ``mirror_urls``
    retain every distinct provenance string and locator contributed by the
    members (AC-03), so no mirror information is discarded.
    """

    canonical: ResearchSource
    identity: SourceIdentity
    members: tuple[ResearchSource, ...]
    mirror_provenances: tuple[str, ...]
    mirror_urls: tuple[str, ...]


@dataclass(frozen=True)
class SourceDedupeAssessment:
    """Full result of collapsing one canonical key group (auditability).

    ``identity`` is the group's canonical identity; ``canonical`` the
    merged :class:`CanonicalSource`; ``collapsed`` is True when more than
    one distinct record shares the key. ``decisions`` / ``matched_rule_id``
    are the trace of the pair evaluation that justifies the group (the
    first two members, or the single member's self-evaluation for a
    singleton group).
    """

    identity: SourceIdentity
    canonical: CanonicalSource
    collapsed: bool
    matched_rule_id: str
    decisions: tuple[SourceDedupeDecision, ...]


def deduplicate_sources(
    sources: Sequence[ResearchSource],
) -> tuple[SourceDedupeAssessment, ...]:
    """Collapse a sequence of source records into canonical sources.

    Records are grouped by canonical identity key (first-seen order, one
    group per key); within a group, duplicate ``source_id`` occurrences
    are collapsed (first occurrence wins). Every group is merged into one
    :class:`CanonicalSource` with all member provenance retained (AC-03)
    and returned as one :class:`SourceDedupeAssessment` -- so duplicate
    DOI mirrors yield exactly one canonical scholarly source (AC-01),
    while SI/dataset/structure records keep their own addresses (AC-02).
    Deterministic: the result depends only on ``sources`` and their order.

    Raises:
        TypeError: ``sources`` is not a sequence (a ``str``/``bytes`` is
            rejected explicitly), or an element is not a
            ``ResearchSource``.
        SourceNormalizationError: a record carries a malformed DOI.
    """
    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError(
            "deduplicate_sources expects a sequence of ResearchSource, got"
            f" {type(sources).__name__}"
        )
    items = tuple(sources)
    for item in items:
        if not isinstance(item, ResearchSource):
            raise TypeError(
                "deduplicate_sources expects ResearchSource elements, got"
                f" {type(item).__name__}"
            )

    groups: list[list[ResearchSource]] = []
    group_by_key: dict[str, list[ResearchSource]] = {}
    for source in items:
        key = canonical_identity(source).key
        group = group_by_key.get(key)
        if group is None:
            group = []
            group_by_key[key] = group
            groups.append(group)
        if not any(existing.source_id == source.source_id for existing in group):
            group.append(source)

    assessments: list[SourceDedupeAssessment] = []
    for group in groups:
        members = tuple(group)
        pair = evaluate_source_pair(
            members[0], members[1] if len(members) > 1 else members[0]
        )
        assessments.append(
            SourceDedupeAssessment(
                identity=canonical_identity(members[0]),
                canonical=_merge_group(members),
                collapsed=len(members) > 1,
                matched_rule_id=pair.matched_rule_id,
                decisions=pair.decisions,
            )
        )
    return tuple(assessments)


# ---------------------------------------------------------------------------
# Merge rules (see module docstring: R-MERGE-1 .. R-MERGE-3)
# ---------------------------------------------------------------------------

#: Optional fields filled by the first non-None member value (R-MERGE-2).
_MERGED_OPTIONAL_FIELDS: tuple[str, ...] = (
    "doi",
    "stable_identifier",
    "url_or_locator",
    "publication_year",
    "acquired_at",
    "local_artifact_id",
    "access_class",
)


def _merge_group(members: tuple[ResearchSource, ...]) -> CanonicalSource:
    """Merge one canonical-key group into a :class:`CanonicalSource`."""
    base = members[0]
    kwargs: dict[str, Any] = {}
    for field in _MERGED_OPTIONAL_FIELDS:
        for member in members:
            value = getattr(member, field)
            if value is not None:
                kwargs[field] = value
                break
    canonical = ResearchSource(
        source_id=base.source_id,
        source_type=base.source_type,
        title=base.title,
        provenance=base.provenance,
        **kwargs,
    )
    provenances: list[str] = []
    for member in members:
        if member.provenance not in provenances:
            provenances.append(member.provenance)
    urls: list[str] = []
    for member in members:
        if member.url_or_locator is not None and member.url_or_locator not in urls:
            urls.append(member.url_or_locator)
    return CanonicalSource(
        canonical=canonical,
        identity=canonical_identity(base),
        members=members,
        mirror_provenances=tuple(provenances),
        mirror_urls=tuple(urls),
    )
