"""Source normalization and canonical source identity (DEV-M5-G01).

Implements the **source normalization** deliverable: deterministic,
lossless-enough normalization of the identity-bearing fields of the frozen
``ResearchSource`` model (``schemas/source.schema.yaml``, modeled in
``core/models.py``) and derivation of a **canonical identity key** per
record. The frozen spec grounds this module:

* ``06-EVIDENCE-SYSTEM.md`` section 7 ("Search deduplication"): *Research
  must maintain source identity using DOI/identifier/hash and avoid treating
  multiple mirrors of the same paper as independent evidence.*
* ``agent-contracts/RESEARCH.md``: a worker must not *treat mirrored copies
  of one paper as independent evidence*.
* Duplicate DOI mirrors collapse to one source (frozen acceptance).

Everything here is pure and deterministic: no randomness, no wall-clock
time, no counter state. The same record always yields the same identity,
on every machine and in every process.

DOI normalization (normative readings)
--------------------------------------
A DOI is case-insensitive, so the canonical form is **lowercase** (the
registered casing, e.g. ``10.1039/D5TA00771B``, is preserved on the record
itself; only the identity key is lowercased). Accepted input forms:

* bare DOI: ``10.1039/D5TA00771B``;
* ``doi:`` prefix (case-insensitive, optional surrounding whitespace):
  ``DOI: 10.1039/D5TA00771B``;
* DOI wrapper URLs on ``doi.org`` / ``dx.doi.org`` (http or https,
  optional ``www.``): ``https://doi.org/10.1039/D5TA00771B``. A wrapper
  URL's trailing slash, query and fragment are **not** part of the DOI and
  are dropped.

A normalized DOI must match the DOI syntax ``10.`` + 4-9 digit registrant
code + ``/`` + non-empty suffix, and must contain no whitespace. Anything
else raises :class:`SourceNormalizationError` with a stable message --
malformed identity data is surfaced loudly, never silently bent into a
plausible-looking key.

URL normalization (normative readings)
--------------------------------------
Only absolute ``http``/``https`` URLs participate in mirror identity
("public/open sources" per 09-RESEARCH-SUBSYSTEM.md section 4). A
normalized URL is ``scheme://netloc/path?query`` where:

* scheme and host are lowercased (URL schemes and hosts are
  case-insensitive; paths are preserved verbatim -- they are
  case-sensitive);
* a leading ``www.`` is stripped from the host (``www.example.com`` and
  ``example.com`` are the same origin in practice);
* default ports (``:80`` for http, ``:443`` for https) are stripped,
  non-default ports kept;
* trailing slashes on the path are stripped (``/`` -> ``""``) and the
  fragment is dropped (fragments address a location *within* a page, not
  the mirror itself);
* query parameters are sorted and empty parameters dropped, so
  parameter-order variants normalize identically.

Non-http(s) locators (``file://``, ``ftp://``, bare paths) cannot be
mirror-location evidence; records carrying only such a locator fall back
to their own record address (see :func:`canonical_identity`).

Canonical identity derivation (normative readings)
--------------------------------------------------
For **mirror-collapsible** scholarly source types the canonical key is, in
priority order: normalized DOI (``doi:<norm>``), else normalized stable
identifier (``stable_identifier:<norm>``), else normalized http(s) URL
(``url:<norm>``), else the record's own address (``record:<source_id>``).
A DOI is the registered identity of the work itself, so it outranks any
other identifier a record may also carry.

**Scoping (AC-02):** the three resource-bearing record types --
``supplementary_information``, ``dataset`` and ``structure_deposition``
(schemas/source.schema.yaml, ``SourceType``) -- are **record-scoped**: they
keep their own address/identifier (``record:<source_id>``) and are never
identified by a DOI they carry (typically the parent paper's DOI). This is
what keeps distinct SI/dataset/structure records separately addressable
even when they share the paper's DOI.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum

from scientific_reproduction.core.models import ResearchSource, SourceType

__all__ = [
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
]

#: Version of the normalization rules. Bumped whenever a rule changes;
#: recorded in identities so old keys stay interpretable (auditability).
NORMALIZATION_VERSION: str = "1.0"

#: DOI syntax: ``10.`` + 4-9 digit registrant code + ``/`` + non-empty
#: suffix (no whitespace; checked separately).
DOI_PATTERN = re.compile(r"^10\.\d{4,9}/.+$")

#: DOI wrapper hosts whose URL form is recognized (https://doi.org/ and
#: https://dx.doi.org/ are the standard resolution endpoints).
DOI_WRAPPER_HOSTS: frozenset[str] = frozenset({"doi.org", "dx.doi.org"})

#: Record types that are NOT part of the scholarly mirror identity space
#: (AC-02): supplementary information, datasets and structure depositions
#: keep their own address and are never collapsed by DOI.
RECORD_SCOPED_TYPES: frozenset[SourceType] = frozenset(
    {
        SourceType.SUPPLEMENTARY_INFORMATION,
        SourceType.DATASET,
        SourceType.STRUCTURE_DEPOSITION,
    }
)

#: Scholarly source types whose identity is the work itself; records of
#: these types collapse when they share a canonical key (AC-01).
MIRROR_COLLAPSIBLE_TYPES: frozenset[SourceType] = frozenset(
    set(SourceType) - RECORD_SCOPED_TYPES
)


class SourceNormalizationError(ValueError):
    """Raised when a DOI/identifier/URL cannot be normalized.

    Stable messages: every message names the offending function and the
    reason, so callers and tests can rely on them.
    """


def normalize_doi(doi: str) -> str:
    """Return the canonical lowercase form of a DOI.

    Accepts the bare form, a ``doi:`` prefix and ``doi.org`` /
    ``dx.doi.org`` wrapper URLs; see the module docstring for the
    normative readings.

    Raises:
        TypeError: ``doi`` is not a ``str``.
        SourceNormalizationError: the value does not normalize to a valid
            DOI (empty, wrong syntax, contains whitespace, or a wrapper
            URL with an unsupported scheme/host).
    """
    if not isinstance(doi, str):
        raise TypeError(f"normalize_doi expects a str, got {type(doi).__name__}")
    value = doi.strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", value):
        value = _extract_doi_from_wrapper_url(value)
    if not value:
        raise SourceNormalizationError("normalize_doi: empty DOI after normalization")
    if "?" in value or "#" in value:
        raise SourceNormalizationError(
            "normalize_doi: DOI must not carry a query or fragment marker"
        )
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise SourceNormalizationError(
            "normalize_doi: DOI must not contain whitespace"
        )
    if DOI_PATTERN.match(value) is None:
        raise SourceNormalizationError(
            "normalize_doi: malformed DOI "
            f"{value!r}: expected 10.<4-9 digits>/<non-empty suffix>"
        )
    return value.lower()


def _extract_doi_from_wrapper_url(value: str) -> str:
    """Extract the DOI path from a recognized DOI wrapper URL.

    Only http(s) URLs on ``doi.org`` / ``dx.doi.org`` (optional ``www.``)
    are wrapper forms; the wrapper URL's trailing slash, query and
    fragment are not part of the DOI and are dropped.
    """
    parts = urllib.parse.urlsplit(value)
    if parts.scheme.lower() not in ("http", "https"):
        raise SourceNormalizationError(
            f"normalize_doi: unsupported DOI wrapper scheme {parts.scheme!r}"
            " (expected http or https)"
        )
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in DOI_WRAPPER_HOSTS:
        raise SourceNormalizationError(
            f"normalize_doi: unsupported DOI wrapper host {host!r}"
            f" (expected one of {sorted(DOI_WRAPPER_HOSTS)})"
        )
    path = parts.path.lstrip("/").rstrip("/")
    if not path:
        raise SourceNormalizationError(
            "normalize_doi: DOI wrapper URL carries no DOI path"
        )
    return path


def normalize_url(url: str) -> str:
    """Return the canonical form of an http(s) URL.

    See the module docstring for the normative readings (lowercase
    scheme/host, ``www.`` stripped, default ports stripped, trailing
    slashes stripped, fragment dropped, query parameters sorted).

    Raises:
        TypeError: ``url`` is not a ``str``.
        SourceNormalizationError: the value is not an absolute http(s)
            URL without userinfo, contains whitespace, or carries an
            invalid port.
    """
    if not isinstance(url, str):
        raise TypeError(f"normalize_url expects a str, got {type(url).__name__}")
    value = url.strip()
    if not value:
        raise SourceNormalizationError("normalize_url: empty URL")
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise SourceNormalizationError(
            "normalize_url: URL must not contain whitespace or control characters"
        )
    parts = urllib.parse.urlsplit(value)
    if parts.scheme == "":
        raise SourceNormalizationError(
            "normalize_url: expected an absolute http(s) URL (missing scheme)"
        )
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise SourceNormalizationError(
            f"normalize_url: unsupported scheme {parts.scheme!r}"
            " (expected http or https)"
        )
    if "@" in parts.netloc:
        raise SourceNormalizationError(
            "normalize_url: URL with userinfo is not a valid mirror locator"
        )
    hostname = (parts.hostname or "").lower()
    if not hostname:
        raise SourceNormalizationError("normalize_url: URL has no host")
    if hostname.startswith("www."):
        hostname = hostname[4:]
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceNormalizationError(
            f"normalize_url: invalid port in URL {value!r}"
        ) from exc
    default_port = 80 if scheme == "http" else 443
    if port is None or port == default_port:
        netloc = f"[{hostname}]" if ":" in hostname else hostname
    else:
        netloc = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
    path = parts.path.rstrip("/")
    query = "&".join(sorted(q for q in parts.query.split("&") if q))
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def normalize_stable_identifier(value: str) -> str:
    """Return the canonical form of a stable identifier.

    Outer whitespace is trimmed; the identifier content is preserved
    verbatim (stable identifiers are already canonical).

    Raises:
        TypeError: ``value`` is not a ``str``.
        SourceNormalizationError: the value is empty after trimming.
    """
    if not isinstance(value, str):
        raise TypeError(
            f"normalize_stable_identifier expects a str, got {type(value).__name__}"
        )
    normalized = value.strip()
    if not normalized:
        raise SourceNormalizationError(
            "normalize_stable_identifier: empty identifier"
        )
    return normalized


def is_mirror_collapsible(source_type: SourceType) -> bool:
    """Return True if records of ``source_type`` can collapse on identity.

    ``supplementary_information`` / ``dataset`` / ``structure_deposition``
    are record-scoped (AC-02) and never collapsible; every other frozen
    source type is.

    Raises:
        TypeError: ``source_type`` is not a ``SourceType``.
    """
    if not isinstance(source_type, SourceType):
        raise TypeError(
            f"is_mirror_collapsible expects a SourceType, got {type(source_type).__name__}"
        )
    return source_type not in RECORD_SCOPED_TYPES


class CanonicalIdentityKind(StrEnum):
    """Which identity dimension produced the canonical key."""

    DOI = "doi"
    STABLE_IDENTIFIER = "stable_identifier"
    URL = "url"
    RECORD = "record"


@dataclass(frozen=True)
class SourceIdentity:
    """Canonical identity of one source record (deterministic key + kind).

    ``key`` is the canonical key (``doi:<norm>``, ``stable_identifier:<norm>``,
    ``url:<norm>`` or ``record:<source_id>``) that deduplication groups by;
    ``kind`` names the dimension that produced it. The normalized components
    are retained for auditability: an identity always records *how* the key
    was derived (AC-03 spirit of the rules modules).
    """

    key: str
    kind: CanonicalIdentityKind
    normalized_doi: str | None = None
    normalized_stable_identifier: str | None = None
    normalized_url: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Plain dict of the identity in canonical field order."""
        return {
            "key": self.key,
            "kind": self.kind.value,
            "normalized_doi": self.normalized_doi,
            "normalized_stable_identifier": self.normalized_stable_identifier,
            "normalized_url": self.normalized_url,
        }


