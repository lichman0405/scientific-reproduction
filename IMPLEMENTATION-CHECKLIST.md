# Implementation Checklist — Autonomous Supervisor Mode

This checklist is a human summary. Canonical development state is defined by `development/development-plan.v1.yaml`, milestone YAMLs, and atomic `DEV-GOAL` YAMLs.

## Supervisor bootstrap

- [ ] Start one Development Supervisor session.
- [ ] Enable/use Agent Teams when available; record fallback if not.
- [ ] Verify/configure `origin` as `lichman0405/scientific-reproduction` and establish specification-only `main` bootstrap if the remote is empty.
- [ ] Set the global M0–M13 `/goal`.
- [ ] Initialize `.development/` from frozen Development Plan v1.
- [ ] Validate the milestone/goal DAG before implementation.

## Automatic execution

- [ ] M0 PASS — Repository bootstrap.
- [ ] M1 PASS — Core models/filesystem state.
- [ ] M2 PASS — Lifecycle/rule engine.
- [ ] M3 PASS — Git audit/artifacts.
- [ ] M4 PASS — Planning primitives.
- [ ] M5 PASS — Research subsystem.
- [ ] M6 PASS — Worker context/permissions.
- [ ] M7 PASS — Lab/Compute adapters.
- [ ] M8 PASS — Execution Monitor/recovery.
- [ ] M9 PASS — Analysis/statistics.
- [ ] M10 PASS — Agent/platform adapters.
- [ ] M11 PASS — Materials Chemistry domain pack.
- [ ] M12 PASS — FDM-201 benchmark and scenarios A–J.
- [ ] M13 PASS — Reporting/final audit.

For each repository-changing atomic development goal:

- [ ] Dedicated goal branch/worktree created from accepted `origin/main`.
- [ ] Branch pushed; PR targets `main`.
- [ ] Required CI is green.
- [ ] Supervisor reviewed the current PR head SHA and diff.
- [ ] PASS goal was merged by Supervisor authority (default squash), with merge SHA recorded.
- [ ] Worker did not directly push/merge `main`.

For each atomic development goal:

- [ ] Worker result package exists.
- [ ] Supervisor reran required checks.
- [ ] Supervisor recorded PASS/REJECT.
- [ ] Rejected attempts remain retained.
- [ ] Newly unblocked goals were scheduled without waiting for routine user confirmation.

## Final release

- [ ] Full test suite passes.
- [ ] Lint/static checks pass.
- [ ] Clean install/package smoke test passes.
- [ ] FDM-201 planning benchmark passes 100% inventory mapping audit.
- [ ] Scenarios A–J all pass.
- [ ] Platform adapter release gates pass.
- [ ] Traceability/audit package validator passes.
- [ ] Failed Run retention verified.
- [ ] Final M0–M13 acceptance table and release verdict written.
