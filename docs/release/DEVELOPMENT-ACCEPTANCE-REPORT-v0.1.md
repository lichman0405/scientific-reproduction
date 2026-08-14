# Development Acceptance Report — Scientific Reproduction Skill v0.1.0

| Field | Value |
|---|---|
| Version | v0.1.0 (spec package v0.1.2) |
| Released | 2026-08-14 |
| Tag | `v0.1.0` (annotated, immutable) @ `dce8d3affbe1297c13dde6819b3bdc9c9366e3a9` |
| Release Acceptance Suite SHA | `dce8d3a` — the exact tagged commit (the suite ran on the tagged SHA itself) |
| Issued by | Development Supervisor |
| Acceptance | **ACCEPTED — release v0.1.0** |

---

## 1. Headline

All fourteen milestones (M0–M13) and all eighty atomic development goals are
**PASS / MERGED** on `main`. The final implementation satisfies every
`18-TEST-AND-ACCEPTANCE-PLAN.md` §4 release gate, and the Release Acceptance
Suite passed on the exact commit that carries the `v0.1.0` tag. No blocking
Human Gate or unresolved audit discrepancy remains.

## 2. Development completion

| Metric | Value |
|---|---|
| Frozen goals | 80/80 MERGED with PASS review evidence |
| Milestones | 14/14 PASS (M0–M13; milestone ledger with per-milestone gate evidence) |
| Repair chains | 4 (REJECT a1 → PASS r2: DEV-M7-G04, DEV-M8-G05, DEV-M9-G01, DEV-M11-G03; frozen ACs unchanged) |
| Review corpus | 83 files (70 `decision: PASS` + 9 `verdict: PASS` + 4 REJECT closed by PASS r2) |
| Events | 88 records (87 GOAL_MERGED chain + plan approval), all dispositioned |
| PR trail | 81 PRs, every merge `--match-head-commit` verified squash |
| DAG | `dag-state.yaml` recomputed by `state_summary.py`: merged 80, ready 0, blocked_human 0 — byte-identical on recompute |
| Human gates | 0 blocking; only the 5 documented Development Human Gates pause the program |

Documented non-blocking observations (see `docs/release/KNOWN-LIMITATIONS.md`
§7 and `docs/release/RELEASE-REVIEW-v0.1.md`):

- **OBS-G05-01** — DEV-M11-G06 has no standalone review file; its PASS is
  machine-recorded in the contemporaneous event `EVT-20260814-0076`
  (`review_id: DEV-M11-G06-a1`, `review_decision: PASS`, `findings: []`, full
  review summary). No backfill was authored to avoid post-hoc evidence; the
  ledger reference resolves through the event record. Disposition: DOCUMENTED,
  non-blocking.
- **OBS-G05-02** — DEV-M10-G04/G06 ledger `outcome: null` despite MERGED
  status and PASS reviews; gap documented in the M10 milestone ledger evidence
  (events EVT-20260814-0071..0074). Disposition: DOCUMENTED, non-blocking.

## 3. Release gates (18-TEST-AND-ACCEPTANCE-PLAN.md §4) — 8/8 PASS

Independent evidence in `docs/release/RELEASE-REVIEW-v0.1.md` (worker phase-2
audit) plus supervisor re-verification; the Release Acceptance Suite below
re-confirms the gate test surfaces on the tagged commit.

| Gate | Criterion | Result |
|---|---|---|
| 1 | Core schemas validate | PASS — 204 schema/model tests + execution packages 20/20 |
| 2 | FDM-201 planning benchmark inventory audit | PASS — 82/82 mapped, 0 unmapped, self_check PASS, 50 benchmark tests |
| 3 | Scenarios A–J | PASS — 115 (A:7 B:9 C:8 D:18 E:14 F:16 G:11 H:10 I:12 J:10); combined with benchmarks 165 |
| 4 | Worker permission tests (runtime rejection) | PASS — 95 matrix + runtime |
| 5 | Monitor recovery with mock adapter | PASS — 22 |
| 6 | Claude Code adapter spawn/resume or documented fallback | PASS — 33 + fallback citation CLAUDE-CODE-HANDOFF.md L273/L345 (verified verbatim) |
| 7 | Codex adapter interface + mock contract | PASS — 33 passed, 1 skipped (real-platform attempt, per gate wording) |
| 8 | Final machine-auditable package validates traceability | PASS — 34 audit/trace over real registries (nothing mocked) |

