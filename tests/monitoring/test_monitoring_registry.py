"""Tests for the Monitor's watched-Run registry (DEV-M8-G01,
deliverable).

Per-AC coverage, named after the acceptance criteria:

* ``test_ac01_*`` -- AC-01: a **fresh registry instance** over the same
  state directory reconstructs the full watch set from the persisted
  entries alone (``list_watched`` / ``get``), and watch-set mutations
  (heartbeat, unwatch) are durable across the fresh instance -- the
  Monitor can reconstruct the watched external Runs from persisted
  state with no session state.
* ``test_registry_*`` -- the durable watch-entry contract: idempotent
  re-watch, refusal to re-watch a run under a different external
  identity, strict ``from_dict`` rejection of corrupt entries (stable
  ``WatchRecordError``, a ``ValueError`` subclass), byte-identical
  canonical persistence for identical inputs, the injected clock, and
  the no-secrets discipline (persisted entries never carry
  credential-shaped fields).

Determinism: every test injects a :class:`FakeClock` producing the
fixed ``FIXED_STAMP`` timestamp (no wall clock), ``tmp_path`` state
directories, and ``generate_id`` ids. No randomness, no network, no
sleeps anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import RunExternal
from scientific_reproduction.monitoring import (
    DuplicateWatchError,
    MonitoringError,
    WatchedRunRecord,
    WatchedRunRegistry,
    WatchNotFoundError,
    WatchRecordError,
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


def make_record(
    index: int = 1,
    *,
    external: RunExternal | None = None,
    adapter_id: str | None = "adapter:compute/slurm_ssh",
    adapter_version: str | None = "1.0",
    watched_at: str = FIXED_STAMP,
) -> WatchedRunRecord:
    """A deterministic watch entry for run ``index``."""
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
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )


def make_registry(state_dir: Path, clock: FakeClock | None = None) -> WatchedRunRegistry:
    """A registry over ``state_dir`` with the injected fixed clock."""
    return WatchedRunRegistry(state_dir, now=clock or FakeClock())


def read_entry_file(state_dir: Path, run_id: str) -> dict[str, object]:
    """The raw persisted JSON of one watch entry."""
    return json.loads(
        (state_dir / "watched" / f"{run_id}.json").read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# AC-01: reconstruction from persisted state
# ---------------------------------------------------------------------------


def test_ac01_fresh_registry_reconstructs_watch_set_from_persisted_state(
    tmp_path: Path,
) -> None:
    """AC-01: a fresh registry instance over the same state directory
    reconstructs the full watch set -- the external backend, ids,
    working directory, adapter producer and watch metadata -- from the
    persisted entries alone."""
    state = tmp_path / "state"
    registry = make_registry(state)
    # Three watched runs with different external shapes: slurm job id,
    # lab dispatch id, and both ids plus adapter stamps.
    slurm_record = make_record(1)
    lab_record = make_record(
        2,
        external=make_external(
            backend="lab",
            dispatch_id=generate_id("dispatch", make_run_id(2)),
        ),
        adapter_id="adapter:lab/filesystem",
        adapter_version="1.0",
    )
    compute_record = make_record(
        3,
        external=make_external(
            backend="slurm_ssh",
            job_id=generate_id("job", make_run_id(3)),
            dispatch_id=generate_id("dispatch", make_run_id(3)),
            working_directory="/home/alice/scratch/work-3",
        ),
        adapter_id="adapter:compute/slurm_ssh",
        adapter_version="1.0",
    )
    for record in (slurm_record, lab_record, compute_record):
        assert registry.watch(record) == record

    # A fresh registry over the same state directory -- no session state
    # carried over -- reconstructs the whole watch set.
    fresh = make_registry(state)
    expected = tuple(sorted(
        (slurm_record, lab_record, compute_record), key=lambda r: r.run_id
    ))
    assert fresh.list_watched() == expected
    for record in expected:
        assert fresh.get(record.run_id) == record
    assert fresh.monitor_id == registry.monitor_id


def test_ac01_fresh_registry_recovers_heartbeat_and_unwatch(tmp_path: Path) -> None:
    """AC-01: watch-set mutations (heartbeat, unwatch) are durable --
    a fresh registry instance observes them from persisted state
    alone."""
    state = tmp_path / "state"
    registry = make_registry(state)
    first = make_record(1)
    second = make_record(2)
    registry.watch(first)
    registry.watch(second)
    registry.heartbeat(first.run_id)
    registry.unwatch(second.run_id)

    fresh = make_registry(state)
    recovered = fresh.get(first.run_id)
    assert recovered.last_heartbeat_at == FIXED_STAMP
    assert recovered.run_id == first.run_id
    assert fresh.list_watched() == (recovered,)
    with pytest.raises(WatchNotFoundError):
        fresh.get(second.run_id)


def test_ac01_empty_state_directory_yields_empty_watch_set(tmp_path: Path) -> None:
    """AC-01: a registry over a fresh state directory (or one without a
    watched/ directory at all) reports an empty watch set."""
    assert make_registry(tmp_path / "state").list_watched() == ()
    empty_watched = tmp_path / "state2" / "watched"
    empty_watched.mkdir(parents=True)
    assert make_registry(tmp_path / "state2").list_watched() == ()


# ---------------------------------------------------------------------------
# Watch-set semantics (idempotent re-establishment)
# ---------------------------------------------------------------------------


def test_registry_watch_is_idempotent_for_identical_entry(tmp_path: Path) -> None:
    """Re-watching the identical entry is an idempotent no-op (the M8
    recovery discipline: a Monitor re-establishes its watch set without
    error), and the entry file stays byte-identical."""
    state = tmp_path / "state"
    registry = make_registry(state)
    record = make_record(1)
    assert registry.watch(record) == record
    before = (state / "watched" / f"{record.run_id}.json").read_bytes()
    assert registry.watch(record) == record
    after = (state / "watched" / f"{record.run_id}.json").read_bytes()
    assert after == before
    assert registry.list_watched() == (record,)


def test_registry_watch_rejects_same_run_different_external_identity(
    tmp_path: Path,
) -> None:
    """Re-watching a run under a different external identity is refused
    with the stable DuplicateWatchError: changing the identity of a
    watched run requires unwatch first."""
    state = tmp_path / "state"
    registry = make_registry(state)
    run_id = make_run_id(1)
    registry.watch(
        make_record(
            1,
            external=make_external(
                backend="slurm_ssh",
                job_id=generate_id("job", run_id),
            ),
        )
    )
    with pytest.raises(DuplicateWatchError):
        registry.watch(
            make_record(
                1,
                external=make_external(
                    backend="lab",
                    dispatch_id=generate_id("dispatch", run_id),
                ),
            )
        )
    # The persisted entry is untouched by the refused re-watch.
    assert registry.get(run_id).external.backend == "slurm_ssh"


def test_registry_get_unknown_run_raises_watch_not_found(tmp_path: Path) -> None:
    """get/heartbeat on an unwatched run raise the stable
    WatchNotFoundError."""
    state = tmp_path / "state"
    registry = make_registry(state)
    with pytest.raises(WatchNotFoundError):
        registry.get(make_run_id(1))
    with pytest.raises(WatchNotFoundError):
        registry.heartbeat(make_run_id(1))


def test_registry_unwatch_is_idempotent(tmp_path: Path) -> None:
    """unwatch removes the entry and is a no-op for unwatched runs."""
    state = tmp_path / "state"
    registry = make_registry(state)
    record = make_record(1)
    registry.watch(record)
    registry.unwatch(record.run_id)
    with pytest.raises(WatchNotFoundError):
        registry.get(record.run_id)
    assert registry.list_watched() == ()
    assert registry.watched_dir.is_dir()  # the dir stays; only the entry goes
    registry.unwatch(record.run_id)  # idempotent: no error
    registry.unwatch(make_run_id(99))  # never watched: no error


def test_registry_heartbeat_uses_injected_clock(tmp_path: Path) -> None:
    """heartbeat stamps last_heartbeat_at from the injected clock (no
    wall clock) and persists the updated entry."""
    state = tmp_path / "state"
    clock = FakeClock(FIXED_STAMP)
    registry = make_registry(state, clock)
    record = make_record(1)
    registry.watch(record)
    updated = registry.heartbeat(record.run_id)
    assert updated.last_heartbeat_at == FIXED_STAMP
    assert updated.run_id == record.run_id
    assert clock.calls == [FIXED_STAMP]
    assert (
        read_entry_file(state, record.run_id)["last_heartbeat_at"]
        == FIXED_STAMP
    )


# ---------------------------------------------------------------------------
# The durable record contract
# ---------------------------------------------------------------------------


def test_registry_record_requires_valid_run_id() -> None:
    """The watch entry refuses non-str and malformed run ids."""
    external = make_external(job_id="sr_job_00000000000000000000000000000000")
    with pytest.raises(TypeError):
        WatchedRunRecord(run_id=42, external=external, watched_at=FIXED_STAMP)  # type: ignore[arg-type]
    with pytest.raises(WatchRecordError):
        WatchedRunRecord(
            run_id="not-an-id", external=external, watched_at=FIXED_STAMP
        )
    assert not is_valid_id("not-an-id", "run")


def test_registry_record_requires_external_identity() -> None:
    """A watch entry must carry the backend and at least one external
    id -- an entry without an external identity could never be
    reconciled and is refused."""
    run_id = make_run_id(1)
    with pytest.raises(TypeError):
        WatchedRunRecord(run_id=run_id, external=None, watched_at=FIXED_STAMP)  # type: ignore[arg-type]
    with pytest.raises(WatchRecordError):
        # No backend.
        WatchedRunRecord(
            run_id=run_id,
            external=make_external(
                backend=None,
                job_id=generate_id("job", run_id),
            ),
            watched_at=FIXED_STAMP,
        )
    with pytest.raises(WatchRecordError):
        # Backend but no external id at all.
        WatchedRunRecord(
            run_id=run_id,
            external=RunExternal(backend="slurm_ssh"),
            watched_at=FIXED_STAMP,
        )


def test_registry_record_rejects_empty_optional_fields() -> None:
    """Empty optional fields (adapter stamps, heartbeat) are refused by
    the entry contract."""
    run_id = make_run_id(1)
    external = make_external(job_id=generate_id("job", run_id))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord(
            run_id=run_id,
            external=external,
            watched_at=FIXED_STAMP,
            adapter_id="",
        )
    with pytest.raises(WatchRecordError):
        WatchedRunRecord(
            run_id=run_id,
            external=external,
            watched_at=FIXED_STAMP,
            last_heartbeat_at="   ",
        )
    with pytest.raises(WatchRecordError):
        WatchedRunRecord(
            run_id=run_id, external=external, watched_at=""
        )


def test_registry_from_dict_roundtrip() -> None:
    """to_dict/from_dict round-trip a full entry (external identity,
    adapter stamps, watch metadata)."""
    record = make_record(3)
    assert WatchedRunRecord.from_dict(record.to_dict()) == record
    assert WatchedRunRecord.from_dict(
        record.to_dict()
    ).external == record.external


def test_registry_from_dict_rejects_corrupt_entries(tmp_path: Path) -> None:
    """Corrupt persisted entries are rejected with the stable
    WatchRecordError (a ValueError subclass): missing fields, unknown
    version, invalid ids, missing/incomplete external identity and
    mistyped values."""
    record = make_record(1)
    raw = record.to_dict()

    def mutate(**changes: object) -> dict[str, object]:
        data = dict(raw)
        data.update(changes)
        return data

    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict({k: v for k, v in raw.items() if k != "run_id"})
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(mutate(record_version="0.9"))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(mutate(run_id="bogus"))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(mutate(run_id=42))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(mutate(external="slurm_ssh"))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(
            mutate(external={"backend": "slurm_ssh"})  # no external id
        )
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(mutate(watched_at=42))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict(mutate(adapter_id=""))
    with pytest.raises(WatchRecordError):
        WatchedRunRecord.from_dict({k: v for k, v in raw.items() if k != "external"})
    with pytest.raises(TypeError):
        WatchedRunRecord.from_dict("not a mapping")  # type: ignore[arg-type]


def test_registry_corrupt_file_raises_stable_error(tmp_path: Path) -> None:
    """A corrupt watch-entry file on disk (garbage, non-object JSON, or
    an entry failing the contract) fails get/list_watched loudly with
    the stable WatchRecordError -- corrupt persisted state is never
    silently skipped."""
    state = tmp_path / "state"
    registry = make_registry(state)
    run_id = make_run_id(1)
    entry_dir = state / "watched"
    entry_dir.mkdir(parents=True)
    path = entry_dir / f"{run_id}.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(WatchRecordError):
        registry.get(run_id)
    with pytest.raises(WatchRecordError):
        registry.list_watched()

    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(WatchRecordError):
        registry.get(run_id)

    path.write_text(
        json.dumps(
            {"record_version": "1.0", "run_id": run_id, "external": "x"},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(WatchRecordError):
        registry.get(run_id)

    # A foreign file in the watched dir is a corrupt entry too.
    (entry_dir / "stray.json").write_text("{}", encoding="utf-8")
    with pytest.raises(WatchRecordError):
        registry.list_watched()


# ---------------------------------------------------------------------------
# Determinism and secrets
# ---------------------------------------------------------------------------


def test_registry_byte_identical_entries_for_identical_inputs(
    tmp_path: Path,
) -> None:
    """Identical injected inputs produce byte-identical persisted
    entries (canonical sorted JSON, fixed clock) -- no randomness, no
    wall clock."""
    dirs = (tmp_path / "a", tmp_path / "b")
    records = tuple(make_record(i) for i in (2, 1, 3))
    payloads: list[bytes] = []
    for state in dirs:
        clock = FakeClock(FIXED_STAMP)
        registry = make_registry(state, clock)
        for record in records:
            registry.watch(record)
        registry.heartbeat(records[1].run_id)
        persisted = read_entry_file(state, records[1].run_id)
        assert persisted == {**records[1].to_dict(),
                             "last_heartbeat_at": FIXED_STAMP}
        payloads.append(
            (state / "watched" / f"{records[1].run_id}.json").read_bytes()
        )
    assert payloads[0] == payloads[1]
    # The persisted text is exactly the canonical serialization.
    record = WatchedRunRecord.from_dict(read_entry_file(dirs[0], records[1].run_id))
    assert payloads[0] == json.dumps(
        record.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")


def test_registry_persisted_entries_never_carry_credentials(
    tmp_path: Path,
) -> None:
    """The registry holds external ids only: no credential-shaped field
    name or value ever appears in a persisted entry (the no-secrets
    discipline, walked over every persisted byte)."""
    state = tmp_path / "state"
    registry = make_registry(state)
    registry.watch(make_record(1))
    registry.watch(
        make_record(
            2,
            external=make_external(
                backend="lab",
                dispatch_id=generate_id("dispatch", make_run_id(2)),
                working_directory="C:/lab/outgoing/user-9/run-2",
            ),
        )
    )
    bytes_ = b"".join(
        path.read_bytes() for path in (state / "watched").glob("*.json")
    )
    lowered = bytes_.decode("utf-8").lower()
    for forbidden in FORBIDDEN_SECRETS:
        assert forbidden not in lowered, (
            f"persisted watch entries must never carry {forbidden!r}"
        )


# ---------------------------------------------------------------------------
# Type boundaries and the error hierarchy
# ---------------------------------------------------------------------------


def test_registry_type_boundaries(tmp_path: Path) -> None:
    """TypeError at the public type boundaries."""
    with pytest.raises(TypeError):
        WatchedRunRegistry(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        WatchedRunRegistry(tmp_path, now="not callable")  # type: ignore[arg-type]
    registry = make_registry(tmp_path)
    with pytest.raises(TypeError):
        registry.watch("not a record")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.get(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.heartbeat(42)  # type: ignore[arg-type]
    with pytest.raises(WatchRecordError):
        registry.get("bad id")


def test_registry_error_hierarchy_is_value_error_based() -> None:
    """The monitoring error hierarchy is ValueError-based with stable
    subclasses (the house paradigm for durable-state errors)."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(WatchRecordError, MonitoringError)
    assert issubclass(WatchNotFoundError, MonitoringError)
    assert issubclass(DuplicateWatchError, MonitoringError)


def test_registry_monitor_id_is_deterministic_per_state_dir(
    tmp_path: Path,
) -> None:
    """The default monitor identity is a deterministic function of the
    state directory (no randomness): the same directory always yields
    the same valid sr_monitor id, and a different directory yields a
    different one."""
    state = tmp_path / "state"
    first = make_registry(state)
    second = make_registry(state)
    other = make_registry(tmp_path / "other")
    assert first.monitor_id == second.monitor_id
    assert is_valid_id(first.monitor_id, "monitor")
    assert first.monitor_id != other.monitor_id
