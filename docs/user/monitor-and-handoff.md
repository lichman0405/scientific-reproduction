# Execution Monitor recovery and filesystem Lab handoff

This guide documents two operational aspects of the v0.1 runtime (AC-02 of
DEV-M13-G04): how the Execution Monitor recovers after its own interruption,
and how wet-lab work is handed off through the filesystem LabAdapter. It is
grounded in the frozen specs `13-EXECUTION-MONITOR.md`,
`08-STRICT-RECOVERY-CLOSURE.md`, `10-EXPERIMENT-SUBSYSTEM.md`,
`12-ANALYSIS-SUBSYSTEM.md`, `15-ADAPTER-SPEC.md` and the implementation in
`src/scientific_reproduction/monitoring/`, `adapters/lab/`, `analysis/`.

## 1. The Execution Monitor role

The Execution Monitor is a logical **project-persistent** role and the only
v0.1 role requiring a high-availability runtime policy
(`13-EXECUTION-MONITOR.md` SS1). Its responsibilities (SS2): inspect external
Runs, poll Slurm states and LabAdapter incoming results, detect state
changes, validate minimal Result Package completeness, append events, move
Runs through atomic lifecycle transitions, execute whitelisted engineering
retries, spawn follow-up collection/analysis workers, maintain heartbeat and
checkpoint state, and recover after its own interruption. Its scientific
boundary (SS6, `agent-contracts/EXECUTION-MONITOR.md`): it reports
deterministic execution/QC facts — it never decides scientific questions.

## 2. Durable state of the Monitor

The Monitor is implemented as deterministic primitives over a durable state
directory (`src/scientific_reproduction/monitoring/`), following the M1
recovery discipline: every operation re-hydrates from disk and never trusts
session state. The files, all written atomically as canonical JSON through
`core.atomic.atomic_write`:

| File / directory | Module | Content |
|---|---|---|
| `<state_dir>/watched/<run_id>.json` | `registry.py` (DEV-M8-G01) | One watch entry per external Run: run id, external identity (`RunExternal`: backend + `dispatch_id` and/or `job_id` + working directory), adapter identity/version, `watched_at`, `last_heartbeat_at`. No credentials ever. |
| `<state_dir>/checkpoint.json` | `checkpoint.py` (DEV-M8-G01) | The recovery checkpoint: per-run reconciliation progress (observed state, timestamps). |
| `<state_dir>/heartbeat.json` | `checkpoint.py` (DEV-M8-G01) | Liveness record: monitor id, heartbeat timestamp, watched-run count. |
| `<state_dir>/trigger/<run_id>.json` | `triggers.py` (DEV-M8-G05) | Duplicate-trigger protection: at most one follow-up trigger per result-bearing Run. |
| `<state_dir>/run/<run_id>.json` | `core/state_backend.py` (`FilesystemStateBackend`) | The durable Run records whose lifecycle the Monitor moves. |
| `<state_dir>/event/` | `core/events.py` (`ProjectEventLog`) | The append-only event log (`external_status_change`, `engineering_retry_decision`, ...). |

The Monitor's identity (`sr_monitor_<32 hex>`) is a pure function of its
state directory (`derive_monitor_id`), so the registry, checkpoint store and
recovery procedure over the same directory always agree on who the Monitor
is. Heartbeat and checkpoint are plain state files — no git bookkeeping.

## 3. The recovery model

### 3.1 High-availability layers (13-EXECUTION-MONITOR.md SS3)

1. **Normal layer** — a persistent agent session in a durable shell/runtime
   (e.g. tmux), as selected by the platform adapter.
2. **Watchdog layer** — a minimal non-scientific watchdog checks liveness and
   may restart/resume the Monitor process.
3. **Session resume layer** — resume the original Monitor session through the
   platform adapter's `resume_session` capability (`15-ADAPTER-SPEC.md` SS5).
4. **Replacement layer** — if resume fails, a new Monitor session reconstructs
   state from the shared workspace, the latest checkpoint, the append-only
   event log and external truth (`sacct`/`squeue`, LabAdapter incoming files).

A brand-new Monitor must be able to take over **without chat-memory access**
(SS4): the checkpoint carries monitor identity, timestamp, Runs under watch,
external job/dispatch ids, last known statuses, pending event handling and
last completed actions.

### 3.2 Replacement reconstruction (`monitoring/recovery.py`, DEV-M8-G04)

`MonitorRecovery(state_dir)` is the deterministic replacement procedure:

- `reconstruct()` builds a `RecoveryPlan` from three durable sources: the
  watch set (`<state_dir>/watched/`), the checkpoint
  (`<state_dir>/checkpoint.json`) and the completion facts from the Run
  records plus the `external_status_change` event log. It is
  **observation-only** (AC-02): it writes nothing and never invokes the
  injected `dispatch` hook — the tests prove zero dispatch calls.
