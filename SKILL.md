---
name: scientific-reproduction
description: >-
  Orchestrate a complete, auditable reproduction of a scientific paper using
  multi-session research, experiment, computation, analysis, monitoring and
  Supervisor governance. Use when the user wants to reproduce a scientific
  paper, initialize or resume a reproduction project, plan/execute/monitor
  reproduction goals, or produce an auditable reproduction report.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - WebFetch
---

# Scientific Reproduction

Reproduce one scientific paper end to end — literature research, experiment
and computation execution, independent analysis, statistics, recovery, and a
machine-auditable report — under strict role governance.

## Entry

Primary semantic command: `/reproduce <PDF|DOI|URL>`.

## Governance (non-negotiable)

Load and obey the role contracts in `agent-contracts/`, the locked decisions
in `20-ARCHITECTURE-DECISIONS.md`, and the role definitions in
`.claude/agents/`. Enforce the separation of duties:

- **Workers report facts. Only the Supervisor makes scientific decisions.**
- The Supervisor owns the project end to end; Research is the
  project-persistent evidence service; the Execution Monitor owns recovery.
- Freeze acceptance criteria and primary analysis protocols before execution.
- Assess every piece of evidence as Source × Claim — never one global score.

## Runtime operations

Delegate deterministic state, schema, lock, event and adapter operations to
the bundled runtime through the zero-install CLI wrapper (no pip install
needed; Python 3.11+ required):

```bash
python scripts/reproduce.py init <PDF|DOI|URL> --root <workspace>
```

The wrapper runs `python -m scientific_reproduction.cli.reproduce` from the
bundled `src/`. The command initializes the one-paper project workspace
(`project.yaml` plus the frozen directory tree, see
`templates/PROJECT-TREE.template.txt`) and records a git checkpoint commit.
The workspace is the Single Source of Truth — sessions are replaceable
executors (see `14-STATE-GIT-ARTIFACTS.md`).

`/goals` views are deterministic pure functions of the planning layer (see
`docs/user/reproduce-and-goals.md`); expose them through the platform's
slash-command mechanism where available, otherwise through the runtime.

## Orchestration of the reproduction flow

1. **Initialize** — `/reproduce` the primary target paper (exactly one per
   project) and record the primary target.
2. **Bootstrap research** — build the complete evidence base from the main
   paper, SI, and linked public data; register the primary target's
   DOI/title metadata on the project record (a PDF target carries only its
   path at init); collapse duplicate DOI mirrors; never treat mirrors as
   independent evidence.
3. **Inventory** — enumerate *all* formally reported experiments, controls,
   and computations; audit for 100% coverage.
4. **Plan v1** — build and freeze Plan v1 (goals as Unit Processes, DAG
   dependencies, resource closure) before any execution.
5. **Execute** — delegate goals to worker sessions; record every run,
   attempt, and artifact in the workspace; wet-lab work hands off through
   the filesystem LabAdapter (`lab/outgoing`, `lab/incoming`).
6. **Monitor and recover** — the Execution Monitor reconciles long-running
   runs and applies the L1–L3 recovery ladder (`08-STRICT-RECOVERY-CLOSURE.md`).
7. **Analyze independently** — analysis is separated from execution; apply
   the frozen statistics and acceptance governance
   (`07-STATISTICS-AND-ACCEPTANCE.md`).
8. **Report** — produce a human-readable report and a machine-auditable
   reproduction package with full traceability.

## Frozen data to respect

- `schemas/` — frozen product schemas (project, goal, run, evidence, ...).
  Validate against them; never edit them.
- `agent-contracts/` — frozen role contracts.
- `benchmarks/fdm201/` — the official reference case (FDM-201,
  DOI `10.1039/D5TA00771B`, materials chemistry domain pack).
- `examples/fdm-201/` — example instance files.

## Verification

Verify any installation with:

```bash
python scripts/smoke.py
```

It checks the skill structure, imports the bundled runtime, and runs a real
`reproduce init` for the reference DOI with output assertions.

## Platform notes

- **Claude Code**: install by copying this directory to
  `~/.claude/skills/scientific-reproduction/`; role agents load from
  `.claude/agents/`.
- **WorkBuddy**: install by copying this directory to
  `~/.workbuddy/skills/scientific-reproduction/` (directory name must match
  the `name` frontmatter).
- **Codex**: open this directory; Codex reads `AGENTS.md` as its entry.
