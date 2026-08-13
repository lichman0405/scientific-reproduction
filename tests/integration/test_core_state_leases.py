"""Integration: leases gate concurrent object writes end-to-end
(DEV-M1-G06, acceptance AC-01).

This suite exercises the layers **together** -- ``LeaseStore``
(DEV-M1-G03), ``FilesystemStateBackend`` (DEV-M1-G02), and the frozen
models / schema gate (DEV-M1-G01) -- for the concurrent duplicate lease
scenario that the unit tests (tests/state/test_leases.py) cover per
layer in isolation:

* multiple concurrent claimants race for the **same** lease on the
  **same** base dir (real threads, two ``LeaseStore`` instances per
  round, simulating two processes); exactly one wins and every loser
  gets ``LeaseHeldError`` (AC-01);
* the winner's lease gates the object write: a claimant only persists
  while it holds the lease, so exactly one canonical, schema-valid
  object survives on disk and the losers' versions never land;
* an interleaved ``os.link`` barrier makes the create race real on any
  machine, so the single-winner property is decided by the atomic
  protocol, not by thread scheduling;
* after expiry (injected clock, no sleeps) a fresh claimant takes over
  deterministically and rewrites the object -- still exactly one
  canonical object on disk, schema-valid (AC-02 gate end-to-end);
* concurrent takeovers of the same expired lease also yield exactly one
  winner and exactly one object.

All timing is injected via the shared ``FakeClock``; there are no
sleeps and no wall-clock dependence.
"""

from __future__ import annotations

import copy
import os
import threading
from typing import Any, Callable

import pytest

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.leases import (
    Lease,
    LeaseError,
    LeaseHeldError,
    LeaseStore,
)
from scientific_reproduction.core.schema_validation import validate_object
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    StateBackend,
)
from tests.core.fixtures import VALID_DOCS


class FakeClock:
    """Injectable deterministic clock (epoch seconds)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _run_doc(
    object_id: str, owner: str, lifecycle_state: str = "CREATED"
) -> dict[str, Any]:
    """A schema-valid run document identifiable by its ``owner`` marker.

    The extra ``claimant`` field is permitted by the run schema
    (``additionalProperties: true``) and makes the writer of each
    candidate version unambiguous on disk.
    """
    doc = copy.deepcopy(VALID_DOCS["run"])
    doc["run_id"] = object_id
    doc["lifecycle_state"] = lifecycle_state
    doc["claimant"] = owner
    return doc


def _claim_and_write(
    store: LeaseStore,
    backend: StateBackend,
    object_id: str,
    owner: str,
    ttl: float,
    *,
    lifecycle_state: str = "CREATED",
) -> tuple[str, object]:
    """Acquire the lease for ``object_id`` and, if acquired, persist the object.

    The lease is deliberately **not** released here: the race harness
    holds the winner's lease until every claimant has attempted
    acquisition, so a loser can never observe the lease freed and sneak
    a write in -- this is the lease-gated write pattern the workflow
    layer is expected to use.

    Returns ``("ok", lease)`` when this claimant held the lease for its
    write, ``("error", exc)`` when acquisition failed (nothing written).
    """
    try:
        lease = store.acquire("run", object_id, owner, ttl)
    except LeaseError as exc:
        return ("error", exc)
    backend.write("run", object_id, _run_doc(object_id, owner, lifecycle_state))
    return ("ok", lease)


def _race_claim_and_write(
    targets: list[Callable[[], tuple[str, object]]],
) -> list[tuple[str, object]]:
    """Run lease-gated write attempts in threads past a barrier (no sleeps).

    The barrier guarantees every claimant passes the pre-create checks
    before any of them reaches the atomic create, so the race is real;
    the outcome is deterministic because the atomic protocol decides it.
    Unexpected exceptions inside a thread are captured into the results
    (``("thread-error", exc)``) instead of crashing the thread silently,
    so any protocol regression surfaces as a clear assertion failure.
    """
    barrier = threading.Barrier(len(targets))
    results: list[tuple[str, object]] = []

    def runner(target: Callable[[], tuple[str, object]]) -> None:
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            pass  # a sibling thread died; proceed so its error surfaces
        try:
            results.append(target())
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assert
            results.append(("thread-error", exc))

    threads = [
        threading.Thread(target=runner, args=(target,), daemon=True)
        for target in targets
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    return results


def _assert_single_winner(results: list[tuple[str, object]]) -> Lease:
    """Assert exactly one claimer held the lease; the rest were rejected."""
    ok = [r for r in results if r[0] == "ok"]
    err = [r for r in results if r[0] == "error"]
    assert len(ok) == 1, f"expected exactly one winner, got {results}"
    assert len(err) == len(results) - 1
    assert all(isinstance(r[1], LeaseHeldError) for r in err)
    winner = ok[0][1]
    assert isinstance(winner, Lease)
    return winner


def _assert_single_canonical_object(
    backend: StateBackend, object_id: str, winner: Lease
) -> dict[str, Any]:
    """Assert exactly one object on disk: the winner's, schema-valid."""
    assert backend.list_ids("run") == [object_id]
    stored = backend.read("run", object_id)
    assert validate_object("run", stored) == []
    assert stored["run_id"] == object_id
    assert stored["claimant"] == winner.owner
    return stored


