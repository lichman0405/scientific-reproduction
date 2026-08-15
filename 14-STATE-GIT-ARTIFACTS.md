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
- safe recovery after partial process failure.

## 3. Avoid a monolithic state file

Preferred:

```text
goals/GOAL-....yaml
runs/RUN-....yaml
evidence/EVID-....yaml
decisions/DEC-....yaml
events/EVT-....json
locks/<object>.lock
```

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

Every workspace is initialized with a starter `.gitignore` (nested
repositories, large raw artifacts per ADR 38, OS/editor noise) and
`.gitattributes` (LF normalization), recorded in the "project
initialized" commit, so the audit history never drags in unrelated or
raw content and commits are identical on any platform.

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
