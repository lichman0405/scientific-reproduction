"""research subsystem package.

Implements the Research/Literature subsystem (09-RESEARCH-SUBSYSTEM.md).
As of DEV-M5-G01 this package provides normalized source records, canonical
source identity and DOI/mirror deduplication (06-EVIDENCE-SYSTEM.md
section 7; agent-contracts/RESEARCH.md).
"""

from scientific_reproduction.research.dedupe import (
    DEDUPE_RULESET_VERSION,
    SOURCE_DEDUPE_RULES,
    CanonicalSource,
    DedupeOutcome,
    SourceDedupeAssessment,
    SourceDedupeDecision,
    SourceDedupeRule,
    SourcePairAssessment,
    SourcePairInput,
    deduplicate_sources,
    evaluate_source_pair,
)
from scientific_reproduction.research.sources import (
    DOI_PATTERN,
    MIRROR_COLLAPSIBLE_TYPES,
    NORMALIZATION_VERSION,
    RECORD_SCOPED_TYPES,
    CanonicalIdentityKind,
    SourceIdentity,
    SourceNormalizationError,
    canonical_identity,
    is_mirror_collapsible,
    normalize_doi,
    normalize_stable_identifier,
    normalize_url,
)

__all__ = [
    "DEDUPE_RULESET_VERSION",
    "NORMALIZATION_VERSION",
    "DOI_PATTERN",
    "MIRROR_COLLAPSIBLE_TYPES",
    "RECORD_SCOPED_TYPES",
    "SourceNormalizationError",
    "normalize_doi",
    "normalize_url",
    "normalize_stable_identifier",
    "is_mirror_collapsible",
    "CanonicalIdentityKind",
    "SourceIdentity",
    "canonical_identity",
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