# ---------------------------------------------------------------------------
# AC-01: concurrent duplicate lease claim, write gated by the lease
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_lease_claim_single_winner_single_object(
    tmp_path,
) -> None:
    """AC-01: real concurrent claim -- one winner, one canonical object.

    Eight claimants across two ``LeaseStore`` instances (simulating two
    processes) share one base dir and one injected clock.  Each claimant
    writes only while holding the lease; the losers' versions must never
    reach disk, and the surviving object must be schema-valid.
    """
    for round_no in range(5):
        base = tmp_path / f"round-{round_no}"
        clock = FakeClock(1000.0)
        backend = FilesystemStateBackend(base)
        store_a = LeaseStore(base, now=clock)
        store_b = LeaseStore(base, now=clock)
        object_id = generate_id("run", f"contest-{round_no}")

        owners = [f"worker-a{i}" for i in range(4)] + [
            f"worker-b{i}" for i in range(4)
        ]
        stores = [store_a] * 4 + [store_b] * 4
        targets = [
            lambda store=store, owner=owner: _claim_and_write(
                store, backend, object_id, owner, 30
            )
            for store, owner in zip(stores, owners)
        ]
        results = _race_claim_and_write(targets)

        winner = _assert_single_winner(results)
        stored = _assert_single_canonical_object(backend, object_id, winner)
        assert stored["lifecycle_state"] == "CREATED"

        # The winner's lease is exactly the live on-disk record while the
        # object is being written; releasing it removes only the record,
        # never the object.
        assert store_a.get("run", object_id) == winner
        assert winner.expires_at == 1030.0
        store_a.release(winner)
        assert store_a.get("run", object_id) is None
        assert backend.list_ids("run") == [object_id]


def test_interleaved_os_link_race_single_winner_single_object(
    tmp_path, monkeypatch
) -> None:
    """AC-01: the create-if-not-exists step decides the winner.

    Both claimers are forced to arrive at ``os.link`` together, so the
    single-winner property cannot come from thread scheduling; the
    winner's write is the only object on disk.
    """
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store_a = LeaseStore(base, now=clock)
    store_b = LeaseStore(base, now=clock)
    object_id = generate_id("run", "interleaved-1")

    barrier = threading.Barrier(2)
    real_link = os.link

    def interleaved_link(src, dst, *args, **kwargs):
        # Best-effort rendezvous: when both claimers reach the atomic
        # create together the race is real; a claimer whose rival lost
        # before reaching os.link must not strand in the barrier.
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", interleaved_link)

    results = _race_claim_and_write(
        [
            lambda: _claim_and_write(store_a, backend, object_id, "worker-a", 30),
            lambda: _claim_and_write(store_b, backend, object_id, "worker-b", 30),
        ]
    )
    monkeypatch.undo()

    winner = _assert_single_winner(results)
    _assert_single_canonical_object(backend, object_id, winner)
    store_b.release(winner)
    assert store_a.get("run", object_id) is None


