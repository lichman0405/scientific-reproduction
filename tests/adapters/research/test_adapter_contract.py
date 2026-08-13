"""Tests for the ResearchAdapter contract and capability vocabulary (DEV-M5-G06).

Acceptance coverage:
  * AC-03 -- the public adapter contract is testable without a live
    network: the interface is an abstract contract, every boundary
    rejects wrong argument types with ``TypeError``, and fixture
    resolution is pure -- the offline tests below block the network
    socket and still acquire every source, twice, with identical
    results.
  * commercial optional capability flags -- the stable capability
    vocabulary (``AdapterState``, ``AdapterOperation``,
    ``AdapterCapability`` with the ``commercial`` flag) is defined and
    the defined "absent" shape is locked.
  * interface shape -- the abstract ``ResearchAdapter`` contract
    (search / fetch_metadata abstract; normalize / acquire concrete
    over the frozen normalization path).

No test in this file touches the network: every network entry point is
patched to raise in the offline tests.
"""

from __future__ import annotations

import socket
from abc import ABC

import pytest

from scientific_reproduction.adapters.research import (
    ADAPTER_CONTRACT_VERSION,
    FIXTURE_VERSION,
    PUBLIC_ADAPTERS,
    AdapterCapability,
    AdapterDataError,
    AdapterOperation,
    AdapterRawRecord,
    AdapterSearchQuery,
    AdapterSourceRef,
    AdapterState,
    CrossrefOpenAlexAdapter,
    ResearchAdapter,
)
from scientific_reproduction.core.models import AccessClass, SourceType
from scientific_reproduction.research.sources import SourceNormalizationError

PRIMARY_FIXTURE = (
    "A highly connected metal-organic framework with stretched inorganic "
    "units for propylene/ethylene separation"
)


