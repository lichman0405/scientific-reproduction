"""Tests for the atomic lock primitive (DEV-M1-G03, AC-01).

Covers ``core.locks``:
  * ``atomic_create`` -- atomic create-if-not-exists with complete
    content: the file either does not exist or exists with the full
    fsynced content (never a partial write, never an empty file), and
    the staging file is always cleaned up;
  * ``FileLockStore`` -- acquire/release, held rejection, context
    manager, idempotent release, ownership-verified release, key
    validation, mtime-based stale breaking, and a real concurrent
    claim with a single winner.

The race tests use threading barriers (no sleeps); determinism comes
from the atomic protocol (``os.link`` create-if-not-exists), not from
timing.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from scientific_reproduction.core.locks import (
    FileLock,
    FileLockStore,
    LockHeldError,
    atomic_create,
)

# ---------------------------------------------------------------------------
# atomic_create: atomic create-if-not-exists with complete content
# ---------------------------------------------------------------------------


def test_atomic_create_creates_file_with_complete_content(tmp_path) -> None:
    target = tmp_path / "obj.lock"
    atomic_create(target, json.dumps({"owner": "a", "seq": 1}))
    assert json.loads(target.read_text(encoding="utf-8")) == {"owner": "a", "seq": 1}
    # No staging file is left behind.
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_create_fails_when_target_exists(tmp_path) -> None:
    target = tmp_path / "obj.lock"
    atomic_create(target, "first")
    with pytest.raises(FileExistsError):
        atomic_create(target, "second")
    # The existing file is untouched.
    assert target.read_text(encoding="utf-8") == "first"


def test_atomic_create_creates_parent_directories(tmp_path) -> None:
    target = tmp_path / "deep" / "nested" / "obj.lock"
    atomic_create(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_atomic_create_accepts_binary_content(tmp_path) -> None:
    atomic_create(tmp_path / "b.lock", b"\x00\x01\xfe\xff")
    assert (tmp_path / "b.lock").read_bytes() == b"\x00\x01\xfe\xff"


def test_atomic_create_content_is_always_complete_under_real_race(tmp_path) -> None:
    """Two concurrent creators: exactly one wins, and the surviving file
    is complete valid JSON -- readers can never observe a partial write.
    """
    target = tmp_path / "obj.lock"
    payloads = [
        json.dumps({"owner": "worker-a", "seq": 1}),
        json.dumps({"owner": "worker-b", "seq": 2}),
    ]
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def claim(payload: str) -> None:
        try:
            barrier.wait(timeout=10)
            atomic_create(target, payload)
            outcomes.append("ok")
        except FileExistsError:
            outcomes.append("exists")

    threads = [
        threading.Thread(target=claim, args=(payload,), daemon=True)
        for payload in payloads
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)

    assert sorted(outcomes) == ["exists", "ok"]
    assert json.loads(target.read_text(encoding="utf-8")) in [
        json.loads(p) for p in payloads
    ]


# ---------------------------------------------------------------------------
# FileLockStore: acquire / release / context manager / ownership
# ---------------------------------------------------------------------------


def test_lock_store_acquire_release(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    lock = store.acquire("run-RUN-1", "worker-a")
    assert store.is_locked("run-RUN-1")
    assert lock.path == tmp_path / "locks" / "run-RUN-1.lock"

    lock.release()
    assert not store.is_locked("run-RUN-1")


def test_lock_file_records_owner_metadata(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    lock = store.acquire("key-1", "worker-a")
    data = json.loads(lock.path.read_text(encoding="utf-8"))
    assert data["owner"] == "worker-a"
    assert data["nonce"]
    assert data["pid"] == os.getpid()


def test_lock_held_raises_and_try_acquire_returns_none(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    lock = store.acquire("key-1", "worker-a")

    with pytest.raises(LockHeldError, match="already held"):
        store.acquire("key-1", "worker-b")
    assert store.try_acquire("key-1", "worker-b") is None
    assert store.is_locked("key-1")

    lock.release()
    second = store.try_acquire("key-1", "worker-b")
    assert second is not None
    second.release()


def test_lock_context_manager_releases_on_exit(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    with store.acquire("key-1", "worker-a") as lock:
        assert lock.path.is_file()
    assert not (tmp_path / "locks" / "key-1.lock").exists()


def test_lock_release_is_idempotent(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    lock = store.acquire("key-1", "worker-a")
    lock.release()
    lock.release()  # no-op


def test_lock_release_refuses_foreign_handle(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    lock = store.acquire("key-1", "worker-a")

    foreign = FileLock(lock.path, "worker-b", "not-the-nonce")
    with pytest.raises(LockHeldError):
        foreign.release()
    assert store.is_locked("key-1")

    # The real owner still releases it.
    lock.release()
    assert not store.is_locked("key-1")


def test_lock_concurrent_claim_single_winner(tmp_path, monkeypatch) -> None:
    """AC-01: two threads claim one lock with the create-if-not-exists
    step interleaved -- exactly one winner."""
    for i in range(5):
        store = FileLockStore(tmp_path / f"locks-{i}")
        outcomes: list[str] = []
        held: list[FileLock] = []
        barrier = threading.Barrier(2)
        real_link = os.link

        def interleaved_link(src, dst, *args, **kwargs):
            barrier.wait(timeout=10)
            return real_link(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "link", interleaved_link)

        def claim(owner: str) -> None:
            try:
                lock = store.acquire("key-1", owner)
                outcomes.append("ok")
                held.append(lock)
            except LockHeldError:
                outcomes.append("held")

        threads = [
            threading.Thread(target=claim, args=(owner,), daemon=True)
            for owner in ("worker-a", "worker-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)

        # The lock is held until released, so a serialized late claimer
        # would still fail: exactly one winner, one rejection.
        assert sorted(outcomes) == ["held", "ok"]
        assert len(held) == 1
        assert store.is_locked("key-1")

        held[0].release()
        assert not store.is_locked("key-1")
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Staleness (mtime grace) and key validation
# ---------------------------------------------------------------------------


def test_fresh_lock_is_never_broken(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks", stale_after=1.0)
    lock = store.acquire("key-1", "worker-a")
    assert store.try_acquire("key-1", "worker-b") is None
    lock.release()


def test_stale_lock_can_be_broken_deterministically(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks", stale_after=1.0)
    lock = store.acquire("key-1", "worker-a")

    # Move the lock file's mtime far into the past: it is abandoned.
    past = time.time() - 100.0
    os.utime(lock.path, (past, past))

    new = store.try_acquire("key-1", "worker-b")
    assert new is not None
    data = json.loads(new.path.read_text(encoding="utf-8"))
    assert data["owner"] == "worker-b"
    new.release()
    assert not store.is_locked("key-1")


def test_stale_breaking_is_disabled_by_default(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")  # stale_after=None
    lock = store.acquire("key-1", "worker-a")
    past = time.time() - 100.0
    os.utime(lock.path, (past, past))

    # Deterministic default: locks are never broken automatically.
    assert store.try_acquire("key-1", "worker-b") is None
    lock.release()


def test_invalid_lock_key_rejected(tmp_path) -> None:
    store = FileLockStore(tmp_path / "locks")
    for bad_key in ["", ".", "..", "a/b", "a\\b", "id\x00x"]:
        with pytest.raises(ValueError):
            store.acquire(bad_key, "worker-a")
        with pytest.raises(ValueError):
            store.is_locked(bad_key)
    with pytest.raises(ValueError):
        store.acquire("key-1", "")
    with pytest.raises(ValueError):
        store.try_acquire("key-1", "   ")
    assert not (tmp_path / "locks").exists()
