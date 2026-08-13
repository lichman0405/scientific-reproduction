# 11 — Computation Subsystem

## 1. v0.1 environment assumption

Common HPC environments are assumed to be SSH-accessible and primarily managed using Slurm + environment modules. Resource discovery/configuration occurs at execution time rather than blocking initial scientific planning.

## 2. Compute Adapter interface

Minimum semantic operations:

- `prepare(run_context)`
- `submit(run_context)`
- `status(run_id)`
- `collect(run_id)`
- `cancel(run_id)`
- `resume(run_id)`

Reference v0.1 adapters:

- `local`
- `ssh`
- `slurm_ssh` (primary)

## 3. Slurm-over-SSH responsibilities

The adapter, not the Computation Worker prompt, owns cluster-specific mechanics such as:

- connection/session details;
- module loading mapping;
- working/scratch path conventions;
- `sbatch` submission;
- `squeue`/`sacct` status inspection;
- `scancel`;
- result collection;
- job-ID persistence;
- checkpoint/resume mechanics;
- file integrity checks.

## 4. Computation Worker responsibilities

- materialize frozen scientific input;
- create input files;
- record software/version/environment;
- request required resources via adapter;
- submit job;
- register job ID and artifacts;
- collect raw outputs when triggered;
- report convergence/runtime facts;
- not change scientific parameters unless preauthorized by Goal policy.

## 5. Engineering versus scientific retry

Allowed automatic retry examples:

- SSH transient failure;
- scheduler node failure;
- identical resubmission after node/system failure;
- network timeout;
- checkpoint continuation with unchanged scientific parameters;
- verified redownload after transient transfer failure.

Supervisor-required examples:

- changing functional;
- changing force field;
- changing k-point mesh;
- changing cutoff or convergence tolerance when scientifically meaningful;
- changing thermostat/integration/model parameters;
- changing molecular charges;
- changing GCMC force-field mixing rules.

## 6. Long-job behavior

Worker submits -> exits. Monitor tracks job. Job completion -> result available -> new collection/analysis worker may be created. The original worker session is never a single point of failure.

## 7. Computational validation

Domain protocols must define numerical convergence/sampling validation and not rely solely on one final scalar value matching the paper.
