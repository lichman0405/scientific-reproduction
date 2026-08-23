"""The deterministic Monitor liveness watchdog (13-EXECUTION-MONITOR.md
§3, deliverable).

The **watchdog layer** of the Monitor's high-availability chain
(``13-EXECUTION-MONITOR.md`` section 3): a minimal, non-scientific
primitive that checks whether the Monitor is alive by evaluating the
staleness of its persisted heartbeat -- the plain durable state file
``<state_dir>/heartbeat.json`` written by
:meth:`MonitorCheckpointStore.heartbeat` (DEV-M8-G01). It never
restarts, resumes or replaces anything itself (that is the platform
adapter's concern): it returns a deterministic
:class:`MonitorAliveness` verdict the supervisor or platform layer can
act on.

Determinism
-----------
The verdict is a pure function of the persisted heartbeat, the injected
clock (``now``) and the injected staleness threshold: identical state
and inputs always produce the identical verdict, with no wall clock in
the tested path. Evaluation writes nothing -- the watchdog only reads
through :meth:`MonitorCheckpointStore.load_heartbeat` (its first real
consumer in ``src/``).

Verdict rules
-------------
* a fresh heartbeat (staleness ``<= stale_after`` seconds) -> ``ALIVE``;
* a heartbeat older than the threshold -> ``DEAD`` (deterministic);
* no heartbeat file at all -> ``DEAD`` (no heartbeat ever = not
  alive);
* a corrupt ``heartbeat.json`` -> loud :class:`CheckpointRecordError`
  (house fail-loud), never a silent verdict.

Discipline
----------
Timestamps are parsed from the house ISO-8601 UTC format
(``YYYY-MM-DDTHH:MM:SS+00:00``): a heartbeat timestamp that cannot be
parsed (or is not timezone-aware) is corrupt record state and fails
loud. Errors follow the house paradigm: ``TypeError`` at type
boundaries, the stable ``MonitoringError`` (``ValueError`` subclass)
hierarchy otherwise. The monitoring subsystem does not import from the
adapters package.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.monitoring.checkpoint import (
    CheckpointRecordError,
    MonitorCheckpointStore,
)
from scientific_reproduction.monitoring.registry import (
    MONITOR_ID_KIND,
    MonitoringClock,
    MonitoringError,
    utc_now,
)

__all__ = [
    "DEFAULT_STALENESS_THRESHOLD_SECONDS",
    "MonitorAliveness",
    "MonitorAlivenessState",
    "MonitorWatchdog",
    "WatchdogError",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Default staleness threshold in seconds: a Monitor whose heartbeat is
#: strictly older than this when evaluated is DEAD. Sized generously for
#: a 24x7 session heartbeating on the order of minutes; callers (the
#: supervisor / platform layer) should size it to their own heartbeat
#: cadence.
DEFAULT_STALENESS_THRESHOLD_SECONDS: float = 300.0


# ---------------------------------------------------------------------------
# Errors and the verdict vocabulary
# ---------------------------------------------------------------------------


class WatchdogError(MonitoringError):
    """Raised for watchdog contract violations: an unusable staleness
    threshold, an injected clock producing a timestamp the staleness
    cannot be computed from, contradictory constructor arguments, or
    invalid verdict fields."""


class MonitorAlivenessState(StrEnum):
    """The deterministic alive/dead verdict values."""

    ALIVE = "ALIVE"
    DEAD = "DEAD"


@dataclass(frozen=True)
class MonitorAliveness:
    """The deterministic watchdog verdict: whether the Monitor is
    ``ALIVE`` or ``DEAD``, with the evaluated heartbeat timestamp and
    the staleness (seconds between the heartbeat and the evaluation
    time -- the injected clock) in the record.

    ``heartbeat_at``/``staleness`` are both None exactly when no
    heartbeat was ever written (a Monitor with no heartbeat at all is
    DEAD by definition). ``checked_at`` is the injected evaluation
    timestamp and ``monitor_id`` the identity being evaluated.
    """

    status: MonitorAlivenessState
    checked_at: str
    heartbeat_at: str | None
    staleness: float | None
    monitor_id: str
    watched_run_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, MonitorAlivenessState):
            raise TypeError(
                "MonitorAliveness.status must be a MonitorAlivenessState,"
                f" got {type(self.status).__name__}"
            )
        if not isinstance(self.checked_at, str) or not self.checked_at.strip():
            raise WatchdogError(
                "MonitorAliveness.checked_at must be a non-empty timestamp"
                f" string, got {self.checked_at!r}"
            )
        if self.heartbeat_at is not None and (
            not isinstance(self.heartbeat_at, str)
            or not self.heartbeat_at.strip()
        ):
            raise WatchdogError(
                "MonitorAliveness.heartbeat_at must be a non-empty timestamp"
                f" string when set, got {self.heartbeat_at!r}"
            )
        if self.staleness is not None and (
            isinstance(self.staleness, bool)
            or not isinstance(self.staleness, (int, float))
        ):
            raise TypeError(
                "MonitorAliveness.staleness must be a number when set, got"
                f" {type(self.staleness).__name__}"
            )
        if (self.heartbeat_at is None) != (self.staleness is None):
            raise WatchdogError(
                "MonitorAliveness.heartbeat_at and staleness must both be"
                " set or both be None (no heartbeat ever written)"
            )
        if not isinstance(self.monitor_id, str):
            raise TypeError(
                "MonitorAliveness.monitor_id must be a str, got"
                f" {type(self.monitor_id).__name__}"
            )
        if not is_valid_id(self.monitor_id, MONITOR_ID_KIND):
            raise WatchdogError(
                f"verdict monitor_id {self.monitor_id!r} is not a valid"
                " monitor id (sr_monitor_<32 hex chars>)"
            )
        if self.watched_run_count is not None and (
            isinstance(self.watched_run_count, bool)
            or not isinstance(self.watched_run_count, int)
        ):
            raise TypeError(
                "MonitorAliveness.watched_run_count must be an int when"
                f" set, got {type(self.watched_run_count).__name__}"
            )
        if self.watched_run_count is not None and self.watched_run_count < 0:
            raise WatchdogError(
                "MonitorAliveness.watched_run_count must be >= 0, got"
                f" {self.watched_run_count}"
            )

    @property
    def alive(self) -> bool:
        """True iff the verdict is ALIVE."""
        return self.status is MonitorAlivenessState.ALIVE


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------


def _parse_timestamp(
    value: str, *, what: str, error: type[MonitoringError]
) -> datetime:
    """Parse a house timestamp for staleness arithmetic; fail loud with
    ``error`` when it is unusable (not ISO-8601, or not timezone-aware
    -- the house format is ``YYYY-MM-DDTHH:MM:SS+00:00``)."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise error(
            f"cannot evaluate staleness: {what} {value!r} is not a valid"
            " ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise error(
            f"cannot evaluate staleness: {what} {value!r} is not"
            " timezone-aware (the house format is"
            " YYYY-MM-DDTHH:MM:SS+00:00)"
        )
    return parsed


