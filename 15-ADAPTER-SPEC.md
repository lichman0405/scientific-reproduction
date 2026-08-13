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
