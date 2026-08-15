# ADR-0001 — Statistical design as a first-class record

- **Status:** proposed <!-- proposed | accepted | superseded -->
- **Date:** 2026-08-15
- **Reviewer:** supervisor (independent PR reviewer)
- **Related PR:** none (reviewed on `feat/statistical-design-record`)
- **Supersedes:** none
- **Superseded by:** none

## Context

`schemas/acceptance-criteria.schema.yaml` declares `statistical_design_ref`
as a free-form string with no target record type: no schema, registry, or
model exists for statistical designs, so `07-STATISTICS-AND-ACCEPTANCE.md`
SS9's freeze mandate (metrics, margin, replication design, primary method,
alpha, preprocessing/exclusion and outlier rules, failed-Run handling) is
unenforceable, the FDM-201 example's `STAT-ADS-C3H6-298K-001` reference is
dangling, and the margin-basis provenance required by SS8 cannot be checked
by machine. The goal-contract family registry (`planning/plan.py`) already
gives every other frozen contract (Goal, Acceptance, Analysis, Closure) a
frozen schema plus an immutable no-clobber registry; statistical designs
were the only SS9 artifact without one.

## Decision

Statistical design becomes a first-class goal-contract record:

- a frozen schema `schemas/statistical-design.schema.yaml`
  (one `StatisticalDesign` record per goal; the SS8 margin-basis categories
  are the enum vocabulary of `margin_basis`);
- a `StatisticalDesign` `CoreModel` registered in `MODEL_REGISTRY` /
  `KNOWN_OBJECT_TYPES`, with `register_statistical_design` /
  `read_statistical_design` / `list_statistical_designs` in the goal-contract
  family registry (`<root>/designs/<design_id>.json`, immutable no-clobber
  `DuplicateStatisticalDesignError`);
- `AcceptanceCriteria.statistical_design_ref` keeps its free-string form
  (consistent with `criteria_ref` / `analysis_protocol_ref` /
  `closure_contract_ref`) but now resolves to a registered design:
  `planning.freeze` resolves every non-null reference during the plan-freeze
  completeness check and freezes the design records together with the rest
  of the goal-contract family.

This does not deviate from any locked decision; it makes the existing SS8/SS9
normative requirements machine-checkable. References remain optional, so
existing acceptance records without a `statistical_design_ref` are
unaffected.

## Consequences

Positive:

- a statistical design is validated before persistence and queryable
  (`list_statistical_designs`), so freeze-time margin/alpha/replication
  provenance is auditable (SS8/SS9);
- the dangling FDM-201 reference is resolved by a real example record;
- plan freeze now fails loudly when an acceptance names a design that was
  never registered, instead of freezing an unvalidated design.

Negative / trade-offs:

- a new record kind must be kept aligned with the SS9 element list if that
  section evolves (schema and spec text must move together);
- acceptances that do reference a design must be registered after it, or the
  plan-freeze completeness check fails.

## Verification notes

- `tests/planning/test_freeze.py` covers: unresolved `statistical_design_ref`
  blocks freeze with `UnresolvedContractReferenceError`; registered designs
  freeze with the family (`version` promoted, `frozen: true`); design
  registry no-clobber; frozen design records reject mutation.
- `tests/core/test_models.py`, `tests/core/test_schema_validation.py`, and
  `tests/core/fixtures.py` cover registry count (22), schema validation, and
  example-document validation of `examples/fdm-201/statistical-design.example.yaml`.
