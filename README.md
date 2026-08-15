# Scientific Reproduction Skill v0.2

An installable **skill** for agentic coding platforms (Claude Code, Codex,
WorkBuddy): a packaged scientific workflow runtime that takes one target paper
and orchestrates a complete, auditable reproduction project — literature
research, experiment and computation execution, independent analysis,
statistics, recovery, and reporting. The first domain pack is materials
chemistry / computational materials science, with the official FDM-201
reference case. See [`00-README.md`](00-README.md) for the product overview
and non-negotiable architecture decisions.

## Get the skill

Download the release archive from
[GitHub Releases](https://github.com/lichman0405/scientific-reproduction/releases/tag/v0.2.1),
or clone the release branch:

```bash
git clone --branch release/skill-v0.2.0 https://github.com/lichman0405/scientific-reproduction.git
```

> Note: the default branch `main` carries the v0.1.0 development record and
> is **not** the skill distribution — always use the release branch or the
> release archive.

## Install as a skill

This repository **is** the skill directory. Copy it into your platform's
skill location (Python 3.11+ and git are the only requirements):

| Platform | Installation |
|---|---|
| **Claude Code** | copy this directory to `~/.claude/skills/scientific-reproduction/` |
| **WorkBuddy** | copy this directory to `~/.workbuddy/skills/scientific-reproduction/` |
| **Codex** | open this directory in Codex (it reads `AGENTS.md`) |

The skill entry is [`SKILL.md`](SKILL.md); Codex reads
[`AGENTS.md`](AGENTS.md). Verify any installation with:

```bash
python scripts/smoke.py
```

The runtime is bundled and **zero-install**: `scripts/reproduce.py` runs it
via `PYTHONPATH` — no venv, no pip.

```bash
python scripts/reproduce.py init 10.1039/D5TA00771B --root <workspace>
```

## Where to start

- [`SKILL.md`](SKILL.md) / [`AGENTS.md`](AGENTS.md) — what the skill does and
  how agents invoke it.
- [**Normative specification index**](docs/spec/index.md) — every locked
  specification document mapped to its canonical path and purpose.
- [`00-README.md`](00-README.md) — product overview, non-negotiable
  architecture decisions, recommended reading order.
- [`docs/user/installation.md`](docs/user/installation.md) — requirements,
  developer install, verification, troubleshooting.
- [`docs/user/reproduce-and-goals.md`](docs/user/reproduce-and-goals.md) —
  the `/reproduce` CLI and the `/goals` planning surfaces.
- [`docs/user/monitor-and-handoff.md`](docs/user/monitor-and-handoff.md) —
  Execution Monitor recovery and wet-lab handoff through the filesystem
  LabAdapter.
- [`docs/operations/adapters-slurm.md`](docs/operations/adapters-slurm.md) —
  Slurm-over-SSH compute adapter operations and limitations.
- [`docs/release/KNOWN-LIMITATIONS.md`](docs/release/KNOWN-LIMITATIONS.md) —
  known limitations and accepted risks of the v0.1 runtime.

## Verification (for contributors)

Requirements: Python 3.11–3.13 (Windows or POSIX), pip, venv.

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows (PowerShell); use bin/activate on POSIX
python -m pip install -e ".[dev]"
python scripts/verify.py
```

`scripts/verify.py` creates a fresh verification environment, installs the
package editable with dev extras, and runs the pytest suite, the ruff lint
gate, and the mypy type check — printing `[verify] PASS` / `[verify] FAIL`
(exit code non-zero on failure).

## Development governance

Read [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`20-ARCHITECTURE-DECISIONS.md`](20-ARCHITECTURE-DECISIONS.md) before making
any change. Every repository change uses one short-lived branch, one pull
request, required verification/CI, and independent review before merge.
