"""Tests for the research adapter registry and commercial optionality
(DEV-M5-G06, AC-01).

Acceptance coverage:
  * AC-01 -- a missing commercial adapter does not block the core
    research workflow: capability queries for unregistered/absent
    commercial adapters return the defined ABSENT state (never raise,
    never block); the registry of available adapters enumerates the
    public ones regardless of commercial presence; and the acquisition
    workflow run with only public adapters is complete.
  * commercial optional capability flags -- absent commercial adapters
    report their flags as defined "absent" values
    (:data:`OPTIONAL_COMMERCIAL_ADAPTERS` vocabulary).

All tests are offline and deterministic.
"""

from __future__ import annotations

import pytest

from scientific_reproduction.adapters.research import (
    OPTIONAL_COMMERCIAL_ADAPTERS,
    PUBLIC_ADAPTERS,
    AdapterRegistrationError,
    AdapterSearchQuery,
    CrossrefOpenAlexAdapter,
    PublicRepositoryAdapter,
    ResearchAdapterRegistry,
    acquire_available_sources,
)
from scientific_reproduction.core.models import AccessClass
from scientific_reproduction.research.sources import canonical_identity


def _default_registry() -> ResearchAdapterRegistry:
    return ResearchAdapterRegistry.with_public_adapters()


def test_default_registry_enumerates_all_public_adapters() -> None:
    """The default v0.1 registry registers exactly the shipped public
    adapters."""
    registry = _default_registry()
    expected = tuple(sorted(a.adapter_id for a in PUBLIC_ADAPTERS))
    assert registry.adapter_ids() == expected
    assert registry.get("crossref_openalex") is not None
    assert registry.get("scifinder") is None


def test_available_adapters_enumerate_public_ones_regardless_of_commercial_presence() -> None:
    """available_adapters() enumerates the public adapters; absent
    commercial adapters never appear and never shrink the set (AC-01)."""
    registry = _default_registry()
    available = registry.available_adapters()
    public = registry.public_adapters()
    assert len(available) == len(PUBLIC_ADAPTERS)
    assert tuple(a.adapter_id for a in available) == tuple(
        sorted(a.adapter_id for a in PUBLIC_ADAPTERS)
    )
    assert tuple(a.adapter_id for a in public) == tuple(
        a.adapter_id for a in available
    )
    for commercial_id in OPTIONAL_COMMERCIAL_ADAPTERS:
        assert commercial_id not in registry.adapter_ids()
        assert registry.get(commercial_id) is None
    # a registry with a single public adapter still works: public
    # enumeration is independent of which commercial adapters are absent
    partial = ResearchAdapterRegistry([CrossrefOpenAlexAdapter()])
    assert tuple(a.adapter_id for a in partial.available_adapters()) == (
        "crossref_openalex",
    )


def test_absent_commercial_adapter_capability_is_defined_not_raising() -> None:
    """A capability query for every optional commercial adapter
    (09-RESEARCH-SUBSYSTEM.md section 4) returns the defined ABSENT
    record: commercial flag set, no operations, stable version; it never
    raises (AC-01)."""
    registry = _default_registry()
    assert set(OPTIONAL_COMMERCIAL_ADAPTERS) == {
        "csd_ccdc",
        "scifinder",
        "web_of_science",
        "scopus",
    }
    for adapter_id in OPTIONAL_COMMERCIAL_ADAPTERS:
        capability = registry.capability(adapter_id)
        assert capability.adapter_id == adapter_id
        assert capability.state.value == "ABSENT"
        assert capability.commercial is True
        assert capability.access_class is AccessClass.OPTIONAL_COMMERCIAL
        assert capability.operations == ()
        assert capability.version == "0.0.0-absent"
        assert capability.description.startswith(f"adapter {adapter_id!r} is not registered")
        assert capability.is_available() is False


def test_unknown_adapter_capability_is_defined_absent() -> None:
    """Even a totally unknown adapter id yields the defined ABSENT state
    (non-commercial, UNKNOWN access class), never an exception."""
    registry = _default_registry()
    capability = registry.capability("no_such_adapter")
    assert capability.state.value == "ABSENT"
    assert capability.commercial is False
    assert capability.access_class is AccessClass.UNKNOWN
    assert capability.operations == ()
    assert capability.version == "0.0.0-absent"


