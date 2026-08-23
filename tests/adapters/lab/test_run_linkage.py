"""Dispatch-to-Run record linkage tests (DEV-M7-G01).

``link_run_to_dispatch`` is the caller-owned linkage of the LabAdapter
contract (15-ADAPTER-SPEC.md SS2 "Run record linkage"): after a
successful ``dispatch`` the dispatching orchestrator records
``run.external.dispatch_id`` / ``run.external.backend`` on the durable
Run record and advances the Run to ``RUNNING_EXTERNAL`` through the
real transition machinery -- the adapter itself never touches the Run
record, so a forgotten linkage (the drift of the issue: runs left in a
pre-external state with no dispatch id while results come back) is a
caller error that this helper makes impossible to commit silently.

The tests prove that linking

* advances a ``READY`` run through the mainline (``READY -> DISPATCHED
  -> RUNNING_EXTERNAL``) and records the dispatch identity on the
  durable record through the real schema-validating backend,
* repairs a run left at ``DISPATCHED`` (the stale pre-external state of
  the issue: results can never be reconciled onto it),
* re-links an already-``RUNNING_EXTERNAL`` run idempotently (the
  recovery discipline: re-issuing the linkage after a crash is a
  no-op),
* refuses a run already linked to a *different* dispatch (never
  silently re-linked),
* refuses lifecycle states that cannot carry the dispatch through the
  real transition machinery (``IllegalTransitionError``, nothing
  persisted),
* preserves unrelated external fields (``job_id``, ``working_directory``),
* is deterministic under the injected clock,
* audits every arc actually performed as one deterministic
  ``run.lifecycle_change`` event in the project event log (the
  external-dispatch phase is structurally visible), and
* composes with the real ``dispatch``: dispatch -> link leaves the run
  addressable by a fresh adapter instance and by the Monitor's
  watch-entry invariant.

Every test runs against injected ``tmp_path`` directories only: no
wall clock (fixed stamps), no network, no path outside the test's own
tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scientific_reproduction.adapters.lab.base import (
    DispatchRecord,
    LabAdapterDataError,
)
from scientific_reproduction.adapters.lab.filesystem import (
    DISPATCH_RECORD_FILENAME,
    FilesystemLabAdapter,
)
from scientific_reproduction.adapters.lab.linkage import (
    DISPATCH_LINKAGE_REASON,
    FILESYSTEM_BACKEND_NAME,
    LINKAGE_ACTOR,
    RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
    link_run_to_dispatch,
)
from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.rules.lifecycle import IllegalTransitionError
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring.registry import validate_external_identity
from tests.adapters.lab.lab_helpers import (
    GOAL_ID,
    PACKAGE_ID,
    PROJECT_ID,
    RUN_ID,
    make_package,
)

FIXED_STAMP = "2026-08-14T00:00:00+00:00"

DISPATCH_ID = generate_id("dispatch", PACKAGE_ID, RUN_ID)
OTHER_DISPATCH_ID = generate_id("dispatch", PACKAGE_ID, RUN_ID, "other")

WORKER_SESSION = "session-experiment-worker-1"


class FakeClock:
    """Injectable clock: the single fixed stamp repeats forever -- no
    wall clock anywhere in the tested path."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp

    def __call__(self) -> str:
        return self._stamp


def make_dispatch(
    *,
    dispatch_id: str = DISPATCH_ID,
    run_id: str = RUN_ID,
    package_id: str = PACKAGE_ID,
) -> DispatchRecord:
    """A DispatchRecord as returned by a successful dispatch (AC-01)."""
    return DispatchRecord(
        dispatch_id=dispatch_id,
        package_id=package_id,
        project_id=PROJECT_ID,
        goal_id=GOAL_ID,
        run_id=run_id,
        outgoing_path=f"/lab/outgoing/{run_id}",
        dispatched_at=FIXED_STAMP,
    )


