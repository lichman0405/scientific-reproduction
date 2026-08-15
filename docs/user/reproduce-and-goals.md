# `/reproduce` and `/goals` usage

This guide documents how the two user-facing surfaces of the product contract
(`01-PRODUCT-REQUIREMENTS.md` SS6 and SS9) are realized in the v0.2 codebase:
the `/reproduce` command-line primitive, and the `/goals` surface, which is
exposed as deterministic planning primitives and worker contracts rather than
a CLI. It describes the scientific runtime surfaces behind the skill entry
files (`SKILL.md`, `AGENTS.md`).

Grounding: `01-PRODUCT-REQUIREMENTS.md` SS4–SS6/SS9, `05-GOAL-RUN-SCHEMA.md`,
`08-STRICT-RECOVERY-CLOSURE.md`, `14-STATE-GIT-ARTIFACTS.md`,
`src/scientific_reproduction/cli/reproduce.py`,
`src/scientific_reproduction/planning/` (init, inventory, audit, plan, freeze,
dag, resources), `src/scientific_reproduction/workers/` (context, results,
permissions), `templates/PROJECT-TREE.template.txt`.

## `/reproduce` — initialize a one-paper project

The `/reproduce <PDF|DOI|URL>` primitive (SS9) is implemented as the
`reproduce` CLI (zero-install wrapper first, module form equivalent):

```bash
python scripts/reproduce.py init <target> [options]
python -m scientific_reproduction.cli.reproduce init <target> [options]
```

`<target>` is the primary target paper in exactly one of three forms
(validated deterministically by `planning.init.parse_target_form`, AC-03):

- a **local PDF path** ending in `.pdf`;
- a **DOI** matching `10.<4-9 digits>/<suffix>` (ISO 26324 style);
- an **http(s) URL** with a host.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--root DIR` | `.` | project root directory (created when missing) |
| `--project-id ID` | deterministic id derived from the target | explicit project id |
| `--title TEXT` | none | project title |
| `--domain-pack ID` | `materials-chemistry` | domain pack identifier |
| `--author-name NAME` | `Scientific Reproduction` | git author/committer name |
| `--author-email EMAIL` | `repro@example.org` | git author/committer email |
| `--timestamp ISO-8601` | now-UTC | pins state/event/commit timestamps |

### Exit codes

- `0` — project initialized;
- `1` — deterministic error (malformed target, already-initialized root,
  naive/non-ISO `--timestamp`); the message is printed on stderr;
- `2` — argument-parsing error.

### What `init` does

`reproduce init` calls `planning.init.initialize_project`, which:

1. creates the one-paper workspace tree (`INIT_DIRECTORIES` in
   `planning/init.py`, mirroring `templates/PROJECT-TREE.template.txt`):
   `sources/`, `knowledge/`, `inventory/`, `plans/`, `work-packages/`,
   `requirements/`, `goals/`, `runs/`, `evidence/`, `assumptions/`,
   `protocols/`, `analysis/`, `decisions/`, `events/`, `resources/`,
   `human-gates/`, `manifests/`, `locks/`, `lab/outgoing/`, `lab/incoming/`,
   `reports/`;
2. writes the project state record `project.yaml` (JSON content, validated
   against `schemas/project.schema.yaml`) with the primary target registered
   exactly once (the one-primary invariant, AC-01);
3. appends the `project.initialized` event under `events/`;
4. initializes the Git repository and records the "project initialized"
   audit checkpoint commit (`audit.git.init_project_repo` /
   `commit_checkpoint`, `14-STATE-GIT-ARTIFACTS.md` SS5).

The command performs **no network access and no inventory discovery**: the
`inventory/` directory is created empty (AC-02 of DEV-M4-G01).

### Determinism

`--timestamp` pins the state/event/commit records to an explicit timezone-aware
ISO-8601 value; `--author-name`/`--author-email` pin the Git identity (the
M3 audit helpers never consult git config). Identical inputs produce
byte-identical state and the same deterministic project id
(`core.ids.generate_id` over the target form + identifier).

### Example (verified against the implementation)

```bash
python -m scientific_reproduction.cli.reproduce init 10.1039/D5TA00771B \
  --root /tmp/fdm201-demo --title "FDM-201" --timestamp 2026-08-14T00:00:00Z
