# 18 — v0.1 Test and Acceptance Plan

## 1. Philosophy

v0.1 acceptance must prove that the scientific workflow logic functions end-to-end without requiring months of real wet-lab reproduction before release.

Use **real planning/orchestration against FDM-201** plus **controlled simulated execution outcomes**.

## 2. Test layers

### Unit tests

- schema validation;
- deterministic ID generation;
- atomic file write;
- lock/lease acquisition and expiration;
- event append;
- legal/illegal Run state transitions;
- evidence rubric calculation;
- criticality checklist mapping;
- final outcome aggregation;
- Closure Contract evaluation;
- Assumption Registry rules;
- dependency/gate logic.

### Integration tests

- project initialization + Git;
- Research source registration;
- Plan freeze flow;
- dynamic Worker spawn via mock platform adapter;
- filesystem LabAdapter dispatch/return;
- mock Slurm ComputeAdapter lifecycle;
- Monitor event processing;
- analysis package creation;
- Git audit commit generation;
- artifact manifest/checksum validation.

### Reference-case planning benchmark

Run real FDM-201 source acquisition and planning. Require:

- complete inventory;
- 100% mapping audit;
- Plan v1;
- Goals DAG;
- acceptance/statistical plans;
- assumptions;
- resource plan;
- experiment packages;
- compute packages.

## 3. Required execution simulation scenarios

### Scenario A — Strict success

- valid independent Runs arrive;
- Analysis supports frozen acceptance;
- Supervisor closes Goal/Requirement reproduced.

Expected: no Recovery; audit chain complete.

### Scenario B — Strict failure -> diagnosis -> research -> recovery -> success

- strict independent Runs fail;
- Supervisor creates Diagnosis Worker;
- Supervisor creates Research Request;
- eligible hypothesis emerges;
- versioned Recovery Goal/Run created;
- Recovery Runs pass.

Expected: scientific Requirement `REPRODUCED_WITH_RECOVERY`; method reproducibility lower than direct reproducibility.

### Scenario C — Strict failure -> Recovery exhausted -> non-reproduced closure

- strict failure statistically sufficient;
- QC valid;
- all eligible hypotheses tested/ruled out;
- research saturation condition met;
- Closure Contract satisfied.

Expected: `NOT_REPRODUCED` Requirement and project aggregation behavior; Human Termination Gate created.

### Scenario D — Inconclusive statistics

- observed mean near target but CI too wide / sample size insufficient.

Expected: `INCONCLUSIVE`, automatic request for additional independent Runs if allowed by sample-size policy; no PASS/FAIL shortcut.

### Scenario E — Engineering Slurm failure

- mock job fails because node/scheduler issue.

Expected: automatic identical resubmission within whitelist; no Supervisor scientific replan.

### Scenario F — Scientific compute failure

- SCF non-convergence requires changing scientific settings.

Expected: worker reports facts; no automatic parameter change; Supervisor review required.

### Scenario G — Monitor crash/recovery

- Monitor session disappears while jobs run.
- Watchdog tries resume; simulate resume failure.
- replacement Monitor reconstructs state from checkpoint/events/external adapter.

Expected: no duplicate Run/job and no lost completion event.

### Scenario H — Delayed lab result

- Experiment Worker dispatches and exits.
- Result returns later.

Expected: Monitor detects incoming package and spawns follow-up analysis; original worker not required.

### Scenario I — Missing critical method parameter

- parameter absent from paper/SI/related sources.

Expected: Assumption Registry classification; A2 prevents pure strict labeling; optional author contact remains disabled unless Human Gate is opened.

### Scenario J — Plan completeness failure

- one SI result intentionally left unmapped.

Expected: Plan v1 freeze prohibited until inventory audit is repaired.

## 4. v0.1 release gates

All must pass:

1. Core schemas validate.
2. FDM planning benchmark passes inventory audit.
3. All scenarios A–J pass.
4. Worker permission tests prove forbidden actions are rejected by runtime/governance layer, not just by prompt text.
5. Monitor recovery works with mock platform adapter.
6. Claude Code adapter can execute at least one real spawn/resume path or documented fallback.
7. Codex adapter interface exists and passes mock contract tests; real-platform verification should be attempted where available.
8. Final machine-auditable package validates traceability.

## 5. Development-orchestration acceptance (historical — v0.1.0 development campaign)

The v0.1.0 implementation campaign ran in `AUTO_RUN_M0_TO_M13` mode under an
autonomous Development Supervisor (recorded on the `main` branch; the
development record is not part of this release). For that campaign, before
calling the implementation handoff successful, verify:

1. M0–M13 were represented as milestones rather than monolithic worker tasks.
2. Atomic development goals were delegated to independent workers where platform capability allowed.
3. Worker completion was independently re-verified by the Development Supervisor.
4. At least one rejected development attempt can be repaired/retried without weakening its frozen acceptance criteria.
5. The Supervisor automatically unlocked downstream work after milestone/goal acceptance.
6. The Supervisor did not require routine user confirmation between milestones.
7. Agent Team task state was not used as the sole development source of truth.
8. If Agent Teams were unavailable or lost after session resume, the development-state directory allowed replacement workers and continuation.