def make_run(
    lifecycle_state: LifecycleState,
    *,
    external: RunExternal | None = None,
    run_id: str = RUN_ID,
) -> Run:
    """A deterministic durable Run record at the given lifecycle state."""
    return Run(
        run_id=run_id,
        goal_id=GOAL_ID,
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=lifecycle_state,
        goal_version="1.0",
        scientific_review=None,
        worker_session_ref=WORKER_SESSION,
        external=external,
        artifacts=["sop.pdf"],
        deviations=[],
        engineering_retries=[],
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def write_run(run_store: FilesystemStateBackend, run: Run) -> None:
    """Persist a run through the real schema-validating backend."""
    run_store.write("run", run.run_id, run.to_dict())


def read_run(run_store: FilesystemStateBackend, run_id: str = RUN_ID) -> Run:
    """Re-hydrate one persisted Run record."""
    return Run.from_dict(run_store.read("run", run_id))


def run_bytes(run_store: FilesystemStateBackend, run_id: str = RUN_ID) -> bytes:
    """The persisted bytes of one Run record (idempotence assertions)."""
    path = run_store._type_dir("run") / f"{run_id}.json"  # type: ignore[attr-defined]
    return path.read_bytes()


# ---------------------------------------------------------------------------
# The canonical linkage: READY -> DISPATCHED -> RUNNING_EXTERNAL + identity
# ---------------------------------------------------------------------------


def test_linkage_ready_run_advances_through_mainline_and_records_dispatch(
    tmp_path,
) -> None:
    # A READY run is linked by walking the real mainline
    # (READY -> DISPATCHED -> RUNNING_EXTERNAL): the persisted record
    # carries the dispatch identity and the backend, stamped by the
    # injected clock, with every unrelated field preserved.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))

    updated = link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert updated.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert updated.external == RunExternal(
        backend=FILESYSTEM_BACKEND_NAME, dispatch_id=DISPATCH_ID
    )
    assert updated.updated_at == FIXED_STAMP
    assert updated.worker_session_ref == WORKER_SESSION
    assert updated.artifacts == ["sop.pdf"]

    persisted = read_run(store)
    assert persisted == updated
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert persisted.external is not None
    assert persisted.external.dispatch_id == DISPATCH_ID
    assert persisted.external.backend == FILESYSTEM_BACKEND_NAME


def test_linkage_dispatched_run_repairs_stale_pre_external_state(tmp_path) -> None:
    # The drift of the issue: a run left at DISPATCHED with no dispatch
    # id can never be completed by the Monitor (DISPATCHED is
    # pre-external). Linking it advances DISPATCHED -> RUNNING_EXTERNAL
    # and records the identity -- the repair a fresh orchestrator can
    # apply over the same durable state.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.DISPATCHED))

    updated = link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert updated.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert updated.external == RunExternal(
        backend=FILESYSTEM_BACKEND_NAME, dispatch_id=DISPATCH_ID
    )
    assert read_run(store).lifecycle_state is LifecycleState.RUNNING_EXTERNAL


def test_linkage_external_run_relink_is_idempotent_no_op(tmp_path) -> None:
    # The recovery discipline: re-issuing the linkage for a run that is
    # already RUNNING_EXTERNAL under the same dispatch id never changes
    # the durable record -- the first re-link normalizes the re-hydrated
    # record (the omitted ``scientific_review`` default is persisted,
    # the same re-hydrate-and-rewrite pattern as the Monitor's
    # transitions), and every re-link of the linked record is
    # byte-identical.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(
        store,
        make_run(
            LifecycleState.RUNNING_EXTERNAL,
            external=RunExternal(
                backend=FILESYSTEM_BACKEND_NAME, dispatch_id=DISPATCH_ID
            ),
        ),
    )
    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())
    before = run_bytes(store)

    updated = link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert updated.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert updated.external is not None
    assert updated.external.dispatch_id == DISPATCH_ID
    assert run_bytes(store) == before


# ---------------------------------------------------------------------------
# Refusals: never silently re-linked, never fabricated onto a run
# ---------------------------------------------------------------------------


def test_linkage_refuses_run_already_linked_to_other_dispatch(tmp_path) -> None:
    # A run whose external identity already names a DIFFERENT dispatch
    # is refused loudly and nothing is persisted -- a run is never
    # silently re-linked (the "never silently matched" discipline of
    # the handoff layer).
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(
        store,
        make_run(
            LifecycleState.RUNNING_EXTERNAL,
            external=RunExternal(
                backend=FILESYSTEM_BACKEND_NAME, dispatch_id=OTHER_DISPATCH_ID
            ),
        ),
    )
    before = run_bytes(store)

    with pytest.raises(LabAdapterDataError) as exc:
        link_run_to_dispatch(store, make_dispatch(), now=FakeClock())
    assert "already linked to dispatch" in str(exc.value)
    assert OTHER_DISPATCH_ID in str(exc.value)
    assert run_bytes(store) == before


