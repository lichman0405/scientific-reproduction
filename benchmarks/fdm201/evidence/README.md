# FDM-201 Plan v1 evidence records (DEV-M12-G03)

Pre-execution evidence records for the FDM-201 reference reproduction
benchmark (`17-FDM201-REFERENCE-CASE.md`, DOI `10.1039/D5TA00771B`), frozen
with Plan v1.

## Layout

- `evidence_model.yaml` — the frozen evidence model: the four axes
  (authority / reliability / directness, 0-4), the basis-to-A/R/D mapping
  (`CROSSREF-METADATA` -> A4/R4/D4, `ACQUISITION-LOG` -> A3/R3/D4,
  `REFERENCE-CASE-SPEC` -> A2/R3/D4, `PUBLIC-SEARCH` -> A2/R2/D3), what
  counts as evidence per item class, and recording conventions.
- `evidence_records.yaml` — one pre-execution record per formally reported
  inventory item: 82 records `EV-INV-xxxx`, each with `source_id` (the G01
  `sr_source_` id from the item's provenance), `claim_id` (= item id),
  `source_location`, `finding`, `limitations`, the A/R/D `assessment`, the
  `used_by` goals (from `../goals/mapping_audit.yaml` mapped_to), and the
  `expected_artifacts` the mapped goals must produce during execution.
- `README.md` — this file.

## Relationship to the plan and the evidence system

- Record vocabulary follows `schemas/evidence.schema.yaml` and the evidence
  model of `06-EVIDENCE-SYSTEM.md` (evidence as Source x Claim; claims here
  are the 82 inventory items).
- The paper text, SI and CIF are not downloaded (registration-only policy);
  the pre-execution assessment is therefore derived deterministically from
  the inventory `evidence_basis_enum`. During execution the mapped goals
  produce the `expected_artifacts` (raw data, analysis records per
  `../plans/analysis_plan.yaml`, comparison verdicts), which upgrade the
  assessment on the same record.
- Recovery eligibility (R >= 3, D >= 2, scientifically actionable) is
  evaluated per `06-EVIDENCE-SYSTEM.md` section 4; the closure contracts in
  `../plans/closure.yaml` apply it per goal class.
- Every number in a record comes verbatim from the inventory (seed facts or
  G01 log) — no invented numbers.

## Integrity

`../goals/audit_check.py` asserts: 82 records, ids `EV-INV-xxxx` matching
the 82 inventory items one-to-one, every `claim_id` exists in the inventory,
every `source_id` is a G01 record id, `used_by` goals exist in
`../goals/goals.yaml`, and the A/R/D values match the frozen basis mapping of
`evidence_model.yaml`.
