# 13 — Execution Monitor

## 1. Role

Execution Monitor is a logical project-persistent role and, in v0.1, the only role requiring a high-availability runtime policy.

Preferred normal behavior: a Codex/Claude Code session remains active 24×7 in a persistent terminal/runtime.

## 2. Responsibilities

- inspect external Runs;
- poll or observe Slurm states;
- inspect LabAdapter incoming results;
- detect state changes;
- validate minimal Result Package completeness;
- append events;
- update operational Run state using atomic state transitions;
- execute whitelisted engineering retries;
- spawn follow-up collection/analysis workers according to frozen rules;
- maintain heartbeat and checkpoint state;
- recover after its own interruption.

## 3. High-availability layers

### Normal layer
Persistent agent session in a durable shell/runtime (e.g. tmux/similar environment as selected by platform adapter).

### Watchdog layer
A minimal non-scientific watchdog checks whether the Monitor process/session is alive. It may restart/resume it.

### Session resume layer
Attempt to resume the original Monitor session through platform adapter capability.

### Replacement layer
If resume fails, create a new Monitor session. It reconstructs state from:

- shared workspace;
- latest checkpoint;
- append-only event log;
- external truth (`sacct/squeue`, LabAdapter incoming files, etc.).

## 4. Checkpoint content

Checkpoint should include:

- monitor role/session identity;
- timestamp;
- Runs under watch;
- external job/dispatch IDs;
- last known statuses;
- pending event handling;
- last completed actions.

A brand-new Monitor must be able to take over without chat-memory access.

## 5. Event model

Events are append-only. Example:

```json
{
  "event_id": "EVT-000482",
  "timestamp": "...",
  "actor": "execution-monitor",
  "run_id": "RUN-COMP-017-01",
  "event_type": "external_status_change",
  "from": "RUNNING_EXTERNAL",
  "to": "RESULT_AVAILABLE",
  "reason": "slurm_job_completed"
}
```

## 6. No scientific interpretation

Monitor can say “job exited with code X” or “result package incomplete”. It cannot decide that a DFT model should use a new functional or that an experiment should change synthesis temperature.