## 4. Release Acceptance Suite — on the exact tagged SHA

All commands ran on `main @ dce8d3affbe1297c13dde6819b3bdc9c9366e3a9` =
the `v0.1.0` tag target, in the shared release venv
(`.worktrees/.post-merge-venv`: Python 3.13.9, pytest 9.1.1, ruff 0.16.3,
mypy 2.3.0, editable `scientific-reproduction 0.1.0` resolving to repo `src/`).

| # | Command | Result |
|---|---|---|
| 1 | `python -m pytest -q tests` | **3181 passed, 7 skipped, 1 deselected in 351.45s, exit 0** — no DEV-DEFECT-002 flake occurrence |
| 2 | `python -m ruff check .` | All checks passed, exit 0 |
| 3 | `python -m mypy src` | Success, no issues in 115 source files, exit 0 |
| 4 | `python benchmarks/fdm201/goals/audit_check.py` | exit 0 (82 evidence records, 20 DAG-ordered plan nodes, 46 assumptions, 17 resources, 10 analysis protocols, 4 closure contracts) |
| 5 | `python benchmarks/fdm201/inventory/self_check.py` | RESULT: PASS, exit 0 |
| 6 | `python benchmarks/fdm201/execution_packages/validate_packages.py` | Checks run 20, failures 0, exit 0 |
| 7 | `python scripts/validate_development_contracts.py` | PASS: 14 milestones, 80 goals, goal DAG acyclic, milestone DAG acyclic, all contracts well-formed, exit 0 |
| 8 | `python .development/scripts/state_summary.py` | merged=80 ready=0 blocked_human=0, exit 0; regenerated `dag-state.yaml` byte-identical |

Post-merge suite after the final goal (origin/main 36127de): 3181 passed in
354.06s, exit 0. Independent G05-head review suite: 3181 passed in 298.96s,
exit 0. All three runs are on identical tested content.

## 5. Audit-trail completeness

- `.development/` is the Single Source of Truth: goals (80), goal-ledger (80
  entries, all MERGED), milestone-ledger (M0–M13 PASS), attempts (80+), reviews
  (83), events (88), plans (1), dag-state (derived, recompute-verified
  byte-identical). `RECOVERY-NOTE.md` documents the continuation path; the
  state is regenerable from goal files.
- Every repository-changing goal followed DEV-GOAL → branch → worktree →
  worker → push → Draft PR → CI → supervisor review (PASS/REJECT) → squash
  merge with `--match-head-commit <exact SHA>` → state records → DAG recompute.
- No credentials persisted anywhere in the repository (DEV-M7-G03 AC-02); no
  worker pushed `main`, merged its own PR, or used `--no-verify`.
- The G05 goal ran the `plan_approval_required` protocol per
  `21-DEVELOPMENT-SUPERVISOR-SPEC.md` §4: runbook `431db1a` → supervisor
  approval record `c75a746` → execution.

## 6. Release artifacts

| Artifact | Location |
|---|---|
| Changelog | `CHANGELOG.md` (v0.1.0 entry, Keep a Changelog) |
| Release review (gates 1–8 + battery) | `docs/release/RELEASE-REVIEW-v0.1.md` |
| Milestone status summary | `docs/release/M0-M13-STATUS.md` |
| Known limitations | `docs/release/KNOWN-LIMITATIONS.md` (all dispositions non-blocking) |
| G05 runbook + execution log | `docs/release/DEV-M13-G05-runbook.md` |
| This report | `docs/release/DEVELOPMENT-ACCEPTANCE-REPORT-v0.1.md` |
| Immutable tag | `v0.1.0` @ `dce8d3affbe1297c13dde6819b3bdc9c9366e3a9` (annotated) |

## 7. Final verdict

**ACCEPTED.** The Scientific Reproduction Skill v0.1.0 meets all frozen
acceptance criteria: 80/80 goals PASS, 14/14 milestones PASS, all 8 release
gates PASS with observed evidence, Release Acceptance Suite PASS on the exact
tagged SHA, full audit trail in `.development/`, no blocking Human Gates.
Known limitations are documented and non-blocking, with a v0.1.1 backlog
candidate (DEV-DEFECT-001 lease/lock hardening).
