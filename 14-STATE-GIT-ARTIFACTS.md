# 14 — State, Git and Artifact Architecture

## 1. Single Source of Truth

The project workspace is the durable truth. Agent sessions are not authoritative memory.

## 2. v0.1 StateBackend

Filesystem only.

Required properties:

- per-object state files;
- atomic write via temp file + rename;
- lock/lease for contested ownership;
- append-only event records;
- deterministic IDs;
- state validation against schemas;
- safe recovery after partial process failure;
- one canonical per-type tree directory per schema name
  (``SCHEMA_TO_STATE_DIR`` in ``core/state_backend.py``): the state
  backend, the event log and the planning registries resolve the same
  directories (``goals/``, ``runs/``, ``events/`` ...), so a reader of
  Core state never misses records a registry wrote.

## 3. Avoid a monolithic state file

Canonical layout (per-type plural tree directories):

```text
goals/GOAL-....yaml
runs/RUN-....yaml
evidence/EVID-....yaml
decisions/DEC-....yaml
events/EVT-....json
locks/<object>.lock
```

``FilesystemStateBackend`` and ``ProjectEventLog`` persist one JSON
object per id under ``base_dir/<tree_dir>/<object_id>.json``, where
``<tree_dir>`` is the canonical tree directory for the schema name
(``core.state_backend.SCHEMA_TO_STATE_DIR``); the planning registries'
``*_STATE_DIR`` constants (``planning.plan.GOALS_STATE_DIR`` etc.) are
the same strings, and the tree template
(``templates/PROJECT-TREE.template.txt`` / ``planning.init.INIT_DIRECTORIES``)
holds the same directories. ``project`` is the exception: the single
canonical Project record is ``project.yaml`` at the workspace root.

This reduces write contention.

## 4. Lease model

When one worker owns an executable Goal/Run stage, acquire a lease using atomic create-if-not-exists.

Lease fields:

- object ID;
- owner session/worker ID;
- acquired timestamp;
- expiry timestamp;
- heartbeat/renewal policy.

Expired leases may be reclaimed only through a deterministic recovery rule.

## 5. Git audit policy

Git commits represent meaningful scientific governance milestones, not operational heartbeat noise.

Commit examples:

- project initialized;
- Inventory audit passed;
- Plan v1 frozen;
- Goal contract revision;
- Acceptance revision;
- Analysis Protocol revision;
- Recovery plan created;
- Requirement closed;
- project final outcome.

## 6. Artifact Store

Large artifacts may reside on project storage/NAS/HPC/object store.

The repository stores manifests with:

- artifact ID;
- URI/path;
- SHA256;
- byte size;
- MIME/data type;
- producer Run;
- acquisition/creation timestamp;
- metadata;
- lineage.

## 7. Report traceability

Every report claim deemed key/decision-relevant must be traceable to:

```text
Report claim
 -> Decision / Requirement outcome
 -> Analysis Result
 -> Run(s)
 -> Raw Artifact manifest(s)
 -> Source/Evidence as appropriate
```

## 8. Future backend interface

Implement `StateBackend` interface from v0.1 even though only FilesystemStateBackend exists. Future options may include SQLite/PostgreSQL without changing agent governance logic.
