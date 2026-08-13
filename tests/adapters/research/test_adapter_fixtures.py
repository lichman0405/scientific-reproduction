"""Tests for the deterministic offline fixtures and their resolution
(DEV-M5-G06, AC-03).

Acceptance coverage:
  * AC-03 -- the public adapter contract is testable without a live
    network: fixtures are pure frozen data (no wall-clock, no
    randomness, no network), search/fetch are pure functions of the
    query/ref, and every adapter resolves its complete fixture set.
  * 09-RESEARCH-SUBSYSTEM.md section 4 fixture families: metadata
    service, public repository, crystallographic database.

All tests are offline.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from scientific_reproduction.adapters.research import (
    PUBLIC_ADAPTERS,
    PUBLIC_SOURCE_FIXTURES,
    AdapterRawRecord,
    AdapterRecordNotFoundError,
    AdapterSearchQuery,
    AdapterSourceRef,
    CrossrefOpenAlexAdapter,
    CrystallographicDatabaseAdapter,
    PublicRepositoryAdapter,
)

EXPECTED_FIXTURE_COUNTS = {
    "crossref_openalex": 3,
    "public_repository": 2,
    "crystallographic_database": 2,
}


# ---------------------------------------------------------------------------
# Pure data (AC-03)
# ---------------------------------------------------------------------------


def test_fixtures_are_pure_frozen_data() -> None:
    """The fixture map covers exactly the shipped public adapters; every
    record is a frozen AdapterRawRecord with a non-empty title and a
    documented identity dimension."""
    assert set(PUBLIC_SOURCE_FIXTURES) == {
        adapter.adapter_id for adapter in PUBLIC_ADAPTERS
    }
    for adapter_id, records in PUBLIC_SOURCE_FIXTURES.items():
        assert len(records) == EXPECTED_FIXTURE_COUNTS[adapter_id]
        for record in records:
            assert isinstance(record, AdapterRawRecord)
            assert record.title.strip()
            assert (
                record.doi
                or record.stable_identifier
                or record.url_or_locator
            )
            with pytest.raises(FrozenInstanceError):
                record.title = "mutated"  # type: ignore[misc]


def test_fixture_sets_cover_documented_public_source_families() -> None:
    """Fixture families map to 09-RESEARCH-SUBSYSTEM.md section 4 public
    source examples: metadata service (with the FDM201 primary paper),
    public repository (preprint + dataset), crystallographic database
    (structure deposit + database record)."""
    crossref = PUBLIC_SOURCE_FIXTURES["crossref_openalex"]
    assert crossref[0].doi == "10.1039/D5TA00771B"  # 17-FDM201-REFERENCE-CASE.md
    assert {r.source_type.value for r in crossref} == {
        "target_paper",
        "peer_reviewed_paper",
        "supplementary_information",
    }
    repository = PUBLIC_SOURCE_FIXTURES["public_repository"]
    assert {r.source_type.value for r in repository} == {
        "preprint",
        "dataset",
    }
    crystallographic = PUBLIC_SOURCE_FIXTURES["crystallographic_database"]
    assert {r.source_type.value for r in crystallographic} == {
        "structure_deposition",
        "database_record",
    }


def test_fixture_data_is_identical_every_import() -> None:
    """Re-reading the fixture constants yields equal data (pure module
    constants, no hidden state)."""
    import importlib

    first = importlib.import_module(
        "scientific_reproduction.adapters.research.fixtures"
    )
    second = importlib.import_module(
        "scientific_reproduction.adapters.research.fixtures"
    )
    assert first is second
    assert first.PUBLIC_SOURCE_FIXTURES == second.PUBLIC_SOURCE_FIXTURES


# ---------------------------------------------------------------------------
# Search resolution
# ---------------------------------------------------------------------------


def test_search_empty_query_returns_entire_fixture_set() -> None:
    """An empty query is the whole-fixture query: every record of the
    adapter's set is returned."""
    for adapter in PUBLIC_ADAPTERS:
        result = adapter.search(AdapterSearchQuery(query_text=""))
        assert result.adapter_id == adapter.adapter_id
        assert len(result.records) == EXPECTED_FIXTURE_COUNTS[adapter.adapter_id]
        assert result.records == PUBLIC_SOURCE_FIXTURES[adapter.adapter_id]


def test_search_filters_by_case_insensitive_substring() -> None:
    """Search is a pure case-insensitive substring filter over the
    searchable text of the fixture records."""
    adapter = CrossrefOpenAlexAdapter()
    result = adapter.search(AdapterSearchQuery(query_text="FDM-201"))
    assert len(result.records) == 1  # only the SI fixture mentions FDM-201
    assert result.records[0].title.startswith("Supplementary information")
    result = adapter.search(AdapterSearchQuery(query_text="METAL"))
    assert len(result.records) == 2  # primary + peer-reviewed papers
    none = adapter.search(AdapterSearchQuery(query_text="zzz-no-such-record"))
    assert none.records == ()


def test_search_max_results_truncates_deterministically() -> None:
    """max_results truncates the deterministic fixture order."""
    adapter = CrossrefOpenAlexAdapter()
    result = adapter.search(
        AdapterSearchQuery(query_text="", max_results=1)
    )
    assert len(result.records) == 1
    assert result.records[0].doi == "10.1039/D5TA00771B"
    zero = adapter.search(AdapterSearchQuery(query_text="", max_results=0))
    assert zero.records == ()


# ---------------------------------------------------------------------------
# fetch_metadata resolution
# ---------------------------------------------------------------------------


def test_fetch_metadata_by_doi_recording_form_variants() -> None:
    """A DOI reference resolves across recording forms: 'doi:' prefix,
    casing and wrapper-URL variants match the same fixture record."""
    adapter = CrossrefOpenAlexAdapter()
    for doi in (
        "10.1039/D5TA00771B",
        "doi: 10.1039/D5TA00771B",
        "10.1039/d5ta00771b",
        "https://doi.org/10.1039/D5TA00771B",
    ):
        record = adapter.fetch_metadata(AdapterSourceRef(doi=doi))
        assert record.doi == "10.1039/D5TA00771B"


def test_fetch_metadata_by_stable_identifier_and_url() -> None:
    """Identifier and URL references resolve the matching records."""
    adapter = PublicRepositoryAdapter()
    by_identifier = adapter.fetch_metadata(
        AdapterSourceRef(stable_identifier="arXiv:2406.12345")
    )
    assert by_identifier.stable_identifier == "arXiv:2406.12345"
    by_url = adapter.fetch_metadata(
        AdapterSourceRef(
            url_or_locator="https://www.arxiv.org:443/abs/2406.12345/"
        )
    )
    assert by_url == by_identifier


def test_fetch_metadata_unknown_identity_raises_stable_error() -> None:
    """An unknown identity raises AdapterRecordNotFoundError naming the
    adapter and the reference (stable message)."""
    adapter = CrystallographicDatabaseAdapter()
    with pytest.raises(
        AdapterRecordNotFoundError,
        match="crystallographic_database: fetch_metadata: no fixture record "
        "matches doi='10.9999/does-not-exist'",
    ):
        adapter.fetch_metadata(AdapterSourceRef(doi="10.9999/does-not-exist"))
