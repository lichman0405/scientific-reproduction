# 23 — Development Goal Catalog

> Normative source: `development/development-plan.v1.yaml`, `development/milestones/*.yaml`, and `development/goals/*.yaml`.

M0–M13 are milestones. Every item below is an atomic development `/goal` contract to be assigned to an independent worker and independently accepted by the Development Supervisor.

## M0 — Repository bootstrap

Milestone dependencies: none

### DEV-M0-G00 — Establish GitHub branch, PR, CI, and merge governance

Establish the repository-level workflow that makes one short-lived branch and one pull request the mandatory integration path for every repository-changing DEV-GOAL after the initial specification-only bootstrap commit.

**Acceptance:**
- Pull requests targeting `main` run deterministic governance/spec validation CI.
- PR template captures Goal ID, acceptance evidence, verification, architecture impact, and limitations.
- Normal direct product commits to `main` and Worker self-merge are forbidden.
- This bootstrap governance goal itself is delivered through a `ci/DEV-M0-G00-*` branch and PR.
- Development contracts receive machine validation.

**Verification:**
- `python scripts/validate_development_contracts.py`

### DEV-M0-G01 — Bootstrap Python package and repository layout

Create the minimal installable repository structure and deterministic development entry points without implementing scientific runtime behavior.

**Acceptance:**
- Package installs in a fresh virtual environment.
- A minimal smoke test imports the package successfully.
- Repository layout matches the handoff architecture without premature subsystem implementation.

**Verification:**
- `python -m pip install -e .`
- `python -m pytest -q`

### DEV-M0-G02 — Configure lint, type and test tooling

Establish one reproducible command set for unit tests, lint/static checks, and full verification.

**Acceptance:**
- Unit-test command exits 0 on bootstrap suite.
- Lint command exits 0.
- Type/static command exits 0 or is explicitly documented as intentionally deferred with a testable replacement.
- One cross-platform verify command runs the configured checks.

**Verification:**
- `python -m pytest -q`
- `python -m ruff check .`

### DEV-M0-G03 — Import normative specification into implementation docs

Copy or reference the implementation specification so future workers can resolve locked contracts locally.

**Acceptance:**
- Locked architecture decisions are discoverable from repository root.
- No normative requirement is silently altered while copying.
- An ADR template exists for implementation deviations.

**Verification:**
- `python -m pytest -q`

### DEV-M0-G04 — Bootstrap install and verification smoke tests

Add automated checks proving M0 can be reproduced from a clean environment.

**Acceptance:**
- Fresh-environment install path is covered by an automated or hermetic test.
- Canonical verify command returns success on the bootstrap repository.

**Verification:**
- `python -m pytest -q`
- `python -m ruff check .`

## M1 — Core object model and filesystem state

Milestone dependencies: M0

### DEV-M1-G01 — Implement deterministic IDs and typed core models

Implement deterministic identifier helpers and typed representations for all normative project objects without embedding workflow decisions in model constructors.

**Acceptance:**
- All normative object types can be represented and serialized.
- IDs are deterministic under the documented input contract and collision behavior is tested.
- Schema-invalid objects are rejected before persistence.

**Verification:**
- `python -m pytest -q tests/core`
- `python -m ruff check .`

### DEV-M1-G02 — Implement filesystem StateBackend CRUD and atomic writes

Implement per-object filesystem persistence using atomic replacement and no monolithic mutable project-state file.

**Acceptance:**
- Objects are persisted per object/type, not in one mutable state blob.
- Interrupted/partial writes do not replace the last valid object.
- Read-after-write returns schema-valid canonical content.

**Verification:**
- `python -m pytest -q tests/state`

### DEV-M1-G03 — Implement leases and file locks

Implement bounded per-object leases/locks that prevent duplicate ownership while allowing expiry/recovery.

**Acceptance:**
- Two concurrent claimers cannot both acquire the same valid lease.
- Expired leases can be recovered deterministically.
- Lease metadata identifies owner and expiry.

**Verification:**
- `python -m pytest -q tests/state -k lease`

### DEV-M1-G04 — Implement append-only event log

Implement append-only, ordered, idempotent project events suitable for recovery and audit.

**Acceptance:**
- Existing event records are never mutated in normal operation.
- Duplicate event submission with same idempotency key does not create duplicate semantic event.
- Ordering is deterministic and recoverable.

**Verification:**
- `python -m pytest -q tests/state -k event`

### DEV-M1-G05 — Harden state recovery and invalid-write behavior

Add failure-injection tests and recovery behavior around filesystem persistence.

