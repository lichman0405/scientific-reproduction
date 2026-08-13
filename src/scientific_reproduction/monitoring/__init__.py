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

__all__ = [
    "CHECKPOINT_FILE",
    "CHECKPOINT_VERSION",
    "CheckpointRecordError",
    "HEARTBEAT_FILE",
    "HEARTBEAT_VERSION",
    "HeartbeatRecord",
    "MONITOR_ID_KIND",
    "MonitorCheckpoint",
    "MonitorCheckpointStore",
    "MonitorRunCheckpoint",
    "MonitoringClock",
    "MonitoringError",
    "WATCH_RECORD_VERSION",
    "WATCHED_STATE_DIR",
    "WatchedRunRecord",
    "WatchedRunRegistry",
    "WatchNotFoundError",
    "WatchRecordError",
    "DuplicateWatchError",
    "derive_monitor_id",
    "utc_now",
    "validate_external_identity",
]
