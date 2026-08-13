"""Tests for source normalization and canonical identity (DEV-M5-G01).

Acceptance coverage:
  * AC-01 -- duplicate DOI mirrors collapse to one canonical scholarly
    source: the normalization grids prove that every accepted DOI form
    (bare, ``doi:`` prefix, ``doi.org`` / ``dx.doi.org`` wrapper URLs,
    any casing, surrounding whitespace) normalizes to one canonical
    string, so two mirrors sharing the same registered DOI carry the same
    canonical identity key regardless of how the DOI was recorded.
  * AC-02 -- distinct SI/dataset/structure records remain separately
    addressable: canonical identity is record-scoped for
    ``supplementary_information`` / ``dataset`` / ``structure_deposition``
    (``record:<source_id>``), never DOI-derived, so a dataset record
    sharing the paper's DOI keeps its own identity.
  * AC-03 -- provenance is retained on the canonical record (the dedupe
    module's merge rules); this file locks the identity-level determinism
    that makes retained provenance meaningful: identical records yield
    identical identities and every identity records how its key was
    derived.

The grids are exhaustive over the documented normalization dimensions and
the invalid batteries assert the stable error messages.
"""

from __future__ import annotations

from typing import Any

import pytest

from scientific_reproduction.core.models import ResearchSource, SourceType
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

REFERENCE_DOI = "10.1039/D5TA00771B"  # 17-FDM201-REFERENCE-CASE.md
CANONICAL_DOI = "10.1039/d5ta00771b"

#: Every accepted recording form of the reference DOI; all must normalize
#: to CANONICAL_DOI.
DOI_FORM_VARIANTS: dict[str, str] = {
    "bare": REFERENCE_DOI,
    "lowercase": "10.1039/d5ta00771b",
    "mixed-case-suffix": "10.1039/D5tA00771B",
    "uppercase-everything": "10.1039/D5TA00771B".upper(),
    "doi-prefix": "DOI:" + REFERENCE_DOI,
    "doi-prefix-lowercase": "doi:" + REFERENCE_DOI,
    "doi-prefix-with-space": "DOI: " + REFERENCE_DOI,
    "surrounding-whitespace": f"  {REFERENCE_DOI}  ",
    "doi.org-url": "https://doi.org/" + REFERENCE_DOI,
    "doi.org-http": "http://doi.org/" + REFERENCE_DOI,
    "dx.doi.org-url": "https://dx.doi.org/" + REFERENCE_DOI,
    "dx.doi.org-http": "http://dx.doi.org/" + REFERENCE_DOI,
    "doi.org-www": "https://www.doi.org/" + REFERENCE_DOI,
    "doi.org-trailing-slash": "https://doi.org/" + REFERENCE_DOI + "/",
    "doi.org-query": "https://doi.org/" + REFERENCE_DOI + "?utm_source=test",
    "doi.org-fragment": "https://doi.org/" + REFERENCE_DOI + "#section-1",
    "doi-prefix-plus-wrapper": "DOI: https://doi.org/" + REFERENCE_DOI,
    "doi.org-casing": "HTTPS://DOI.ORG/" + REFERENCE_DOI,
}

#: Accepted URL recording forms; each group must normalize to its canonical
#: string.
URL_VARIANTS: dict[str, str] = {
    "canonical": "https://example.com/journal/paper.pdf",
    "scheme-case": "HTTPS://example.com/journal/paper.pdf",
    "host-case": "https://EXAMPLE.com/journal/paper.pdf",
    "www-host": "https://www.example.com/journal/paper.pdf",
    "default-https-port": "https://example.com:443/journal/paper.pdf",
    "trailing-slash": "https://example.com/journal/paper.pdf/",
    "fragment": "https://example.com/journal/paper.pdf#page=3",
    "leading-whitespace": "  https://example.com/journal/paper.pdf",
    "http-canonical": "http://example.org/a",
    "http-scheme-case": "HTTP://example.org/a",
    "http-default-port": "http://example.org:80/a",
    "root-path": "http://example.org/",
    "bare-host": "http://example.org",
}
CANONICAL_URL_PDF = "https://example.com/journal/paper.pdf"
CANONICAL_URL_HTTP = "http://example.org/a"
CANONICAL_URL_HTTP_ORIGIN = "http://example.org"

#: URLs that differ only in query parameter order must normalize equally.
QUERY_URL_CANONICAL = "https://example.com/search?a=1&b=2"
QUERY_URL_VARIANTS = [
    "https://example.com/search?a=1&b=2",
    "https://example.com/search?b=2&a=1",
    "https://example.com/search?b=2&a=1#results",
    "https://example.com/search?a=1&&b=2&",
]


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