**Acceptance:**
- Last valid state survives simulated interrupted write.
- Invalid schema write cannot become canonical state.
- Corrupt external edits surface a deterministic error instead of silent repair.

**Verification:**
- `python -m pytest -q tests/state`

### DEV-M1-G06 — Run M1 concurrency and object-model integration suite

Integrate models, persistence, events and leases into a coherent tested core.

**Acceptance:**
- Concurrent duplicate lease scenario passes.
- Object create/read/update/version path remains schema-valid.
- All M1 tests and repository regression checks pass.

**Verification:**
- `python -m pytest -q tests/core tests/state tests/integration`
- `python -m ruff check .`

## M2 — Lifecycle and rule engine

Milestone dependencies: M1

### DEV-M2-G01 — Implement project and Run lifecycle state machines

Encode legal project phases and Run transitions as deterministic rules.

**Acceptance:**
- Every normative legal transition is accepted.
- Representative illegal shortcut transitions are rejected.
- Transition code is deterministic and not prompt-dependent.

**Verification:**
- `python -m pytest -q tests/rules -k lifecycle`

### DEV-M2-G02 — Implement dependency, execution-gate and acceptance-gate evaluation

Implement hard/soft/informational dependency semantics and separate execution vs acceptance gates.

**Acceptance:**
- Hard gate blocks execution when unresolved.
- Soft/informational dependencies do not incorrectly serialize the DAG.
- Acceptance can remain blocked after execution is allowed.

**Verification:**
- `python -m pytest -q tests/rules -k depend`

### DEV-M2-G03 — Implement evidence rubric rule engine

Implement structured Authority/Reliability/Directness calculation hooks, with Reliability derived from checklist inputs rather than direct subjective assignment.

**Acceptance:**
- Reliability score cannot be produced without checklist inputs/reference.
- Source×Claim assessment is supported.
- Hard-gate predicates can be evaluated independently of any weighted display score.

**Verification:**
- `python -m pytest -q tests/rules -k evidence`

### DEV-M2-G04 — Implement criticality checklist mapping

Implement checklist-driven CRITICAL/REQUIRED/SUPPORTING mapping.

**Acceptance:**
- Main-figure location alone cannot force CRITICAL.
- Same checklist yields deterministic classification.
- Classification inputs remain auditable.

**Verification:**
- `python -m pytest -q tests/rules -k critical`

### DEV-M2-G05 — Implement Closure Contract evaluator

Implement closure hard-gate logic including statistics sufficiency, valid execution, recovery hypothesis exhaustion and research saturation.

**Acceptance:**
- Closure fails when any mandatory gate is unresolved.
- No fixed failure-count shortcut can close a required goal.
- Eligible recovery hypothesis remaining >0 prevents non-reproduced closure.

**Verification:**
- `python -m pytest -q tests/rules -k closure`

### DEV-M2-G06 — Implement requirement/project outcome aggregation

Implement Requirement outcome and project reproduction_outcome aggregation separately from project_phase.

**Acceptance:**
- project_phase never substitutes for reproduction_outcome.
- Final outcome remains UNDETERMINED before final validation rules allow determination.
- Criticality affects project aggregation according to locked specification.

**Verification:**
- `python -m pytest -q tests/rules -k outcome`

### DEV-M2-G07 — Implement Assumption Registry effects

Implement A0/A1/A2 classification effects on strict-reproduction labeling and audit.

**Acceptance:**
- A0 does not change scientific strict identity.
- A1 is recorded and can classify strict-with-assumptions.
- A2 prevents pure STRICT labeling.

**Verification:**
- `python -m pytest -q tests/rules -k assumption`

### DEV-M2-G08 — Run normative lifecycle and rule-engine integration suite

Prove all M2 rule modules compose without scientific shortcut transitions.

**Acceptance:**
- All M2 rule tests pass.
- At least one test covers each locked shortcut prohibition.
- Repository regression checks remain green.

**Verification:**
- `python -m pytest -q tests/rules tests/integration`
- `python -m ruff check .`

## M3 — Git audit and artifact manifests

Milestone dependencies: M1

### DEV-M3-G01 — Implement project Git initialization and scientific audit commits

Implement Git initialization and explicit audit commit helper for scientific/governance checkpoints, excluding heartbeats.

**Acceptance:**
- Plan freeze produces an auditable commit.
- Goal/protocol revision can produce an auditable commit.
- Heartbeat/runtime polling does not create Git commits.

**Verification:**
- `python -m pytest -q tests/audit -k git`

### DEV-M3-G02 — Implement artifact manifest and checksum registry

