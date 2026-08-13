# 19 — Version Roadmap

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

- stronger watchdog/service install scripts;
- richer artifact synchronization;
- more robust resume/reconciliation logic;
- richer interactive `/goals` views;
- additional materials characterization protocols;
- provenance dashboard export;
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
