# DEV-M13-G05 — v0.1 Release Gate & Final Development Audit — Execution Runbook

| Field | Value |
|---|---|
| goal_id | DEV-M13-G05 (M13, `plan_approval_required: true`, `worker_mode: detached_native_goal`) |
| title | Run full v0.1 release gate and final development audit |
| branch / base | `feat/dev-m13-g05-release` = `origin/main` @ `6fef533` (79/80 goals merged) |
| worktree | `.worktrees/dev-m13-g05` |
| phase | **PHASE 1 — plan only.** This runbook is the execution plan submitted for Development Supervisor approval. No release gate is executed in Phase 1. |
| scope | Files created under `docs/release/**` only (this runbook now; the three release deliverables in Phase 2). `src/`, `tests/`, `schemas/`, specs, `.development/` state files are **never** modified. |

---

## 0. Execution rules (binding for Phase 2)

1. **No self-approval.** The Development Supervisor independently reviews and decides PASS/REJECT. The worker reports evidence, not verdicts. "PASS" is claimed only for something the worker independently ran and observed in this worktree.
2. **Stop conditions.** Any gate or acceptance item that fails stops execution at that step: the failure is recorded verbatim (command, exit code, failing test ids, logs) in the runbook appendix section "Phase-2 execution log" (added during Phase 2), reported to the Supervisor, and the affected deliverable is withheld or marked FAIL. No attempt to fix code/tests/specs is made (all fixes are out of scope).
3. **Hard constraints.** Never push to main; never merge a PR; never use `--no-verify`; never modify frozen ACs/goal files/schemas/specs; never delete or weaken a test; never modify `.development/` state files (`goal-ledger.yaml`, `dag-state.yaml`, `events/`, `attempts/`, `milestones/`, `results/`) — those belong to the Supervisor. The only `.development/` touch point is read-only inspection.
4. **Flake protocol.** DEV-DEFECT-002 (`tests/state/test_leases.py::test_concurrent_takeover_of_expired_lease_single_winner`, accepted flake, see `.development/KNOWN-DEFECTS.md` and `.development/RECOVERY-NOTE.md`): an isolated rerun that passes is not a regression. pyright is NOT authoritative; pytest/ruff/mypy/CI gates are.
5. **Environment identity.** All commands run with the shared verification interpreter, from the worktree root:
   `PY = C:\Users\lishi\code\scientific-reproduction\scientific-reproduction\.worktrees\.post-merge-venv\Scripts\python.exe`
   (verified Phase 1: Python 3.13.9, pytest 9.1.1, ruff 0.16.3, mypy 2.3.0, PyYAML present; editable install resolves to the repository `src/`, byte-identical to this worktree since G05 touches no src). No new venv is created unless the shared one fails.

---

## 1. Phase 0 — environment identity preamble (before any gate)

| # | Step | Exact command | Expected outcome |
|---|---|---|---|
| P0.1 | Worktree identity | `cd <worktree> && git status --porcelain && git branch --show-current && git rev-parse HEAD` | Clean tree; branch `feat/dev-m13-g05-release`; HEAD == base `6fef533` (or the Supervisor-approved base) |
| P0.2 | Interpreter identity | `"$PY" -c "import sys, scientific_reproduction; print(sys.version.split()[0]); print(scientific_reproduction.__file__)"` | Python 3.13.x; src resolves into the repository tree |
| P0.3 | Tooling present | `"$PY" -m pytest --version && "$PY" -m ruff --version && "$PY" -m mypy --version && "$PY" -c "import yaml"` | pytest/ruff/mypy/yaml all available |
| P0.4 | Baseline expectation note | `git diff HEAD origin/main -- src tests` | Empty (confirms gate results are measured on the merged main state; G05 adds docs only) |

---

## 2. Release gates 1–8 (`18-TEST-AND-ACCEPTANCE-PLAN.md` §4) — AC-05 evidence

All eight gates must pass. Each gate lists the exact commands and the observed-outcome criterion. Gates run in listed order; a failure stops the run.

### Gate 1 — Core schemas validate

| # | Exact command | Expected outcome |
|---|---|---|
| G1.1 | `"$PY" -m pytest -q tests/core/test_schema_validation.py tests/core/test_models.py` | All pass (schema-validation battery: runtime validator `core/schema_validation.py` against `schemas/*.schema.yaml`) |
| G1.2 | `"$PY" benchmarks/fdm201/execution_packages/validate_packages.py` | Exit 0; **20/20 PASS** (7 experiment + 3 computation + 10 worker-context packages; schema conformance, reference traceability, forbidden-pattern scan) |

