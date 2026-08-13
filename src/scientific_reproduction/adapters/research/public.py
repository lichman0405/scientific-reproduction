"""Public-source adapter skeletons resolving deterministic fixtures (DEV-M5-G06).

Implements the **public-source adapter skeletons/fixtures** deliverable:
an abstract fixture-resolving base (:class:`FixtureResearchAdapter`) plus
the first open-source adapters, one per public-source family named in
09-RESEARCH-SUBSYSTEM.md section 4:

* ``CrossrefOpenAlexAdapter`` -- Crossref/OpenAlex-like public metadata
  service (DOI/publisher pages);
* ``PublicRepositoryAdapter`` -- public repositories (arXiv-like
  preprints, figshare-like datasets);
* ``CrystallographicDatabaseAdapter`` -- public crystallographic/
  materials databases (COD-like).

Every adapter is a **skeleton** whose ``search`` and ``fetch_metadata``
are pure functions of the query/reference and the class-level
deterministic fixture set (``fixtures.py``): no network, no wall-clock,
no randomness anywhere in the tested path (AC-03). The fixture contract
(15-ADAPTER-SPEC.md section 4) is: query/search and metadata fetch
normalize into stable source records through ``ResearchAdapter.normalize``
(AC-02); content/file fetch is *not* advertised by these skeletons
(fetch when legally and technically available is a later capability).
"""

from __future__ import annotations

from typing import ClassVar

from scientific_reproduction.adapters.research.base import (
    AdapterDataError,
    AdapterOperation,
    AdapterRawRecord,
    AdapterRecordNotFoundError,
    AdapterSearchQuery,
    AdapterSearchResult,
    AdapterSourceRef,
    ResearchAdapter,
)
from scientific_reproduction.adapters.research.fixtures import (
    CROSSREF_OPENALEX_FIXTURES,
    CRYSTALLOGRAPHIC_DATABASE_FIXTURES,
    PUBLIC_REPOSITORY_FIXTURES,
)
from scientific_reproduction.core.models import AccessClass
from scientific_reproduction.research.sources import (
    normalize_doi,
    normalize_stable_identifier,
    normalize_url,
)

__all__ = [
    "FixtureResearchAdapter",
    "CrossrefOpenAlexAdapter",
    "PublicRepositoryAdapter",
    "CrystallographicDatabaseAdapter",
    "PUBLIC_ADAPTERS",
]


def _searchable_text(record: AdapterRawRecord) -> str:
    """The deterministic searchable text of one fixture record."""
    return " ".join(
        part
        for part in (
            record.title,
            record.doi,
            record.stable_identifier,
            record.url_or_locator,
        )
        if part is not None
    )


def _matches_ref(record: AdapterRawRecord, ref: AdapterSourceRef) -> bool:
    """True iff ``record`` carries the identity ``ref`` names.

    Comparison goes through the frozen normalization path
    (``research.sources``), so recording-form differences (casing,
    prefixes, wrapper URLs, ``www.``/port/trailing-slash noise) never
    defeat an identity lookup. Malformed refs raise
    ``SourceNormalizationError`` loudly rather than matching nothing.
    """
    if ref.doi is not None:
        return (
            record.doi is not None
            and normalize_doi(record.doi) == normalize_doi(ref.doi)
        )
    if ref.stable_identifier is not None:
        return (
            record.stable_identifier is not None
            and normalize_stable_identifier(record.stable_identifier)
            == normalize_stable_identifier(ref.stable_identifier)
        )
    url = ref.url_or_locator
    assert url is not None  # AdapterSourceRef.__post_init__ guarantees it
    return (
        record.url_or_locator is not None
        and normalize_url(record.url_or_locator) == normalize_url(url)
    )