Implement artifact registration with URI/path, size, checksum, run linkage and metadata.

**Acceptance:**
- Registered artifact checksum validates.
- Missing or changed artifact is detected.
- Manifest can point outside Git repository.

**Verification:**
- `python -m pytest -q tests/artifacts`

### DEV-M3-G03 — Prove large artifact exclusion and audit linkage

Add integration tests showing large raw artifacts stay outside normal Git history while manifests and scientific audit remain traceable.

**Acceptance:**
- Dummy large artifact is not committed to Git.
- Manifest is committed/auditable at required checkpoint.
- Checksum detects post-registration modification.

**Verification:**
- `python -m pytest -q tests/audit tests/artifacts tests/integration`

## M4 — Project initialization and Supervisor planning primitives

Milestone dependencies: M2, M3

### DEV-M4-G01 — Implement /reproduce project initialization primitives

Implement deterministic APIs/CLI primitives to initialize one-paper reproduction projects and register the primary target.

**Acceptance:**
- One Primary Target Paper is enforced per project.
- Project initializes Git/state structures without requiring lab/HPC inventory.
- PDF/DOI/URL target forms can be represented.

**Verification:**
- `python -m pytest -q tests/planning -k init`

### DEV-M4-G02 — Implement Reproduction Inventory registration and mapping

Implement inventory item registration and mapping to Requirements and Goals.

**Acceptance:**
- Formal reported items can map to one or more Goals.
- Unmapped/ambiguous counts are deterministic.
- Mappings preserve source location/provenance references.

**Verification:**
- `python -m pytest -q tests/planning -k inventory`

### DEV-M4-G03 — Implement completeness audit

Implement 100% formal-item coverage audit that blocks Plan v1 freeze when any item is unmapped or unresolved.

**Acceptance:**
- One intentionally unmapped item prevents freeze eligibility.
- 100% mapped and zero ambiguous items passes.
- Audit evidence lists offending item IDs.

**Verification:**
- `python -m pytest -q tests/planning -k completeness`

### DEV-M4-G04 — Implement Plan v1 construction and frozen contracts

Implement Plan creation, Goal/Acceptance/Analysis/Closure freezing and immutable revision semantics.

**Acceptance:**
- Freeze is prohibited without passing completeness audit.
- Frozen Goal/Acceptance/Analysis/Closure objects reject direct mutation.
- Formal revision creates a new version and preserves old record.

**Verification:**
- `python -m pytest -q tests/planning -k freeze`

### DEV-M4-G05 — Implement resource blockers and planning DAG export

Integrate Resource objects and semantic dependencies into a plan DAG suitable for /goals views and execution.

**Acceptance:**
- AVAILABLE/PROCURE/OUTSOURCE/CAPABILITY_GAP states are representable.
- Resource gap can block a Goal without altering scientific acceptance.
- DAG distinguishes hard/soft/informational and execution/acceptance gates.

**Verification:**
- `python -m pytest -q tests/planning -k dag`

### DEV-M4-G06 — Run planning primitive integration acceptance

Integrate initialization, inventory, audit, freeze and DAG behavior on synthetic fixture.

**Acceptance:**
- Incomplete synthetic inventory fails freeze.
- Complete inventory produces frozen Plan v1.
- Frozen plan can be reloaded from filesystem state and audited.

**Verification:**
- `python -m pytest -q tests/planning tests/integration`

## M5 — Research subsystem

Milestone dependencies: M2

### DEV-M5-G01 — Implement normalized Source records and deduplication

Implement canonical source identity and duplicate DOI/mirror collapse without discarding distinct versions.

**Acceptance:**
- Duplicate DOI mirrors collapse to one canonical scholarly source.
- Distinct SI/dataset/structure records remain separately addressable.
- Source provenance for mirrors is retained.

**Verification:**
- `python -m pytest -q tests/research -k source`

### DEV-M5-G02 — Implement bootstrap Research workflow and Research Requests

Implement records/contracts for initial systematic acquisition and Supervisor-issued Research Requests.

**Acceptance:**
- Bootstrap workflow covers paper/SI/data/structure/citations/related methods categories.
- Research Requests can only be formally issued through Supervisor-facing API.
- Request/result linkage is auditable.

**Verification:**
- `python -m pytest -q tests/research -k request`

### DEV-M5-G03 — Implement claim-specific Evidence records

Implement Source×Claim evidence records and linkage to Goals/Requirements.

**Acceptance:**
- A source cannot have a single global reliability field standing in for all claims.
- Same source can carry different Directness/Reliability assessments for different claims.
- Used-by links are stored.

