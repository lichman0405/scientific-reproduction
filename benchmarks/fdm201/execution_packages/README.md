# FDM-201 execution packages

Execution packages for the FDM-201 benchmark (Plan v1), generated for
DEV-M12-G04. They are planning packages: plans a lab-filesystem protocol
runner or a Slurm-over-SSH computation runner can execute, with every
scientific parameter traced to the frozen FDM-201 benchmark data.

## Contents

```
execution_packages/
  validate_packages.py                  deterministic validation gate (AC-01/02/03)
  README.md                             this file
  experiment/                           7 lab execution packages (GOAL-EXE-10..70)
    exp-pkg-01-pybc-ligand.yaml                 WP-10  GOAL-EXE-10
    exp-pkg-02-fdm201-synthesis-activation.yaml WP-20  GOAL-EXE-20
    exp-pkg-03-pxrd-characterization.yaml       WP-30  GOAL-EXE-30
    exp-pkg-04-porosity-n2-bet.yaml             WP-40  GOAL-EXE-40
    exp-pkg-05-single-component-isotherms.yaml  WP-50  GOAL-EXE-50
    exp-pkg-06-derived-thermodynamics.yaml      WP-60  GOAL-EXE-60
    exp-pkg-07-breakthrough.yaml                WP-70  GOAL-EXE-70
  computation/                           3 Slurm-oriented compute packages (GOAL-EXE-80..82)
    cmp-pkg-01-model-construction.yaml   WP-80  GOAL-EXE-80
    cmp-pkg-02-binding-energy.yaml       WP-81  GOAL-EXE-81
    cmp-pkg-03-gcmc.yaml                 WP-82  GOAL-EXE-82
  worker-context/                        10 worker-context packages (GOAL-EXE-10..82)
    wctx-goal-exe-10.yaml ... wctx-goal-exe-82.yaml
```

## Package types

- **Experiment packages** (`benchmark_lab_execution_package`) validate
  against `schemas/lab-execution-package.schema.yaml` and are oriented
  toward a lab-filesystem protocol runner: each `procedure` step carries
  an `action` plus `inputs`/`outputs` paths under a declared
  `filesystem.workspace_root`, and each package declares reagents,
  instruments, critical control variables, prohibited changes,
  operator records, safety notes, parameters (with `source_refs`) and a
  `traceability` block.

- **Compute packages** (`benchmark_compute_execution_package`) are the
  compute variant of the same schema: a `compute` section declares
  `scheduler: slurm`, A0 engineering planning defaults for
  partition/resource requests (precedent ANL-060; cluster specifics are
  injected by the Slurm-over-SSH adapter at submission from the
  inventoried compute access, RES-011), logical module groups mapped by
  the adapter from the inventoried software (RES-010), and every
  `procedure` step is an `sbatch_script` carrying `#SBATCH` directives.
  Scripts call a `run-command.sh` contract that the adapter fills at
  submission with the registered scientific tool command, so the
  packages never invent software commands.

- **Worker-context packages** (`benchmark_worker_context_package`)
  validate against `schemas/worker-context.schema.yaml` and contain
  only the context the worker of one goal needs: goal id/version,
  allowed/forbidden actions, environment, required outputs, and the
  goal/assumption/parameter/protocol references for that goal. They
  reference their execution package via `execution_package_refs`.

## Validation

```bash
python benchmarks/fdm201/execution_packages/validate_packages.py
```

Deterministic and offline; prints PASS/FAIL per package and exits 0
when every package validates, 1 otherwise. It checks:

- **AC-01** schema conformance (required keys, enums, element types;
  compute packages require `compute.scheduler == slurm` and `#SBATCH`
  in every step script).
- **AC-02** traceability: every id referenced under a `*_refs` key (and
  `plan_ref`) resolves to an id existing in the frozen benchmark data
  (INVENTORY, evidence, assumptions, resources, analysis plan, goals,
  plan, source records); `sr_package_*` references resolve to declared
  package ids.
- **AC-03** context isolation: the raw package text is scanned for
  forbidden patterns (development tooling, repository source/test
  paths, absolute local paths, unrelated project markers).
- Coverage: the execution packages and the worker-context packages each
  cover exactly the ten execution goals GOAL-EXE-10..82, once each.

## Scope note (issue #162)

`validate_packages.py` is a **benchmark-only** gate: it validates the
frozen DEV-M12-G04 corpus under `execution_packages/**` against the
frozen FDM-201 benchmark data. It is not part of the runtime dispatch
path and enforces nothing on real reproduction-project handoffs.

### What the runtime covers instead

The same acceptance concerns on real handoffs are enforced by runtime
primitives under `src/scientific_reproduction/`:

- **AC-01 (schema conformance)** — every runtime execution package is
  schema-gated on the way out: `workers.lab_package.generate_lab_execution_package`
  (issue #158) and `adapters.lab.filesystem.FilesystemLabAdapter.dispatch`
  gate the lab package through the real `lab-execution-package` schema;
  `adapters.compute.package` (issue #161) builds and validates the
  compute package against the real `compute-execution-package` schema,
  and `adapters.compute.local.LocalComputeAdapter.prepare` runs that
  gate before persisting anything.
- **AC-02 (traceability to the frozen Goal)** — runtime packages are
  derived only from the frozen Goal Contract (a non-frozen goal is
  refused, `GoalNotFrozenError`), and the dispatch path verifies the
  package's goal reference against the registered goal store before
  anything is written: the goal must be registered, frozen, and its
  formal version must match the package's `goal_version` (issue #159,
  `adapters.lab.filesystem`). Run registration resolves the goal the
  same way (`workers.run_helpers.register_run`). The corpus-wide
  reference scan of this validator has no runtime counterpart by
  design: the runtime constructs references from registered state
  rather than scanning hand-authored text.
- **AC-03 (context isolation)** — the runtime worker-context generator
  (`workers.context.generate_goal_context`) exposes only the minimum
  necessary context: the relevance-reference filter excludes every
  registry document the goal does not explicitly reference, and the
  context manifest records exactly which references were exposed
  (`context_hash` fingerprints the set). The raw-text forbidden-pattern
  scan of this validator is the benchmark-corpus mechanism; the runtime
  enforces the same substance constructively.

### What is still benchmark-only

- The frozen-corpus integrity checks: coverage of GOAL-EXE-10..82
  exactly once per package family, unique package ids, the frozen
  `benchmark_id` / `plan_id` / `goal_version` constants, and the
  directory layout.
- The checks against the frozen benchmark registers themselves (every
  `*_refs` value resolving into INVENTORY/evidence/assumptions/…, the
  `sr_package_*` cross-references, and the forbidden-pattern scan of
  the frozen fixture text).

### Known runtime gap (out of scope here)

The worker-context → execution-package binding (the benchmark's
`execution_package_refs`) has no runtime field or gate yet; it is
tracked by the open context-linkage issue (#160) and is not claimed as
covered.

Only `benchmarks/fdm201/execution_packages/**` is generated by this
milestone; all referenced benchmark data files are frozen and were not
modified.