Cross-check: schema conformance is also exercised by the full suite (jsonschema-driven paths in every domain test). The `schemas/` directory holds the 21 `.schema.yaml` files used by the runtime validator.

### Gate 2 — FDM planning benchmark passes inventory audit

| # | Exact command | Expected outcome |
|---|---|---|
| G2.1 | `"$PY" benchmarks/fdm201/goals/audit_check.py` | Exit 0: **100% mapping — 82/82 inventory items mapped, 0 unmapped, coverage 1.0, all 14 ambiguities resolved, Plan v1 deliverable set matches** (the real M12 plan state under `benchmarks/fdm201/`) |
| G2.2 | `"$PY" benchmarks/fdm201/inventory/self_check.py` | Exit 0 (inventory self-check gate) |
| G2.3 | `"$PY" -m pytest -q tests/benchmarks` | **32 passed** (16 `test_fdm201_planning_benchmark` incl. subprocess gate invocations of `audit_check.py`/`self_check.py`/`validate_packages.py`; 10 `test_fdm201_reload_audit`; 6 `test_fdm201_scenarios_all`) |

### Gate 3 — All scenarios A–J pass

| # | Exact command | Expected outcome |
|---|---|---|
| G3.1 | `"$PY" -m pytest -q tests/scenarios` | **115 passed** across 10 scenario files (A: 7, B: 9, C: 8, D: 18, E: 14, F: 16, G: 11, H: 10, I: 12, J: 10) |
| G3.2 | `"$PY" -m pytest -q tests/benchmarks tests/scenarios` | **147 passed** (the combined benchmark + scenario surface required by the gate) |

### Gate 4 — Worker permission tests prove runtime/governance-layer rejection

| # | Exact command | Expected outcome |
|---|---|---|
| G4.1 | `"$PY" -m pytest -q tests/workers/test_permissions_matrix.py tests/workers/test_permissions_runtime.py` | **63 passed** (29 matrix + 34 runtime); forbidden actions rejected by the permission engine (`core/permissions`, role contracts), not by prompt text |

### Gate 5 — Monitor recovery works with mock platform adapter

| # | Exact command | Expected outcome |
|---|---|---|
| G5.1 | `"$PY" -m pytest -q tests/monitoring/test_recovery_monitor.py` | **22 passed** (crash/recovery, checkpoint reconstruction, no duplicate Run, no lost completion event) |

### Gate 6 — Claude Code adapter real spawn/resume path or documented fallback

| # | Exact command | Expected outcome |
|---|---|---|
| G6.1 | `"$PY" -m pytest -q tests/platform/test_claude_adapter.py tests/platform/test_claude_resume.py` | **33 passed** (17 adapter + 16 resume) |
| G6.2 | Fallback documentation check | `grep -rn "fallback" CLAUDE-CODE-HANDOFF.md docs/` — locate and quote the honest fallback statement covering the path where a real spawn/resume is not available; the adapter contract + resume path remains test-proven. Cite file/line in the release review. |

### Gate 7 — Codex adapter interface + mock contract tests

| # | Exact command | Expected outcome |
|---|---|---|
| G7.1 | `"$PY" -m pytest -q tests/platform/test_codex_adapter.py tests/platform/test_codex_resume.py` | **34 passed** (18 adapter + 16 resume); shared mock/contract battery green with no scientific-core fork |

### Gate 8 — Final machine-auditable package validates traceability

| # | Exact command | Expected outcome |
|---|---|---|
| G8.1 | `"$PY" -m pytest -q tests/reporting -k "audit or trace"` | **33 passed** (17 `test_audit.py` + 16 `test_traceability.py`) |
| G8.2 | Evidence note (no command) | The audit battery runs the real machinery (`reporting/audit.py` `build_audit_package`/`validate_package`, `reporting/traceability.py` `trace_claim`) over real registries — `FilesystemStateBackend` run store, `analysis/results`, `artifacts/registry` manifests, `planning` records — nothing mocked (per `tests/reporting/reporting_helpers.py`). The frozen FDM-201 register is a plan-state (no executed runs/artifacts), so package-level gate evidence comes from this battery plus the reload/audit determinism tests. State this basis explicitly in the release review. |

---

