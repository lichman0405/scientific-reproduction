"""Integration: event log, leases and objects as one coherent state core
(DEV-M1-G06, acceptance AC-02/AC-03).

Cross-layer scenarios the unit suites (tests/state/) cannot express:

* a lease-guarded write with an idempotent event: re-running the same
  step (acquire lease -> write object -> append event -> release) with
  the same idempotency key never duplicates the semantic event, and the
  object rewrite keeps the state schema-valid (AC-02);
* concurrent workers, each leasing its own object, writing it, and
  logging its completion: every object is schema-valid and the event
  log order is deterministic and duplicate-free (AC-03);
* a takeover after expiry (injected clock, no sleeps): the events
  record the handover in append order (AC-03) while exactly one
  canonical object remains on disk;
* the whole state -- objects, event records, sequence counter -- is
  recoverable from the persisted files alone: fresh backend and fresh
  log instances over the same base dir see identical state (AC-03).
"""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from scientific_reproduction.core import events as ev
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.leases import LeaseHeldError, LeaseStore
from scientific_reproduction.core.models import ProjectEvent
from scientific_reproduction.core.schema_validation import validate_object
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from tests.core.fixtures import VALID_DOCS


class FakeClock:
    """Injectable deterministic clock (epoch seconds)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _event(n: int, event_type: str, object_id: str, **overrides: object) -> ProjectEvent:
    """A schema-valid event with a unique deterministic id."""
    kwargs: dict[str, object] = {
        "event_id": generate_id("event", f"core-state-{n:03d}"),
        "timestamp": "2026-01-01T00:00:00Z",
        "actor": "supervisor",
        "event_type": event_type,
        "object_id": object_id,
        "payload": {"n": n},
    }
    kwargs.update(overrides)
    return ProjectEvent(**kwargs)


def _run_doc(object_id: str, *, goal_version: str, claimant: str) -> dict:
    """A schema-valid run document identifiable by its ``claimant`` marker."""
    doc = copy.deepcopy(VALID_DOCS["run"])
    doc["run_id"] = object_id
    doc["goal_version"] = goal_version
    doc["claimant"] = claimant  # additionalProperties: true
    return doc


# ---------------------------------------------------------------------------
# AC-02: lease-guarded write + idempotent event replay
# ---------------------------------------------------------------------------


def test_lease_guarded_write_with_idempotent_event_replay(tmp_path) -> None:
    """AC-02: replaying a lease-guarded publish step is idempotent.

    The full worker step -- acquire lease, write object, append event,
    release -- re-run with the same idempotency key returns the original
    event record instead of duplicating it, and the object rewrite
    stays schema-valid with exactly one canonical file.
    """
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store = LeaseStore(base, now=clock)
    log = ev.ProjectEventLog(base)
    run_id = generate_id("run", "publish-1")

    def publish(goal_version: str) -> tuple[bool, ev.EventRecord]:
        lease = store.acquire("run", run_id, "worker-a", 30)
        backend.write("run", run_id, _run_doc(run_id, goal_version=goal_version, claimant="worker-a"))
        record = log.append(
            _event(1, "run.published", run_id, payload={"goal_version": goal_version}),
            idempotency_key="publish-run-1",
        )
        store.release(lease)
        return record.replayed, record

    first_replayed, first_record = publish("v1")
    assert first_replayed is False
    assert first_record.sequence == 1
    assert store.get("run", run_id) is None  # released

    # Re-running the identical step replays the event (no duplicate, no
    # sequence advance) and rewrites the object schema-valid.
    second_replayed, second_record = publish("v2")
    assert second_replayed is True
    assert second_record.sequence == 1
    records = log.list_events()
    assert len(records) == 1
    assert records[0].event.event_type == "run.published"
    assert records[0].event.payload["goal_version"] == "v1"  # original event

    stored = backend.read("run", run_id)
    assert stored["goal_version"] == "v2"
    assert validate_object("run", stored) == []
    assert backend.list_ids("run") == [run_id]


# ---------------------------------------------------------------------------
# AC-03: concurrent workers -- deterministic, duplicate-free event order
# ---------------------------------------------------------------------------


def test_concurrent_workers_lease_write_and_log_deterministically(tmp_path) -> None:
    """AC-03: 8 workers each lease, write, and log their own object.

    Every object is on disk (exactly one file per object) and
    schema-valid; the event log contains exactly one record per worker
    with strictly increasing sequences and a deterministic order, and
    re-submission with the same idempotency keys replays without
    duplicating anything.
    """
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store = LeaseStore(base, now=clock)
    log = ev.ProjectEventLog(base)
    n_workers = 8
    run_ids = [generate_id("run", f"worker-{i}") for i in range(n_workers)]

    def worker(i: int) -> None:
        run_id = run_ids[i]
        lease = store.acquire("run", run_id, f"worker-{i}", 30)
        backend.write("run", run_id, _run_doc(run_id, goal_version="v1", claimant=f"worker-{i}"))
        log.append(
            _event(i + 1, "run.claimed", run_id, payload={"worker": i}),
            idempotency_key=f"worker-{i}-done",
        )
        store.release(lease)

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        list(pool.map(worker, range(n_workers)))

    # Every object is on disk, exactly one file per object, schema-valid.
    assert sorted(backend.list_ids("run")) == sorted(run_ids)
    for run_id in backend.list_ids("run"):
        assert validate_object("run", backend.read("run", run_id)) == []
    # All leases were released.
    for run_id in run_ids:
        assert store.get("run", run_id) is None

    # The event log is complete, ordered, and duplicate-free (AC-03).
    # Sequence order is the append order (thread scheduling decides which
    # worker appends first, so the workers appear in *some* permutation),
    # but every worker appears exactly once with its own sequence.
    records = log.list_events()
    assert len(records) == n_workers
    assert [r.sequence for r in records] == list(range(1, n_workers + 1))
    assert len({r.event.event_id for r in records}) == n_workers
    assert sorted(r.event.payload["worker"] for r in records) == list(
        range(n_workers)
    )

    # Re-submission with the same idempotency keys replays, never
    # duplicates, and does not advance the sequence (each replay returns
    # the original record with its original sequence).
    original = {r.event.event_id: r for r in records}
    for i in range(n_workers):
        replay = log.append(
            _event(i + 1, "run.claimed", run_ids[i], payload={"worker": i}),
            idempotency_key=f"worker-{i}-done",
        )
        assert replay.replayed is True
        assert replay.sequence == original[replay.event.event_id].sequence
    assert len(log.list_events()) == n_workers


# ---------------------------------------------------------------------------
# AC-02 + AC-03: expiry takeover recorded by the event log
# ---------------------------------------------------------------------------


def test_expired_lease_takeover_records_ordered_events(tmp_path) -> None:
    """AC-02/AC-03: an expiry handover writes exactly one object and logs
    the lifecycle in append order."""
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store = LeaseStore(base, now=clock)
    log = ev.ProjectEventLog(base)
    run_id = generate_id("run", "handover-1")

    # Holder A acquires, writes, and logs; then it crashes -- no release,
    # the lease simply expires.
    lease_a = store.acquire("run", run_id, "worker-a", 30)
    backend.write("run", run_id, _run_doc(run_id, goal_version="v1", claimant="worker-a"))
    log.append(
        _event(1, "run.claimed", run_id, payload={"owner": "worker-a"}),
        idempotency_key="handover-claim",
    )

    clock.advance(31)  # lease_a expired

    # Fresh claimant B takes over deterministically and rewrites the object.
    lease_b = store.acquire("run", run_id, "worker-b", 10)
    assert lease_b.owner == "worker-b"
    assert lease_b.acquired_at == 1031.0
    assert lease_b.nonce != lease_a.nonce
    backend.write("run", run_id, _run_doc(run_id, goal_version="v2", claimant="worker-b"))
    log.append(
        _event(2, "run.taken-over", run_id, payload={"owner": "worker-b"}),
        idempotency_key="handover-takeover",
    )

    # Exactly one canonical object remains, and it is the new owner's.
    assert backend.list_ids("run") == [run_id]
    stored = backend.read("run", run_id)
    assert stored["claimant"] == "worker-b"
    assert stored["goal_version"] == "v2"
    assert validate_object("run", stored) == []

    # The event log recorded the handover in append order (AC-03).
    records = log.list_events()
    assert [r.event.event_type for r in records] == ["run.claimed", "run.taken-over"]
    assert [r.sequence for r in records] == [1, 2]
    assert [r.event.payload["owner"] for r in records] == ["worker-a", "worker-b"]

    # The crashed holder's stale handle can neither renew nor release
    # the new grant while it is live on disk.
    with pytest.raises(LeaseHeldError, match="worker-b"):
        store.renew(lease_a)
    with pytest.raises(LeaseHeldError, match="worker-b"):
        store.release(lease_a)
    assert store.get("run", run_id) == lease_b

    store.release(lease_b)
    assert store.get("run", run_id) is None  # B released cleanly


# ---------------------------------------------------------------------------
# AC-03: the whole persisted state is recoverable from files alone
# ---------------------------------------------------------------------------


def test_whole_state_recoverable_from_fresh_instances(tmp_path) -> None:
    """AC-03: a fresh backend and a fresh log over the same base dir
    reconstruct the identical state from the persisted files alone."""
    base = tmp_path / "state"
    clock = FakeClock(1000.0)
    backend = FilesystemStateBackend(base)
    store = LeaseStore(base, now=clock)
    log = ev.ProjectEventLog(base)
    run_ids = [generate_id("run", f"mixed-{i}") for i in range(5)]

    for i, run_id in enumerate(run_ids):
        lease = store.acquire("run", run_id, f"worker-{i}", 30)
        backend.write("run", run_id, _run_doc(run_id, goal_version="v1", claimant=f"worker-{i}"))
        log.append(_event(i + 1, "run.created", run_id), idempotency_key=f"mixed-{i}")
        store.release(lease)

    # Fresh instances over the same base dir see the same state.
    fresh_backend = FilesystemStateBackend(base)
    fresh_log = ev.ProjectEventLog(base)
    assert fresh_log.list_events() == log.list_events()
    assert [r.sequence for r in fresh_log.list_events()] == [1, 2, 3, 4, 5]
    assert fresh_backend.list_ids("run") == sorted(run_ids)
    for run_id in fresh_backend.list_ids("run"):
        assert validate_object("run", fresh_backend.read("run", run_id)) == []
    # Every lease was released: no lease records survive.
    for run_id in run_ids:
        assert store.get("run", run_id) is None
