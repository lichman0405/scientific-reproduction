"""Integration: full object lifecycle -- create/read/update/delete --
with per-step events, all schema-valid (DEV-M1-G06, acceptance AC-02).

The unit suites cover each layer alone (tests/core/, tests/state/);
here the layers run together:

* ``FilesystemStateBackend`` (DEV-M1-G02) persists a schema-valid object
  at every lifecycle step -- create, update, event-driven rewrite,
  delete -- and every persisted write round-trips schema-valid through
  ``validate_object`` (the DEV-M1-G01 gate, enforced end-to-end);
* identity fields (``run_id``/``plan_id``/``goal_id``) survive updates
  and there is exactly one canonical file per object id at every step
  (per-object file layout, no version clutter);
* ``ProjectEventLog`` (DEV-M1-G04) records each step on the same base
  dir; ``list_events`` returns the append order with strictly
  increasing sequences, and every persisted event record is
  schema-valid;
* an event-driven rewrite -- an object written from event payload data
  -- stays schema-valid;
* a schema-invalid update is rejected before persistence and the last
  valid object is untouched on disk;
* the whole model registry round-trips write/read/validate/delete with
  an idempotent event per type.

No sleeps; the lease clock is irrelevant here (no leases involved).
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

import pytest

from scientific_reproduction.core import events as ev
from scientific_reproduction.core import models as m
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import ProjectEvent
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_object,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from tests.core.fixtures import VALID_DOCS


def _id_field(obj_type: str) -> str:
    """Name of the identity field of the model for ``obj_type``.

    Every model declares its ID as its first dataclass field, named
    ``<something>_id`` (mirroring the schema's identity property). The
    assertion pins that assumption so a future reorder fails loudly.
    """
    field_name = dataclasses.fields(m.MODEL_REGISTRY[obj_type])[0].name
    assert field_name.endswith("_id"), field_name
    return field_name


def _plan_doc(
    plan_id: str, *, version: str, status: str, **overrides: object
) -> dict[str, Any]:
    """A schema-valid plan document with the given version/status."""
    doc = copy.deepcopy(VALID_DOCS["plan"])
    doc["plan_id"] = plan_id
    doc["version"] = version
    doc["status"] = status
    doc.update(overrides)
    return doc


def _lifecycle_event(
    n: int, event_type: str, object_id: str, **overrides: object
) -> ProjectEvent:
    """A schema-valid event with a unique deterministic id."""
    kwargs: dict[str, object] = {
        "event_id": generate_id("event", f"obj-lifecycle-{n:03d}"),
        "timestamp": "2026-01-01T00:00:00Z",
        "actor": "supervisor",
        "event_type": event_type,
        "object_id": object_id,
        "payload": {"n": n},
    }
    kwargs.update(overrides)
    return ProjectEvent(**kwargs)


# ---------------------------------------------------------------------------
# AC-02: plan lifecycle with an event per step
# ---------------------------------------------------------------------------


def test_plan_lifecycle_create_read_update_rewrite_delete_with_events(
    tmp_path,
) -> None:
    """AC-02: the full create/read/update/delete path with per-step events.

    Every persisted write round-trips schema-valid, the version/identity
    fields survive every step, exactly one canonical file exists per
    object id, and the event log preserves the lifecycle order.
    """
    base = tmp_path / "state"
    backend = FilesystemStateBackend(base)
    log = ev.ProjectEventLog(base)
    plan_id = "PLAN-INT-001"

    # -- create: the first persisted write round-trips schema-valid ------
    doc_v1 = _plan_doc(plan_id, version="v1-draft", status="DRAFT")
    backend.write("plan", plan_id, doc_v1)
    stored = backend.read("plan", plan_id)
    assert stored == doc_v1
    assert validate_object("plan", stored) == []
    created = log.append(
        _lifecycle_event(
            1, "plan.created", plan_id, payload={"version": "v1-draft"}
        )
    )
    assert created.sequence == 1

    # -- update: version bumps, identity survives, single canonical file -
    doc_v2 = _plan_doc(
        plan_id, version="v2-frozen", status="FROZEN", parent_plan_version="v1-draft"
    )
    backend.write("plan", plan_id, doc_v2)
    stored = backend.read("plan", plan_id)
    assert stored["plan_id"] == plan_id  # identity survives
    assert stored["version"] == "v2-frozen"  # the version path survives
    assert stored["status"] == "FROZEN"
    assert validate_object("plan", stored) == []
    assert backend.list_ids("plan") == [plan_id]  # no version clutter
    frozen = log.append(
        _lifecycle_event(
            2,
            "plan.frozen",
            plan_id,
            from_="DRAFT",
            to="FROZEN",
            payload={"version": "v2-frozen"},
        )
    )
    assert frozen.sequence == 2

    # -- event-driven rewrite: an object derived from the event stays valid
    record = log.get(frozen.event.event_id)
    assert record is not None
    payload_version = record.event.payload["version"]
    doc_v3 = _plan_doc(plan_id, version=f"{payload_version}-rewrite", status="FROZEN")
    backend.write("plan", plan_id, doc_v3)
    stored = backend.read("plan", plan_id)
    assert stored["version"] == "v2-frozen-rewrite"
    assert validate_object("plan", stored) == []
    rewritten = log.append(
        _lifecycle_event(
            3, "plan.rewritten", plan_id, payload={"version": stored["version"]}
        )
    )
    assert rewritten.sequence == 3

    # -- delete: object gone, the log outlives it ------------------------
    backend.delete("plan", plan_id)
    assert not backend.exists("plan", plan_id)
    assert backend.list_ids("plan") == []
    deleted = log.append(_lifecycle_event(4, "plan.deleted", plan_id))
    assert deleted.sequence == 4

    # The event log preserved the exact lifecycle order (AC-03 rule
    # (sequence, event_id) on a single log instance == append order).
    records = log.list_events()
    assert [r.event.event_type for r in records] == [
        "plan.created",
        "plan.frozen",
        "plan.rewritten",
        "plan.deleted",
    ]
    assert [r.sequence for r in records] == [1, 2, 3, 4]
    # Every persisted event record is schema-valid (AC-02 gate end-to-end).
    for event_id in backend.list_ids("event"):
        assert validate_object("event", backend.read("event", event_id)) == []


# ---------------------------------------------------------------------------
# AC-02: run lifecycle -- identity survives, invalid update rejected
# ---------------------------------------------------------------------------


def test_run_update_keeps_identity_and_invalid_update_preserves_object(
    tmp_path,
) -> None:
    """AC-02: identity/version fields survive the update path, and a
    schema-invalid update is rejected before persistence, leaving the
    last valid object intact."""
    base = tmp_path / "state"
    backend = FilesystemStateBackend(base)
    log = ev.ProjectEventLog(base)
    run_id = "RUN-INT-001"

    def run_doc(lifecycle_state: str, goal_version: str, claimant: str) -> dict[str, Any]:
        doc = copy.deepcopy(VALID_DOCS["run"])
        doc["run_id"] = run_id
        doc["lifecycle_state"] = lifecycle_state
        doc["goal_version"] = goal_version
        doc["claimant"] = claimant  # additionalProperties: true
        return doc

    # -- create --
    v1 = run_doc("CREATED", "v1", "worker-a")
    backend.write("run", run_id, v1)
    assert backend.read("run", run_id) == v1
    assert validate_object("run", backend.read("run", run_id)) == []
    log.append(_lifecycle_event(1, "run.created", run_id, payload={"goal_version": "v1"}))

    # -- update: identity and version path survive --
    v2 = run_doc("RESULT_AVAILABLE", "v2", "worker-a")
    backend.write("run", run_id, v2)
    stored = backend.read("run", run_id)
    assert stored["run_id"] == run_id  # identity survives the update
    assert stored["goal_version"] == "v2"  # the version path survives
    assert stored["lifecycle_state"] == "RESULT_AVAILABLE"
    assert validate_object("run", stored) == []
    assert backend.list_ids("run") == [run_id]
    log.append(
        _lifecycle_event(
            2, "run.updated", run_id, from_="CREATED", to="RESULT_AVAILABLE"
        )
    )

    # -- invalid update: rejected before persistence (AC-02 gate) --
    invalid = run_doc("NOT_A_LIFECYCLE_STATE", "v2", "worker-a")
    with pytest.raises(SchemaValidationError):
        backend.write("run", run_id, invalid)
    # The last valid object stays intact on disk; nothing was persisted.
    assert backend.read("run", run_id) == v2
    assert validate_object("run", backend.read("run", run_id)) == []
    assert backend.list_ids("run") == [run_id]

    # -- delete --
    backend.delete("run", run_id)
    assert not backend.exists("run", run_id)
    records = log.list_events()
    assert [r.event.event_type for r in records] == ["run.created", "run.updated"]
    assert [r.sequence for r in records] == [1, 2]


# ---------------------------------------------------------------------------
# AC-02: whole model registry round-trips with an idempotent event per type
# ---------------------------------------------------------------------------


def test_all_model_types_lifecycle_with_events(tmp_path) -> None:
    """AC-02: every normative object type round-trips write/read/delete
    and stays schema-valid at every step, with an idempotent event per
    type on the same base dir.

    ``event`` itself is deliberately excluded: event records live under
    the log's append-only discipline (records carry the log-managed
    ``sequence``; a raw backend write of a plain event document would be
    a corrupt log record, which tests/state/test_events.py covers).
    """
    base = tmp_path / "state"
    backend = FilesystemStateBackend(base)
    log = ev.ProjectEventLog(base)

    types = sorted(t for t in VALID_DOCS if t != "event")
    for n, obj_type in enumerate(types, start=1):
        doc = copy.deepcopy(VALID_DOCS[obj_type])
        object_id = str(doc[_id_field(obj_type)])
        backend.write(obj_type, object_id, doc)
        stored = backend.read(obj_type, object_id)
        assert stored == doc
        assert validate_object(obj_type, stored) == []
        # One event per type, idempotent on re-submission.
        record = log.append(
            _lifecycle_event(
                n, "state.object.created", object_id, payload={"obj_type": obj_type}
            ),
            idempotency_key=f"created-{obj_type}",
        )
        assert record.sequence == n
        assert record.replayed is False
        replay = log.append(
            _lifecycle_event(
                n, "state.object.created", object_id, payload={"obj_type": obj_type}
            ),
            idempotency_key=f"created-{obj_type}",
        )
        assert replay.replayed is True
        assert replay.sequence == n
        backend.delete(obj_type, object_id)
        assert not backend.exists(obj_type, object_id)
        assert backend.list_ids(obj_type) == []

    # The log preserved one record per type in deterministic order, and
    # every persisted event record is schema-valid.
    records = log.list_events()
    assert len(records) == len(types)
    assert [r.sequence for r in records] == list(range(1, len(types) + 1))
    assert all(not r.replayed for r in records)
    for event_id in backend.list_ids("event"):
        assert validate_object("event", backend.read("event", event_id)) == []
    # The sweep deleted every object again.
    assert backend.list_ids("run") == []
    assert backend.list_ids("plan") == []
