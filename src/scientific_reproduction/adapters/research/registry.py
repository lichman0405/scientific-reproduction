"""Research adapter registry and commercial optionality (DEV-M5-G06).

Implements the **commercial optional capability flags** deliverable and
the registry behind AC-01. Grounding: 09-RESEARCH-SUBSYSTEM.md section 4
-- v0.1 supports public/open sources and defines *optional adapters for
commercial sources* (CSD/CCDC subscription, SciFinder, Web of Science,
Scopus, institutional search systems); *missing paid access must degrade
gracefully rather than block the whole project*.

Semantics
---------
* :data:`OPTIONAL_COMMERCIAL_ADAPTERS` is the defined vocabulary of
  optional commercial adapter ids (stable values).
* ``ResearchAdapterRegistry.capability`` never raises for an absent
  adapter: an unregistered id -- commercial or not -- yields the defined
  ABSENT capability (:meth:`AdapterCapability.absent`, ``state=ABSENT``,
  ``commercial`` flag per the vocabulary, ``operations=()``, version
  ``"0.0.0-absent"``).
* ``available_adapters`` enumerates the registered adapters in AVAILABLE
  state, ordered by ``adapter_id``: the public ones are always
  enumerated regardless of commercial presence.
* ``acquire_available_sources`` is the minimal adapter-driven acquisition
  workflow a Research agent runs (agent-contracts/RESEARCH.md): it only
  iterates available adapters, so absent commercial adapters never block
  it -- the core research workflow is complete with only public adapters
  (AC-01).

Error discipline: ``TypeError`` at public boundaries (non-adapter
registration, non-str ids), ``AdapterRegistrationError`` for duplicate
registration.
"""

from __future__ import annotations

from collections.abc import Iterable

from scientific_reproduction.adapters.research.base import (
    AdapterAcquisitionResult,
    AdapterCapability,
    AdapterRegistrationError,
    AdapterSearchQuery,
    ResearchAdapter,
)
from scientific_reproduction.adapters.research.public import PUBLIC_ADAPTERS
from scientific_reproduction.core.models import AccessClass

__all__ = [
    "OPTIONAL_COMMERCIAL_ADAPTERS",
    "ResearchAdapterRegistry",
    "acquire_available_sources",
]

#: The defined vocabulary of optional commercial adapter ids
#: (09-RESEARCH-SUBSYSTEM.md section 4: "Optional adapters"). Stable
#: values; a capability query for one of these ids reports the defined
#: "absent" state when the adapter is not registered (AC-01).
OPTIONAL_COMMERCIAL_ADAPTERS: tuple[str, ...] = (
    "csd_ccdc",
    "scifinder",
    "web_of_science",
    "scopus",
)


class ResearchAdapterRegistry:
    """Registry of research adapters with defined absence semantics.

    All queries are deterministic: enumeration is ordered by
    ``adapter_id``, and capability queries are pure functions of the
    registered set.
    """

    def __init__(self, adapters: Iterable[ResearchAdapter] = ()) -> None:
        self._adapters: dict[str, ResearchAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: ResearchAdapter) -> None:
        """Register one adapter; duplicate ids are rejected.

        Raises:
            TypeError: ``adapter`` is not a ``ResearchAdapter``.
            AdapterRegistrationError: an adapter with the same
                ``adapter_id`` is already registered.
        """
        if not isinstance(adapter, ResearchAdapter):
            raise TypeError(
                "register expects a ResearchAdapter, "
                f"got {type(adapter).__name__}"
            )
        if adapter.adapter_id in self._adapters:
            raise AdapterRegistrationError(
                f"register: adapter {adapter.adapter_id!r} is already "
                "registered"
            )
        self._adapters[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> ResearchAdapter | None:
        """The registered adapter with ``adapter_id``, or None.

        Raises:
            TypeError: ``adapter_id`` is not a ``str``.
        """
        if not isinstance(adapter_id, str):
            raise TypeError(
                "get expects an adapter_id str, "
                f"got {type(adapter_id).__name__}"
            )
        return self._adapters.get(adapter_id)

    def capability(self, adapter_id: str) -> AdapterCapability:
        """Capability flags of one adapter; never raises for absent ones.

        A registered adapter reports its own (AVAILABLE) capability; an
        unregistered id returns the defined ABSENT capability, with the
        ``commercial`` flag set for ids in
        :data:`OPTIONAL_COMMERCIAL_ADAPTERS` (AC-01: a capability query
        for an unregistered/absent commercial adapter never raises and
        never blocks).

        Raises:
            TypeError: ``adapter_id`` is not a ``str``.
        """
        if not isinstance(adapter_id, str):
            raise TypeError(
                "capability expects an adapter_id str, "
                f"got {type(adapter_id).__name__}"
            )
        adapter = self._adapters.get(adapter_id)
        if adapter is not None:
            return adapter.capability()
        commercial = adapter_id in OPTIONAL_COMMERCIAL_ADAPTERS
        access_class = (
            AccessClass.OPTIONAL_COMMERCIAL
            if commercial
            else AccessClass.UNKNOWN
        )
        return AdapterCapability.absent(
            adapter_id=adapter_id,
            commercial=commercial,
            access_class=access_class,
            description=(
                f"adapter {adapter_id!r} is not registered; commercial "
                "adapters are optional (09-RESEARCH-SUBSYSTEM.md section 4)"
                if commercial
                else f"adapter {adapter_id!r} is not registered"
            ),
        )

    def available_adapters(self) -> tuple[ResearchAdapter, ...]:
        """Registered adapters in AVAILABLE state, ordered by adapter_id.

        Enumerates the public adapters regardless of commercial presence
        (AC-01): absent commercial adapters are not registered and
        therefore never appear and never block enumeration.
        """
        return tuple(
            sorted(
                (
                    adapter
                    for adapter in self._adapters.values()
                    if adapter.capability().is_available()
                ),
                key=lambda adapter: adapter.adapter_id,
            )
        )

    def public_adapters(self) -> tuple[ResearchAdapter, ...]:
        """Available adapters whose access class is PUBLIC."""
        return tuple(
            adapter
            for adapter in self.available_adapters()
            if adapter.access_class is AccessClass.PUBLIC
        )

    def adapter_ids(self) -> tuple[str, ...]:
        """Registered adapter ids, sorted."""
        return tuple(sorted(self._adapters))

    @classmethod
    def with_public_adapters(cls) -> ResearchAdapterRegistry:
        """The default v0.1 registry: every shipped public adapter."""
        return cls(PUBLIC_ADAPTERS)


def acquire_available_sources(
    registry: ResearchAdapterRegistry,
    query: AdapterSearchQuery,
) -> tuple[AdapterAcquisitionResult, ...]:
    """Acquire sources from every available adapter (AC-01 workflow).

    The minimal adapter-driven acquisition workflow of the Research
    agent (agent-contracts/RESEARCH.md): iterate the available adapters
    and normalize every hit through the research source identity path.
    Absent commercial adapters are never queried and never raise, so the
    workflow is complete with only public adapters present.

    Raises:
        TypeError: ``registry`` is not a ``ResearchAdapterRegistry`` or
            ``query`` is not an ``AdapterSearchQuery``.
    """
    if not isinstance(registry, ResearchAdapterRegistry):
        raise TypeError(
            "acquire_available_sources expects a ResearchAdapterRegistry, "
            f"got {type(registry).__name__}"
        )
    if not isinstance(query, AdapterSearchQuery):
        raise TypeError(
            "acquire_available_sources expects an AdapterSearchQuery, "
            f"got {type(query).__name__}"
        )
    return tuple(adapter.acquire(query) for adapter in registry.available_adapters())
