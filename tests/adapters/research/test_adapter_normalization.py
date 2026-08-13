"""Tests for adapter -> ResearchSource normalization (DEV-M5-G06, AC-02).

Acceptance coverage:
  * AC-02 -- adapter outputs normalize to Source records: every
    identity-bearing field of a raw adapter record flows through the
    frozen normalization path of ``research.sources`` (DEV-M5-G01:
    ``normalize_doi`` / ``normalize_stable_identifier`` / ``normalize_url``),
    producing a frozen ``core.models.ResearchSource`` with the correct
    ``SourceType`` and deterministic adapter provenance, whose
    ``canonical_identity`` matches the expected normalized identity
    (06-EVIDENCE-SYSTEM.md section 7).
  * Mirrors acquired through different adapters collapse through the
    research dedupe path (``research.dedupe.deduplicate_sources``):
    duplicate DOI mirrors yield one canonical scholarly source.

Every test is offline: it only touches the fixture data and the pure
normalization functions.
"""

from __future__ import annotations

import re

import pytest

from scientific_reproduction.adapters.research import (
    PUBLIC_ADAPTERS,
    PUBLIC_SOURCE_FIXTURES,
    AdapterRawRecord,
    AdapterSearchQuery,
    CrossrefOpenAlexAdapter,
    CrystallographicDatabaseAdapter,
    FixtureResearchAdapter,
    PublicRepositoryAdapter,
)
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import AccessClass, ResearchSource, SourceType
from scientific_reproduction.research.dedupe import deduplicate_sources
from scientific_reproduction.research.sources import (
    CanonicalIdentityKind,
    SourceIdentity,
    SourceNormalizationError,
    canonical_identity,
)

PRIMARY_TITLE = (
    "A highly connected metal-organic framework with stretched inorganic "
    "units for propylene/ethylene separation"
)
PRIMARY_DOI = "10.1039/D5TA00771B"
PRIMARY_DOI_CANONICAL = "10.1039/d5ta00771b"

SOURCE_ID_PATTERN = re.compile(r"^sr_src_[0-9a-f]{32}$")


def _first_fixture(adapter_id: str) -> AdapterRawRecord:
    return PUBLIC_SOURCE_FIXTURES[adapter_id][0]


# ---------------------------------------------------------------------------
# Identity normalization through research.sources (AC-02)
# ---------------------------------------------------------------------------


def test_primary_paper_fixture_normalizes_to_expected_identity() -> None:
    """The reference-case primary paper (17-FDM201-REFERENCE-CASE.md)
    normalizes to a frozen ResearchSource whose canonical identity is
    the expected lowercase DOI key."""
    adapter = CrossrefOpenAlexAdapter()
    source = adapter.normalize(_first_fixture("crossref_openalex"))
    assert isinstance(source, ResearchSource)
    assert source.source_type is SourceType.TARGET_PAPER
    assert source.title == PRIMARY_TITLE
    assert source.doi == PRIMARY_DOI_CANONICAL
    # URLs normalize scheme/host/port/trailing-slash/fragment; paths are
    # preserved verbatim (research/sources.py: paths are case-sensitive)
    assert source.url_or_locator == "https://doi.org/" + PRIMARY_DOI
    assert source.provenance == "adapter:crossref_openalex@v0.1.0"
    assert source.acquired_at is None  # acquisition time is not the adapter's
    identity = canonical_identity(source)
    assert identity == SourceIdentity(
        key=f"doi:{PRIMARY_DOI_CANONICAL}",
        kind=CanonicalIdentityKind.DOI,
        normalized_doi=PRIMARY_DOI_CANONICAL,
    )


def test_prefixed_doi_fixture_normalizes_to_canonical_record() -> None:
    """A 'doi:'-prefixed raw DOI normalizes away: the record carries the
    canonical form and the identity key is the normalized DOI."""
    adapter = CrossrefOpenAlexAdapter()
    raw = PUBLIC_SOURCE_FIXTURES["crossref_openalex"][1]  # 'doi:'-prefixed DOI
    source = adapter.normalize(raw)
    assert source.doi == "10.1016/j.ces.2024.120001"
    identity = canonical_identity(source)
    assert identity.key == "doi:10.1016/j.ces.2024.120001"
    assert identity.kind is CanonicalIdentityKind.DOI


