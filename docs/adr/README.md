# Architecture Decision Records (ADR) — `docs/adr/`

## What an ADR is here

An Architecture Decision Record (ADR) is a numbered, dated, statused note that
records **one implementation decision** affecting the architecture of
Scientific Reproduction Skill v0.1, together with its context, the chosen
decision, and its consequences. ADRs make the *reason* for a decision auditable
without touching the frozen normative specification documents.

The locked decisions themselves are not re-derived from ADRs. The canonical
locked decisions live in `20-ARCHITECTURE-DECISIONS.md` (50 product decisions,
`ADR-D01`–`ADR-D05` development-orchestration decisions, and
`ADR-DEV-008`–`ADR-DEV-011` Git-governance decisions). ADRs recorded under this
directory are *implementation-level* records: they document deviations from,
or subordinate choices under, the locked architecture — they never silently
replace it.

## When an ADR is required

Record an ADR when **any** of the following applies:

1. **Implementation deviation from a locked architecture decision.** Any
   change that would alter the semantics of a decision in
   `20-ARCHITECTURE-DECISIONS.md` (or any other normative requirement in the
   root specification documents) requires a formal change request followed by a
   Supervisor decision; the outcome is recorded as an ADR. Per
   `ADR-DEV-011` in `20-ARCHITECTURE-DECISIONS.md`, a worker discovering
   functionality outside its frozen contract must submit a
   `DEVELOPMENT_CHANGE_REQUEST` (see
   `development/templates/DEVELOPMENT-CHANGE-REQUEST.template.yaml`) and must
   not implement anything before the Development Supervisor decides.
2. **Supervisor-recorded implementation choices.** Per
   `21-DEVELOPMENT-SUPERVISOR-SPEC.md` section 4, the Development Supervisor
   records ADRs for implementation choices that do not alter locked product
   semantics, so that later workers can resolve them locally without asking.
3. **Scope or process deviations that must be auditable.** For example,
   exception records for the supervisor implementation boundary
   (`21-DEVELOPMENT-SUPERVISOR-SPEC.md` section 5) and alternative merge
   methods per `ADR-DEV-010`.

An ADR is **not** required for ordinary implementation work inside an accepted
DEV-GOAL contract: uncontroversial, within-scope choices that follow the locked
specification. When in doubt, record the ADR — a short extra record is cheaper
than an undiscoverable decision.

## Relationship to change requests

A change request (DCR, per `26-DEVELOPMENT-CHANGE-CI-RELEASE.md` section 3 and
`development/templates/DEVELOPMENT-CHANGE-REQUEST.template.yaml`) is the
**trigger**; the ADR is the **record of the resulting decision**. A new
DEV-GOAL or DEV-PLAN version may be the outcome instead of (or in addition to)
an ADR. Every ADR should link to the change request and/or DEV-GOAL that
triggered it.

## Lifecycle

```text
proposed  ->  accepted  ->  superseded
  |            (or rejected, no merge into accepted set)
  +-> superseded (rejected proposals are archived or deleted, never "accepted")
```

- **proposed** — a worker or the Supervisor believes a decision is needed and
  drafts the ADR. A proposed ADR is not normative.
- **accepted** — the Development Supervisor (the sole governance authority for
  development decisions; see `21-DEVELOPMENT-SUPERVISOR-SPEC.md` and
  `25-DEVELOPMENT-GIT-GOVERNANCE.md`) reviews and accepts the ADR. Workers may
  then rely on it.
- **superseded** — a later accepted ADR replaces this one; the superseded ADR
  must point to its replacement, and the replacement must point back.

The Development Supervisor is the **reviewer** for all ADRs. Workers never
self-approve their own ADRs.

## Numbering

- New ADRs use sequential numbers: `ADR-0001`, `ADR-0002`, … (see
  `0000-adr-template.md`).
- `0000-adr-template.md` is the template; it is not a decision and must never be
  renumbered or assigned a status.
- The existing `ADR-D01`–`ADR-D05` and `ADR-DEV-008`–`ADR-DEV-011` identifiers
  in `20-ARCHITECTURE-DECISIONS.md` belong to the frozen specification package
  and are not part of this sequence.

## File format

Each ADR is one Markdown file named `<NNNN>-<short-slug>.md`, e.g.
`0001-lab-adapter-filesystem-handoff.md`. Copy `0000-adr-template.md` and fill
in the fields. Status changes and superseded links are edits to the same file
(history stays in Git).
