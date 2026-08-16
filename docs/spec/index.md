# Normative Specification Index

This index maps every locked specification document of Scientific Reproduction
Skill v0.2 to its canonical path and a one-line purpose. It is a **local
pointer index only**: the normative text lives in the canonical files listed
below. Do not duplicate normative requirements here — any quoted snippet must
be verbatim from the canonical document, and if an entry needs more than a
one-line purpose, the canonical document is the place for it.

The canonical documents are frozen. Changes to the specification itself are
recorded as ADRs under `docs/adr/` (see `docs/adr/README.md`).

## Locked specification documents (repository root)

| Document | Purpose |
|---|---|
| [`00-README.md`](../../00-README.md) | Product overview, non-negotiable architecture decisions, recommended reading order, package contents. |
| [`01-PRODUCT-REQUIREMENTS.md`](../../01-PRODUCT-REQUIREMENTS.md) | Product objective and requirements for the reproduction skill. |
| [`02-SYSTEM-ARCHITECTURE.md`](../../02-SYSTEM-ARCHITECTURE.md) | Architectural model and planes of the runtime. |
| [`03-ROLE-AND-PERMISSION-SPEC.md`](../../03-ROLE-AND-PERMISSION-SPEC.md) | Roles, permissions, and the governance principle "Workers report facts. Supervisor makes scientific decisions." |
| [`04-PROJECT-LIFECYCLE.md`](../../04-PROJECT-LIFECYCLE.md) | Project lifecycle: `project_phase` and `reproduction_outcome` are strictly separate. |
| [`05-GOAL-RUN-SCHEMA.md`](../../05-GOAL-RUN-SCHEMA.md) | Goal / Run / Requirement hierarchy and model (`/goal` = Unit Process, `run` = execution instance). |
| [`06-EVIDENCE-SYSTEM.md`](../../06-EVIDENCE-SYSTEM.md) | Evidence system: evidence is assessed as Source × Claim, never one global score. |
| [`07-STATISTICS-AND-ACCEPTANCE.md`](../../07-STATISTICS-AND-ACCEPTANCE.md) | Statistics and acceptance governance: equivalence/uncertainty, PASS/FAIL/INCONCLUSIVE. |
| [`08-STRICT-RECOVERY-CLOSURE.md`](../../08-STRICT-RECOVERY-CLOSURE.md) | Strict Reproduction, Recovery L1–L3, Method Redesign L4, and the Closure Contract. |
| [`09-RESEARCH-SUBSYSTEM.md`](../../09-RESEARCH-SUBSYSTEM.md) | Research / literature subsystem: a project-persistent evidence service. |
| [`10-EXPERIMENT-SUBSYSTEM.md`](../../10-EXPERIMENT-SUBSYSTEM.md) | Experiment subsystem: durable external wet-lab runs with replaceable worker sessions. |
| [`11-COMPUTATION-SUBSYSTEM.md`](../../11-COMPUTATION-SUBSYSTEM.md) | Computation subsystem: SSH-accessible HPC, Slurm + environment modules in v0.1. |
| [`12-ANALYSIS-SUBSYSTEM.md`](../../12-ANALYSIS-SUBSYSTEM.md) | Analysis and diagnosis subsystem: independent analysis separated from execution. |
| [`13-EXECUTION-MONITOR.md`](../../13-EXECUTION-MONITOR.md) | Execution Monitor role: the only v0.1 role requiring a high-availability runtime policy. |
| [`14-STATE-GIT-ARTIFACTS.md`](../../14-STATE-GIT-ARTIFACTS.md) | State, Git and artifact architecture: workspace is the Single Source of Truth. |
| [`15-ADAPTER-SPEC.md`](../../15-ADAPTER-SPEC.md) | Adapter specification: Lab, Compute, Research, and Platform adapters. |
| [`16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md`](../../16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md) | Materials chemistry / computational materials science domain pack (v0.1 domain). |
| [`17-FDM201-REFERENCE-CASE.md`](../../17-FDM201-REFERENCE-CASE.md) | FDM-201 official reference reproduction case (DOI `10.1039/D5TA00771B`). |
| [`18-TEST-AND-ACCEPTANCE-PLAN.md`](../../18-TEST-AND-ACCEPTANCE-PLAN.md) | v0.1 test and acceptance plan (planning/orchestration plus simulated execution). |
| [`19-VERSION-ROADMAP.md`](../../19-VERSION-ROADMAP.md) | Version roadmap for v0.1 and later releases. |
| [`20-ARCHITECTURE-DECISIONS.md`](../../20-ARCHITECTURE-DECISIONS.md) | **Locked architecture decisions** (ADR summary): 50 product decisions; canonical source for architecture review. |

