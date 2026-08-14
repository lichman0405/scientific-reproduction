# FDM-201 reported-item inventory (DEV-M12-G02)

Formal inventory of every reported experiment / control / computation of the
FDM-201 reference reproduction benchmark (`17-FDM201-REFERENCE-CASE.md`, DOI
`10.1039/D5TA00771B`) that is discoverable from the source material registered
by DEV-M12-G01.

## Layout

- `INVENTORY.yaml` — the inventory: 82 reported items across 8 categories,
  14 explicit ambiguity entries (AC-03), a G01 acquisition-log cross-reference,
  and a summary/integrity block.
- `self_check.py` — deterministic, offline integrity gate: loads
  `INVENTORY.yaml`, validates source-id references against
  `../sources/records/*.yaml`, enum membership, summary counts, placeholder
  scan, ambiguity references, and G01-log consistency. Run:
  `python benchmarks/fdm201/inventory/self_check.py`.
- `README.md` — this file.

## Evidence model

The paper text, SI and CIF are **not** downloaded into the repository
(registration-only policy of DEV-M12-G01; the benchmark must not redistribute
publisher content). The inventory is therefore built from what IS accessible:

1. the source records and acquisition log under `../sources/` (identities,
   availability statuses, ESI locator);
2. the Crossref-verified publication metadata recorded there;
3. the seed facts of spec section 2 and the candidate unit processes of spec
   section 4 — which the spec itself marks as "seed facts to verify, not a
   substitute for source acquisition".

Every item carries provenance: the exact `sr_source_` id(s) of the record(s)
in `../sources/records/*.yaml`, the specific part (Crossref record /
acquisition-log item / spec section 2 seed fact / spec section 4 work package),
and an evidence basis (`CROSSREF-METADATA` / `REFERENCE-CASE-SPEC` /
`ACQUISITION-LOG` / `PUBLIC-SEARCH`).

## Status semantics

| Status | Meaning |
|---|---|
| `INVENTORIED` | Existence/identity established from accessible material (Crossref metadata, acquisition log). No primary-source reading needed. |
| `INVENTORIED-DEFERRED` | Existence and reported value known from accessible material (spec section 2 seed facts, or the G01 acquisition log / published abstract). The primary source text is not in the repository; the value is revalidated against the primary source during benchmark execution. All reported numbers here are tagged `seed_fact_sourced` / `value_status: REPORTED-NON-FINAL`. |
| `DEFERRED-TO-EXECUTION` | Existence implied by the spec (section 3 required bootstrap items or section 4 candidate unit processes / required parameter sets), but values or existence require reading the paper/SI, which is not in the repository. Always carries `missing_reason` and/or `resolves_in` (the execution-phase goal that populates it). |

`value_status` values: `ESTABLISHED` (identity/locator facts), `REPORTED-NON-FINAL`
(reported numbers pending revalidation), `EXISTENCE-ONLY` (the item exists per
accessible material but no value is available), `MISSING` (parameter value not
in accessible material, with reason), `NOT-APPLICABLE` (non-quantitative item).

## Category organization

Coverage is checkable per reference-case category:

| Category | Spec anchor | Items |
|---|---|---|
| `00` publication identity and source availability | spec section 3 (G01 cross-reference) | 10 |
| `a` structure and identity characterization | WP-30 (SCXRD / PXRD / TGA / composition) | 9 |
| `b` porosity | WP-40 (N2 isotherm, BET, pore size) | 7 |
| `c` single-component adsorption isotherms | WP-50 (per gas / temperature) | 8 |
| `d` derived thermodynamic / selectivity items | WP-60 / WP-81 (IAST, Qst, binding energies) | 9 |
| `e` breakthrough experiments | WP-70 (per composition / temperature / cycle) | 14 |
| `f` computational items | WP-80 / WP-81 / WP-82 (CIF, GCMC, binding sites) | 11 |
| `g` synthesis and activation | WP-10 / WP-20 | 14 |

## WP-70 pre-execution parameter policy

The mandatory pre-execution breakthrough parameter set (column geometry,
adsorbent mass, packing density, flow rate, gas composition/purity,
temperature/pressure, detector calibration, dead-volume correction,
regeneration protocol, cycling protocol) is inventoried item by item
(INV-0511..0521). Each parameter is either inventoried with the value known
from accessible material (gas composition and temperatures for the 298 K run,
from the section 2 seed facts) or registered `value_status: MISSING` with
`assumption_class: A2` and a reason — per spec WP-70, "any missing
scientifically consequential parameter is registered as A2 and prevents pure
strict classification".

## Ambiguity policy (AC-03)

Every ambiguity whose parameter values, conditions, or very existence require
primary-source reading that is unavailable at registration is listed in the
`ambiguities` section (`AMB-0001..0014`), each with an `ambiguity_note`, the
affected item ids, and the execution-phase goal that resolves it
(`GOAL-INV-001..004`, `GOAL-RES-003/004`, `WP-60/70/80/81/82`). Nothing is
silently omitted.

## Requirement mapping

Every item maps to its candidate work-package / goal ids from spec section 4
(`GOAL-RES-001..004`, `GOAL-INV-001..004`, `GOAL-AUD-001`, `WP-10..WP-90`) in
`requirement_mapping`. The spec's GOAL-AUD-001 completeness audit (100% mapped,
zero unresolved ambiguity before Plan freeze) applies to the whole inventory.

## Determinism and honesty

- Fixed timestamp `2026-08-14T00:00:00Z`; no wall clock, no randomness, no
  network at runtime (data files and an offline checker only).
- No unresolved marker text anywhere (no unfinished-value tokens and no filler
  values); genuinely unknown fields carry `missing_reason` describing exactly
  what is unknown and why.
- Every reported number is tagged `seed_fact_sourced` (spec section 2) or
  `REPORTED-NON-FINAL` (abstract-level via the acquisition log) and is
  NON-FINAL until revalidated against the primary source during benchmark
  execution.

## Relationship to DEV-M12-G01 and later goals

- The inventory is consistent with `../sources/ACQUISITION_LOG.yaml` (see
  `g01_cross_reference`): the acquisition-log status describes SOURCE
  AVAILABILITY at registration; the inventory status describes the EVIDENCE
  STATE of the reported item. They are related but not identical — e.g. the
  CIF source is UNAVAILABLE while the reported structure itself is inventoried
  at abstract level (INV-0102) with the deposition deferred (INV-0004).
- During benchmark execution, GOAL-INV-001..004 populate the deferred items
  from the fetched paper/SI, GOAL-RES-003 re-verifies the CIF, and
  GOAL-AUD-001 audits 100% mapping with zero unresolved ambiguity before the
  Plan freeze. Later milestones must extend `INVENTORY.yaml` (never silently
  drop items) and must keep `summary` and `self_check.py` in sync.
