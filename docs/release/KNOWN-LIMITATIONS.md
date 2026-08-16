# Known Limitations — Scientific Reproduction Skill v0.2.2 (runtime v0.1)

| Field | Value |
|---|---|
| Status | **Non-blocking for release.** Every item below is documented and dispositioned. |
| Evidence basis | module docstrings, `docs/operations/adapters-slurm.md`, the v0.1.0 release audit (2026-08-14) |

---

## 1. Accepted engineering risks

### 1.1 DEV-DEFECT-001 — lease/lock TOCTOU at the expiry boundary (ACCEPTED-RISK FOR v0.1)
- **Where:** `src/scientific_reproduction/core/leases.py` (`_compare_and_unlink`), `src/scientific_reproduction/core/locks.py` (`FileLock.release`, `_break_stale`).
- **What:** the read-verify-then-unlink sequences are not atomic; a holder renewing/releasing at the exact expiry instant can have its fresh record removed by a claimer's takeover unlink (and vice versa). Worst case: transient dual ownership where both principals briefly believe they hold the lease/lock.
- **Why not blocking:** never corrupts persisted state (the create step is always atomic create-if-absent, so at most one record exists on disk at any instant); the loser detects the loss on its very next lease/lock operation (record != its grant -> `LeaseHeldError`); requires a nanosecond interleaving between concurrent processes at the exact boundary; the deterministic tests (injected clock) cannot exercise the true wall-clock race.
- **Disposition:** ACCEPTED-RISK, documented (hazard window, invariant and detection semantics in module docstrings). Backlog candidate for v0.1.1 (advisory locks / inode-bound removal).

### 1.2 DEV-DEFECT-002 — timing-flaky lease-takeover test (ACCEPTED)
- **Where:** `tests/state/test_leases.py::test_concurrent_takeover_of_expired_lease_single_winner`.
- **What:** observed ~1/10 runs Windows-local; CI green on ubuntu; full-suite rerun clean. Under adversarial interleaving both claimants may receive the documented transient `LeaseHeldError` and neither writes — zero winners, nothing persisted, no corruption (the safe direction; the product contract guarantees at most one winner, never that the loop resolves within 3 passes).
- **Disposition:** ACCEPTED, documented. Isolated rerun that passes is not a regression. Remediation bundled with DEV-DEFECT-001 or a dedicated flake-hardening follow-up (v0.1.1 backlog).

## 2. Adapter / platform limitations (v0.1 surface)

Verified against `docs/operations/adapters-slurm.md`; each traces to real code:

- **Constructor-bound configuration** — the Slurm-over-SSH adapter takes every knob as a constructor argument; there is no config-file loader and no env-var reads.
- **No shipped SSH transport** — `SSHTransport` is a pure ABC; no production transport ships in v0.1.
- **Queue-blind probing** — status derives its `--jobs` argument from the recorded external id only (squeue/sacct/scancel); no partition/account/GPU/reservation flags on `sbatch` (`--chdir`/`--output` only).
- **No exactly-once launch across a mid-operation drop** — a reconnect re-runs the pending step.
- **Credentials in memory only** — no credential fields in durable records; never persisted.
- **Platform adapters** — Claude Code spawn/resume is supported with a documented subagent/process fallback; Codex adapter passes the shared mock/contract battery; real-platform verification is attempted where available and otherwise skipped by design (no fake real-platform claims).

## 3. Reporting / state-model limits

- **Evidence registry is in-memory** — `research.evidence.EvidenceRegistry` has no v0.1 durable store; audit/traceability APIs take the registry as an explicit input.
- **No decision registry** — Supervisor decisions are recorded only as event payloads in v0.1; the report-traceability chain represents that hop via the requirement records the claim's evidence is used by.
- **No `register_assumption` / `register_human_gate` convenience helpers** — assumption and human-gate registration flows through the existing registries without dedicated helpers.
- **Inventory-registry repair semantics are immutable-functional** — audits and repairs are applied functionally.
- **Failed-run visibility** — the frozen Run vocabulary has no "failed" state; scientific PASS/FAIL is a review decision stored separately, and abandoned/invalidated runs are recorded by the terminal states `CANCELLED` / `INVALIDATED`, which the audit package maps (plus `ScientificReview.FAIL`) to `FAILED` so failed runs stay visible in the machine-auditable package.

## 4. Domain-pack limitations

- **TGA rule R-CHA-A6 consumes two facts under a one-measurement rule shape** — a missing `reference_mass_loss_pct` raises `KeyError` (loud failure, never a silent skip); a multi-measurement rule shape is out of v0.1 scope.

## 5. Benchmark scope

- The v0.1 planning benchmark is a **single reference paper** (FDM-201, DOI 10.1039/D5TA00771B) — real source acquisition and planning against one paper, not a multi-paper corpus.
- The frozen FDM-201 register is a **plan-state** (Plan v1 frozen, no executed runs/artifacts on disk). Release-gate evidence therefore comes from the audit/traceability battery over real registries.

---

All items above are judged **non-blocking** for this release. No frozen acceptance criterion is modified by any disposition in this document.
