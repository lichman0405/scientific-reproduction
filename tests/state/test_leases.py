"""Tests for bounded per-object leases (DEV-M1-G03).

Acceptance coverage:
  * AC-01 -- two concurrent claimers cannot both acquire the same valid
    lease: proven three ways -- (a) an interleaved simulation of the
    atomic file operations (barrier inside ``os.replace`` so both
    claimers complete their staging writes "concurrently"), (b) an
    interleaved simulation of the create-if-not-exists step itself
    (barrier inside ``os.link``), and (c) a real two-backend/two-thread
    claim against the same base directory.
  * AC-02 -- expired leases are recovered deterministically with an
    injected clock: stale-lease takeover records the new owner; expiry
    at the exact boundary is recoverable; concurrent takeovers yield
    exactly one winner; corrupt records are stale (claimable).
  * AC-03 -- lease metadata identifies owner, expiry, and object
    reference; the on-disk record round-trips exactly.
  * plus: acquire/renew/release lifecycle, renew on an expired lease
    fails, release frees for re-acquire, unknown object type errors,
    invalid TTL/owner rejection, idempotent release, crash safety.

All timing is injected (no sleeps, no wall-clock dependence).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Callable

import pytest

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.leases import (
    Lease,
    LeaseCorruptError,
    LeaseError,
    LeaseExpiredError,
    LeaseHeldError,
    LeaseStore,
)
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    UnknownObjectTypeError,
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


def make_store(tmp_path, clock: FakeClock) -> LeaseStore:
    return LeaseStore(tmp_path / "state", now=clock)


def _run_concurrently(*targets: Callable[[], Lease]) -> list[tuple[str, object]]:
    """Run ``targets`` in threads racing past a barrier (no sleeps).

    Returns ``("ok", lease)`` / ``("error", exc)`` per target.  The
    barrier guarantees every claimer passes the pre-create checks before
    any of them reaches the atomic create, so the race is real; the
    outcome is deterministic because the atomic protocol decides it.
    """
    barrier = threading.Barrier(len(targets))
    results: list[tuple[str, object]] = []

    def runner(target: Callable[[], Lease]) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(("ok", target()))
        except LeaseError as exc:
            results.append(("error", exc))

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
    """Assert exactly one claimer succeeded and every loser was rejected."""
    ok = [r for r in results if r[0] == "ok"]
    err = [r for r in results if r[0] == "error"]
    assert len(ok) == 1, f"expected exactly one winner, got {results}"
    assert len(err) == len(results) - 1
    assert all(isinstance(r[1], LeaseHeldError) for r in err)
    winner = ok[0][1]
    assert isinstance(winner, Lease)
    return winner


# ---------------------------------------------------------------------------
# AC-03: metadata -- owner, expiry, object reference
# ---------------------------------------------------------------------------


def test_lease_record_identifies_owner_and_expiry(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)

    lease = store.acquire("run", "RUN-1", "worker-a", 30)

    # AC-03: the grant carries owner, expiry, and the object reference.
    assert lease.object_type == "run"
    assert lease.object_id == "RUN-1"
    assert lease.owner == "worker-a"
    assert lease.acquired_at == 1000.0
    assert lease.expires_at == 1030.0
    assert lease.ttl == 30.0
    assert not lease.is_expired(1029.999)
    assert lease.is_expired(1030.0)
    assert lease.remaining(1000.0) == 30.0

    # Metadata survives persistence: read back through get() and as the
    # raw on-disk record.
    assert store.get("run", "RUN-1") == lease
    raw_path = tmp_path / "state" / "leases" / "run" / "RUN-1.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["owner"] == "worker-a"
    assert raw["expires_at"] == 1030.0
    assert raw["object_type"] == "run"
    assert raw["object_id"] == "RUN-1"
    assert raw["nonce"] == lease.nonce
    assert raw["acquired_at"] == 1000.0
    assert raw["ttl"] == 30.0

    # Canonical deterministic serialization (sorted keys, two-space indent).
    assert raw_path.read_text(encoding="utf-8") == json.dumps(
        lease.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
    )


def test_get_returns_none_when_unleased(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    assert store.get("run", "RUN-1") is None


def test_generate_id_object_ids_work(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    object_id = generate_id("run", "RUN-1")
    lease = store.acquire("run", object_id, "worker-a", 30)
    assert store.get("run", object_id) == lease


# ---------------------------------------------------------------------------
# AC-01: two concurrent claimers cannot both acquire the same valid lease
# ---------------------------------------------------------------------------


def test_second_claimer_rejected_while_lease_valid(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    first = store.acquire("run", "RUN-1", "worker-a", 30)

    with pytest.raises(LeaseHeldError) as exc_info:
        store.acquire("run", "RUN-1", "worker-b", 30)
    assert "worker-a" in str(exc_info.value)

    # The held lease is untouched.
    assert store.get("run", "RUN-1") == first


def test_same_owner_cannot_acquire_twice(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    store.acquire("run", "RUN-1", "worker-a", 30)
    with pytest.raises(LeaseHeldError, match="worker-a"):
        store.acquire("run", "RUN-1", "worker-a", 30)


def test_interleaved_os_replace_race_has_single_winner(
    tmp_path, monkeypatch
) -> None:
    """AC-01: interleaved ``os.replace`` simulation.

    Force both claimers to complete their staging writes (the
    ``os.replace`` step of ``atomic_write``) concurrently, then race the
    atomic create-if-not-exists.  At most one claimer may acquire.
    """
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    staged = threading.Barrier(2)
    proceed = threading.Barrier(2)
    real_replace = os.replace

    def interleaved_replace(src, dst, *args, **kwargs):
        staged.wait(timeout=10)
        result = real_replace(src, dst, *args, **kwargs)
        proceed.wait(timeout=10)
        return result

    monkeypatch.setattr(os, "replace", interleaved_replace)

    results = _run_concurrently(
        lambda: store.acquire("run", "RUN-1", "worker-a", 30),
        lambda: store.acquire("run", "RUN-1", "worker-b", 30),
    )
    monkeypatch.undo()

    winner = _assert_single_winner(results)
    assert store.get("run", "RUN-1") == winner
    raw = json.loads(
        (tmp_path / "state" / "leases" / "run" / "RUN-1.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["owner"] == winner.owner
    assert raw["expires_at"] == 1030.0


def test_interleaved_create_race_has_single_winner(tmp_path, monkeypatch) -> None:
    """AC-01: interleave the atomic create-if-not-exists step itself.

    Both claimers pass the "no lease" check and arrive at ``os.link``
    together; the OS-level create-if-not-exists decides the winner.
    """
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    barrier = threading.Barrier(2)
    real_link = os.link

    def interleaved_link(src, dst, *args, **kwargs):
        barrier.wait(timeout=10)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", interleaved_link)

    results = _run_concurrently(
        lambda: store.acquire("run", "RUN-1", "worker-a", 30),
        lambda: store.acquire("run", "RUN-1", "worker-b", 30),
    )
    monkeypatch.undo()

    winner = _assert_single_winner(results)
    assert store.get("run", "RUN-1") == winner


def test_acquire_race_two_backends_two_threads(tmp_path) -> None:
    """AC-01: real concurrent claim across two backend instances.

    Two separate ``LeaseStore`` instances on the same base directory
    (simulating two processes) race the full acquire protocol; the
    create-if-not-exists step must yield exactly one winner every time.
    """
    for i in range(10):
        base = tmp_path / f"race-{i}"
        clock = FakeClock(1000.0)
        store_a = LeaseStore(base, now=clock)
        store_b = LeaseStore(base, now=clock)

        results = _run_concurrently(
            lambda: store_a.acquire("run", "RUN-1", "worker-a", 30),
            lambda: store_b.acquire("run", "RUN-1", "worker-b", 30),
        )
        winner = _assert_single_winner(results)
        # The surviving record is exactly the winner's, complete on disk.
        assert store_a.get("run", "RUN-1") == winner
        raw = json.loads(
            (base / "leases" / "run" / "RUN-1.json").read_text(encoding="utf-8")
        )
        assert raw["owner"] == winner.owner
        assert raw["expires_at"] == 1030.0


# ---------------------------------------------------------------------------
# AC-02: expired leases are recovered deterministically
# ---------------------------------------------------------------------------


def test_expired_lease_is_recovered_by_new_owner(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    old = store.acquire("run", "RUN-1", "worker-a", 30)
    assert old.expires_at == 1030.0

    # The previous owner never releases; time passes beyond the expiry.
    clock.advance(31)

    taken = store.acquire("run", "RUN-1", "worker-b", 10)
    # AC-02 + AC-03: the recovery records the new owner and new expiry.
    assert taken.owner == "worker-b"
    assert taken.acquired_at == 1031.0
    assert taken.expires_at == 1041.0
    assert taken.nonce != old.nonce
    assert store.get("run", "RUN-1") == taken


def test_valid_lease_blocks_other_claimers_even_after_ttl_passed_once(
    tmp_path,
) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    store.acquire("run", "RUN-1", "worker-a", 30)

    clock.advance(29)  # still valid (expires at 1030.0)
    with pytest.raises(LeaseHeldError, match="worker-a"):
        store.acquire("run", "RUN-1", "worker-b", 30)


def test_lease_expired_at_exact_boundary_is_recoverable(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    store.acquire("run", "RUN-1", "worker-a", 30)

    clock.t = 1030.0  # exactly at the expiry instant: expired
    taken = store.acquire("run", "RUN-1", "worker-b", 10)
    assert taken.owner == "worker-b"
    assert taken.acquired_at == 1030.0
    assert taken.expires_at == 1040.0


def test_concurrent_takeover_of_expired_lease_single_winner(
    tmp_path, monkeypatch
) -> None:
    """AC-02: two claimers race to recover the same expired lease."""
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    store.acquire("run", "RUN-1", "worker-a", 30)
    clock.advance(31)  # expired

    barrier = threading.Barrier(2)
    real_link = os.link

    def interleaved_link(src, dst, *args, **kwargs):
        barrier.wait(timeout=10)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", interleaved_link)

    results = _run_concurrently(
        lambda: store.acquire("run", "RUN-1", "worker-b", 30),
        lambda: store.acquire("run", "RUN-1", "worker-c", 30),
    )
    monkeypatch.undo()

    winner = _assert_single_winner(results)
    current = store.get("run", "RUN-1")
    assert current == winner
    assert current.owner in ("worker-b", "worker-c")
    assert current.acquired_at == 1031.0


def test_corrupt_lease_record_is_recoverable(tmp_path) -> None:
    """A corrupt record is not a valid lease: it can be claimed (AC-02)."""
    store = make_store(tmp_path, FakeClock(1000.0))
    raw_path = tmp_path / "state" / "leases" / "run" / "RUN-1.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text('{"object_type": "run", "truncat', encoding="utf-8")

    lease = store.acquire("run", "RUN-1", "worker-a", 30)
    assert lease.owner == "worker-a"
    assert store.get("run", "RUN-1") == lease


def test_get_raises_on_corrupt_record(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    raw_path = tmp_path / "state" / "leases" / "run" / "RUN-1.json"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("not json at all", encoding="utf-8")

    with pytest.raises(LeaseCorruptError, match="lease record"):
        store.get("run", "RUN-1")


def test_renew_on_expired_lease_fails(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    lease = store.acquire("run", "RUN-1", "worker-a", 30)

    clock.advance(31)  # expired
    with pytest.raises(LeaseExpiredError, match="expired"):
        store.renew(lease)

    # The stale record stays on disk (recoverable, not auto-deleted).
    assert store.get("run", "RUN-1") == lease


def test_renew_after_release_fails(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    lease = store.acquire("run", "RUN-1", "worker-a", 30)
    store.release(lease)

    with pytest.raises(LeaseExpiredError, match="no longer held"):
        store.renew(lease)


# ---------------------------------------------------------------------------
# Acquire / renew / release lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_acquire_renew_release(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)

    lease = store.acquire("run", "RUN-1", "worker-a", 30)
    clock.advance(10)

    # Renew keeps grant identity and extends the expiry by the TTL.
    renewed = store.renew(lease)
    assert renewed.owner == lease.owner
    assert renewed.nonce == lease.nonce
    assert renewed.acquired_at == 1000.0
    assert renewed.expires_at == 1040.0
    assert renewed.ttl == 30.0

    # Renewal with an explicit TTL.
    clock.advance(10)
    renewed2 = store.renew(renewed, ttl=60)
    assert renewed2.expires_at == 1080.0
    assert renewed2.ttl == 60.0

    # Release frees the object for re-acquire (even by another owner).
    store.release(renewed2)
    assert store.get("run", "RUN-1") is None
    other = store.acquire("run", "RUN-1", "worker-b", 30)
    assert other.owner == "worker-b"
    assert other.acquired_at == 1020.0


def test_release_is_idempotent(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    lease = store.acquire("run", "RUN-1", "worker-a", 30)

    store.release(lease)
    store.release(lease)  # no-op
    # Releasing a never-held handle is also a no-op.
    ghost = Lease("run", "RUN-2", "worker-a", "deadbeef", 1.0, 2.0, 30.0)
    store.release(ghost)
    assert store.get("run", "RUN-1") is None
    assert store.get("run", "RUN-2") is None


def test_release_refuses_to_delete_others_lease(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    lease = store.acquire("run", "RUN-1", "worker-a", 30)

    forged = Lease(
        object_type="run",
        object_id="RUN-1",
        owner="worker-b",
        nonce=lease.nonce,
        acquired_at=lease.acquired_at,
        expires_at=lease.expires_at,
        ttl=lease.ttl,
    )
    with pytest.raises(LeaseHeldError, match="worker-a"):
        store.release(forged)
    # The real lease is untouched.
    assert store.get("run", "RUN-1") == lease


def test_release_of_superseded_own_lease_refused(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    first = store.acquire("run", "RUN-1", "worker-a", 30)
    store.release(first)

    newer = store.acquire("run", "RUN-1", "worker-a", 30)
    # The old handle must not delete the newer grant of the same owner.
    with pytest.raises(LeaseHeldError):
        store.release(first)
    assert store.get("run", "RUN-1") == newer


def test_leases_are_isolated_per_object(tmp_path) -> None:
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    store.acquire("run", "RUN-1", "worker-a", 30)
    store.acquire("run", "RUN-2", "worker-b", 30)
    store.acquire("goal", "GOAL-1", "worker-c", 30)

    assert store.get("run", "RUN-1").owner == "worker-a"
    assert store.get("run", "RUN-2").owner == "worker-b"
    assert store.get("goal", "GOAL-1").owner == "worker-c"
    assert store.get("run", "GOAL-1") is None
    assert store.get("goal", "RUN-1") is None

    base = tmp_path / "state" / "leases"
    assert (base / "run" / "RUN-1.json").is_file()
    assert (base / "run" / "RUN-2.json").is_file()
    assert (base / "goal" / "GOAL-1.json").is_file()


def test_lease_store_coexists_with_state_backend(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["run"])
    backend.write("run", doc["run_id"], doc)

    clock = FakeClock(1000.0)
    store = LeaseStore(tmp_path / "state", now=clock)
    lease = store.acquire("run", doc["run_id"], "worker-a", 30)

    # The lease record lives in leases/ and does not disturb the object
    # tree of the state backend (and vice versa).
    assert store.get("run", doc["run_id"]) == lease
    assert backend.list_ids("run") == [doc["run_id"]]
    assert backend.read("run", doc["run_id"]) == doc


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_unknown_object_type_rejected(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        store.acquire("no-such-type", "X1", "worker-a", 30)
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        store.get("no-such-type", "X1")
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        store.renew(Lease("no-such-type", "X1", "w", "n", 1.0, 2.0, 30.0))
    assert not (tmp_path / "state").exists()


def test_invalid_object_id_rejected(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    for bad_id in ["", ".", "..", "../escape", "a/b", "a\\b", "id\x00x"]:
        with pytest.raises(ValueError):
            store.acquire("run", bad_id, "worker-a", 30)
    assert not (tmp_path / "state").exists()


def test_invalid_owner_rejected(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    for bad_owner in ["", "   ", 123, None]:
        with pytest.raises(ValueError):
            store.acquire("run", "RUN-1", bad_owner, 30)
    assert not (tmp_path / "state").exists()


def test_invalid_ttl_rejected(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    for bad_ttl in [0, -1, 0.0, float("inf"), float("nan"), True, "30"]:
        with pytest.raises((ValueError, TypeError)):
            store.acquire("run", "RUN-1", "worker-a", bad_ttl)
    assert not (tmp_path / "state").exists()


def test_renew_and_release_reject_non_lease(tmp_path) -> None:
    store = make_store(tmp_path, FakeClock())
    with pytest.raises(TypeError, match="Lease"):
        store.renew("not-a-lease")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="Lease"):
        store.release({"owner": "x"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Crash safety
# ---------------------------------------------------------------------------


def test_interrupted_acquire_leaves_no_lease_and_no_litter(
    tmp_path, monkeypatch
) -> None:
    """A crash between staging and create leaves no lease and no litter
    (mirrors the interrupted-write guarantee of ``atomic_write``)."""
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)

    def boom(src, dst):
        raise OSError("simulated crash before create")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash"):
        store.acquire("run", "RUN-1", "worker-a", 30)
    monkeypatch.undo()

    assert store.get("run", "RUN-1") is None
    leases_dir = tmp_path / "state" / "leases" / "run"
    if leases_dir.exists():
        assert list(leases_dir.iterdir()) == []

    # A later acquire succeeds normally.
    lease = store.acquire("run", "RUN-1", "worker-a", 30)
    assert lease.owner == "worker-a"
    assert store.get("run", "RUN-1") == lease


def test_stale_claim_tmp_never_mistaken_for_lease(tmp_path) -> None:
    """A leftover staging file from a crashed claimer is invisible."""
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock)
    leases_dir = tmp_path / "state" / "leases" / "run"
    leases_dir.mkdir(parents=True)
    stale = leases_dir / ".RUN-1.json.claim-0123456789ab.tmp"
    stale.write_text('{"object_type": "run"', encoding="utf-8")  # truncated

    lease = store.acquire("run", "RUN-1", "worker-a", 30)
    assert lease.owner == "worker-a"
    assert store.get("run", "RUN-1") == lease