def test_linkage_refuses_lifecycle_states_that_cannot_carry_dispatch(
    tmp_path,
) -> None:
    # The real transition machinery refuses a dispatch onto any state
    # that cannot carry it (CREATED is before the mainline walk;
    # result-bearing and terminal states can never be re-linked) --
    # loudly, with nothing persisted.
    store = FilesystemStateBackend(tmp_path / "runs")
    for state in (
        LifecycleState.CREATED,
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
        LifecycleState.CANCELLED,
        LifecycleState.INVALIDATED,
    ):
        run_id = generate_id("run", "linkage-refuse", state.value)
        write_run(store, make_run(state, run_id=run_id))
        with pytest.raises(IllegalTransitionError):
            link_run_to_dispatch(store, make_dispatch(run_id=run_id), now=FakeClock())
        leftover = read_run(store, run_id)
        assert leftover.lifecycle_state is state
        assert leftover.external is None


def test_linkage_preserves_unrelated_external_fields(tmp_path) -> None:
    # A run that already names a job_id / working directory (e.g. a
    # compute-side identity) keeps those fields; the linkage only adds
    # the dispatch identity and the backend.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(
        store,
        make_run(
            LifecycleState.RUNNING_EXTERNAL,
            external=RunExternal(job_id="job-1", working_directory="/scratch/w1"),
        ),
    )

    updated = link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert updated.external == RunExternal(
        backend=FILESYSTEM_BACKEND_NAME,
        job_id="job-1",
        working_directory="/scratch/w1",
        dispatch_id=DISPATCH_ID,
    )
    assert updated.lifecycle_state is LifecycleState.RUNNING_EXTERNAL


def test_linkage_backend_is_injected(tmp_path) -> None:
    # The recorded backend comes from the caller (a future lab adapter
    # names its own backend); the v0.1 default is the filesystem adapter
    # identity.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))

    updated = link_run_to_dispatch(
        store, make_dispatch(), backend="elab", now=FakeClock()
    )

    assert updated.external is not None
    assert updated.external.backend == "elab"
    assert updated.external.dispatch_id == DISPATCH_ID


# ---------------------------------------------------------------------------
# Boundaries and determinism
# ---------------------------------------------------------------------------


def test_linkage_type_and_data_boundaries(tmp_path) -> None:
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))
    with pytest.raises(TypeError):
        link_run_to_dispatch("not a store", make_dispatch())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        link_run_to_dispatch(store, {"dispatch_id": "x"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        link_run_to_dispatch(store, make_dispatch(), now=7)  # type: ignore[arg-type]
    with pytest.raises(LabAdapterDataError):
        link_run_to_dispatch(store, make_dispatch(), backend="")
    with pytest.raises(LabAdapterDataError):
        link_run_to_dispatch(store, make_dispatch(), backend=7)  # type: ignore[arg-type]


def test_linkage_missing_run_record_raises(tmp_path) -> None:
    # A dispatch whose Run record does not exist fails loudly (the store
    # contract: FileNotFoundError) -- the linkage is never silently
    # skipped onto nothing.
    store = FilesystemStateBackend(tmp_path / "runs")
    with pytest.raises(FileNotFoundError):
        link_run_to_dispatch(store, make_dispatch(), now=FakeClock())


def test_linkage_deterministic_identical_bytes(tmp_path) -> None:
    # Identical inputs -> identical outputs: two independent stores
    # linked under the fixed clock produce byte-identical Run records.
    first = FilesystemStateBackend(tmp_path / "a")
    second = FilesystemStateBackend(tmp_path / "b")
    for store in (first, second):
        write_run(store, make_run(LifecycleState.READY))
        link_run_to_dispatch(store, make_dispatch(), now=FakeClock())
    assert run_bytes(first) == run_bytes(second)


# ---------------------------------------------------------------------------
# Composition with the real dispatch (the orchestrated flow)
# ---------------------------------------------------------------------------


def test_linkage_composes_with_real_dispatch_and_is_addressable(tmp_path) -> None:
    # The orchestrated flow end to end: dispatch through the real
    # adapter, then link. The persisted run record ends at
    # RUNNING_EXTERNAL under the real schema gate, carries the
    # deterministic dispatch id, satisfies the Monitor's watch-entry
    # identity invariant, and a FRESH adapter instance over the same
    # handoff addresses the dispatch from the recorded id alone (AC-01).
    base = tmp_path / "lab"
    store = FilesystemStateBackend(tmp_path / "runs")
    adapter = FilesystemLabAdapter(base)
    write_run(store, make_run(LifecycleState.READY))

    record = adapter.dispatch(make_package(), dispatched_at=FIXED_STAMP)
    assert record.dispatch_id == DISPATCH_ID
    updated = link_run_to_dispatch(store, record, now=FakeClock())

    assert updated.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert updated.external == RunExternal(
        backend=FILESYSTEM_BACKEND_NAME, dispatch_id=DISPATCH_ID
    )
    assert (base / "outgoing" / RUN_ID / DISPATCH_RECORD_FILENAME).is_file()
    # The linked identity satisfies the Monitor's external-identity
    # invariant (backend + at least one external id), so the run can be
    # watched and reconciled by its dispatch.
    validate_external_identity(
        RunExternal(backend=FILESYSTEM_BACKEND_NAME, dispatch_id=DISPATCH_ID)
    )

    fresh = FilesystemLabAdapter(base)
    status = fresh.status(record.dispatch_id)
    assert status.run_id == RUN_ID
    assert status.state.value == "RUNNING_EXTERNAL"


def test_linkage_persisted_record_is_schema_valid(tmp_path) -> None:
    # The linked Run record round-trips the real run schema (the
    # backend's write gate accepted it; the re-read proves the durable
    # record stays schema-valid).
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))
    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    raw = store.read("run", RUN_ID)
    json.dumps(raw)  # plain serializable dict
    assert raw["lifecycle_state"] == LifecycleState.RUNNING_EXTERNAL.value
    assert raw["external"] == {
        "backend": FILESYSTEM_BACKEND_NAME,
        "dispatch_id": DISPATCH_ID,
    }
    assert raw["worker_session_ref"] == WORKER_SESSION


