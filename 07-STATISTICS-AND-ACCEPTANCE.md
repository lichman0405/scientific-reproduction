# 07 — Statistics and Acceptance

## 1. Non-negotiable principle

A result is not reproduced merely because one observed value is numerically close to the published value.

Acceptance must be based on a pre-frozen scientific/statistical criterion and adequate repeated evidence.

## 2. Independent replication

Experimental Goals require independent replicates by default.

Default floor: `n >= 3`, but the Supervisor must dynamically determine the final sample size using:

- expected variability;
- required confidence/precision;
- equivalence margin;
- power/sample-size logic where applicable;
- experimental cost/feasibility;
- prior evidence;
- domain-specific guidance.

Technical replicates and instrument repeats are additional evidence but cannot replace independent replication.

## 3. Preferred acceptance logic

For continuous quantitative results, prefer methods that directly address equivalence or bounded difference rather than using “no significant difference” as evidence of reproduction.

Depending on the Goal, the frozen analysis may use:

- confidence intervals;
- effect sizes;
- equivalence tests (e.g. TOST);
- bootstrap confidence intervals;
- hierarchical/random-effects models for batch variability;
- robust methods for non-normal data;
- uncertainty propagation;
- measurement uncertainty.

## 4. Forbidden shortcut

`p > 0.05` from a difference test is not sufficient evidence that two results are equivalent.

## 5. Three-way result state

Acceptance systems must support:

- `PASS`
- `FAIL`
- `INCONCLUSIVE`

Example for a quantitative Goal:

- uncertainty interval sufficiently inside pre-defined equivalence region -> PASS;
- uncertainty interval sufficiently outside equivalence region / evidence of non-equivalence -> FAIL;
- interval overlaps decision boundaries or evidence is insufficient -> INCONCLUSIVE.

## 6. Binary/process success Goals

Some experimental processes are naturally success/failure at the Run level (e.g. successful target phase synthesis). Do not infer reproducibility from three successes/failures alone without considering uncertainty in process success rate.

A domain-specific model may use binomial/Beta-Binomial or another appropriate repeated-success model. The Supervisor should dynamically add Runs if the success-rate interval is too wide for the frozen decision rule.

## 7. Computational Goals

Do not force wet-lab statistics onto deterministic/numerical computations. Use appropriate validation such as:

- numerical convergence;
- finite-size sensitivity;
- basis/cutoff/k-point convergence;
- stochastic sampling error for Monte Carlo;
- block averaging/autocorrelation analysis;
- replicate seeds when stochastic;
- model/force-field sensitivity when required by the frozen protocol.

## 8. Acceptance criterion provenance

Every numeric margin or decision threshold must record its basis:

- target paper error/variation;
- independent reproduction literature;
- standard method/instrument uncertainty;
- domain-specific accepted threshold;
- explicit scientific equivalence judgment with documented rationale.

The basis category is recorded machine-checkably in the frozen statistical
design record (SS9) as `margin_basis` — the five SS8 categories are the
enum vocabulary of `schemas/statistical-design.schema.yaml`
(`target_paper_error` / `reproduction_literature` /
`instrument_uncertainty` / `domain_threshold` / `scientific_judgment`).

No global “±10% for everything” rule is allowed.

## 9. Freeze requirement

Before data generation, freeze:

- target metric(s);
- equivalence margin/acceptance region;
- replication design;
- primary statistical method;
- alpha/confidence level if relevant;
- preprocessing/exclusion criteria;
- outlier rules;
- handling of failed/invalid Runs.

The frozen design is a **first-class record**: one
`StatisticalDesign` record per goal, validated against the frozen
`schema` `schemas/statistical-design.schema.yaml` and registered through
the goal-contract family registry at `designs/<design_id>.json`
(`planning.plan.register_statistical_design`; per-object state files,
`14-STATE-GIT-ARTIFACTS.md` SS3). The acceptance criteria of the Goal
reference it via `statistical_design_ref`
(`schemas/acceptance-criteria.schema.yaml`); the plan freeze requires
every such reference to resolve to a registered design record and freezes
the design together with the rest of the goal-contract family
(`planning.freeze`), so a statistical design is never unvalidated or
unqueryable.

Changes after data are observed require a versioned Supervisor decision and cannot retroactively erase the original Primary Analysis.
