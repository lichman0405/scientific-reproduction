"""Tests for the deterministic adapter-state -> monitor-vocabulary
bridge (issue #151).

The reconcile engine's completion vocabulary is
``{"RESULT_AVAILABLE"}`` (the lab adapter's ``DispatchState`` value),
while the compute adapters report their ``JobState`` vocabulary. The
mapping table of ``monitoring/adapter_state.py`` is the single
deterministic runtime bridge between those vocabularies; these tests
lock it:

* the mapping table covers **every** member of both adapter state
  vocabularies and nothing else -- adding, removing or renaming any
  ``JobState`` / ``DispatchState`` member without updating the table
  fails the consistency test (both vocabularies are locked);
* terminal success states map to the completion signal and are the
  only states that ever do -- the mapping can never fabricate
  completion (the AC-02 safe default is unchanged);
* non-terminal states map to the running observation, terminal
  non-success states map to non-completion observations;
* the mapping is deterministic, raises ``TypeError`` at the type
  boundary, and passes unrecognized backend-specific strings through
  unchanged (never as completion);
* ``build_mapped_probe`` wraps a raw adapter-state source into a
  reconcile probe routed through the mapping, and the reconcile
  engine's default probe is that mapped construction -- a raw
  ``JobState.COMPLETED`` observation moves a Run to
  ``RESULT_AVAILABLE`` through the real engine, and a raw
  ``JobState.FAILED`` observation never does.

Determinism: no wall clock, no randomness, no network -- injected
clocks and ``tmp_path`` state directories only, mirroring the
discipline of ``test_reconcile.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scientific_reproduction.adapters.compute.local import JobState
from scientific_reproduction.adapters.lab import DispatchState
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring.adapter_state import (
    ADAPTER_STATE_TO_EXTERNAL_STATE,
    build_mapped_probe,
    map_adapter_state,
)
from scientific_reproduction.monitoring.reconcile import (
    COMPLETION_SIGNALS,
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATE_RUNNING,
    EXTERNAL_STATE_UNKNOWN,
    ReconcileEngine,
    default_probe,
)
from scientific_reproduction.monitoring.registry import WatchedRunRecord

#: Every injected timestamp is this fixed value (no wall clock).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"


class FakeClock:
    """Injectable clock: the fixed stamp repeats forever (mirrors the
    reconcile tests' FakeClock)."""

    def __call__(self) -> str:
        return FIXED_STAMP


def make_external(job_id: str) -> RunExternal:
    """A deterministic external identity with the given job id."""
    return RunExternal(
        backend="local",
        job_id=job_id,
        dispatch_id=None,
        working_directory="/home/alice/scratch/work-1",
    )


def make_run(index: int = 1) -> Run:
    """A deterministic durable Run record at ``RUNNING_EXTERNAL``."""
    run_id = generate_id("run", f"goal-{index}", f"seq-{index}")
    return Run(
        run_id=run_id,
        goal_id=generate_id("goal", f"g{index}"),
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
        goal_version="v1",
        external=make_external(generate_id("job", run_id)),
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def make_watch_record(run: Run) -> WatchedRunRecord:
    """A deterministic watch entry for ``run`` (the identity the engine
    polls under)."""
    return WatchedRunRecord(
        run_id=run.run_id,
        external=run.external,
        watched_at=FIXED_STAMP,
        adapter_id="adapter:compute/local",
        adapter_version="1.0",
    )


def make_engine(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    probe: object | None = None,
) -> ReconcileEngine:
    """An engine over ``state_dir`` with an injected run store over
    ``runs_dir``, an event log over ``events_dir`` and the fixed clock
    (mirrors the reconcile tests' make_engine)."""
    return ReconcileEngine(
        state_dir,
        now=FakeClock(),
        probe=probe,  # type: ignore[arg-type]
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
    )


# ---------------------------------------------------------------------------
# The vocabulary-consistency lock (issue #151)
# ---------------------------------------------------------------------------


def test_mapping_table_locks_both_adapter_vocabularies() -> None:
    """The mapping table covers every member of both adapter state
    vocabularies and nothing else: adding, removing or renaming any
    ``JobState`` / ``DispatchState`` member without updating the table
    fails this test."""
    expected = {member.value for member in JobState} | {
        member.value for member in DispatchState
    }
    assert set(ADAPTER_STATE_TO_EXTERNAL_STATE) == expected


# ---------------------------------------------------------------------------
# The mapping semantics
# ---------------------------------------------------------------------------


def test_terminal_success_states_map_to_the_completion_signal() -> None:
    """``JobState.COMPLETED`` and ``DispatchState.RESULT_AVAILABLE`` map
    deterministically to the completion signal -- and they are the only
    adapter states that ever do."""
    assert (
        map_adapter_state(JobState.COMPLETED)
        == EXTERNAL_STATE_RESULT_AVAILABLE
    )
    assert (
        map_adapter_state(DispatchState.RESULT_AVAILABLE)
        == EXTERNAL_STATE_RESULT_AVAILABLE
    )
    assert EXTERNAL_STATE_RESULT_AVAILABLE in COMPLETION_SIGNALS
    completion_keys = {
        key
        for key, target in ADAPTER_STATE_TO_EXTERNAL_STATE.items()
        if target in COMPLETION_SIGNALS
    }
    assert completion_keys == {
        JobState.COMPLETED.value,
        DispatchState.RESULT_AVAILABLE.value,
    }


def test_non_terminal_states_map_to_the_running_observation() -> None:
    """Non-terminal adapter states map to the non-completion running
    observation (``RUNNING_EXTERNAL``), never to a completion
    signal."""
    for state in (
        JobState.PREPARED,
        JobState.RUNNING,
        DispatchState.RUNNING_EXTERNAL,
    ):
        mapped = map_adapter_state(state)
        assert mapped == EXTERNAL_STATE_RUNNING
        assert mapped not in COMPLETION_SIGNALS


def test_terminal_non_success_states_map_to_non_completion_observations(
) -> None:
    """Terminal non-success states (``failed`` / ``cancelled``) keep
    their own backend-specific strings: observed and recorded, never
    treated as completion (the AC-02 safe default is unchanged)."""
    for state in (JobState.FAILED, JobState.CANCELLED):
        mapped = map_adapter_state(state)
        assert mapped == state.value
        assert mapped not in COMPLETION_SIGNALS


def test_mapping_is_deterministic() -> None:
    """Every table entry maps to its target on every call -- the
    mapping is a pure function over the frozen table."""
    for state, target in ADAPTER_STATE_TO_EXTERNAL_STATE.items():
        assert map_adapter_state(state) == target
        assert map_adapter_state(state) == target


def test_unrecognized_adapter_state_passes_through_never_completing(
) -> None:
    """An unrecognized backend-specific string is returned unchanged --
    a non-completion observation, never a fabricated completion."""
    assert (
        map_adapter_state("backend_specific_state")
        == "backend_specific_state"
    )
    assert "backend_specific_state" not in COMPLETION_SIGNALS


def test_map_adapter_state_type_boundary() -> None:
    """``TypeError`` at the public type boundary (the house paradigm)."""
    with pytest.raises(TypeError):
        map_adapter_state(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The mapped probe
# ---------------------------------------------------------------------------


def test_build_mapped_probe_routes_raw_adapter_states() -> None:
    """The mapped probe routes a raw adapter-state source through the
    deterministic mapping: a raw ``completed`` report becomes the
    completion signal, a raw ``running`` report the running
    observation."""
    external = make_external("job-1")
    probe = build_mapped_probe(lambda _external: JobState.COMPLETED.value)
    assert probe(external) == EXTERNAL_STATE_RESULT_AVAILABLE
    probe = build_mapped_probe(lambda _external: JobState.RUNNING.value)
    assert probe(external) == EXTERNAL_STATE_RUNNING


def test_build_mapped_probe_type_boundary() -> None:
    """``TypeError`` at the public type boundary (the house paradigm)."""
    with pytest.raises(TypeError):
        build_mapped_probe("not callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The reconcile engine's default probe (issue #151)
# ---------------------------------------------------------------------------


def test_reconcile_engine_default_probe_is_the_mapped_default(
    tmp_path: Path,
) -> None:
    """The engine's default probe is the mapped default construction:
    the always-unknown raw source routed through the deterministic
    adapter-state mapping -- observing ``UNKNOWN``, never fabricating
    completion (AC-02)."""
    engine = ReconcileEngine(tmp_path / "state", now=FakeClock())
    assert engine.probe is default_probe
    observed = engine.probe(make_external("job-1"))
    assert observed == EXTERNAL_STATE_UNKNOWN
    assert observed not in COMPLETION_SIGNALS


def test_engine_completes_a_run_from_a_raw_adapter_state_probe(
    tmp_path: Path,
) -> None:
    """End to end: a mapped probe over a raw ``JobState`` source
    (``completed``) moves a Run to ``RESULT_AVAILABLE`` through the
    real engine -- the runtime bridge replaces the hand-written probe
    mapping."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = build_mapped_probe(lambda _external: JobState.COMPLETED.value)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(make_watch_record(run))
    engine.run_store.write("run", run.run_id, run.to_dict())

    outcome = engine.reconcile(run.run_id)

    assert outcome.observed_state == EXTERNAL_STATE_RESULT_AVAILABLE
    assert outcome.completed is True
    persisted = Run.from_dict(engine.run_store.read("run", run.run_id))
    assert persisted.lifecycle_state is LifecycleState.RESULT_AVAILABLE
    assert len(engine.event_log.list_events()) == 1


def test_engine_never_completes_a_run_from_a_raw_failed_state(
    tmp_path: Path,
) -> None:
    """The AC-02 safe default through the mapping: a raw ``failed``
    ``JobState`` observation is recorded but never treated as
    completion -- the Run stays ``RUNNING_EXTERNAL`` and no transition
    event is emitted."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = build_mapped_probe(lambda _external: JobState.FAILED.value)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(make_watch_record(run))
    engine.run_store.write("run", run.run_id, run.to_dict())

    outcome = engine.reconcile(run.run_id)

    assert outcome.observed_state == JobState.FAILED.value
    assert outcome.completed is False
    persisted = Run.from_dict(engine.run_store.read("run", run.run_id))
    assert persisted.lifecycle_state is LifecycleState.RUNNING_EXTERNAL
    assert engine.event_log.list_events() == []
