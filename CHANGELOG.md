# Changelog

All notable changes are tracked here. This repository follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) conventions with a single immutable release line per version.

## [Unreleased]

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
