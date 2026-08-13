# Start Claude Code — Autonomous M0–M13 + GitHub PR Governance

This is the shortest handoff path.

Default implementation repository:

```text
https://github.com/lichman0405/scientific-reproduction.git
```

You may simply unpack this specification into the intended local project directory and run:

```bash
claude
```

Then paste the startup instruction from:

`development/templates/DEVELOPMENT-SUPERVISOR-START.md`

The Supervisor is responsible for verifying/initializing Git, configuring `origin`, and then autonomously completing M0–M13.

## Required Git behavior

If the GitHub repository is still empty, the Supervisor may perform exactly one direct `main` bootstrap commit containing only this specification/baseline metadata so that a PR base exists.

After that:

```text
one repository-changing DEV-GOAL
        ↓
one short-lived branch/worktree
        ↓
Worker implementation + commits
        ↓
push branch
        ↓
Draft PR → main
        ↓
CI + tests
        ↓
Supervisor independent current-head review
        ↓
PASS / REJECT
        ↓
squash merge / repair
```

Normal implementation must not be committed directly to `main`.

## Required reading order

The Supervisor should read first:

1. `00-README.md`
2. `20-ARCHITECTURE-DECISIONS.md`
3. `21-DEVELOPMENT-SUPERVISOR-SPEC.md`
4. `22-AUTONOMOUS-M0-M13-EXECUTION.md`
5. `23-DEVELOPMENT-GOAL-CATALOG.md`
6. `24-DEVELOPMENT-QUALITY-GATES.md`
7. `25-DEVELOPMENT-GIT-GOVERNANCE.md`
8. `26-DEVELOPMENT-CHANGE-CI-RELEASE.md`
9. `CLAUDE-CODE-HANDOFF.md`
10. `development/development-plan.v1.yaml`
11. `development/git-policy.v1.yaml`

## Autonomous completion condition

Do **not** stop after M0 or any intermediate milestone.

Finish only when:

- M0–M13 all `PASS`;
- 80/80 frozen required DEV-GOALs `PASS`, plus any formally added repair/change goals;
- every repository-changing goal has a valid branch/PR/review/merge trail;
- FDM-201 planning benchmark `PASS`;
- scenarios A–J `PASS`;
- full repository verification `PASS`;
- release audit `PASS`;
- no blocking Development Human Gate remains.

Agent Teams are an execution optimization, not the Single Source of Truth. `.development/`, Git history, and PR/review records are durable governance evidence.