class MonitorWatchdog:
    """The deterministic liveness watchdog (13-EXECUTION-MONITOR.md
    §3).

    ``evaluate()`` reads the persisted heartbeat through
    :meth:`MonitorCheckpointStore.load_heartbeat` and returns a
    deterministic :class:`MonitorAliveness` verdict computed from the
    injected clock and staleness threshold: a fresh heartbeat
    (staleness <= ``stale_after`` seconds) is ALIVE, a heartbeat
    strictly older than the threshold is DEAD, and no heartbeat file
    at all is DEAD by definition (no heartbeat ever = not alive). A
    corrupt ``heartbeat.json`` fails loudly with the store's
    ``CheckpointRecordError`` -- never a silent verdict.

    The watchdog only reads: evaluation never writes anything (the
    tests prove the state directory stays byte-identical), and it
    never restarts or resumes the Monitor -- that is the platform
    adapter's concern; the verdict is what the supervisor or platform
    layer acts on.

    Args:
        state_dir: the monitor's durable state directory
            (``heartbeat.json`` at its root), or a
            ``MonitorCheckpointStore`` over it -- the watchdog then
            reads through that exact store.
        now: injectable clock producing a timestamp string (default
            ``utc_now``, mirroring the checkpoint store) -- the
            evaluation timestamp; no wall clock in the tested path.
        stale_after: the staleness threshold in seconds (default
            ``DEFAULT_STALENESS_THRESHOLD_SECONDS``).
        monitor_id: the Monitor identity to evaluate
            (``sr_monitor_<32 hex>``). Only valid together with a
            ``state_dir`` path; an injected store carries its own
            identity. Defaults to the deterministic identity of the
            state directory.

    Raises:
        TypeError: ``state_dir`` is neither a str/Path nor a
            ``MonitorCheckpointStore``, or ``now`` is not callable, or
            ``stale_after`` is not a number.
        WatchdogError: ``stale_after`` is negative or not finite, or
            ``monitor_id`` is injected together with a store.
        MonitoringError: an injected ``monitor_id`` is not a valid
            monitor id.
    """

    def __init__(
        self,
        state_dir: str | Path | MonitorCheckpointStore,
        *,
        now: MonitoringClock | None = None,
        stale_after: float = DEFAULT_STALENESS_THRESHOLD_SECONDS,
        monitor_id: str | None = None,
    ) -> None:
        if now is not None and not callable(now):
            raise TypeError(
                f"now must be callable, got {type(now).__name__}"
            )
        if isinstance(stale_after, bool) or not isinstance(
            stale_after, (int, float)
        ):
            raise TypeError(
                "stale_after must be a number of seconds, got"
                f" {type(stale_after).__name__}"
            )
        if stale_after < 0 or not math.isfinite(stale_after):
            raise WatchdogError(
                "stale_after must be a finite non-negative number of"
                f" seconds, got {stale_after!r}"
            )
        self._now_fn = now if now is not None else utc_now
        self._stale_after = float(stale_after)
        if isinstance(state_dir, MonitorCheckpointStore):
            if monitor_id is not None:
                raise WatchdogError(
                    "monitor_id cannot be injected together with a"
                    " MonitorCheckpointStore; the store carries its own"
                    " identity"
                )
            self._store = state_dir
        else:
            if not isinstance(state_dir, (str, Path)):
                raise TypeError(
                    "state_dir must be a str/Path or a"
                    " MonitorCheckpointStore, got"
                    f" {type(state_dir).__name__}"
                )
            self._store = MonitorCheckpointStore(
                state_dir, now=self._now_fn, monitor_id=monitor_id
            )

    # -- identity and injected configuration ---------------------------------

    @property
    def store(self) -> MonitorCheckpointStore:
        """The checkpoint store the watchdog reads the heartbeat
        through."""
        return self._store

    @property
    def stale_after(self) -> float:
        """The staleness threshold in seconds."""
        return self._stale_after

    # -- the verdict ----------------------------------------------------------

    def evaluate(self) -> MonitorAliveness:
        """Evaluate liveness deterministically and return the verdict.

        Reads the persisted heartbeat through
        :meth:`MonitorCheckpointStore.load_heartbeat`; the evaluation
        timestamp comes from the injected clock. Writes nothing.

        Raises:
            CheckpointRecordError: the stored heartbeat is corrupt
                (unreadable JSON, a contract violation, or a timestamp
                the staleness cannot be computed from) -- loud fail,
                never a silent verdict.
            WatchdogError: the injected clock produced a timestamp the
                staleness cannot be computed from.
        """
        checked_at = self._now_fn()
        record = self._store.load_heartbeat()
        if record is None:
            return MonitorAliveness(
                status=MonitorAlivenessState.DEAD,
                checked_at=checked_at,
                heartbeat_at=None,
                staleness=None,
                monitor_id=self._store.monitor_id,
            )
        staleness = self._staleness_seconds(record.heartbeat_at, checked_at)
        status = (
            MonitorAlivenessState.DEAD
            if staleness > self._stale_after
            else MonitorAlivenessState.ALIVE
        )
        return MonitorAliveness(
            status=status,
            checked_at=checked_at,
            heartbeat_at=record.heartbeat_at,
            staleness=staleness,
            monitor_id=record.monitor_id,
            watched_run_count=record.watched_run_count,
        )

    def _staleness_seconds(self, heartbeat_at: str, checked_at: str) -> float:
        """Seconds between the heartbeat and the evaluation time (the
        injected clock).

        Raises:
            CheckpointRecordError: the heartbeat timestamp is corrupt
                (not a timezone-aware ISO-8601 timestamp).
            WatchdogError: the injected clock produced an unusable
                timestamp.
        """
        beat = _parse_timestamp(
            heartbeat_at,
            what="heartbeat timestamp",
            error=CheckpointRecordError,
        )
        now_dt = _parse_timestamp(
            checked_at,
            what="injected clock timestamp",
            error=WatchdogError,
        )
        return (now_dt - beat).total_seconds()
