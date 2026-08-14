# FDM-201 benchmark Goals DAG and 100% mapping audit (DEV-M12-G03)

Frozen benchmark Goals DAG, the 100% reported-item mapping audit, and the
deterministic offline audit gate for the FDM-201 reference reproduction
benchmark (`17-FDM201-REFERENCE-CASE.md`, DOI `10.1039/D5TA00771B`).

## Layout

- `goals.yaml` — the machine-readable Goals DAG: every goal the FDM-201
  benchmark will execute (`GOAL-RES-001..004`, `GOAL-INV-001..004`,
  `GOAL-AUD-001`, `GOAL-EXE-10..90`), with purpose, acceptance criteria,
  dependencies (hard/soft/informational with execution and acceptance
  gates), resolved inventory items, resolved ambiguities, and the
  `wp_alias` / `wp_realization` tables that make every
  `requirement_mapping` value of `../inventory/INVENTORY.yaml` resolve to
  a goal defined here.
- `mapping_audit.yaml` — the GOAL-AUD-001 anticipation: the 100% mapping
  audit at Plan v1 freeze (82/82 items mapped, 0 unmapped, 14/14
  ambiguities assigned to resolution goals, 0 unresolved; all formal
  unavailability handled per spec, never ignored).
- `audit_check.py` — deterministic, offline self-check: asserts the audit
  numbers, resolves every inventory `requirement_mapping` / `resolves_in`
  value against the DAG, checks DAG acyclicity, and validates the Plan v1
  object references. Run: `python benchmarks/fdm201/goals/audit_check.py`.
  Exits 0 on pass.
- `README.md` — this file.

## Policy

- **The inventory is the source of truth** for the 82 reported items
  (`../inventory/INVENTORY.yaml`, DEV-M12-G02): this audit never drops or
  renames an item; every item is mapped exactly once, to goal ids that
  resolve from the inventory's own `requirement_mapping` (via the
  `wp_alias` table) plus the inventory's `resolves_in` goals.
- **Registration-only** (inherited from DEV-M12-G01): no paper/SI/CIF
  content is downloaded, embedded or mirrored; every reported number stays
  `REPORTED-NON-FINAL` until revalidated against the primary source during
  benchmark execution.
- **Determinism**: fixed timestamp `2026-08-14T00:00:00Z`; no wall clock,
  no randomness, no network at runtime; no placeholder text anywhere —
  genuinely unknown values carry a reason and a resolution goal.
- **Zero unresolved ambiguity at freeze** (spec section 4 WP-00
  GOAL-AUD-001): at Plan v1 freeze every one of the 14 ambiguity entries
  (`AMB-0001..0014`) is assigned to the goal(s) that resolve it; the
  execution-phase re-run of GOAL-AUD-001 (after GOAL-INV-001..004 populate
  the inventory from the primary source) applies the same gate to every
  later plan freeze.

## DAG summary

The DAG has 20 goals: 4 source-acquisition goals (`GOAL-RES-001..004`),
4 inventory goals (`GOAL-INV-001..004`), 1 completeness audit goal
(`GOAL-AUD-001`), and 11 execution goals (`GOAL-EXE-10/20/30/40/50/60/70/
80/81/82/90`, one per spec section 4 work package). It is acyclic and
ordered so that:

1. acquisition and inventory precede the audit: `GOAL-RES-*` -> `GOAL-INV-*`
   -> `GOAL-AUD-001`;
2. the experiment branch runs `GOAL-EXE-10` (ligand) -> `GOAL-EXE-20`
   (synthesis/activation) -> `GOAL-EXE-30` (structure) -> `GOAL-EXE-40`
   (porosity) -> `GOAL-EXE-50` (isotherms) -> `GOAL-EXE-60` (derived
   thermodynamics), with `GOAL-EXE-70` (breakthrough) parallel to the
   isotherm chain once samples and the reported conditions exist;
3. the computation branch (`GOAL-EXE-80` -> `GOAL-EXE-81/82`) depends only
   on the CIF and the inventoried settings, so it can run independently of
   wet-lab completion (spec section 5);
4. `GOAL-EXE-90` (final integration) closes after every goal.

See `goals.yaml` (per-goal `dependencies`) for the exact edges and gate
semantics, and `mapping_audit.yaml` for the 82-item coverage table.

## Relationship to other milestones

- DEV-M12-G04 generates execution packages **from the frozen Goals in
  `goals.yaml`** (`benchmarks/fdm201/execution_packages/**`).
- The completeness-audit verdict vocabulary and rule table
  (`R-AUD-U1/A1/P1`) of `src/scientific_reproduction/planning/audit.py`
  (DEV-M4-G03) apply to every Plan freeze; `mapping_audit.yaml` is the
  Plan v1 instantiation of that gate.
- The frozen Plan v1 lives in `../plans/plan_v1.yaml` and references this
  directory (`goals.yaml`, `mapping_audit.yaml`, `audit_check.py`).