**Verification:**
- `python -m pytest -q tests/research -k evidence`

### DEV-M5-G04 — Implement Reliability checklist workflow

Implement structured reliability checklist persistence and rule-derived score reference.

**Acceptance:**
- Evidence Reliability cannot be accepted without checklist record/reference.
- Checklist records raw-data, replication, uncertainty, method completeness, validation and consistency factors.
- Rule result is reproducible from stored checklist.

**Verification:**
- `python -m pytest -q tests/research -k reliability`

### DEV-M5-G05 — Implement search cycles and saturation records

Implement repeatable search-cycle records and the frozen two-zero-novelty-cycle saturation rule.

**Acceptance:**
- One zero-novelty cycle is insufficient.
- Two consecutive completed expansion cycles with zero new eligible hypotheses satisfy v0.1 saturation.
- Eligible hypothesis threshold can reference Reliability/Directness rules.

**Verification:**
- `python -m pytest -q tests/research -k saturation`

### DEV-M5-G06 — Define public ResearchAdapter interfaces and first open-source fixtures

Implement adapter contracts for open scholarly/public database acquisition with mock/offline fixtures; commercial adapters remain optional.

**Acceptance:**
- Missing commercial adapter does not block core research workflow.
- Adapter outputs normalize to Source records.
- Public adapter contract is testable without live network.

**Verification:**
- `python -m pytest -q tests/adapters/research tests/research`

## M6 — Worker context and permission enforcement

Milestone dependencies: M4, M5

### DEV-M6-G01 — Implement Goal Execution Context Package generator

Generate minimal necessary worker context from frozen Goal plus explicitly relevant sources, evidence, upstream outputs and policies.

**Acceptance:**
- Worker context contains the frozen Goal Contract and required policies.
- Unrelated project documents are not included by default.
- Context package records exactly which references were exposed.

**Verification:**
- `python -m pytest -q tests/workers -k context`

### DEV-M6-G02 — Implement Worker Result Package contract

Implement normalized experiment/computation/analysis worker return package registration.

**Acceptance:**
- Result package distinguishes facts/data/deviations from Supervisor decisions.
- Worker cannot encode authoritative PASS/FAIL for Requirement.
- Artifacts can be linked through manifests.

**Verification:**
- `python -m pytest -q tests/workers -k result`

### DEV-M6-G03 — Implement role permission enforcement

Enforce Supervisor/Research/Monitor/Execution/Analysis authority boundaries in deterministic runtime APIs.

**Acceptance:**
- Experiment/Computation worker cannot mutate frozen Goal.
- Worker cannot create formal Recovery Goal or close Requirement.
- Research cannot make scientific Plan decisions.
- Monitor cannot make scientific interpretation decisions.

**Verification:**
- `python -m pytest -q tests/workers -k permission`

### DEV-M6-G04 — Implement engineering retry authorization boundary

Expose pre-authorized automatic retry whitelist while routing scientific parameter changes to Supervisor.

**Acceptance:**
- Transient SSH/network/scheduler node failure can be authorized when contract allows.
- Identical checkpoint continuation can be authorized without scientific change.
- Scientific parameter modifications are rejected for automatic worker action.

**Verification:**
- `python -m pytest -q tests/workers -k retry`

### DEV-M6-G05 — Run worker isolation and permission integration suite

Prove worker context minimization and authority boundaries with adversarial fixtures.

**Acceptance:**
- Forbidden plan mutation is blocked by runtime, not only prompt instructions.
- Worker context leak fixture fails if unrelated refs are exposed.
- All M6 tests pass.

**Verification:**
- `python -m pytest -q tests/workers tests/integration`

## M7 — Lab and Compute adapters

Milestone dependencies: M2, M6

### DEV-M7-G01 — Implement filesystem LabAdapter reference flow

Implement outgoing experiment package dispatch and incoming result package detection/collection using filesystem handoff.

**Acceptance:**
- Experiment Execution Package can be dispatched to outgoing path.
- Returned package is associated with correct Run.
- Missing required result manifest/data is detected.

**Verification:**
- `python -m pytest -q tests/adapters/lab`

### DEV-M7-G02 — Implement Local ComputeAdapter

Implement local process execution adapter with persistent external-job identity abstraction.

**Acceptance:**
- prepare/submit/status/collect/cancel/resume interface is implemented.
- Job identity persists independently of worker session object.
- Result collection produces artifact registrations.

**Verification:**
- `python -m pytest -q tests/adapters/compute -k local`

### DEV-M7-G03 — Implement SSH ComputeAdapter transport layer

