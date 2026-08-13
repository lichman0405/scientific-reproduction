"""Tests for source deduplication rules: DOI/mirror collapse (DEV-M5-G01).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_ac01_*``: duplicate DOI mirrors collapse to one
    canonical scholarly source: the collapse rules (``R-DEDUPE-C1``)
    identify mirrors through their normalized DOI regardless of recorded
    DOI form, mirror URL or collapsible source type; ``deduplicate_sources``
    yields exactly one assessment (one canonical record) per shared DOI.
  * AC-02 -- ``test_ac02_*``: distinct SI/dataset/structure records remain
    separately addressable: record-scoped identity plus the ``R-DEDUPE-S1``
    scoping rule keep a dataset/SI/structure record that carries the
    paper's DOI addressable on its own; even two datasets sharing the same
    DOI never collapse.
  * AC-03 -- ``test_ac03_*``: source provenance for mirrors is retained:
    the canonical record keeps every mirror's provenance string (and every
    mirror locator) in the merge result (``R-MERGE-3``).

Invariants: the pair verdict is SAME_SOURCE if and only if the canonical
identity keys are equal (asserted over an exhaustive variant grid), the
rule table is total and versioned, and every assessment records the full
rule-decision trace.
"""

from __future__ import annotations

import dataclasses
import itertools
from typing import Any

import pytest

from scientific_reproduction.core.models import ResearchSource, SourceType
from scientific_reproduction.research.dedupe import (
    DEDUPE_RULESET_VERSION,
    SOURCE_DEDUPE_RULES,
    CanonicalSource,
    DedupeOutcome,
    SourceDedupeAssessment,
    SourceDedupeDecision,
    SourceDedupeRule,
    deduplicate_sources,
    evaluate_source_pair,
)
from scientific_reproduction.research.sources import (
    CanonicalIdentityKind,
    SourceNormalizationError,
    canonical_identity,
)

PAPER_DOI = "10.1039/D5TA00771B"  # 17-FDM201-REFERENCE-CASE.md
PAPER_DOI_NORMALIZED = "10.1039/d5ta00771b"


def _source(
    source_id: str,
    source_type: SourceType = SourceType.PEER_REVIEWED_PAPER,
    title: str = "A scholarly source",
    provenance: str = "acquisition:manual",
    **kwargs: Any,
) -> ResearchSource:
    """Build a frozen ResearchSource with compact defaults."""
    return ResearchSource(
        source_id=source_id,
        source_type=source_type,
        title=title,
        provenance=provenance,
        **kwargs,
    )