# ---------------------------------------------------------------------------
# DOI normalization grid (AC-01 identity groundwork)
# ---------------------------------------------------------------------------


def test_source_doi_normalization_grid_all_forms_equal() -> None:
    normalized = {normalize_doi(variant) for variant in DOI_FORM_VARIANTS.values()}
    assert normalized == {CANONICAL_DOI}


def test_source_doi_normalization_is_lowercase_and_idempotent() -> None:
    once = normalize_doi(REFERENCE_DOI)
    assert once == CANONICAL_DOI
    assert normalize_doi(once) == once
    assert normalize_doi(CANONICAL_DOI) == CANONICAL_DOI


def test_source_doi_normalization_preserves_suffix_with_inner_case() -> None:
    # Casing anywhere in the DOI (prefix digits or suffix) is insignificant.
    assert normalize_doi("10.1039/D5TA00771B") == normalize_doi("10.1039/d5ta00771b")
    assert normalize_doi("10.1039/D5TA00771B") == normalize_doi("10.1039/D5tA00771B")


def test_source_doi_normalization_exhaustive_reference_grid() -> None:
    # Every accepted recording dimension of the reference DOI collapses to
    # the same canonical string (bi-implication of form-equivalence).
    for variant_id, variant in DOI_FORM_VARIANTS.items():
        assert normalize_doi(variant) == CANONICAL_DOI, variant_id


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "10.1039",
        "10.1039/",
        "doi:",
        "doi:   ",
        "DOI : 10.1039/D5TA00771B",  # whitespace inside the prefix
        "10.1/abc",  # registrant code too short (min 4 digits)
        "10.10390abc/xy",  # non-digit in the registrant code
        "10/abc",  # no dot
        "11.1039/abc",  # not a 10. DOI
        "10.1039/ab cd",  # interior whitespace
        "10.1039/ab\tcd",  # tab
        "10.1039/D5TA00771B?x=1",  # query on a bare DOI
        "10.1039/D5TA00771B#frag",  # fragment on a bare DOI
        "not a doi",
        "https://example.com/10.1039/D5TA00771B",  # wrong wrapper host
        "ftp://doi.org/10.1039/x",  # wrong wrapper scheme
        "https://doi.org/",  # wrapper without a DOI path
        "https://doi.org/10",  # wrapper path is not a DOI
    ],
    ids=[
        "empty",
        "whitespace-only",
        "prefix-without-suffix",
        "prefix-with-empty-suffix",
        "bare-doi-prefix",
        "doi-prefix-whitespace-only",
        "prefix-whitespace-before-colon",
        "registrant-too-short",
        "non-digit-registrant",
        "missing-dot",
        "not-10-prefix",
        "interior-whitespace",
        "tab",
        "bare-query",
        "bare-fragment",
        "free-text",
        "wrong-wrapper-host",
        "wrong-wrapper-scheme",
        "wrapper-without-path",
        "wrapper-non-doi-path",
    ],
)
def test_source_doi_normalization_rejects_malformed(value: str) -> None:
    with pytest.raises(SourceNormalizationError):
        normalize_doi(value)


def test_source_doi_normalization_error_message_is_stable() -> None:
    with pytest.raises(SourceNormalizationError, match="expected 10.<4-9 digits>"):
        normalize_doi("10.1/abc")
    with pytest.raises(SourceNormalizationError, match="empty DOI"):
        normalize_doi("")
    with pytest.raises(SourceNormalizationError, match="whitespace"):
        normalize_doi("10.1039/ab cd")
    with pytest.raises(SourceNormalizationError, match="wrapper host"):
        normalize_doi("https://example.com/10.1039/x")


def test_source_doi_normalization_type_error_at_boundary() -> None:
    with pytest.raises(TypeError):
        normalize_doi(10.1039)  # type: ignore[arg-type]


def test_source_doi_pattern_matches_canonical_reference() -> None:
    assert DOI_PATTERN.match(CANONICAL_DOI) is not None
    assert DOI_PATTERN.match("10.1039/D5TA00771B") is not None


# ---------------------------------------------------------------------------
# URL normalization grid (mirror locator forms)
# ---------------------------------------------------------------------------


def test_source_url_normalization_grid_all_forms_equal() -> None:
    for variant_id, variant in URL_VARIANTS.items():
        if variant_id in ("root-path", "bare-host"):
            expected = CANONICAL_URL_HTTP_ORIGIN
        elif variant_id.startswith("http"):
            expected = CANONICAL_URL_HTTP
        else:
            expected = CANONICAL_URL_PDF
        assert normalize_url(variant) == expected, variant_id


def test_source_url_normalization_query_order_invariant() -> None:
    normalized = {normalize_url(variant) for variant in QUERY_URL_VARIANTS}
    assert normalized == {QUERY_URL_CANONICAL}


