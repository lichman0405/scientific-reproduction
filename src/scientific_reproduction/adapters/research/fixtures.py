"""Deterministic offline fixtures for public research adapters (DEV-M5-G06).

Pure data: module-level constants built from the frozen
:class:`AdapterRawRecord` DTO -- no wall-clock, no randomness, no
network. Fixture resolution is a pure lookup, which is what makes the
public adapter contract testable without a live network (AC-03).

Grounded in:
* ``09-RESEARCH-SUBSYSTEM.md`` section 4 -- public/open source examples:
  DOI/publisher public pages, Crossref/OpenAlex-like metadata services,
  public repositories, public crystallographic/materials databases,
  public standards/manuals where legally accessible.
* ``17-FDM201-REFERENCE-CASE.md`` -- the v0.1 benchmark reference case;
  the primary paper's DOI ``10.1039/D5TA00771B`` is used as the
  canonical example and the mock titles are shaped around that case.

Every record is **mock data** that mimics the shape of a real service
response; the primary-paper title/DOI mirror the reference case, all
other titles and identifiers are invented fixtures for contract testing
and are not citations of real records.
"""

from __future__ import annotations

from scientific_reproduction.adapters.research.base import AdapterRawRecord
from scientific_reproduction.core.models import SourceType

__all__ = [
    "FIXTURE_VERSION",
    "PUBLIC_SOURCE_FIXTURES",
]

#: Version of the fixture sets. Bumped whenever a fixture record
#: changes; fixture content and version are stable contract inputs.
FIXTURE_VERSION: str = "1.0"

#: Crossref/OpenAlex-like public metadata service records
#: (09-RESEARCH-SUBSYSTEM.md section 4: "Crossref/OpenAlex-like metadata
#: services"; 17-FDM201-REFERENCE-CASE.md primary paper).
CROSSREF_OPENALEX_FIXTURES: tuple[AdapterRawRecord, ...] = (
    AdapterRawRecord(
        title=(
            "A highly connected metal-organic framework with stretched "
            "inorganic units for propylene/ethylene separation"
        ),
        source_type=SourceType.TARGET_PAPER,
        doi="10.1039/D5TA00771B",
        url_or_locator="https://doi.org/10.1039/D5TA00771B",
        publication_year=2025,
    ),
    AdapterRawRecord(
        title="Adsorption equilibria of light olefins on metal-organic frameworks",
        source_type=SourceType.PEER_REVIEWED_PAPER,
        # "doi:" prefix is a valid recording form the adapter must
        # normalize away (research/sources.py accepted input forms).
        doi="DOI: 10.1016/j.ces.2024.120001",
        url_or_locator="https://doi.org/10.1016/j.ces.2024.120001",
        publication_year=2024,
    ),
    AdapterRawRecord(
        title="Supplementary information for the FDM-201 primary paper",
        source_type=SourceType.SUPPLEMENTARY_INFORMATION,
        url_or_locator=(
            "https://pubs.rsc.org/en/content/articlelanding/2025/ta/"
            "d5ta00771b#si"
        ),
        publication_year=2025,
    ),
)

#: Public repository records (09-RESEARCH-SUBSYSTEM.md section 4:
#: "public repositories"; arXiv-like preprint and figshare-like dataset).
PUBLIC_REPOSITORY_FIXTURES: tuple[AdapterRawRecord, ...] = (
    AdapterRawRecord(
        title="Reproduction notes for the FDM-201 benchmark case",
        source_type=SourceType.PREPRINT,
        stable_identifier="arXiv:2406.12345",
        url_or_locator="https://arxiv.org/abs/2406.12345",
        publication_year=2024,
    ),
    AdapterRawRecord(
        title="FDM-201 single-component isotherm dataset",
        source_type=SourceType.DATASET,
        stable_identifier="10.6084/m9.figshare.25000001",
        # "www." host, default port and trailing slash must be
        # normalized away by the adapter.
        url_or_locator=(
            "https://www.figshare.com:443/articles/"
            "FDM-201-isotherms/25000001/"
        ),
        publication_year=2024,
    ),
)

#: Public crystallographic/materials database records
#: (09-RESEARCH-SUBSYSTEM.md section 4: "public crystallographic/
#: materials databases"; COD-like service).
CRYSTALLOGRAPHIC_DATABASE_FIXTURES: tuple[AdapterRawRecord, ...] = (
    AdapterRawRecord(
        title="FDM-201 crystal structure deposit (CIF)",
        source_type=SourceType.STRUCTURE_DEPOSITION,
        stable_identifier="COD 2110001",
        url_or_locator="http://www.crystallography.net/cod/2110001.html",
        publication_year=2025,
    ),
    AdapterRawRecord(
        title="FDM-201 public database record",
        source_type=SourceType.DATABASE_RECORD,
        doi="10.9999/cod.2110001",
        url_or_locator="http://crystallography.net/cod/2110001.html",
        publication_year=2025,
    ),
)

#: adapter_id -> deterministic fixture records of that public adapter.
#: Key order is normative (registry/description order); lookup is
#: content-independent and repeatable.
PUBLIC_SOURCE_FIXTURES: dict[str, tuple[AdapterRawRecord, ...]] = {
    "crossref_openalex": CROSSREF_OPENALEX_FIXTURES,
    "public_repository": PUBLIC_REPOSITORY_FIXTURES,
    "crystallographic_database": CRYSTALLOGRAPHIC_DATABASE_FIXTURES,
}
