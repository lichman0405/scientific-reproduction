# 10 — Experiment Subsystem

## 1. Execution model

Wet-lab Runs are durable external tasks. Experiment Worker sessions do not remain alive throughout multi-hour/day/week experiments.

Typical flow:

```text
Supervisor -> spawn Experiment Worker
Worker -> create Experiment Execution Package
LabAdapter -> outgoing handoff
Run -> RUNNING_EXTERNAL / WAITING_EXTERNAL
Worker exits
...
Lab operator returns Result Package
Execution Monitor detects result
Run -> RESULT_AVAILABLE
Analysis Worker processes data
Supervisor reviews
```

The dispatching Worker performs the outgoing handoff **and** the
run-record linkage: it records the returned dispatch id on the Run
(`run.external.dispatch_id` / `run.external.backend`, via the bundled
`adapters.lab.linkage.link_run_to_dispatch` helper) and advances the
Run to `RUNNING_EXTERNAL` through the real transition machinery
(15-ADAPTER-SPEC.md SS2 "Run record linkage"). The adapter itself never
touches the Run record.

## 2. Lab Adapter v0.1

Use filesystem/manual handoff as the reference implementation:

```text
lab/
  outgoing/<RUN_ID>/
  incoming/<RUN_ID>/
```

Future adapters may include email, WeCom, ELN, internal API and other systems.

## 3. Experiment Execution Package

Must include at least:

- project/Goal/Run IDs;
- track and frozen Goal version;
- objective;
- exact protocol steps;
- reagents/materials and specification requirements;
- equipment requirements;
- critical control variables;
- allowed engineering handling;
- prohibited scientific modifications;
- independent/technical replicate identity;
- required operator records;
- required raw-data exports;
- sample labeling rules;
- deviation reporting rules;
- safety/resource notes;
- result-return manifest template.

## 4. Operator result requirements

Operators must not return only “success/failure”. Result Package should contain:

- actual quantities/conditions;
- reagent lot/grade where relevant;
- instrument ID/configuration;
- time/temperature logs where relevant;
- sample IDs;
- raw instrument files;
- images/photos when requested;
- operator notes;
- all deviations from protocol;
- failures/interruptions;
- manifest/checksums where practical.

The result manifest declares the returned data files and may map each
required raw-data export (the dispatched package's `required_return`
tokens) to the returned file that covers it (`required_return_files`),
so operators can return files under their natural names without
filename engineering.

## 5. Independent replication

Independent Runs must be independently executed at the process level. Re-measuring one batch does not count as independent synthesis replication.

## 6. Result integrity

Failed Runs remain in the project. No cherry-picking/deletion because a later Run succeeds.

## 7. Resource states

Each experiment may depend on Resource objects:

- `AVAILABLE`
- `PROCURE`
- `OUTSOURCE`
- `CAPABILITY_GAP`

Unavailable resources can block execution without invalidating the Goal itself.