def _paper(
    source_id: str,
    doi: str = PAPER_DOI,
    provenance: str = "acquisition:manual",
    url: str | None = None,
    **kwargs: Any,
) -> ResearchSource:
    """Build a mirror-collapsible paper record (peer-reviewed paper)."""
    return _source(
        source_id,
        provenance=provenance,
        doi=doi,
        url_or_locator=url,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Rule table shape (deliverable: deduplication rules)
# ---------------------------------------------------------------------------


def test_source_dedupe_ruleset_is_versioned_and_total() -> None:
    assert DEDUPE_RULESET_VERSION == "1.0"
    rule_ids = [rule.rule_id for rule in SOURCE_DEDUPE_RULES]
    assert len(rule_ids) == len(set(rule_ids)), "rule ids must be unique"
    assert len(SOURCE_DEDUPE_RULES) == 6
    for rule in SOURCE_DEDUPE_RULES:
        assert isinstance(rule, SourceDedupeRule)
        assert isinstance(rule.outcome, DedupeOutcome)
        assert rule.description
    # The trailing default rule matches every pair, so the table is total:
    # every pair of records gets exactly one verdict. Prove it on a real
    # pair of records (the predicate is input-agnostic).
    pair = evaluate_source_pair(
        _paper("src-a", doi=PAPER_DOI), _paper("src-b", doi="10.1000/182")
    ).input
    assert SOURCE_DEDUPE_RULES[-1].rule_id == "R-DEDUPE-D1"
    assert SOURCE_DEDUPE_RULES[-1].predicate(pair) is True
    assert pair.left.source_id == "src-a"


def test_source_dedupe_rules_are_frozen_dataclasses() -> None:
    rule = SOURCE_DEDUPE_RULES[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.rule_id = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.predicate = lambda i: False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC-01: duplicate DOI mirrors collapse to one canonical scholarly source
# ---------------------------------------------------------------------------


def test_ac01_duplicate_doi_mirrors_collapse_to_one_canonical_source() -> None:
    mirror_a = _paper(
        "src-mirror-a",
        doi=PAPER_DOI,
        provenance="acquisition:doi.org",
        url="https://doi.org/10.1039/D5TA00771B",
    )
    mirror_b = _paper(
        "src-mirror-b",
        doi=PAPER_DOI,
        provenance="acquisition:publisher",
        url="https://pubs.rsc.org/en/content/articlelanding/2025/ta/d5ta00771b",
    )
    assessments = deduplicate_sources([mirror_a, mirror_b])
    assert len(assessments) == 1
    assessment = assessments[0]
    assert assessment.collapsed is True
    assert assessment.matched_rule_id == "R-DEDUPE-C1"
    # One canonical scholarly source: the earliest address wins.
    assert assessment.canonical.canonical.source_id == "src-mirror-a"
    assert assessment.canonical.canonical.source_type is SourceType.PEER_REVIEWED_PAPER
    assert len(assessment.canonical.members) == 2
    assert assessment.canonical.members == (mirror_a, mirror_b)


def test_ac01_doi_form_mirrors_collapse_to_one_canonical_source() -> None:
    # Mirrors that recorded the DOI differently (wrapper URL vs bare vs
    # DOI: prefix) still share the canonical identity and collapse.
    mirror_a = _paper("src-a", doi="https://doi.org/10.1039/D5TA00771B")
    mirror_b = _paper("src-b", doi="DOI: 10.1039/d5ta00771b")
    mirror_c = _paper("src-c", doi=PAPER_DOI_NORMALIZED)
    assessments = deduplicate_sources([mirror_a, mirror_b, mirror_c])
    assert len(assessments) == 1
    assert len(assessments[0].canonical.members) == 3
    assert assessments[0].identity.key == f"doi:{PAPER_DOI_NORMALIZED}"
    assert assessments[0].identity.kind is CanonicalIdentityKind.DOI


def test_ac01_collapsible_source_types_share_doi_collapse() -> None:
    # A shared DOI identifies the same registered work across collapsible
    # source types (distinct versions carry distinct DOIs).
    target = _source(
        "src-target",
        source_type=SourceType.TARGET_PAPER,
        doi=PAPER_DOI,
        provenance="acquisition:primary",
    )
    mirror = _paper(
        "src-mirror",
        doi=PAPER_DOI,
        provenance="acquisition:repository",
        url="https://repository.example.com/mirror.pdf",
    )
    assessments = deduplicate_sources([target, mirror])
    assert len(assessments) == 1
    assert assessments[0].canonical.canonical.source_id == "src-target"
    assert len(assessments[0].canonical.members) == 2


def test_ac01_distinct_dois_do_not_collapse() -> None:
    a = _paper("src-a", doi=PAPER_DOI)
    b = _paper("src-b", doi="10.1000/182")
    assessments = deduplicate_sources([a, b])
    assert len(assessments) == 2
    assert assessments[0].canonical.canonical.source_id == "src-a"
    assert assessments[1].canonical.canonical.source_id == "src-b"
    assert assessments[0].collapsed is False
    assert assessments[1].collapsed is False


def test_ac01_collapse_pair_verdict_via_rule_c1() -> None:
    a = _paper("src-a", doi=PAPER_DOI)
    b = _paper("src-b", doi="DOI:10.1039/D5TA00771B")
    assessment = evaluate_source_pair(a, b)
    assert assessment.outcome is DedupeOutcome.SAME_SOURCE
    assert assessment.matched_rule_id == "R-DEDUPE-C1"
    rule_ids = [decision.rule_id for decision in assessment.decisions]
    assert rule_ids == [rule.rule_id for rule in SOURCE_DEDUPE_RULES]
    for decision in assessment.decisions:
        assert isinstance(decision, SourceDedupeDecision)
    c1_decision = assessment.decisions[2]
    assert c1_decision.matched is True
    assert c1_decision.outcome is DedupeOutcome.SAME_SOURCE


# ---------------------------------------------------------------------------
# AC-02: distinct SI/dataset/structure records remain separately addressable
# ---------------------------------------------------------------------------


def test_ac02_dataset_sharing_paper_doi_remains_separately_addressable() -> None:
    paper = _paper("src-paper", doi=PAPER_DOI)
    dataset = _source(
        "src-dataset",
        source_type=SourceType.DATASET,
        doi=PAPER_DOI,
        title="Supporting dataset for D5TA00771B",
        provenance="acquisition:data-repository",
        url_or_locator="https://repository.example.com/dataset.zip",
    )
    assessments = deduplicate_sources([paper, dataset])
    assert len(assessments) == 2, (
        "the dataset must stay separately addressable from the paper "
        "even though it shares the paper's DOI"
    )
    paper_assessment = next(
        a for a in assessments if a.canonical.canonical.source_id == "src-paper"
    )
    dataset_assessment = next(
        a for a in assessments if a.canonical.canonical.source_id == "src-dataset"
    )
    assert paper_assessment.identity.kind is CanonicalIdentityKind.DOI
    assert dataset_assessment.identity.kind is CanonicalIdentityKind.RECORD
    assert dataset_assessment.identity.key == "record:src-dataset"
    assert dataset_assessment.collapsed is False
    # The dataset keeps its own address and its own recorded DOI.
    assert dataset_assessment.canonical.canonical.doi == PAPER_DOI


def test_ac02_si_sharing_paper_doi_remains_separately_addressable() -> None:
    paper = _paper("src-paper", doi=PAPER_DOI)
    si = _source(
        "src-si",
        source_type=SourceType.SUPPLEMENTARY_INFORMATION,
        doi=PAPER_DOI,
        title="Supplementary information for D5TA00771B",
        provenance="acquisition:publisher",
    )
    assessments = deduplicate_sources([paper, si])
    assert len(assessments) == 2
    si_assessment = next(
        a for a in assessments if a.canonical.canonical.source_id == "src-si"
    )
    assert si_assessment.identity.key == "record:src-si"
    assert si_assessment.identity.kind is CanonicalIdentityKind.RECORD
    assert si_assessment.collapsed is False


def test_ac02_structure_deposition_sharing_paper_doi_remains_separately_addressable() -> None:
    paper = _paper("src-paper", doi=PAPER_DOI)
    structure = _source(
        "src-structure",
        source_type=SourceType.STRUCTURE_DEPOSITION,
        doi=PAPER_DOI,
        stable_identifier="CCDC-123456",
        title="CIF structure deposition",
        provenance="acquisition:ccdc",
    )
    assessments = deduplicate_sources([paper, structure])
    assert len(assessments) == 2
    structure_assessment = next(
        a for a in assessments if a.canonical.canonical.source_id == "src-structure"
    )
    assert structure_assessment.identity.key == "record:src-structure"
    assert structure_assessment.identity.kind is CanonicalIdentityKind.RECORD
    assert structure_assessment.collapsed is False


def test_ac02_two_datasets_with_same_doi_do_not_collapse() -> None:
    dataset_a = _source(
        "src-ds-a",
        source_type=SourceType.DATASET,
        doi=PAPER_DOI,
        title="Dataset A",
        provenance="acquisition:repo-a",
    )
    dataset_b = _source(
        "src-ds-b",
        source_type=SourceType.DATASET,
        doi=PAPER_DOI,
        title="Dataset B",
        provenance="acquisition:repo-b",
    )
    assessments = deduplicate_sources([dataset_a, dataset_b])
    assert len(assessments) == 2
    for assessment in assessments:
        assert assessment.collapsed is False
        assert assessment.identity.kind is CanonicalIdentityKind.RECORD


def test_ac02_scoping_rule_fires_for_mixed_pairs() -> None:
    paper = _paper("src-paper", doi=PAPER_DOI)
    dataset = _source(
        "src-dataset",
        source_type=SourceType.DATASET,
        doi=PAPER_DOI,
        title="Dataset",
        provenance="acquisition:manual",
    )
    assessment = evaluate_source_pair(paper, dataset)
    assert assessment.outcome is DedupeOutcome.DISTINCT_SOURCES
    assert assessment.matched_rule_id == "R-DEDUPE-S1"
    si = _source(
        "src-si", source_type=SourceType.SUPPLEMENTARY_INFORMATION, doi=PAPER_DOI
    )
    assert evaluate_source_pair(dataset, si).outcome is DedupeOutcome.DISTINCT_SOURCES
    assert evaluate_source_pair(dataset, si).matched_rule_id == "R-DEDUPE-S1"


def test_ac02_dataset_pair_with_equal_urls_do_not_collapse() -> None:
    dataset_a = _source(
        "src-ds-a",
        source_type=SourceType.DATASET,
        url_or_locator="https://example.com/data.zip",
        provenance="acquisition:a",
    )
    dataset_b = _source(
        "src-ds-b",
        source_type=SourceType.DATASET,
        url_or_locator="https://example.com/data.zip",
        provenance="acquisition:b",
    )
    assert (
        evaluate_source_pair(dataset_a, dataset_b).outcome
        is DedupeOutcome.DISTINCT_SOURCES
    )
    assert len(deduplicate_sources([dataset_a, dataset_b])) == 2


# ---------------------------------------------------------------------------
# AC-03: source provenance for mirrors is retained
# ---------------------------------------------------------------------------


def test_ac03_mirror_provenance_retained_on_canonical_source() -> None:
    mirror_a = _paper("src-a", provenance="acquisition:doi.org")
    mirror_b = _paper("src-b", provenance="acquisition:publisher")
    assessments = deduplicate_sources([mirror_a, mirror_b])
    assert len(assessments) == 1
    canonical: CanonicalSource = assessments[0].canonical
    assert canonical.mirror_provenances == (
        "acquisition:doi.org",
        "acquisition:publisher",
    )
    # The canonical record's own provenance field is the first member's.
    assert canonical.canonical.provenance == "acquisition:doi.org"


def test_ac03_mirror_urls_retained_on_canonical_source() -> None:
    mirror_a = _paper(
        "src-a",
        url="https://doi.org/10.1039/D5TA00771B",
        provenance="acquisition:a",
    )
    mirror_b = _paper(
        "src-b",
        url="https://pubs.rsc.org/en/content/articlelanding/2025/ta/d5ta00771b",
        provenance="acquisition:b",
    )
    assessments = deduplicate_sources([mirror_a, mirror_b])
    canonical: CanonicalSource = assessments[0].canonical
    assert canonical.mirror_urls == (
        "https://doi.org/10.1039/D5TA00771B",
        "https://pubs.rsc.org/en/content/articlelanding/2025/ta/d5ta00771b",
    )
    assert canonical.canonical.url_or_locator == "https://doi.org/10.1039/D5TA00771B"


def test_ac03_provenance_retained_without_duplicates() -> None:
    mirrors = [
        _paper("src-a", provenance="acquisition:doi.org"),
        _paper("src-b", provenance="acquisition:publisher"),
        _paper("src-c", provenance="acquisition:doi.org"),
    ]
    assessments = deduplicate_sources(mirrors)
    canonical: CanonicalSource = assessments[0].canonical
    # Every distinct provenance is retained exactly once, input order.
    assert canonical.mirror_provenances == (
        "acquisition:doi.org",
        "acquisition:publisher",
    )


def test_ac03_singleton_retains_own_provenance() -> None:
    paper = _paper("src-a", provenance="acquisition:manual")
    assessments = deduplicate_sources([paper])
    assert len(assessments) == 1
    canonical: CanonicalSource = assessments[0].canonical
    assert canonical.mirror_provenances == ("acquisition:manual",)
    assert canonical.mirror_urls == ()


# ---------------------------------------------------------------------------
# Merge semantics (R-MERGE-1 .. R-MERGE-3)
# ---------------------------------------------------------------------------


def test_source_dedupe_merge_first_seen_wins_for_required_fields() -> None:
    first = _paper(
        "src-a",
        title="First recorded title",
        provenance="acquisition:a",
    )
    second = _paper(
        "src-b",
        title="Second recorded title",
        provenance="acquisition:b",
    )
    assessments = deduplicate_sources([first, second])
    canonical = assessments[0].canonical.canonical
    assert canonical.source_id == "src-a"
    assert canonical.title == "First recorded title"
    assert canonical.provenance == "acquisition:a"


def test_source_dedupe_merge_first_non_none_for_optional_fields() -> None:
    first = _paper("src-a", provenance="acquisition:a")
    second = _paper(
        "src-b",
        provenance="acquisition:b",
        url="https://mirror.example.com/b.pdf",
        publication_year=2025,
        acquired_at="2026-08-13T00:00:00Z",
        local_artifact_id="art-b",
        access_class="PUBLIC",
    )
    assessments = deduplicate_sources([first, second])
    canonical = assessments[0].canonical.canonical
    assert canonical.doi == PAPER_DOI
    assert canonical.url_or_locator == "https://mirror.example.com/b.pdf"
    assert canonical.publication_year == 2025
    assert canonical.acquired_at == "2026-08-13T00:00:00Z"
    assert canonical.local_artifact_id == "art-b"
    assert canonical.access_class == "PUBLIC"


def test_source_dedupe_merge_keeps_doi_casing_as_recorded() -> None:
    first = _paper("src-a", doi=PAPER_DOI, provenance="acquisition:a")
    second = _paper("src-b", doi=PAPER_DOI_NORMALIZED, provenance="acquisition:b")
    assessments = deduplicate_sources([first, second])
    canonical = assessments[0].canonical.canonical
    # The canonical record keeps the first mirror's recorded DOI casing;
    # only the identity key is normalized.
    assert canonical.doi == PAPER_DOI
    assert assessments[0].identity.key == f"doi:{PAPER_DOI_NORMALIZED}"


# ---------------------------------------------------------------------------
# Rule semantics and invariants
# ---------------------------------------------------------------------------


def test_source_dedupe_same_record_is_trivially_same() -> None:
    paper = _paper("src-a")
    assessment = evaluate_source_pair(paper, paper)
    assert assessment.outcome is DedupeOutcome.SAME_SOURCE
    assert assessment.matched_rule_id == "R-DEDUPE-S0"


def test_source_dedupe_stable_identifier_collapse() -> None:
    a = _source("src-a", stable_identifier="arXiv:2401.00001")
    b = _source(
        "src-b",
        stable_identifier="arXiv:2401.00001",
        url_or_locator="https://arxiv.org/abs/2401.00001",
    )
    assessment = evaluate_source_pair(a, b)
    assert assessment.outcome is DedupeOutcome.SAME_SOURCE
    assert assessment.matched_rule_id == "R-DEDUPE-C2"
    assert len(deduplicate_sources([a, b])) == 1


def test_source_dedupe_url_collapse_without_doi() -> None:
    a = _source("src-a", url_or_locator="https://example.com/papers/a.pdf")
    b = _source("src-b", url_or_locator="https://www.example.com/papers/a.pdf/")
    assessment = evaluate_source_pair(a, b)
    assert assessment.outcome is DedupeOutcome.SAME_SOURCE
    assert assessment.matched_rule_id == "R-DEDUPE-C3"
    assert len(deduplicate_sources([a, b])) == 1


def test_source_dedupe_doi_outranks_url() -> None:
    # Same normalized URL but different DOIs: the DOI identifies the work,
    # so the records are distinct sources that happen to share a locator.
    a = _paper("src-a", doi=PAPER_DOI, url="https://example.com/papers/a.pdf")
    b = _paper("src-b", doi="10.1000/182", url="https://example.com/papers/a.pdf")
    assessment = evaluate_source_pair(a, b)
    assert assessment.outcome is DedupeOutcome.DISTINCT_SOURCES
    assert assessment.matched_rule_id == "R-DEDUPE-D1"
    assert len(deduplicate_sources([a, b])) == 2


def test_source_dedupe_doi_vs_url_identity_never_matches() -> None:
    a = _paper("src-a", doi=PAPER_DOI)
    b = _source("src-b", url_or_locator="https://example.com/papers/a.pdf")
    assessment = evaluate_source_pair(a, b)
    assert assessment.outcome is DedupeOutcome.DISTINCT_SOURCES


#: Exhaustive variant grid: every identity dimension, both collapsible and
#: record-scoped records, and degenerate records without identity fields.
DEDUPE_VARIANTS: dict[str, ResearchSource] = {
    "mirror-a-doi": _paper("v-doi-a", doi=PAPER_DOI, url="https://doi.org/10.1039/D5TA00771B"),
    "mirror-b-doi": _paper("v-doi-b", doi=PAPER_DOI_NORMALIZED, url="https://pubs.rsc.org/en/content/articlelanding/2025/ta/d5ta00771b"),
    "other-doi": _paper("v-doi-other", doi="10.1000/182"),
    "stable-a": _source("v-stable-a", stable_identifier="arXiv:2401.00001"),
    "stable-b": _source(
        "v-stable-b",
        stable_identifier="arXiv:2401.00001",
        url_or_locator="https://arxiv.org/abs/2401.00001",
    ),
    "url-a": _source("v-url-a", url_or_locator="https://example.com/papers/a.pdf"),
    "url-b": _source(
        "v-url-b", url_or_locator="https://www.example.com/papers/a.pdf/"
    ),
    "bare": _source("v-bare"),
    "dataset-doi": _source("v-dataset", source_type=SourceType.DATASET, doi=PAPER_DOI),
    "si-doi": _source("v-si", source_type=SourceType.SUPPLEMENTARY_INFORMATION, doi=PAPER_DOI),
    "structure-doi": _source("v-structure", source_type=SourceType.STRUCTURE_DEPOSITION, doi=PAPER_DOI),
}


def test_source_dedupe_pair_collapse_iff_equal_canonical_identity() -> None:
    # Bi-implication invariant over the exhaustive variant grid: two
    # records collapse (SAME_SOURCE) if and only if their canonical
    # identity keys are equal. Clustering by key is therefore sound.
    variants = list(DEDUPE_VARIANTS.values())
    for left, right in itertools.product(variants, repeat=2):
        pair = evaluate_source_pair(left, right)
        keys_equal = canonical_identity(left).key == canonical_identity(right).key
        assert (
            pair.outcome is DedupeOutcome.SAME_SOURCE
        ) == keys_equal, (left.source_id, right.source_id)


def test_source_dedupe_pair_verdict_is_symmetric() -> None:
    variants = list(DEDUPE_VARIANTS.values())
    for left, right in itertools.product(variants, repeat=2):
        forward = evaluate_source_pair(left, right)
        backward = evaluate_source_pair(right, left)
        assert forward.outcome is backward.outcome
        assert forward.matched_rule_id == backward.matched_rule_id


def test_source_dedupe_is_deterministic_for_equal_inputs() -> None:
    a1 = _paper("src-a", doi=PAPER_DOI)
    a2 = _paper("src-a", doi=PAPER_DOI)
    b1 = _paper("src-b", doi=PAPER_DOI)
    b2 = _paper("src-b", doi=PAPER_DOI)
    assert evaluate_source_pair(a1, b1) == evaluate_source_pair(a2, b2)
    assert deduplicate_sources([a1, b1]) == deduplicate_sources([a2, b2])


def test_source_dedupe_default_rule_covers_unrelated_records() -> None:
    a = _source("src-a")
    b = _source("src-b")
    assessment = evaluate_source_pair(a, b)
    assert assessment.outcome is DedupeOutcome.DISTINCT_SOURCES
    assert assessment.matched_rule_id == "R-DEDUPE-D1"
    # The default rule's decision is recorded as matched.
    assert assessment.decisions[-1].matched is True
    assert assessment.decisions[-1].rule_id == "R-DEDUPE-D1"


def test_source_dedupe_assessment_records_full_trace() -> None:
    a = _paper("src-a", doi=PAPER_DOI)
    b = _paper("src-b", doi="10.1000/182")
    assessment = evaluate_source_pair(a, b)
    assert len(assessment.decisions) == len(SOURCE_DEDUPE_RULES)
    # Exactly one rule matched (D1, the default), all others did not.
    matched = [d for d in assessment.decisions if d.matched]
    assert len(matched) == 1
    assert matched[0].rule_id == "R-DEDUPE-D1"
    # The assessment preserves the exact input pair.
    assert assessment.input.left == a
    assert assessment.input.right == b


# ---------------------------------------------------------------------------
# deduplicate_sources behavior
# ---------------------------------------------------------------------------


def test_source_deduplicate_sources_empty_sequence() -> None:
    assert deduplicate_sources([]) == ()


def test_source_deduplicate_sources_single_record() -> None:
    paper = _paper("src-a")
    assessments = deduplicate_sources([paper])
    assert len(assessments) == 1
    assessment = assessments[0]
    assert assessment.collapsed is False
    assert assessment.matched_rule_id == "R-DEDUPE-S0"
    assert assessment.canonical.members == (paper,)
    assert assessment.canonical.canonical == paper


def test_source_deduplicate_sources_preserves_input_order() -> None:
    papers = [
        _paper("src-a", doi="10.1000/182"),
        _paper("src-b", doi=PAPER_DOI),
        _paper("src-c", doi="10.1000/182"),  # collapses into src-a's group
    ]
    assessments = deduplicate_sources(papers)
    assert [a.canonical.canonical.source_id for a in assessments] == [
        "src-a",
        "src-b",
    ]
    assert assessments[0].canonical.members[0].source_id == "src-a"
    assert assessments[0].canonical.members[1].source_id == "src-c"


def test_source_deduplicate_sources_collapses_duplicate_source_ids() -> None:
    duplicate = _paper("src-a", doi=PAPER_DOI)
    assessments = deduplicate_sources([duplicate, duplicate])
    assert len(assessments) == 1
    assert len(assessments[0].canonical.members) == 1
    assert assessments[0].collapsed is False
    assert assessments[0].canonical.mirror_provenances == ("acquisition:manual",)


def test_source_deduplicate_sources_raises_on_malformed_doi() -> None:
    with pytest.raises(SourceNormalizationError, match="malformed DOI"):
        deduplicate_sources([_paper("src-a", doi="10.1/abc")])


def test_source_deduplicate_sources_type_errors_at_boundary() -> None:
    with pytest.raises(TypeError, match="sequence of ResearchSource"):
        deduplicate_sources("src-a")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ResearchSource elements"):
        deduplicate_sources([_paper("src-a"), "not a source"])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="ResearchSource elements"):
        deduplicate_sources([b"bytes"])  # type: ignore[list-item]


def test_source_evaluate_pair_type_errors_at_boundary() -> None:
    with pytest.raises(TypeError, match="ResearchSource"):
        evaluate_source_pair("src-a", _paper("src-b"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ResearchSource"):
        evaluate_source_pair(_paper("src-a"), None)  # type: ignore[arg-type]


def test_source_dedupe_assessments_are_frozen_records() -> None:
    paper = _paper("src-a")
    assessment = deduplicate_sources([paper])[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        assessment.collapsed = True  # type: ignore[misc]
    assert isinstance(assessment, SourceDedupeAssessment)
    assert isinstance(assessment.canonical, CanonicalSource)