def test_source_url_normalization_is_idempotent() -> None:
    once = normalize_url("HTTPS://www.example.com:443/path/?a=2&b=1")
    assert normalize_url(once) == once


def test_source_url_normalization_keeps_non_default_port_and_path_case() -> None:
    assert normalize_url("http://example.com:8080/a") == "http://example.com:8080/a"
    # Paths are case-sensitive and preserved verbatim.
    assert normalize_url("https://Example.com/PATH") == "https://example.com/PATH"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not a url",
        "example.com/path",  # missing scheme
        "//example.com/path",  # scheme-relative
        "ftp://example.com/x",
        "file:///c:/tmp/paper.pdf",
        "mailto:someone@example.com",
        "https://",  # no host
        "https://example.com:abc/",  # invalid port
        "https://exa mple.com/x",  # whitespace in host
        "https://example.com/a b",  # whitespace in path
        "https://user:pass@example.com/x",  # userinfo
    ],
    ids=[
        "empty",
        "whitespace-only",
        "free-text",
        "missing-scheme",
        "scheme-relative",
        "ftp-scheme",
        "file-scheme",
        "mailto-scheme",
        "no-host",
        "invalid-port",
        "whitespace-host",
        "whitespace-path",
        "userinfo",
    ],
)
def test_source_url_normalization_rejects_malformed(value: str) -> None:
    with pytest.raises(SourceNormalizationError):
        normalize_url(value)


def test_source_url_normalization_error_message_is_stable() -> None:
    with pytest.raises(SourceNormalizationError, match="absolute http"):
        normalize_url("example.com/path")
    with pytest.raises(SourceNormalizationError, match="unsupported scheme"):
        normalize_url("ftp://example.com/x")
    with pytest.raises(SourceNormalizationError, match="userinfo"):
        normalize_url("https://user:pass@example.com/x")