class FixtureResearchAdapter(ResearchAdapter):
    """Base skeleton for public-source adapters resolving offline fixtures.

    ``search`` returns every fixture record whose searchable text
    contains the (case-insensitive) query text -- an empty/whitespace
    query returns the whole fixture set; ``max_results`` truncates
    deterministically. ``fetch_metadata`` returns the unique record
    matching the reference identity, or raises
    :class:`AdapterRecordNotFoundError` with a stable message.

    Pure and deterministic (AC-03): both operations are functions of the
    query/ref and the class-level ``fixture_records`` constant only.
    """

    fixture_records: ClassVar[tuple[AdapterRawRecord, ...]] = ()

    def search(self, query: AdapterSearchQuery) -> AdapterSearchResult:
        if not isinstance(query, AdapterSearchQuery):
            raise TypeError(
                "search expects an AdapterSearchQuery, "
                f"got {type(query).__name__}"
            )
        if not isinstance(query.query_text, str):
            raise AdapterDataError(
                f"{self.adapter_id}: search: query_text must be a str, "
                f"got {type(query.query_text).__name__}"
            )
        if not isinstance(query.max_results, int) or query.max_results < 0:
            raise AdapterDataError(
                f"{self.adapter_id}: search: max_results must be a "
                "non-negative int"
            )
        needle = query.query_text.strip().lower()
        matches = [
            record
            for record in self.fixture_records
            if not needle or needle in _searchable_text(record).lower()
        ]
        if query.max_results:
            matches = matches[: query.max_results]
        else:
            matches = []
        return AdapterSearchResult(
            adapter_id=self.adapter_id,
            query=query,
            records=tuple(matches),
        )

    def fetch_metadata(self, ref: AdapterSourceRef) -> AdapterRawRecord:
        if not isinstance(ref, AdapterSourceRef):
            raise TypeError(
                "fetch_metadata expects an AdapterSourceRef, "
                f"got {type(ref).__name__}"
            )
        for record in self.fixture_records:
            if _matches_ref(record, ref):
                return record
        raise AdapterRecordNotFoundError(
            f"{self.adapter_id}: fetch_metadata: no fixture record "
            f"matches {ref.describe()}"
        )


class CrossrefOpenAlexAdapter(FixtureResearchAdapter):
    """Crossref/OpenAlex-like public metadata service adapter.

    Skeleton over ``CROSSREF_OPENALEX_FIXTURES``: DOI/publisher public
    pages and metadata lookup (09-RESEARCH-SUBSYSTEM.md section 4).
    """

    adapter_id = "crossref_openalex"
    version = "0.1.0"
    access_class = AccessClass.PUBLIC
    description = (
        "Crossref/OpenAlex-like public metadata service resolving "
        "deterministic offline fixtures (09-RESEARCH-SUBSYSTEM.md "
        "section 4)."
    )
    operations = frozenset(
        {AdapterOperation.SEARCH, AdapterOperation.FETCH_METADATA}
    )
    fixture_records = CROSSREF_OPENALEX_FIXTURES


class PublicRepositoryAdapter(FixtureResearchAdapter):
    """Public repository adapter (arXiv-like preprints, figshare-like
    datasets).

    Skeleton over ``PUBLIC_REPOSITORY_FIXTURES``: public repositories
    (09-RESEARCH-SUBSYSTEM.md section 4).
    """

    adapter_id = "public_repository"
    version = "0.1.0"
    access_class = AccessClass.PUBLIC
    description = (
        "Public repository adapter (arXiv-like preprints, figshare-like "
        "datasets) resolving deterministic offline fixtures "
        "(09-RESEARCH-SUBSYSTEM.md section 4)."
    )
    operations = frozenset(
        {AdapterOperation.SEARCH, AdapterOperation.FETCH_METADATA}
    )
    fixture_records = PUBLIC_REPOSITORY_FIXTURES


class CrystallographicDatabaseAdapter(FixtureResearchAdapter):
    """Public crystallographic/materials database adapter (COD-like).

    Skeleton over ``CRYSTALLOGRAPHIC_DATABASE_FIXTURES``: public
    crystallographic/materials databases (09-RESEARCH-SUBSYSTEM.md
    section 4).
    """

    adapter_id = "crystallographic_database"
    version = "0.1.0"
    access_class = AccessClass.PUBLIC
    description = (
        "Public crystallographic/materials database adapter (COD-like) "
        "resolving deterministic offline fixtures "
        "(09-RESEARCH-SUBSYSTEM.md section 4)."
    )
    operations = frozenset(
        {AdapterOperation.SEARCH, AdapterOperation.FETCH_METADATA}
    )
    fixture_records = CRYSTALLOGRAPHIC_DATABASE_FIXTURES


#: The first open-source adapters shipped in v0.1, in normative order.
#: Stateless instances: all behavior is a function of class constants.
PUBLIC_ADAPTERS: tuple[ResearchAdapter, ...] = (
    CrossrefOpenAlexAdapter(),
    PublicRepositoryAdapter(),
    CrystallographicDatabaseAdapter(),
)