- `resume_engine()` returns a `ReconcileEngine` bound to the same state
  directory, clock, monitor identity, probe seam, run store and event log;
  the replacement's **first reconciliation pass** reconciles completions
  that occurred while the original Monitor was down **exactly once**
  (AC-03) — the engine's exactly-once machinery is reused, never
  reimplemented.
- Corrupt durable state fails loudly (`CorruptRecoveryStateError`), and a
  Run record whose external identity disagrees with its watch entry is
  refused (`RecoveryContractError`) — the replacement never resumes polling
  an external Run under a mismatched identity.

### 3.3 Reconciliation (`monitoring/reconcile.py`, DEV-M8-G02)

`ReconcileEngine` observes each watched Run through an injected
external-status probe. Only an exact probe result of `"RESULT_AVAILABLE"`
(the single member of `COMPLETION_SIGNALS`) completes a Run; every other
outcome (unknown, unavailable, backend-specific, probe exception) is
observed and recorded in the checkpoint but never treated as completion —
the default probe always reports unknown, so the default configuration can
never fabricate completion (AC-02). A completion (AC-01) is recorded exactly
once: the Run record transitions through `core.transitions.transition` to
`RESULT_AVAILABLE`, one `external_status_change` event is appended under a
deterministic idempotency key, and the checkpoint marks the observation —
a crash between any two steps converges on the same single completion on
restart.

### 3.4 Engineering retry (`monitoring/retry.py`, DEV-M8-G03)

The Monitor may perform **identical resubmission** for failures whose
adapter-recorded class is on `ENGINEERING_RETRY_WHITELIST` — today exactly
`"transport"` (connection-level: unreachable scheduler/node), the class
recorded by the ssh/slurm_ssh adapters (`adapters/compute/ssh.py`,
`adapters/compute/slurm_ssh.py`). A `"job"` class, an unclassified `None` or
any unrecognized string is a scientific compute failure: it is observed and
recorded as a refused decision and **never** resubmitted (safe by
construction). Every decision — authorized and refused — is appended as an
`engineering_retry_decision` event (actor `execution-monitor`, stable reasons
`engineering_failure_retry_authorized` / `scientific_failure_retry_refused`),
under an idempotency key, so a Monitor restart reconstructs the full retry
history from durable state alone and never resubmits twice.

### 3.5 Follow-up triggers (`monitoring/triggers.py`, DEV-M8-G05)

Each `RESULT_AVAILABLE` Run may trigger **one** semantic follow-up (a
collection/analysis request) through the injected follow-up hook; the
`TriggerRegistry` persists at most one trigger per Run ever, across repeated
scans and Monitor restarts (exactly-once, AC-01/AC-02).

### 3.6 Scientific recovery levels (08-STRICT-RECOVERY-CLOSURE.md)

The Monitor recovers *sessions*; scientific recovery of a failed Run is a
Supervisor decision governed by `08-STRICT-RECOVERY-CLOSURE.md` SS2, entered
only after a formal strict failure/inconclusive state:

- **Level 1** — minor/tacit detail recovery (drying detail, order of
  operations, brand/purity constraints);
- **Level 2** — limited parameter adjustment within the same method family;
- **Level 3** — method composition adjustment (precursor salt, modulator,
  solvent system);
- **Level 4** — method redesign, labeled `METHOD_REDESIGN`.

Recovery is recorded structurally: every goal contract carries its track
(`STRICT_REPRODUCTION` / `RECOVERY` / `METHOD_REDESIGN`,
`schemas/goal.schema.yaml`), assumptions are registered in the assumption
registry (`A0_TECHNICAL_DEFAULT`, `A1_METHODOLOGICAL_DEFAULT`,
`A2_SCIENTIFIC_ASSUMPTION`; A2 must not be silently used inside strict
reproduction), and a plan revision produces a new versioned draft
(`planning/freeze.py` `revise_plan`, `v1 -> v2-draft`, AC-03 — the old frozen
record is never rewritten). A Goal may close
`NOT_REPRODUCED_WITHIN_DEFINED_SCOPE` only when the frozen Closure Contract
is satisfied (SS4: statistical sufficiency, execution validity, diagnosis
completion, recovery-space exhaustion, research saturation), and final
project closure triggers a `TERMINATION_GATE` for human sign-off (SS6).

## 4. Filesystem Lab handoff

### 4.1 The handoff layout

The v0.1 reference LabAdapter is filesystem/manual handoff
(`15-ADAPTER-SPEC.md` SS2, `10-EXPERIMENT-SUBSYSTEM.md` SS2). The
`FilesystemLabAdapter` (`src/scientific_reproduction/adapters/lab/filesystem.py`,
DEV-M7-G01) is configured with a `base_dir` (the `lab/` directory of the
project tree) and derives the run directories from it; it never touches any
other location:

