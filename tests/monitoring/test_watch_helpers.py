"""Tests for the Monitor's watch authoring helper (issue #154).

The helper (``monitoring/watch_helpers.py``) is the authoring gate with
referential integrity in front of the pure state-dir primitive
``WatchedRunRegistry.watch``: the Run is resolved through the run
registry and its lifecycle must be externally watchable before anything
is persisted, so a watch entry can no longer name a nonexistent run or
a run that was never dispatched (failures that would otherwise surface
only later, per reconciliation pass). Coverage maps to the frozen
acceptance criteria:

* ``test_watch_run_watchable_state_persists_entry`` -- a run at
  ``DISPATCHED`` or ``RUNNING_EXTERNAL`` is watched: the resolved,
  contract-valid entry is persisted through the injected registry;
* ``test_watch_run_nonexistent_run_raises_and_persists_nothing`` -- a
  run id with no registered run raises the run registry's stable
  ``RunNotFoundError`` and persists nothing;
* ``test_watch_run_non_watchable_state_raises_and_persists_nothing`` --
  every other lifecycle state (pre-dispatch ``CREATED``/``READY``,
  later and terminal states) raises the stable
  ``UnwatchableRunStateError`` naming the run id and the offending
  state, and persists nothing;
* ``test_registry_watch_alone_accepts_entries_without_run_resolution``
  -- the documented boundary: ``WatchedRunRegistry.watch`` itself is
  unchanged and still accepts contract-valid entries without reading
  the Run registry; the referential-integrity check lives in the
  authoring helper;
* ``test_watch_run_*`` -- the injected reader, the watch-entry contract
  enforcement, the idempotent re-watch, the TypeError boundaries and
  the error hierarchy.

Determinism: every test injects the fixed ``FIXED_STAMP`` timestamp (no
wall clock anywhere), ``tmp_path`` state directories and
``generate_id`` ids. No randomness, no network, no sleeps anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.monitoring import (
    MonitoringError,
    WatchedRunRecord,
    WatchedRunRegistry,
    WatchRecordError,
)
from scientific_reproduction.monitoring.watch_helpers import (
    WATCHABLE_RUN_STATES,
    UnwatchableRunStateError,
    watch_run,
)
from scientific_reproduction.planning.init import initialize_project
from scientific_reproduction.planning.plan import register_goal
from scientific_reproduction.workers.run_helpers import (
    RunNotFoundError,
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

#: Injected actor and stamps (no wall clock anywhere).
ACTOR = "monitor"
RECORDED_AT = "2026-01-02T00:00:00Z"
FIXED_STAMP = "2026-08-14T00:00:00+00:00"


class FakeClock:
    """Injectable clock: a single fixed stamp repeats forever and every
    read is recorded (mirrors the monitoring suites' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


def make_run_id(index: int = 1) -> str:
    """A deterministic run id (``sr_run_<32 hex>``)."""
    return generate_id("run", f"goal-{index}", f"seq-{index}")


def make_external(run_id: str) -> RunExternal:
    """A slurm-ssh-shaped external identity for ``run_id``."""
    return RunExternal(
        backend="slurm_ssh",
        job_id=generate_id("job", run_id),
        working_directory=f"/home/alice/scratch/{run_id}",
    )


def make_registry(state_dir: Path) -> WatchedRunRegistry:
    """A registry over ``state_dir`` with the fixed clock."""
    return WatchedRunRegistry(state_dir, now=FakeClock())


def make_goal() -> GoalContract:
    """Build the frozen goal contract runs reference (the issue #148
    registration gate: ``goal_id`` resolves to the frozen contract and
    ``goal_version`` equals its formal version)."""
    return GoalContract(
        goal_id="GOAL-1",
        title="Reproduce the reported isotherm.",
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the formally reported isotherm dataset.",
        requirement_ids=["REQ-1"],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=True),
        analysis_protocol_ref="ANP-1",
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version="v1",
        frozen=True,
    )


def init_workspace(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root`` and
    register the frozen goal contract runs reference; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    register_goal(root, make_goal())
    return root


#: The normative walk from ``CREATED`` to each test lifecycle state
#: (``core.rules.lifecycle``: the mainline chain, the pre-result
#: ``CANCELLED`` arc and the result-bearing ``INVALIDATED`` arc).
STATE_WALKS: dict[LifecycleState, tuple[LifecycleState, ...]] = {
    LifecycleState.CREATED: (),
    LifecycleState.READY: (LifecycleState.READY,),
    LifecycleState.DISPATCHED: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
    ),
    LifecycleState.RUNNING_EXTERNAL: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
    ),
    LifecycleState.RESULT_AVAILABLE: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
        LifecycleState.RESULT_AVAILABLE,
    ),
    LifecycleState.ANALYZING: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
    ),
    LifecycleState.SUBMITTED_FOR_REVIEW: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
    ),
    LifecycleState.CLOSED: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.ANALYZING,
        LifecycleState.SUBMITTED_FOR_REVIEW,
        LifecycleState.CLOSED,
    ),
    LifecycleState.CANCELLED: (LifecycleState.CANCELLED,),
    LifecycleState.INVALIDATED: (
        LifecycleState.READY,
        LifecycleState.DISPATCHED,
        LifecycleState.RUNNING_EXTERNAL,
        LifecycleState.RESULT_AVAILABLE,
        LifecycleState.INVALIDATED,
    ),
}

