"""Tests for the append-only, ordered, idempotent project event log
(DEV-M1-G04, acceptance AC-01/AC-02/AC-03).

Covered behaviors:
  * append/get/list round-trips over FilesystemStateBackend (obj_type
    "event") with per-record deterministic sequences, empty-log
    behavior, and ``from``/``to`` alias round-tripping;
  * append-only (AC-01): re-append of an existing event id is rejected,
    the log exposes no update/replace/delete API, and record files stay
    byte-identical across every operation;
  * idempotency (AC-02): re-submission with the same idempotency key
    returns the existing record -- never a duplicate semantic event --
    different keys produce distinct events, and stale claims left by a
    crash are reclaimed deterministically;
  * ordering (AC-03): list_events is deterministic across fresh log
    instances, recoverable from the persisted records alone, uses
    strictly increasing non-repeating sequences, and idempotent
    re-submission never advances the sequence;
  * schema-invalid events are rejected before anything is persisted;
  * unknown object types and malformed records in the event store are
    surfaced loudly;
  * concurrent appends from threads produce no duplicates, no repeated
    sequences, and a deterministic final order.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from scientific_reproduction.core import events as ev
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import ProjectEvent
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_object,
)
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    UnknownObjectTypeError,
)


def _event(n: int, **overrides: object) -> ProjectEvent:
    """A schema-valid event with a unique deterministic id."""
    kwargs: dict[str, object] = {
        "event_id": generate_id("event", f"evt-{n:03d}"),
        "timestamp": "2026-01-01T00:00:00Z",
        "actor": "supervisor",
        "event_type": f"test.{n:03d}",
        "payload": {"n": n},
    }
    kwargs.update(overrides)
    return ProjectEvent(**kwargs)


def _record_path(base, event_id):
    return base / "events" / f"{event_id}.json"


# ---------------------------------------------------------------------------
# Basic round-trips / empty log
# ---------------------------------------------------------------------------


def test_append_get_event_round_trip(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    event = _event(1)
    record = log.append(event)
    assert record.event == event
    assert record.sequence == 1
    assert record.replayed is False

    fetched = log.get(event.event_id)
    assert fetched is not None
    assert fetched.event == event
    assert fetched.sequence == 1
    assert fetched.replayed is False
    assert log.get(generate_id("event", "never-appended")) is None


def test_empty_event_log(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    assert log.list_events() == []
    assert log.get("anything") is None
    assert log.append(_event(1)).sequence == 1
    assert [r.event.event_id for r in log.list_events()] == [
        _event(1).event_id
    ]


def test_event_from_to_fields_round_trip(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    event = _event(1, from_="DRAFT", to="FROZEN")
    log.append(event)
    fetched = log.get(event.event_id)
    assert fetched is not None
    assert fetched.event.from_ == "DRAFT"
    assert fetched.event.to == "FROZEN"


# ---------------------------------------------------------------------------
# AC-01: append-only, never mutated
# ---------------------------------------------------------------------------


def test_reappend_same_event_id_rejected(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    event = _event(1)
    log.append(event)
    with pytest.raises(ev.DuplicateEventIdError):
        log.append(event)
    # A fresh idempotency key cannot smuggle in a duplicate event id.
    with pytest.raises(ev.DuplicateEventIdError):
        log.append(event, idempotency_key="key-for-existing-event")
    assert [r.event.event_id for r in log.list_events()] == [event.event_id]


def test_event_log_exposes_no_mutation_api(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    for forbidden in ("update", "replace", "delete", "remove", "overwrite", "put"):
        assert not hasattr(log, forbidden), f"log must not expose {forbidden}()"


def test_event_records_stay_byte_identical(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    event1 = _event(1)
    log.append(event1, idempotency_key="k1")
    path = _record_path(base, event1.event_id)
    before = path.read_bytes()

    log.append(_event(2))
    log.append(_event(3), idempotency_key="k3")
    assert [r.event.event_id for r in log.list_events()] == [
        event1.event_id,
        _event(2).event_id,
        _event(3).event_id,
    ]
    replay = log.append(_event(1), idempotency_key="k1")
    assert replay.event.event_id == event1.event_id
    with pytest.raises(ev.DuplicateEventIdError):
        log.append(event1)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# AC-02: idempotency keys
# ---------------------------------------------------------------------------


def test_idempotent_event_returns_existing_record(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    event1 = _event(1)
    first = log.append(event1, idempotency_key="resume")
    assert first.sequence == 1
    assert first.replayed is False

    # Same key, different event object/id: still the same semantic event.
    replay = log.append(_event(2), idempotency_key="resume")
    assert replay.event.event_id == event1.event_id
    assert replay.sequence == 1
    assert replay.replayed is True

    records = log.list_events()
    assert [r.event.event_id for r in records] == [event1.event_id]
    assert [r.sequence for r in records] == [1]
    assert len(list((base / "events").glob("*.json"))) == 1


def test_distinct_keys_produce_distinct_events(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    log.append(_event(1), idempotency_key="k1")
    log.append(_event(2), idempotency_key="k2")
    records = log.list_events()
    assert [r.event.event_id for r in records] == [
        _event(1).event_id,
        _event(2).event_id,
    ]
    assert [r.sequence for r in records] == [1, 2]
    assert all(r.replayed is False for r in records)


def test_event_stale_idempotency_claim_is_reclaimed(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    event1 = _event(1)
    log.append(event1, idempotency_key="crashy")
    claim_files = list((base / "_event_log" / "idempotency").glob("*.json"))
    assert len(claim_files) == 1

    # Simulate a crash between claim creation and record write: the
    # claim survives but the record is gone. The claim must be reclaimed
    # deterministically so the key remains usable.
    _record_path(base, event1.event_id).unlink()
    record = log.append(_event(2), idempotency_key="crashy")
    assert record.event.event_id == _event(2).event_id
    assert record.replayed is False
    assert record.sequence == 2
    assert len(list((base / "events").glob("*.json"))) == 1
    assert len(list((base / "_event_log" / "idempotency").glob("*.json"))) == 1

    # The recovered key now replays against the recovered record.
    replay = log.append(_event(3), idempotency_key="crashy")
    assert replay.event.event_id == _event(2).event_id
    assert replay.replayed is True


# ---------------------------------------------------------------------------
# AC-03: deterministic, recoverable ordering
# ---------------------------------------------------------------------------


def test_event_order_deterministic_across_instances(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    # Hash-based event ids do not sort in append order: ordering must
    # come from the sequence, not from the ids.
    for n in (1, 2, 3):
        log.append(_event(n))
    first = log.list_events()
    fresh = ev.ProjectEventLog(base)
    assert fresh.list_events() == first
    append_order = [_event(n).event_id for n in (1, 2, 3)]
    assert [r.event.event_id for r in first] == append_order
    assert append_order != sorted(append_order)


def test_event_order_recoverable_from_persisted_records(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    for n in (1, 2, 3):
        log.append(_event(n))

    # Recover purely from the record files, without any log instance.
    backend = FilesystemStateBackend(base)
    raw = [backend.read("event", eid) for eid in backend.list_ids("event")]
    assert sorted(r["sequence"] for r in raw) == [1, 2, 3]

    # Deleting the sequence counter does not change the recorded order,
    # and the next append continues past the maximum recorded sequence.
    (base / "_event_log" / "sequence.json").unlink()
    fresh = ev.ProjectEventLog(base)
    assert [r.sequence for r in fresh.list_events()] == [1, 2, 3]
    assert fresh.append(_event(4)).sequence == 4


def test_event_sequences_never_repeat(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    for n in range(1, 21):
        log.append(_event(n))
    sequences = [r.sequence for r in log.list_events()]
    assert sequences == list(range(1, 21))
    assert len(set(sequences)) == 20


def test_event_replay_does_not_advance_sequence(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    log.append(_event(1), idempotency_key="k1")
    log.append(_event(2), idempotency_key="k2")
    log.append(_event(1), idempotency_key="k1")  # replay
    log.append(_event(3), idempotency_key="k3")
    log.append(_event(2), idempotency_key="k2")  # replay
    records = log.list_events()
    assert [r.sequence for r in records] == [1, 2, 3]
    assert all(r.replayed is False for r in records)
    assert len(records) == 3


# ---------------------------------------------------------------------------
# Validation gates
# ---------------------------------------------------------------------------


def test_schema_invalid_event_rejected_before_persistence(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    # payload must be an object per schemas/event.schema.yaml.
    bad = _event(1, payload=[])
    with pytest.raises(SchemaValidationError):
        log.append(bad)
    assert not (base / "events").exists()
    assert not (base / "_event_log").exists()
    # The log still works afterwards, and the first sequence is 1.
    assert log.append(_event(2)).sequence == 1


def test_event_stored_records_remain_schema_valid(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    log.append(_event(1), idempotency_key="k1")
    log.append(_event(2))
    backend = FilesystemStateBackend(tmp_path / "state")
    for event_id in backend.list_ids("event"):
        assert validate_object("event", backend.read("event", event_id)) == []


def test_append_rejects_non_event_and_bad_keys(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    with pytest.raises(TypeError):
        log.append("not an event")
    with pytest.raises(TypeError):
        log.append(None)
    with pytest.raises(ValueError):
        log.append(_event(1), idempotency_key="")
    with pytest.raises(TypeError):
        log.append(_event(2), idempotency_key=123)
    assert log.list_events() == []


# ---------------------------------------------------------------------------
# Unknown types / corrupt state in the event store
# ---------------------------------------------------------------------------


def test_event_store_backend_rejects_unknown_object_types(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(UnknownObjectTypeError):
        backend.write("not-a-type", "X-1", {})


def test_non_event_record_in_event_dir_surfaces_error(tmp_path) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    log.append(_event(1))

    # A stray file that is not a JSON object: the backend error surfaces.
    (base / "events" / "not_an_event.json").write_text(
        "[1, 2, 3]", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        log.list_events()
    with pytest.raises(ValueError):
        log.get("not_an_event")

    # A schema-valid event JSON without a sequence is a corrupt record.
    doc = _event(2).to_dict()
    (base / "events" / "no_sequence.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )
    with pytest.raises(ev.CorruptEventLogError):
        log.list_events()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_event_appends_no_duplicates_deterministic_order(
    tmp_path,
) -> None:
    base = tmp_path / "state"
    log = ev.ProjectEventLog(base)
    n_threads, per_thread = 8, 25
    total = n_threads * per_thread

    def worker(thread_index: int) -> None:
        for i in range(per_thread):
            n = thread_index * per_thread + i + 1
            log.append(_event(n), idempotency_key=f"t{thread_index}-i{i}")

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    records = log.list_events()
    assert len(records) == total
    # No duplicate records and no repeated/gapped sequences.
    assert len({r.event.event_id for r in records}) == total
    assert [r.sequence for r in records] == list(range(1, total + 1))
    assert len({(r.event.event_id, r.sequence) for r in records}) == total
    # The deterministic order matches the records on disk.
    backend = FilesystemStateBackend(base)
    raw = [backend.read("event", eid) for eid in backend.list_ids("event")]
    raw.sort(key=lambda doc: doc["sequence"])
    assert [doc["event_id"] for doc in raw] == [
        r.event.event_id for r in records
    ]


def test_concurrent_event_same_key_appends_single_record(tmp_path) -> None:
    log = ev.ProjectEventLog(tmp_path / "state")
    n_threads = 16
    results: list[ev.EventRecord] = []
    results_lock = threading.Lock()

    def worker(thread_index: int) -> None:
        record = log.append(
            _event(thread_index + 100), idempotency_key="shared-key"
        )
        with results_lock:
            results.append(record)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        list(pool.map(worker, range(n_threads)))

    records = log.list_events()
    assert len(records) == 1  # exactly one semantic event
    assert all(r.event.event_id == records[0].event.event_id for r in results)
    # Exactly one thread was the first to append; every other thread
    # received the existing record (replayed).
    assert sum(1 for r in results if r.replayed) == n_threads - 1
    assert sum(1 for r in results if not r.replayed) == 1
