# feat(rules): assumption effect evaluator for strict-reproduction labeling [DEV-M2-G07]

## Objective
Implement A0/A1/A2 classification effects on strict-reproduction labeling and audit.

## AC evidence
- **AC-01 (A0 does not change scientific strict identity):**
  `tests/rules/test_assumptions.py::test_assumptions_empty_set_keeps_pure_strict_label`,
  `test_assumptions_a0_only_keeps_pure_strict_label`,
  `test_assumptions_a0_only_label_is_identical_to_no_assumptions`,
  `test_assumptions_a0_effect_is_none_for_any_a0_entry` — assert an empty
  assumption set and an A0-only set both keep the pure-strict `STRICT` label
  (matched rules `R-STRICT-1` / `R-STRICT-4`) and that each A0 entry records
  `StrictStatusEffect.NONE` via `R-EFF-3`.
- **AC-02 (A1 is recorded and can classify strict-with-assumptions):**
  `test_assumptions_a1_classifies_strict_with_assumptions`,
  `test_assumptions_a1_is_recorded_in_the_result`,
  `test_assumptions_a0_plus_a1_classifies_strict_with_assumptions`,
  `test_assumptions_multiple_a1_still_strict_with_assumptions` — assert the
  label `STRICT_WITH_ASSUMPTIONS` (the exact frozen schema enum value,
  distinct from `STRICT`) and that the assessment carries back the exact A1
  assumption input with its recorded `STRICT_WITH_ASSUMPTIONS` effect and
  matched rule `R-EFF-2` / `R-STRICT-3`.
- **AC-03 (A2 prevents pure STRICT labeling):**
  `test_assumptions_a2_never_returns_pure_strict_label`,
  `test_assumptions_a2_is_recorded_with_its_effect`,
  `test_assumptions_any_a2_combination_never_pure_strict`,
  `test_assumptions_a2_dominates_a1`,
  `test_assumptions_a2_from_frozen_example_disqualifies` — assert the
  evaluator never returns the pure-strict label when an A2 is present (alone,
  with A0, with A1, or with both), that the result records the A2 assumption
  with its `DISQUALIFIES_PURE_STRICT` effect (`R-EFF-1`, `R-STRICT-2`), and
  that the frozen FDM-201 example assumption evaluates the same way.
- Determinism and auditability battery:
  `test_assumptions_same_input_yields_identical_assessment`,
  `test_assumptions_deterministic_across_repeated_evaluations`,
  `test_assumptions_equivalent_inputs_classify_identically`,
  `test_assumptions_label_is_order_independent`,
  `test_assumptions_grid_invariant_over_all_combinations`,
  `test_assumptions_every_assumption_set_produces_a_label`,
  `test_assumptions_assessment_records_exact_inputs`,
  `test_assumptions_effects_track_every_input_in_order`,
  `test_assumptions_label_decisions_record_every_rule`,
  `test_assumptions_matched_rule_is_the_first_true_predicate` — exhaustive
  battery over all A0/A1/A2 combinations (sizes 0-4, 121 inputs) proving the
  label bi-implication, totality, order-independence and full audit trail.

## Verification table
| command | result / exit code | notes |
|---|---|---|
| `python -m pytest -q tests/rules -k assumption` | PASS / 0 | 31 passed, 20 deselected |
| `python -m pytest -q tests/rules` | PASS / 0 | 51 passed |
| `python -m pytest -q` | PASS / 0 | 521 passed, 6 skipped, 1 deselected |
| `python -m ruff check .` | PASS / 0 | All checks passed |
| `python -m mypy src/` | PASS / 0 | Success: no issues found in 31 source files |

## Architecture impact
- Added `src/scientific_reproduction/core/rules/assumptions.py` — the
  assumption effect evaluator, following the established DEV-M2-G04
  criticality.py pattern: versioned rule tables (`ASSUMPTION_EFFECT_RULES`,
  `STRICT_LABEL_RULES`), frozen dataclass model (`StrictLabelAssessment`,
  effect/label rule decisions), pure total functions
  (`assumption_effect`, `evaluate_strict_label`), no LLM/randomness/clock.
  Vocabulary is the frozen schema/model enums: `AssumptionClassification`
  (A0/A1/A2) and `StrictStatusEffect` (NONE / STRICT_WITH_ASSUMPTIONS /
  DISQUALIFIES_PURE_STRICT); `StrictLabel.STRICT_WITH_ASSUMPTIONS` is the
  exact frozen value. A2 dominates A1 by rule-table ordering (R-STRICT-2
  before R-STRICT-3). Works as a PEP 420 namespace-package member —
  `core/rules/__init__.py` is intentionally left absent (owned by DEV-M2-G01).
- Added `tests/rules/test_assumptions.py` — exhaustive acceptance battery
  (31 tests) for the three ACs plus determinism and auditability.

## Known limitations
- The evaluator derives an assumption's strict-status effect from its frozen
  `classification`; a recorded `strict_status_effect` field on an Assumption
  input is carried in the audit trail but not trusted for the derivation.
- The label domain only distinguishes the three levels (STRICT /
  STRICT_WITH_ASSUMPTIONS / NOT_STRICT); an A2-affected reproduction is
  labeled NOT_STRICT, with the disqualifying A2 entries and their effects
  recorded for audit.
- `tests/state/test_leases.py::test_concurrent_takeover_of_expired_lease_single_winner`
  flaked once in a full-suite run on Windows (timing-dependent threading
  test, passes 5/5 in isolation and in the final full run); unrelated to this
  change (only new files added).

## Worker declaration
Conventional commit `feat(rules): assumption effect evaluator for strict-reproduction labeling [DEV-M2-G07]`;
no scope changes; no tests deleted; no AC modifications; no force-push; no self-approval; no merge.
