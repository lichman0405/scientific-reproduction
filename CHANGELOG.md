# Changelog

All notable changes are tracked here. This repository follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions with a single immutable release line per version.

## [0.1.0] - 2026-08-14

First release. The Scientific Reproduction Skill package (`scientific-reproduction` 0.1.0) plus the full M0–M13 development record (80/80 atomic goals, all milestones PASS, release gates green).

### Added

**Package (`src/scientific_reproduction/`)**

- **Core models & state** — frozen model vocabulary (Run, Resource, AnalysisProtocol, EvidenceRecord, Requirement, Assumption, HumanGate, ClosureContract), termination gate, durable filesystem state backend with atomic writes, lease/lock primitives with stale-break and takeover semantics, event log, immutable project/plan identities with deterministic id generation.
- **Planning engine** — target form parsing, Plan v1 build/validation (inputs, steps, resource and protocol closure), superseded-plan rules, inventory registry with audit/repair (assumptions, evidence records, resources, analysis protocols, closure contracts, human gates), DAG constraints, freeze and register operations, goal ledger.
- **Research module** — protocol registry (freeze/revise, lineage), results registry with unresolved-reference rejection, evidence registry.
- **Workers** — worker context enforcement (goal-scope relevance filtering, frozen-goal guard, forbidden practices), permission matrix + runtime layer with engineering/scientific failure classification and retry whitelist, supervisor decision authorities.
- **Monitoring & recovery** — monitor registry, durable checkpoint/heartbeat state, strict recovery closure (MonitorRecovery, resume engine, corrupt-state refusal), reconciliation with completion signals, triggers, engineering-retry decisions.
- **Analysis** — protocol lineage, result registration/reading, closure validation.
- **Platform adapters** — RoleContract descriptor layer (CoreStateApi, DecisionAuthority, VerdictAuthority, RetryAuthority, Action sets, expose_command); Slurm-over-SSH adapter (queue-blind probing, R-SLURM-S1..S27 state normalization, SSHRetryPolicy, credentials in memory only — never persisted); Claude Code and Codex adapter contracts with documented subagent/process fallback; filesystem LabAdapter with exactly-once dispatch and result-manifest validation.
- **Domain pack (FDM-201)** — synthesis/characterization/adsorption/computation rule families (R-TPL, R-BF, R-CHA-A1..A6, R-ADS, R-COM) with real pack machinery, chemistry strictly instance data, validation and freeze flows, acceptance-statistics proposals.
- **CLI** — `scientific_reproduction.cli.reproduce` (init with deterministic identity, freeze, dispatch, status, collect) and the `/goals`-style views over planning primitives.
- **Reporting** — machine-auditable audit package builder/validator, traceability over real registries.

**Benchmark**

- **FDM-201 planning benchmark** — single reference paper (DOI 10.1039/D5TA00771B) with a frozen plan-state register, goal inventory (82 evidence records, 20 DAG-ordered plan nodes), execution packages (20/20 validate), and deterministic reload/audit checks.

**Docs**

- `docs/user/` — installation guide (stdlib-only runtime, editable install, `scripts/verify.py` canonical entry, manual gates, troubleshooting), reproduce-and-goals guide (CLI surface + honest `/goals` positioning), monitor-and-handoff guide (durable state, recovery model, filesystem Lab handoff).
- `docs/operations/` — Slurm-over-SSH adapter reference (constructor-bound configuration, operations, limitations), Claude Code handoff (spawn/resume with documented fallback).
- `docs/release/` — release review with all eight §4 gates PASS and observed evidence (`RELEASE-REVIEW-v0.1.md`), per-milestone status summary (`M0-M13-STATUS.md`), known limitations and accepted risks (`KNOWN-LIMITATIONS.md`), runbook with execution log (`DEV-M13-G05-runbook.md`).
- `README.md` — quickstart + doc links.

**Development record (`.development/`)**

- 80/80 atomic development goals (M0–M13) merged with PASS reviews; 4 REJECT→`-r2` repair chains with frozen ACs unchanged; goal ledger, milestone ledger (M0–M13 PASS), 88 event records, DAG state (recomputed byte-identical by `state_summary.py`), plans (G05 plan-approval protocol), attempts, reviews (83 review files).

### Known limitations

See `docs/release/KNOWN-LIMITATIONS.md` — all items are dispositioned and non-blocking for v0.1.0. Highlights: lease/lock TOCTOU at expiry boundary (ACCEPTED-RISK), one accepted timing-flaky test (DEV-DEFECT-002), evidence registry in-memory, constructor-bound adapter configuration, plan-state FDM-201 register (no executed runs), two documented ledger observations (DEV-M11-G06 review carried by event record; DEV-M10-G04/G06 `outcome: null`).

### Verification

- Full test suite: **3181 passed, 7 skipped, 1 deselected** on the tagged commit (pytest 9.1.1 / Python 3.13.9).
- `ruff check .` clean; `mypy src` clean (115 files); `scripts/validate_development_contracts.py` PASS (14 milestones, 80 goals, DAGs acyclic); FDM-201 inventory audit PASS (82/82 mapped); execution packages 20/20; CI: Test/lint + Validate-contracts both PASS per PR.
- All eight `18-TEST-AND-ACCEPTANCE-PLAN.md` §4 release gates PASS (evidence in `docs/release/RELEASE-REVIEW-v0.1.md`).
