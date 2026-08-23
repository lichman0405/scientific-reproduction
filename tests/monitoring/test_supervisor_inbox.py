"""Tests for the durable supervisor inbox of arrived Result Packages
(issue #163, deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac1_*`` -- AC-1: a completed run creates **exactly one** inbox
  entry (run id, dispatch id, completion event id, injected timestamp,
  pending flag) at ``<state_dir>/supervisor-inbox/<run_id>.json``, and
  re-reconciliation is idempotent: re-reconciling the same progress --
  same engine, a fresh engine, or a crash-window recovery -- never
  creates a duplicate entry for the same completion.
* ``test_ac2_*`` -- AC-2: ``list_inbox()`` returns the pending entries
  in deterministic sorted run-id order; ``mark_reviewed()`` marks them
  reviewed (pending flag flipped, persisted), after which the entry is
  no longer pending work. Non-completion observations never file an
  entry.
* ``test_ac02_*`` -- the checkpoint AC-02 design the inbox mirrors:
  entries are plain atomic state-file writes on a durable state
  directory -- no git involvement of any kind.
* ``test_inbox_*`` -- the durable contracts: strict ``from_dict``
  validation with stable ``InboxRecordError`` (a ValueError subclass),
  idempotent re-filing keyed by run id / completion event id, refusal
  of a conflicting completion event for the same run, sorted
  deterministic persistence, byte-identical determinism for identical
  inputs, the injected clock (the filing principal's clock, no wall
  clock anywhere) and the no-secrets discipline.

Determinism: every test injects a :class:`FakeClock` producing the
fixed ``FIXED_STAMP`` timestamp (no wall clock), ``tmp_path`` state
directories and ``generate_id`` ids. No randomness, no network, no
sleeps anywhere.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunExternal,
    RunType,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring import (
    INBOX_STATE_DIR,
    DuplicateInboxEntryError,
    InboxEntryNotFoundError,
    InboxRecordError,
    MonitoringError,
    SupervisorInbox,
    SupervisorInboxEntry,
)
from scientific_reproduction.monitoring.reconcile import (
    EXTERNAL_STATE_RESULT_AVAILABLE,
    EXTERNAL_STATE_RUNNING,
    EXTERNAL_STATE_UNKNOWN,
    ReconcileEngine,
)
from scientific_reproduction.monitoring.registry import WatchedRunRecord
from scientific_reproduction.monitoring.supervisor_inbox import (
    INBOX_ENTRY_VERSION,
)

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: Credential-shaped strings that must never appear in persisted bytes.
FORBIDDEN_SECRETS = ("password", "passphrase", "secret", "credential",
                     "token", "api_key")


class FakeClock:
    """Injectable clock: a single fixed stamp repeats forever and every
    read is recorded (mirrors the checkpoint tests' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


class ConstantProbe:
    """External-status probe that always reports one fixed state."""

    def __init__(self, state: str) -> None:
        self._state = state
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._state


class MappingProbe:
    """External-status probe that reports a state per external job id
    (order-independent external truth for restart tests)."""

    def __init__(self, states: dict[str, str]) -> None:
        self._states = dict(states)
        self.calls: list[RunExternal] = []

    def __call__(self, external: RunExternal) -> str:
        self.calls.append(external)
        return self._states.get(external.job_id, EXTERNAL_STATE_RUNNING)


def make_run_id(index: int = 1) -> str:
    """A deterministic run id (``sr_run_<32 hex>``)."""
    return generate_id("run", f"goal-{index}", f"seq-{index}")


def make_external(
    *,
    backend: str = "slurm_ssh",
    job_id: str | None = None,
    dispatch_id: str | None = None,
    working_directory: str | None = None,
) -> RunExternal:
    """An external identity; by default a slurm-ssh-shaped one with a
    job id and a working directory."""
    return RunExternal(
        backend=backend,
        job_id=job_id,
        dispatch_id=dispatch_id,
        working_directory=working_directory,
    )


def make_watch_record(
    index: int = 1,
    *,
    external: RunExternal | None = None,
    watched_at: str = FIXED_STAMP,
) -> WatchedRunRecord:
    """A deterministic watch entry for run ``index`` (the identity the
    engine polls under)."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return WatchedRunRecord(
        run_id=run_id,
        external=external,
        watched_at=watched_at,
        adapter_id="adapter:compute/slurm_ssh",
        adapter_version="1.0",
    )


def make_run(
    index: int = 1,
    *,
    lifecycle_state: LifecycleState = LifecycleState.RUNNING_EXTERNAL,
    external: RunExternal | None = None,
) -> Run:
    """A deterministic durable Run record (``RUNNING_EXTERNAL`` by
    default, with the fixed created/updated stamps)."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return Run(
        run_id=run_id,
        goal_id=generate_id("goal", f"g{index}"),
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=lifecycle_state,
        goal_version="v1",
        external=external,
        created_at=FIXED_STAMP,
        updated_at=FIXED_STAMP,
    )


