# Scientific Reproduction Skill v0.2 — Implementation Specification

## Purpose

This package is a **Scientific Reproduction Skill**: an installable skill that
runs on agentic coding environments such as Claude Code, Codex, and WorkBuddy.
The Skill is not a single prompt. It is a packaged scientific workflow runtime
composed of role contracts, schemas, adapters, state management, evidence
governance, statistical validation, and domain-specific rules.

The first domain pack is **materials chemistry / computational materials
science**. The official v0.1 reference case is:

- Paper: *A highly connected metal–organic framework with stretched inorganic units for propylene/ethylene separation*
- DOI: `10.1039/D5TA00771B`
- Reference material: FDM-201

## Primary user experience

The intended user interaction is conceptually:

```text
/reproduce <PDF | DOI | URL>
```

The user primarily interacts with the **Supervisor**. The system initializes a
reproduction project, acquires sources, builds a complete reproduction
inventory, creates and freezes Plan v1, executes a `/goals` DAG using dynamic
worker sessions, monitors long-running experiment/computation runs, performs
independent analysis, handles evidence-backed recovery, and produces both a
human-readable report and a machine-auditable reproduction package.

## Non-negotiable architecture decisions

1. One primary target paper equals one reproduction project.
2. Reproduction means **all formally reported experiments, controls, and computations** in the main paper, SI, and linked public data—not merely the minimum evidence needed to validate headline claims.
3. `/goal` is a **Unit Process**. A `run` is one execution instance of that Unit Process.
4. Independent replication is mandatory for experimental reproduction. Default floor is `n >= 3`, but final sample size is dynamically designed.
5. Acceptance criteria and primary analysis protocols are frozen before execution.
6. Workers execute and report facts. **Only the Supervisor makes scientific decisions.**
7. Execution and analysis are separated.
8. Strict reproduction, recovery, and method redesign are distinct tracks.
9. Every evidence assessment is **source × claim specific**.
10. Project workspace is the Single Source of Truth; sessions are replaceable executors.
11. v0.1 state backend is filesystem-based, using atomic writes, leases/locks, and append-only events. No SQLite dependency in v0.1.
12. Git stores scientific decision history; large artifacts are stored externally and registered using manifests plus checksums.
13. Three project-persistent roles exist: Supervisor, Research, Execution Monitor. The Execution Monitor is the only v0.1 role requiring high-availability runtime behavior.
14. Dynamic worker sessions are created automatically by platform adapters whenever possible.
15. Core scientific logic must be shared across agent platforms; platform-specific behavior is isolated behind adapters.

## Distribution (v0.2 skill packaging)

This repository **is** the installable skill. The skill entry is
[`SKILL.md`](SKILL.md) (Agent Skills standard: Claude Code, WorkBuddy), the
Codex entry is [`AGENTS.md`](AGENTS.md), and the Python runtime under `src/`
is bundled with **zero install steps** — the wrapper
[`scripts/reproduce.py`](scripts/reproduce.py) runs it via `PYTHONPATH`.

- **Claude Code**: copy this directory to `~/.claude/skills/scientific-reproduction/`
  (or install as a plugin/marketplace skill).
- **WorkBuddy**: copy this directory to `~/.workbuddy/skills/scientific-reproduction/`
  (or install from a skill hub).
- **Codex**: open this directory (Codex reads `AGENTS.md`).

Verify any installation with `python scripts/smoke.py`. The skill is
self-contained: Python 3.11+ and git are the only external requirements.

## Recommended reading order

For skill users:

1. `docs/user/installation.md`
2. `docs/user/reproduce-and-goals.md`
3. `docs/user/monitor-and-handoff.md`
4. `docs/operations/adapters-slurm.md`

For implementers, the product specification:

1. `01-PRODUCT-REQUIREMENTS.md`
2. `02-SYSTEM-ARCHITECTURE.md`
3. `20-ARCHITECTURE-DECISIONS.md`
4. `03-ROLE-AND-PERMISSION-SPEC.md`
5. `05-GOAL-RUN-SCHEMA.md`
6. `06-EVIDENCE-SYSTEM.md`
7. `07-STATISTICS-AND-ACCEPTANCE.md`
8. `08-STRICT-RECOVERY-CLOSURE.md`
9. `13-EXECUTION-MONITOR.md`
10. `14-STATE-GIT-ARTIFACTS.md`
11. `17-FDM201-REFERENCE-CASE.md`
12. `18-TEST-AND-ACCEPTANCE-PLAN.md`
13. `docs/spec/index.md` — canonical index of every normative document

## Package contents

- Skill manifest (`SKILL.md`) and Codex entry (`AGENTS.md`)
- Zero-install `/reproduce` CLI wrapper and smoke verification
- Bundled Python runtime (stdlib-only) under `src/scientific_reproduction/`
- Product and architecture specifications
- Lifecycle and permission models
- Goal/Run/Evidence/Assumption/Resource schemas
- Statistical and acceptance governance
- Strict/Recovery/Closure rules
- Research, experiment, computation, analysis and monitoring subsystem specifications
- State, Git, artifact, adapter and concurrency rules
- Materials Chemistry domain pack specification
- FDM-201 reference benchmark and examples
- Test plan, version roadmap, and role contracts

## What this package does NOT contain

The v0.1 runtime is a **skeleton**: it implements the deterministic state,
schema, planning-init, and audit primitives, while the scientific execution
flow (research → plan → execute → analyze → report) is orchestrated by the
agent roles according to the locked specifications. The complete scientific
runtime is implemented incrementally per `19-VERSION-ROADMAP.md`.