def test_capability_never_raises_for_absent_adapters() -> None:
    """Capability queries are total over adapter ids: every absent id --
    commercial, unknown, empty-ish -- returns a defined capability."""
    registry = _default_registry()
    for adapter_id in OPTIONAL_COMMERCIAL_ADAPTERS + (
        "no_such_adapter",
        "scopus_proxy",
        "csd_ccdc_mirror",
    ):
        capability = registry.capability(adapter_id)
        assert capability.adapter_id == adapter_id
        assert capability.is_available() is False


def test_registered_adapter_capability_is_available() -> None:
    """Registered adapters report their own AVAILABLE capability."""
    registry = _default_registry()
    for adapter_id in registry.adapter_ids():
        capability = registry.capability(adapter_id)
        assert capability.state.value == "AVAILABLE"
        assert capability.is_available() is True
        assert capability.version == "0.1.0"


def test_register_rejects_duplicate_adapter() -> None:
    """Registering the same adapter twice raises AdapterRegistrationError
    with a stable message."""
    registry = _default_registry()
    with pytest.raises(
        AdapterRegistrationError,
        match="register: adapter 'crossref_openalex' is already registered",
    ):
        registry.register(CrossrefOpenAlexAdapter())


def test_register_rejects_non_adapter() -> None:
    """Registering a non-ResearchAdapter raises TypeError."""
    registry = _default_registry()
    with pytest.raises(TypeError, match="register expects a ResearchAdapter"):
        registry.register("crossref_openalex")  # type: ignore[arg-type]


def test_registry_boundary_type_errors() -> None:
    """Registry public boundaries raise TypeError on wrong argument
    types."""
    registry = _default_registry()
    with pytest.raises(TypeError, match="capability expects an adapter_id str"):
        registry.capability(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="get expects an adapter_id str"):
        registry.get(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="acquire_available_sources expects a"):
        acquire_available_sources("registry", AdapterSearchQuery("x"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="acquire_available_sources expects an"):
        acquire_available_sources(registry, "query")  # type: ignore[arg-type]


def test_workflow_with_only_public_adapters_is_complete() -> None:
    """AC-01 end-to-end: the acquisition workflow run with only public
    adapters acquires every fixture source, normalizes every one through
    the identity path, and completes even though every optional
    commercial adapter is absent."""
    registry = _default_registry()
    results = acquire_available_sources(
        registry, AdapterSearchQuery(query_text="")
    )
    # one result per available (public) adapter, in adapter_id order
    assert tuple(r.adapter_id for r in results) == registry.adapter_ids()
    assert len(results) == len(PUBLIC_ADAPTERS)
    sources = [source for result in results for source in result.sources]
    assert len(sources) == 7  # all first open-source fixtures
    for source in sources:
        identity = canonical_identity(source)
        assert identity.key.startswith(("doi:", "record:", "stable_identifier:"))
    # absent commercial adapters never blocked anything: they are not
    # registered, report ABSENT, and the workflow above completed
    for adapter_id in OPTIONAL_COMMERCIAL_ADAPTERS:
        assert registry.get(adapter_id) is None
        assert registry.capability(adapter_id).state.value == "ABSENT"


def test_workflow_result_is_ordered_and_deterministic() -> None:
    """Two workflow runs produce identical, adapter_id-ordered results."""
    registry = _default_registry()
    query = AdapterSearchQuery(query_text="")
    first = acquire_available_sources(registry, query)
    second = acquire_available_sources(registry, query)
    assert first == second
    ids = [result.adapter_id for result in first]
    assert ids == sorted(ids)


def test_workflow_with_partial_registry_completes() -> None:
    """A registry with a subset of public adapters still yields a
    complete workflow for what it has."""
    registry = ResearchAdapterRegistry([PublicRepositoryAdapter()])
    results = acquire_available_sources(
        registry, AdapterSearchQuery(query_text="")
    )
    assert len(results) == 1
    assert results[0].adapter_id == "public_repository"
    assert len(results[0].sources) == 2