def canonical_identity(source: ResearchSource) -> SourceIdentity:
    """Derive the canonical identity of one source record.

    Deterministic and total for every well-formed ``ResearchSource`` (the
    frozen ``core.models.ResearchSource`` model): the identity is a pure
    function of the record's fields. See the module docstring for the
    normative readings (scoping, priority chain, URL fallback).

    Raises:
        TypeError: ``source`` is not a ``ResearchSource``.
        SourceNormalizationError: the record carries a DOI that cannot be
            normalized (a malformed DOI is a data-quality failure that is
            surfaced loudly, never silently converted to a weaker key).
    """
    if not isinstance(source, ResearchSource):
        raise TypeError(
            f"canonical_identity expects a ResearchSource, got {type(source).__name__}"
        )
    if not is_mirror_collapsible(source.source_type):
        # AC-02: SI/dataset/structure records keep their own address.
        return SourceIdentity(
            key=f"record:{source.source_id}",
            kind=CanonicalIdentityKind.RECORD,
        )
    if source.doi is not None:
        normalized = normalize_doi(source.doi)
        return SourceIdentity(
            key=f"doi:{normalized}",
            kind=CanonicalIdentityKind.DOI,
            normalized_doi=normalized,
        )
    if source.stable_identifier is not None and source.stable_identifier.strip():
        normalized = normalize_stable_identifier(source.stable_identifier)
        return SourceIdentity(
            key=f"stable_identifier:{normalized}",
            kind=CanonicalIdentityKind.STABLE_IDENTIFIER,
            normalized_stable_identifier=normalized,
        )
    if source.url_or_locator is not None:
        try:
            normalized = normalize_url(source.url_or_locator)
        except SourceNormalizationError:
            # Non-http(s) locators cannot be mirror-location evidence; the
            # record falls back to its own address (normative reading).
            return SourceIdentity(
                key=f"record:{source.source_id}",
                kind=CanonicalIdentityKind.RECORD,
            )
        return SourceIdentity(
            key=f"url:{normalized}",
            kind=CanonicalIdentityKind.URL,
            normalized_url=normalized,
        )
    return SourceIdentity(
        key=f"record:{source.source_id}",
        kind=CanonicalIdentityKind.RECORD,
    )
