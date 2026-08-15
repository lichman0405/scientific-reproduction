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
* is deterministic under the injected clock, and
* composes with the real ``dispatch``: dispatch -> link leaves the run
  addressable by a fresh adapter instance and by the Monitor's
  watch-entry invariant.

Every test runs against injected ``tmp_path`` directories only: no
wall clock (fixed stamps), no network, no path outside the test's own
tree.
"""

from __future__ import annotations

import json

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
    FILESYSTEM_BACKEND_NAME,
    link_run_to_dispatch,
)
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