def _refuse_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any network attempt raise inside the test."""

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted in offline test path")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "create_connection", _refuse)


# ---------------------------------------------------------------------------
# Interface shape (AC-03: abstract contract)
# ---------------------------------------------------------------------------


def test_research_adapter_is_abstract_contract() -> None:
    """ResearchAdapter is an abstract base declaring search/fetch_metadata."""
    assert issubclass(ResearchAdapter, ABC)
    abstract_methods = ResearchAdapter.__abstractmethods__
    assert "search" in abstract_methods
    assert "fetch_metadata" in abstract_methods
    with pytest.raises(TypeError):
        ResearchAdapter()  # type: ignore[abstract]


def test_adapter_boundaries_reject_wrong_types() -> None:
    """Every public adapter boundary raises TypeError on wrong argument
    types (frozen rule-engine paradigm)."""
    adapter = CrossrefOpenAlexAdapter()
    with pytest.raises(TypeError, match="search expects an AdapterSearchQuery"):
        adapter.search("not a query")  # type: ignore[arg-type]
    with pytest.raises(
        TypeError, match="fetch_metadata expects an AdapterSourceRef"
    ):
        adapter.fetch_metadata("not a ref")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="normalize expects an AdapterRawRecord"):
        adapter.normalize("not a record")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="acquire expects"):
        adapter.acquire(123)  # type: ignore[arg-type]


def test_source_ref_requires_exactly_one_identity_dimension() -> None:
    """AdapterSourceRef with zero or two identity dimensions is rejected."""
    with pytest.raises(
        TypeError, match="requires exactly one of doi, stable_identifier, url"
    ):
        AdapterSourceRef()
    with pytest.raises(
        TypeError, match="requires exactly one of doi, stable_identifier, url"
    ):
        AdapterSourceRef(doi="10.1/x", stable_identifier="arXiv:2401.00001")


def test_search_rejects_malformed_query_values() -> None:
    """Malformed query parameters raise AdapterDataError with stable
    messages."""
    adapter = CrossrefOpenAlexAdapter()
    with pytest.raises(AdapterDataError, match="max_results must be a non-negative"):
        adapter.search(AdapterSearchQuery(query_text="x", max_results=-1))
    with pytest.raises(AdapterDataError, match="max_results must be a non-negative"):
        adapter.search(AdapterSearchQuery(query_text="x", max_results="20"))  # type: ignore[arg-type]
    with pytest.raises(AdapterDataError, match="query_text must be a str"):
        adapter.search(AdapterSearchQuery(query_text=7))  # type: ignore[arg-type]


def test_normalize_rejects_malformed_raw_fields() -> None:
    """Wrong raw field types raise TypeError; blank titles raise
    AdapterDataError."""
    adapter = CrossrefOpenAlexAdapter()
    with pytest.raises(TypeError, match="title must be a str"):
        adapter.normalize(
            AdapterRawRecord(title=7, source_type=SourceType.TARGET_PAPER)  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="source_type must be a SourceType"):
        adapter.normalize(
            AdapterRawRecord(title="x", source_type="target_paper")  # type: ignore[arg-type]
        )
    with pytest.raises(AdapterDataError, match="empty title after trimming"):
        adapter.normalize(
            AdapterRawRecord(title="   ", source_type=SourceType.TARGET_PAPER)
        )
    with pytest.raises(TypeError, match="publication_year must be an int"):
        adapter.normalize(
            AdapterRawRecord(
                title="x",
                source_type=SourceType.TARGET_PAPER,
                publication_year="2025",  # type: ignore[arg-type]
            )
        )


# ---------------------------------------------------------------------------
# Capability vocabulary (commercial optional capability flags)
# ---------------------------------------------------------------------------


def test_capability_vocabulary_has_stable_values() -> None:
    """The capability vocabulary is defined with stable values."""
    assert tuple(AdapterState) == (AdapterState.AVAILABLE, AdapterState.ABSENT)
    assert tuple(AdapterOperation) == (
        AdapterOperation.SEARCH,
        AdapterOperation.FETCH_METADATA,
        AdapterOperation.FETCH_CONTENT,
    )
    assert ADAPTER_CONTRACT_VERSION == "1.0"
    assert FIXTURE_VERSION == "1.0"


def test_absent_capability_shape_is_defined() -> None:
    """AdapterCapability.absent is the defined 'absent' record: ABSENT
    state, no operations, version 0.0.0-absent (AC-01 vocabulary)."""
    absent = AdapterCapability.absent(
        adapter_id="scifinder",
        commercial=True,
        access_class=AccessClass.OPTIONAL_COMMERCIAL,
        description="not registered",
    )
    assert absent.state is AdapterState.ABSENT
    assert absent.commercial is True
    assert absent.access_class is AccessClass.OPTIONAL_COMMERCIAL
    assert absent.operations == ()
    assert absent.version == "0.0.0-absent"
    assert absent.is_available() is False


def test_public_adapters_declare_public_non_commercial_capabilities() -> None:
    """Every shipped public adapter reports AVAILABLE, PUBLIC access and
    search+metadata operations (09-RESEARCH-SUBSYSTEM.md section 4)."""
    assert len(PUBLIC_ADAPTERS) == 3
    for adapter in PUBLIC_ADAPTERS:
        capability = adapter.capability()
        assert capability.state is AdapterState.AVAILABLE
        assert capability.commercial is False
        assert capability.access_class is AccessClass.PUBLIC
        assert set(capability.operations) == {
            AdapterOperation.SEARCH,
            AdapterOperation.FETCH_METADATA,
        }
        assert capability.version == adapter.version
        assert capability.adapter_id == adapter.adapter_id


def test_public_adapters_have_distinct_stable_ids() -> None:
    """Adapter ids are unique and stable across instances."""
    ids = [adapter.adapter_id for adapter in PUBLIC_ADAPTERS]
    assert len(set(ids)) == len(ids)
    assert set(ids) == {
        "crossref_openalex",
        "public_repository",
        "crystallographic_database",
    }
    assert CrossrefOpenAlexAdapter().adapter_id == "crossref_openalex"


# ---------------------------------------------------------------------------
# Offline determinism (AC-03)
# ---------------------------------------------------------------------------


def test_fixture_resolution_never_touches_the_network(monkeypatch) -> None:
    """With every socket entry point patched to raise, all public
    adapters still search, fetch and acquire -- the contract is testable
    without a live network (AC-03)."""
    _refuse_network(monkeypatch)
    for adapter in PUBLIC_ADAPTERS:
        result = adapter.acquire(AdapterSearchQuery(query_text=""))
        assert result.adapter_id == adapter.adapter_id
        assert result.sources
        first = result.sources[0]
        if first.doi is not None:
            ref = AdapterSourceRef(doi=first.doi)
        elif first.stable_identifier is not None:
            ref = AdapterSourceRef(stable_identifier=first.stable_identifier)
        else:
            ref = AdapterSourceRef(url_or_locator=first.url_or_locator)
        fetched = adapter.fetch_metadata(ref)
        assert fetched is not None
        assert adapter.search(AdapterSearchQuery(query_text="zzz")).records == ()


def test_fixture_resolution_is_deterministic_across_runs(monkeypatch) -> None:
    """Two acquisition runs over the same adapters yield identical
    results (pure fixture data: no wall-clock, no randomness)."""
    _refuse_network(monkeypatch)
    query = AdapterSearchQuery(query_text="")
    first = tuple(adapter.acquire(query) for adapter in PUBLIC_ADAPTERS)
    second = tuple(adapter.acquire(query) for adapter in PUBLIC_ADAPTERS)
    assert first == second


def test_primary_fixture_roundtrips_through_normalization() -> None:
    """The reference-case primary-paper fixture normalizes to a
    well-formed source (AC-02 exercised at the contract level)."""
    adapter = CrossrefOpenAlexAdapter()
    raw = adapter.search(AdapterSearchQuery(query_text="metal")).records[0]
    source = adapter.normalize(raw)
    assert source.title == PRIMARY_FIXTURE
    assert source.source_type is SourceType.TARGET_PAPER
    assert source.doi == "10.1039/d5ta00771b"
    assert source.provenance == "adapter:crossref_openalex@v0.1.0"


def test_malformed_identity_is_surfaced_not_bent() -> None:
    """A malformed DOI in raw adapter data raises SourceNormalizationError
    (stable message) through the frozen normalization path."""
    adapter = CrossrefOpenAlexAdapter()
    with pytest.raises(
        SourceNormalizationError, match="normalize_doi: malformed DOI"
    ):
        adapter.normalize(
            AdapterRawRecord(
                title="x",
                source_type=SourceType.PEER_REVIEWED_PAPER,
                doi="not-a-doi",
            )
        )
