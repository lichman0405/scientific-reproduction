# 20 — Locked Architecture Decisions (ADR Summary)

This file is intentionally terse and normative. Claude Code should treat these as **locked v0.1 product decisions** unless the user explicitly changes them.

1. Skill Package, not pure prompt.
2. Core shared across Claude Code and Codex; platform adapters only for orchestration differences.
3. One target paper per Reproduction Project.
4. Primary entry: `/reproduce <PDF|DOI|URL>`.
5. Complete reproduction scope covers all formally reported main/SI/public-data experimental and computational items.
6. Unpublished/undeclared trial-and-error in author development is not mandatory.
7. Plan v1 must be complete before execution and pass a 100% Inventory Completeness Audit.
8. `/goal` = Unit Process; `run` = execution instance.
9. Independent experimental replication mandatory; default floor n>=3, final n dynamically designed.
10. Technical/instrument repeats cannot replace independent replication.
11. Acceptance Criteria frozen pre-data.
12. Primary Analysis Protocol frozen pre-data; later exploratory analysis labeled separately.
13. Statistics must support equivalence/uncertainty; “p>0.05 means same” is forbidden.
14. PASS/FAIL/INCONCLUSIVE required.
15. Strict, Recovery L1-L3, and Method Redesign L4 are distinct.
16. A2 Scientific Assumption prevents pure Strict classification.
17. Evidence is Source × Claim specific.
18. Reliability is checklist/rule-derived, not LLM intuition.
19. Evidence A/R/D axes use 0–4; composite ranking does not replace hard gates.
20. Closure Contract governs stopping; no fixed “N failures and stop”.
21. Project outcome and project phase are distinct.
22. Final outcomes: Fully, Partially, Not Reproduced Within Defined Scope, Inconclusive.
23. Requirement criticality CRITICAL/REQUIRED/SUPPORTING is checklist-derived and frozen pre-result.
24. Scientific outcome and Method Reproducibility are separate axes.
25. Goal dependencies have hard/soft/informational semantics and separate execution/acceptance gates.
26. Supervisor is sole scientific governance authority.
27. Workers do not propose scientific modifications.
28. Worker may auto-handle only whitelisted engineering failures.
29. Execution and analysis are separated.
30. Diagnosis Worker is dynamic; it diagnoses but does not design Recovery.
31. Three project-persistent roles: Supervisor, Research, Execution Monitor.
32. Execution Monitor is high-availability; Supervisor/Research are resumable but not mandatory 24×7 watchdog roles in v0.1.
33. Persistent role is not equivalent to an immortal session; a replacement session may assume a role from workspace state.
34. Shared workspace is Single Source of Truth.
35. v0.1 runtime state = filesystem + atomic writes + leases + append-only events.
36. No SQLite core dependency in v0.1; keep backend interface for future.
37. Git stores scientific audit history, not heartbeat noise.
38. Large raw data stored externally; manifests/checksums stored in project.
39. LabAdapter is standardized; filesystem/manual handoff is v0.1 reference.
40. ComputeAdapter standardized; Slurm-over-SSH is v0.1 primary.
41. Research uses open sources by default; commercial databases optional adapters.
42. Author contact disabled by default, optional only through Human Gate.
43. Resource/Procurement Plan is a first-class part of the reproduction plan.
44. Major resources/access/safety/scope/termination/external contact trigger Human Gates.
45. Concurrency is automatic but bounded by configurable policy.
46. Dynamic worker sessions should be automatically spawned by platform adapter; manual launch is fallback only.
47. v0.1 domain pack = materials chemistry/computational materials science.
48. FDM-201 DOI 10.1039/D5TA00771B is official reference case.
49. v0.1 acceptance = real FDM planning/orchestration + simulated execution scenarios; real full lab/HPC reproduction is later field validation.
50. Final deliverables include both human-readable report and machine-auditable package; failed Runs are retained.

## Development orchestration decisions (spec package v0.1.1)

*Historical: these decisions governed the v0.1.0 development campaign
(M0–M13), whose record lives on the `main` branch and is not part of this
release. They are kept here as locked history, not as current governance.*

### ADR-D01 — Autonomous M0–M13 Development Supervisor

The default implementation handoff is one long-lived Claude Code Development Supervisor operating continuously from M0 through M13. It must delegate atomic implementation goals to other Claude Code sessions/workers and independently review their output.

### ADR-D02 — Milestones are not worker tasks

M0–M13 are development Work Packages. The frozen atomic `DEV-GOAL` catalog (kept on the `main` branch) is the worker-level unit of development.

### ADR-D03 — Development state is external to Agent Team task state

The development-state directory (kept on the `main` branch) is the Single Source of Truth for implementation progress, reviews, attempts and gates. Claude Code Agent Team task lists are an execution mirror only.

### ADR-D04 — Global native /goal, worker goal-contract semantics

The Development Supervisor should use Claude Code native `/goal` for the global M0–M13 terminal condition. Agent Team teammates use frozen Development Goal Contracts plus deterministic completion hooks and Supervisor review. Detached native-`/goal` CLI workers may be used for self-contained tasks where this is advantageous.

### ADR-D05 — No routine milestone confirmation

The Development Supervisor must automatically progress the DAG and may ask the user only for a formal Development Human Gate. Ordinary defects, failed tests, teammate failures, merge conflicts and retry decisions do not require user confirmation.

## ADR-DEV-008 — Protected-main short-lived branch/PR integration

**Decision:** After the specification-only empty-repository bootstrap, every repository-changing atomic DEV-GOAL uses a dedicated short-lived branch and pull request targeting `main`.

**Rationale:** Autonomous parallel workers need isolation, auditable scope, deterministic integration points, and an independent review boundary. Long-lived milestone/develop branches create unnecessary integration graphs.

## ADR-DEV-009 — Supervisor-only merge authority

**Decision:** Workers may commit, push, and maintain their own goal PRs but may not merge. The Development Supervisor independently verifies the current PR head SHA, acceptance, scope, tests, and CI before authorizing merge.

## ADR-DEV-010 — Squash merge by default

**Decision:** One DEV-GOAL PR normally becomes one squash commit on `main`, retaining the DEV-GOAL ID in the title. Alternative merge methods require recorded justification.

## ADR-DEV-011 — New functionality requires formal change control

**Decision:** A Worker discovering functionality outside its frozen contract submits a `DEVELOPMENT_CHANGE_REQUEST`. The Supervisor decides current-goal necessity, new goal/plan version, ADR/Human Gate, or backlog. Opportunistic scope expansion is forbidden.