def test_url_only_record_normalizes_to_url_identity() -> None:
    """A URL-only collapsible record normalizes its locator (www host,
    default port, trailing slash stripped) and derives a url identity."""
    adapter = PublicRepositoryAdapter()
    raw = PUBLIC_SOURCE_FIXTURES["public_repository"][1]  # figshare dataset
    source = adapter.normalize(raw)
    assert source.source_type is SourceType.DATASET
    assert source.url_or_locator == (
        "https://figshare.com/articles/FDM-201-isotherms/25000001"
    )
    assert source.stable_identifier == "10.6084/m9.figshare.25000001"
    # datasets are record-scoped (DEV-M5-G01 AC-02): own address, never
    # the parent DOI
    identity = canonical_identity(source)
    assert identity.kind is CanonicalIdentityKind.RECORD
    assert identity.key == f"record:{source.source_id}"


def test_stable_identifier_record_derives_identifier_identity() -> None:
    """A record without DOI but with a stable identifier (arXiv id)
    derives a stable_identifier identity key."""
    adapter = PublicRepositoryAdapter()
    source = adapter.normalize(_first_fixture("public_repository"))
    assert source.doi is None
    assert source.stable_identifier == "arXiv:2406.12345"
    identity = canonical_identity(source)
    assert identity == SourceIdentity(
        key="stable_identifier:arXiv:2406.12345",
        kind=CanonicalIdentityKind.STABLE_IDENTIFIER,
        normalized_stable_identifier="arXiv:2406.12345",
    )


def test_record_scoped_fixtures_keep_own_addresses() -> None:
    """SI / dataset / structure-deposition fixtures are record-scoped:
    each keeps its own record identity, distinct from every other."""
    adapters = {
        "crossref_openalex": CrossrefOpenAlexAdapter(),
        "public_repository": PublicRepositoryAdapter(),
        "crystallographic_database": CrystallographicDatabaseAdapter(),
    }
    record_scoped = (
        SourceType.SUPPLEMENTARY_INFORMATION,
        SourceType.DATASET,
        SourceType.STRUCTURE_DEPOSITION,
    )
    keys: set[str] = set()
    for adapter_id, adapter in adapters.items():
        for raw in PUBLIC_SOURCE_FIXTURES[adapter_id]:
            if raw.source_type not in record_scoped:
                continue
            source = adapter.normalize(raw)
            identity = canonical_identity(source)
            assert identity.kind is CanonicalIdentityKind.RECORD
            assert identity.key == f"record:{source.source_id}"
            keys.add(identity.key)
    assert len(keys) == 3


def test_si_fixture_drops_fragment_from_url() -> None:
    """URL fragments address a location within a page, not the mirror:
    the SI fixture's '#si' fragment is normalized away."""
    adapter = CrossrefOpenAlexAdapter()
    raw = PUBLIC_SOURCE_FIXTURES["crossref_openalex"][2]
    assert raw.source_type is SourceType.SUPPLEMENTARY_INFORMATION
    source = adapter.normalize(raw)
    assert source.url_or_locator == (
        "https://pubs.rsc.org/en/content/articlelanding/2025/ta/d5ta00771b"
    )
    assert "#" not in (source.url_or_locator or "")


def test_malformed_doi_surfaces_source_normalization_error() -> None:
    """Malformed identity data is surfaced loudly through the frozen
    normalization path, never silently bent."""
    adapter = CrossrefOpenAlexAdapter()
    with pytest.raises(SourceNormalizationError, match="normalize_doi: malformed DOI"):
        adapter.normalize(
            AdapterRawRecord(
                title="x",
                source_type=SourceType.PEER_REVIEWED_PAPER,
                doi="11.9999/not-a-registered-doi",
            )
        )


# ---------------------------------------------------------------------------
# Deterministic record construction (AC-02 record shape)
# ---------------------------------------------------------------------------


def test_source_id_is_pure_function_of_adapter_and_identity() -> None:
    """Record ids are deterministic and adapter-scoped: the same raw
    record yields the same id; a different adapter yields a different
    id; the id matches generate_id('src', adapter_id, canonical material)."""
    adapter = CrossrefOpenAlexAdapter()
    raw = _first_fixture("crossref_openalex")
    first = adapter.normalize(raw)
    second = adapter.normalize(raw)
    assert first.source_id == second.source_id
    assert SOURCE_ID_PATTERN.match(first.source_id) is not None
    assert first.source_id == generate_id(
        "src", "crossref_openalex", PRIMARY_DOI_CANONICAL
    )
    other = PublicRepositoryAdapter().normalize(raw)
    assert other.source_id != first.source_id