```

Output (exit code 0):

```text
initialized project sr_project_83f57b0b35f5235095c17318d1a9601b at <root>
primary target: doi '10.1039/D5TA00771B'
git checkpoint: aca01e900f52 (project initialized)
```

(The project id and commit sha differ per target; the shape is fixed.)

`register_primary_target(root, target)` is the one-primary enforcement point
(`planning.init`): because initialization always registers the primary target,
it rejects with `TargetAlreadyRegisteredError` on any unmodified project.
`read_project_state(root)` reads the persisted record back as a typed
`Project`.

`register_target_metadata(root, *, doi=None, title=None, timestamp=None)` is
the primary-target metadata-registration step of the research bootstrap
(`09-RESEARCH-SUBSYSTEM.md` section 2): it registers the paper DOI and/or
title on the existing primary target record and returns the updated `Project`.
A PDF target carries only its local path at init, so the DOI extracted from
the PDF during bootstrap research — or supplied manually by the operator —
is registered here; the record keeps exactly one primary target (AC-01) whose
form and identifier are never replaced, re-registering identical metadata is
a deterministic no-op, and a malformed DOI or one contradicting a DOI-form
identifier is rejected with a stable error.

## `/goals` — Goal DAG, contracts, and states

The semantic contract of `01-PRODUCT-REQUIREMENTS.md` SS6:

- `/goals` — summarize the Goal DAG and current states;
- `/goals show <GOAL_ID>` — show the frozen contract, dependencies, Runs
  and evidence;
- `/goals runs <GOAL_ID>` — show Runs;
- `/goals blocked` — show blocked Goals and blocker objects.

In v0.1 these are **not implemented as slash-command code in this
repository**: `/reproduce` has a CLI module (`cli/reproduce.py`), but `/goals`
has none. Per SS6, "platform adapters may expose these as slash commands or
equivalent commands" — the platform adapter surface is the `expose_command`
contract of `15-ADAPTER-SPEC.md` SS5 and the runtime role-contract descriptors
of `src/scientific_reproduction/adapters/platform/contracts/base.py`
(DEV-M10-G01). What v0.1 ships is the deterministic planning layer that every
`/goals` view is a pure function of, plus the worker execution surface.
Platform note: built-in slash commands cannot be reliably injected into
teammate sessions on all platforms, so the Supervisor uses native `/goal`
commands and teammates receive frozen goal contracts (SS5).

### The goal/run model

`05-GOAL-RUN-SCHEMA.md` fixes the hierarchy
`Project -> Work Package -> Requirement -> Goal (Unit Process) -> Run`, and
SS3: a Goal must be independently executable, observable, analyzable and
reviewable, with explicit inputs and outputs. The frozen `GoalContract` schema
(`schemas/goal.schema.yaml`) requires the track
(`STRICT_REPRODUCTION`/`RECOVERY`/`METHOD_REDESIGN`, `08-STRICT-RECOVERY-CLOSURE.md`),
dependencies (`hard_gate`/`soft_dependency`/`informational`, with separate
`execution_gate`/`acceptance_gate`), acceptance criteria, primary analysis
protocol reference, closure contract reference, resource requirements, the
automatic engineering retry policy, allowed outputs and forbidden
modifications, and version/freeze metadata. Run lifecycle states are the
`CREATED -> READY -> DISPATCHED -> RUNNING_EXTERNAL -> RESULT_AVAILABLE -> ...`
chain of `05-GOAL-RUN-SCHEMA.md` SS7; scientific PASS/FAIL is a Supervisor
review decision stored separately, never a Run lifecycle state.

### The real surfaces (file-by-file)

**Plan v1 builder and registries** — `src/scientific_reproduction/planning/plan.py`
(DEV-M4-G04):

- `register_goal(root, goal)`, `read_goal(root, goal_id)`, `list_goals(root)`
  — the goal-contract registry (drafts at `<root>/goals/<goal_id>.json`,
  immutable: a goal id registers exactly once);
- `register_acceptance`, `register_analysis_protocol`,
  `register_closure_contract` with the matching read/list helpers — the
  goal-contract family (`<root>/acceptance/`, `<root>/protocols/`,
  `<root>/closure/`; the last two directories are created on demand);
- `build_plan_v1(root)` — the deterministic Plan v1 draft, a pure function
  of the registered state (project + inventory items + requirements);
- `register_plan`, `read_plan`, `list_plans`, `plan_lineage` — the
  version-keyed plan registry (`<root>/plans/<version>.json`; `v<N>` /
  `v<N>-draft`), with the effective status computed by the `SUPERSEDED_RULES`
  table (a FROZEN record with a newer version registered reports
  `SUPERSEDED`; stored bytes are never rewritten, AC-03).

**Inventory and the 100%-coverage audit gate** —
`src/scientific_reproduction/planning/inventory.py` (DEV-M4-G02) and
`planning/audit.py` (DEV-M4-G03): `audit_inventory_registry(root)` /
`evaluate_completeness_audit(...)` decide freeze eligibility by the
`AUDIT_RULES` table — one unmapped or ambiguous formally reported item
fails the audit; 100% mapped passes.

**Freeze and versioned revision** — `src/scientific_reproduction/planning/freeze.py`
(DEV-M4-G04): `freeze_plan(root, plan)` is prohibited unless the completeness
audit passes (AC-01), produces the frozen Plan record and the frozen
Goal/Acceptance/Analysis/Closure contracts in memory (AC-02, drafts on disk
are never rewritten), and `revise_plan(root, plan)` creates the next draft
version (`v1 -> v2-draft`) from a registered FROZEN plan (AC-03).

**DAG and blocker views** — `src/scientific_reproduction/planning/dag.py`
(DEV-M4-G05): `build_plan_dag(root, version)` builds the ready-first
topological DAG of the plan's Goals with gate-kind classification
(`GATE_AXIS_RULES`, six-kind vocabulary), `export_plan_dag(root, version)`
renders it as JSON, and `resource_blockers_for_goal(...)` /
`resource_blocker_mapping(...)` compute the "/goals blocked" view from the
resource registry (`planning/resources.py`, `RESOURCE_GAP_STATES`). These
are the views `/goals` and `/goals blocked` summarize.

**Run records** — durable Run records live in the filesystem state backend
(`core/state_backend.py`, `FilesystemStateBackend`, kind `run` at
`<state_dir>/run/<run_id>.json`); their lifecycle is moved by the real
transition machinery (`core/transitions.transition`), which the Execution
Monitor uses to record external completion (`monitoring/reconcile.py` — see
[`monitor-and-handoff.md`](monitor-and-handoff.md)). The "/goals runs" and
"current states" views read these records.

**Worker execution surface** — `src/scientific_reproduction/workers/`:

- `context.py` (DEV-M6-G01): `generate_goal_context(root, goal, *,
  worker_role, ...)` produces the Goal Execution Context Package
  (`05-GOAL-RUN-SCHEMA.md` SS8) from the **frozen** Goal Contract only —
  a draft or a frozen record without a formal version is rejected
  (`GoalNotFrozenError`), and a relevance-reference filter
  (`RELEVANCE_FILTER_RULES`) keeps unrelated registry documents out;
- `results.py` (DEV-M6-G02): worker Result Package registration (typed
  `facts`/`data`/`deviations` sections, artifact refs resolved against the
  artifact registry, exactly-once records at `<root>/workers/results/`);
  a result package can never encode a requirement-level outcome — PASS/FAIL
  stays with the Supervisor decision layer;
- `permissions.py` (DEV-M6-G03): the role action matrix (`core.permissions`)
  that forbids workers from mutating plans or goals.

The role contracts that drive who may do what with goals are the frozen
descriptors of `adapters/platform/contracts/base.py` (DEV-M10-G01) and the
agent contracts in `agent-contracts/` (`SUPERVISOR.md`, `EXECUTION-MONITOR.md`,
`EXPERIMENT-WORKER.md`, `COMPUTATION-WORKER.md`, `ANALYSIS-WORKER.md`).

### Programmatic example (verified against the implementation)

```python
from scientific_reproduction.planning.plan import (
    list_goals, list_plans, plan_lineage, register_goal,
)
from scientific_reproduction.planning.freeze import freeze_plan
from scientific_reproduction.planning.dag import build_plan_dag, export_plan_dag
from scientific_reproduction.planning.audit import audit_inventory_registry
from scientific_reproduction.workers.context import generate_goal_context
from scientific_reproduction.core.models import GoalContract, WorkerRole

