# 25 — Development Git Governance

## 1. Status

This document is **normative** for autonomous implementation of Scientific Reproduction Skill v0.1.

Git is not merely a backup mechanism. Branches, commits, pull requests, CI checks, Supervisor review, and merge records are part of the Development Supervisor's execution protocol.

Repository:

- GitHub: `lichman0405/scientific-reproduction`
- Remote: `https://github.com/lichman0405/scientific-reproduction.git`
- Default integration branch: `main`
- Development model: protected-main, short-lived goal branches, pull-request integration

The canonical machine-readable policy is `development/git-policy.v1.yaml`.

## 2. Core invariants

After the one-time empty-repository bootstrap exception described below:

1. **No product implementation change is committed directly to `main`.**
2. Every atomic `DEV-GOAL` that changes repository content uses its own branch.
3. Every such branch is pushed to `origin`.
4. Every such branch is integrated through a pull request targeting `main`.
5. Worker completion never authorizes merge.
6. A PR cannot merge until goal-specific verification, required CI, and independent Supervisor review pass.
7. The Supervisor is the only development role that may authorize merge.
8. Default merge method is **squash merge**.
9. The merged PR is the durable integration/audit record for the DEV-GOAL.
10. Force-pushing or rewriting `main` history is forbidden.

This protocol applies to `FEATURE`, `BUGFIX`, `REFACTOR`, `TEST`, `DOCUMENTATION`, `CI`, `BUILD`, and `CHORE` changes, except ephemeral Supervisor runtime state that is intentionally not product source.

## 3. One-time empty-repository bootstrap exception

An empty GitHub repository has no usable base branch for PRs. Therefore one exception is permitted:

- If `origin` has no `main`, the Development Supervisor may create the initial `main` commit containing only the supplied specification/baseline repository metadata required to establish the project.
- Recommended commit:
  `chore(spec): import autonomous implementation specification v0.1.2`
- The Supervisor then pushes `main` once.
- **No product feature implementation may be included in this bootstrap commit.**
- Immediately after `main` exists, normal branch/PR governance becomes mandatory.
- `DEV-M0-G00` is the first governed implementation PR and establishes repository PR/CI scaffolding.

If `origin/main` already exists, this exception does not apply. The Supervisor must fetch and reconcile with the existing history and must not overwrite it.

## 4. Branch-per-goal model

A branch belongs to exactly one active DEV-GOAL attempt.

Branch format:

```text
<prefix>/<goal-id-lower>-<short-slug>
```

Prefixes:

| Change type | Prefix |
|---|---|
| FEATURE | `feat/` |
| BUGFIX | `fix/` |
| REFACTOR | `refactor/` |
| TEST | `test/` |
| DOCUMENTATION | `docs/` |
| CI | `ci/` |
| BUILD | `build/` |
| CHORE | `chore/` |

Examples:

```text
feat/dev-m4-g03-plan-freeze
fix/dev-m8-g04-monitor-resume-race
refactor/dev-m3-g02-artifact-boundary
test/dev-m12-g06-recovery-scenarios
docs/dev-m13-g04-operations-guide
ci/dev-m0-g00-github-governance
```

Retry branches may add `-r2`, `-r3`, etc. only when a fresh branch/PR is required.

Do not use long-lived feature, milestone, or `develop` branches for normal M0–M13 integration. `main` is the only ordinary integration base.

## 5. Dependency-aware branch creation

Before creating a DEV-GOAL branch:

1. all `depends_on` goals must have Supervisor status `PASS`;
2. their PRs must be merged into `origin/main`;
3. the Supervisor must fetch `origin`;
4. the new branch must start from the current accepted `origin/main` commit.

A dependent goal must never be implemented from an unmerged upstream feature branch.

Independent goals may branch concurrently from the same accepted `origin/main` base when write scopes do not materially overlap.

## 6. Worktrees and worker isolation

For parallel workers, prefer one Git worktree per active DEV-GOAL attempt:

```text
.worktrees/<goal-id>/<attempt-id>/
```

Rules:

- `.worktrees/` must not be committed.
- One Worker receives one worktree and one goal branch.
- A Worker must not switch to another Worker's branch.
- A Worker must not modify another goal's worktree.
- Overlapping write ownership is resolved by the Supervisor before parallel dispatch.
- If two goals need the same cross-cutting file, serialize them or create a prerequisite integration goal.

## 7. Commit standard

Use Conventional Commit semantics plus the DEV-GOAL identifier.

Format:

```text
<type>(<scope>): <imperative summary> [<DEV-GOAL-ID>]
```

Examples:

```text
feat(runtime): implement run lifecycle [DEV-M2-G01]
fix(state): reject stale lease overwrite [DEV-M1-G03]
test(adapters): cover slurm node failure retry [DEV-M7-G04]
docs(ops): document monitor recovery [DEV-M13-G04]
refactor(core): isolate state backend interface [DEV-M1-G02]
ci(repo): add pull-request quality gates [DEV-M0-G00]
```

Requirements:

- each commit is a coherent logical unit;
- commit text must identify the DEV-GOAL;
- vague messages such as `update`, `changes`, `fix stuff`, `Claude changes` are forbidden;
- unrelated cleanup must not be hidden in a feature commit;
- secrets, credentials, generated large artifacts, caches, and transient `.development` runtime data must not be committed.

Worker branches may contain multiple coherent commits. The default final integration is still one squash merge per DEV-GOAL PR.

## 8. Pull request lifecycle

Normal lifecycle:

```text
READY DEV-GOAL
  -> branch/worktree
  -> Worker implementation
  -> local verification
  -> commit(s)
  -> push branch
  -> open Draft PR
  -> CI
  -> Worker result package
  -> mark Ready for Review
  -> Supervisor independent review
  -> PASS or REJECT
  -> merge or repair
```

A PR is not a substitute for a goal contract. The PR must reference the frozen `DEV-GOAL`.

PR title should be suitable for squash-merge history, for example:

```text
feat(runtime): implement run lifecycle [DEV-M2-G01]
```

The body must include:

- Goal ID and milestone;
- objective;
- files/modules changed;
- acceptance-criteria checklist;
- exact verification commands and outcomes;
- architecture/contract impact;
- known limitations;
- change-request/ADR references if any.

Template: `development/templates/PULL_REQUEST_TEMPLATE.md`.

## 9. Worker authority

A Worker may:

- edit only its permitted goal scope;
- run tests;
- make commits on its goal branch;
- push its branch;
- create/update its draft PR;
- report evidence.

A Worker may **not**:

- push directly to `main`;
- merge its own PR;
- approve its own PR as the final authority;
- force-push `main`;
- bypass hooks/checks with `--no-verify`;
- weaken CI or acceptance criteria to make a PR green;
- silently broaden scope;
- add unrelated new features.

## 10. Supervisor PR review gate

For every PR, the Supervisor must review the **current head SHA**, not merely a previous worker report.

Before PASS:

1. fetch the PR/head;
2. inspect changed filenames and diff;
3. verify only permitted scope changed;
4. review commits and PR metadata;
5. verify frozen acceptance criteria;
6. independently rerun required deterministic checks where practical;
7. confirm required GitHub CI checks are green;
8. confirm no unresolved blocking review thread remains;
9. confirm the PR head SHA has not changed since verification;
10. record PASS/REJECT.

If the head changes after acceptance review, acceptance is invalidated and the relevant verification must be rerun.

## 11. Merge policy

Default merge method: **squash**.

Only the Development Supervisor may authorize merge after PASS.

The squash commit title must retain the DEV-GOAL ID.

After successful merge:

1. record merge commit SHA;
2. update `.development` goal/attempt state;
3. fetch `origin/main`;
4. remove local worktree when safe;
5. delete the remote goal branch when safe;
6. recompute the DAG and dispatch newly ready goals.

`merge` or `rebase` integration methods may be used only when the Supervisor records why squash would destroy materially useful audit semantics.

## 12. Rejected PRs and repair

A rejected PR is normal development history.

If the goal contract is unchanged and the branch remains clean:

- keep the same PR open;
- send the failure evidence to a repair Worker;
- add repair commit(s);
- rerun CI and Supervisor review.

Use a fresh branch/PR when:

- the old branch is contaminated by unrelated changes;
- the repair requires destructive history surgery;
- the retry is intentionally isolated;
- the original PR is closed/abandoned.

Rejected attempts must remain recorded in `.development/attempts/` and `.development/reviews/`.

## 13. Merge conflicts

Merge conflicts are **not** a Development Human Gate.

The Supervisor should:

1. identify ownership and dependency cause;
2. update/rebase the branch onto accepted `origin/main`;
3. delegate conflict resolution to a bounded repair Worker if product code changes are needed;
4. rerun affected tests and CI;
5. invalidate stale prior review evidence;
6. re-review before merge.

If the conflict exposes contradictory frozen architecture contracts, escalate under `LOCKED_ARCHITECTURE_CONFLICT`.

## 14. Direct-main and force-push prohibitions

After bootstrap:

- no Worker may commit or push implementation directly to `main`;
- the Supervisor also must not use direct-main implementation commits as a shortcut;
- `git push --force` or `--force-with-lease` to `main` is forbidden;
- published release tags must not be moved;
- public history should be repaired with `git revert` and a new BUGFIX goal, not rewritten.

## 15. GitHub repository protection

Where repository settings and permissions allow, configure `main` to require:

- pull request before merge;
- required CI status checks;
- resolved review conversations where supported;
- no force pushes;
- no branch deletion of `main`.

Actual GitHub protection settings are defense-in-depth. The Development Supervisor must still enforce this document even when administrative settings are unavailable.

## 16. Audit fields

Every development attempt should retain:

```text
goal_id
attempt_id
change_type
base_sha
branch
worktree
commit_shas
pr_number
pr_url
pr_head_sha
ci_status
supervisor_reviewed_sha
merge_method
merge_commit_sha
merged_at
```

Schemas: `development/schemas/development-attempt.schema.yaml`,
`development/schemas/development-result.schema.yaml`, and
`development/schemas/development-review.schema.yaml`.
