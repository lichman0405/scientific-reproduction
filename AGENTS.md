# Scientific Reproduction Skill — Codex Entry

You are operating the **Scientific Reproduction Skill**. This directory is a
self-contained skill: the full specification is `SKILL.md`, and the normative
documents are indexed in `docs/spec/index.md`. Read `SKILL.md` first; this
file adds the Codex-specific operating instructions.

## How to run this skill in Codex

1. Treat this directory as the skill root. All relative paths below are
   relative to it.
2. Delegate every deterministic operation to the bundled runtime through the
   zero-install CLI wrapper (no pip install needed; Python 3.11+ required):

   ```bash
   python scripts/reproduce.py init <PDF|DOI|URL> --root <workspace>
   ```

   The wrapper adds `src/` to `PYTHONPATH` and runs
   `python -m scientific_reproduction.cli.reproduce`. Do not reimplement
   state, schema, lock, event, or adapter operations — call the runtime.
3. Verify the installation once with `python scripts/smoke.py`; it must
   print `[smoke] PASS`.

## Role governance

The reproduction is run by four roles defined in `.claude/agents/` and bound
by the frozen contracts in `agent-contracts/`:

- **Supervisor** — the only role that makes scientific decisions; owns the
  project end to end.
- **Research** — project-persistent evidence service.
- **Execution Monitor** — recovery and reconciliation of long-running runs.
- **Workers** — execute goals and report facts; never make Supervisor-level
  decisions.

Use Codex sub-agents (or sequential sessions with explicit role context) to
enact these roles. When Codex cannot spawn independent sessions, run the
roles sequentially in one session but keep the separation of duties: record
which role produced which record, and never let worker output substitute for
Supervisor adjudication. On Claude Code the role agents additionally
enforce these boundaries at the platform tool level: each `.claude/agents/`
definition carries a per-role `tools:` allowlist, so only the Supervisor
holds direct file-mutation and worker-dispatch tools; other roles' state
writes flow through the runtime CLI, which enforces the role-action matrix.

## Workflow

Follow the orchestration flow in `SKILL.md` (initialize → bootstrap research
→ inventory → freeze Plan v1 → execute goals → monitor/recover → independent
analysis → auditable report). The workspace created by `reproduce init` is
the Single Source of Truth; every state change goes through it, and every
scientific decision is recorded there. During bootstrap research, register
the primary target's DOI/title metadata on the project record
(`planning.init.register_target_metadata`) so a PDF target's identity is
machine-usable before Plan v1.

## Reference case

The official reference is FDM-201 (DOI `10.1039/D5TA00771B`); its frozen
benchmark state lives under `benchmarks/fdm201/`.