Implement SSH execution/file-transfer abstraction with test doubles and transient failure classification.

**Acceptance:**
- Transient connection failure is distinguishable from scientific job failure.
- Credentials are not persisted in project state.
- Mock SSH integration covers reconnect behavior.

**Verification:**
- `python -m pytest -q tests/adapters/compute -k ssh`

### DEV-M7-G04 — Implement Slurm-over-SSH ComputeAdapter

Implement Slurm submit/status/accounting/cancel/collect flow over SSH with Modules-aware execution metadata.

**Acceptance:**
- External Slurm job ID persists after submitting Worker exits.
- PENDING/RUNNING/COMPLETED/FAILED scheduler states normalize correctly.
- Scientific input files are not modified by scheduler retry logic.

**Verification:**
- `python -m pytest -q tests/adapters/compute -k slurm`

### DEV-M7-G05 — Run Lab/Compute adapter end-to-end fixtures

Validate delayed lab result and mock Slurm lifecycle across state backend.

**Acceptance:**
- Lab dispatch can outlive original worker and later be collected.
- Slurm-style job can outlive original worker and later be reconciled.
- Run state references external IDs deterministically.

**Verification:**
- `python -m pytest -q tests/adapters tests/integration`

## M8 — Execution Monitor and recovery

Milestone dependencies: M7, M6

### DEV-M8-G01 — Implement watched-Run registry and Monitor checkpoints

Implement Monitor-owned watched Run registry, heartbeat metadata and recovery checkpoint format.

**Acceptance:**
- Monitor can reconstruct watched external Runs from persisted state.
- Heartbeat/checkpoint updates do not require Git audit commits.
- Checkpoint references adapter/external IDs needed for reconciliation.

**Verification:**
- `python -m pytest -q tests/monitoring -k checkpoint`

### DEV-M8-G02 — Implement external status reconciliation and event emission

Implement deterministic polling/reconciliation across Lab/Compute adapters and append transition events.

**Acceptance:**
- External completion moves Run to RESULT_AVAILABLE exactly once.
- Unknown/temporary adapter state does not fabricate completion.
- Reconciliation is idempotent across restart.

**Verification:**
- `python -m pytest -q tests/monitoring -k reconcile`

### DEV-M8-G03 — Implement engineering retry whitelist in Monitor

Apply pre-authorized engineering recovery without scientific replan.

**Acceptance:**
- Whitelisted scheduler/node failure can trigger identical resubmission.
- Scientific compute failure does not trigger parameter mutation.
- Retry history remains auditable.

**Verification:**
- `python -m pytest -q tests/monitoring -k retry`

### DEV-M8-G04 — Implement Monitor session recovery/replacement contract

Implement resume/replacement interfaces and deterministic state reconstruction when original Monitor session disappears.

**Acceptance:**
- Replacement Monitor reconstructs watched Runs without original conversation.
- No duplicate external job is created during reconstruction.
- Completion event occurring during Monitor outage is reconciled later.

**Verification:**
- `python -m pytest -q tests/monitoring -k recovery`

### DEV-M8-G05 — Implement duplicate-trigger protection and follow-up worker request

Ensure a returned result creates at most one follow-up analysis/handling request under repeated reconciliation.

**Acceptance:**
- Repeated scan of same RESULT_AVAILABLE Run does not spawn duplicate semantic follow-up.
- Trigger record survives Monitor restart.

**Verification:**
- `python -m pytest -q tests/monitoring -k trigger`

### DEV-M8-G06 — Pass Monitor execution simulations E, G and H

Exercise engineering Slurm failure, Monitor crash/recovery and delayed lab result scenarios.

**Acceptance:**
- Scenario E performs identical engineering recovery only.
- Scenario G loses no Run/job/completion event after Monitor replacement.
- Scenario H does not require the original experiment worker to remain alive.

**Verification:**
- `python -m pytest -q tests/scenarios -k "E or G or H"`

## M9 — Analysis/statistics subsystem

Milestone dependencies: M2, M4

### DEV-M9-G01 — Implement frozen Primary Analysis Protocol records

Implement analysis protocol persistence, freeze/version rules and PRIMARY vs EXPLORATORY classification.

**Acceptance:**
- Primary protocol is frozen before data analysis acceptance.
- Exploratory analysis cannot overwrite/replace primary result.
- Formal protocol revision is versioned.

**Verification:**
- `python -m pytest -q tests/analysis -k protocol`

### DEV-M9-G02 — Implement Analysis Result Package and trace links

Implement analysis outputs linked to Runs, raw artifacts, protocol version and acceptance criterion.

