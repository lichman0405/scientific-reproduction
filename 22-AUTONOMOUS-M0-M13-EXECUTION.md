# 22 — Autonomous M0–M13 Execution Protocol

## 1. Default mode

`AUTO_RUN_M0_TO_M13` is the default development mode for this package.

The user should be able to start one Development Supervisor and then leave normal implementation decisions, worker creation, retries, milestone progression, and verification to that Supervisor.

## 2. Startup sequence

1. Read the implementation specification, locked ADRs, `25-DEVELOPMENT-GIT-GOVERNANCE.md`, and `development/git-policy.v1.yaml`.
2. Verify local Git/GitHub access, configure/fetch `origin` for `lichman0405/scientific-reproduction`, and perform the specification-only empty-repository baseline bootstrap only if `origin/main` does not yet exist.
4. Verify Claude Code version/capabilities relevant to Agent Teams and `/goal`.
4. Create `.development/` from `development/development-plan.v1.yaml` and frozen milestone/goal contracts.
5. Validate the development DAG and acceptance contracts.
6. Freeze Development Plan v1.
7. Enable or confirm Agent Teams when available.
8. Register development quality gates/hooks when practical.
9. Start/continue the Supervisor's global `/goal`.
10. Spawn workers for all ready, conflict-safe goals.
11. Continue automatically until M13 and final release gates pass.

## 3. No-pause policy

The Supervisor must not pause for user approval because:

- a milestone finished;
- a worker finished;
- a test failed;
- a worker was rejected;
- a new dependency became ready;
- a merge conflict occurred;
- a teammate stopped early;
- an implementation choice is ordinary and does not change locked semantics.

It should repair, retry, reassign, or choose a reasonable documented implementation.

## 4. Milestone progression

A milestone is `PASS` only after:

- every required atomic development goal is `PASS`;
- milestone-level acceptance checks are independently executed;
- required regression checks pass;
- required docs/contracts are updated;
- no unresolved milestone-blocking review item remains.

Milestone acceptance unlocks downstream milestones automatically.

## 5. Dependency strategy

Normative milestone DAG:

```text
M0
 |
 v
M1
 |\
 | +--> M3
 v
M2 -----> M5 ----\
 | \      M9 -----+--> M11 --\
 |  \                            \
 |   +--> M7 --> M8 ----\         \
 v                     +--> M10 ---+--> M12 --> M13
M4 --> M6 -------------/          /
  \-----------------------------/
```

More precisely, see `development/development-plan.v1.yaml`. The Supervisor may schedule atomic goals more finely than milestone boundaries, but may not violate frozen dependencies.

## 6. Git-aware dispatch

A goal is not dispatchable until:

- all frozen dependencies are PASS;
- all dependency PRs are merged into `origin/main`;
- the Supervisor has fetched the accepted `origin/main`;
- a dedicated goal branch/worktree can be created without material ownership conflict.

Each repository-changing goal receives its own branch and PR. Independent branches may run concurrently. Dependent branches are never based on unmerged feature branches.

## 7. Conflict-aware parallelism

Parallelize aggressively only when goals do not have material write overlap. Use one of:

- separate modules;
- separate test files;
- isolated Git worktrees if the platform orchestration supports them safely;
- serialized execution for schema/core-model cross-cutting changes.

Do not create parallelism merely to maximize teammate count.

## 8. Development goal lifecycle

```text
PLANNED
  -> READY
  -> ASSIGNED
  -> IN_PROGRESS
  -> RESULT_SUBMITTED
  -> UNDER_REVIEW
     -> PASS
     -> REJECTED -> READY (new attempt)
     -> BLOCKED_HUMAN (rare)
```

## 9. Milestone lifecycle

```text
BLOCKED
  -> READY
  -> ACTIVE
  -> UNDER_REVIEW
  -> PASS
```

A milestone can return from `UNDER_REVIEW` to `ACTIVE` when acceptance detects defects.

## 10. Native `/goal` policy

### Supervisor

Use native `/goal` for the global M0–M13 completion condition whenever available. This keeps the lead working across turns until the terminal condition is demonstrated.

### Worker

Agent Team teammates receive goal contracts as tasks plus deterministic completion hooks. If native `/goal` is specifically required for a worker, use a detached CLI worker mode rather than assuming a lead can inject built-in slash commands into a teammate session.

## 11. Independent verification policy

Every development goal has two evaluation layers:

1. Worker completion evidence.
2. Supervisor acceptance.

Milestones add a third layer:

4. Milestone integration acceptance.

M13 adds a fourth:

5. Repository-wide release gate.

## 12. Retry policy

Default retry behavior:

- worker/test defect: retry automatically;
- teammate API error: resume/nudge/replace automatically;
- file conflict: serialize or use fresh worker/worktree;
- architectural mismatch: compare with locked ADRs, issue repair goal;
- repeated failure: use independent diagnosis/reviewer worker;
- only escalate if a formal Development Human Gate is triggered.

Do not use a fixed `max_retries` to abandon a required milestone. Continue until accepted or formally blocked by a Human Gate.

## 13. Completion evidence

Before stopping, the Supervisor must surface and persist:

- M0–M13 status table;
- all atomic goal PASS counts;
- repository-wide test/lint/type results;
- scenario A–J results;
- FDM-201 benchmark result;
- platform-adapter verification status;
- unresolved non-blocking limitations;
- Git commit/review references;
- final v0.1 release verdict.