## Skill package files

| File | Purpose |
|---|---|
| [`SKILL.md`](../../SKILL.md) | Skill manifest and entry instructions (Claude Code, WorkBuddy and any Agent-Skills-standard platform). |
| [`AGENTS.md`](../../AGENTS.md) | Agent entry instructions for Codex. |
| [`scripts/check-update.py`](../../scripts/check-update.py) | Pre-flight skill update check (local checkout vs the remote release branch; exit 0/1/2 = up to date / update available / cannot check). |
| [`scripts/reproduce.py`](../../scripts/reproduce.py) | Zero-install `/reproduce` CLI wrapper (no pip install required). |
| [`scripts/smoke.py`](../../scripts/smoke.py) | Platform-independent smoke verification (skill structure + `reproduce init`). |
| [`scripts/verify.py`](../../scripts/verify.py) | Canonical repository verification entry point (pytest + ruff + mypy). |

## Root auxiliary documents

| Document | Purpose |
|---|---|
| [`CONTRIBUTING.md`](../../CONTRIBUTING.md) | Contribution rules: branch/PR/commit governance and verification gates. |

## Product schemas (`schemas/`)

| Schema | Title | Purpose |
|---|---|---|
| [`schemas/project.schema.yaml`](../../schemas/project.schema.yaml) | ReproductionProject | One reproduction project (one target paper). |
| [`schemas/plan.schema.yaml`](../../schemas/plan.schema.yaml) | ReproductionPlan | Reproduction plan incl. resource/procurement planning. |
| [`schemas/goal.schema.yaml`](../../schemas/goal.schema.yaml) | GoalContract | `/goal` Unit Process contract. |
| [`schemas/run.schema.yaml`](../../schemas/run.schema.yaml) | Run | One execution instance of a goal. |
| [`schemas/requirement.schema.yaml`](../../schemas/requirement.schema.yaml) | ReproductionRequirement | Requirement with checklist-derived criticality. |
| [`schemas/inventory-item.schema.yaml`](../../schemas/inventory-item.schema.yaml) | ReproductionInventoryItem | Reproduction inventory item. |
| [`schemas/acceptance-criteria.schema.yaml`](../../schemas/acceptance-criteria.schema.yaml) | AcceptanceCriteria | Frozen acceptance criteria. |
| [`schemas/statistical-design.schema.yaml`](../../schemas/statistical-design.schema.yaml) | StatisticalDesign | Frozen statistical design (07-SS9), referenced by `statistical_design_ref`. |
| [`schemas/closure-contract.schema.yaml`](../../schemas/closure-contract.schema.yaml) | ClosureContract | Stopping/closure governance. |
| [`schemas/assumption.schema.yaml`](../../schemas/assumption.schema.yaml) | Assumption | Assumption (incl. A2 Scientific Assumption). |
| [`schemas/resource.schema.yaml`](../../schemas/resource.schema.yaml) | Resource | Resource/procurement item. |
| [`schemas/human-gate.schema.yaml`](../../schemas/human-gate.schema.yaml) | HumanGate | Human-gate trigger record. |
| [`schemas/source.schema.yaml`](../../schemas/source.schema.yaml) | ResearchSource | Research source record. |
| [`schemas/evidence.schema.yaml`](../../schemas/evidence.schema.yaml) | ClaimSpecificEvidence | Source × Claim specific evidence record. |
| [`schemas/analysis.schema.yaml`](../../schemas/analysis.schema.yaml) | AnalysisProtocolOrResult | Analysis protocol/result record. |
| [`schemas/decision.schema.yaml`](../../schemas/decision.schema.yaml) | SupervisorDecision | Supervisor scientific decision record. |
| [`schemas/research-request.schema.yaml`](../../schemas/research-request.schema.yaml) | ResearchRequest | Research request record. |
| [`schemas/retry-policy.schema.yaml`](../../schemas/retry-policy.schema.yaml) | AutomaticRetryPolicy | Whitelisted automatic-retry policy. |
| [`schemas/event.schema.yaml`](../../schemas/event.schema.yaml) | ProjectEvent | Append-only project event. |
| [`schemas/artifact-manifest.schema.yaml`](../../schemas/artifact-manifest.schema.yaml) | ArtifactManifest | External-artifact manifest with checksums. |
| [`schemas/lab-execution-package.schema.yaml`](../../schemas/lab-execution-package.schema.yaml) | LabExecutionPackage | Lab execution package for wet-lab handoff. |
| [`schemas/worker-context.schema.yaml`](../../schemas/worker-context.schema.yaml) | GoalExecutionContextPackage | Goal execution context handed to a worker. |

