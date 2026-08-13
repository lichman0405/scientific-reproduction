# Claude Code Implementation Handoff

> **Default development mode: `AUTO_RUN_M0_TO_M13`.** One long-lived Development Supervisor must autonomously orchestrate atomic development workers, independently accept/reject their results, and continue until M13 plus all v0.1 release gates pass. Do not stop after M0/M1 or at milestone boundaries. See `21-DEVELOPMENT-SUPERVISOR-SPEC.md` through `24-DEVELOPMENT-QUALITY-GATES.md` and `development/`.


## 0. Your role

You are receiving an already-designed product. **Do not restart product discovery.** Implement the v0.1 Scientific Reproduction Skill according to this specification.

Before coding, read:

1. `00-README.md`
2. `20-ARCHITECTURE-DECISIONS.md`
3. `02-SYSTEM-ARCHITECTURE.md`
4. `03-ROLE-AND-PERMISSION-SPEC.md`
5. `18-TEST-AND-ACCEPTANCE-PLAN.md`

If implementation constraints conflict with a locked decision, document the conflict in an ADR and ask the user only when the conflict cannot be resolved without changing the product contract.

## GitHub implementation governance

The default remote is `https://github.com/lichman0405/scientific-reproduction.git`.

After a one-time specification-only empty-repository bootstrap, all repository-changing DEV-GOALs use branch -> push -> PR -> CI -> independent Supervisor review -> merge. Default merge is squash. Workers never merge themselves or commit product implementation directly to `main`.

Read `25-DEVELOPMENT-GIT-GOVERNANCE.md`, `26-DEVELOPMENT-CHANGE-CI-RELEASE.md`, and `development/git-policy.v1.yaml` before implementation.

## 1. Non-negotiable constraints

Do not:

- turn the system into a single giant prompt;
- merge Supervisor and Worker authority;
- allow workers to modify scientific plans;
- skip pre-data freezing of acceptance and analysis protocols;
- implement “p > 0.05 = reproduced” logic;
- delete or hide failed Runs;
- use one monolithic mutable state file;
- introduce SQLite/Postgres as a required v0.1 dependency;
- hardcode Claude Code behavior into the scientific Core;
- make commercial databases mandatory;
- require real months-long FDM wet-lab validation before v0.1 can pass tests.

## 2. Suggested technology choices

Use Python for the deterministic runtime unless the repository context strongly justifies another language. Keep dependencies modest.

Suggested modules/packages:

```text
scientific_reproduction/
  core/
    ids.py
    models.py
    state_backend.py
    transitions.py
    events.py
    leases.py
    rules/
      evidence.py
      criticality.py
      outcome.py
      closure.py
  planning/
  research/
  monitoring/
  workers/
  analysis/
  adapters/
    lab/
    compute/
    research/
    platform/
  domain_packs/
    materials_chemistry/
  cli/
  reporting/
```

Use JSON Schema validation or an equivalent typed validation layer. Schema files in `schemas/` are normative field-level starting points and may be refined without violating semantics.

## 3. Milestone plan

M0–M13 are **Milestones / Work Packages**, not worker-sized tasks. The normative atomic decomposition is frozen in:

- `development/development-plan.v1.yaml`
- `development/milestones/M0.yaml` … `M13.yaml`
- `development/goals/DEV-M*-G*.yaml`
- `23-DEVELOPMENT-GOAL-CATALOG.md`

The Development Supervisor must mirror ready atomic goals into Claude Code teammates/detached workers and independently review each result. Milestone acceptance below remains normative as a higher-level integration gate.


### M0 — Repository bootstrap

Deliver:

- project package structure;
- tests directory;
- lint/test tooling;
- copy schemas/spec docs into repository docs;
- one command to run tests.

Acceptance:

- clean install in an isolated environment;
- test command succeeds.

### M1 — Core object model and filesystem state

Implement:

- Project, Plan, InventoryItem, Requirement, Goal, Run, Source, Evidence, AcceptanceCriteria, Analysis, Assumption, Resource, Decision, Event, HumanGate, ArtifactManifest, WorkerContext;
- deterministic IDs;
- FilesystemStateBackend;
- atomic writes;
- object validation;
- per-object lock/lease;
- append-only events.

Acceptance:

- concurrent/duplicate lease tests;
- partial-write recovery test;
- schema-invalid write rejected;
- event ordering and idempotency tests.

### M2 — Lifecycle and rule engine

Implement:

- legal project phases;
- Run lifecycle state machine;
- dependency/gate evaluation;
- evidence rubric function;
- criticality rule interface;
- Closure Contract evaluator;
- outcome aggregator;
- Assumption A0/A1/A2 effects.

Acceptance:

- rule tests cover all normative paths;
- illegal scientific shortcut transitions rejected.

### M3 — Git audit and artifact manifests

Implement:

- project Git initialization;
- scientific audit commit helper;
- artifact manifest/checksum registration;
- no heartbeat-per-commit behavior.

Acceptance:

- Plan freeze, Goal revision and final outcome produce auditable commits;
- large dummy artifact stays outside Git while manifest validates checksum.

### M4 — Project initialization and Supervisor planning primitives

Implement runtime APIs/tools that allow Supervisor to:

- initialize `/reproduce` project;
- register primary target;
- create inventory items;
- map inventory -> Requirements -> Goals;
- validate completeness;
- create Plan v1;
- freeze Goal/Acceptance/Analysis/Closure records;
- block freeze unless 100% inventory audit passes.

Do not yet attempt perfect scientific LLM planning. Build deterministic contracts and tool boundaries first.

Acceptance:

- intentionally unmapped item prevents freeze;
- complete synthetic inventory freezes successfully.

### M5 — Research subsystem