root = "path/to/initialized/project"   # from `reproduce init`

audit = audit_inventory_registry(root)   # 100%-coverage gate (planning/audit.py)
plan = build_plan_v1(root)               # deterministic v1-draft
frozen = freeze_plan(root, plan)         # FROZEN plan + frozen goal family
for goal in list_goals(root):            # goals/<goal_id>.json, sorted by id
    print(goal.goal_id, goal.title)
for entry in plan_lineage(root):         # effective status (SUPERSEDED computed)
    print(entry.plan.version, entry.status.value)
dag = build_plan_dag(root, "v1")         # ready-first topological DAG
context = generate_goal_context(root, frozen.goals[0], worker_role=WorkerRole.COMPUTATION_WORKER)
```

All registries are immutable-functional and deterministic: identical inputs
produce byte-identical records, and re-registration raises the stable
`DuplicateGoalError` / `DuplicatePlanVersionError` family.

## Relationship to the frozen plan lifecycle

These surfaces implement `01-PRODUCT-REQUIREMENTS.md` SS5 steps 4–8 exactly:
Research acquires sources, the Supervisor builds the Reproduction Inventory
(`planning/inventory.py`), the audit proves 100% coverage of formally
reported items (`planning/audit.py`), the Supervisor authors Work
Packages/Requirements/Goals/acceptance/analysis/closure records
(`planning/plan.py`), Plan v1 is frozen (`planning/freeze.py`), and eligible
workers then execute one Goal context at a time
(`workers/context.py` + `workers/results.py`) while the Execution Monitor
tracks external Runs (`monitoring/`).
