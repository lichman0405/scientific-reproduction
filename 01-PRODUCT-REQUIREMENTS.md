# 01 — Product Requirements

## 1. Product objective

Build a reusable agent Skill that can take one scientific paper as the primary reproduction target and orchestrate a complete, auditable reproduction project across literature research, wet-lab execution, computational execution, independent analysis, statistics, recovery, and final reporting.

The product must behave like a small scientific project team rather than a paper summarizer.

## 2. Primary problem

Scientific papers routinely omit tacit details, compress methods, report representative results without robust repeated measurements, and distribute crucial information across main text, SI, repositories, structures, and prior publications. A competent reproduction system must therefore:

- recover all formally reported procedures and results;
- distinguish explicit parameters from assumptions;
- decompose the paper into executable Unit Processes;
- attach evidence-backed acceptance criteria to each Unit Process;
- mandate statistically meaningful independent replication;
- separate execution from interpretation;
- preserve failures rather than selecting only successful runs;
- allow recovery when strict reproduction fails, without rewriting history;
- terminate rigorously when non-reproduction has sufficient evidence;
- remain resumable across long external jobs and lost agent sessions.

## 3. v0.1 scope

### In scope

- One primary target paper per project.
- Materials chemistry / computational materials science domain pack.
- Main paper, SI, CIF/structure files, linked repositories and related references.
- Wet-lab task packaging via filesystem/manual handoff adapter.
- Compute execution via local/SSH/Slurm-over-SSH adapters, with Slurm-over-SSH as the primary v0.1 path.
- DFT/GCMC/MD planning and execution abstractions.
- Experiment and computation Runs that may outlive their worker sessions.
- Literature research from open/public sources and optional commercial adapters.
- Statistical analysis and equivalence-oriented acceptance logic.
- Plan/Goal/Protocol freezing and versioned re-planning.
- Human-readable final report plus machine-auditable package.
- Claude Code and Codex platform adapter interfaces.
- FDM-201 full planning benchmark plus simulated execution scenarios.

### Out of scope for v0.1

- Direct control of laboratory instruments.
- Mandatory enterprise messaging integration.
- Mandatory commercial database access.
- Multi-paper projects under one reproduction Project.
- SQLite/PostgreSQL runtime state.
- Web dashboard.
- Full biology/medicine domain logic.
- Fully automated purchasing.
- Autonomous external contact with paper authors.

## 4. Primary user journey

1. User invokes `/reproduce <paper>`.
2. Skill initializes a new Git-backed project workspace.
3. Supervisor starts persistent Research and Execution Monitor roles.
4. Research performs mandatory bootstrap source acquisition and builds the knowledge base.
5. Supervisor creates a complete Reproduction Inventory.
6. Inventory is audited for 100% coverage of formally reported items.
7. Supervisor creates Work Packages, Requirements, Unit Process `/goals`, dependencies, resources, acceptance criteria, replication plans, primary analysis protocols, assumption registry and closure contracts.
8. Plan v1 is audited and frozen.
9. Supervisor dispatches eligible dynamic workers according to the DAG and concurrency policy.
10. Workers execute one Goal context at a time and write Result Packages.
11. Execution Monitor tracks external experiment/computation Runs, handles engineering retries within policy, and triggers analysis when results arrive.
12. Analysis Workers independently process data using frozen Primary Analysis Protocols.
13. Supervisor reviews evidence and decides Requirement outcomes.
14. Failures trigger Diagnosis, Research, Recovery and versioned re-planning as needed.
15. When all Requirements are closed or validly inconclusive, project enters Final Validation.
16. Supervisor produces final scientific outcome, method reproducibility outcome, report and auditable package.

## 5. Required product properties

### Reproducibility
All major system decisions must be reconstructable from versioned records.

### Auditability
A final conclusion must trace backward to analysis, Run(s), raw artifacts and source evidence.

### Resumability
A fresh session must be able to continue the project from the shared workspace without relying on hidden chat memory.

### Role isolation
Workers do not become mini-supervisors. Scientific governance is centralized.

### Controlled autonomy
Supervisor is highly autonomous in normal scientific workflow, but must stop at Human Escalation Gates.

### Domain extensibility
Core runtime remains domain-agnostic; domain packs add experimental/computational domain rules.

## 6. User-facing commands (semantic contract)

Platform adapters may expose these as slash commands or equivalent commands:

- `/reproduce <PDF|DOI|URL>` — initialize project.
- `/goals` — summarize Goal DAG and current states.
- `/goals show <GOAL_ID>` — show frozen contract, dependencies, Runs and evidence.
- `/goals runs <GOAL_ID>` — show Runs.
- `/goals blocked` — show blocked Goals and blocker objects.
- `/status` — project phase, active roles, active workers, external Runs and gates.
- `/evidence <claim|goal>` — show relevant evidence assessments.
- `/report` — current interim report; final report only after final validation.

These commands must not bypass Supervisor governance.