# ---------------------------------------------------------------------------
# Lifecycle audit events (the dispatch phase is visible in the event log)
# ---------------------------------------------------------------------------


def flow_events(run_store: FilesystemStateBackend) -> list[EventRecord]:
    """Event records of the default audit log bound over the run store's
    base dir (the canonical workspace ``events/`` tree)."""
    return ProjectEventLog(run_store.base_dir).list_events()


def lifecycle_event_id(
    run_id: str, from_state: LifecycleState, to_state: LifecycleState
) -> str:
    """The deterministic event id of one lifecycle arc (the transition
    vocabulary pattern: a pure function of run id, from, to)."""
    return generate_id(
        "event",
        RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
        run_id,
        from_state.value,
        to_state.value,
    )


def tree_bytes(directory: Path) -> dict[str, bytes]:
    """The persisted bytes of every file under ``directory``, keyed by
    its relative path (determinism assertions)."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_linkage_ready_run_audits_both_arcs_as_lifecycle_events(tmp_path) -> None:
    # A run starting at READY walks both mainline arcs, and each arc is
    # audited as one deterministic ``run.lifecycle_change`` event in the
    # default log (the canonical workspace ``events/`` tree over the run
    # store's base dir): the external-dispatch phase is structurally
    # visible in the audit trail -- the drift of the issue.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))

    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    records = flow_events(store)
    assert [record.event.event_id for record in records] == [
        lifecycle_event_id(
            RUN_ID, LifecycleState.READY, LifecycleState.DISPATCHED
        ),
        lifecycle_event_id(
            RUN_ID, LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL
        ),
    ]
    for record, from_state, to_state in zip(
        records,
        (LifecycleState.READY, LifecycleState.DISPATCHED),
        (
            LifecycleState.DISPATCHED,
            LifecycleState.RUNNING_EXTERNAL,
        ),
    ):
        event = record.event
        assert event.event_type == RUN_LIFECYCLE_CHANGE_EVENT_TYPE
        assert event.object_id == RUN_ID
        assert event.run_id == RUN_ID
        assert event.from_ == from_state.value
        assert event.to == to_state.value
        assert event.actor == LINKAGE_ACTOR
        assert event.reason == DISPATCH_LINKAGE_REASON
        assert event.timestamp == FIXED_STAMP
    # A fresh log instance over the same base dir reads the same records
    # (the durable audit trail the reporting subsystem consumes).
    fresh = ProjectEventLog(store.base_dir)
    assert [r.event.event_id for r in fresh.list_events()] == [
        r.event.event_id for r in records
    ]


def test_linkage_dispatched_run_audits_the_single_arc(tmp_path) -> None:
    # A stale DISPATCHED run performs exactly one arc, and exactly one
    # lifecycle event is appended -- never a fabricated READY arc.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.DISPATCHED))

    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    records = flow_events(store)
    assert len(records) == 1
    event = records[0].event
    assert event.event_id == lifecycle_event_id(
        RUN_ID, LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL
    )
    assert event.from_ == LifecycleState.DISPATCHED.value
    assert event.to == LifecycleState.RUNNING_EXTERNAL.value


def test_linkage_external_run_relink_appends_no_events(tmp_path) -> None:
    # The idempotent re-link performs no arc, so it audits nothing:
    # re-issuing the linkage for an already-linked run never appends a
    # duplicate lifecycle event.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(
        store,
        make_run(
            LifecycleState.RUNNING_EXTERNAL,
            external=RunExternal(
                backend=FILESYSTEM_BACKEND_NAME, dispatch_id=DISPATCH_ID
            ),
        ),
    )
    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())
    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert flow_events(store) == []


def test_linkage_relink_after_successful_linkage_appends_no_duplicates(
    tmp_path,
) -> None:
    # The recovery discipline end to end: a completed linkage followed
    # by a crash-window re-link leaves the audit trail untouched -- the
    # same two original records, no duplicates.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))

    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())
    before = [(r.event.event_id, r.sequence) for r in flow_events(store)]
    assert len(before) == 2

    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert [(r.event.event_id, r.sequence) for r in flow_events(store)] == before


def test_linkage_crash_between_event_append_and_record_write_converges(
    tmp_path,
) -> None:
    # The events are appended BEFORE the record write, so a crash after
    # the appends leaves the record stale while the events survive. The
    # re-link re-performs the same arcs and re-appends the same events
    # under the same idempotency keys: the log converges to the single
    # original records (exactly-once) and the record converges.
    store = FilesystemStateBackend(tmp_path / "runs")
    write_run(store, make_run(LifecycleState.READY))
    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())
    before = [r.event.event_id for r in flow_events(store)]

    # Simulate the crash: the record write never landed -- the record
    # is back at its pre-linkage state while the events stay.
    write_run(store, make_run(LifecycleState.READY))
    link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert read_run(store).lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert [r.event.event_id for r in flow_events(store)] == before


def test_linkage_event_log_is_injected(tmp_path) -> None:
    # The injected event log wins: the events land wherever the caller
    # bound the log, never under the run store's base dir.
    store = FilesystemStateBackend(tmp_path / "runs")
    log = ProjectEventLog(tmp_path / "audit")
    write_run(store, make_run(LifecycleState.READY))

    link_run_to_dispatch(
        store, make_dispatch(), now=FakeClock(), event_log=log
    )

    assert len(log.list_events()) == 2
    assert flow_events(store) == []


def test_linkage_refusals_append_no_events(tmp_path) -> None:
    # Refused linkages persist nothing -- and audit nothing: no event
    # may claim a lifecycle advance that never happened.
    other = FilesystemStateBackend(tmp_path / "other")
    write_run(
        other,
        make_run(
            LifecycleState.RUNNING_EXTERNAL,
            external=RunExternal(
                backend=FILESYSTEM_BACKEND_NAME, dispatch_id=OTHER_DISPATCH_ID
            ),
        ),
    )
    with pytest.raises(LabAdapterDataError):
        link_run_to_dispatch(other, make_dispatch(), now=FakeClock())

    closed = FilesystemStateBackend(tmp_path / "closed")
    write_run(closed, make_run(LifecycleState.CLOSED))
    with pytest.raises(IllegalTransitionError):
        link_run_to_dispatch(closed, make_dispatch(), now=FakeClock())

    assert flow_events(other) == []
    assert flow_events(closed) == []


def test_linkage_audit_trail_deterministic_identical_bytes(tmp_path) -> None:
    # Identical inputs -> identical audit trails: two independent stores
    # linked under the fixed clock produce byte-identical event trees
    # (records, sequence counter, and idempotency claims).
    first = FilesystemStateBackend(tmp_path / "a")
    second = FilesystemStateBackend(tmp_path / "b")
    for store in (first, second):
        write_run(store, make_run(LifecycleState.READY))
        link_run_to_dispatch(store, make_dispatch(), now=FakeClock())

    assert tree_bytes(first.base_dir / "events") == tree_bytes(
        second.base_dir / "events"
    )
