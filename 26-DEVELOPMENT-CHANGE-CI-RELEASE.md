# 26 — Development Change Management, CI, and Release

## 1. Purpose

This document defines what happens when implementation discovers new work, how CI participates in acceptance, and how M0–M13 becomes a releasable version without bypassing Git governance.

## 2. Change classes

Every repository-changing DEV-GOAL attempt is classified as one of:

- `FEATURE`: new user-visible/runtime capability;
- `BUGFIX`: repair of incorrect behavior;
- `REFACTOR`: internal restructuring with no intended behavior change;
- `TEST`: tests/fixtures/verification only;
- `DOCUMENTATION`: docs/spec-index/operations text only;
- `CI`: workflow/quality-gate automation;
- `BUILD`: package/build/distribution machinery;
- `CHORE`: bounded maintenance that is not product behavior.

Classification determines branch and commit prefixes; it does not weaken acceptance.

## 3. New functionality discovered during implementation

A Worker may discover useful work but may not implement it silently.

It must submit a `DEVELOPMENT_CHANGE_REQUEST` containing:

```text
request_id
discovered_by_goal
problem
proposed_change
why_required
scope
architecture_impact
acceptance_impact
dependency_impact
suggested_change_type
urgency
```

Schema: `development/schemas/development-change-request.schema.yaml`.

The Supervisor classifies it:

### A. Necessary within current frozen goal

If it is strictly required to satisfy the existing goal without changing architecture or scope semantics, the Supervisor may explicitly authorize it inside the current PR and record the decision.

### B. Missing required implementation work

Create a new atomic DEV-GOAL and version the Development Plan (`DEV-PLAN-V2`, etc.) without rewriting Plan v1. The new goal gets its own branch, PR, acceptance, and dependencies.

### C. Architecture/product semantic change

Create an ADR/change proposal. If locked semantics would change, trigger `LOCKED_ARCHITECTURE_CONFLICT` or another defined Development Human Gate before execution.

### D. Nice-to-have

Place it in backlog; do not implement it during M0–M13 unless promoted through a formal plan revision.

## 4. Refactor policy

Refactoring is not a license for hidden feature work.

- If behavior is intended to remain unchanged, use `REFACTOR` and require regression proof.
- If behavior changes, classify the behavior change as FEATURE or BUGFIX.
- Broad cleanup discovered inside a feature PR should normally become a separate DEV-GOAL/PR.
- A tightly coupled tiny refactor may stay in the feature PR only when explicitly justified in the PR body and accepted by the Supervisor.

## 5. Bug policy

### Before merge

Repair within the existing DEV-GOAL PR when the frozen objective is unchanged.

### After merge

Create a new `BUGFIX` DEV-GOAL:

```text
fix/<new-goal-id>-<slug>
```

The fix must receive its own PR, tests, review, and merge.

### Release regression

Do not rewrite `main`. Revert the faulty merge when necessary, create a BUGFIX goal, and re-run affected milestone/release gates.

## 6. PR CI as mandatory evidence

A green local Worker test is insufficient.

Each PR must receive CI appropriate to the current milestone. M0 establishes the CI skeleton; later goals extend it.

Baseline required categories by the time they exist:

- development contract/schema validation;
- unit tests;
- lint/format check;
- type/static checks;
- affected integration tests;
- forbidden-path/scope checks where feasible;
- security/secret sanity checks;
- package/install smoke test as soon as packaging exists.

Later release CI additionally includes:

- lifecycle/rule engine suite;
- adapter tests;
- monitor/recovery tests;
- analysis/statistics tests;
- FDM-201 planning benchmark;
- scenarios A–J;
- traceability/audit validation.

CI is a gate, not the sole reviewer.

## 7. CI failure handling

CI failure is not a Human Gate.

The Supervisor:

1. identifies the failing check;
2. determines whether it is implementation, flaky infrastructure, or obsolete test;
3. delegates repair;
4. reruns the failed/relevant suite;
5. requires green status before merge.

A Worker may not delete/disable a legitimate failing test or lower a quality threshold solely to obtain green CI.

Changing a required quality gate itself requires explicit Supervisor justification and may require plan/ADR review.

## 8. PR review sequence

Recommended order:

1. Draft PR opened after first push.
2. Worker finishes implementation and local verification.
3. PR CI green.
4. Worker submits Development Result Package.
5. PR marked ready for review.
6. Supervisor reviews diff and current head SHA.
7. Supervisor reruns goal verification and relevant regression checks.
8. Supervisor submits review outcome.
9. PASS -> squash merge.
10. REJECT -> repair commits -> repeat from CI.

## 9. Milestone acceptance and Git

A Milestone is not PASS merely because all of its PRs merged.

Milestone closure requires:

```text
all required DEV-GOAL PRs merged
AND
all required DEV-GOAL Supervisor reviews PASS
AND
milestone integration/regression suite PASS on current main
AND
milestone architecture invariants PASS
AND
required documentation/audit state complete
```

Milestone integration defects create new BUGFIX/TEST/CHORE goals with their own PRs.

## 10. Release model

Use trunk-based integration into `main`; do not create long-lived milestone branches.

After M13:

1. ensure every required goal PR is merged;
2. ensure working tree is clean;
3. run the complete Release Acceptance Suite on the exact `origin/main` candidate SHA;
4. generate release manifest, changelog, and known limitations;
5. if a release fix is needed, create a normal BUGFIX PR;
6. repeat Release Acceptance on the new `main`;
7. when all gates pass, create immutable tag `v0.1.0`;
8. record the tagged commit SHA in the final development report.

Pre-release tags may use:

```text
v0.1.0-alpha.N
v0.1.0-beta.N
v0.1.0-rc.N
```

Do not tag a commit that has not passed the corresponding declared release gates.

## 11. Rollback

If merged code later proves harmful:

- prefer `git revert` of the squash merge;
- open the revert through a `fix/` or `chore/` PR unless immediate repository safety requires emergency action;
- create a BUGFIX DEV-GOAL for the durable repair;
- rerun all affected gates.

Do not rewrite published `main` history.

## 12. GitHub issues

Issues are optional planning/audit helpers, not the Development SSOT.

Use issues for:

- backlog items;
- externally visible bugs;
- deferred nice-to-have requests;
- release follow-up.

Frozen Development Plan contracts remain in the repository; GitHub issue state does not override them.

## 13. GitHub Actions and branch-protection downgrade

If Actions, branch protection, or PR mutation is temporarily unavailable:

- continue non-conflicting local development where useful;
- do not declare affected goal PASS if its frozen GitHub/CI gate cannot be satisfied;
- use `EXTERNAL_ACCESS_BLOCK` only when required external access blocks acceptance and no other unblocked work remains.

The Supervisor must not silently replace the required PR/CI workflow with direct pushes.
