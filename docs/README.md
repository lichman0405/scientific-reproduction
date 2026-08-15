# Development documentation

Developer-focused documentation for the `scientific-reproduction` package.

## Repository layout

The repository root **is** the installable skill package (see
[`SKILL.md`](../SKILL.md) for the skill entry and
[`AGENTS.md`](../AGENTS.md) for the Codex entry). Start with:

- `00-README.md` — product overview
- `02-SYSTEM-ARCHITECTURE.md` — architectural model and planes
- `docs/spec/index.md` — normative specification index

Python package sources:

```text
src/scientific_reproduction/
  core/        core object model and filesystem state backend
  planning/    Supervisor planning primitives
  research/    research subsystem (state_helpers: role-facing authoring)
  monitoring/  execution monitor
  workers/     worker context and permission enforcement (run_helpers: run authoring)
  analysis/    analysis/statistics subsystem
  adapters/    platform and execution adapters
    lab/       lab adapter (filesystem handoff)
    compute/   compute adapter (local / SSH / Slurm)
    research/  research adapter interfaces
    platform/  Claude Code / Codex platform adapters
  domain_packs/
    materials_chemistry/  materials chemistry domain pack
  cli/         command-line entry points
  reporting/   reporting and audit
scripts/
  reproduce.py  zero-install `/reproduce` CLI wrapper
  smoke.py      platform-independent smoke verification
  verify.py     canonical repository verification entry point
schemas/        frozen product schemas (validated by the runtime)
agent-contracts/  frozen agent role contracts
benchmarks/fdm201/  official FDM-201 reference reproduction case
examples/fdm-201/   example instance files
templates/      skill/project skeleton templates
tests/          pytest suite
docs/           user, operations, spec, ADR, and release documentation
```

## Installation

Requirements: Python 3.11–3.13 (Windows or POSIX).

The skill is self-contained: `scripts/reproduce.py` runs the runtime with
**no install step** (it adds `src/` to `PYTHONPATH`). For development of the
package itself:

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

The root specification documents (`0x-*.md`, `SKILL.md`, `AGENTS.md`,
`CONTRIBUTING.md`), `schemas/`, `agent-contracts/`, and `.github/` are
frozen product content. Changes to them require an ADR under `docs/adr/`
(see `docs/adr/README.md`); implementation changes to `src/` go through
normal branch/PR/CI review per `CONTRIBUTING.md`.