Implement:

- normalized source record;
- bootstrap-research workflow contract;
- Research Request objects;
- Source×Claim evidence records;
- reliability checklist/rule hook;
- source deduplication;
- search-cycle/saturation records;
- public-source adapter interfaces.

Acceptance:

- duplicate DOI mirrors collapse to one source;
- evidence is claim-specific;
- Reliability cannot be written without checklist result reference.

### M6 — Worker context and permission enforcement

Implement:

- Goal Execution Context Package generator;
- Worker return package contract;
- role-specific allowed runtime actions;
- hard rejection of forbidden Worker plan mutations.

Acceptance:

- worker cannot modify frozen Goal;
- worker cannot close Requirement;
- worker context includes only declared relevant refs.

### M7 — Lab and Compute adapters

Lab:

- filesystem outgoing/incoming reference implementation.

Compute:

- local;
- SSH skeleton;
- Slurm-over-SSH implementation or a robust testable first implementation.

Acceptance:

- dispatch experiment package, simulate return;
- submit mock/real local Slurm-like flow where practical;
- external job ID persists after worker exits.

### M8 — Execution Monitor and recovery

Implement:

- watched Run registry/checkpoint;
- heartbeat;
- external status reconciliation;
- engineering retry whitelist;
- Monitor restart/resume interface;
- replacement Monitor reconstruction;
- duplicate-trigger protection.

Acceptance:

- Test Scenarios D, G, H pass.

### M9 — Analysis/statistics subsystem

Implement:

- frozen Primary Analysis Protocol records;
- Analysis Result Package;
- exploratory separation;
- basic statistical module/interface supporting CI/equivalence logic;
- computational convergence validation hooks.

Acceptance:

- inconclusive case cannot be coerced to PASS;
- exploratory analysis cannot overwrite primary;
- additional replicate request can be generated by Supervisor logic.

### M10 — Supervisor/Research/Monitor agent contracts and platform adapters

Implement prompts/contracts and platform orchestration interfaces.

Claude Code adapter should attempt real:

- persistent-role start/resume;
- dynamic worker spawn;
- worker termination;
- subagent/process fallback.

Codex adapter should implement the same Core contract and be tested as far as environment access allows.

Do not duplicate scientific logic inside platform adapters.

### M11 — Materials Chemistry domain pack

Implement templates/rules for:

- synthesis and independent batches;
- PXRD;
- TGA;
- BET;
- gas adsorption;
- IAST/Qst;
- breakthrough;
- DFT/GCMC/MD metadata and validation hooks.

Acceptance:

- FDM-201 planning can express all required Work Packages/Goals.

### M12 — FDM-201 benchmark

Use DOI `10.1039/D5TA00771B` as the primary target.

Run real source acquisition where accessible. Acquire paper/SI/CIF/related sources. Build a real Reproduction Inventory and Plan v1.

Hard acceptance:

- 100% formally reported-item mapping;
- full Goals DAG;
- resources/assumptions/evidence/acceptance/analysis/closure objects;
- experiment and compute execution package generation.

Then execute simulated scenarios A–J from `18-TEST-AND-ACCEPTANCE-PLAN.md`.

### M13 — Reporting and final audit

Implement:

- machine-auditable package validator;
- traceability checker;
- human-readable report generator;
- method-reproducibility summary;
- project final outcome summary.

Acceptance:

- a report key claim can be traced to Analysis -> Run -> Artifact/Evidence;
- failed Runs remain visible in audit package.

## 4. Development workflow requirements

- Use tests before broad refactors.
- Keep deterministic logic out of prompts where feasible.
- Record architecture deviations as ADRs.
- Use one short-lived branch and pull request per repository-changing DEV-GOAL; do not wait for milestone boundaries to integrate accepted goals.
- Default to squash merge after independent Supervisor review and green CI.
- Tag `v0.1.0` only after the final release suite passes on the exact main commit.
- Do not silently change schemas/semantics to make tests easier.
- Build small runnable fixtures for every lifecycle edge case.

## 5. First instruction after cloning/receiving this spec

The recommended path is now **autonomous M0–M13 development**, not a manual M0/M1 handoff.

Use `development/templates/DEVELOPMENT-SUPERVISOR-START.md` as the startup instruction. The Development Supervisor must:

1. initialize `.development/` from the frozen Development Plan v1;
2. run under a global native Claude Code `/goal` when available;
3. explicitly create/manage an Agent Team or documented fallback workers;
4. assign atomic `DEV-GOAL`s, not whole milestones, to workers;
5. independently rerun acceptance checks and reject/repair failures;
6. automatically unlock and continue through M0–M13;
7. stop only when the full v0.1 Definition of Done is satisfied or a formal Development Human Gate blocks all useful progress.

**Native `/goal` nuance:** use native `/goal` on the Development Supervisor. Agent Team teammates use frozen goal contracts plus `TaskCompleted`/`TeammateIdle` gates because built-in slash commands cannot be reliably injected into teammate sessions by the lead. For selected self-contained workers that benefit from native `/goal`, launch detached non-interactive Claude Code sessions using the platform-supported `claude -p "/goal ..."` path and still require independent Supervisor review.

## 6. Definition of done for v0.1

The Development Supervisor must not return control simply because an individual milestone is complete. Completion is the global terminal condition below.

v0.1 is done only when:

- M0–M13 acceptance conditions are met;
- FDM-201 real planning benchmark passes 100% inventory audit;
- all required simulated execution scenarios pass;
- Claude Code and Codex platform adapter contracts exist without scientific-core divergence;
- the final audit package validator passes;
- documentation explains installation, `/reproduce`, `/goals`, Monitor runtime, Lab handoff and Slurm configuration.