def test_source_url_normalization_type_error_at_boundary() -> None:
    with pytest.raises(TypeError):
        normalize_url(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Stable identifier normalization
# ---------------------------------------------------------------------------


def test_source_stable_identifier_normalization_trims_whitespace_only() -> None:
    assert normalize_stable_identifier("  CCDC-123456  ") == "CCDC-123456"
    assert normalize_stable_identifier("CCDC-123456") == "CCDC-123456"
    assert normalize_stable_identifier("CCDC-123456") == normalize_stable_identifier(
        "\tCCDC-123456\n"
    )


def test_source_stable_identifier_normalization_rejects_empty() -> None:
    with pytest.raises(SourceNormalizationError, match="empty identifier"):
        normalize_stable_identifier("")
    with pytest.raises(SourceNormalizationError, match="empty identifier"):
        normalize_stable_identifier("   ")


def test_source_stable_identifier_normalization_type_error() -> None:
    with pytest.raises(TypeError):
        normalize_stable_identifier(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Mirror-collapsible source type vocabulary (AC-01/AC-02 scoping)
# ---------------------------------------------------------------------------


def test_source_mirror_collapsible_types_cover_scholarly_sources() -> None:
    assert SourceType.SUPPLEMENTARY_INFORMATION in RECORD_SCOPED_TYPES
    assert SourceType.DATASET in RECORD_SCOPED_TYPES
    assert SourceType.STRUCTURE_DEPOSITION in RECORD_SCOPED_TYPES
    assert len(RECORD_SCOPED_TYPES) == 3
    assert MIRROR_COLLAPSIBLE_TYPES == set(SourceType) - RECORD_SCOPED_TYPES


@pytest.mark.parametrize(
    "source_type",
    [
        SourceType.TARGET_PAPER,
        SourceType.PEER_REVIEWED_PAPER,
        SourceType.REVIEW,
        SourceType.THESIS,
        SourceType.PREPRINT,
        SourceType.STANDARD,
        SourceType.OFFICIAL_DOCUMENTATION,
        SourceType.VENDOR_NOTE,
        SourceType.INFORMAL,
        SourceType.DATABASE_RECORD,
        SourceType.OTHER,
    ],
)
def test_source_scholarly_types_are_mirror_collapsible(source_type: SourceType) -> None:
    assert is_mirror_collapsible(source_type)


@pytest.mark.parametrize(
    "source_type",
    [
        SourceType.SUPPLEMENTARY_INFORMATION,
        SourceType.DATASET,
        SourceType.STRUCTURE_DEPOSITION,
    ],
)
def test_source_resource_records_are_not_mirror_collapsible(source_type: SourceType) -> None:
    assert not is_mirror_collapsible(source_type)


def test_source_is_mirror_collapsible_type_error_at_boundary() -> None:
    with pytest.raises(TypeError):
        is_mirror_collapsible("dataset")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Canonical identity derivation (AC-01/AC-02 identity keys)
# ---------------------------------------------------------------------------


def test_source_canonical_identity_doi_key() -> None:
    source = _source("s1", doi=REFERENCE_DOI)
    identity = canonical_identity(source)
    assert identity.key == f"doi:{CANONICAL_DOI}"
    assert identity.kind is CanonicalIdentityKind.DOI
    assert identity.normalized_doi == CANONICAL_DOI
    assert identity.normalized_stable_identifier is None
    assert identity.normalized_url is None


def test_source_canonical_identity_doi_forms_yield_identical_keys() -> None:
    keys = {
        canonical_identity(_source(f"s{i}", doi=variant)).key
        for i, variant in enumerate(DOI_FORM_VARIANTS.values())
    }
    assert keys == {f"doi:{CANONICAL_DOI}"}


def test_source_canonical_identity_doi_outranks_stable_identifier() -> None:
    identity = canonical_identity(
        _source("s1", doi=REFERENCE_DOI, stable_identifier="arXiv:2401.00001")
    )
    assert identity.key == f"doi:{CANONICAL_DOI}"
    assert identity.kind is CanonicalIdentityKind.DOI


def test_source_canonical_identity_stable_identifier_key() -> None:
    identity = canonical_identity(_source("s1", stable_identifier="  CCDC-123456 "))
    assert identity.key == "stable_identifier:CCDC-123456"
    assert identity.kind is CanonicalIdentityKind.STABLE_IDENTIFIER
    assert identity.normalized_stable_identifier == "CCDC-123456"


def test_source_canonical_identity_url_key() -> None:
    identity = canonical_identity(
        _source("s1", url_or_locator="https://www.example.com/journal/paper.pdf")
    )
    assert identity.key == "url:https://example.com/journal/paper.pdf"
    assert identity.kind is CanonicalIdentityKind.URL
    assert identity.normalized_url == CANONICAL_URL_PDF


def test_source_canonical_identity_url_falls_back_to_record_for_non_http() -> None:
    for locator in ("file:///c:/tmp/paper.pdf", "ftp://example.com/x", "not a url"):
        identity = canonical_identity(_source("s1", url_or_locator=locator))
        assert identity.key == "record:s1"
        assert identity.kind is CanonicalIdentityKind.RECORD


def test_source_canonical_identity_record_key_without_identity_fields() -> None:
    identity = canonical_identity(_source("s1"))
    assert identity.key == "record:s1"
    assert identity.kind is CanonicalIdentityKind.RECORD
    assert identity.normalized_doi is None
    assert identity.normalized_stable_identifier is None
    assert identity.normalized_url is None


@pytest.mark.parametrize(
    "source_type",
    [
        SourceType.SUPPLEMENTARY_INFORMATION,
        SourceType.DATASET,
        SourceType.STRUCTURE_DEPOSITION,
    ],
)
def test_source_canonical_identity_record_scoped_for_resource_records(
    source_type: SourceType,
) -> None:
    # AC-02: SI/dataset/structure records are addressed by their own
    # identifier even when they carry the parent paper's DOI.
    identity = canonical_identity(
        _source("ds1", source_type=source_type, doi=REFERENCE_DOI)
    )
    assert identity.key == "record:ds1"
    assert identity.kind is CanonicalIdentityKind.RECORD
    assert identity.normalized_doi is None


def test_source_canonical_identity_deterministic_for_identical_records() -> None:
    a = _source("s1", doi=REFERENCE_DOI)
    b = _source("s1", doi=REFERENCE_DOI)
    assert canonical_identity(a) == canonical_identity(b)
    assert canonical_identity(a).to_dict() == canonical_identity(b).to_dict()


def test_source_canonical_identity_to_dict_round_trips() -> None:
    identity = canonical_identity(_source("s1", doi=REFERENCE_DOI))
    plain = identity.to_dict()
    assert plain["key"] == f"doi:{CANONICAL_DOI}"
    assert plain["kind"] == "doi"
    assert plain["normalized_doi"] == CANONICAL_DOI
    assert plain["normalized_stable_identifier"] is None
    assert plain["normalized_url"] is None
    assert SourceIdentity(**plain) == identity


def test_source_canonical_identity_raises_on_malformed_doi() -> None:
    with pytest.raises(SourceNormalizationError, match="malformed DOI"):
        canonical_identity(_source("s1", doi="10.1/abc"))


def test_source_canonical_identity_type_error_at_boundary() -> None:
    with pytest.raises(TypeError):
        canonical_identity("s1")  # type: ignore[arg-type]


def test_source_normalization_version_is_stable() -> None:
    assert NORMALIZATION_VERSION == "1.0"
