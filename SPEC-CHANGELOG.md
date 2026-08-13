# Specification Package Changelog

## v0.1.2

Added normative Git/GitHub development governance for autonomous M0–M13 implementation.

Added:

- default GitHub remote `lichman0405/scientific-reproduction`;
- protected-`main`, short-lived goal branch model;
- one branch + one PR per repository-changing DEV-GOAL;
- Git worktree isolation guidance for parallel Workers;
- Conventional Commit + DEV-GOAL ID standard;
- Draft PR lifecycle and PR evidence template;
- mandatory CI + independent Supervisor current-head review before merge;
- Supervisor-only merge authority and default squash merge;
- new-feature/change-request governance;
- bugfix/refactor/revert/rollback policy;
- release/tag policy;
- machine-readable `development/git-policy.v1.yaml`;
- Development Attempt and Development Change Request schemas;
- `DEV-M0-G00` GitHub/PR/CI bootstrap goal.

Changed:

- frozen required goal count from 79 to 80;
- M0 now begins with GitHub governance bootstrap;
- Development Plan now pins the repository and Git policy;
- Worker result/review schemas now require Git/PR evidence;
- startup instruction now initializes the GitHub remote and then autonomously uses branch -> PR -> CI -> review -> merge for M0–M13.

## v0.1.1

Development handoff changed from incremental/manual milestone execution to autonomous M0–M13 orchestration.

Added:

- Development Supervisor specification;
- `AUTO_RUN_M0_TO_M13` protocol;
- 14 machine-readable milestone contracts;
- 79 atomic development goal contracts;
- development plan/goal/milestone/result/review schemas;
- Agent Team + detached native `/goal` worker policy;
- TaskCompleted/TeammateIdle quality-gate guidance;
- development Single Source of Truth under `.development/`;
- Development Human Gates;
- automatic rejection/retry/replacement rules;
- root `START-CLAUDE-CODE.md` startup path.

Changed:

- `CLAUDE-CODE-HANDOFF.md`: no longer tells Claude Code to implement only M0/M1 first; default is autonomous M0–M13 completion.
- `IMPLEMENTATION-CHECKLIST.md`: now tracks Supervisor-driven milestone/goal acceptance.
- `18-TEST-AND-ACCEPTANCE-PLAN.md`: adds development-orchestration acceptance.
- `20-ARCHITECTURE-DECISIONS.md`: adds locked autonomous-development ADRs.
