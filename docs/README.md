# Development documentation

Developer-focused documentation for the `scientific-reproduction` package
(M0 bootstrap, DEV-M0-G01).

## Repository layout

The repository root contains the frozen specification package. Start with:

- `00-README.md` — product overview
- `02-SYSTEM-ARCHITECTURE.md` — architectural model and planes
- `CLAUDE-CODE-HANDOFF.md` — implementation handoff, milestone plan, and
  repository layout for `scientific_reproduction/`
- `development/` — frozen Development Plan v1 (milestones, goals, policies)

Python package sources:

```text
src/scientific_reproduction/
  core/        core object model and filesystem state backend (M1)
  planning/    Supervisor planning primitives (M4)
  research/    research subsystem (M5)
  monitoring/  execution monitor (M8)
  workers/     worker context and permission enforcement (M6)
  analysis/    analysis/statistics subsystem (M9)
  adapters/    platform and execution adapters (M7, M10)
    lab/       lab adapter (filesystem handoff)
    compute/   compute adapter (local / SSH / Slurm)
    research/  research adapter interfaces
    platform/  Claude Code / Codex platform adapters
  domain_packs/
    materials_chemistry/  materials chemistry domain pack (M11)
  cli/         command-line entry points
  reporting/   reporting and audit (M13)
tests/         pytest suite (smoke tests at this stage)
scripts/       development tooling (see below)
```

At M0 only the skeleton exists: every package directory has a minimal
`__init__.py` and there is no scientific runtime behavior yet. Subsystems
are implemented in later milestones and must match this layout.

## Installation

Requirements: Python 3.11–3.13 (Windows or POSIX).

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows (PowerShell); use bin/activate on POSIX
python -m pip install -e ".[dev]"
```

No runtime dependencies are required by the package itself; `dev` extras
add pytest and pytest-cov.

## Verification

Quick local verification (installs, then runs the test suite):

```bash
python scripts/verify.py
```

The script works on Windows PowerShell, Git Bash, and POSIX shells. It
creates a fresh `.verify-venv` (`.verify-venv` is gitignored), installs
the package editable with dev extras, runs `python -m pytest -q`, prints
`[verify] PASS` / `[verify] FAIL`, and exits non-zero on failure. Use
`--reuse-venv` to reuse an existing verification environment.

Manual verification commands:

```bash
python -m pip install -e .
python -m pytest -q
```

## Scope note

Per `25-DEVELOPMENT-GIT-GOVERNANCE.md`, the root specification documents
(`0x-*.md`, `CLAUDE-CODE-HANDOFF.md`, `CONTRIBUTING.md`, ...), `schemas/`,
`.github/`, and `development/` are frozen outside development-goal scope.
Only the paths listed in each DEV-GOAL contract may change.
