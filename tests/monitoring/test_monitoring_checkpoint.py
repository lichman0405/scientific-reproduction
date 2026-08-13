"""Tests for the monitor checkpoint and heartbeat records (DEV-M8-G01,
deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac02_*`` -- AC-02: heartbeat and checkpoint updates are plain
  atomic file writes on a durable state directory. The tests prove no
  git involvement: after updates the state directory holds exactly the
  durable JSON state files -- no git bookkeeping of any kind (no
  ``.git``, no ``HEAD``/``index``/``objects``/``refs``), the update
  replaced the file content in place through the filesystem atomic-write
  mechanism, and the directory is not a git worktree (``git rev-parse``
  fails), so no audit commit was or could be involved.
* ``test_ac03_*`` -- AC-03: the checkpoint references the
  adapter/external ids needed for reconciliation (backend,
  dispatch_id/job_id, working directory) and is persisted durably,
  validated on read and recoverable by a fresh store instance over the
  same state directory.
* ``test_checkpoint_*`` / ``test_heartbeat_*`` -- the record contracts:
  strict from_dict validation with stable ``CheckpointRecordError`` (a
  ValueError subclass), sorted deterministic persistence, byte-identical
  determinism for identical inputs, the injected clock and the
  no-secrets discipline.

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

from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import RunExternal
from scientific_reproduction.monitoring import (
    CHECKPOINT_FILE,
    HEARTBEAT_FILE,
    CheckpointRecordError,
    HeartbeatRecord,
    MonitorCheckpoint,
    MonitorCheckpointStore,
    MonitorRunCheckpoint,
)

#: Every injected timestamp is this fixed value (no wall clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"

#: Credential-shaped strings that must never appear in persisted bytes.
FORBIDDEN_SECRETS = ("password", "passphrase", "secret", "credential",
                     "token", "api_key")


class FakeClock:
    """Injectable clock: a single fixed stamp repeats forever and every
    read is recorded (mirrors the compute-adapter tests' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


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


def make_entry(
    index: int = 1,
    *,
    external: RunExternal | None = None,
    observed_state: str | None = "RUNNING_EXTERNAL",
    observed_at: str = FIXED_STAMP,
) -> MonitorRunCheckpoint:
    """A deterministic per-run reconciliation checkpoint entry."""
    run_id = make_run_id(index)
    if external is None:
        external = make_external(
            job_id=generate_id("job", run_id),
            dispatch_id=generate_id("dispatch", run_id),
            working_directory=f"/home/alice/scratch/work-{index}",
        )
    return MonitorRunCheckpoint(
        run_id=run_id,
        external=external,
        observed_state=observed_state,
        observed_at=observed_at,
        reconciled_at=FIXED_STAMP,
    )


def make_store(state_dir: Path, clock: FakeClock | None = None) -> MonitorCheckpointStore:
    """A store over ``state_dir`` with the injected fixed clock."""
    return MonitorCheckpointStore(state_dir, now=clock or FakeClock())


def make_checkpoint(
    monitor_id: str,
    *,
    entries: tuple[MonitorRunCheckpoint, ...] = (),
    created_at: str = FIXED_STAMP,
) -> MonitorCheckpoint:
    """A deterministic checkpoint for ``monitor_id``."""
    return MonitorCheckpoint(
        monitor_id=monitor_id,
        created_at=created_at,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# AC-02: heartbeat/checkpoint updates are plain atomic file writes -- no git
# ---------------------------------------------------------------------------


def test_ac02_checkpoint_update_is_plain_atomic_file_write_without_git(
    tmp_path: Path,
) -> None:
    """AC-02: updating a checkpoint is a plain filesystem atomic write
    on a durable state directory -- the state directory holds exactly
    the durable JSON state file (no git bookkeeping anywhere), the
    update replaced the file content in place, and the directory is not
    a git worktree: no audit commit was or could be involved."""
    state = tmp_path / "state"
    store = make_store(state)
    monitor_id = store.monitor_id
    store.save(make_checkpoint(monitor_id, entries=(make_entry(1),)))
    first_bytes = (state / CHECKPOINT_FILE).read_bytes()
    # The update path: save a newer checkpoint over the same file.
    store.save(
        make_checkpoint(monitor_id, entries=(make_entry(1), make_entry(2)))
    )

    # The state directory holds exactly the durable state file -- no
    # .git directory, no HEAD/index/objects/refs, no commit bookkeeping
    # of any kind.
    files = sorted(
        p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file()
    )
    assert files == [CHECKPOINT_FILE]
    assert not any(
        part in (".git", "objects", "refs") or part.startswith("HEAD")
        for p in state.rglob("*")
        for part in p.parts
    )
    # The update replaced the content in place (atomic_write): the new
    # checkpoint is readable as JSON and carries both entries (persisted
    # in sorted run-id order).
    updated = (state / CHECKPOINT_FILE).read_bytes()
    assert updated != first_bytes
    persisted_run_ids = [
        entry["run_id"] for entry in json.loads(updated)["entries"]
    ]
    assert sorted(persisted_run_ids) == sorted(
        [make_run_id(1), make_run_id(2)]
    )
    # The state directory is not inside any git worktree: the update
    # mechanism is filesystem-atomic-write only, never git.
    probe = subprocess.run(
        ["git", "-C", str(state), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0


def test_ac02_heartbeat_update_is_plain_atomic_file_write_without_git(
    tmp_path: Path,
) -> None:
    """AC-02: updating a heartbeat is a plain filesystem atomic write
    on a durable state directory -- same no-git proof as the checkpoint
    update: exactly the state file exists, the beat replaced the file
    content in place, and no git worktree/repository bookkeeping was
    created."""
    state = tmp_path / "state"
    store = make_store(state)
    first = store.heartbeat(1)
    first_bytes = (state / HEARTBEAT_FILE).read_bytes()
    second = store.heartbeat(2)  # the update path: another beat

    files = sorted(
        p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file()
    )
    assert files == [HEARTBEAT_FILE]
    updated = (state / HEARTBEAT_FILE).read_bytes()
    assert updated != first_bytes
    assert json.loads(updated)["watched_run_count"] == 2
    assert first.heartbeat_at == second.heartbeat_at == FIXED_STAMP
    probe = subprocess.run(
        ["git", "-C", str(state), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0


def test_ac02_checkpoint_and_heartbeat_are_plain_json_state_files(
    tmp_path: Path,
) -> None:
    """AC-02: the checkpoint and the heartbeat are plain JSON durable
    state files carrying their record version -- nothing else is ever
    created in the state directory."""
    state = tmp_path / "state"
    store = make_store(state)
    store.save(
        make_checkpoint(store.monitor_id, entries=(make_entry(1),))
    )
    store.heartbeat(1)
    files = sorted(
        p.relative_to(state).as_posix() for p in state.rglob("*") if p.is_file()
    )
    assert files == [CHECKPOINT_FILE, HEARTBEAT_FILE]
    checkpoint = json.loads((state / CHECKPOINT_FILE).read_text(encoding="utf-8"))
    heartbeat = json.loads((state / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert checkpoint["record_version"] == "1.0"
    assert heartbeat["record_version"] == "1.0"
    assert set(checkpoint) == {
        "record_version", "monitor_id", "created_at", "entries"
    }
    assert set(heartbeat) == {
        "record_version", "monitor_id", "heartbeat_at", "watched_run_count"
    }


# ---------------------------------------------------------------------------
# AC-03: the checkpoint references the adapter/external ids needed for
# reconciliation
# ---------------------------------------------------------------------------


def test_ac03_checkpoint_references_external_ids_needed_for_reconciliation(
    tmp_path: Path,
) -> None:
    """AC-03: the persisted checkpoint references the adapter/external
    ids needed for reconciliation -- backend, dispatch_id, job_id,
    working directory -- for every entry, so a restarted Monitor can
    address the external runs again."""
    state = tmp_path / "state"
    store = make_store(state)
    monitor_id = store.monitor_id
    run_id = make_run_id(1)
    external = RunExternal(
        backend="slurm_ssh",
        job_id=generate_id("job", run_id),
        dispatch_id=generate_id("dispatch", run_id),
        working_directory="/home/alice/scratch/work-1",
    )
    entry = MonitorRunCheckpoint(
        run_id=run_id,
        external=external,
        observed_state="RUNNING_EXTERNAL",
        observed_at=FIXED_STAMP,
        reconciled_at=FIXED_STAMP,
    )
    store.save(make_checkpoint(monitor_id, entries=(entry,)))

    # The persisted JSON carries the full external identity under the
    # core external-id vocabulary (backend/dispatch_id/job_id/
    # working_directory).
    raw = json.loads((state / CHECKPOINT_FILE).read_text(encoding="utf-8"))
    persisted_external = raw["entries"][0]["external"]
    assert persisted_external == external.to_dict()
    assert persisted_external["backend"] == "slurm_ssh"
    assert persisted_external["job_id"] == external.job_id
    assert persisted_external["dispatch_id"] == external.dispatch_id
    assert persisted_external["working_directory"] == "/home/alice/scratch/work-1"

    # A fresh store instance recovers the same external identity from
    # the persisted checkpoint alone.
    fresh = make_store(state)
    recovered = fresh.load()
    assert recovered is not None
    assert recovered.entries == (entry,)
    assert recovered.entries[0].external == external
    assert recovered.entries[0].external.backend == "slurm_ssh"
    assert recovered.entries[0].external.dispatch_id == external.dispatch_id
    assert recovered.entries[0].external.job_id == external.job_id


def test_ac03_checkpoint_recoverable_by_fresh_instance(tmp_path: Path) -> None:
    """AC-03: a fresh store instance over the same state directory
    recovers the persisted checkpoint (entries, monitor identity,
    timestamps) -- recovery needs no session state."""
    state = tmp_path / "state"
    store = make_store(state)
    entries = (make_entry(1), make_entry(2), make_entry(3))
    checkpoint = make_checkpoint(store.monitor_id, entries=entries)
    store.save(checkpoint)
    # The persisted checkpoint is recovered in the sorted run-id order
    # the store persists deterministically.
    expected = make_checkpoint(
        store.monitor_id,
        entries=tuple(
            sorted(entries, key=lambda entry: entry.run_id)
        ),
    )

    fresh = make_store(state)
    recovered = fresh.load()
    assert recovered == expected
    assert fresh.monitor_id == store.monitor_id
    assert recovered is not None
    assert [entry.run_id for entry in recovered.entries] == sorted(
        [make_run_id(1), make_run_id(2), make_run_id(3)]
    )
    assert recovered.entries[0].observed_state == "RUNNING_EXTERNAL"
    assert recovered.entries[0].reconciled_at == FIXED_STAMP


def test_ac03_checkpoint_entry_requires_external_identity() -> None:
    """AC-03: a checkpoint entry must reference the external ids needed
    for reconciliation -- entries without the backend or without any
    external id are refused with the stable CheckpointRecordError."""
    run_id = make_run_id(1)
    with pytest.raises(TypeError):
        MonitorRunCheckpoint(
            run_id=run_id, external=None,  # type: ignore[arg-type]
        )
    with pytest.raises(CheckpointRecordError):
        MonitorRunCheckpoint(
            run_id=run_id,
            external=RunExternal(backend="slurm_ssh"),  # no external id
        )
    with pytest.raises(CheckpointRecordError):
        MonitorRunCheckpoint(
            run_id=run_id,
            external=RunExternal(
                job_id=generate_id("job", run_id)  # no backend
            ),
        )
    with pytest.raises(CheckpointRecordError):
        MonitorRunCheckpoint(run_id="not-an-id", external=make_external())
    with pytest.raises(TypeError):
        MonitorRunCheckpoint(run_id=42, external=make_external())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Checkpoint record contract
# ---------------------------------------------------------------------------


def test_checkpoint_save_load_roundtrip(tmp_path: Path) -> None:
    """save/load round-trip a full checkpoint through the durable file."""
    state = tmp_path / "state"
    store = make_store(state)
    entries = (make_entry(1), make_entry(2))
    checkpoint = make_checkpoint(
        store.monitor_id,
        entries=entries,
        created_at=FIXED_STAMP,
    )
    store.save(checkpoint)
    # The store persists entries in sorted run-id order, so the reloaded
    # checkpoint is the order-normalized form of the saved one.
    expected = make_checkpoint(
        store.monitor_id,
        entries=tuple(sorted(entries, key=lambda entry: entry.run_id)),
        created_at=FIXED_STAMP,
    )
    assert store.load() == expected
    assert MonitorCheckpoint.from_dict(expected.to_dict()) == expected


def test_checkpoint_load_absent_returns_none(tmp_path: Path) -> None:
    """load/load_heartbeat return None before anything was written."""
    store = make_store(tmp_path / "state")
    assert store.load() is None
    assert store.load_heartbeat() is None


def test_checkpoint_corrupt_file_raises_stable_error(tmp_path: Path) -> None:
    """A corrupt checkpoint file (garbage, non-object JSON, or a file
    failing the contract) fails load loudly with the stable
    CheckpointRecordError (a ValueError subclass)."""
    state = tmp_path / "state"
    store = make_store(state)
    path = state / CHECKPOINT_FILE
    store.save(make_checkpoint(store.monitor_id, entries=(make_entry(1),)))
    good = json.loads(path.read_text(encoding="utf-8"))

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        store.load()
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        store.load()

    def write(**changes: object) -> None:
        data = dict(good)
        data.update(changes)
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    write(record_version="0.9")
    with pytest.raises(CheckpointRecordError):
        store.load()
    write(monitor_id="bogus")
    with pytest.raises(CheckpointRecordError):
        store.load()
    write(entries="nope")
    with pytest.raises(CheckpointRecordError):
        store.load()
    write(entries=[{"run_id": "bogus", "external": {"backend": "x", "job_id": "j"}}])
    with pytest.raises(CheckpointRecordError):
        store.load()
    del good["entries"]
    path.write_text(json.dumps(good, sort_keys=True), encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        store.load()


def test_checkpoint_rejects_save_under_wrong_monitor(tmp_path: Path) -> None:
    """A store only persists its own monitor's checkpoint: saving a
    checkpoint of another monitor is refused without writing anything."""
    state = tmp_path / "state"
    store = make_store(state)
    other_monitor = generate_id("monitor", "some-other-state-dir")
    with pytest.raises(CheckpointRecordError):
        store.save(make_checkpoint(other_monitor, entries=(make_entry(1),)))
    assert store.load() is None
    with pytest.raises(TypeError):
        store.save("not a checkpoint")  # type: ignore[arg-type]


def test_checkpoint_entries_persisted_in_sorted_run_id_order(
    tmp_path: Path,
) -> None:
    """Checkpoint entries are persisted in sorted run-id order
    (deterministic bytes) regardless of the in-memory order."""
    state = tmp_path / "state"
    store = make_store(state)
    run_ids = (make_run_id(3), make_run_id(1), make_run_id(2))
    unsorted = tuple(make_entry(i) for i in (3, 1, 2))
    store.save(make_checkpoint(store.monitor_id, entries=unsorted))
    raw = json.loads((state / CHECKPOINT_FILE).read_text(encoding="utf-8"))
    persisted_run_ids = [entry["run_id"] for entry in raw["entries"]]
    assert persisted_run_ids == sorted(run_ids)
    recovered = store.load()
    assert recovered is not None
    assert [entry.run_id for entry in recovered.entries] == sorted(run_ids)


def test_checkpoint_byte_identical_for_identical_inputs(tmp_path: Path) -> None:
    """Identical injected inputs produce byte-identical checkpoint and
    heartbeat files (canonical sorted JSON, fixed clock) -- no
    randomness, no wall clock."""
    dirs = (tmp_path / "a", tmp_path / "b")
    # The same injected monitor identity for both stores: with the state
    # directories the only difference, the inputs are identical.
    monitor_id = generate_id("monitor", "determinism-probe")
    payloads: list[bytes] = []
    heartbeats: list[bytes] = []
    for state in dirs:
        store = MonitorCheckpointStore(
            state, now=FakeClock(FIXED_STAMP), monitor_id=monitor_id
        )
        store.save(
            make_checkpoint(
                monitor_id,
                entries=(make_entry(2), make_entry(1)),
            )
        )
        store.heartbeat(2)
        payloads.append((state / CHECKPOINT_FILE).read_bytes())
        heartbeats.append((state / HEARTBEAT_FILE).read_bytes())
    assert payloads[0] == payloads[1]
    assert heartbeats[0] == heartbeats[1]
    persisted_run_ids = [
        entry["run_id"] for entry in json.loads(payloads[0])["entries"]
    ]
    assert sorted(persisted_run_ids) == sorted([make_run_id(1), make_run_id(2)])


# ---------------------------------------------------------------------------
# Heartbeat record contract
# ---------------------------------------------------------------------------


def test_heartbeat_uses_injected_clock(tmp_path: Path) -> None:
    """The heartbeat is stamped from the injected clock (no wall clock)
    and records the watched-run count."""
    state = tmp_path / "state"
    clock = FakeClock(FIXED_STAMP)
    store = make_store(state, clock)
    record = store.heartbeat(3)
    assert record.heartbeat_at == FIXED_STAMP
    assert record.watched_run_count == 3
    assert is_valid_id(record.monitor_id, "monitor")
    assert record.monitor_id == store.monitor_id
    assert clock.calls == [FIXED_STAMP]
    raw = json.loads((state / HEARTBEAT_FILE).read_text(encoding="utf-8"))
    assert raw["watched_run_count"] == 3
    assert raw["heartbeat_at"] == FIXED_STAMP


def test_heartbeat_record_rejects_bad_counts(tmp_path: Path) -> None:
    """The heartbeat contract refuses non-int counts (TypeError) and
    negative counts (CheckpointRecordError)."""
    store = make_store(tmp_path / "state")
    with pytest.raises(TypeError):
        store.heartbeat("3")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.heartbeat(True)  # type: ignore[arg-type]
    with pytest.raises(CheckpointRecordError):
        store.heartbeat(-1)
    with pytest.raises(CheckpointRecordError):
        HeartbeatRecord.from_dict(
            {
                "record_version": "1.0",
                "monitor_id": store.monitor_id,
                "heartbeat_at": FIXED_STAMP,
                "watched_run_count": -1,
            }
        )


def test_heartbeat_from_dict_roundtrip() -> None:
    """The heartbeat round-trips through to_dict/from_dict."""
    record = HeartbeatRecord(
        monitor_id=generate_id("monitor", "state-dir-1"),
        heartbeat_at=FIXED_STAMP,
        watched_run_count=2,
    )
    assert HeartbeatRecord.from_dict(record.to_dict()) == record
    # An unknown version is refused with the stable contract error.
    with pytest.raises(CheckpointRecordError):
        HeartbeatRecord.from_dict(
            {
                "record_version": "0.9",
                "monitor_id": record.monitor_id,
                "heartbeat_at": FIXED_STAMP,
                "watched_run_count": 1,
            }
        )
    with pytest.raises(CheckpointRecordError):
        HeartbeatRecord.from_dict(
            {
                "record_version": "1.0",
                "monitor_id": "bogus",
                "heartbeat_at": FIXED_STAMP,
                "watched_run_count": 1,
            }
        )
    with pytest.raises(TypeError):
        HeartbeatRecord.from_dict("not a mapping")  # type: ignore[arg-type]


def test_heartbeat_corrupt_file_raises_stable_error(tmp_path: Path) -> None:
    """A corrupt heartbeat file fails load_heartbeat loudly with the
    stable CheckpointRecordError."""
    state = tmp_path / "state"
    store = make_store(state)
    store.heartbeat(1)
    path = state / HEARTBEAT_FILE
    path.write_text("garbage", encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        store.load_heartbeat()
    path.write_text(json.dumps({"monitor_id": store.monitor_id}), encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        store.load_heartbeat()


# ---------------------------------------------------------------------------
# No secrets
# ---------------------------------------------------------------------------


def test_checkpoint_persisted_records_never_carry_credentials(
    tmp_path: Path,
) -> None:
    """The checkpoint and heartbeat hold external ids only: no
    credential-shaped field name or value ever appears in any persisted
    byte (the no-secrets discipline, walked over every state file)."""
    state = tmp_path / "state"
    store = make_store(state)
    store.save(
        make_checkpoint(
            store.monitor_id,
            entries=(
                make_entry(
                    1,
                    external=make_external(
                        backend="lab",
                        dispatch_id=generate_id("dispatch", make_run_id(1)),
                        working_directory="C:/lab/outgoing/user-9/run-1",
                    ),
                ),
            ),
        )
    )
    store.heartbeat(1)
    bytes_ = b"".join(path.read_bytes() for path in state.rglob("*") if path.is_file())
    lowered = bytes_.decode("utf-8").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered, (
            f"persisted checkpoint/heartbeat must never carry {forbidden!r}"
        )
