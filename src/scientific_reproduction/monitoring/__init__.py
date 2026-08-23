"""
monitoring subsystem package (DEV-M8-G01).

The Execution Monitor's durable-state vocabulary, the foundation of the
M8 monitoring subsystem:

* :mod:`scientific_reproduction.monitoring.registry` -- the Monitor-owned
  watched-Run registry: one persisted entry per external Run the Monitor
  is watching, so a restart reconstructs the watch set from persisted
  state alone (AC-01 of DEV-M8-G01).
* :mod:`scientific_reproduction.monitoring.checkpoint` -- the monitor
  recovery checkpoint (the Monitor's reconciliation progress, referencing
  the adapter/external ids needed for reconciliation, AC-03) and the
  heartbeat record (AC-02). Both are plain durable state files written
  atomically -- they never involve git.
* :mod:`scientific_reproduction.monitoring.watchdog` -- the watchdog
  layer of the Monitor's high-availability chain (13-EXECUTION-MONITOR.md
  SS3): a deterministic ALIVE/DEAD verdict evaluated from the persisted
  heartbeat, the injected clock and the injected staleness threshold.
* :mod:`scientific_reproduction.monitoring.supervisor_inbox` -- the
  durable supervisor inbox of arrived Result Packages (issue #163): one
  plain state-file entry per completed Run at
  ``<state_dir>/supervisor-inbox/<run_id>.json`` (run id, dispatch id,
  completion event id, injected timestamp, pending flag) that surfaces
  the arrival to the Supervisor.
"""

from scientific_reproduction.monitoring.checkpoint import (
    CHECKPOINT_FILE,
    CHECKPOINT_VERSION,
    HEARTBEAT_FILE,
    HEARTBEAT_VERSION,
    CheckpointRecordError,
    HeartbeatRecord,
    MonitorCheckpoint,
    MonitorCheckpointStore,
    MonitorRunCheckpoint,
)
from scientific_reproduction.monitoring.registry import (
    MONITOR_ID_KIND,
    WATCH_RECORD_VERSION,
    WATCHED_STATE_DIR,
    DuplicateWatchError,
    MonitoringClock,
    MonitoringError,
    WatchedRunRecord,
    WatchedRunRegistry,
    WatchNotFoundError,
    WatchRecordError,
    derive_monitor_id,
    utc_now,
    validate_external_identity,
)
from scientific_reproduction.monitoring.supervisor_inbox import (
    INBOX_ENTRY_VERSION,
    INBOX_STATE_DIR,
    DuplicateInboxEntryError,
    InboxEntryNotFoundError,
    InboxRecordError,
    SupervisorInbox,
    SupervisorInboxEntry,
)
from scientific_reproduction.monitoring.watchdog import (
    DEFAULT_STALENESS_THRESHOLD_SECONDS,
    MonitorAliveness,
    MonitorAlivenessState,
    MonitorWatchdog,
    WatchdogError,
)

__all__ = [
    "CHECKPOINT_FILE",
    "CHECKPOINT_VERSION",
    "CheckpointRecordError",
    "DEFAULT_STALENESS_THRESHOLD_SECONDS",
    "HEARTBEAT_FILE",
    "HEARTBEAT_VERSION",
    "HeartbeatRecord",
    "INBOX_ENTRY_VERSION",
    "INBOX_STATE_DIR",
    "InboxEntryNotFoundError",
    "InboxRecordError",
    "DuplicateInboxEntryError",
    "MONITOR_ID_KIND",
    "MonitorAliveness",
    "MonitorAlivenessState",
    "MonitorCheckpoint",
    "MonitorCheckpointStore",
    "MonitorRunCheckpoint",
    "MonitorWatchdog",
    "MonitoringClock",
    "MonitoringError",
    "SupervisorInbox",
    "SupervisorInboxEntry",
    "WATCH_RECORD_VERSION",
    "WATCHED_STATE_DIR",
    "WatchdogError",
    "WatchedRunRecord",
    "WatchedRunRegistry",
    "WatchNotFoundError",
    "WatchRecordError",
    "DuplicateWatchError",
    "derive_monitor_id",
    "utc_now",
    "validate_external_identity",
]
