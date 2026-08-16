"""Tests for the worker/monitor run authoring helpers (issue #92).

The helpers (``workers/run_helpers.py``) give the worker and monitor
roles the official authoring facade over the run primitives: Run record
writes at ``runs/<run_id>.json`` and lifecycle moves through the
normative Run rule table, with deterministic event ids and idempotency
keys (the monitoring pattern) instead of hand-rolled plumbing. Coverage
maps to the issue's observed hand-rolled layer:

* run writes -- ``test_register_run_*``: canonical-JSON records at
  ``runs/<id>.json`` (the exact directory the audit package reads),
  exactly-once with crash-window convergence;
* run transitions -- ``test_transition_run_*``: the full mainline chain
  plus the ``CANCELLED`` / ``INVALIDATED`` arcs, each move persisted
  (``lifecycle_state`` advanced, ``updated_at`` stamped with the
  injected ``at``) and audited with one ``run.lifecycle_change`` event
  carrying ``from``/``to``/``reason``; illegal pairs and no-op
  transitions rejected (they never enter the audit record); crash-window
  convergence only along the unique normative arc (states with several
  legal predecessors cannot reconstruct the interrupted move);
* reads -- ``test_reads_*``: typed reads and sorted listings with
  stable not-found and corrupt-record errors.

The deterministic path follows the house suites: every fixture pins the
identity/timestamp of ``initialize_project``, and all timestamps and
actors are injected (no wall clock anywhere).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.models import LifecycleState, Run, RunType
from scientific_reproduction.core.rules.lifecycle import IllegalTransitionError
from scientific_reproduction.planning.init import (
    INIT_EVENT_TYPE,
    ProjectNotInitializedError,
    initialize_project,
)
from scientific_reproduction.workers.run_helpers import (
    RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
    RUN_PREDECESSOR_STATE,
    RUN_RECORDED_EVENT_TYPE,
    RUNS_STATE_DIR,
    DuplicateRunError,
    RunNotFoundError,
    RunRegistryError,
    list_runs,
    read_run,
    register_run,
    transition_run,
)

# ---------------------------------------------------------------------------
# Fixtures (deterministic: pinned identity/timestamp, injected stamps)
# ---------------------------------------------------------------------------

#: Deterministic author/committer identity for the initialized project.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects.
DOI = "10.1039/D5TA00771B"

#: Injected actor and recording stamps (no wall clock anywhere).
ACTOR = "worker"
RECORDED_AT = "2026-01-02T00:00:00Z"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def event_log(root: Path) -> ProjectEventLog:
    """The workspace event log bound to the ``events/`` directory."""
    return ProjectEventLog(root)


def run_flow_events(root: Path) -> list[EventRecord]:
    """Event records of the run authoring flow, excluding the
    deterministic ``project.initialized`` event ``initialize_project``
    appends to the same canonical log (``events/``)."""
    return [
        record
        for record in event_log(root).list_events()
        if record.event.event_type != INIT_EVENT_TYPE
    ]


def make_run(
    run_id: str = "RUN-1",
    *,
    lifecycle_state: LifecycleState = LifecycleState.CREATED,
) -> Run:
    """Build a schema-valid run record with compact defaults."""
    return Run(
        run_id=run_id,
        goal_id="GOAL-1",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=lifecycle_state,
        goal_version="v1",
        created_at=TIMESTAMP.isoformat(),
    )


# ---------------------------------------------------------------------------
# register_run
# ---------------------------------------------------------------------------


def test_register_run_persists_canonical_record_and_audits(tmp_path):
    root = init_project(tmp_path)
    registration = register_run(
        root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT
    )
    assert registration.run == make_run()
    assert registration.replayed is False
    # The default call audits through the workspace-bound log.
    record = registration.event_record
    assert record is not None and record.replayed is False
    assert record.event.event_type == RUN_RECORDED_EVENT_TYPE
    assert event_log(root).get(record.event.event_id) is not None
    stored = json.loads(
        (root / RUNS_STATE_DIR / "RUN-1.json").read_text(encoding="utf-8")
    )
    assert stored["run_id"] == "RUN-1"
    assert stored["lifecycle_state"] == "CREATED"
    assert list_runs(root) == (make_run(),)


def test_register_run_with_event_log_audits_recorded_event(tmp_path):
    root = init_project(tmp_path)
    registration = register_run(
        root,
        make_run(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=event_log(root),
    )
    record = registration.event_record
    assert record is not None and record.replayed is False
    event = record.event
    assert event.event_type == RUN_RECORDED_EVENT_TYPE
    assert event.object_id == "RUN-1"
    assert event.actor == ACTOR
    assert event.timestamp == RECORDED_AT
    assert event_log(root).get(event.event_id) is not None


def test_register_run_duplicate_rejected_and_bytes_untouched(tmp_path):
    root = init_project(tmp_path)
    register_run(root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)
    path = root / RUNS_STATE_DIR / "RUN-1.json"
    original = path.read_text(encoding="utf-8")
    with pytest.raises(DuplicateRunError, match="already registered"):
        register_run(
            root,
            make_run(lifecycle_state=LifecycleState.CLOSED),
            actor=ACTOR,
            recorded_at=RECORDED_AT,
        )
    assert path.read_text(encoding="utf-8") == original


def test_register_run_crash_window_converges_exactly_once(tmp_path):
    root = init_project(tmp_path)
    log = event_log(root)
    # First call: the record write lands, the event append does not (a
    # crash between the two steps). Simulated by writing the record
    # through the raw state backend, exactly the hand-rolled layer's
    # interruption point.
    (root / RUNS_STATE_DIR / "RUN-1.json").parent.mkdir(
        parents=True, exist_ok=True
    )
    (root / RUNS_STATE_DIR / "RUN-1.json").write_text(
        json.dumps(make_run().to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    registration = register_run(
        root,
        make_run(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=log,
    )
    assert registration.replayed is True
    assert registration.event_record is not None
    assert registration.event_record.replayed is False
    # A third call finds record and event both present: a true duplicate.
    with pytest.raises(DuplicateRunError, match="already registered"):
        register_run(
            root,
            make_run(),
            actor=ACTOR,
            recorded_at=RECORDED_AT,
            event_log=log,
        )
    assert len(run_flow_events(root)) == 1


def test_default_event_log_is_workspace_bound_and_ordered(tmp_path):
    root = init_project(tmp_path)
    # No explicit event log anywhere: every call audits through the
    # workspace-bound log (records at events/) in append order.
    register_run(root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)
    transition_run(
        root, "RUN-1", LifecycleState.READY,
        actor=ACTOR, reason="run queued", at="2026-01-03T00:00:00Z",
    )
    records = run_flow_events(root)
    assert [record.event.event_type for record in records] == [
        RUN_RECORDED_EVENT_TYPE,
        RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
    ]
    assert [record.event.object_id for record in records] == ["RUN-1", "RUN-1"]
    # The append-only order invariants hold over the shared canonical
    # log: strictly increasing, unique sequence numbers, every event
    # readable back from the workspace log.
    sequences = [record.sequence for record in records]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    assert all(
        event_log(root).get(record.event.event_id) is not None for record in records
    )


def test_explicit_event_log_overrides_workspace_default(tmp_path):
    root = init_project(tmp_path)
    other = init_project(tmp_path / "other")
    registration = register_run(
        root,
        make_run(),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
        event_log=event_log(other),
    )
    record = registration.event_record
    assert record is not None
    assert record.event.event_type == RUN_RECORDED_EVENT_TYPE
    # The event went to the explicitly given log, never the workspace one.
    assert run_flow_events(root) == []
    assert event_log(other).get(record.event.event_id) is not None
    assert len(run_flow_events(other)) == 1


def test_register_run_requires_initialized_project(tmp_path):
    with pytest.raises(ProjectNotInitializedError):
        register_run(tmp_path, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)


def test_register_run_type_errors(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        register_run(root, "RUN-1", actor=ACTOR, recorded_at=RECORDED_AT)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        register_run(root, make_run(), actor=1, recorded_at=RECORDED_AT)  # type: ignore[arg-type]
    with pytest.raises(RunRegistryError):
        register_run(root, make_run(), actor="", recorded_at=RECORDED_AT)


# ---------------------------------------------------------------------------
# transition_run
# ---------------------------------------------------------------------------


def test_transition_run_full_mainline_chain(tmp_path):
    root = init_project(tmp_path)
    log = event_log(root)
    register_run(
        root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT, event_log=log
    )
    chain = [
        (LifecycleState.READY, "run queued"),
        (LifecycleState.DISPATCHED, "run dispatched to worker"),
        (LifecycleState.RUNNING_EXTERNAL, "computation started"),
        (LifecycleState.RESULT_AVAILABLE, "raw result produced"),
        (LifecycleState.ANALYZING, "analysis started"),
        (LifecycleState.SUBMITTED_FOR_REVIEW, "result submitted for review"),
        (LifecycleState.CLOSED, "review passed"),
    ]
    stamps = [
        "2026-01-03T00:00:00Z",
        "2026-01-04T00:00:00Z",
        "2026-01-05T00:00:00Z",
        "2026-01-06T00:00:00Z",
        "2026-01-07T00:00:00Z",
        "2026-01-08T00:00:00Z",
        "2026-01-09T00:00:00Z",
    ]
    previous = LifecycleState.CREATED
    for (to_state, reason), at in zip(chain, stamps):
        transition = transition_run(
            root, "RUN-1", to_state,
            actor=ACTOR, reason=reason, at=at,
            event_log=log,
        )
        assert transition.previous_state is previous
        assert transition.replayed is False
        assert transition.run.lifecycle_state is to_state
        assert transition.run.updated_at == at
        record = transition.event_record
        assert record is not None
        event = record.event
        assert event.event_type == RUN_LIFECYCLE_CHANGE_EVENT_TYPE
        assert event.from_ == previous.value
        assert event.to == to_state.value
        assert event.reason == reason
        assert event.actor == ACTOR
        assert event.timestamp == at
        previous = to_state
    assert read_run(root, "RUN-1").lifecycle_state is LifecycleState.CLOSED
    assert len(run_flow_events(root)) == 8  # recorded + seven transitions


def test_transition_run_cancel_and_invalidate_arcs(tmp_path):
    root = init_project(tmp_path)
    register_run(root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)
    cancelled = transition_run(
        root, "RUN-1", LifecycleState.CANCELLED,
        actor=ACTOR, reason="hardware failure", at="2026-01-03T00:00:00Z",
        event_log=event_log(root),
    )
    assert cancelled.run.lifecycle_state is LifecycleState.CANCELLED
    cancelled_record = cancelled.event_record
    assert cancelled_record is not None
    assert cancelled_record.event.from_ == "CREATED"
    assert cancelled_record.event.to == "CANCELLED"
    # INVALIDATED is legal from any result-bearing state: start another
    # run and invalidate from RESULT_AVAILABLE.
    register_run(
        root, make_run("RUN-2"), actor=ACTOR, recorded_at=RECORDED_AT
    )
    for to_state, reason in (
        (LifecycleState.READY, "run queued"),
        (LifecycleState.DISPATCHED, "dispatched"),
        (LifecycleState.RUNNING_EXTERNAL, "started"),
        (LifecycleState.RESULT_AVAILABLE, "result produced"),
    ):
        transition_run(
            root, "RUN-2", to_state,
            actor=ACTOR, reason=reason, at="2026-01-04T00:00:00Z",
            event_log=event_log(root),
        )
    invalidated = transition_run(
        root, "RUN-2", LifecycleState.INVALIDATED,
        actor=ACTOR, reason="provenance broken", at="2026-01-05T00:00:00Z",
        event_log=event_log(root),
    )
    assert invalidated.run.lifecycle_state is LifecycleState.INVALIDATED
    invalidated_record = invalidated.event_record
    assert invalidated_record is not None
    assert invalidated_record.event.from_ == "RESULT_AVAILABLE"
    assert invalidated_record.event.to == "INVALIDATED"


def test_transition_run_rejects_illegal_pairs(tmp_path):
    root = init_project(tmp_path)
    register_run(root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)
    # CREATED -> RESULT_AVAILABLE skips the chain and is not in the table.
    with pytest.raises(IllegalTransitionError, match="CREATED.*RESULT_AVAILABLE"):
        transition_run(
            root, "RUN-1", LifecycleState.RESULT_AVAILABLE,
            actor=ACTOR, reason="skip", at="2026-01-03T00:00:00Z",
        )
    # A no-op (CREATED -> CREATED) is never legal and never enters the
    # audit record (default or explicit log).
    with pytest.raises(IllegalTransitionError, match="CREATED.*CREATED"):
        transition_run(
            root, "RUN-1", LifecycleState.CREATED,
            actor=ACTOR, reason="noop", at="2026-01-03T00:00:00Z",
        )
    with pytest.raises(IllegalTransitionError, match="CREATED.*CREATED"):
        transition_run(
            root, "RUN-1", LifecycleState.CREATED,
            actor=ACTOR, reason="noop", at="2026-01-03T00:00:00Z",
            event_log=event_log(root),
        )
    assert [r.event.event_type for r in run_flow_events(root)] == [
        RUN_RECORDED_EVENT_TYPE
    ]
    assert read_run(root, "RUN-1").lifecycle_state is LifecycleState.CREATED


def test_transition_run_steady_state_and_crash_window_converge(tmp_path):
    root = init_project(tmp_path)
    log = event_log(root)
    register_run(
        root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT, event_log=log
    )
    first = transition_run(
        root, "RUN-1", LifecycleState.READY,
        actor=ACTOR, reason="run queued", at="2026-01-03T00:00:00Z",
        event_log=log,
    )
    # Steady-state re-submission of the same move resolves to the single
    # original event (replayed): the sequence never advances twice.
    second = transition_run(
        root, "RUN-1", LifecycleState.READY,
        actor=ACTOR, reason="run queued", at="2026-01-05T00:00:00Z",
        event_log=log,
    )
    assert second.replayed is True
    second_record = second.event_record
    first_record = first.event_record
    assert second_record is not None and first_record is not None
    assert second_record.event.event_id == first_record.event.event_id
    assert second_record.sequence == first_record.sequence
    assert len(run_flow_events(root)) == 2  # recorded + transition, nothing new
    assert read_run(root, "RUN-1").lifecycle_state is LifecycleState.READY
    # Crash-window convergence: the record is already at RESULT_AVAILABLE
    # but the deterministic arc RUNNING_EXTERNAL -> RESULT_AVAILABLE was
    # lost (raw writes, no events) -- the unique arc is appended and the
    # call reports replayed.
    other = init_project(tmp_path / "crash")
    register_run(
        other, make_run(), actor=ACTOR, recorded_at=RECORDED_AT,
        event_log=event_log(other),
    )
    path = other / RUNS_STATE_DIR / "RUN-1.json"
    advanced = make_run(lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    path.write_text(
        json.dumps(advanced.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    converged = transition_run(
        other, "RUN-1", LifecycleState.RESULT_AVAILABLE,
        actor=ACTOR, reason="result produced", at="2026-01-06T00:00:00Z",
        event_log=event_log(other),
    )
    assert converged.replayed is True
    converged_record = converged.event_record
    assert converged_record is not None
    assert converged_record.event.from_ == "RUNNING_EXTERNAL"
    assert converged_record.event.to == "RESULT_AVAILABLE"
    assert len(run_flow_events(other)) == 2  # recorded + converged arc


def test_transition_run_multi_predecessor_no_convergence(tmp_path):
    root = init_project(tmp_path)
    register_run(root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)
    path = root / RUNS_STATE_DIR / "RUN-1.json"
    cancelled = make_run(lifecycle_state=LifecycleState.CANCELLED)
    path.write_text(
        json.dumps(cancelled.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    # CANCELLED has several legal predecessors (CREATED, READY,
    # DISPATCHED, RUNNING_EXTERNAL): the interrupted arc cannot be
    # reconstructed, so the no-op guard wins even with an event log.
    with pytest.raises(IllegalTransitionError, match="CANCELLED.*CANCELLED"):
        transition_run(
            root, "RUN-1", LifecycleState.CANCELLED,
            actor=ACTOR, reason="cancel", at="2026-01-03T00:00:00Z",
            event_log=event_log(root),
        )
    assert [r.event.event_type for r in run_flow_events(root)] == [
        RUN_RECORDED_EVENT_TYPE
    ]


def test_transition_run_unknown_run_and_type_errors(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(RunNotFoundError, match="RUN-9"):
        transition_run(
            root, "RUN-9", LifecycleState.READY,
            actor=ACTOR, reason="queued", at=RECORDED_AT,
        )
    register_run(root, make_run(), actor=ACTOR, recorded_at=RECORDED_AT)
    with pytest.raises(TypeError):
        transition_run(
            root, "RUN-1", "READY",  # type: ignore[arg-type]
            actor=ACTOR, reason="queued", at=RECORDED_AT,
        )
    with pytest.raises(RunRegistryError):
        transition_run(
            root, "RUN-1", LifecycleState.READY,
            actor=ACTOR, reason="", at=RECORDED_AT,
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_reads_not_found_and_corrupt_state(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(RunNotFoundError, match="RUN-9"):
        read_run(root, "RUN-9")
    path = root / RUNS_STATE_DIR / "RUN-1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="is corrupt"):
        read_run(root, "RUN-1")


def test_reads_sorted_and_typed(tmp_path):
    root = init_project(tmp_path)
    register_run(
        root, make_run("RUN-2"), actor=ACTOR, recorded_at=RECORDED_AT
    )
    register_run(
        root, make_run("RUN-1"), actor=ACTOR, recorded_at=RECORDED_AT
    )
    assert [r.run_id for r in list_runs(root)] == ["RUN-1", "RUN-2"]
    assert all(isinstance(record, Run) for record in list_runs(root))


# ---------------------------------------------------------------------------
# Contract locks
# ---------------------------------------------------------------------------


def test_unique_predecessor_table_matches_rule_table():
    # Every mainline state is reached through exactly one normative arc
    # (what makes crash-window convergence deterministic); CANCELLED and
    # INVALIDATED have several legal predecessors and are None.
    assert RUN_PREDECESSOR_STATE == {
        LifecycleState.CREATED: None,
        LifecycleState.READY: LifecycleState.CREATED,
        LifecycleState.DISPATCHED: LifecycleState.READY,
        LifecycleState.RUNNING_EXTERNAL: LifecycleState.DISPATCHED,
        LifecycleState.RESULT_AVAILABLE: LifecycleState.RUNNING_EXTERNAL,
        LifecycleState.ANALYZING: LifecycleState.RESULT_AVAILABLE,
        LifecycleState.SUBMITTED_FOR_REVIEW: LifecycleState.ANALYZING,
        LifecycleState.CLOSED: LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CANCELLED: None,
        LifecycleState.INVALIDATED: None,
    }