def write_run(run_store: FilesystemStateBackend, run: Run) -> None:
    """Persist a run through the real schema-validating backend."""
    run_store.write("run", run.run_id, run.to_dict())


def make_engine(
    state_dir: Path,
    runs_dir: Path,
    events_dir: Path,
    *,
    probe: ConstantProbe | MappingProbe | None = None,
    clock: FakeClock | None = None,
    monitor_id: str | None = None,
) -> ReconcileEngine:
    """An engine over ``state_dir`` with an injected run store over
    ``runs_dir``, an event log over ``events_dir`` and the fixed clock."""
    return ReconcileEngine(
        state_dir,
        now=clock or FakeClock(),
        probe=probe,
        run_store=FilesystemStateBackend(runs_dir),
        event_log=ProjectEventLog(events_dir),
        monitor_id=monitor_id,
    )


def make_inbox(state_dir: Path) -> SupervisorInbox:
    """An inbox store over ``state_dir``."""
    return SupervisorInbox(state_dir)


def make_entry(
    index: int = 1,
    *,
    dispatch_id: str | None = None,
    pending: bool = True,
    injected_at: str = FIXED_STAMP,
) -> SupervisorInboxEntry:
    """A deterministic inbox entry for run ``index`` (a deterministic
    completion event id derived from the run id)."""
    run_id = make_run_id(index)
    if dispatch_id is None:
        dispatch_id = generate_id("dispatch", run_id)
    return SupervisorInboxEntry(
        run_id=run_id,
        dispatch_id=dispatch_id,
        completion_event_id=generate_id("event", "completion", run_id),
        injected_at=injected_at,
        pending=pending,
    )


