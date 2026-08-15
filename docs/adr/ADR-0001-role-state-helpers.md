# ADR-0001 — Role-facing state authoring helpers

- **Status:** proposed <!-- proposed | accepted | superseded -->
- **Date:** 2026-08-15
- **Reviewer:** PR reviewer (assigned by the supervisor; authors never self-approve)
- **Related PR:** PR shipping branch `feat/role-state-helpers` (number assigned at opening)
- **Supersedes:** none
- **Superseded by:** none

## Context

*"Role agents must hand-roll state authoring (canonical JSON, event ids,
lifecycle plumbing) — no official helper APIs."* (issue #92.) In a real
v0.2 run, the operator built a ~200-line helper layer for request
lifecycle transitions, source/evidence writes, checklist-derived
reliability scoring, mirror dedupe, event appends and run transitions —
all of it plumbing that duplicates, and risks diverging from, the
deterministic primitives the runtime already ships (`core.state_backend`
atomic canonical-JSON writes, `core.events.ProjectEventLog` with
deterministic ids and idempotent re-append, the rule tables of
`research/requests.py` and `core.transitions`).

The role contracts (agent-contracts/) require the Research role to
build *the project evidence base using traceable sources and
claim-specific evidence assessments*, and the worker/monitor roles to
record and advance Run lifecycle state; the frozen specs are
implemented canonically by the runtime, but no official facade exposes
that to role agents. This ADR records the decision to ship such a
facade. It is an implementation-level choice under the locked
architecture (20-ARCHITECTURE-DECISIONS.md), not a deviation: the
helper modules are thin facades over the existing deterministic
primitives and weaken no existing guarantee.

## Decision

Ship two role-facing helper modules as the official state-authoring
APIs:

* `research/state_helpers.py` — Research role:
  `register_source` (with canonical mirror identity derived at
  authoring time and mirror-collision rejection per 06-EVIDENCE-SYSTEM.md
  §7), `register_evidence` (frozen evidence-shape validation via
  `validate_evidence_record`), `register_research_request`
  (issued-state only: `requested_by == "supervisor"`, `status == OPEN`;
  issuance itself stays the Supervisor-facing
  `research.requests.issue_research_request`), `advance_research_request`
  (through the R-REQ-S0/C1/E1/D1 rule table),
  `link_result_to_request` (through the R-LINK-S1/D1 rule table), and
  typed reads / sorted listings.
* `workers/run_helpers.py` — worker/monitor roles: `register_run`,
  `transition_run` (through the Run lifecycle rule table of
  `core.transitions` / `core.rules.lifecycle`, stamping the record's
  `updated_at` with the injected `at`), `read_run`, `list_runs`.

Every write is canonical-JSON persisted through
`FilesystemStateBackend` (atomic, schema-gated) and audited with exactly
one event under a deterministic id (`generate_id("event", type, ...)`)
and a namespaced idempotency key — the monitoring reconcile pattern,
generalized. Timestamps and actors are always injected (no wall clock),
so the deterministic path is preserved. Registration is exactly-once per
id: a re-run whose deterministic event is missing but whose record
exists converges by appending the missing event (`replayed=True`);
record and event both present is a true duplicate. Lifecycle moves
reject no-op transitions outright (they must never enter the audit
record) and converge only when the target state has a unique normative
predecessor — states with several legal predecessors (run `CANCELLED` /
`INVALIDATED`) have no reconstructible interrupted arc, and without an
event log convergence is impossible; the no-op guard wins there.

## Consequences

Positive:

* Role agents no longer hand-roll canonical JSON, event ids, or
  lifecycle plumbing: the observed ~200-line helper layer collapses to
  documented one-call APIs, and every operation keeps a complete,
  idempotent audit trail.
* The frozen guarantees are strengthened at the authoring boundary:
  mirror dedupe runs at source-write time (06-EVIDENCE-SYSTEM.md §7:
  mirrors are never independent evidence), evidence is shape-validated
  before it can reach the store, and research-request records can never
  bypass the lifecycle rule table into a later status.
* Crash-window convergence (the exact interruption the hand-rolled
  layer mis-handles) is first-class: the helpers self-heal the
  write-then-append window idempotently.

Negative / trade-offs (guarded in the implementation):

* Convergence only exists along unique normative arcs; interrupted
  moves into multi-predecessor states cannot be reconstructed and are
  rejected as no-ops. The predecessor tables
  (`REQUEST_PREDECESSOR_STATUS`, `RUN_PREDECESSOR_STATE`) are locked by
  tests against the rule tables, so a future rule change surfaces as a
  test failure, never a silent behavior drift.
* The helpers are facades, not new rules: they must never weaken the
  rule tables, and schema evolution still requires updating the helper
  coercion/validation layers (same as any consumer of the frozen
  models).

## Verification notes

* `pytest tests/research/test_state_helpers.py tests/workers/test_run_helpers.py -q`
  — the two new suites cover registration persistence (canonical JSON,
  exact-once, byte-untouched duplicates), event ids/idempotency and
  replay semantics, crash-window convergence, the full legal lifecycle
  chains, every illegal-pair rejection, mirror collisions, corrupt and
  missing state, and the predecessor-table locks.
* `ruff check .` and `mypy src/` must stay clean.
* No existing test is deleted or weakened; `tests/research` and
  `tests/workers` subsets plus `tests/core` / `tests/state` remain
  green (CI runs the full suite).