**Acceptance:**
- Analysis result names exact input Run/artifact refs.
- Protocol version is immutable in result record.
- Result does not independently close Requirement.

**Verification:**
- `python -m pytest -q tests/analysis -k result`

### DEV-M9-G03 — Implement confidence interval and equivalence testing primitives

Implement basic quantitative validation interfaces for confidence intervals and equivalence logic without treating non-significance as equivalence.

**Acceptance:**
- p>0.05 alone can never produce REPRODUCED.
- Wide interval crossing equivalence bounds produces INCONCLUSIVE.
- Equivalence margins are inputs from frozen Acceptance Criteria, not inferred after results.

**Verification:**
- `python -m pytest -q tests/analysis -k statistic`

### DEV-M9-G04 — Implement independent-replicate and additional-run decision hook

Implement Supervisor-facing analysis result that can request additional independent Runs when precision/power criteria are not met.

**Acceptance:**
- Independent vs technical/instrument replicate are distinguished.
- Default n>=3 floor is enforceable for experimental Goals.
- Insufficient precision yields INCONCLUSIVE/additional-run request rather than forced PASS/FAIL.

**Verification:**
- `python -m pytest -q tests/analysis -k replic`

### DEV-M9-G05 — Implement computational convergence and sampling validation hooks

Implement generic convergence/sampling validation interfaces for DFT/GCMC/MD-style outputs.

**Acceptance:**
- Scientific convergence failure is representable without auto-changing parameters.
- Monte Carlo/sampling uncertainty hook can report uncertainty.
- Validation output can feed Supervisor acceptance.

**Verification:**
- `python -m pytest -q tests/analysis -k computational`

### DEV-M9-G06 — Pass analysis simulations D and F

Execute inconclusive statistics and scientific compute failure scenarios.

**Acceptance:**
- Scenario D cannot be coerced to PASS and can request more independent Runs.
- Scenario F reports scientific compute failure without automatic parameter mutation.

**Verification:**
- `python -m pytest -q tests/scenarios -k "D or F"`

## M10 — Agent contracts and platform adapters

Milestone dependencies: M6, M8

### DEV-M10-G01 — Implement runtime agent role contracts

Turn Supervisor/Research/Monitor/Worker specifications into reusable project agent/prompt contracts with tool/authority boundaries.

**Acceptance:**
- Scientific authority boundaries match locked role spec.
- Agent contracts reference Core state rather than conversation memory as truth.
- Worker contract forbids plan mutation and self-acceptance.

**Verification:**
- `python -m pytest -q tests/platform -k contract`

### DEV-M10-G02 — Implement platform orchestration interface

Implement platform-neutral spawn/resume/stop/message/fallback interfaces.

**Acceptance:**
- Core never imports Claude-specific task APIs directly.
- Platform adapter returns durable-enough session/worker identity for project state.
- Fallback mode is explicit.

**Verification:**
- `python -m pytest -q tests/platform -k base`

### DEV-M10-G03 — Implement Claude Code orchestration adapter

Implement/test Claude Code persistent-role and dynamic-worker orchestration using currently available Agent Teams/session resume mechanisms with documented fallbacks.

**Acceptance:**
- Adapter can demonstrate at least one real or hermetically mocked spawn path.
- Resume/replacement behavior does not make Agent Team task store authoritative.
- Known Agent Teams resume limitation is explicitly reconciled.

**Verification:**
- `python -m pytest -q tests/platform -k claude`

### DEV-M10-G04 — Implement Claude Code quality-gate hook templates

Provide TaskCompleted/TeammateIdle hook templates or generated configuration for deterministic task gates.

**Acceptance:**
- Task completion can be blocked on failing deterministic verification.
- Hook failure feeds actionable feedback rather than marking task complete.
- Hooks do not replace Supervisor review.

**Verification:**
- `python -m pytest -q tests/platform -k hook`

### DEV-M10-G05 — Implement Codex platform contract adapter

Implement the same platform-neutral Core contract for Codex to the extent environment access allows, with mock contract tests and no scientific-core fork.

**Acceptance:**
- Codex adapter satisfies PlatformAdapter contract in tests.
- No duplicate scientific logic exists in adapter.
- Unavailable live capability is reported as capability/fallback, not silently faked.

**Verification:**
- `python -m pytest -q tests/platform -k codex`

### DEV-M10-G06 — Run cross-platform orchestration contract acceptance

Verify both adapters against shared orchestration contract and role-permission behavior.

**Acceptance:**
- Claude and Codex adapters pass the same core contract tests where capabilities overlap.
- Platform-specific fallback differences do not alter Goal/Run/Evidence semantics.

