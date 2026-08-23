# Changelog

All notable changes are tracked here. This repository follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions with a single immutable release line per version.

## [Unreleased]

## [0.2.3] - 2026-08-24

Operational-hardening release: 33 pull requests resolving issues
#133–#164 (including #136 part 3) — the execution chain is now
schema-gated end to end (typed Goal procedure, runtime-generated lab
and compute execution packages, worker-context ↔ execution-package
binding, dispatch verification against the frozen Goal), monitoring is
hardened (watchdog liveness, supervisor inbox, automatic retry-policy
consultation with persisted aftermath, per-run isolation and
concurrency control), and the planning/reporting path gains integrity
checks from research provenance to freeze-time
goal/requirement/dependency validation.

### Added

- **First-class acquisition status on source records** (issue #134) —
  `SourceRecord` now carries an `AcquisitionStatus` (`REGISTERED`,
  `OBTAINED`, `PARTIAL`, `UNAVAILABLE` — harmonized with the FDM-201
  benchmark) plus a stable `UnavailabilityReason` vocabulary; the
  registration event carries the fields, `list_sources` exposes them,
  and defaults keep legacy records unchanged.
- **Research-process narrative section in the final report** (issue
  #135) — the reproduction report gains a research-process section
  rendered from recorded research state only (bootstrap workflow,
  per-source acquisition statuses, request transitions, decision
  timeline) with an explicit empty-state line, in both the EN and ZH
  language packs.
- **First-class value-verification fidelity on inventory items** (issue
  #139) — inventory items carry a `ValueStatus` (`VERIFIED`,
  `REPORTED_NON_FINAL`, `UNKNOWN`) orthogonal to mapping status, with
  `missing_reason`/`resolves_in` detail; the FDM-201 reload preserves
  per-item fidelity and legacy serializations stay valid.
- **Human-readable experiment sheet in the dispatch handoff** (issue
  #157) — dispatching a lab package now renders the deterministic
  human-readable experiment sheet into the handoff directory (PDF +
  SHA-256 sidecar + HTML), and the machine-auditable package (v1.2)
  registers the new files.
- **Lab Execution Package generator from the frozen Goal** (issue #158)
  — a runtime generator derives the schema-gated `LabExecutionPackage`
  from the frozen Goal's typed procedure and execution constraints
  (issue #156), so every dispatch carries the frozen scientific content
  instead of a hand-assembled package.
- **Runtime compute execution package model, schema and gate** (issue
  #161) — the computation side gets its own `ComputeExecutionPackage`
  model and `compute-execution-package` schema (software/environment,
  scientific parameters from the frozen Goal — force field, k-point
  mesh, cutoffs, convergence criteria — input-file creation
  instructions, declared outputs, resource requirements), a
  deterministic generator symmetric with the lab generator, and a
  schema gate on the adapter handoff, backing 11-COMPUTATION-SUBSYSTEM
  §4 with a runtime artifact.
- **Durable supervisor inbox for arrived Result Packages** (issue #163)
  — a `SupervisorInbox` records durable entries for arrived Result
  Packages (keyed by run id / completion event id) with deterministic
  `list_inbox` and `mark_reviewed`; the reconcile completion path
  writes the inbox, and the role contracts and `SKILL.md` point
  supervisors at it.
- **Per-goal revision lineage** (issue #164) — `revise_goal` revises
  one goal and records its `parent_goal_id` lineage, and `revise_plan`
  reopens only the changed goals — goals equal to the frozen version
  stay frozen — with stable `GoalNotFound`/`GoalNotFrozen`/
  `GoalStateMismatch` checks.

### Fixed

- **Cross-registry referential integrity at research registration**
  (issue #133) — the file-backed facades now hard-reject records whose
  references dangle: `register_evidence` validates `source_id` against
  the source registry and `link_result_to_request` validates the linked
  `evidence_id`, both before any write (zero footprint on rejection).
- **Inventory provenance and linkage integrity at registration** (issue
  #136) — registration validates `source_id` and `linked_inventory_ids`
  before writing (parts 1–2, PR #175), and the freeze gate runs a
  bidirectional goal↔requirement back-reference check — every
  registered goal referenced by its requirements and every requirement
  back-referenced by its goal, with a documented exemption for umbrella
  audit/integration goals and the five divergent FDM-201 items repaired
  (part 3, PR #197).
- **Freeze gate inventory-reachability preconditions** (issue #137) —
  freezing now requires the project phase to have reached
  `REPRODUCTION_INVENTORY` (mainline rank) with at least one formally
  reported item recomputed; off-mainline phases cannot freeze, and the
  FDM-201 reload authors the phase so it still freezes.
- **Report recomputes inventory mapping status** (issue #138) — the
  report's scope section recomputes each item's mapping status through
  `evaluate_item_mapping` (with ambiguity notes) instead of trusting
  stored snapshots.
- **Freeze rejects drafts divergent from the registry** (issue #140) —
  both freshness-check branches validate the submitted plan's
  `goal_ids`/`requirement_ids` against the registry (stable error
  naming the divergent fields), and `revise_plan` re-derives the lists
  from registered state, so a v2 freeze can no longer persist stale
  lists next to a recomputed audit.
- **Freeze rejects orphan goal family records** (issue #141) — a
  registered goal with no requirement edge fails the freeze with a
  stable error naming the orphan goal ids, complementing the #136
  bidirectional check without ever folding orphans silently.
- **Freeze resolves and cycle-checks goal dependencies** (issue #142) —
  at freeze, unresolved `goal.dependencies` raise an
  `UnresolvedContractReferenceError` naming the goal and id, and
  hard-gate dependency cycles are detected by a DAG pass and rejected
  (`HardGateDependencyCycleError` naming the cycle); soft/informational
  edges stay execution-time.
- **Dead `PlanStatus.UNDER_AUDIT` vocabulary removed** (issue #143) —
  the never-produced status is gone from the enum, the schema, and
  every docstring.
- **Execution/acceptance gate engines wired to a runtime consumer**
  (issue #144) — `register_run` is now a real state-reading consumer of
  the gate engines: `BLOCKED` runs are refused and the matched rule id
  is recorded on the run record, with the heal-path non-enforcement
  deliberately documented.
- **Run-record concurrency control** (issue #145) — the per-run
  lease/lock machinery (`LeaseStore`/`FileLockStore`) is wired into
  `transition_run`, `link_run_to_dispatch`, and the reconcile
  completion write: a writer whose read is stale fails loudly instead
  of overwriting, and idempotent re-link semantics are preserved.
- **Lifecycle audit events on dispatch linkage** (issue #146) —
  `link_run_to_dispatch` appends one deterministic lifecycle audit
  event per arc (`run.lifecycle_change:<run_id>:<from>:<to>` ids)
  through a workspace-bound event log, preserving crash-window
  convergence.
- **Result package `run_ref` resolved against the run registry** (issue
  #147) — registering a result package resolves `run_ref` against the
  run registry before writing (after artifact refs), reusing the
  stable `UnresolvedWorkerResultReferenceError`.
- **`register_run` resolves `goal_id`/`goal_version`** (issue #148) —
  the run's goal reference is resolved pre-write with
  `GoalNotFound`/`GoalNotFrozen`/`GoalVersionMismatch` checks, and all
  `register_run` call sites were adapted.
- **Retry dispatcher consults the frozen automatic retry policy**
  (issue #149) — retry authorization is routed through the frozen
  policy evaluator, with attempt-indexed decision keys enforcing
  `max_identical_retries` and the policy's
  `invalidate_run_on`/`supervisor_required_changes` applied; a missing
  policy reference defaults to refusal.
- **Retry aftermath persisted to run record and watch entry** (issue
  #150) — after an authorized retry, the run record's
  `engineering_retries` gains the retry entry (a history update, not
  parameter mutation), the run and watch entry adopt the resubmitted
  external identity under the #145 lease discipline, and the report
  finally renders real retry counts.
- **Adapter-state → monitor-vocabulary bridge** (issue #151) — a frozen
  mapping table translates both adapter job-state vocabularies into
  the monitor's completion vocabulary, with failed/cancelled states
  never fabricating completion; the default probe routes through the
  mapped wrapper, and a consistency test locks both enums.
- **Pass-level per-run error isolation** (issue #152) — `reconcile_all`
  isolates a failed run's error from the rest of the pass, and
  `decide_all` classifies only `RUNNING_EXTERNAL` runs; `list_watched`
  keeps its whole-set loud failure with documented rationale (the
  watch set is project-level state).
- **Watchdog liveness verdict** (issue #153) — a deterministic watchdog
  primitive (injected clock and threshold) renders the liveness verdict
  from heartbeat state files: missing heartbeat = `DEAD`, corrupt state
  = loud error; `load_heartbeat` consumes it, and the execution-monitor
  contract wording now matches the implementation.
- **Watch-authoring helper resolves runs** (issue #154) — the new
  `watch_run` authoring helper resolves the run and enforces the
  lifecycle gate (`DISPATCHED`/`RUNNING_EXTERNAL`) before writing a
  watch entry; the registry primitive stays a pure state-dir helper.
- **Result Package completeness validator** (issue #155) — a
  deterministic rule-table validator checks each declared output's
  coverage in the returned manifest (id → name → unkeyable matching,
  `matched_rule_id` recorded) and is consulted before the
  `RESULT_AVAILABLE`→`ANALYZING` transition; a vacuous zero-output
  pass mirrors the planning/audit pattern.
- **Typed Goal contract procedure and execution constraints** (issue
  #156) — `GoalProcedureStep` and `GoalExecutionConstraints` are typed
  and schema-required at the persistence gate, with an
  accept-and-migrate empty-object path for legacy records.
- **Dispatch verifies packages against the frozen Goal** (issue #159) —
  `dispatch` resolves the package's `goal_id` against the goal registry
  before writing anything: nonexistent, unfrozen, or version-mismatched
  goals are rejected with stable typed errors and nothing is written.
- **Worker context bound to its execution package** (issue #160) —
  `execution_package_refs` is a first-class worker-context schema
  property (referenced packages must exist), `generate_goal_context`
  emits the link, and `context_hash` now fingerprints the linked
  protocol, so tampering with the procedure changes the hash.
- **Benchmark validator scope documented truthfully** (issue #162) —
  the execution-package AC-01/02/03 validator is explicitly scoped
  benchmark-only, with the runtime primitives that supersede it
  (dispatch verification #159, package generators #158/#161, context
  binding #160) documented — no silent gaps, no overstated claims.

### Verification

- Full test suite, ruff lint, and mypy type check via `python scripts/verify.py`.
- Skill smoke verification via `python scripts/smoke.py`.

## [0.2.2] - 2026-08-16

Issue-resolution release: seven more issues resolved — the pre-flight
skill update check, the sanctioned `close_requirement` API, parameterized
language packs (`zh`) and a first-class experiment-sheet PDF, cross-claim
evidence resolution, the goal-contract protocol registry bridge, the
frozen goal version on lab execution packages, and workspace-bound audit
events by default.

### Added

- **Pre-flight skill update check** (issue #117) — the skill directory
  is a git clone of the release branch, so any push to it makes every
  installed copy outdated; `scripts/check-update.py` now surfaces that
  signal before a reproduction starts. The deterministic, read-only
  script (no working-tree mutation) fetches
  `origin/release/skill-v0.2.0`, prints the local version, the latest
  available version and the N-behind commit count, and exits `0` (up to
  date), `1` (update available, with apply instructions) or `2`
  (cannot check: offline / not a clone / missing origin or release
  branch / unreadable version). `SKILL.md`,
  `templates/SKILL.md.template` and `AGENTS.md` carry the Pre-flight
  step: agents run the check before `/reproduce`, report an available
  update to the user, and apply it only after explicit confirmation via
  `git merge --ff-only` (refused on a dirty worktree, never forced),
  gated by `python scripts/smoke.py` (`[smoke] PASS`). The reproduction
  path itself stays network-free; the check is covered by exit-code
  contract tests (`tests/scripts/`) and demonstrated end to end in
  scenario K (`tests/scenarios/test_K_skill_update_flow.py`), and
  `scripts/smoke.py` asserts the script is part of the skill structure.
- **Sanctioned `close_requirement` API** (issue #120) — the Supervisor
  can now close a Requirement with a final `RequirementOutcome` through
  the registry: `planning.inventory.close_requirement` persists the
  rewritten record atomically (schema-validated), enforces the normative
  closure rules from `core/rules/outcome.py` (an `OPEN` requirement is
  refused before any write), appends one deterministic
  `requirement.outcome.updated` event under an idempotency key (the
  declared audit event of the "Requirement outcome updated" git
  checkpoint), and is exactly-once with crash-window convergence — the
  hand-rolled `store.write` path is no longer needed.
- **Parameterized language packs and first-class experiment-sheet PDF**
  (issue #122) — every operator-/human-facing renderer now takes an
  explicit injected `language` (default `"en"`, never locale-detected,
  so `(state, language)` → byte-identical output) resolved through the
  frozen `TemplatePack` mechanism in `reporting/language.py`; a full
  `zh` pack ships for the experiment/computation sheets and the
  reproduction report (manifest content is data and never translated).
  New `reporting.sheet_pdf.build_experiment_sheet_pdf` renders one
  deterministic PDF 1.4 per dispatched lab package through the shared
  `rendering` visual system, writing `experiment-sheet-<RUN_ID>.pdf` +
  a canonical JSON sidecar (SHA-256) auto-registered by the audit scan
  under `reports/`.

### Fixed

- **Cross-claim `evidence_refs` resolution** (issue #118) — the
  traceability resolver resolved an acceptance's `evidence_refs` only
  against the traced claim's own evidence records, so an acceptance
  legitimately citing evidence of a different claim (evidence is Source ×
  Claim per `06-EVIDENCE-SYSTEM.md`) produced false `trace_gap`
  validation errors; `evidence_refs` entries naming any registered
  evidence record (or a resolved analysis result) now resolve, while
  genuinely unresolved refs still gap.
- **Goal-contract protocol registry bridge** (issue #119) —
  `read_protocol_version` resolves the goal-contract family's formal
  in-place records (frozen by `freeze_plan`) by exact `protocol_version`
  match, so a project frozen through the public planning flow can
  register analysis results without private `_write_versioned` calls;
  already-frozen projects resolve unchanged, and the loose
  `"v1-draft"`-after-freeze read now correctly raises
  `ProtocolNotFoundError`.
- **Frozen goal version on lab execution packages** (issue #121) —
  `LabExecutionPackage` and `schemas/lab-execution-package.schema.yaml`
  carry the frozen `goal_version` (optional, backwards compatible:
  existing manifests keep validating, `to_dict()` omits unset values),
  the experiment execution sheet renders it (manifest-first, registered
  goal fallback, "not recorded" marker), and `dispatch` surfaces it in
  the manifest.
- **Workspace-bound audit events by default** (issue #123) — the
  registration/transition APIs (`research.state_helpers`,
  `workers.run_helpers`, `planning.inventory.close_requirement`)
  declared a workspace-bound `event_log` default that was never
  constructed, silently skipping every audit event on the normal call
  path; the default now resolves a `ProjectEventLog` bound to the
  workspace `events/` directory, restoring the ADR-0001 audit-trail
  guarantee.

### Verification

- Full test suite, ruff lint, and mypy type check via `python scripts/verify.py`.
- Skill smoke verification via `python scripts/smoke.py`.

## [0.2.1] - 2026-08-15

Issue-resolution release: every open issue is now resolved — the state tree
is canonical, freeze persistence and init guardrails are in place, per-role
tool allowlists enforce role boundaries, the statistical design record and
state/linkage helpers landed, research fetch policies are documented and
enforced, the Plan document renderer joined the renderer family, and the
three renderers now share one visual system (`rendering.style`). Release
branch CI gates are enabled and `main` is frozen in CONTRIBUTING.

### Added

- **Deterministic final reproduction report renderer** (issue #107) —
  `reporting.pdf_report.build_pdf_report` renders a review-ready PDF from
  recorded project state only: executive summary with verdict callout and
  the single most important number with its confidence interval vs the
  frozen acceptance band, target paper identity and reproduction scope,
  pipeline summary, per-requirement outcome tables with evidence trails
  (analysis records, decisions, closure-contract status), governance
  exercised (recovery ladder, recorded n-policy/margin statistical
  designs, supervisor decisions, monitor reconciliations), the audit
  trail (git state, frozen plan refs, checkpoint events, artifact
  manifests with full SHA-256 checksums), explicit simulation/real-data
  labeling, and a table of contents. Rendering is deterministic (no wall
  clock, no network, byte-identical for identical state; `generated_at`
  is injected) and stdlib-only (no dependencies at render time). The
  shared `rendering` package — a stdlib-only deterministic PDF 1.4
  writer (base-14 fonts, uncompressed greppable streams) — provides the
  visual system. The report lands in `reports/reproduction-report.pdf`
  with a canonical JSON sidecar `reports/reproduction-report.json`.
- **Deterministic designed Plan document renderer** (issue #105) —
  `reporting.plan_doc.render_plan_document` renders a print-ready designed
  Plan document from recorded planning state only: project/plan identity,
  goals as Unit Processes, the dependency DAG as an inline SVG diagram
  (hard/soft/informational edges with their own dash patterns), acceptance
  criteria with verdict placeholders, and the frozen plan annotations.
  Rendering is deterministic (no wall clock, no network, byte-identical
  for identical state; `generated_at` is caller-injected), stdlib-only,
  and draws its palette from the shared `rendering` visual system.
- **Report-file registration** — the machine-auditable package
  (`reporting.audit` v1.1) registers the files of the workspace
  `reports/` directory with SHA-256 checksums and sizes, sorted by name,
  so the report files are part of the audited state.
- **Operator-facing execution sheets** (`reporting.sheets`, issue #106) —
  deterministic print-ready A4 sheets rendered as pure functions of the
  registered state (stdlib-only, no wall clock/randomness/network; an
  optional `generated_at` stamp is caller-injected):
  - `reporting.sheets.html` — the shared A4 print visual system (dark-blue
    banner, red prohibited-changes block, amber safety block, fill-in
    fields, checkboxes, signatures, fixed print footer) any future
    renderer (plan sheets) must reuse.
  - `reporting.sheets.experiment.build_experiment_sheet` — one sheet per
    dispatched lab package, from the real outgoing handoff
    (`lab/outgoing/<RUN_ID>/`) plus the project/goal context: identity,
    objective, reagents/instruments tables, numbered procedure, critical
    control variables, the visually dominant prohibited-changes block
    (STRICT-track emphasis), safety notes, operator record fill-in
    fields, the exact `required_return` checklist tokens (issue #85),
    unknown manifest keys in an "Additional package data" section
    (1:1 manifest fidelity), signatures and the fixed footer.
  - `reporting.sheets.computation.build_computation_sheet` — one sheet
    per compute job, from the durable job record
    (`<state_dir>/jobs/<job_id>.json`, re-hydrated through the real
    `JobRecord`/`SSHJobRecord`/`SlurmJobRecord` contracts) plus the
    run → goal → acceptance → statistical-design chain (07 SS7/SS9):
    identity/job state, inputs, the verbatim shell-joined command,
    resource requests (Slurm modules/environment overrides, scheduler
    state), required outputs with the deterministic artifact-id rule and
    the convergence/validation criteria with "not registered" markers
    for absent links (never guessed).
  - Stable error surface (`SheetError` hierarchy) and `TypeError` at the
    public boundaries; canonical JSON sheet surfaces; byte-identical
    determinism. Benchmark acceptance: every FDM-201 experiment and
    computation package renders 1:1 (PDF-convertible full documents).
- **First-class statistical design record** (issue #89) — `planning`
  records the frozen acceptance statistics as a dedicated design record
  (`n-policy`, margin, method) on the goal contract family, so the
  statistics an analysis must meet are machine-usable, not free text.
- **`link_run_to_dispatch` run-record linkage helper** (issue #84) —
  adapters helper that records the run → dispatch linkage on the run
  record, so a dispatched lab/compute package and its returned run are
  connected in the durable state.
- **Fake-IP DNS fetch policy** (issue #98) — research adapters resolve
  fetch targets and refuse DNS names that resolve to fake-IP ranges
  (captive-portal / DNS-blocking space), with the policy documented and
  covered by tests.
- **Role state helpers** (issue #92) — Research and Worker/Monitor roles
  ship state helpers so role code reads and updates registered state
  through one canonical helper surface instead of ad-hoc registry access.

### Changed

- **Unified renderer visual system** — the three renderers (plan
  document, execution sheets, PDF report) now draw their color palette
  from one source of truth, `rendering.style`: the CSS hex tokens are
  derived from the same RGB constants the PDF writer uses, and the plan
  document and execution-sheet stylesheets consume them (`--ink`,
  `--accent`, `--fail`, …) instead of carrying private hex values.
- **CI PR gates on the release branch** — the "Test, lint, and type check"
  workflow now also triggers on pull requests against
  `release/skill-v0.2.0`, and CONTRIBUTING freezes `main` as the v0.1.0
  development record that never receives new commits.
- **`/goals` positioning documented** (issue #95) — the docs state
  explicitly that `/goals` views are agent-authored pure functions over
  planning state in v0.2, not a platform command.
- **Versioned `ranking_score` formula** (issue #97) — the evidence
  `ranking_score` formula is versioned and documented, and the documented
  example value was corrected to match the formula.

### Fixed

- **Primary-target metadata registration** — a PDF target carried only its
  local path on the project record at init; the new
  `planning.init.register_target_metadata` API is the first-class
  metadata-registration step of the research bootstrap (W-BOOT-1) and
  registers the paper DOI/title on the existing primary target record,
  making PDF target identity machine-usable for mirror collapse and evidence
  linking before Plan v1 (see `docs/adr/0001-primary-target-metadata-registration.md`).
- **Lab result-manifest coverage** — a dispatched package's `required_return` entries (raw-data-export tokens) can now be covered by an explicit `required_return_files` mapping in the returned result manifest (`{token: file name}`, the operator's declaration of coverage), so natural lab file names (`yield_pct.txt`, `2026-08-15_product_photo.jpg`) no longer need to be engineered to equal the token. The v1.0 exact-name rule remains the fallback for unmapped required returns; a malformed mapping is refused as corrupt operator data. Result manifest v1.1 / ruleset v1.1 (`adapters/lab/manifest.py`).
- **Canonical state tree layout** (issues #82/#109) — the state backend and
  the planning registries now converge on one canonical directory tree, so
  every subsystem reads and writes the same layout.
- **Frozen goal-contract persistence** (issue #108) — freezing a goal
  contract family persists the frozen records in place on disk; the freeze
  is durable, not memory-only.
- **Init guardrails** (issue #86) — `reproduce init` refuses non-empty
  project roots, and the package ships starter `.gitignore`/`.gitattributes`
  so a fresh workspace is git-ready from the first checkpoint.
- **Per-role tool allowlists** (issue #88) — role boundaries are enforced
  by per-role tool allowlists: the worker contracts list exactly the tools
  each role may call, and the enforcement layer rejects calls outside the
  allowlist.
- **Execution-phase checkpoint kinds** (issue #90) — the audit checkpoint
  vocabulary gains execution-phase kinds, so monitoring/recovery
  checkpoints are auditable in the machine-auditable package.
- **IP-literal fetch refusal** (issue #110) — research adapters refuse
  IP-literal fetch targets inside blocked networks (defense-in-depth
  alongside the fake-IP DNS policy).

### Verification

- Full test suite, ruff lint, and mypy type check via `python scripts/verify.py`.
- Skill smoke verification via `python scripts/smoke.py`.

## [0.2.0] - 2026-08-15

Skill-packaging release: this repository is now directly distributable as an installable skill.

### Added

- **`SKILL.md`** — skill manifest and entry instructions (Agent Skills standard; loadable by Claude Code and WorkBuddy).
- **`AGENTS.md`** — entry instructions for Codex.
- **`scripts/reproduce.py`** — zero-install `/reproduce` CLI wrapper (adds `src/` to `PYTHONPATH`; no venv or pip install required).
- **`scripts/smoke.py`** — platform-independent smoke verification: skill structure checks plus a real `reproduce init` run against the FDM-201 reference DOI with output assertions.

### Changed

- **Distribution form** — the repository root *is* the skill directory (`scientific-reproduction`); installation is copying the directory into `~/.claude/skills/` (Claude Code), `~/.workbuddy/skills/` (WorkBuddy), or opening it with Codex.
- **Docs** — `00-README.md` (distribution + reading order), `README.md`, `docs/spec/index.md` (skill-package section), `docs/README.md` (layout), `docs/user/*` (zero-install usage), `CONTRIBUTING.md` (standard branch/PR/CI governance), `docs/adr/README.md`, `docs/release/KNOWN-LIMITATIONS.md` (release-audit content trimmed to technical limitations), `pyproject.toml` (version 0.2.0).

### Removed

- Development-process artifacts (autonomous M0–M13 development record and governance): `.development/`, `development/`, specs `21-DEVELOPMENT-SUPERVISOR-SPEC.md` … `26-DEVELOPMENT-CHANGE-CI-RELEASE.md`, `START-CLAUDE-CODE.md`, `CLAUDE-CODE-HANDOFF.md`, `IMPLEMENTATION-CHECKLIST.md`, `PACKAGE-MANIFEST.json`, `SPEC-CHANGELOG.md`, `docs/release/` process records, `scripts/validate_development_contracts.py`, and the CI development-contract validation job. The v0.1.0 development record remains immutable in the `main` branch history.

### Verification

- Full test suite, ruff lint, and mypy type check via `python scripts/verify.py`.
- Skill smoke verification via `python scripts/smoke.py`.

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
