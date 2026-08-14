# FDM-201 source set (DEV-M12-G01)

Registered source set for the FDM-201 reference reproduction benchmark
(17-FDM201-REFERENCE-CASE.md, DOI `10.1039/D5TA00771B`).

## Layout

- `ACQUISITION_LOG.yaml` — the 9-item acquisition log (required bootstrap
  items of spec 17 section 3), one entry per item: `item`, `required_item`,
  `status` (`REGISTERED` / `PARTIAL` / `UNAVAILABLE`), `source_ids`,
  `established`, and `unavailability_reason` where applicable.
- `records/` — one YAML file per source record. Each file is exactly the
  serialization of a frozen `scientific_reproduction.core.models.ResearchSource`
  (field order follows `to_dict()`; `None` optional fields are omitted), with
  ids from `core.ids.generate_id` and identity fields normalized through
  `scientific_reproduction.research` (`normalize_doi`, `normalize_url`,
  `canonical_identity`). File names carry the required-item number (e.g.
  `06a` / `06b` both serve item 6).

## Policies

- **Registration only**: no paper/SI/CIF content is downloaded into the
  repository; publisher content is never redistributed. Records carry the
  authoritative identity (DOI / stable identifier / URL) and the availability
  status.
- **Determinism**: fixed timestamp `2026-08-14T00:00:00Z`, no wall clock, no
  randomness, no network at runtime; every record round-trips through the
  real research APIs.
- **Honesty**: where a source is not accessible to this benchmark at
  registration (no CSD/CCDC deposition located, data-availability statement
  behind a bot-protected page, in-paper citation lists unreadable under the
  no-download policy), the acquisition log records an explicit
  `UNAVAILABLE`/`PARTIAL` entry with the reason. Re-verification and full
  inventory happen during benchmark execution (DEV-M12-G02+), never here.

## Relationship to the benchmark project record

The benchmark project record lives at `../project/project.yaml` and points
at this source set (inventory summary per status, acquisition log path,
record count).