**Verification:**
- `python -m pytest -q tests/platform tests/integration`

## M11 — Materials Chemistry domain pack

Milestone dependencies: M4, M5, M9

### DEV-M11-G01 — Implement materials-chemistry synthesis templates

Implement domain templates/rules for ligand/material/MOF synthesis, activation, independent batch replication and protocol capture.

**Acceptance:**
- Templates support strict/recovery labeling and independent Runs.
- Missing scientific parameters enter Assumption Registry pathway.
- Templates do not hardcode FDM-201-specific chemistry as universal rule.

**Verification:**
- `python -m pytest -q tests/domain/materials_chemistry -k synth`

### DEV-M11-G02 — Implement characterization templates

Implement PXRD, SCXRD, TGA and spectroscopy characterization planning/analysis metadata templates.

**Acceptance:**
- Templates define required raw data/instrument metadata.
- Analysis protocol and acceptance can be frozen separately from execution.
- PXRD identity/quality checks can be represented without a worker self-decision.

**Verification:**
- `python -m pytest -q tests/domain/materials_chemistry -k character`

### DEV-M11-G03 — Implement gas adsorption/separation templates

Implement BET, gas adsorption, IAST, Qst, breakthrough, cycling and stability templates.

**Acceptance:**
- Temperature/pressure/composition conditions are explicit Unit Process inputs.
- BET/IAST/Qst analysis remains separate from raw execution.
- Breakthrough raw/result metadata can map to formal paper figures/results.

**Verification:**
- `python -m pytest -q tests/domain/materials_chemistry -k adsorption`

### DEV-M11-G04 — Implement DFT/GCMC/MD domain metadata templates

Implement computational materials templates for structure preparation, DFT, GCMC and MD execution/validation metadata.

**Acceptance:**
- Software/method/force-field/functionals and convergence inputs are explicitly captured or assumed via registry.
- Execution and post-processing are separate.
- Templates are compatible with Slurm/Modules metadata.

**Verification:**
- `python -m pytest -q tests/domain/materials_chemistry -k comput`

### DEV-M11-G05 — Implement materials statistics/acceptance defaults as proposal rules

Implement domain-pack proposal helpers for replicate design, measurement uncertainty metadata and acceptance construction without universal fixed ±percentage rules.

**Acceptance:**
- No universal ±10% acceptance is hardcoded.
- Default n>=3 independent experimental replicate floor is proposed.
- Supervisor can incorporate literature/method evidence before freezing.

**Verification:**
- `python -m pytest -q tests/domain/materials_chemistry -k statistic`

### DEV-M11-G06 — Prove FDM-201 expressiveness of domain pack

Construct a non-final fixture demonstrating the domain pack can represent every FDM-201 work-package category before the full M12 benchmark.

**Acceptance:**
- Synthesis/structure/PXRD/TGA/BET/adsorption/IAST/Qst/breakthrough/stability/computation categories are representable.
- No required category needs an ad hoc schema fork.

**Verification:**
- `python -m pytest -q tests/domain/materials_chemistry`

## M12 — FDM-201 reference benchmark

Milestone dependencies: M3, M4, M5, M6, M7, M8, M9, M10, M11

### DEV-M12-G01 — Acquire and register FDM-201 benchmark sources

Use DOI 10.1039/D5TA00771B to acquire/register the primary paper, accessible SI/CIF/data and supporting source set needed for real planning benchmark.

**Acceptance:**
- Primary paper identity is correct.
- Accessible SI/structure/data are registered or explicit unavailability is recorded.
- Supporting sources are normalized through Research subsystem.

**Verification:**
- `python -m pytest -q tests/benchmarks -k fdm201`

### DEV-M12-G02 — Build real FDM-201 Reproduction Inventory

Create formal reported-item inventory from main text/SI/linked public data with source locations and requirement mapping candidates.

**Acceptance:**
- Every formally reported experiment/control/computation discovered in accessible sources is inventoried.
- Items include source location/provenance.
- Ambiguities are explicit rather than silently omitted.

**Verification:**
- `python -m pytest -q tests/benchmarks -k inventory`

### DEV-M12-G03 — Complete FDM-201 100% inventory mapping audit and Plan v1

Map all formal items to Requirements/Goals, create resources/assumptions/evidence/acceptance/analysis/closure records and freeze benchmark Plan v1.

**Acceptance:**
- Formally reported item mapping coverage is 100%.
- Unmapped items=0 and ambiguous items=0 at freeze, or source unavailability is formally handled per spec rather than ignored.
- Plan includes experiment and computation branches plus resource/assumption/evidence/analysis/closure objects.

