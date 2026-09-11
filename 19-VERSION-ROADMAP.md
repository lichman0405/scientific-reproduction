# 19 — Version Roadmap

## Current state: v0.3.1 (local line, not upstream)

The shipped v0.3.1 line is the v0.2 base plus a local-only layer (see
`LOCAL-CHANGES.md`; distribution is a **zip archive** via
`scripts/package_dist.py`, never GitHub Releases):

- human-readable delivery gates + v3.1 zh/complete summary rendering;
- HumanGate registry (`planning/human_gates.py`) with close-out blocking
  and the "人工确认项" summary section;
- `planning.phase.advance_project_phase` — the sanctioned phase-advance
  API (rule-gated, exactly-once events, crash-window reconcilable);
- freeze-time trace-chain warnings (empty `acceptance.evidence_refs`);
- plain-SSH execution path in `docs/user/ssh-execution.md`;

The v0.2 upstream line remains the baseline of record for everything
documented below.

## v0.1 — Materials reproduction core

- filesystem StateBackend;
- Supervisor/Research/Monitor persistent roles;
- dynamic workers;
- Goal/Run/Requirement/Evidence models;
- strict/recovery/closure governance;
- statistics and primary-analysis freeze;
- Lab filesystem adapter;
- Slurm-over-SSH compute adapter;
- open-source research adapters;
- FDM-201 benchmark;
- Claude Code/Codex platform adapter layer.

## v0.2 — Operational hardening

Potential additions:

- ✓ stronger watchdog/service install scripts — watchdog liveness verdict
  shipped in v0.2.3; service install scripts still open;
- richer artifact synchronization;
- ✓ more robust resume/reconciliation logic — per-run error isolation,
  retry-policy consultation and persisted retry aftermath (v0.2.3);
- richer interactive `/goals` views;
- additional materials characterization protocols;
- ✓ provenance dashboard export — research-process provenance section in
  the final report (v0.2.3);
- first real field-validation feedback from FDM-201 or another MOF paper.

## v0.3 — Communication and lab integration

- email adapter;
- WeCom adapter;
- ELN/API adapter;
- standardized operator forms;
- result upload UI if warranted.

## v0.4 — Expanded computational domain

- richer VASP/CP2K/RASPA/LAMMPS/GROMACS execution profiles;
- environment detection;
- cost/resource estimation;
- automatic reproducible post-processing notebooks.

## v0.5 — Additional domain packs

Candidates:

- electrochemistry;
- heterogeneous catalysis;
- polymers;
- semiconductor/material devices.

## v1.0 — Mature reproduction runtime

Potential criteria:

- multiple real field validations;
- stable platform adapters;
- mature recovery/closure policies;
- documented safety/governance boundaries;
- robust source/evidence adapter ecosystem;
- optional SQLite/PostgreSQL StateBackend if concurrency warrants it;
- optional multi-project `Reproduction Program` layer.

## Explicit non-goal

Do not add architectural weight merely because it is possible. SQLite, web dashboards, distributed queues and external orchestration services should be introduced only when real v0.x usage demonstrates a concrete need.