## Agent role contracts (`agent-contracts/`)

| Contract | Purpose |
|---|---|
| [`agent-contracts/SUPERVISOR.md`](../../agent-contracts/SUPERVISOR.md) | Supervisor role contract (sole scientific governance authority). |
| [`agent-contracts/RESEARCH.md`](../../agent-contracts/RESEARCH.md) | Research role contract (project-persistent evidence service). |
| [`agent-contracts/EXECUTION-MONITOR.md`](../../agent-contracts/EXECUTION-MONITOR.md) | Execution Monitor role contract (high-availability runtime). |
| [`agent-contracts/EXPERIMENT-WORKER.md`](../../agent-contracts/EXPERIMENT-WORKER.md) | Experiment Worker contract. |
| [`agent-contracts/COMPUTATION-WORKER.md`](../../agent-contracts/COMPUTATION-WORKER.md) | Computation Worker contract. |
| [`agent-contracts/ANALYSIS-WORKER.md`](../../agent-contracts/ANALYSIS-WORKER.md) | Analysis Worker contract. |

## Skeleton templates (`templates/`)

| Template | Purpose |
|---|---|
| [`templates/CLAUDE.md.template`](../../templates/CLAUDE.md.template) | User-project `CLAUDE.md` skeleton. |
| [`templates/PROJECT-TREE.template.txt`](../../templates/PROJECT-TREE.template.txt) | Project tree skeleton (starter `.gitignore`/`.gitattributes` are written by `reproduce init`). |
| [`templates/SKILL.md.template`](../../templates/SKILL.md.template) | Skill manifest skeleton (source for the root `SKILL.md`). |

## Reference examples (`examples/`)

| Example | Purpose |
|---|---|
| [`examples/fdm-201/project.example.yaml`](../../examples/fdm-201/project.example.yaml) | FDM-201 project instance example. |
| [`examples/fdm-201/goal.example.yaml`](../../examples/fdm-201/goal.example.yaml) | FDM-201 goal instance example. |
| [`examples/fdm-201/inventory.example.yaml`](../../examples/fdm-201/inventory.example.yaml) | FDM-201 inventory instance example. |
| [`examples/fdm-201/assumption.example.yaml`](../../examples/fdm-201/assumption.example.yaml) | FDM-201 assumption instance example. |
| [`examples/fdm-201/evidence.example.yaml`](../../examples/fdm-201/evidence.example.yaml) | FDM-201 evidence instance example. |
| [`examples/fdm-201/acceptance.example.yaml`](../../examples/fdm-201/acceptance.example.yaml) | FDM-201 acceptance-criteria instance example. |
| [`examples/fdm-201/statistical-design.example.yaml`](../../examples/fdm-201/statistical-design.example.yaml) | FDM-201 statistical-design instance example. |
| [`examples/fdm-201/research-request.example.yaml`](../../examples/fdm-201/research-request.example.yaml) | FDM-201 research-request instance example. |
| [`examples/fdm-201/simulated-scenarios.md`](../../examples/fdm-201/simulated-scenarios.md) | Simulated execution scenarios for v0.1 acceptance. |

## Local documentation

| Path | Purpose |
|---|---|
| [`README.md`](../../README.md) | Repository entry point (skill installation and usage). |
| [`docs/README.md`](../README.md) | Developer-focused package documentation (also the PyPI readme). |
| [`docs/spec/index.md`](index.md) | This normative specification index. |
| [`docs/adr/`](../adr/README.md) | Architecture Decision Records directory (README, template, recorded ADRs). |
| [`docs/release/KNOWN-LIMITATIONS.md`](../release/KNOWN-LIMITATIONS.md) | Known limitations and accepted risks of the v0.1 runtime. |

## Index maintenance rules

1. This index must only **reference** canonical documents; never copy normative
   text into it (avoids specification drift; see `20-ARCHITECTURE-DECISIONS.md`).
2. When the specification package changes, update the affected rows and record
   an ADR under `docs/adr/` in the same change.
3. One-line purposes must remain faithful summaries; when in doubt, shorten
   rather than paraphrase normatively.
