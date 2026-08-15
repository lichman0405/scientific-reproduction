"""research adapters subsystem package (DEV-M5-G06).

Implements the public ResearchAdapter interface for open scholarly /
public database acquisition (09-RESEARCH-SUBSYSTEM.md section 4;
15-ADAPTER-SPEC.md section 4): the adapter contract and capability
vocabulary (``base.py``), the registry with defined absence semantics
for optional commercial adapters (``registry.py``), the deterministic
offline fixtures (``fixtures.py``) and the first open-source adapter
skeletons (``public.py``). Commercial adapters remain optional and
absent in v0.1; missing paid access degrades gracefully and never
blocks the core research workflow (AC-01).
"""

from scientific_reproduction.adapters.research.base import (
    ADAPTER_CONTRACT_VERSION,
    AdapterAcquisitionResult,
    AdapterCapability,
    AdapterDataError,
    AdapterError,
    AdapterOperation,
    AdapterRawRecord,
    AdapterRecordNotFoundError,
    AdapterRegistrationError,
    AdapterSearchQuery,
    AdapterSearchResult,
    AdapterSourceRef,
    AdapterState,
    ResearchAdapter,
)
from scientific_reproduction.adapters.research.fixtures import (
    FIXTURE_VERSION,
    PUBLIC_SOURCE_FIXTURES,
)
from scientific_reproduction.adapters.research.network_policy import (
    BLOCKED_IP_LITERAL_NETWORKS,
    FAKE_IP_NETWORK,
    AdapterNetworkPolicyError,
    host_is_ip_literal,
    validate_fetch_url,
)
from scientific_reproduction.adapters.research.public import (
    PUBLIC_ADAPTERS,
    CrossrefOpenAlexAdapter,
    CrystallographicDatabaseAdapter,
    FixtureResearchAdapter,
    PublicRepositoryAdapter,
)
from scientific_reproduction.adapters.research.registry import (
    OPTIONAL_COMMERCIAL_ADAPTERS,
    ResearchAdapterRegistry,
    acquire_available_sources,
)

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "FIXTURE_VERSION",
    "AdapterError",
    "AdapterDataError",
    "AdapterRecordNotFoundError",
    "AdapterRegistrationError",
    "AdapterNetworkPolicyError",
    "AdapterState",
    "AdapterOperation",
    "AdapterSearchQuery",
    "AdapterSourceRef",
    "AdapterRawRecord",
    "AdapterSearchResult",
    "AdapterAcquisitionResult",
    "AdapterCapability",
    "ResearchAdapter",
    "FixtureResearchAdapter",
    "CrossrefOpenAlexAdapter",
    "PublicRepositoryAdapter",
    "CrystallographicDatabaseAdapter",
    "PUBLIC_ADAPTERS",
    "OPTIONAL_COMMERCIAL_ADAPTERS",
    "ResearchAdapterRegistry",
    "acquire_available_sources",
    "PUBLIC_SOURCE_FIXTURES",
    "BLOCKED_IP_LITERAL_NETWORKS",
    "FAKE_IP_NETWORK",
    "host_is_ip_literal",
    "validate_fetch_url",
]
