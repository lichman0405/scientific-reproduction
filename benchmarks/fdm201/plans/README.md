# FDM-201 Plan v1 (DEV-M12-G03)

Frozen reproduction plan for the FDM-201 reference reproduction benchmark
(`17-FDM201-REFERENCE-CASE.md`, DOI `10.1039/D5TA00771B`): Plan v1, frozen at
the fixed timestamp `2026-08-14T00:00:00Z`.

## Layout

- `plan_v1.yaml` — the frozen plan: plan identity and versioning, the
  `inventory_audit` block (82/82 mapped, 0 unmapped, 0 unresolved ambiguity,
  coverage 1.0, status PASS), the 20 goal ids and 82 requirement ids, work
  packages WP-00..WP-90, the experiment branch (WP-10/20..70) and the
  computation branch (WP-80..82), resource/assumption/evidence/analysis/
  closure object sections (AC-03), and the change-control section.
- `assumptions.yaml` — 46 assumption objects: the 12 inventory A2
  registrations (WP-70 parameters INV-0511..0521, computational settings
  INV-0605) with `strict_status_effect: DISQUALIFIES_PURE_STRICT`; the 19
  ambiguity-coverage references (18 inventory `self_check.py` warnings plus 1
  documented coverage extension ASM-CVR-AMB-0007-INV-0611, A1); and
  15 Plan v1 methodological defaults (A1: fit models, BET criteria, pore
  kernel, Qst protocol, force-field/charges candidates, convergence criteria,
  minimum-n replication policy, 8 acceptance tolerance bands).
- `analysis_plan.yaml` — 10 frozen analysis protocols (ANL-001 revalidation;
  ANL-010 batch reproducibility; ANL-020 PXRD identity; ANL-030 isotherms;
  ANL-040 IAST/Qst; ANL-050 breakthrough; ANL-060 model checklist; ANL-061
  binding energies; ANL-062 GCMC; ANL-090 statistics/acceptance design).
- `closure.yaml` — 4 closure contracts (CC-BOOTSTRAP, CC-EXPERIMENT,
  CC-COMPUTATION, CC-INTEGRATION) per `schemas/closure-contract.schema.yaml`,
  including the whole-benchmark closure criteria.
- `README.md` — this file.

## Branch structure (AC-03)

- **Experiment branch** — WP-10 (ligand chemistry) and WP-20 (FDM-201
  synthesis + activation) -> WP-30 (structure/identity verification) ->
  WP-40 (porosity) -> WP-50 (single-component isotherms) -> WP-60 (derived
  thermodynamics/IAST/Qst); WP-70 (breakthrough) runs in parallel once
  samples exist. Acceptance gates on sample identity (BET criterion) per the
  spec's example work packages.
- **Computation branch** — WP-80 (model construction from the CIF) ->
  WP-81 (binding sites/energies) and WP-82 (GCMC), independent of the wet
  lab (spec section 5).
- WP-90 (final integration) hard-gates on all execution goals plus the
  GOAL-AUD-001 audit.

## Determinism, numbers and honesty

- Fixed timestamp `2026-08-14T00:00:00Z`; no wall clock, randomness or
  network at runtime; no placeholder text.
- Every comparison target is traceable to the spec section 2 seed facts
  (REPORTED-NON-FINAL until revalidated against the primary source by
  GOAL-INV-001/002 per ANL-001): BET ~1965 m2/g; pores ~7.9 / ~13.0 A; 298 K
  / 1 bar uptakes C3H6 180.5 / C2H4 68.2 cm3/g; IAST selectivity ~8.6 (298 K
  / 100 kPa, 50/50); Qst 27.1 / 19.1 kJ/mol; binding energies 48.72 / 38.61
  kJ/mol (difference 10.11 derived); breakthrough C2H4 ~23.5 / C3H6 ~64.8
  min, window ~41.3 min; the 328 K diluted run. Nothing else is a number —
  every tolerance band is a frozen A1 assumption (ASM-A1-TOL-01..08).
- Unavailability is formal, never ignored: 65 DEFERRED-TO-EXECUTION items
  and 3 UNAVAILABLE acquisition-log items all carry resolution goals
  (`../goals/mapping_audit.yaml`).
- `frozen_commit: null` is intentional: the freeze marker is the fixed
  timestamp; the commit SHA is recorded in the change-control ledger at
  merge/review time.

## Verification

`../goals/audit_check.py` is the deterministic offline gate for this
milestone (mapping 82/82, ambiguities 14/14, DAG acyclicity, assumption/
evidence/resource counts, placeholder scan). The goal verification command
`python -m pytest -q tests/benchmarks -k "fdm201 and plan"` is VACUOUS at
this milestone: `tests/benchmarks/` is owned by the frozen goal DEV-M12-G07
and does not exist yet (pytest exits 4); no test files are created here.
