# Scientific Reproduction Skill v0.1 — Implementation Specification

## Purpose

This package is an implementation-ready specification for a **Scientific Reproduction Skill** intended to run on agentic coding environments such as Claude Code and Codex. The Skill is not a single prompt. It is a packaged scientific workflow runtime composed of role contracts, schemas, adapters, state management, evidence governance, statistical validation, and domain-specific rules.

The first domain pack is **materials chemistry / computational materials science**. The official v0.1 reference case is:

- Paper: *A highly connected metal–organic framework with stretched inorganic units for propylene/ethylene separation*
- DOI: `10.1039/D5TA00771B`
- Reference material: FDM-201

## Primary user experience

The intended user interaction is conceptually:

```text
/reproduce <PDF | DOI | URL>
```

The user primarily interacts with the **Supervisor**. The system initializes a reproduction project, acquires sources, builds a complete reproduction inventory, creates and freezes Plan v1, executes a `/goals` DAG using dynamic worker sessions, monitors long-running experiment/computation runs, performs independent analysis, handles evidence-backed recovery, and produces both a human-readable report and a machine-auditable reproduction package.

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
15. Core scientific logic must be shared across Claude Code and Codex; platform-specific behavior is isolated behind adapters.

## Recommended reading order for implementers

For autonomous Claude Code development, read first:

1. `21-DEVELOPMENT-SUPERVISOR-SPEC.md`
2. `22-AUTONOMOUS-M0-M13-EXECUTION.md`
3. `23-DEVELOPMENT-GOAL-CATALOG.md`
4. `24-DEVELOPMENT-QUALITY-GATES.md`
5. `25-DEVELOPMENT-GIT-GOVERNANCE.md`
6. `26-DEVELOPMENT-CHANGE-CI-RELEASE.md`
7. `CLAUDE-CODE-HANDOFF.md`
8. `development/development-plan.v1.yaml`
9. `development/git-policy.v1.yaml`

Then read the product specification:

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
13. `CLAUDE-CODE-HANDOFF.md`

## Package contents

- Product and architecture specifications
- Lifecycle and permission models
- Goal/Run/Evidence/Assumption/Resource schemas
- Statistical and acceptance governance
- Strict/Recovery/Closure rules
- Research, experiment, computation, analysis and monitoring subsystem specifications
- State, Git, artifact, adapter and concurrency rules
- Materials Chemistry domain pack specification
- FDM-201 reference benchmark
- v0.1 test plan and version roadmap
- Role contracts and skeleton templates
- Claude Code implementation handoff
- Autonomous Development Supervisor specification
- Frozen M0–M13 milestone DAG and 80 atomic development goals
- Development goal/milestone/result/review schemas and startup templates

## What this package does NOT contain

This is **not** a complete implementation. It intentionally stops at an implementation-ready design. Claude Code (or another coding agent) should implement the package according to these locked requirements rather than redesigning the product from scratch.

## Autonomous implementation mode

The default handoff now assumes one Claude Code **Development Supervisor** that automatically completes M0–M13 by delegating frozen atomic development goals to independent Claude Code workers, independently verifying results, retrying rejected work, and continuing until all v0.1 release gates pass. The user should not need to approve routine milestone progression. See `development/templates/DEVELOPMENT-SUPERVISOR-START.md`.


## GitHub development remote

The default implementation remote is `https://github.com/lichman0405/scientific-reproduction.git`. After the specification-only empty-repository bootstrap commit, every repository-changing DEV-GOAL must use a short-lived goal branch, PR, required verification/CI, independent Supervisor review, and merge to `main`. See `25-DEVELOPMENT-GIT-GOVERNANCE.md`.