#: Every lifecycle state that cannot be externally watched (the
#: rejection set: everything but ``DISPATCHED`` / ``RUNNING_EXTERNAL``).
NON_WATCHABLE_STATES = tuple(
    state for state in STATE_WALKS if state not in WATCHABLE_RUN_STATES
)


def register_run_in_state(
    root: Path, run_id: str, state: LifecycleState
) -> Run:
    """Register one run at ``CREATED`` and walk it to ``state`` through
    the normative rule table (``register_run`` / ``transition_run``);
    return the persisted record."""
    register_run(
        root,
        Run(
            run_id=run_id,
            goal_id="GOAL-1",
            run_type=RunType.INDEPENDENT_REPLICATE,
            lifecycle_state=LifecycleState.CREATED,
            goal_version="v1",
            created_at=TIMESTAMP.isoformat(),
        ),
        actor=ACTOR,
        recorded_at=RECORDED_AT,
    )
    for target in STATE_WALKS[state]:
        transition_run(
            root,
            run_id,
            target,
            actor=ACTOR,
            reason="test setup",
            at=RECORDED_AT,
        )
    return read_run(root, run_id)


# ---------------------------------------------------------------------------
# The authoring gate: watchable states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL],
)
def test_watch_run_watchable_state_persists_entry(
    tmp_path: Path, state: LifecycleState
) -> None:
    """A dispatched run (``DISPATCHED`` / ``RUNNING_EXTERNAL``) is
    watched: the run resolves, the entry is persisted through the
    injected registry and re-hydrated from disk."""
    root = init_workspace(tmp_path)
    run_id = make_run_id()
    assert register_run_in_state(root, run_id, state).lifecycle_state is state
    external = make_external(run_id)
    registry = make_registry(tmp_path / "monitor")
    record = watch_run(
        root, run_id, external, registry=registry, watched_at=FIXED_STAMP
    )
    assert record.run_id == run_id
    assert record.external == external
    assert record.watched_at == FIXED_STAMP
    assert registry.get(run_id) == record
    assert (registry.watched_dir / f"{run_id}.json").is_file()
    assert registry.list_watched() == (record,)


def test_watch_run_is_idempotent_for_identical_entry(tmp_path: Path) -> None:
    """Re-watching the identical entry through the helper is an
    idempotent no-op (the M8 recovery discipline)."""
    root = init_workspace(tmp_path)
    run_id = make_run_id()
    register_run_in_state(root, run_id, LifecycleState.DISPATCHED)
    external = make_external(run_id)
    registry = make_registry(tmp_path / "monitor")
    first = watch_run(
        root, run_id, external, registry=registry, watched_at=FIXED_STAMP
    )
    second = watch_run(
        root, run_id, external, registry=registry, watched_at=FIXED_STAMP
    )
    assert second == first
    assert registry.list_watched() == (first,)


# ---------------------------------------------------------------------------
# The authoring gate: rejected watch attempts persist nothing
# ---------------------------------------------------------------------------


def test_watch_run_nonexistent_run_raises_and_persists_nothing(
    tmp_path: Path,
) -> None:
    """A run id with no registered run raises the run registry's stable
    ``RunNotFoundError`` (naming the run id) at authoring time and
    persists nothing."""
    root = init_workspace(tmp_path)
    run_id = make_run_id()  # never registered
    registry = make_registry(tmp_path / "monitor")
    with pytest.raises(RunNotFoundError) as exc:
        watch_run(
            root,
            run_id,
            make_external(run_id),
            registry=registry,
            watched_at=FIXED_STAMP,
        )
    assert run_id in str(exc.value)
    assert registry.list_watched() == ()
    assert not registry.watched_dir.exists()


@pytest.mark.parametrize("state", NON_WATCHABLE_STATES)
def test_watch_run_non_watchable_state_raises_and_persists_nothing(
    tmp_path: Path, state: LifecycleState
) -> None:
    """A run whose lifecycle cannot be externally watched (pre-dispatch
    ``CREATED``/``READY``, later and terminal states) is rejected at
    authoring time with the stable ``UnwatchableRunStateError`` naming
    the run id and the offending state, and persists nothing."""
    root = init_workspace(tmp_path)
    run_id = make_run_id()
    assert register_run_in_state(root, run_id, state).lifecycle_state is state
    registry = make_registry(tmp_path / "monitor")
    with pytest.raises(UnwatchableRunStateError) as exc:
        watch_run(
            root,
            run_id,
            make_external(run_id),
            registry=registry,
            watched_at=FIXED_STAMP,
        )
    message = str(exc.value)
    assert run_id in message
    assert state.value in message
    assert registry.list_watched() == ()
    assert not registry.watched_dir.exists()


