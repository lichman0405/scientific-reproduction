# 24 — Development Quality Gates

## 1. Principle

Workers do implementation. The Development Supervisor verifies completion. Claude Code task status is not itself proof of correctness.

## 2. Atomic goal gate

A `DEV-GOAL` cannot be accepted unless all of the following are true:

1. required deliverables exist;
2. goal-specific tests pass;
3. regression tests named by the contract pass;
4. scope constraints are satisfied;
5. forbidden semantic changes are absent;
6. worker result package is present;
7. Supervisor has independently inspected/re-run the required checks;
8. the DEV-GOAL branch has a PR targeting `main`;
9. required PR CI is green;
10. Supervisor has reviewed the current PR head SHA and scope/diff;
11. the reviewed head SHA still matches the merge candidate;
12. the Worker has not merged or directly pushed product changes to `main`.

## 3. TaskCompleted / TeammateIdle hooks

When Agent Teams are used, configure quality gates where practical:

- `TaskCompleted`: reject completion when mandatory deterministic verification fails;
- `TeammateIdle`: keep a teammate working when required output/tests are absent;
- `TaskCreated`: optionally reject malformed task subjects/contracts.

Hooks are a pre-filter. They do not replace Supervisor review.

## 4. Frozen acceptance

Development acceptance criteria shipped in `development/goals/*.yaml` and `development/milestones/*.yaml` are frozen inputs to Development Plan v1.

A worker cannot modify them.

The Supervisor may only revise a frozen criterion when:

- it is internally contradictory;
- it is technically impossible under a locked platform fact;
- satisfying it would violate another locked product decision.

Such revision requires an ADR and, if product semantics change, a Development Human Gate.

## 5. Repository-wide verification

M13 must execute the full verification suite established by M0. At minimum:

- unit tests;
- integration tests;
- schema validation;
- lifecycle/rule tests;
- scenario A–J suite;
- FDM-201 planning benchmark validation;
- lint/static checks configured by the repository;
- audit/traceability validator;
- packaging/install smoke test.

## 6. Failed attempts

Every rejected attempt remains recorded in `.development/attempts/` and `.development/reviews/`. It is not removed after a later successful repair.

## 7. PR and merge gate

A repository-changing DEV-GOAL is not `PASS` merely because code/tests are correct locally.

Required sequence:

`branch -> PR -> CI -> Worker result -> Supervisor current-head review -> PASS -> merge`

Default integration is squash merge. If a PR head changes after review, stale acceptance is invalid and relevant review/checks must be rerun.

## 8. Change-control gate

Unexpected feature/refactor scope must not be implemented silently. Workers submit a `DEVELOPMENT_CHANGE_REQUEST`; the Supervisor either authorizes it within the current frozen goal, creates a new goal/plan version, routes to ADR/Human Gate, or backlogs it.
