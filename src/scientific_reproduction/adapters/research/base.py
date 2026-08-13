"""ResearchAdapter contract for public scholarly source acquisition (DEV-M5-G06).

Defines the **ResearchAdapter interface** deliverable: the contract a
Research agent uses to acquire sources through adapters
(agent-contracts/RESEARCH.md), grounded in the frozen specs:

* ``09-RESEARCH-SUBSYSTEM.md`` section 4 ("Source adapters"): v0.1 must
  support **public/open sources** (DOI/publisher public pages,
  Crossref/OpenAlex-like metadata services, public repositories, public
  crystallographic/materials databases, public standards/manuals where
  legally accessible) and define **optional adapters for commercial
  sources** (CSD/CCDC subscription, SciFinder, Web of Science, Scopus,
  institutional search systems). *Missing paid access must degrade
  gracefully rather than block the whole project* (AC-01).
* ``15-ADAPTER-SPEC.md`` section 4 ("ResearchSourceAdapter"): operations
  vary by source type but should normalize query/search, metadata fetch,
  content/file fetch when legally and technically available, stable
  identifiers, provenance and access limitations. Commercial adapters
  are optional.
* ``06-EVIDENCE-SYSTEM.md`` section 7 ("Search deduplication"): source
  identity is maintained by DOI/identifier/hash, and multiple mirrors of
  one paper are never treated as independent evidence.

Adapter outputs normalize to Source records (AC-02)
---------------------------------------------------
Every :class:`ResearchAdapter` constructs frozen
``core.models.ResearchSource`` records whose identity-bearing fields
flow through the frozen normalization path of ``research.sources``
(DEV-M5-G01): ``normalize_doi`` / ``normalize_stable_identifier`` /
``normalize_url``. Adapter-normalized records therefore carry
**canonical identity fields** -- the adapter layer is the IO boundary
that canonicalizes external recording noise (``doi:`` prefixes, DOI
wrapper URLs, ``www.`` hosts, default ports, trailing slashes, query and
fragment noise) once at ingestion -- so ``research.sources.canonical_identity``
derives the same dedupe keys the research layer relies on. Malformed
identity data surfaces loudly as ``SourceNormalizationError`` (stable
message), never silently bent into a plausible-looking key.

Determinism (AC-03)
-------------------
The contract is testable without a live network: the shipped public
adapters (``public.py``) resolve **pure deterministic offline fixtures**
(``fixtures.py``) -- no wall-clock, no randomness, no network anywhere
in the tested path. ``acquired_at`` is deliberately left unset by
adapters (acquisition time is the research layer's concern), and record
IDs derive from ``core.ids.generate_id`` as pure functions of
adapter + record identity.

Capability vocabulary (commercial optionality)
----------------------------------------------
Adapters are IO boundaries, so optionality is expressed with stable
values: an adapter reports its capability flags (:class:`AdapterCapability`
-- the operations it supports, whether it is commercial, and its
availability state), and a registry query for an absent commercial
adapter returns a **defined "absent" state** instead of raising
(AC-01, ``registry.py``).

Error discipline follows the frozen rule-engine paradigm: ``TypeError``
at public boundaries, a ``ValueError``-subclass error hierarchy with
stable messages otherwise.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import AccessClass, ResearchSource, SourceType
from scientific_reproduction.research.sources import (
    RECORD_SCOPED_TYPES,
    normalize_doi,
    normalize_stable_identifier,
    normalize_url,
)

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "AdapterError",
    "AdapterDataError",
    "AdapterRecordNotFoundError",
    "AdapterRegistrationError",
    "AdapterState",
    "AdapterOperation",
    "AdapterSearchQuery",
    "AdapterSourceRef",
    "AdapterRawRecord",
    "AdapterSearchResult",
    "AdapterAcquisitionResult",
    "AdapterCapability",
    "ResearchAdapter",
]

#: Version of the adapter contract. Bumped whenever a contract rule
#: changes; the same version always accepts the same adapter inputs and
#: yields the same normalized records.
ADAPTER_CONTRACT_VERSION: str = "1.0"


class AdapterError(ValueError):
    """Base error of the research adapter subsystem.

    Every message is stable: it names the failing operation and the
    reason, so callers and tests can rely on it.
    """


class AdapterDataError(AdapterError):
    """Raised when an adapter receives malformed data (blank title, bad
    query parameters, ...)."""


class AdapterRecordNotFoundError(AdapterError):
    """Raised when an adapter holds no record matching a lookup reference."""


class AdapterRegistrationError(AdapterError):
    """Raised when an adapter cannot be registered (duplicate adapter_id)."""


class AdapterState(StrEnum):
    """Availability state of an adapter.

    The defined "absent" state is the stable answer a capability query
    returns for an unregistered/absent adapter: it never raises and
    never blocks the core research workflow (AC-01).
    """

    AVAILABLE = "AVAILABLE"
    ABSENT = "ABSENT"


class AdapterOperation(StrEnum):
    """One normalized operation an adapter may support.

    The vocabulary of 15-ADAPTER-SPEC.md section 4 ("ResearchSourceAdapter"):
    query/search, fetch metadata, and fetch content/file when legally
    and technically available.
    """

    SEARCH = "search"
    FETCH_METADATA = "fetch_metadata"
    FETCH_CONTENT = "fetch_content"


@dataclass(frozen=True)
class AdapterSearchQuery:
    """A normalized query/search request (15-ADAPTER-SPEC.md section 4).

    ``search_family`` names one of the 09-RESEARCH-SUBSYSTEM.md section 6
    search families (e.g. ``"primary_paper"``, ``"public_database"``)
    when known; the default ``"general"`` is the catch-all.
    """

    query_text: str
    search_family: str = "general"
    max_results: int = 20


@dataclass(frozen=True)
class AdapterSourceRef:
    """Exactly one stable identity dimension used to fetch one source.

    Identity dimensions follow 06-EVIDENCE-SYSTEM.md section 7
    (DOI/identifier/URL); a reference must carry exactly one of them.

    Raises:
        TypeError: zero or more than one dimension is set.
    """

    doi: str | None = None
    stable_identifier: str | None = None
    url_or_locator: str | None = None

    def __post_init__(self) -> None:
        dimensions = sum(
            1
            for value in (self.doi, self.stable_identifier, self.url_or_locator)
            if value is not None
        )
        if dimensions != 1:
            raise TypeError(
                "AdapterSourceRef requires exactly one of doi, "
                f"stable_identifier, url_or_locator; got {dimensions}"
            )

    def describe(self) -> str:
        """Human-readable summary of the carried dimension (stable form)."""
        for name in ("doi", "stable_identifier", "url_or_locator"):
            value = getattr(self, name)
            if value is not None:
                return f"{name}={value!r}"
        return "<empty>"  # unreachable: __post_init__ enforces exactly one


@dataclass(frozen=True)
class AdapterRawRecord:
    """Raw record as reported by an external service, pre-normalization.

    Identity fields are the service-reported recording forms (bare or
    prefixed/wrapper DOIs, unnormalized URLs, ...); normalization into a
    frozen :class:`ResearchSource` happens in
    :meth:`ResearchAdapter.normalize` (AC-02).
    """

    title: str
    source_type: SourceType
    doi: str | None = None
    stable_identifier: str | None = None
    url_or_locator: str | None = None
    publication_year: int | None = None
    access_class: AccessClass | None = None


@dataclass(frozen=True)
class AdapterSearchResult:
    """The deterministic result of one adapter search."""

    adapter_id: str
    query: AdapterSearchQuery
    records: tuple[AdapterRawRecord, ...]


@dataclass(frozen=True)
class AdapterAcquisitionResult:
    """Adapter output already normalized to Source records (AC-02).

    Every ``sources`` member is a frozen ``ResearchSource`` whose
    identity fields flowed through ``research.sources`` normalization.
    """

    adapter_id: str
    sources: tuple[ResearchSource, ...]


@dataclass(frozen=True)
class AdapterCapability:
    """Stable capability flags of one adapter (commercial optionality).

    Grounding: 09-RESEARCH-SUBSYSTEM.md section 4 (public/open sources
    must be supported; commercial adapters are optional) and
    15-ADAPTER-SPEC.md section 4 (commercial adapters are optional).
    ``commercial`` is the defined commercial flag, ``state`` the
    availability, ``operations`` the supported capability set.
    """

    adapter_id: str
    commercial: bool
    access_class: AccessClass
    state: AdapterState
    operations: tuple[AdapterOperation, ...]
    version: str
    description: str

    @classmethod
    def absent(
        cls,
        adapter_id: str,
        *,
        commercial: bool,
        access_class: AccessClass,
        description: str,
    ) -> AdapterCapability:
        """The defined "absent" capability of an unregistered adapter.

        Stable shape: ``state=ABSENT``, no operations, version
        ``"0.0.0-absent"``. Capability queries for unregistered/absent
        adapters (commercial ones included) never raise (AC-01).
        """
        return cls(
            adapter_id=adapter_id,
            commercial=commercial,
            access_class=access_class,
            state=AdapterState.ABSENT,
            operations=(),
            version="0.0.0-absent",
            description=description,
        )

    def is_available(self) -> bool:
        """True iff the adapter is registered and in AVAILABLE state."""
        return self.state is AdapterState.AVAILABLE


class ResearchAdapter(ABC):
    """Contract for acquiring scholarly sources through an adapter.

    Subclasses declare their identity and capabilities as stable,
    versioned class constants: ``adapter_id``, ``version``,
    ``access_class``, ``commercial``, ``description``, ``operations``.

    Implementations must be deterministic and must not touch the
    network in the tested path (AC-03); the shipped public adapters
    resolve pure offline fixtures. Absence of any commercial adapter
    never blocks the core research workflow (AC-01): a registry query
    for an unregistered adapter returns the defined ABSENT capability
    (``registry.py``).
    """

    adapter_id: ClassVar[str]
    version: ClassVar[str]
    access_class: ClassVar[AccessClass]
    commercial: ClassVar[bool] = False
    description: ClassVar[str] = ""
    operations: ClassVar[frozenset[AdapterOperation]] = frozenset()

    def capability(self) -> AdapterCapability:
        """The adapter's capability flags; always AVAILABLE for a
        registered, working adapter."""
        return AdapterCapability(
            adapter_id=self.adapter_id,
            commercial=self.commercial,
            access_class=self.access_class,
            state=AdapterState.AVAILABLE,
            operations=tuple(sorted(self.operations, key=lambda op: op.value)),
            version=self.version,
            description=self.description,
        )

    @abstractmethod
    def search(self, query: AdapterSearchQuery) -> AdapterSearchResult:
        """Query/search for source metadata (15-ADAPTER-SPEC.md section 4).

        Raises:
            TypeError: ``query`` is not an :class:`AdapterSearchQuery`.
            AdapterDataError: malformed query parameters.
        """

    @abstractmethod
    def fetch_metadata(self, ref: AdapterSourceRef) -> AdapterRawRecord:
        """Fetch the metadata record of one source by stable identity.

        Raises:
            TypeError: ``ref`` is not an :class:`AdapterSourceRef`.
            AdapterRecordNotFoundError: no record matches the identity.
        """

    def normalize(self, raw: AdapterRawRecord) -> ResearchSource:
        """Normalize a raw adapter record into a frozen ResearchSource (AC-02).

        The identity-bearing fields flow through the frozen
        ``research.sources`` normalization path (DEV-M5-G01):
        ``doi`` -> ``normalize_doi``, ``stable_identifier`` ->
        ``normalize_stable_identifier``, ``url_or_locator`` ->
        ``normalize_url``, so the resulting record carries canonical
        identity fields and ``canonical_identity`` derives the same
        dedupe keys the research layer relies on (06-EVIDENCE-SYSTEM.md
        section 7). Record IDs are pure functions of adapter + record
        identity (``core.ids.generate_id``); ``provenance`` is the
        deterministic adapter stamp ``adapter:<id>@v<version>``;
        ``acquired_at`` is left unset (acquisition time is the research
        layer's concern); ``access_class`` defaults to the adapter's own
        access class.

        Raises:
            TypeError: ``raw`` is not an :class:`AdapterRawRecord`, or a
                field has the wrong type.
            AdapterDataError: a value is malformed (blank title).
            SourceNormalizationError: an identity-bearing field cannot be
                normalized (malformed identity is surfaced loudly, never
                silently bent).
        """
        if not isinstance(raw, AdapterRawRecord):
            raise TypeError(
                "normalize expects an AdapterRawRecord, "
                f"got {type(raw).__name__}"
            )
        if not isinstance(raw.title, str):
            raise TypeError(
                f"{self.adapter_id}: normalize: title must be a str, "
                f"got {type(raw.title).__name__}"
            )
        if not isinstance(raw.source_type, SourceType):
            raise TypeError(
                f"{self.adapter_id}: normalize: source_type must be a "
                f"SourceType, got {type(raw.source_type).__name__}"
            )
        if raw.access_class is not None and not isinstance(
            raw.access_class, AccessClass
        ):
            raise TypeError(
                f"{self.adapter_id}: normalize: access_class must be an "
                f"AccessClass, got {type(raw.access_class).__name__}"
            )
        if raw.publication_year is not None and not isinstance(
            raw.publication_year, int
        ):
            raise TypeError(
                f"{self.adapter_id}: normalize: publication_year must be "
                f"an int, got {type(raw.publication_year).__name__}"
            )
        title = raw.title.strip()
        if not title:
            raise AdapterDataError(
                f"{self.adapter_id}: normalize: empty title after trimming"
            )
        doi = normalize_doi(raw.doi) if raw.doi is not None else None
        stable_identifier = (
            normalize_stable_identifier(raw.stable_identifier)
            if raw.stable_identifier is not None
            else None
        )
        url_or_locator = (
            normalize_url(raw.url_or_locator)
            if raw.url_or_locator is not None
            else None
        )
        if raw.source_type in RECORD_SCOPED_TYPES:
            # AC-02 (DEV-M5-G01): SI/dataset/structure records keep their
            # own address; the parent paper's DOI must not conflate
            # distinct records into one source id.
            identity_material = stable_identifier or url_or_locator or title
        else:
            identity_material = doi or stable_identifier or url_or_locator or title
        source_id = generate_id("src", self.adapter_id, identity_material)
        return ResearchSource(
            source_id=source_id,
            source_type=raw.source_type,
            title=title,
            provenance=f"adapter:{self.adapter_id}@v{self.version}",
            doi=doi,
            stable_identifier=stable_identifier,
            url_or_locator=url_or_locator,
            publication_year=raw.publication_year,
            access_class=raw.access_class or self.access_class,
        )

    def acquire(self, query: AdapterSearchQuery) -> AdapterAcquisitionResult:
        """Search and normalize every hit into Source records (AC-02).

        The acquisition result of the adapter interface is normalized
        through the research source identity path, so downstream code
        receives only well-formed ``ResearchSource`` records.

        Raises:
            TypeError: ``query`` is not an :class:`AdapterSearchQuery`.
        """
        if not isinstance(query, AdapterSearchQuery):
            raise TypeError(
                "acquire expects an AdapterSearchQuery, "
                f"got {type(query).__name__}"
            )
        result = self.search(query)
        sources = tuple(self.normalize(record) for record in result.records)
        return AdapterAcquisitionResult(
            adapter_id=self.adapter_id,
            sources=sources,
        )