```text
<base_dir>/
  outgoing/<RUN_ID>/
    dispatch.json              the DispatchRecord
    manifest.json              the schema-gated execution package manifest
    <artifact files>           optional companion artifacts
    missing-result-request.json   (after request_missing_result)
  incoming/<RUN_ID>/
    result-manifest.json       the returned Result Package manifest
    <returned data files>      the declared data files
```

The project workspace created by `reproduce init` already contains
`lab/outgoing/` and `lab/incoming/` (`templates/PROJECT-TREE.template.txt`,
`planning.init.INIT_DIRECTORIES`).

### 4.2 The flow (`10-EXPERIMENT-SUBSYSTEM.md` SS1)

```text
Supervisor -> spawn Experiment Worker
Worker -> create Experiment Execution Package
LabAdapter -> outgoing handoff
Run -> RUNNING_EXTERNAL
Worker exits
...
Lab operator returns Result Package into incoming/
Execution Monitor detects result        (monitoring/reconcile.py)
Run -> RESULT_AVAILABLE                 (core/transitions.transition)
Analysis Worker processes data           (12-ANALYSIS-SUBSYSTEM.md)
Supervisor reviews
```

Wet-lab Runs are durable external tasks: worker sessions do not stay alive
throughout multi-hour experiments, and the original worker session is never
a single point of failure (`10-EXPERIMENT-SUBSYSTEM.md` SS1,
`11-COMPUTATION-SUBSYSTEM.md` SS6).

### 4.3 The adapter operations

- `dispatch(execution_package)` — writes the package to
  `outgoing/<RUN_ID>/` **after** the real schema gate
  (`core.schema_validation.validate_and_reject` against
  `schemas/lab-execution-package.schema.yaml`); a malformed package is
  refused loudly and nothing is written. Dispatch is exactly-once, and the
  `dispatch_id` is a deterministic pure function of the package identity.
  The adapter never touches the Run record: the **dispatching worker
  owns the run-record linkage** (`15-ADAPTER-SPEC.md` SS2) and must
  perform it right after a successful dispatch — record
  `run.external.dispatch_id` (the returned `DispatchRecord.dispatch_id`,
  plus `run.external.backend`) and advance the Run to
  `RUNNING_EXTERNAL` through the real transition machinery. The bundled
  helper `adapters.lab.linkage.link_run_to_dispatch(run_store, dispatch,
  now=clock)` performs both steps as one validated operation (real
  transition rules, real `run` schema gate, refuses a run already linked
  to a different dispatch). A run left at `READY`/`DISPATCHED` without a
  `dispatch_id` drifts away from the handoff: the Monitor only completes
  runs at `RUNNING_EXTERNAL`, so the returned package can never be
  reconciled onto it.
- `status(dispatch_id)` — reports `RUNNING_EXTERNAL` after dispatch and
  `RESULT_AVAILABLE` once a result manifest appears in `incoming/<RUN_ID>/`.
- `collect(dispatch_id)` — reads the returned result manifest and evaluates
  it with `evaluate_result_manifest` (`adapters/lab/manifest.py`); the
  collection is refused (`CollectionRejectedError`) iff anything is missing
  or mismatched: the run reference absent or not matching the dispatch's
  Run (AC-02 — never guessed), required manifest entries missing, declared
  data files absent, or the dispatched package's required returns uncovered
  (AC-03).
- `request_missing_result(dispatch_id, manifest_requirements)` — writes
  `missing-result-request.json` into the outgoing directory.

Result integrity is a hard rule (`10-EXPERIMENT-SUBSYSTEM.md` SS6): failed
Runs remain in the project; no cherry-picking/deletion because a later Run
succeeds. Independent replication is mandatory for experimental Goals
(SS5; default floor `n >= 3`, `00-README.md` decision 4), and unavailable
resources (`AVAILABLE`/`PROCURE`/`OUTSOURCE`/`CAPABILITY_GAP`, SS7) block
execution without invalidating the Goal.

### 4.4 Analysis of returned results (`12-ANALYSIS-SUBSYSTEM.md`)

Execution and analysis are separated: Analysis Workers independently process
the returned data using **frozen** Primary Analysis Protocols
(`12-ANALYSIS-SUBSYSTEM.md` SS1–SS3; `src/scientific_reproduction/analysis/protocols.py`
implements `register_analysis_record`, `freeze_primary_protocol`,
`revise_protocol`, `protocol_lineage` — a primary protocol is frozen before
data generation and revision produces a new version, never a rewrite).
Exploratory analysis is allowed only when labeled `EXPLORATORY_ANALYSIS` and
can never overwrite the Primary Analysis result (SS4). Analysis Result
Packages are registered exactly once through
`src/scientific_reproduction/analysis/results.py` (`register_result`,
`read_result`, `list_results`), with artifact refs resolved against the
artifact registry (`manifests/`) and the protocol/acceptance refs resolved
at registration (`UnresolvedResultReferenceError` otherwise) — the same
immutable-functional registry discipline as the planning registries.
