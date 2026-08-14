# Recovery Note (2026-08-14)

Written before a machine shutdown/hibernate so any new session can resume the
M0-M13 execution without the original conversation context.

## Where we are

- Milestones: M0-M12 fully PASS (13/14); M13 in progress.
- Goals: **76/80 merged** on origin/main (head at writing: `ffa509a`).
- Remaining: DEV-M13-G02 (report generator), DEV-M13-G03 (outcome/recovery
  summaries) dispatched and IN FLIGHT; then G04 (docs/templates), G05
  (release gate + final audit, `plan_approval_required: true`), then the
  v0.1.0 Release Gate (18-TEST-AND-ACCEPTANCE-PLAN.md SS4, 8 gates), the
  immutable tag `v0.1.0` and the final Development Acceptance Report.

## Resume steps (in a new Claude Code session)

1. `cd C:\Users\lishi\code\scientific-reproduction\scientific-reproduction`
2. Prefer `claude --continue` (same session). If the session cannot resume:
   start a new session and follow THIS note.
3. Verify actual state before assuming anything (never trust that background
   tasks survived a shutdown):
   - `gh pr list --state open` — any in-flight worker PRs (draft) to review.
   - `Get-Process python` — any leftover pytest/worker processes (kill stale
     ones; results are re-runnable).
   - `git worktree list` + `git status --short` in each of
     `.worktrees/dev-m13-g02`, `.worktrees/dev-m13-g03` — workers' work is on
     disk even if the worker died.
4. Resume in-flight workers via SendMessage (their transcripts persist):
   - M13-G02 worker agent id: `a3008e392a919cbf1` (worktree `.worktrees/dev-m13-g02`,
     branch `feat/dev-m13-g02-report`, module `reporting/report.py`)
   - M13-G03 worker agent id: `ae64f0257b46e0bed` (worktree `.worktrees/dev-m13-g03`,
     branch `feat/dev-m13-g03-summary`, module `reporting/summary.py`)
   - If a worker cannot be resumed, re-dispatch a fresh worker with a brief
     pointing at the existing worktree files (they are on disk, uncommitted or
     committed on the feature branch).
5. If the M13-G01 post-merge suite never recorded its result: re-run
   `.worktrees/.post-merge-venv/Scripts/python.exe -m pytest -q tests` from the
   repo root on origin/main and append the result to the `post_merge` block of
   `.development/reviews/DEV-M13-G01-a1.yaml` (state commit + push).
6. Standard closure chain per goal (see 18-TEST-AND-ACCEPTANCE-PLAN.md):
   supervisor independent battery on PR head (review venv pattern:
   copy `.worktrees/.sup-review-venv-dev-m13-g01`, repoint its
   `_editable_impl_scientific_reproduction.pth` to the worktree src) -> PASS
   review yaml -> `gh pr ready` + `gh pr merge --squash --match-head-commit
   <exact head sha>` -> attempt/event/ledger -> DAG recompute
   (`.development/scripts/state_summary.py` from the repo root with a venv
   python and the ABSOLUTE script path) -> state commit -> `git fetch origin`
   -> `git rebase --onto origin/main HEAD~1 HEAD` -> `git branch -f main HEAD`
   -> push -> cleanup worktree/branch -> post-merge suite.

## Operational constraints (hard)

- Max 5 active workers, max 3 same-milestone, max 1 cross-cutting.
- Workers must not push main, merge their own PR, modify frozen ACs, expand
  scope, delete tests, use --no-verify, or self-PASS.
- New scope -> DEVELOPMENT_CHANGE_REQUEST. Only the 5 Development Human Gates
  pause execution; ordinary failures are fixed via -r2/-r3 automatically.
- DEV-DEFECT-002 (test_leases.py::test_concurrent_takeover_of_expired_lease_
  single_winner) is an accepted flake: isolated rerun pass = not a regression.
- pyright is NOT authoritative; mypy/ruff/pytest/CI are.
- PowerShell dot-folder trap: `.development` resolves to `development`; use
  absolute paths and `& python.exe <abs path>` (never bare `.\file.py`).
