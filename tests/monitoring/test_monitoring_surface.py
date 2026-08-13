"""Tests for the monitoring package surface (DEV-M8-G01).

The public surface of the monitoring subsystem: the package exports
(``monitoring.__all__``), the ValueError-based error hierarchy shared
by the registry and the checkpoint modules, the injectable clock
contract, and the architectural boundary -- the monitoring subsystem
must not couple to the adapters package (external ids are plain
documented fields of the core ``RunExternal`` vocabulary).
"""

from __future__ import annotations

import re
import subprocess
import sys

from scientific_reproduction import monitoring
from scientific_reproduction.monitoring import (
    CheckpointRecordError,
    DuplicateWatchError,
    MonitoringError,
    WatchNotFoundError,
    WatchRecordError,
    utc_now,
)


def test_monitoring_package_exports_public_api() -> None:
    """Every name in ``monitoring.__all__`` is importable from the
    package (the documented public surface of the subsystem)."""
    assert monitoring.__all__
    for name in monitoring.__all__:
        assert hasattr(monitoring, name), name
    from scientific_reproduction.monitoring import (  # noqa: F401
        CHECKPOINT_FILE,
        CHECKPOINT_VERSION,
        HEARTBEAT_FILE,
        HEARTBEAT_VERSION,
        MonitorCheckpoint,
        MonitorCheckpointStore,
        MonitorRunCheckpoint,
        WatchedRunRecord,
        WatchedRunRegistry,
        derive_monitor_id,
        validate_external_identity,
    )


def test_monitoring_error_hierarchy_is_value_error_based() -> None:
    """The monitoring error hierarchy is ValueError-based (the house
    paradigm for durable-state errors) with stable subclasses."""
    assert issubclass(MonitoringError, ValueError)
    assert issubclass(WatchRecordError, MonitoringError)
    assert issubclass(WatchNotFoundError, MonitoringError)
    assert issubclass(DuplicateWatchError, MonitoringError)
    assert issubclass(CheckpointRecordError, MonitoringError)
    assert WatchRecordError is not WatchNotFoundError
    assert WatchRecordError is not DuplicateWatchError
    assert WatchRecordError is not CheckpointRecordError


def test_utc_now_returns_iso_utc_timestamp() -> None:
    """The default clock produces the documented ISO-8601 UTC timestamp
    shape (the format stamped into durable records)."""
    stamp = utc_now()
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", stamp
    ), stamp


def test_monitoring_imports_do_not_couple_to_adapters() -> None:
    """Importing the monitoring subsystem never pulls in the adapters
    package (proven in a fresh interpreter): the registry and the
    checkpoint carry the external ids as plain documented core
    ``RunExternal`` fields, not adapter types."""
    code = (
        "import sys\n"
        "import scientific_reproduction.monitoring\n"
        "assert 'scientific_reproduction.adapters' not in sys.modules\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
