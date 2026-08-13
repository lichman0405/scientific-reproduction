# 21 — Development Supervisor Specification

## 1. Purpose

This document defines how Claude Code should **autonomously implement v0.1 from M0 through M13** using a long-lived Development Supervisor and disposable development workers.

This is the default development mode. The intended user does **not** manually approve each milestone or worker task.

## 2. Core model

```text
User
  |
  | one startup instruction / global /goal
  v
Development Supervisor (long-lived Claude Code session)
  |
  | owns Development Plan, DAG, frozen acceptance, reviews
  |
  +--> Development Worker A ----> atomic DEV-GOAL ----> result
  +--> Development Worker B ----> atomic DEV-GOAL ----> result
  +--> Development Worker C ----> atomic DEV-GOAL ----> result
  |
  +--> Reviewer/Repair Worker when required
  |
  v
independent Supervisor acceptance
  |
  +--> PASS -> unlock dependents
  +--> REJECT -> retry/repair in fresh or resumed worker
  |
  v
M0 ... M13 all accepted
  |
  v
v0.1 release gates all pass
```

## 3. Milestone vs development goal

M0–M13 are **Milestones / Work Packages**. They are not single implementation tasks.

Every milestone is decomposed into atomic `DEV-GOAL-*` contracts. A development goal must be:

- independently assignable;
- bounded in file/module scope where practical;
- independently testable;
- independently reviewable;
- associated with frozen acceptance criteria;
- safe to reject and retry without redefining the milestone.

The normative decomposition is in `development/milestones/` and `development/goals/`.

## 4. Development Supervisor authority

The Development Supervisor may:

- initialize/verify Git remote `origin` as `https://github.com/lichman0405/scientific-reproduction.git`;
- create goal branches/worktrees, push branches, open/update PRs, submit independent reviews, and merge accepted PRs;

- initialize `.development/` state in the implementation repository;
- create the M0–M13 execution DAG from the supplied frozen definitions;
- mirror eligible goals into Claude Code Agent Team tasks;
- spawn, message, stop, replace, and review teammates;
- launch detached native-`/goal` Claude Code workers when appropriate;
- choose safe parallelism based on dependencies and file-conflict risk;
- require teammate plan approval for risky or cross-cutting goals;
- run tests, inspect diffs, read code, and independently verify deliverables;
- reject worker output;
- create retry/repair attempts against the same frozen goal;
- record ADRs for implementation choices that do not alter locked product semantics;
- continue automatically from M0 until M13 and final release acceptance.

The Development Supervisor must **not**:

- rewrite locked scientific/product architecture to simplify implementation;
- silently weaken acceptance criteria;
- declare a milestone complete based only on a worker's self-report;
- perform the product implementation itself as the normal path;
- stop at milestone boundaries merely to ask the user for confirmation;
- hide failed development attempts;
- make Agent Teams' internal task store the project Single Source of Truth.

## 5. Supervisor implementation boundary

The Supervisor is primarily an orchestrator/reviewer. It may edit only:

- `.development/**` runtime/audit state;
- development coordination metadata;
- ADRs and review records;
- trivial conflict-resolution metadata that does not implement product behavior.

Product source changes should be delegated to a worker and integrated only through the Git governance in `25-DEVELOPMENT-GIT-GOVERNANCE.md`. If a one-line repair is tempting, the Supervisor should still create a repair goal/worker unless doing so is technically impossible or would create greater risk than the edit itself. Any exception must be recorded.

## 6. Development Single Source of Truth

Create and maintain:

```text
.development/
  plan.yaml
  milestones/
  goals/
  attempts/
  results/
  reviews/
  events/
  gates/
  sessions/
```

The supplied `development/` directory in this specification is the **template/frozen source**. The implementation repository's `.development/` directory is the live state.

Claude Code Agent Team task files are an execution mirror only. They may disappear, lag, or fail to resume; `.development/` remains authoritative.

## 7. Goal execution modes

### 7.1 Agent Team task worker — default

Use Claude Code Agent Teams for independent parallel goals. Each teammate receives one atomic development goal contract and relevant context.

Because built-in slash commands typed while viewing a teammate are handled by the lead session, do **not** assume the lead can remotely activate native `/goal` inside a teammate. Instead enforce the same semantics through:

- a frozen Development Goal Contract;
- explicit acceptance criteria in the task description;
- `TaskCompleted` and/or `TeammateIdle` quality-gate hooks;
- mandatory worker evidence in the result package;
- independent Supervisor re-verification.

This is the default mode for M0–M13 parallel development.

### 7.2 Detached native `/goal` worker — optional

For a self-contained long-running goal where native `/goal` is valuable, the Supervisor may launch an independent Claude Code CLI process, for example conceptually:

```text
claude -p "/goal <frozen measurable condition>" \
  --output-format stream-json --verbose
```

The Supervisor records its session/process identity, working tree, logs, and final result. The detached worker still has no authority to mark the development goal PASS; the Supervisor reviews it independently.

### 7.3 Subagent fallback

If Agent Teams are unavailable or unsuitable for a tightly scoped task, the platform may use a subagent. This is a fallback, not a change to the development contract.

## 8. Global autonomous run condition

The lead Development Supervisor should itself run under a global native Claude Code `/goal` whenever available. The condition should express the real terminal state, for example:

```text
All M0-M13 milestones are independently accepted by the Development Supervisor;
all required atomic development goals are PASS;
all v0.1 release gates in 18-TEST-AND-ACCEPTANCE-PLAN.md pass;
FDM-201 planning benchmark and scenarios A-J pass;
final repository-wide tests/lint/type checks succeed;
no unresolved BLOCKING development gate remains;
and final development audit/report files are written.
Do not stop merely because a milestone finished or a teammate became idle.
```

The startup template is in `development/templates/DEVELOPMENT-SUPERVISOR-START.md`.

## 9. Git/PR integration loop

Every repository-changing atomic goal follows:

```text
READY
 -> dependency PRs confirmed merged into origin/main
 -> fresh goal branch/worktree from accepted origin/main
 -> Worker implementation/commits
 -> push + Draft PR
 -> CI + Worker Result Package
 -> Supervisor independent review of current PR head SHA
 -> PASS: squash merge + branch cleanup
 -> REJECT: repair/retry and repeat review
```

The Supervisor must not accept a Worker self-report, green local test, or green CI alone as final PASS. It must also review scope/diff and frozen acceptance. A changed PR head invalidates stale review evidence.

The one-time empty-repository specification bootstrap exception is defined in `25-DEVELOPMENT-GIT-GOVERNANCE.md`. After it, normal direct product commits to `main` are forbidden.

## 10. Automatic continuation rule

After any goal/milestone transition, the Supervisor must immediately:

1. update `.development/` state;
2. recompute the DAG;
3. identify newly unblocked goals;
4. detect likely file conflicts;
5. spawn/assign the safe set according to concurrency policy;
6. independently review any returned results;
7. continue until the global completion condition is met.

There is no default "stop after M1" or "ask before M7" behavior.

## 11. Development Human Gates

The Supervisor may ask the user only when at least one of these conditions holds and no other unblocked work can proceed around it:

- `LOCKED_ARCHITECTURE_CONFLICT`: a locked requirement appears technically impossible and any fix would change product semantics;
- `EXTERNAL_ACCESS_BLOCK`: unavailable credential/account/network/service is required to verify a mandatory release gate;
- `IRREVERSIBLE_EXTERNAL_ACTION`: an implementation step would perform an irreversible external write not already authorized;
- `UNRESOLVABLE_PLATFORM_LIMITATION`: current Claude Code/Codex capability prevents a mandatory platform acceptance and no documented fallback satisfies the contract;
- `RELEASE_GATE_SPEC_CONTRADICTION`: two frozen acceptance conditions are mutually inconsistent.

Ordinary test failures, code defects, merge conflicts, API errors, worker failures, and teammate early stops are **not** Human Gates. They trigger repair/retry.

## 12. Worker result package

Every worker must report at least:

```yaml
goal_id: DEV-...
attempt_id: ATTEMPT-...
status_claim: completed
files_changed: []
tests_run:
  - command: ...
    exit_code: ...
acceptance_evidence:
  - criterion_id: AC-...
    evidence: ...
known_limitations: []
unexpected_changes: []
```

The worker is reporting evidence, not granting PASS.

## 13. Supervisor review

For every returned goal:

- inspect the pull request's current head SHA, `git diff`, and changed files;
- verify branch/base/dependency provenance against `origin/main`;
- rerun required deterministic checks;
- verify scope and forbidden changes;
- verify goal-specific acceptance criteria;
- run relevant regression tests;
- check dependency contracts;
- confirm required PR CI status is green;
- confirm reviewed head SHA still equals the merge candidate;
- record `PASS` or `REJECT` with evidence in `.development/reviews/`.

On PASS the Supervisor may squash-merge the PR, record the merge SHA, clean the goal branch/worktree, fetch `origin/main`, and recompute the DAG.

A worker's own `/goal achieved`, task completion, green local output, or statement "done" is insufficient.

## 14. Retry and repair

On rejection:

```text
DEV-GOAL-X attempt 1 -> REJECT
  |
  +-> small/local defect: resume or spawn repair worker
  |
  +-> contaminated context / broad defect: fresh worker
  |
  +-> cross-goal regression: create bounded repair goal owned by Supervisor plan
```

Acceptance criteria stay frozen unless a Development Human Gate justifies formal revision.

## 15. Concurrency

Default development policy:

```yaml
max_active_workers: 5
max_same_milestone_workers: 3
max_cross_cutting_workers: 1
```

The Supervisor may lower parallelism when workers would edit overlapping files. It should prefer parallel goals that own separate modules/tests/docs.

## 16. Agent Teams limitations and recovery

Agent Teams are treated as an execution layer, not durable state. Known limitations such as teammate resumption gaps, stale task status, or teammate early stop must be handled by reconciliation:

- compare Agent Team task state with `.development/goals/`;
- inspect actual repository state;
- re-open/reassign tasks when needed;
- spawn replacement teammates after Supervisor resume if old teammates no longer exist;
- never infer milestone completion from task-list appearance alone.

## 17. Completion

The Development Supervisor may end only when:

- M0 through M13 milestone reviews are `PASS`;
- all required development goals are `PASS`;
- all required release gates are satisfied;
- final full-suite verification is green;
- no blocking audit discrepancy exists;
- final development summary and implementation handoff are written.
