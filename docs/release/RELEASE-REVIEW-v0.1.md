# v0.1.0 Release Review — Scientific Reproduction Skill

| Field | Value |
|---|---|
| Goal | DEV-M13-G05 (M13 — v0.1 release gate and final development audit) |
| Reviewed by | DEV-M13-G05 detached native worker (evidence collector) — final verdict rests with the Development Supervisor |
| Date | 2026-08-14 |
| Base | `6fef533` (origin/main at dispatch); runbook `docs/release/DEV-M13-G05-runbook.md` @ `431db1a`; plan approval `.development/plans/DEV-M13-G05-a1-plan-approval.yaml` @ `c75a746` (decision: APPROVED) |
| Worktree | `.worktrees/dev-m13-g05`, branch `feat/dev-m13-g05-release` |
| Environment | `.worktrees/.post-merge-venv` — Python 3.13.9, pytest 9.1.1, ruff 0.16.3, mypy 2.3.0; editable install `scientific-reproduction 0.1.0` resolving to the repository `src/` (byte-identical to the worktree; `git diff HEAD origin/main -- src tests` empty) |

---

## 1. Release gates (18-TEST-AND-ACCEPTANCE-PLAN.md §4) — AC-05

All eight gates observed green, independently executed on 2026-08-14:

| Gate | Criterion | Commands (observed) | Result |
|---|---|---|---|
| 1 | Core schemas validate | `pytest -q tests/core/test_schema_validation.py tests/core/test_models.py` -> **204 passed** (10.03s); `benchmarks/fdm201/execution_packages/validate_packages.py` -> **20/20 PASS, failures 0, exit 0** (AC-01 schema conformance / AC-02 traceability / AC-03 context isolation) | PASS |
| 2 | FDM planning benchmark passes inventory audit | `benchmarks/fdm201/goals/audit_check.py` -> **exit 0** (82/82 mapped, 0 unmapped, coverage 1.0, 14/14 ambiguities resolved, Plan v1 deliverable set exact); `benchmarks/fdm201/inventory/self_check.py` -> **RESULT: PASS, exit 0**; `pytest -q tests/benchmarks` -> **50 passed** (119.16s; planning-benchmark family incl. subprocess gate invocations + reload/audit determinism) | PASS |
| 3 | All scenarios A–J pass | `pytest -q tests/scenarios` -> **115 passed** (46.85s; A:7 B:9 C:8 D:18 E:14 F:16 G:11 H:10 I:12 J:10); `pytest -q tests/benchmarks tests/scenarios` -> **165 passed** (158.76s) | PASS |
| 4 | Worker permission tests prove runtime-layer rejection | `pytest -q tests/workers/test_permissions_matrix.py tests/workers/test_permissions_runtime.py` -> **95 passed** (0.17s; matrix + runtime enforcement on `core/permissions`, not prompt text) | PASS |
| 5 | Monitor recovery with mock platform adapter | `pytest -q tests/monitoring/test_recovery_monitor.py` -> **22 passed** (1.68s) | PASS |
| 6 | Claude Code adapter real spawn/resume or documented fallback | `pytest -q tests/platform/test_claude_adapter.py tests/platform/test_claude_resume.py` -> **33 passed** (0.29s); documented fallback located: CLAUDE-CODE-HANDOFF.md L273 ("subagent/process fallback") and L345 ("documented fallback workers"); M10 milestone ledger records the spawn/resume/fallback path (M10-G03/G04) | PASS |
| 7 | Codex adapter interface + mock contract tests | `pytest -q tests/platform/test_codex_adapter.py tests/platform/test_codex_resume.py` -> **33 passed, 1 skipped** (0.25s; the skip is the real-platform attempt, per gate wording "should be attempted where available") | PASS |
| 8 | Final machine-auditable package validates traceability | `pytest -q tests/reporting -k "audit or trace"` -> **34 passed, 34 deselected** (11.40s; `reporting/audit.py` build_audit_package/validate_package + `reporting/traceability.py` trace_claim over real registries — FilesystemStateBackend run store, analysis results, artifact manifests, planning records; nothing mocked, per tests/reporting/reporting_helpers.py) | PASS |

