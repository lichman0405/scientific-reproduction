# Autonomous Development Package

This directory turns M0–M13 into a frozen, machine-readable autonomous implementation plan for Claude Code.

## Canonical files

- `development-plan.v1.yaml` — global mode, concurrency, Human Gates, terminal condition.
- `milestones/M0.yaml` … `M13.yaml` — milestone dependencies and milestone-level acceptance.
- `goals/DEV-M*-G*.yaml` — atomic worker contracts. There are 79 frozen development goals.
- `schemas/` — development plan/goal/milestone/result/review schemas.
- `templates/DEVELOPMENT-SUPERVISOR-START.md` — the intended first instruction.
- `templates/DEVELOPMENT-WORKER-CONTRACT.md` — default worker behavior.
- `hooks/README.md` — TaskCompleted/TeammateIdle quality-gate guidance.

## Runtime copy

The implementation repository should copy/instantiate these into a live:

```text
.development/
```

The source `development/` directory remains the frozen template. `.development/` holds live attempt/status/review state and must not alter frozen acceptance semantics without a formal revision/ADR.

## Execution rule

The Development Supervisor automatically runs from M0 to M13. It should not stop for routine confirmation. Ready goals are delegated, independently reviewed, and retried until accepted. Only a formal Development Human Gate may pause terminal progress.