**Verification:**
- `python -m pytest -q tests/benchmarks -k "fdm201 and plan"`

### DEV-M12-G04 — Generate FDM-201 experiment and compute execution packages

Generate representative/final planning execution packages from frozen Goals, including Lab filesystem and Slurm-oriented computation packages.

**Acceptance:**
- Packages validate against normative schemas.
- Scientific parameters trace to paper/evidence/assumptions.
- Packages do not expose unrelated project context by default.

**Verification:**
- `python -m pytest -q tests/benchmarks -k package`

### DEV-M12-G05 — Execute simulation scenarios A-E

Run strict success, recovery success, recovery exhausted, inconclusive statistics and engineering Slurm failure scenarios end-to-end.

**Acceptance:**
- A closes without Recovery.
- B ends REPRODUCED_WITH_RECOVERY with lower method reproducibility.
- C satisfies Closure before non-reproduced outcome.
- D remains inconclusive/adds runs.
- E uses engineering retry without scientific replan.

**Verification:**
- `python -m pytest -q tests/scenarios -k "A or B or C or D or E"`

### DEV-M12-G06 — Execute simulation scenarios F-J

Run scientific compute failure, Monitor recovery, delayed lab result, missing scientific parameter and plan completeness failure scenarios end-to-end.

**Acceptance:**
- F requires Supervisor scientific review.
- G recovers without duplicate job/Run.
- H handles late result without original worker.
- I uses Assumption Registry and blocks pure STRICT for A2.
- J blocks Plan freeze until completeness repaired.

**Verification:**
- `python -m pytest -q tests/scenarios -k "F or G or H or I or J"`

### DEV-M12-G07 — Run complete FDM-201 benchmark acceptance

Execute full benchmark verification and persist benchmark acceptance evidence.

**Acceptance:**
- 100% planning benchmark passes.
- All scenarios A-J pass in one clean run.
- Benchmark state can be reloaded and audited.

**Verification:**
- `python -m pytest -q tests/benchmarks tests/scenarios`

## M13 — Reporting and final audit

Milestone dependencies: M12, M10

### DEV-M13-G01 — Implement audit package validator and traceability checker

Implement machine-auditable package validation including key-claim backtrace to Analysis/Run/Artifact/Evidence.

**Acceptance:**
- Key report claim can be traced to Analysis -> Run -> Artifact/Evidence.
- Missing link fails validation.
- Failed Runs remain visible in package validation.

**Verification:**
- `python -m pytest -q tests/reporting -k "audit or trace"`

### DEV-M13-G02 — Implement human-readable reproduction report generator

Implement report generation covering scope, methods, statistics, strict/recovery history, failures, outcomes, method reproducibility and limitations.

**Acceptance:**
- Report distinguishes scientific outcome from method reproducibility.
- Material failed Runs/deviations are summarized rather than hidden.
- Report references auditable object IDs for key claims.

**Verification:**
- `python -m pytest -q tests/reporting -k report`

### DEV-M13-G03 — Implement final outcome and method-reproducibility summaries

Implement final summary rendering/serialization from already-evaluated Requirements without introducing new scientific decision logic in reporting.

**Acceptance:**
- Reporting reflects Core aggregation exactly.
- FULLY/PARTIALLY/NOT_REPRODUCED/INCONCLUSIVE are rendered without conflation with project_phase.
- Recovery levels are summarized.

**Verification:**
- `python -m pytest -q tests/reporting -k summary`

### DEV-M13-G04 — Implement packaging/install/user-operation documentation

Finalize installation, /reproduce, /goals, Monitor, Lab filesystem handoff, Slurm configuration and platform-adapter documentation.

**Acceptance:**
- A new implementer/operator can install and run tests from docs.
- Monitor recovery and filesystem Lab handoff are documented.
- Slurm-over-SSH configuration and limitations are documented.

**Verification:**
- `python -m pytest -q`
- `python -m ruff check .`

### DEV-M13-G05 — Run full v0.1 release gate and final development audit

Execute repository-wide verification, benchmark, scenario suite and development audit; persist final release verdict.

**Acceptance:**
- All M0-M13 milestone reviews are PASS.
- All required atomic development goals are PASS.
- Full unit/integration/scenario/benchmark/lint/static/install verification succeeds.
- No unresolved blocking Human Gate or audit discrepancy remains.
- Final implementation satisfies 18-TEST-AND-ACCEPTANCE-PLAN.md release gates.

**Verification:**
- `python -m pytest -q`
- `python -m ruff check .`