**Gate 8 basis note:** the frozen FDM-201 register is plan-state (Plan v1, no executed runs/artifacts on disk), so the package-level gate evidence is the audit/traceability battery over real registries plus the reload/audit determinism tests — not a package built over the FDM-201 register itself. Basis recorded so the Supervisor can judge the gate fairly.

**AC-05 verdict:** the final implementation satisfies all eight §4 release gates — **PASS (evidence above)**.

## 2. Full verification battery — AC-03

| # | Command | Observed |
|---|---|---|
| V1 | `pytest -q` (full suite) | **3181 passed, 7 skipped, 1 deselected in 318.79s, exit 0** — exactly the merged-main baseline (DEV-M13-G04 review: 3181); no DEV-DEFECT-002 flake occurrence |
| V2 | `ruff check .` | **All checks passed**, exit 0 |
| V3 | `mypy src/` | **Success: no issues found in 115 source files**, exit 0 |
| V4 | `scripts/validate_development_contracts.py` | **PASS: 14 milestones, 80 goals, goal DAG acyclic, milestone DAG acyclic, all contracts well-formed**, exit 0 |
| V5 | `benchmarks/fdm201/execution_packages/validate_packages.py` | 20/20 PASS, failures 0, exit 0 |
| V6 | install verification | editable install `scientific-reproduction 0.1.0` at the shared venv site-packages, resolving to the repository src; full suite green under it |

**AC-03 verdict:** unit / integration / scenario / benchmark / lint / static / install verification all succeed — **PASS**.

## 3. AC-01 — milestone reviews M0–M13 all PASS