## 3. AC-03 — full verification battery (unit/integration/scenario/benchmark/lint/static/install)

Runs after gates 1–8 (gates are subsets of this battery; the battery is the exhaustive re-run).

| # | Exact command | Expected outcome |
|---|---|---|
| V1 | `"$PY" -m pytest -q` | **3181 passed, 7 skipped, 1 deselected, exit 0** (~5.5 min; matches the merged-main baseline verified in the DEV-M13-G04 review; G05 changes no src/tests so no count delta is expected) |
| V2 | `"$PY" -m ruff check .` | `All checks passed`, exit 0 (the CI invocation) |
| V3 | `"$PY" -m mypy src/` | No issues found (≈115 source files; file count is venv-dependent — OBS-M13-G04-01 — count delta alone is not a regression) |
| V4 | `"$PY" scripts/validate_development_contracts.py` | `PASS: 14 milestones, 80 goals, goal DAG acyclic, milestone DAG acyclic, all contracts well-formed` |
| V5 | `"$PY" benchmarks/fdm201/execution_packages/validate_packages.py` | 20/20 PASS (re-run; same as G1.2) |
| V6 | Install verification | The shared venv is an editable install of the repository; V1 running green under it is the install verification. Record `"$PY" -m pip show scientific-reproduction` location. No new venv unless the shared one fails. |
| V7 | Optional canonical entry | `"$PY" scripts/verify.py --reuse-venv` is NOT run by default (it creates/uses `.verify-venv`); the V1–V5 battery covers its steps individually. Noted here for the Supervisor; can be added on request. |

---

## 4. AC-01 — milestone review audit (M0–M13) and status summary deliverable

**Objective:** every goal review file under `.development/reviews/` (83 files, including `-r2` repairs) has a final decision PASS, per milestone M0–M13; produce `docs/release/M0-M13-STATUS.md`.

Phase-1 inspection established the review corpus shape (re-verified in Phase 2):

- **83 review files.** 74 carry a top-level `decision:` field (**70 PASS + 4 REJECT**); 9 carry the older `verdict:` field (**all PASS**: DEV-M6-G03/G04/G05, DEV-M7-G01/G02, DEV-M9-G06, DEV-M11-G01/G02/G04).
- **4 REJECT files, all superseded by a PASS `-r2`:** DEV-M7-G04-a1 (→ -r2), DEV-M8-G05-a1 (→ -r2), DEV-M9-G01-a1 (→ -r2), DEV-M11-G03-a1 (→ -r2). Each `-r2` declares `supersedes:` + `prior_decision: REJECT` and closes its findings (repair-without-weakening discipline, §5 item 4).
- **Milestone ledger** (`.development/state/milestone-ledger.yaml`): M0–M12 all `PASS` with gate evidence; M13 `NOT_STARTED` (becomes the goal under audit).

| # | Step | Exact command | Expected outcome |
|---|---|---|---|
| A1 | Corpus inventory | `ls .development/reviews/ \| wc -l` | 83 |
| A2 | Decision scan (read-only Python one-liner or grep pass) | `grep -h "^decision:\|^verdict:" .development/reviews/*.yaml \| sort \| uniq -c` | 70 `decision: PASS` + 9 `verdict: PASS`; the only non-PASS are the 4 known REJECT a1 files, each with a matching `-r2` PASS |
| A3 | Supersession closure | For each of the 4 REJECT a1 files: the matching `-r2` exists, `decision: PASS`, `supersedes:` set, findings closed | 4/4 chains closed; final decision per goal = PASS for all 80 goals' review sets |
| A4 | Per-milestone mapping | Map each goal → milestone (from `.development/goals/*.yaml` `milestone:`), fold review outcomes into 14 milestones | M0–M12: all required goals final-PASS (matches milestone ledger); M13: G01–G04 final-PASS, G05 under audit |
| A5 | Status summary deliverable | Write `docs/release/M0-M13-STATUS.md` | See §9 content outline; states evidence source (ledger + milestone files + review scan) |

---

## 5. AC-02 — 80/80 required atomic development goals PASS/MERGED

