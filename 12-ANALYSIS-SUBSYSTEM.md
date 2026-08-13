# 12 — Analysis and Diagnosis Subsystem

## 1. Separation of execution and analysis

Execution Workers produce raw data and execution metadata. Analysis Workers independently process those data.

This prevents the executor from silently optimizing interpretation to make its own Run appear successful.

## 2. Analysis profiles

Use one dynamic Analysis Worker class with explicit profiles:

- `ROUTINE_ANALYSIS`
- `STATISTICAL_VALIDATION`
- `FAILURE_DIAGNOSIS`

Additional profiles may be added by domain packs.

## 3. Primary Analysis Protocol

Before data generation, freeze the analysis protocol where applicable:

- preprocessing;
- exclusions;
- fitting rules;
- model equations;
- pressure/range selection criteria;
- integration windows;
- uncertainty method;
- statistical model;
- equivalence margin;
- convergence checks;
- stochastic sampling analysis.

## 4. Exploratory analysis

Allowed only if labeled `EXPLORATORY_ANALYSIS`. It must never overwrite or replace the Primary Analysis result. A Supervisor may later version the formal protocol for future Runs, but past Primary Analysis remains preserved.

## 5. Analysis Result Package

Must include:

- input artifact IDs/checksums;
- analysis protocol ID/version/hash;
- software/environment;
- QC findings;
- derived metrics;
- uncertainty/statistics;
- plots/tables artifacts;
- warnings/assumptions;
- primary/exploratory label;
- reproducible scripts/notebooks where appropriate.

## 6. Diagnosis

Diagnosis Worker reports:

- observed abnormal patterns;
- whether anomalies are consistent across Runs;
- candidate causes supported by current evidence;
- evidence strength for each candidate;
- contradictory evidence;
- unresolved missing information.

Diagnosis Worker must not produce the formal Recovery Plan. Supervisor integrates diagnosis with Research and then decides.