def test_record_scoped_source_id_ignores_parent_doi() -> None:
    """Record-scoped source ids derive from the record's own locator, so
    a shared parent DOI never conflates distinct records."""
    adapter = CrossrefOpenAlexAdapter()
    raw = PUBLIC_SOURCE_FIXTURES["crossref_openalex"][2]  # SI record
    source = adapter.normalize(raw)
    assert source.source_id == generate_id(
        "src",
        "crossref_openalex",
        "https://pubs.rsc.org/en/content/articlelanding/2025/ta/d5ta00771b",
    )


def test_access_class_is_stamped_from_adapter() -> None:
    """Records acquired by a public adapter carry PUBLIC access class
    unless the raw record overrides it."""
    adapter = CrossrefOpenAlexAdapter()
    source = adapter.normalize(_first_fixture("crossref_openalex"))
    assert source.access_class is AccessClass.PUBLIC


def test_acquire_normalizes_every_search_hit() -> None:
    """acquire() returns an AdapterAcquisitionResult whose sources are
    exactly the normalized search hits (AC-02 at the workflow level)."""
    adapter = CrossrefOpenAlexAdapter()
    result = adapter.acquire(AdapterSearchQuery(query_text="metal"))
    assert result.adapter_id == "crossref_openalex"
    assert len(result.sources) == 2  # primary paper + peer-reviewed paper
    for source in result.sources:
        assert isinstance(source, ResearchSource)
        assert source.source_type is not SourceType.SUPPLEMENTARY_INFORMATION
        identity = canonical_identity(source)  # must not raise
        assert identity.key.startswith("doi:")


# ---------------------------------------------------------------------------
# Mirror collapse through the research dedupe path (06-EVIDENCE-SYSTEM.md §7)
# ---------------------------------------------------------------------------


class EchoPrimaryPaperAdapter(FixtureResearchAdapter):
    """A second adapter serving the same primary-paper fixture, to prove
    cross-adapter mirror collapse through canonical identity."""

    adapter_id = "test_echo_adapter"
    version = "0.1.0"
    access_class = AccessClass.PUBLIC
    operations = frozenset()
    fixture_records = (PUBLIC_SOURCE_FIXTURES["crossref_openalex"][0],)


def test_mirrors_across_adapters_collapse_via_dedupe() -> None:
    """The same paper acquired through two different adapters produces
    distinct source ids but one canonical identity, and the research
    dedupe path collapses them to one canonical source (06-EVIDENCE-SYSTEM.md
    section 7; duplicate DOI mirrors must not become independent
    evidence)."""
    crossref = CrossrefOpenAlexAdapter().normalize(
        _first_fixture("crossref_openalex")
    )
    echo = EchoPrimaryPaperAdapter().normalize(
        _first_fixture("crossref_openalex")
    )
    assert crossref.source_id != echo.source_id
    assert canonical_identity(crossref).key == canonical_identity(echo).key
    assessments = deduplicate_sources([crossref, echo])
    assert len(assessments) == 1
    assert assessments[0].collapsed is True
    assert len(assessments[0].canonical.members) == 2
    # both provenance stamps are retained, nothing a mirror contributed
    # is discarded (dedupe AC-03)
    assert set(assessments[0].canonical.mirror_provenances) == {
        "adapter:crossref_openalex@v0.1.0",
        "adapter:test_echo_adapter@v0.1.0",
    }


def test_distinct_dois_stay_distinct_through_dedupe() -> None:
    """Records with different canonical identities never collapse."""
    crossref = CrossrefOpenAlexAdapter()
    primary = crossref.normalize(PUBLIC_SOURCE_FIXTURES["crossref_openalex"][0])
    other = crossref.normalize(PUBLIC_SOURCE_FIXTURES["crossref_openalex"][1])
    assessments = deduplicate_sources([primary, other])
    assert len(assessments) == 2
    assert all(not a.collapsed for a in assessments)


def test_every_fixture_normalizes_to_a_canonical_identity() -> None:
    """Every shipped fixture record of every public adapter normalizes
    to a well-formed ResearchSource whose canonical identity is
    derivable (AC-02 grid over all first open-source fixtures)."""
    adapters = {adapter.adapter_id: adapter for adapter in PUBLIC_ADAPTERS}
    assert set(adapters) == set(PUBLIC_SOURCE_FIXTURES)
    for adapter_id, raw in (
        (adapter_id, record)
        for adapter_id, records in PUBLIC_SOURCE_FIXTURES.items()
        for record in records
    ):
        source = adapters[adapter_id].normalize(raw)
        assert isinstance(source, ResearchSource)
        identity = canonical_identity(source)
        assert identity.key
        if identity.kind is CanonicalIdentityKind.DOI:
            assert identity.normalized_doi == source.doi