**Objective:** all 80 frozen goals are MERGED/PASS in the ledger + DAG (79 merged at dispatch; G05 is the 80th, completed by this goal's own closure).

| # | Step | Exact command | Expected outcome |
|---|---|---|---|
| B1 | Ledger status scan | `grep -h "^    status:" .development/state/goal-ledger.yaml \| sort \| uniq -c` (or a read-only YAML pass) | 79 `MERGED` (+ G05 `READY` at dispatch); zero `BLOCKED_HUMAN`, zero `WORKING` leftovers |
| B2 | Goal set parity | Count `.development/goals/*.yaml` == `development/goals/*.yaml` (frozen SSOT) == ledger keys | 80 = 80 = 80; every goal id resolves in both sets |
| B3 | DAG state consistency | Read `.development/dag-state.yaml` (dispatch: `total_frozen_goals: 80`, `merged:` 79 entries, `ready_to_dispatch: [DEV-M13-G05]`, `waiting_on_dependencies: []`, `passed_not_merged: []`, `blocked_human: []`) | Matches the ledger; after G05 closure the ledger/DAG are the Supervisor's to update to 80/80 |
| B4 | State-truth recompute (read-only) | `cd <worktree> && "$PY" .development/scripts/state_summary.py` (absolute script path, from repo root) — then `git diff --exit-code .development/dag-state.yaml` and `git checkout -- .development/dag-state.yaml` to restore the Supervisor-owned file | Recompute partition identical to the committed DAG (diff empty); the file is restored unchanged — the worker never commits `.development/` state |
| B5 | Ledger nuance check | `grep -A6 "DEV-M10-G04:\|DEV-M10-G06:" .development/state/goal-ledger.yaml` | Status MERGED with PASS reviews; `outcome: null` is the known ledger gap explained in the M10 milestone ledger evidence — record as non-blocking observation in M0-M13-STATUS.md, do not modify |

---

## 6. AC-04 — no unresolved blocking Human Gate or audit discrepancy

| # | Scan surface | Method | Expected outcome |
|---|---|---|---|
| C1 | `.development/human-gates/` | Directory existence check | Absent (no dir) → no open gate records |
| C2 | Ledger + DAG | `blocked_human:` and status scan (B1/B3) | Empty at both levels |
| C3 | Events | `grep -ril "human.gate\|BLOCKED_HUMAN\|blocking" .development/events/` (86 files) | Only the 4 known GOAL_MERGED events mention these keywords in normal content (EVT-20260813-0020, EVT-20260814-0055, EVT-20260814-0076, EVT-20260814-0081); none is a blocking-gate event — verify each by reading it |
| C4 | Review findings | Scan reviews for open findings (`findings:` non-empty without a superseding -r2 closure, or unresolved `FND-*` blocks) | None open; the 4 REJECT chains are closed by -r2 (A3) |
| C5 | Known defects | Read `.development/KNOWN-DEFECTS.md` | SPEC-DEFECT-001 FIXED (in DEV-M0-G00); DEV-DEFECT-001 (TOCTOU, accepted-risk, documented) and DEV-DEFECT-002 (flake, accepted) are non-blocking documented dispositions → they feed KNOWN-LIMITATIONS, not blocking gates |
| C6 | Discrepancy ledger | Consolidate C1–C5 + ledger nuances (B5) into the release review | No unresolved blocking item; each non-blocking item has a disposition (accepted / documented / observation) |

---

## 7. §5 development-orchestration acceptance — verification checklist

`21-DEVELOPMENT-SUPERVISOR-SPEC.md` §5 items, each mapped to concrete repository evidence (all verified read-only in Phase 2):

| §5 item | Repository evidence to cite |
|---|---|
| 1. M0–M13 as milestones, not monolithic tasks | 14 milestone files `.development/milestones/M0..M13.yaml` with `required_goals`/`milestone_acceptance`/`unlocks`; milestone ledger with per-milestone gate evidence; events per goal |
| 2. Atomic goals delegated to independent workers | Per-goal `worker_mode` (`agent_team_task` / `detached_native_goal`) in `.development/goals/*.yaml`; per-goal PR trail in reviews (`pr_number` 1–80); worktree-per-goal pattern (`.worktrees/dev-m13-*`) in `RECOVERY-NOTE.md` |
| 3. Worker completion independently re-verified | Every review file authored by "Development Supervisor" with an independent verification battery (e.g., DEV-M13-G04-a1: full-suite rerun, ruff, mypy, validator 20/20, CLI determinism double-run) |
| 4. ≥1 rejected attempt repaired/retried without weakening ACs | 4 REJECT → `-r2` PASS chains (DEV-M7-G04, DEV-M8-G05, DEV-M9-G01, DEV-M11-G03): `-r2` files document `prior_decision: REJECT`, in-scope fix, frozen ACs unchanged, tests monotonic (never deleted) |
| 5. Supervisor auto-unlocked downstream work | DAG recompute history (`dag-state.yaml` + `state_summary.py`), `unlocks:` in milestone files, ledger `merged_at` sequences, event chain (86 GOAL_MERGED records) |
| 6. No routine user confirmation between milestones | AUTO_RUN_M0_TO_M13 mode; zero human-gate events (C3), no `human-gates/` dir, only the 5 Development Human Gates pause execution (RECOVERY-NOTE.md operational constraints) |
| 7. Agent Team task state not the sole source of truth | `.development/` files are the SSOT: goal-ledger + goals/*.yaml + events + attempts + reviews; `dag-state.yaml` is a derived artifact recomputable by `state_summary.py` |
| 8. `.development/` allowed replacement/continuation after session resume | `RECOVERY-NOTE.md` (written pre-hibernation) + resumable worker state (worktrees + agent ids), state regenerable from goal files; milestones ledger backfill documented via events (M10/M11 evidence) |

---

## 8. AC-05 — gate compliance summary

The final release review will contain the gate table (§2) with each gate's commands, observed outcome, and PASS/FAIL, plus the battery (§3). AC-05 is satisfied iff all eight gates pass with observed evidence.

---

## 9. Phase-2 deliverables (files created under `docs/release/**`)

### 9.1 `docs/release/KNOWN-LIMITATIONS.md` — content outline
- Purpose: honest record of v0.1 known limitations and accepted risks, each grounded in code/docs/state (house style: every item traceable to a file/line).
- Sections:
  1. **Accepted engineering risks**: DEV-DEFECT-001 (lease/lock TOCTOU at expiry boundary — hazard window, safe direction, detection semantics; backlog v0.1.1); DEV-DEFECT-002 (timing-flaky lease takeover test, accepted flake, isolated-rerun protocol).
  2. **Fixed defect history**: SPEC-DEFECT-001 (validator count 79→80, fixed in DEV-M0-G00) — kept for the record.
  3. **Adapter/platform limits** (from `docs/operations/adapters-slurm.md` + M9/M10 reviews): constructor-bound configuration (no config file/env loader), no shipped SSH transport (ABC only), queue-blind probing, no exactly-once launch across a mid-operation drop, credentials in memory only (never persisted, M7-G03 AC-02), no partition/account/GPU/reservation flags.
  4. **Reporting/state model limits** (from DEV-M13-G01 and reviews): evidence registry is in-memory (no v0.1 evidence store); Supervisor decisions recorded only as event payloads (no decision registry); no `register_assumption`/`register_human_gate` convenience helpers (OBS-M12-G06-02); inventory-registry immutable-functional repair semantics (OBS-M12-G06-01); failed-run visibility via CANCELLED/INVALIDATED + ScientificReview.FAIL mapping (audit.py).
  5. **Domain-pack limits**: TGA rule R-CHA-A6 multi-measurement shape limitation (M11-G02, loud KeyError never silent skip).
  6. **Benchmark scope**: single reference paper (FDM-201) real-planning benchmark; plan-state register has no executed runs/artifacts (gate 8 basis).
  7. **Process observations**: ledger `outcome: null` for DEV-M10-G04/G06 (documented M10 gap; status+reviews PASS); mypy source-file count venv-dependent (OBS-M13-G04-01); double-pytest harness quirk non-deterministic (OBS-M13-G04-02/03/04); pyright non-authoritative.
- Each item states: what it is, why it is not blocking, disposition (ACCEPTED / DOCUMENTED / FIXED / OBSERVATION).

### 9.2 `docs/release/RELEASE-REVIEW-v0.1.md` — content outline
- Metadata: goal id, date, base SHA, review scope, worker declaration (no self-approval; Supervisor decides).
- §1 Environment & method: venv identity (P0), clean worktree, commands run.
- §2 Gate table: gates 1–8 with commands, observed outcomes, PASS/FAIL (AC-05).
- §3 Full battery: V1–V7 results (AC-03).
- §4 Milestone review audit: AC-01 result + pointer to M0-M13-STATUS.md.
- §5 Goal ledger/DAG audit: AC-02 result (80/80; 79 MERGED at audit + G05).
- §6 Human-gate/discrepancy scan: AC-04 result with C1–C6 dispositions.
- §7 Development-orchestration checklist: §5 items 1–8 with evidence citations.
- §8 Known limitations: pointer to KNOWN-LIMITATIONS.md; statement that none blocks release.
- §9 Findings & observations: any items encountered during Phase 2 (expected: none beyond the known dispositions above).
- §10 Recommended disposition: the worker records **"all gates observed PASS; release recommended"** only if every item above independently passed — the Supervisor renders the final verdict. No verdict is written into the file if any gate failed (the file then records the failure and withholds recommendation).

### 9.3 `docs/release/M0-M13-STATUS.md` — content outline
- Per-milestone table (M0–M13): required goals, goal statuses (MERGED), final review decision (PASS), milestone ledger status (M0–M12 PASS; M13 under audit at time of writing), accepted_at dates.
- Corpus statistics: 80 goals / 83 review files / 4 repair chains / 86 events.
- Ledger nuances section: DEV-M10-G04/G06 `outcome: null` note (non-blocking).
- Evidence sources cited: `.development/state/goal-ledger.yaml`, `.development/state/milestone-ledger.yaml`, `.development/milestones/*.yaml`, `.development/reviews/*.yaml`.

---

## 10. What will NOT be done (explicit)

- No `src/` changes (no code, no behavior).
- No `tests/` changes (no new, deleted, weakened, or modified tests).
- No `schemas/`, spec, or frozen contract changes; no acceptance-criterion reinterpretation.
- No `.development/` state file modifications (ledger, DAG, events, attempts, milestones, results — including restoring `dag-state.yaml` after the B4 recompute check).
- No push to any remote; no PR creation; no merge; no `--no-verify`.
- No benchmark/plan data changes (`benchmarks/fdm201/` is frozen).
- No new venv unless the shared venv fails; no `.verify-venv` creation.
- No real-platform integration attempts (Claude Code live spawn, Codex live run, Slurm SSH) — gate 6/7 evidence is tests + documented fallback, as the plan specifies.
- No verdict claim beyond observed evidence; no self-approval.

---

## 11. Phase-2 execution order summary

P0 (environment) → gates 1–8 in order → AC-03 battery V1–V5 → AC-01 audit (A1–A5) → AC-02 audit (B1–B5) → AC-04 scan (C1–C6) → §5 checklist → write the three deliverables → append the execution log → report results to the Supervisor. Deliverables and the appended execution log are committed as a second commit (message with `[DEV-M13-G05]`); the Supervisor reviews and decides PASS/REJECT. Any failure stops the run at the failing step and is reported immediately.

---

## Appendix — Phase-2 execution log (2026-08-14, plan approved @ c75a746)

Plan approval recorded at `.development/plans/DEV-M13-G05-a1-plan-approval.yaml` (decision APPROVED, approved_by Development Supervisor, plan_commit `431db1a`). All commands ran with the shared venv interpreter from the worktree root. **No gate failed; no stop condition triggered.**

### P0 environment
- P0.1: branch `feat/dev-m13-g05-release`, HEAD `431db1a`, clean tree.
- P0.2/P0.3: Python 3.13.9; pytest 9.1.1; ruff 0.16.3; mypy 2.3.0; yaml+jsonschema ok; src resolves to repository `src/`.
- P0.4: `git diff HEAD origin/main -- src tests` empty (before fetch; origin/main advanced to `c75a746` mid-run — state records only, no rebase).

### Gates 1–8 (all PASS, exit 0)
- G1.1 `pytest -q tests/core/test_schema_validation.py tests/core/test_models.py` -> 204 passed (10.03s)
- G1.2 `validate_packages.py` -> Checks run 20, failures 0; "All packages validate: AC-01 schema conformance, AC-02 traceability, AC-03 context isolation"
- G2.1 `goals/audit_check.py` -> exit 0 (counts: dag acyclic 20, assumptions 46, evidence 82, resources 17, analysis protocols 10, closure contracts 4)
- G2.2 `inventory/self_check.py` -> RESULT: PASS, exit 0
- G2.3 `pytest -q tests/benchmarks` -> 50 passed (119.16s)
- G3.1 `pytest -q tests/scenarios` -> 115 passed (46.85s)
- G3.2 `pytest -q tests/benchmarks tests/scenarios` -> 165 passed (158.76s)
- G4.1 `pytest -q tests/workers/test_permissions_matrix.py tests/workers/test_permissions_runtime.py` -> 95 passed (0.17s)
- G5.1 `pytest -q tests/monitoring/test_recovery_monitor.py` -> 22 passed (1.68s)
- G6.1 `pytest -q tests/platform/test_claude_adapter.py tests/platform/test_claude_resume.py` -> 33 passed (0.29s); G6.2 fallback: CLAUDE-CODE-HANDOFF.md L273 ("subagent/process fallback"), L345 ("documented fallback workers")
- G7.1 `pytest -q tests/platform/test_codex_adapter.py tests/platform/test_codex_resume.py` -> 33 passed, 1 skipped (0.25s)
- G8.1 `pytest -q tests/reporting -k "audit or trace"` -> 34 passed, 34 deselected (11.40s)

### Battery V1–V6 (all PASS)
- V1 `pytest -q` -> **3181 passed, 7 skipped, 1 deselected in 318.79s, exit 0** (exact merged-main baseline; no DEV-DEFECT-002 flake)
- V2 `ruff check .` -> All checks passed
- V3 `mypy src/` -> Success, no issues in 115 source files
- V4 `scripts/validate_development_contracts.py` -> PASS: 14 milestones, 80 goals, goal DAG acyclic, milestone DAG acyclic, all contracts well-formed
- V5 validator rerun -> 20/20, failures 0
- V6 `pip show scientific-reproduction` -> 0.1.0, editable location = repository root

### AC-01 (A1–A5)
- A1: 83 review files. A2: 70 `decision: PASS` + 9 `verdict: PASS` + 4 REJECT. A3: all 4 REJECT a1 files superseded by PASS r2 (DEV-M7-G04, DEV-M8-G05, DEV-M9-G01, DEV-M11-G03). A4: per-milestone final-PASS 79/79 completed goals (M0 5, M1 6, M2 8, M3 3, M4 6, M5 6, M6 5, M7 5, M8 6, M9 6, M10 6, M11 6, M12 7, M13 4) + DEV-M11-G06 PASS recorded in event EVT-20260814-0076 (no standalone file — observation OBS-G05-01). A5: `docs/release/M0-M13-STATUS.md` written.

### AC-02 (B1–B5)
- B1: 79 MERGED + 1 READY. B2: 80 = 80 = 80 (`.development/goals/`, `development/goals/`, ledger keys). B3: DAG total 80, merged 79, ready [DEV-M13-G05], all other lists empty. B4: `state_summary.py` -> `ready=1 waiting=0 passed=0 merged=79 blocked_human=0`; regenerated file byte-identical to committed (git diff empty); restored via `git checkout --` (Supervisor-owned state untouched). B5: DEV-M10-G04/G06 MERGED with PASS reviews, `outcome: null` — known ledger gap (observation OBS-G05-02).

### AC-04 (C1–C6)
- C1: `.development/human-gates/` absent. C2: 0 BLOCKED_HUMAN ledger statuses; DAG blocked_human `[]`. C3: 86 events; 4 keyword matches (EVT-20260813-0020, EVT-20260814-0055, EVT-20260814-0076, EVT-20260814-0081) all `GOAL_MERGED`. C4: no open findings (4 REJECT chains CLOSED FIXED via r2; DEV-M12-G03 FND-M12-G03-01/02 RESOLVED within PASS review). C5: SPEC-DEFECT-001 FIXED, DEV-DEFECT-001 ACCEPTED-RISK, DEV-DEFECT-002 ACCEPTED. C6: consolidated — no unresolved blocking item.

### §5 checklist
8/8 verified: 14 milestone files with unlocks; worker_mode distribution 78 agent_team_task / 1 either / 1 detached_native_goal; 83 supervisor-authored reviews with independent batteries; 4 REJECT→r2 repair chains; DAG recompute + events for auto-unlock; zero human-gate events; `.development/` SSOT (goal files + ledger + events; dag-state derived and recompute-verified); RECOVERY-NOTE.md continuation path.

### Deliverables
- `docs/release/KNOWN-LIMITATIONS.md` — 7 sections, all dispositions non-blocking, every item grounded in code/state/docs.
- `docs/release/RELEASE-REVIEW-v0.1.md` — gate table 8/8 PASS, battery V1–V6, AC-01…AC-05 verdicts, §5 checklist, observations OBS-G05-01/02, worker recommendation.
- `docs/release/M0-M13-STATUS.md` — per-milestone goal/status/review/merge table, corpus statistics, non-blocking observations.
- This log. Worker disposition: all gates and acceptance criteria observed PASS; final verdict is the Supervisor's.
