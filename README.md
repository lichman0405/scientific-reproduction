# Scientific Reproduction Skill v0.1

An implementation-ready specification and runtime skeleton for a **Scientific
Reproduction Skill**: a packaged scientific workflow runtime that takes one
target paper and orchestrates a complete, auditable reproduction project —
literature research, experiment and computation execution, independent
analysis, statistics, recovery, and reporting. The first domain pack is
materials chemistry / computational materials science, with the official
FDM-201 reference case. See [`00-README.md`](00-README.md) for the product
overview and non-negotiable architecture decisions.

## Where to start

- [**Normative specification index — locked architecture decisions**](docs/spec/index.md)
  — every locked specification document mapped to its canonical path and
  purpose.
- [**Architecture Decision Records**](docs/adr/) — when and how implementation
  deviations from the locked architecture are recorded and approved.
- [`00-README.md`](00-README.md) — product overview and recommended reading
  order for implementers.
- [`docs/README.md`](docs/README.md) — developer documentation (repository
  layout, package skeleton).
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — development governance: branch/PR/CI
  rules, commit conventions, and the Supervisor-only merge authority.

## Quickstart

Requirements: Python 3.11–3.13 (Windows or POSIX).

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows (PowerShell); use bin/activate on POSIX
python -m pip install -e ".[dev]"
```

Run the canonical repository verification entry point:

```bash
python scripts/verify.py
```

`scripts/verify.py` is the canonical verification command for this repository:
it creates a fresh verification environment, installs the package editable with
dev extras, runs the pytest suite, and reports `[verify] PASS` / `[verify] FAIL`
(exit code non-zero on failure). Manual equivalent:

```bash
python -m pytest -q
```

## Development governance

This repository is developed autonomously by a Development Supervisor and
disposable workers (M0–M13), governed by the frozen specification. Read
[`CONTRIBUTING.md`](CONTRIBUTING.md), `25-DEVELOPMENT-GIT-GOVERNANCE.md`, and
`development/git-policy.v1.yaml` before making any change. Every
repository-changing DEV-GOAL uses one short-lived branch, one PR, required
verification/CI, and independent Supervisor review before merge.
