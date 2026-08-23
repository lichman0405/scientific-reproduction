"""Tests for the deterministic Monitor liveness watchdog
(13-EXECUTION-MONITOR.md §3, deliverable).

Covers the watchdog layer of the Monitor's high-availability chain:
the deterministic ALIVE/DEAD verdict evaluated from the persisted
heartbeat, the injected clock and the injected staleness threshold.

* ``test_verdict_*`` -- the verdict rules: a fresh heartbeat is ALIVE
  (including staleness exactly at the threshold), a heartbeat older
  than the threshold is DEAD, no heartbeat file at all is DEAD (no
  heartbeat ever = not alive), and a corrupt ``heartbeat.json`` fails
  loudly with the stable ``CheckpointRecordError`` -- never a silent
  verdict.
* ``test_determinism_*`` / ``test_discipline_*`` -- determinism with
  the injected clock (identical state and inputs always produce the
  identical verdict), the injected clock as the only timestamp source,
  evaluation writes nothing, and the watchdog reads through
  ``MonitorCheckpointStore.load_heartbeat`` (its first real consumer
  in ``src/``).

Determinism: every test injects :class:`FakeClock` clocks producing
fixed ISO-8601 UTC stamps, ``tmp_path`` state directories and
``generate_id`` ids. No randomness, no network, no sleeps anywhere.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.monitoring import (
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    HEARTBEAT_FILE,
    CheckpointRecordError,
    MonitorAliveness,
    MonitorAlivenessState,
    MonitorCheckpointStore,
    MonitoringError,
    MonitorWatchdog,
    WatchdogError,
)

#: Every injected timestamp is derived from this fixed value (no wall
#: clock anywhere).
FIXED_STAMP = "2026-08-14T00:00:00+00:00"


class FakeClock:
    """Injectable clock: one fixed stamp repeats forever and every
    read is recorded (mirrors the checkpoint tests' FakeClock)."""

    def __init__(self, stamp: str = FIXED_STAMP) -> None:
        self._stamp = stamp
        self.calls: list[str] = []

    def __call__(self) -> str:
        self.calls.append(self._stamp)
        return self._stamp


def stamp_after(seconds: float) -> str:
    """An ISO-8601 UTC stamp ``seconds`` after FIXED_STAMP."""
    base = datetime.fromisoformat(FIXED_STAMP)
    return (base + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def make_store(state_dir: Path, clock: FakeClock | None = None) -> MonitorCheckpointStore:
    """A checkpoint store over ``state_dir`` with the injected fixed
    clock."""
    return MonitorCheckpointStore(state_dir, now=clock or FakeClock())


def beat(state_dir: Path, *, at: str = FIXED_STAMP, count: int = 1) -> None:
    """Write one heartbeat into ``state_dir`` stamped ``at``."""
    make_store(state_dir, FakeClock(at)).heartbeat(count)


class CountingStore(MonitorCheckpointStore):
    """A checkpoint store that counts ``load_heartbeat`` reads."""

    def __init__(self, state_dir: Path, **kwargs: object) -> None:
        super().__init__(state_dir, **kwargs)  # type: ignore[arg-type]
        self.load_heartbeat_calls = 0

    def load_heartbeat(self):  # type: ignore[no-untyped-def]
        self.load_heartbeat_calls += 1
        return super().load_heartbeat()


# ---------------------------------------------------------------------------
# The verdict rules
# ---------------------------------------------------------------------------


def test_verdict_fresh_heartbeat_is_alive(tmp_path: Path) -> None:
    """A fresh heartbeat yields the deterministic ALIVE verdict: at
    staleness 0 and at staleness exactly equal to the threshold (only
    strictly older than the threshold is DEAD), with the heartbeat
    timestamp, staleness, evaluation time, monitor identity and
    watched-run count in the record."""
    state = tmp_path / "state"
    beat(state, count=2)
    store = make_store(state)

    # Staleness 0: evaluated at the heartbeat's own timestamp.
    watchdog = MonitorWatchdog(
        state, now=FakeClock(FIXED_STAMP), stale_after=300.0
    )
    verdict = watchdog.evaluate()
    assert verdict.status is MonitorAlivenessState.ALIVE
    assert verdict.alive is True
    assert verdict.heartbeat_at == FIXED_STAMP
    assert verdict.staleness == 0.0
    assert verdict.checked_at == FIXED_STAMP
    assert verdict.monitor_id == store.monitor_id
    assert verdict.watched_run_count == 2

    # Staleness exactly at the threshold is still alive (the DEAD
    # boundary is strictly older than the threshold).
    watchdog = MonitorWatchdog(
        state, now=FakeClock(stamp_after(300.0)), stale_after=300.0
    )
    verdict = watchdog.evaluate()
    assert verdict.status is MonitorAlivenessState.ALIVE
    assert verdict.staleness == 300.0


def test_verdict_heartbeat_older_than_threshold_is_dead(tmp_path: Path) -> None:
    """A heartbeat older than the injected threshold yields the
    deterministic DEAD verdict, carrying the heartbeat timestamp and
    the staleness in the record."""
    state = tmp_path / "state"
    beat(state, at=FIXED_STAMP, count=3)
    watchdog = MonitorWatchdog(
        state, now=FakeClock(stamp_after(301.0)), stale_after=300.0
    )
    verdict = watchdog.evaluate()
    assert verdict.status is MonitorAlivenessState.DEAD
    assert verdict.alive is False
    assert verdict.heartbeat_at == FIXED_STAMP
    assert verdict.staleness == 301.0
    assert verdict.checked_at == stamp_after(301.0)
    assert verdict.watched_run_count == 3


def test_verdict_future_heartbeat_is_alive(tmp_path: Path) -> None:
    """A heartbeat stamped later than the evaluation time (clock
    skew) is fresh: only a heartbeat strictly older than the
    threshold is DEAD."""
    state = tmp_path / "state"
    beat(state, at=stamp_after(600.0))
    watchdog = MonitorWatchdog(
        state, now=FakeClock(FIXED_STAMP), stale_after=300.0
    )
    verdict = watchdog.evaluate()
    assert verdict.status is MonitorAlivenessState.ALIVE
    assert verdict.staleness == -600.0


def test_verdict_missing_heartbeat_is_dead(tmp_path: Path) -> None:
    """No heartbeat file at all yields the deterministic DEAD verdict
    (no heartbeat ever = not alive), with heartbeat timestamp and
    staleness None -- even when the state directory does not exist
    yet."""
    state = tmp_path / "state"
    store = make_store(state)
    watchdog = MonitorWatchdog(
        state, now=FakeClock(FIXED_STAMP), stale_after=300.0
    )
    verdict = watchdog.evaluate()
    assert verdict.status is MonitorAlivenessState.DEAD
    assert verdict.alive is False
    assert verdict.heartbeat_at is None
    assert verdict.staleness is None
    assert verdict.checked_at == FIXED_STAMP
    assert verdict.monitor_id == store.monitor_id
    assert verdict.watched_run_count is None

    # A state directory that does not exist at all is equally DEAD
    # (the same deterministic verdict).
    watchdog = MonitorWatchdog(
        tmp_path / "does-not-exist",
        now=FakeClock(FIXED_STAMP),
        stale_after=300.0,
    )
    assert watchdog.evaluate().status is MonitorAlivenessState.DEAD


def test_verdict_corrupt_heartbeat_fails_loud(tmp_path: Path) -> None:
    """A corrupt heartbeat file fails the evaluation loudly with the
    stable CheckpointRecordError -- never a silent verdict: unreadable
    bytes, non-object JSON, contract violations, an unparseable
    timestamp and a non-timezone-aware timestamp."""
    state = tmp_path / "state"
    store = make_store(state)
    store.heartbeat(1)
    path = state / HEARTBEAT_FILE
    good = json.loads(path.read_text(encoding="utf-8"))
    watchdog = MonitorWatchdog(
        state, now=FakeClock(FIXED_STAMP), stale_after=300.0
    )

    path.write_text("garbage", encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        watchdog.evaluate()
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(CheckpointRecordError):
        watchdog.evaluate()

    def write(**changes: object) -> None:
        data = dict(good)
        data.update(changes)
        path.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")

    write(record_version="0.9")
    with pytest.raises(CheckpointRecordError):
        watchdog.evaluate()
    write(heartbeat_at="not-a-timestamp")
    with pytest.raises(CheckpointRecordError):
        watchdog.evaluate()
    # A naive timestamp (no timezone) cannot be evaluated
    # deterministically -- corrupt record state, loud error.
    write(heartbeat_at="2026-08-14T00:00:00")
    with pytest.raises(CheckpointRecordError):
        watchdog.evaluate()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_determinism_identical_inputs_produce_identical_verdicts(
    tmp_path: Path,
) -> None:
    """Identical state and identical injected inputs produce the
    identical verdict across fresh watchdog instances over different
    state directories, and repeated evaluation on the same watchdog
    repeats the identical verdict (pure function of the persisted
    heartbeat, the injected clock and the injected threshold)."""
    monitor_id = generate_id("monitor", "determinism-probe")
    dirs = (tmp_path / "a", tmp_path / "b")
    for state in dirs:
        MonitorCheckpointStore(
            state, now=FakeClock(FIXED_STAMP), monitor_id=monitor_id
        ).heartbeat(1)

    verdicts: list[MonitorAliveness] = []
    for state in dirs:
        watchdog = MonitorWatchdog(
            state,
            now=FakeClock(stamp_after(301.0)),
            stale_after=300.0,
            monitor_id=monitor_id,
        )
        verdicts.append(watchdog.evaluate())
        # Repeated evaluation is the identical verdict.
        assert watchdog.evaluate() == verdicts[-1]
    assert verdicts[0] == verdicts[1]
    assert verdicts[0].status is MonitorAlivenessState.DEAD
    assert verdicts[0].staleness == 301.0
    assert verdicts[0].monitor_id == monitor_id


def test_determinism_verdict_uses_only_the_injected_clock(
    tmp_path: Path,
) -> None:
    """The evaluation timestamp comes from the injected clock alone:
    the clock is consulted exactly once per evaluation and its stamp
    is the verdict's checked_at (no wall clock in the tested path)."""
    state = tmp_path / "state"
    beat(state)
    clock = FakeClock(stamp_after(120.0))
    watchdog = MonitorWatchdog(state, now=clock, stale_after=300.0)
    verdict = watchdog.evaluate()
    assert clock.calls == [stamp_after(120.0)]
    assert verdict.checked_at == stamp_after(120.0)
    assert verdict.status is MonitorAlivenessState.ALIVE


# ---------------------------------------------------------------------------
# Discipline: observation-only, reads through the store
# ---------------------------------------------------------------------------


def test_evaluation_writes_nothing(tmp_path: Path) -> None:
    """The watchdog only reads: evaluating liveness leaves the state
    directory byte-identical (the verdict is observation-only, like
    the recovery procedure)."""
    state = tmp_path / "state"
    beat(state, count=1)
    before = sorted(
        (p.relative_to(state).as_posix(), p.read_bytes())
        for p in state.rglob("*")
        if p.is_file()
    )
    watchdog = MonitorWatchdog(
        state, now=FakeClock(stamp_after(600.0)), stale_after=300.0
    )
    assert watchdog.evaluate().status is MonitorAlivenessState.DEAD
    after = sorted(
        (p.relative_to(state).as_posix(), p.read_bytes())
        for p in state.rglob("*")
        if p.is_file()
    )
    assert after == before


def test_evaluation_reads_through_load_heartbeat(tmp_path: Path) -> None:
    """The watchdog evaluates liveness by reading through
    ``MonitorCheckpointStore.load_heartbeat`` -- the store's read path
    is its real consumer in ``src/``."""
    state = tmp_path / "state"
    store = CountingStore(state, now=FakeClock(FIXED_STAMP))
    store.heartbeat(1)
    watchdog = MonitorWatchdog(
        store, now=FakeClock(FIXED_STAMP), stale_after=300.0
    )
    assert watchdog.store is store
    verdict = watchdog.evaluate()
    assert store.load_heartbeat_calls == 1
    assert verdict.status is MonitorAlivenessState.ALIVE


def test_watchdog_accepts_store_or_state_directory(tmp_path: Path) -> None:
    """The watchdog evaluates identically over a state directory path
    and over an injected ``MonitorCheckpointStore`` bound to it."""
    state = tmp_path / "state"
    beat(state, at=FIXED_STAMP, count=1)
    store = make_store(state)
    from_path = MonitorWatchdog(
        state, now=FakeClock(stamp_after(301.0)), stale_after=300.0
    )
    from_store = MonitorWatchdog(
        store, now=FakeClock(stamp_after(301.0)), stale_after=300.0
    )
    assert from_path.evaluate() == from_store.evaluate()
    assert from_store.evaluate().status is MonitorAlivenessState.DEAD


# ---------------------------------------------------------------------------
# Constructor contract and package surface
# ---------------------------------------------------------------------------


def test_constructor_rejects_contract_violations(tmp_path: Path) -> None:
    """The constructor enforces the injected-contract boundaries:
    TypeError at type boundaries, the stable MonitoringError hierarchy
    for value violations."""
    with pytest.raises(TypeError):
        MonitorWatchdog(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MonitorWatchdog(tmp_path, now="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MonitorWatchdog(tmp_path, stale_after="300")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MonitorWatchdog(tmp_path, stale_after=True)  # type: ignore[arg-type]
    with pytest.raises(WatchdogError):
        MonitorWatchdog(tmp_path, stale_after=-1.0)
    with pytest.raises(WatchdogError):
        MonitorWatchdog(tmp_path, stale_after=float("inf"))
    # A store carries its own identity: injecting one on top is
    # contradictory.
    store = make_store(tmp_path / "state")
    with pytest.raises(WatchdogError):
        MonitorWatchdog(store, monitor_id=store.monitor_id)
    # An invalid monitor id is refused with the store's stable error.
    with pytest.raises(CheckpointRecordError):
        MonitorWatchdog(tmp_path / "state", monitor_id="bogus")


def test_watchdog_package_surface() -> None:
    """The watchdog vocabulary is part of the monitoring package
    surface: a stable ValueError-based error hierarchy, the
    ALIVE/DEAD values and the default staleness threshold."""
    from scientific_reproduction import monitoring

    for name in (
        "DEFAULT_STALENESS_THRESHOLD_SECONDS",
        "MonitorAliveness",
        "MonitorAlivenessState",
        "MonitorWatchdog",
        "WatchdogError",
    ):
        assert name in monitoring.__all__, name
        assert hasattr(monitoring, name), name
    assert issubclass(WatchdogError, MonitoringError)
    assert issubclass(CheckpointRecordError, MonitoringError)
    assert MonitorAlivenessState.ALIVE.value == "ALIVE"
    assert MonitorAlivenessState.DEAD.value == "DEAD"
    assert DEFAULT_STALENESS_THRESHOLD_SECONDS > 0
