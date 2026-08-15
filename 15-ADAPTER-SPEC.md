# 15 — Adapter Specification

## 1. Why adapters exist

Scientific workflow objects must not depend on specific communication tools, HPC clusters or agent platforms.

## 2. LabAdapter

Minimum conceptual operations:

- `dispatch(execution_package)`
- `status(dispatch_id)`
- `collect(dispatch_id)`
- `request_missing_result(dispatch_id, manifest_requirements)`

v0.1 reference: filesystem/manual handoff.

Future: email, WeCom, ELN, custom API.

### Run record linkage (who maintains `run.external.dispatch_id`)

The adapter performs the handoff **only** and never touches the Run
record (`run.external.*`, lifecycle state): it is a pure function of the
injected handoff paths and has no knowledge of the project's run store.
The **dispatching orchestrator** (the Experiment Worker) owns the
linkage and must perform it immediately after a successful `dispatch`:

1. record the returned `DispatchRecord.dispatch_id` as
   `run.external.dispatch_id` (and the backend as
   `run.external.backend`); and
2. advance the Run to `RUNNING_EXTERNAL` through the real transition
   machinery (`READY -> DISPATCHED -> RUNNING_EXTERNAL`).

The bundled runtime ships the helper
`scientific_reproduction.adapters.lab.linkage.link_run_to_dispatch`
that performs both steps as one validated operation over the injected
run store: it re-hydrates the Run record, advances the lifecycle
through the real rules, refuses a run already linked to a different
dispatch, and persists through the store's real `run` schema gate.

A Run left in a pre-external state (`READY`/`DISPATCHED`) without a
`dispatch_id` drifts away from the handoff layer: the Execution Monitor
only records completions for runs at `RUNNING_EXTERNAL`, and the
returned package can no longer be addressed to its Run. The linkage is
exactly the kind of state consistency the lifecycle design exists to
enforce — never leave it to hand-maintained fields.

## 3. ComputeAdapter

Minimum conceptual operations:

- `prepare(run_context)`
- `submit(run_context)`
- `status(run_id)`
- `collect(run_id)`
- `cancel(run_id)`
- `resume(run_id)`

v0.1: local, ssh, slurm_ssh.

## 4. ResearchSourceAdapter

Operations vary by source type but should normalize:

- query/search;
- fetch metadata;
- fetch content/file when legally and technically available;
- stable identifiers;
- provenance;
- access limitations.

Commercial adapters are optional.

Network-capable adapters (live metadata fetch, content/file fetch) must validate every http(s) fetch target before opening a connection (`adapters/research/network_policy.py`): domain-name hosts are always allowed, because transparent-proxy fake-IP DNS legitimately resolves them into `198.18.0.0/15` (RFC 2544 / RFC 5735); IP-literal hosts inside that range are refused, because an IP literal bypasses DNS and is never produced by a fake-IP proxy (SSRF guard). The policy is a documented guard for the fake-IP range only; it is not a general SSRF firewall (09-RESEARCH-SUBSYSTEM.md section 4).

## 5. PlatformAdapter

Must implement a normalized orchestration contract:

- `spawn_persistent_role(role, project_context)`
- `spawn_worker(role, goal_context)`
- `resume_session(session_ref)`
- `terminate_session(session_ref)`
- `is_session_alive(session_ref)`
- `expose_command(command_spec)`
- `fallback_subagent(...)`

The Core must not embed platform-specific command syntax.

## 6. Adapter configuration

All environment-specific details belong in project/user configuration, not Goal scientific contracts. Examples:

- hostnames;
- SSH identity references;
- module names;
- queue/partition defaults;
- lab handoff directory;
- commercial database credential references.

Never store secrets directly in Git-tracked project records.
