# ADR-0001 — Primary-target metadata registration in the research bootstrap

- **Status:** proposed
- **Date:** 2026-08-15
- **Reviewer:** PR reviewer (assigned at review)
- **Related PR:** none (branch `fix/pdf-metadata-registration`)
- **Supersedes:** none
- **Superseded by:** none

## Context

For a local PDF target, `planning.init` registers only the file path on the
primary target record: `parse_target_form` has no channel for the paper DOI,
so the optional `doi`/`title` fields of the frozen `PrimaryTarget` model
(`schemas/project.schema.yaml`) stay empty. Target identity is therefore not
machine-usable for PDF targets — mirror collapse (`06-EVIDENCE-SYSTEM.md`
section 7), evidence linking and the research bootstrap cannot key on the
paper DOI — until research runs; in a real run the operator had to extract
the DOI manually with pypdf.

Two candidate fixes were considered:

- **(a) extract title/DOI from the PDF at init (opt-in).** Rejected:
  initialization is documented as deterministic from its inputs alone — it
  never reads the workspace, never touches the network and never discovers
  inventory (`planning/init.py` module docstring, AC-02 of DEV-M4-G01) — and
  the runtime is stdlib-only, so PDF extraction would need a non-stdlib
  dependency or an external tool.
- **(b) a first-class "metadata registration" step in the research bootstrap
  that updates the primary target record.** Chosen: bootstrap research is
  where the paper identity is established (before Plan v1), the frozen model
  already carries `doi`/`title`, and the registration is a plain
  deterministic, schema-validated state update.

## Decision

Add `planning.init.register_target_metadata(root, *, doi=None, title=None,
timestamp=None)`: a first-class API that registers DOI/title metadata on the
existing primary target record of an initialized project. The registration:

- preserves the one-primary invariant (decision 3 of
  `20-ARCHITECTURE-DECISIONS.md`): the target's `source_type` and
  `identifier` are never replaced, and a DOI that contradicts a DOI-form
  identifier is rejected;
- validates the DOI against the frozen DOI syntax
  (`planning.init.DOI_PATTERN`, stable `TargetValidationError` messages);
- requires at least one of `doi`/`title`, rejects uninitialized roots,
  persists the schema-validated record atomically and returns the updated
  `Project`; re-registering identical metadata is a deterministic no-op;
- is state-only, following the `register_primary_target` convention: no new
  event type and no new git checkpoint, so the audit mapping of
  `audit/git.py` stays untouched.

The research bootstrap contract (`research/workflows.py`) declares the
obligation as `TARGET_METADATA_REGISTRATION`, bound to the primary paper
step W-BOOT-1. It is not a new acquisition category: the six-category table
(`BOOTSTRAP_WORKFLOW`) and its version are unchanged, and the frozen
`SourceType` mapping stays total and disjoint.

Governance: Research reports the extracted identity facts during W-BOOT-1;
the project-record write follows the Supervisor governance path of
`03-ROLE-AND-PERMISSION-SPEC.md` section 1 ("Workers report facts. Supervisor
makes scientific decisions."). The runtime API itself is role-neutral, like
`register_primary_target`.

## Consequences

Positive: PDF target identity becomes machine-usable before Plan v1;
operators have a documented channel to register a manually extracted DOI;
no schema change; no new dependency; init stays deterministic and offline.

Negative: the project record can now be updated after init (a documented
correction path). The update is schema-validated and atomic, consistent with
every other project-record write, and identical re-registration is a no-op.

## Verification notes

- `tests/planning/test_init.py` — `test_init_*metadata_registration*`:
  PDF-target registration (the issue scenario), AC-01 preservation
  (form/identifier unchanged), malformed/contradictory DOI rejection,
  correction path, no-op idempotency, uninitialized root, type errors,
  schema validity and persistence round-trip.
- `tests/research/test_requests_workflows.py` —
  `test_target_metadata_registration_*`: the obligation binds to W-BOOT-1
  (primary paper step), its API resolves in `planning.init`, and the entry
  is frozen.
