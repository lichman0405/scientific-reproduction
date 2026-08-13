# 02 — System Architecture

## 1. Architectural model

```text
User / PI
   |
   v
Supervisor  <------------------------------+
   |                                        |
   +--> Research (persistent)               |
   |                                        |
   +--> Execution Monitor (persistent + HA) |
   |                                        |
   +--> spawn dynamic workers --------------+
          | Experiment Worker
          | Computation Worker
          | Analysis Worker
          | Diagnosis Worker

Shared Project Workspace = Single Source of Truth
   |
   +-- source/evidence knowledge
   +-- plan/goal/run state
   +-- append-only events
   +-- Git-audited decisions
   +-- artifact manifests

External execution systems
   |-- LabAdapter -> human laboratory / filesystem handoff
   +-- ComputeAdapter -> local / SSH / Slurm
```

## 2. Persistent roles

### Supervisor
Project-persistent, resumable. Owns all scientific governance and final decisions.

### Research
Project-persistent, resumable. Performs mandatory bootstrap research and later Supervisor-directed searches.

### Execution Monitor
Project-persistent, high-availability. Tracks long-running external Runs and handles deterministic execution events.

## 3. Dynamic roles

### Experiment Worker
Executes a lab Unit Process Run or prepares a Lab Execution Package. Does not redesign protocol.

### Computation Worker
Prepares, submits, or collects computational Runs. Does not change scientific settings outside a frozen engineering-retry policy.

### Analysis Worker
Performs pre-specified analysis on returned data. No acceptance authority.

### Diagnosis Worker
Performs evidence-bounded failure analysis when created by Supervisor. It may report candidate causes and evidence strength but must not propose formal plan modifications.

## 4. Separation of concerns

### Scientific control plane
Supervisor + frozen project records.

### Execution control plane
Execution Monitor + Lab/Compute adapters + Run lifecycle.

### Evidence plane
Research + evidence registry + claim-specific assessments.

### Data plane
Raw artifacts in artifact storage; manifests in project repository.

## 5. Session lifecycle versus Run lifecycle

Sessions are disposable. Runs are durable.

Example:

```text
Computation Worker A
 -> prepares VASP input
 -> submits Slurm job 3842716
 -> records job id
 -> exits

RUN-COMP-004-02 remains RUNNING_EXTERNAL

Execution Monitor observes job completion
 -> marks RESULT_AVAILABLE
 -> spawns Computation/Analysis Worker B
 -> Worker B reads shared state and artifacts
```

No scientific Run may depend on one session remaining alive for days.

## 6. Platform adapter boundary

The Core must never embed Claude Code or Codex-specific orchestration logic. Platform adapters implement:

- spawn role/session;
- resume role/session;
- terminate session;
- attach Goal Execution Context Package;
- invoke subagent fallback when separate sessions are unavailable;
- manage persistent shell/watchdog integration;
- expose semantic commands.

## 7. State backend

v0.1 uses a filesystem StateBackend:

- object-per-file rather than one monolithic state file;
- atomic temp-write + rename;
- lock/lease files for contested resources;
- append-only event records;
- deterministic IDs;
- recoverable current state derived from object files + recent events + external adapter truth.

Do not introduce SQLite as a core dependency in v0.1.

## 8. Audit architecture

Git stores scientific history, not every heartbeat.

Commit-worthy events include:

- Plan freeze/revision;
- Goal Contract freeze/revision;
- acceptance criterion revision;
- Primary Analysis Protocol revision;
- Recovery creation;
- Supervisor decision;
- Requirement closure;
- project final outcome.

## 9. Large artifact handling

Raw experimental/computational data may be too large for Git. Store externally and register using:

- artifact ID;
- Run ID;
- URI/path;
- SHA256 checksum;
- size;
- timestamps;
- source/producer;
- metadata and MIME/type.