def tree_bytes(root: Path) -> list[tuple[str, bytes]]:
    """(relative path, bytes) of every file under ``root``, sorted."""
    if not root.is_dir():
        return []
    return sorted(
        (p.relative_to(root).as_posix(), p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    )


def inbox_files(state_dir: Path) -> list[tuple[str, bytes]]:
    """(relative path, bytes) of every persisted inbox entry, sorted
    (paths relative to ``state_dir``)."""
    inbox_dir = state_dir / INBOX_STATE_DIR
    if not inbox_dir.is_dir():
        return []
    return sorted(
        (p.relative_to(state_dir).as_posix(), p.read_bytes())
        for p in inbox_dir.rglob("*")
        if p.is_file()
    )


# ---------------------------------------------------------------------------
# AC-1: a completed run creates exactly one inbox entry; re-reconciliation
# is idempotent
# ---------------------------------------------------------------------------


def test_ac1_completed_run_creates_exactly_one_inbox_entry(
    tmp_path: Path,
) -> None:
    """AC-1: when reconciliation records an external completion, it
    files exactly one supervisor-inbox entry for the arrived Result
    Package -- run id, dispatch id (the watch entry's), the id of the
    completion event the log recorded, the injected timestamp and the
    pending flag -- at ``<state_dir>/supervisor-inbox/<run_id>.json``."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    watch = make_watch_record(
        1,
        external=RunExternal(
            backend="lab",
            dispatch_id=generate_id("dispatch", make_run_id(1)),
            working_directory="C:/lab/outgoing/user-9/run-1",
        ),
    )
    run = make_run(1, external=watch.external)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(watch)
    write_run(engine.run_store, run)

    outcome = engine.reconcile(run.run_id)

    assert outcome.completed is True
    # Exactly one inbox entry file, in the documented directory.
    files = inbox_files(state)
    assert [path for path, _ in files] == [
        f"{INBOX_STATE_DIR}/{run.run_id}.json"
    ]
    # The entry carries the full documented vocabulary.
    raw = json.loads(files[0][1])
    assert set(raw) == {
        "record_version",
        "run_id",
        "dispatch_id",
        "completion_event_id",
        "injected_at",
        "pending",
    }
    assert raw["record_version"] == INBOX_ENTRY_VERSION
    assert raw["run_id"] == run.run_id
    assert raw["dispatch_id"] == watch.external.dispatch_id
    assert raw["injected_at"] == FIXED_STAMP
    assert raw["pending"] is True
    # The completion event id is the id of the single event the log
    # recorded -- the inbox entry points at the exact completion record.
    events = engine.event_log.list_events()
    assert len(events) == 1
    assert raw["completion_event_id"] == events[0].event.event_id
    # The engine's inbox store lists exactly the one pending entry.
    assert engine.inbox.list_inbox() == (
        SupervisorInboxEntry(
            run_id=run.run_id,
            dispatch_id=watch.external.dispatch_id,
            completion_event_id=events[0].event.event_id,
            injected_at=FIXED_STAMP,
            pending=True,
        ),
    )


def test_ac1_completed_run_without_dispatch_id_still_gets_an_entry(
    tmp_path: Path,
) -> None:
    """AC-1: a completed run whose external backend has no dispatch id
    (a compute job) still gets exactly one inbox entry -- the dispatch
    id is omitted from the persisted entry, never fabricated."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)  # slurm-ssh-shaped: job id, no dispatch id
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(make_watch_record(1, external=run.external))
    write_run(engine.run_store, run)

    outcome = engine.reconcile(run.run_id)

    assert outcome.completed is True
    files = inbox_files(state)
    assert len(files) == 1
    raw = json.loads(files[0][1])
    assert "dispatch_id" not in raw
    entry = engine.inbox.list_inbox()
    assert len(entry) == 1
    assert entry[0].dispatch_id is None
    assert entry[0].run_id == run.run_id


def test_ac1_rereconciliation_is_idempotent_no_duplicate_entry(
    tmp_path: Path,
) -> None:
    """AC-1: re-reconciling the same completion -- same engine and a
    fresh engine over the same durable state -- never creates a second
    inbox entry: exactly one entry file with identical bytes."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(make_watch_record(1, external=run.external))
    write_run(engine.run_store, run)

    first = engine.reconcile(run.run_id)
    assert first.completed is True
    entry_bytes = inbox_files(state)

    # The steady-state re-poll of the same engine.
    again = engine.reconcile(run.run_id)
    assert again.completed is False
    assert inbox_files(state) == entry_bytes
    assert len(engine.inbox.list_inbox()) == 1

    # A FRESH engine over the same durable state (AC-03 restart).
    fresh = make_engine(state, runs_dir, events_dir, probe=probe)
    again = fresh.reconcile(run.run_id)
    assert again.completed is False
    assert inbox_files(state) == entry_bytes
    assert len(fresh.inbox.list_inbox()) == 1


def test_ac1_crash_window_converges_on_single_inbox_entry(
    tmp_path: Path,
) -> None:
    """AC-1: a crash between the Run transition and the bookkeeping
    (Run already RESULT_AVAILABLE, no completion in the checkpoint or
    event log) converges on a single completion -- and a single inbox
    entry: the recovery reconcile files exactly the one entry and
    re-reconciling never duplicates it."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1, lifecycle_state=LifecycleState.RESULT_AVAILABLE)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(make_watch_record(1, external=run.external))
    write_run(engine.run_store, run)
    assert engine.event_log.list_events() == []
    assert engine.inbox.list_inbox() == ()

    outcome = engine.reconcile(run.run_id)

    assert outcome.completed is False  # the transition already happened
    events = engine.event_log.list_events()
    assert len(events) == 1
    files = inbox_files(state)
    assert len(files) == 1
    assert json.loads(files[0][1])["completion_event_id"] == (
        events[0].event.event_id
    )
    entry_bytes = files

    # Re-reconciling the recovered completion never duplicates the entry.
    again = engine.reconcile(run.run_id)
    assert again.completed is False
    assert inbox_files(state) == entry_bytes
    assert len(engine.inbox.list_inbox()) == 1


# ---------------------------------------------------------------------------
# AC-2: list_inbox returns pending entries deterministically; reviewing
# marks them
# ---------------------------------------------------------------------------


def test_ac2_list_inbox_returns_pending_entries_deterministically(
    tmp_path: Path,
) -> None:
    """AC-2: ``list_inbox()`` returns the pending entries reconstructed
    from the persisted files alone, in sorted run-id order
    (deterministic), regardless of the filing order; reviewed entries
    are not pending work and are excluded."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    # File out of run-id order to prove the listing order is
    # deterministic, not insertion order.
    entries = (make_entry(3), make_entry(1), make_entry(2))
    for entry in entries:
        inbox.file_entry(entry)

    assert inbox.list_inbox() == tuple(
        sorted(entries, key=lambda entry: entry.run_id)
    )

    # Marking the middle entry reviewed removes it from the pending
    # listing only -- the other two stay pending in sorted run-id order.
    inbox.mark_reviewed(make_run_id(2))
    pending = inbox.list_inbox()
    assert [entry.run_id for entry in pending] == sorted(
        [make_run_id(1), make_run_id(3)]
    )


def test_ac2_mark_reviewed_flips_pending_and_persists(tmp_path: Path) -> None:
    """AC-2: ``mark_reviewed()`` marks the entry reviewed -- the pending
    flag is flipped to False and persisted, so a fresh store instance
    over the same state directory sees the reviewed entry -- and the
    entry drops out of the pending listing."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    entry = make_entry(1)
    inbox.file_entry(entry)
    assert inbox.list_inbox() == (entry,)

    reviewed = inbox.mark_reviewed(entry.run_id)

    assert reviewed == SupervisorInboxEntry(
        run_id=entry.run_id,
        dispatch_id=entry.dispatch_id,
        completion_event_id=entry.completion_event_id,
        injected_at=entry.injected_at,
        pending=False,
    )
    raw = json.loads(
        (state / INBOX_STATE_DIR / f"{entry.run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["pending"] is False
    # A fresh store instance recovers the reviewed entry from disk alone.
    fresh = make_inbox(state)
    assert fresh.list_inbox() == ()
    # Idempotent: marking an already-reviewed entry is a pure no-op
    # (identical persisted bytes).
    before = inbox_files(state)
    assert fresh.mark_reviewed(entry.run_id).pending is False
    assert inbox_files(state) == before


def test_ac2_non_completion_observations_file_no_inbox_entry(
    tmp_path: Path,
) -> None:
    """AC-2: only a recorded completion files an inbox entry -- unknown
    and running probe outcomes (observed, recorded in the checkpoint,
    never treated as completion) leave the inbox empty."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    running_run = make_run(1)
    unknown_run = make_run(2)
    states = {
        running_run.external.job_id: EXTERNAL_STATE_RUNNING,
        unknown_run.external.job_id: EXTERNAL_STATE_UNKNOWN,
    }
    probe = MappingProbe(states)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    for index, run in enumerate((running_run, unknown_run), start=1):
        engine.registry.watch(
            make_watch_record(index, external=run.external)
        )
        write_run(engine.run_store, run)

    summary = engine.reconcile_all()

    assert summary.completed_count == 0
    assert engine.inbox.list_inbox() == ()
    assert inbox_files(state) == []


def test_ac2_mark_reviewed_missing_entry_raises(tmp_path: Path) -> None:
    """AC-2: marking a run with no inbox entry reviewed is refused
    loudly (stable ``InboxEntryNotFoundError``) -- review is never
    silently claimed for an arrival that was never filed."""
    inbox = make_inbox(tmp_path / "state")
    with pytest.raises(InboxEntryNotFoundError):
        inbox.mark_reviewed(make_run_id(1))


# ---------------------------------------------------------------------------
# The checkpoint AC-02 design: plain atomic state files, no git
# ---------------------------------------------------------------------------


def test_ac02_inbox_entries_are_plain_atomic_state_files_without_git(
    tmp_path: Path,
) -> None:
    """The inbox mirrors the checkpoint AC-02 design: filing entries is
    a plain filesystem atomic write on a durable state directory -- the
    inbox directory holds exactly the durable JSON state files (no git
    bookkeeping anywhere), an update replaced the file content in place,
    and the directory is not a git worktree: no audit commit was or
    could be involved."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    inbox.file_entry(make_entry(1))
    first_bytes = inbox_files(state)
    # The update path: re-filing the identical entry is an idempotent
    # no-op, so exercise an actual update through mark_reviewed.
    inbox.mark_reviewed(make_run_id(1))

    files = sorted(
        p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file()
    )
    assert files == [f"{INBOX_STATE_DIR}/{make_run_id(1)}.json"]
    assert not any(
        part in (".git", "objects", "refs") or part.startswith("HEAD")
        for p in state.rglob("*")
        for part in p.parts
    )
    updated = inbox_files(state)
    assert updated != first_bytes
    assert json.loads(updated[0][1])["pending"] is False
    probe = subprocess.run(
        ["git", "-C", str(state), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0


def test_ac02_inbox_entry_is_a_plain_json_state_file(tmp_path: Path) -> None:
    """The inbox entry is a plain JSON durable state file carrying its
    record version -- nothing else is ever created in the inbox
    directory."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    inbox.file_entry(make_entry(1))
    files = sorted(
        p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file()
    )
    assert files == [f"{INBOX_STATE_DIR}/{make_run_id(1)}.json"]
    raw = json.loads(
        (state / INBOX_STATE_DIR / f"{make_run_id(1)}.json").read_text(
            encoding="utf-8"
        )
    )
    assert raw["record_version"] == INBOX_ENTRY_VERSION


# ---------------------------------------------------------------------------
# The inbox contracts
# ---------------------------------------------------------------------------


def test_inbox_entry_to_from_dict_roundtrip() -> None:
    """The entry round-trips through to_dict/from_dict (pending and
    reviewed, with and without a dispatch id)."""
    for entry in (make_entry(1), make_entry(2, pending=False), make_entry(
        3,
        dispatch_id=None,
    )):
        assert SupervisorInboxEntry.from_dict(entry.to_dict()) == entry


def test_inbox_entry_from_dict_rejects_corrupt_entries() -> None:
    """Strict from_dict validation with stable ``InboxRecordError``:
    unknown version, missing fields, invalid run/event ids, mistyped or
    empty fields."""
    entry = make_entry(1)

    def write(**changes: object) -> dict[str, object]:
        data = dict(entry.to_dict())
        data.update(changes)
        return data

    with pytest.raises(InboxRecordError):
        SupervisorInboxEntry.from_dict(write(record_version="0.9"))
    with pytest.raises(InboxRecordError):
        SupervisorInboxEntry.from_dict(write(run_id="bogus"))
    with pytest.raises(InboxRecordError):
        SupervisorInboxEntry.from_dict(write(completion_event_id="bogus"))
    with pytest.raises(InboxRecordError):
        SupervisorInboxEntry.from_dict(write(injected_at=""))
    # A mistyped field fails the persisted-shape contract: from_dict
    # wraps the constructor error into the stable InboxRecordError.
    with pytest.raises(InboxRecordError):
        SupervisorInboxEntry.from_dict(write(pending="yes"))
    with pytest.raises(InboxRecordError):
        SupervisorInboxEntry.from_dict(write(dispatch_id=""))
    for field in (
        "record_version",
        "run_id",
        "completion_event_id",
        "injected_at",
        "pending",
    ):
        data = dict(entry.to_dict())
        del data[field]
        with pytest.raises(InboxRecordError):
            SupervisorInboxEntry.from_dict(data)
    with pytest.raises(TypeError):
        SupervisorInboxEntry.from_dict("not a mapping")  # type: ignore[arg-type]


def test_inbox_entry_constructor_type_boundaries() -> None:
    """TypeError at the entry type boundaries (house paradigm)."""
    with pytest.raises(TypeError):
        SupervisorInboxEntry(  # type: ignore[arg-type]
            run_id=42,
            completion_event_id=generate_id("event", "x"),
            injected_at=FIXED_STAMP,
        )
    with pytest.raises(TypeError):
        SupervisorInboxEntry(  # type: ignore[arg-type]
            run_id=make_run_id(1),
            completion_event_id=42,
            injected_at=FIXED_STAMP,
        )
    with pytest.raises(TypeError):
        SupervisorInboxEntry(  # type: ignore[arg-type]
            run_id=make_run_id(1),
            completion_event_id=generate_id("event", "x"),
            injected_at=FIXED_STAMP,
            pending="yes",
        )


def test_inbox_filing_identical_entry_is_idempotent_noop(
    tmp_path: Path,
) -> None:
    """Re-filing the identical completion entry (same run id and
    completion event id) is an idempotent no-op that returns the
    persisted entry: one file, identical bytes, never a duplicate."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    entry = make_entry(1)
    assert inbox.file_entry(entry) == entry
    before = inbox_files(state)
    # Re-filing with a different (later) stamp still converges on the
    # original entry: the deterministic key is run id / completion
    # event id, not the timestamp.
    replayed = SupervisorInboxEntry(
        run_id=entry.run_id,
        dispatch_id=entry.dispatch_id,
        completion_event_id=entry.completion_event_id,
        injected_at="2026-08-15T00:00:00+00:00",
        pending=True,
    )
    assert inbox.file_entry(replayed) == entry
    assert inbox_files(state) == before
    assert inbox.list_inbox() == (entry,)


def test_inbox_filing_conflicting_completion_event_refused(
    tmp_path: Path,
) -> None:
    """Filing a *different* completion event for a run that already has
    an entry is refused with the stable ``DuplicateInboxEntryError`` --
    one arrival, one entry -- and the persisted entry is untouched."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    entry = make_entry(1)
    inbox.file_entry(entry)
    before = inbox_files(state)
    conflicting = SupervisorInboxEntry(
        run_id=entry.run_id,
        dispatch_id=entry.dispatch_id,
        completion_event_id=generate_id("event", "some-other-completion"),
        injected_at=FIXED_STAMP,
        pending=True,
    )
    with pytest.raises(DuplicateInboxEntryError):
        inbox.file_entry(conflicting)
    assert inbox_files(state) == before
    assert inbox.list_inbox() == (entry,)


def test_inbox_corrupt_entry_fails_listing_loudly(tmp_path: Path) -> None:
    """A corrupt entry file (garbage, non-object JSON, or a file
    failing the contract) fails ``list_inbox`` loudly with the stable
    ``InboxRecordError`` -- the inbox is a durable mailbox and
    everything in it is an inbox entry."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    inbox.file_entry(make_entry(1))
    path = state / INBOX_STATE_DIR / f"{make_run_id(1)}.json"

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InboxRecordError):
        inbox.list_inbox()
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(InboxRecordError):
        inbox.list_inbox()
    good = make_entry(1).to_dict()
    path.write_text(
        json.dumps({**good, "record_version": "0.9"}, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(InboxRecordError):
        inbox.list_inbox()
    path.write_text(json.dumps({**good, "pending": "yes"}, sort_keys=True),
                    encoding="utf-8")
    with pytest.raises(InboxRecordError):
        inbox.list_inbox()


def test_inbox_mark_reviewed_invalid_run_id(tmp_path: Path) -> None:
    """mark_reviewed enforces the run-id contract: TypeError for a
    non-str, InboxRecordError for a non-run id."""
    inbox = make_inbox(tmp_path / "state")
    with pytest.raises(TypeError):
        inbox.mark_reviewed(42)  # type: ignore[arg-type]
    with pytest.raises(InboxRecordError):
        inbox.mark_reviewed("not-a-run-id")


def test_inbox_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the store type boundaries."""
    with pytest.raises(TypeError):
        SupervisorInbox(42)  # type: ignore[arg-type]
    inbox = make_inbox(tmp_path / "state")
    with pytest.raises(TypeError):
        inbox.file_entry("not an entry")  # type: ignore[arg-type]


def test_inbox_error_hierarchy_is_value_error_based() -> None:
    """The inbox error hierarchy is ValueError-based with stable
    subclasses (the house paradigm for durable-state errors)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(InboxRecordError, MonitoringError)
    assert issubclass(InboxEntryNotFoundError, MonitoringError)
    assert issubclass(DuplicateInboxEntryError, MonitoringError)
    assert InboxRecordError is not InboxEntryNotFoundError
    assert InboxRecordError is not DuplicateInboxEntryError
    assert InboxEntryNotFoundError is not DuplicateInboxEntryError


def test_inbox_entries_listed_in_sorted_run_id_order(tmp_path: Path) -> None:
    """Entries are persisted as canonical sorted JSON and listed in
    sorted run-id order (deterministic bytes and order)."""
    state = tmp_path / "state"
    inbox = make_inbox(state)
    for entry in (make_entry(3), make_entry(1), make_entry(2)):
        inbox.file_entry(entry)
    assert [entry.run_id for entry in inbox.list_inbox()] == sorted(
        [make_run_id(1), make_run_id(2), make_run_id(3)]
    )
    # The persisted bytes are canonical sorted JSON.
    raw = json.loads(
        (state / INBOX_STATE_DIR / f"{make_run_id(1)}.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(raw) == sorted(raw)


def test_inbox_byte_identical_for_identical_inputs(tmp_path: Path) -> None:
    """Identical injected inputs produce byte-identical inbox files
    (canonical sorted JSON, deterministic ids) -- no randomness, no
    wall clock."""
    payloads: list[list[tuple[str, bytes]]] = []
    for variant in ("a", "b"):
        state = tmp_path / variant
        inbox = make_inbox(state)
        for entry in (make_entry(2), make_entry(1)):
            inbox.file_entry(entry)
        payloads.append(inbox_files(state))
    assert payloads[0] == payloads[1]


def test_inbox_uses_injected_clock_via_engine(tmp_path: Path) -> None:
    """The entry's injected timestamp comes from the engine's injected
    clock -- no wall clock anywhere in the tested path."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    clock = FakeClock(FIXED_STAMP)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe, clock=clock)
    engine.registry.watch(make_watch_record(1, external=run.external))
    write_run(engine.run_store, run)
    engine.reconcile(run.run_id)
    entry = engine.inbox.list_inbox()
    assert len(entry) == 1
    assert entry[0].injected_at == FIXED_STAMP
    assert clock.calls, "the engine must consult the injected clock"


def test_inbox_persisted_entries_never_carry_credentials(
    tmp_path: Path,
) -> None:
    """The no-secrets discipline: after a full reconciliation scenario,
    no persisted byte anywhere (inbox entries included) carries
    credential-shaped content."""
    state, runs_dir, events_dir = (
        tmp_path / "state", tmp_path / "runs", tmp_path / "events"
    )
    run = make_run(1)
    probe = ConstantProbe(EXTERNAL_STATE_RESULT_AVAILABLE)
    engine = make_engine(state, runs_dir, events_dir, probe=probe)
    engine.registry.watch(make_watch_record(1, external=run.external))
    write_run(engine.run_store, run)
    engine.reconcile(run.run_id)

    bytes_ = b"".join(
        p.read_bytes()
        for root in (state, runs_dir, events_dir)
        for p in root.rglob("*")
        if p.is_file()
    )
    lowered = bytes_.decode("utf-8", errors="replace").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered, (
            f"persisted state must never carry {forbidden!r}"
        )


def test_inbox_entry_ids_are_valid() -> None:
    """The inbox entry holds well-formed generated ids only."""
    entry = make_entry(1)
    assert is_valid_id(entry.run_id, "run")
    assert is_valid_id(entry.completion_event_id, "event")


def test_inbox_module_does_not_couple_to_adapters() -> None:
    """Importing the inbox primitive never pulls in the adapters
    package (proven in a fresh interpreter): the dispatch id is a plain
    documented core ``RunExternal`` field."""
    import sys

    code = (
        "import sys\n"
        "import scientific_reproduction.monitoring.supervisor_inbox\n"
        "assert 'scientific_reproduction.adapters' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