# ---------------------------------------------------------------------------
# AC-01 + AC-02: expiry recovery, deterministic takeover, one canonical object
# ---------------------------------------------------------------------------


def test_expired_lease_taken_over_deterministically_rewrites_object(
    tmp_path,
) -> None:
    """AC-01/AC-02: expiry (injected clock) frees the object for a fresh
    claimant, who rewrites it -- exactly one canonical object on disk."""
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store = LeaseStore(base, now=clock)
    object_id = generate_id("run", "takeover-1")

    first = _claim_and_write(store, backend, object_id, "worker-a", 30)
    assert first[0] == "ok"
    first_lease = first[1]
    assert isinstance(first_lease, Lease)
    stored_v1 = backend.read("run", object_id)
    assert stored_v1["claimant"] == "worker-a"

    # The holder never releases; time passes beyond the lease expiry.
    clock.advance(31)

    # A fresh claimant takes over deterministically...
    second = _claim_and_write(
        store,
        backend,
        object_id,
        "worker-b",
        10,
        lifecycle_state="RESULT_AVAILABLE",
    )
    assert second[0] == "ok"
    taken = second[1]
    assert isinstance(taken, Lease)
    assert taken.owner == "worker-b"
    assert taken.acquired_at == 1031.0
    assert taken.expires_at == 1041.0
    assert taken.nonce != first_lease.nonce

    # ...and its write is the only canonical object on disk, schema-valid.
    stored_v2 = _assert_single_canonical_object(backend, object_id, taken)
    assert stored_v2["lifecycle_state"] == "RESULT_AVAILABLE"
    assert stored_v2["claimant"] == "worker-b"

    # The old grant is dead: it cannot renew the record (now another
    # grant) and it cannot delete the new grant via release.
    with pytest.raises(LeaseHeldError, match="worker-b"):
        store.renew(first_lease)
    with pytest.raises(LeaseHeldError, match="worker-b"):
        store.release(first_lease)
    assert store.get("run", object_id) == taken

    # The new owner releases cleanly; the object survives the release.
    store.release(taken)
    assert store.get("run", object_id) is None
    assert backend.list_ids("run") == [object_id]


def test_concurrent_takeover_of_expired_lease_single_winner_single_object(
    tmp_path, monkeypatch
) -> None:
    """AC-02: two claimers race to recover the same expired lease.

    Exactly one takeover wins (interleaved ``os.link`` makes the race
    real) and exactly one claimant writes; the surviving object is the
    winner's and schema-valid.
    """
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store = LeaseStore(base, now=clock)
    object_id = generate_id("run", "recovery-1")

    assert _claim_and_write(store, backend, object_id, "worker-a", 30)[0] == "ok"
    clock.advance(31)  # expired

    barrier = threading.Barrier(2)
    real_link = os.link

    def interleaved_link(src, dst, *args, **kwargs):
        # Best-effort rendezvous (see the fresh-claim variant): a
        # claimer whose rival already won must not strand in the barrier.
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", interleaved_link)

    results = _race_claim_and_write(
        [
            lambda: _claim_and_write(store, backend, object_id, "worker-b", 30),
            lambda: _claim_and_write(store, backend, object_id, "worker-c", 30),
        ]
    )
    monkeypatch.undo()

    winner = _assert_single_winner(results)
    stored = _assert_single_canonical_object(backend, object_id, winner)
    assert stored["claimant"] in ("worker-b", "worker-c")
    assert winner.acquired_at == 1031.0
    assert store.get("run", object_id) == winner
    store.release(winner)
