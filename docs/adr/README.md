# Architecture Decision Records (ADR) — `docs/adr/`

## What an ADR is here

An Architecture Decision Record (ADR) is a numbered, dated, statused note that
records **one implementation decision** affecting the architecture of
Scientific Reproduction Skill, together with its context, the chosen decision,
and its consequences. ADRs make the *reason* for a decision auditable without
touching the frozen normative specification documents.

The locked decisions themselves are not re-derived from ADRs. The canonical
locked decisions live in `20-ARCHITECTURE-DECISIONS.md` (50 product decisions
and the development-orchestration decision history). ADRs recorded under this
directory are *implementation-level* records: they document deviations from,
or subordinate choices under, the locked architecture — they never silently
replace it.

## When an ADR is required

Record an ADR when **any** of the following applies:

1. **Implementation deviation from a locked architecture decision.** Any
   change that would alter the semantics of a decision in
   `20-ARCHITECTURE-DECISIONS.md` (or any other normative requirement in the
   root specification documents) requires an ADR before the change merges.
2. **Implementation choices that later contributors must resolve locally.**
   Record ADRs for implementation choices that do not alter locked product
   semantics but would otherwise be undiscoverable from the code alone.
3. **Scope or process deviations that must be auditable.**

An ADR is **not** required for ordinary implementation work inside an accepted
contract: uncontroversial, within-scope choices that follow the locked
specification. When in doubt, record the ADR — a short extra record is cheaper
than an undiscoverable decision.

## Lifecycle

```text
proposed  ->  accepted  ->  superseded
  |            (or rejected, no merge into accepted set)
  +-> superseded (rejected proposals are archived or deleted, never "accepted")
```

- **proposed** — a contributor believes a decision is needed and drafts the
  ADR. A proposed ADR is not normative.
- **accepted** — an independent PR reviewer approves the ADR as part of the
  pull request that introduces the change. Contributors may then rely on it.
- **superseded** — a later accepted ADR replaces this one; the superseded ADR
  must point to its replacement, and the replacement must point back.

ADR acceptance goes through the same PR review as code: the ADR ships in the
pull request that implements the decision it records. Contributors never
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