- Review corpus: **83 files** under `.development/reviews/` (70 `decision: PASS` + 9 older-format `verdict: PASS` + 4 `decision: REJECT` a1 files, each **superseded by a PASS `-r2`**: DEV-M7-G04, DEV-M8-G05, DEV-M9-G01, DEV-M11-G03).
- Per-milestone final-decision mapping (regex scan over goal + review files): M0 5/5, M1 6/6, M2 8/8, M3 3/3, M4 6/6, M5 6/6, M6 5/5, M7 5/5, M8 6/6, M9 6/6, M10 6/6, M11 6/6, M12 7/7, M13 4/5 (G01–G04 PASS; G05 is this goal) — **79/79 completed goals have final PASS evidence**.
- Milestone ledger: M0–M12 `PASS` with gate evidence; M13 completed by this goal (ledger transition is the Supervisor's record).
- **Observation OBS-G05-01:** DEV-M11-G06 has no standalone review file; its PASS is recorded in the events ledger (EVT-20260814-0076: `review_id: DEV-M11-G06-a1`, `review_decision: PASS`, `findings: []`). Non-blocking — see KNOWN-LIMITATIONS.md §7.

**AC-01 verdict:** all M0–M13 milestone reviews PASS — **PASS** (details in `M0-M13-STATUS.md`).

## 4. AC-02 — all required atomic development goals PASS

- Ledger: **79 MERGED + 1 READY (DEV-M13-G05)** — zero BLOCKED_HUMAN, zero WORKING leftovers.
- Goal set parity: `.development/goals/` 80 == `development/goals/` (frozen SSOT) 80 == ledger keys 80.
- DAG (`dag-state.yaml`): total_frozen_goals 80, merged 79, ready_to_dispatch [DEV-M13-G05], waiting_on_dependencies [], passed_not_merged [], blocked_human [].
- State-truth recompute (`state_summary.py`): `ready=1 waiting=0 passed=0 merged=79 blocked_human=0`; regenerated file **byte-identical** to the committed DAG (restored untouched).
- Ledger nuance: DEV-M10-G04/G06 `outcome: null` (status MERGED, PASS reviews) — known gap documented in the M10 milestone ledger evidence; non-blocking (OBS-G05-02, KNOWN-LIMITATIONS.md §7).

**AC-02 verdict:** 79/79 merged goals PASS; G05 completes the 80th — **PASS** (G05 ledger transition by Supervisor at closure).

## 5. AC-04 — no unresolved blocking Human Gate or audit discrepancy

| Surface | Observed |
|---|---|
| `.development/human-gates/` | absent |
| Ledger statuses | 0 `BLOCKED_HUMAN` |
| DAG `blocked_human` | `[]` |
| Events (86 files) | 4 files match human-gate/blocking keywords; all 4 are `GOAL_MERGED` records (EVT-20260813-0020, EVT-20260814-0055, EVT-20260814-0076, EVT-20260814-0081) — no blocking-gate event |
| Review findings | no open findings: 4 REJECT a1 chains closed by `-r2` PASS (FND-* CLOSED FIXED); DEV-M12-G03 findings (FND-M12-G03-01/02) RESOLVED within its PASS review |
| Known defects | SPEC-DEFECT-001 FIXED; DEV-DEFECT-001 ACCEPTED-RISK; DEV-DEFECT-002 ACCEPTED (all dispositioned, non-blocking) |

**AC-04 verdict:** no unresolved blocking Human Gate or audit discrepancy — **PASS**. Two non-blocking audit observations recorded (OBS-G05-01, OBS-G05-02) with dispositions in KNOWN-LIMITATIONS.md §7.

## 6. Development-orchestration acceptance (18-TEST §5) — 8/8 verified

| Item | Evidence |
|---|---|
| 1. M0–M13 as milestones | 14 milestone files with required_goals/acceptance/unlocks; milestone ledger per-milestone gate evidence |
| 2. Atomic goals to independent workers | per-goal `worker_mode` (78 `agent_team_task`, 1 `either`, 1 `detached_native_goal` = this goal); per-goal PR trail in reviews (PRs 1–80); worktree-per-goal pattern in RECOVERY-NOTE.md |
| 3. Independent supervisor re-verification | all 83 reviews authored by supervisor roles ("Development Supervisor...", "development-supervisor", "supervisor") with independent verification batteries (e.g., DEV-M13-G04-a1 full-suite rerun) |
| 4. Rejected attempt repaired without weakening ACs | 4 REJECT -> `-r2` PASS chains; r2 files document prior_decision, in-scope fixes, frozen ACs unchanged, tests monotonic |
| 5. Automatic downstream unlock | DAG recompute history, `unlocks:` in milestone files, ledger merged_at sequences, 86 GOAL_MERGED events |
| 6. No routine user confirmation | AUTO_RUN_M0_TO_M13 mode; zero human-gate events; only the 5 Development Human Gates pause (RECOVERY-NOTE.md) |
| 7. Agent Team state not sole source of truth | `.development/` goal files + goal-ledger + events + attempts + reviews are the SSOT; dag-state.yaml is derived (state_summary.py recompute verified identical) |
| 8. `.development/` allowed replacement/continuation | RECOVERY-NOTE.md (pre-hibernation), resumable workers, state regenerable from goal files |

## 7. Findings and observations

- **Findings:** none.
- **Observations:** OBS-G05-01 (M11-G06 review recorded in events ledger, no standalone file) and OBS-G05-02 (M10-G04/G06 ledger `outcome: null`) — both non-blocking, dispositioned in KNOWN-LIMITATIONS.md §7.
- Note: origin/main advanced to `c75a746` (supervisor state records only) during execution; this branch is based on `6fef533` and the PR diff contains only `docs/release/**` files.

## 8. Worker disposition

All eight release gates, the full verification battery, and the four acceptance criteria (AC-01…AC-05) were **observed PASS** in this audit. The worker therefore **recommends release of v0.1.0** — the Development Supervisor independently reviews this evidence and renders the final verdict.