def test_watch_run_incomplete_external_identity_raises_and_persists_nothing(
    tmp_path: Path,
) -> None:
    """The watch-entry contract is enforced at authoring time: an
    external identity without a backend is refused with the stable
    ``WatchRecordError`` before anything is persisted."""
    root = init_workspace(tmp_path)
    run_id = make_run_id()
    register_run_in_state(root, run_id, LifecycleState.DISPATCHED)
    registry = make_registry(tmp_path / "monitor")
    with pytest.raises(WatchRecordError):
        watch_run(
            root,
            run_id,
            RunExternal(job_id=generate_id("job", run_id)),
            registry=registry,
            watched_at=FIXED_STAMP,
        )
    assert registry.list_watched() == ()
    assert not registry.watched_dir.exists()


# ---------------------------------------------------------------------------
# The injected reader
# ---------------------------------------------------------------------------


def test_watch_run_uses_injected_reader(tmp_path: Path) -> None:
    """An injected run reader replaces the workspace resolution: the
    helper consults it with the run id and watches the returned record
    (the workspace itself is never read)."""
    run_id = make_run_id()
    run = Run(
        run_id=run_id,
        goal_id="GOAL-1",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.DISPATCHED,
        goal_version="v1",
        created_at=FIXED_STAMP,
    )
    asked: list[str] = []

    def reader(asked_id: str) -> Run:
        asked.append(asked_id)
        return run

    registry = make_registry(tmp_path / "monitor")
    record = watch_run(
        tmp_path,  # no project state here: the injected reader replaces it
        run_id,
        make_external(run_id),
        registry=registry,
        watched_at=FIXED_STAMP,
        run_reader=reader,
    )
    assert asked == [run_id]
    assert record.run_id == run_id
    assert registry.get(run_id) == record


def test_watch_run_injected_reader_errors_propagate(tmp_path: Path) -> None:
    """An injected reader's not-found error propagates unchanged and
    nothing is persisted."""
    run_id = make_run_id()

    def reader(asked_id: str) -> Run:
        raise RunNotFoundError(f"no run registered with id {asked_id!r}")

    registry = make_registry(tmp_path / "monitor")
    with pytest.raises(RunNotFoundError) as exc:
        watch_run(
            tmp_path,
            run_id,
            make_external(run_id),
            registry=registry,
            watched_at=FIXED_STAMP,
            run_reader=reader,
        )
    assert run_id in str(exc.value)
    assert registry.list_watched() == ()
    assert not registry.watched_dir.exists()


# ---------------------------------------------------------------------------
# The documented boundary: the raw registry is unchanged
# ---------------------------------------------------------------------------


def test_registry_watch_alone_accepts_entries_without_run_resolution(
    tmp_path: Path,
) -> None:
    """Documented boundary: ``WatchedRunRegistry.watch`` itself is
    unchanged and still accepts any contract-valid entry without reading
    the Run registry -- the referential-integrity check lives in the
    authoring helper, not in the registry."""
    registry = make_registry(tmp_path / "monitor")
    run_id = make_run_id()  # no workspace, no run record anywhere
    record = WatchedRunRecord(
        run_id=run_id,
        external=make_external(run_id),
        watched_at=FIXED_STAMP,
    )
    assert registry.watch(record) == record
    assert registry.get(run_id) == record


# ---------------------------------------------------------------------------
# Boundaries and the error hierarchy
# ---------------------------------------------------------------------------


def test_watch_run_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public type boundaries (the house paradigm)."""
    run_id = make_run_id()
    external = make_external(run_id)
    registry = make_registry(tmp_path / "monitor")
    with pytest.raises(TypeError):
        watch_run(
            42,  # type: ignore[arg-type]
            run_id,
            external,
            registry=registry,
            watched_at=FIXED_STAMP,
        )
    with pytest.raises(TypeError):
        watch_run(
            tmp_path,
            42,  # type: ignore[arg-type]
            external,
            registry=registry,
            watched_at=FIXED_STAMP,
        )
    with pytest.raises(TypeError):
        watch_run(
            tmp_path,
            run_id,
            None,  # type: ignore[arg-type]
            registry=registry,
            watched_at=FIXED_STAMP,
        )
    with pytest.raises(TypeError):
        watch_run(
            tmp_path,
            run_id,
            external,
            registry=None,  # type: ignore[arg-type]
            watched_at=FIXED_STAMP,
        )
    with pytest.raises(TypeError):
        watch_run(
            tmp_path,
            run_id,
            external,
            registry=registry,
            watched_at=42,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        watch_run(
            tmp_path,
            run_id,
            external,
            registry=registry,
            watched_at=FIXED_STAMP,
            run_reader=42,  # type: ignore[arg-type]
        )


def test_watch_run_error_hierarchy_is_value_error_based() -> None:
    """``UnwatchableRunStateError`` is a stable ``MonitoringError`` (a
    ``ValueError`` subclass, the house paradigm)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(UnwatchableRunStateError, MonitoringError)


def test_watchable_run_states_constant() -> None:
    """The watchable-state set is exactly the dispatched/external-
    executing pair (every other lifecycle state is refused at authoring
    time)."""
    assert WATCHABLE_RUN_STATES == frozenset(
        {LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL}
    )
