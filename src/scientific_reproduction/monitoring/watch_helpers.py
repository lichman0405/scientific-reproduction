"""Monitor-side watch authoring helper with referential integrity
(issue #154).

``WatchedRunRegistry.watch`` (``monitoring/registry.py``) is the pure
state-dir primitive: it validates the watch-entry *contract* (run id
format, external identity, record version) and persists -- it never
reads the Run registry. A watch entry could therefore name a run that
does not exist or a run that was never dispatched to an external
backend, and the failure would surface only later, per reconciliation
pass (``CorruptProgressError`` / ``ReconcileContractError`` in
``monitoring/reconcile.py``).

This module ships the Monitor's authoring gate, mirroring the
worker-side authoring helpers of ``workers/run_helpers.py`` (issue
#92): resolve the Run through the run registry first, require a
lifecycle that can be externally watched, and only then write the watch
entry through the injected :class:`WatchedRunRegistry`. The registry
itself is unchanged and stays the pure state-dir primitive (AC-01: a
fresh instance reconstructs the full watch set from persisted entries
alone); this helper is the documented boundary where authoring-time
referential integrity is enforced -- a nonexistent run, or a run that
was never dispatched, fails here with a stable error and persists
nothing, instead of failing every later monitor pass.

Discipline
----------
The acting Monitor identity is the injected registry's ``monitor_id``
(the registry is the Monitor-owned durable record and the carrier of
the Monitor identity). Timestamps and the run reader are injected by
the caller (no wall clock in the tested path); the monitoring subsystem
never imports the adapters package. Errors follow the house paradigm:
``TypeError`` at the public type boundaries, stable ``MonitoringError``
subclasses (a ``ValueError`` hierarchy) otherwise, naming the run id
and the offending state.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeAlias

from scientific_reproduction.core.models import LifecycleState, Run, RunExternal
from scientific_reproduction.monitoring.registry import (
    MonitoringError,
    WatchedRunRecord,
    WatchedRunRegistry,
)
from scientific_reproduction.workers.run_helpers import read_run

__all__ = [
    "WATCHABLE_RUN_STATES",
    "RunReader",
    "UnwatchableRunStateError",
    "watch_run",
]

# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: The Run lifecycle states that can be externally watched: a run is
#: watchable once it has been dispatched to an external backend
#: (``DISPATCHED``) and while it is executing there
#: (``RUNNING_EXTERNAL``). Every other state -- pre-dispatch
#: (``CREATED`` / ``READY``, never dispatched to any backend), later
#: and terminal states (already past external execution) -- is refused
#: at authoring time.
WATCHABLE_RUN_STATES: frozenset[LifecycleState] = frozenset(
    {LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL}
)


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class UnwatchableRunStateError(MonitoringError):
    """Raised when a run is watched in a lifecycle state that cannot be
    externally watched (anything but ``DISPATCHED`` /
    ``RUNNING_EXTERNAL``); the message names the run id and the
    offending state."""


# ---------------------------------------------------------------------------
# The authoring gate
# ---------------------------------------------------------------------------

#: A run reader resolving a run id to its durable record (mirrors
#: ``workers.run_helpers.read_run``).
RunReader: TypeAlias = Callable[[str], Run]


def watch_run(
    root: str | Path,
    run_id: str,
    external: RunExternal,
    *,
    registry: WatchedRunRegistry,
    watched_at: str,
    run_reader: RunReader | None = None,
    adapter_id: str | None = None,
    adapter_version: str | None = None,
) -> WatchedRunRecord:
    """Watch one external Run through the authoring gate (issue #154).

    Resolves the run through the run registry, requires an externally
    watchable lifecycle, and only then writes the watch entry through
    the injected registry. Nothing is persisted when the resolution or
    the lifecycle check fails.

    Args:
        root: the initialized workspace root (unused when
            ``run_reader`` is injected).
        run_id: the id of the run to watch.
        external: the external identity of the dispatched run (backend
            plus ``dispatch_id`` and/or ``job_id`` -- the entry contract
            of :class:`WatchedRunRecord`).
        registry: the injected :class:`WatchedRunRegistry` the entry is
            written through; its ``monitor_id`` is the acting Monitor
            identity.
        watched_at: the injected deterministic watch timestamp.
        run_reader: the reader resolving the run (default:
            ``workers.run_helpers.read_run`` over ``root``).
        adapter_id: optional producing adapter identity.
        adapter_version: optional producing adapter version.

    Returns:
        The persisted :class:`WatchedRunRecord`.

    Raises:
        TypeError: ``root`` is not a str/Path, ``run_id`` is not a str,
            ``external`` is not a ``RunExternal``, ``registry`` is not a
            ``WatchedRunRegistry``, ``watched_at`` is not a str, or
            ``run_reader`` is not callable / does not return a ``Run``.
        RunNotFoundError: no run with that id is registered (the
            default reader; an injected reader raises its own not-found
            error).
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root`` (default reader).
        UnwatchableRunStateError: the run's lifecycle state is not
            externally watchable (not ``DISPATCHED`` /
            ``RUNNING_EXTERNAL``).
        WatchRecordError: the entry violates the watch-entry contract
            (e.g. an incomplete external identity or an empty
            ``watched_at``).
        DuplicateWatchError: the run is already watched with a
            different external identity.
        ValueError: the stored run record is corrupt (default reader).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    if not isinstance(external, RunExternal):
        raise TypeError(
            "external must be a RunExternal, got" f" {type(external).__name__}"
        )
    if not isinstance(registry, WatchedRunRegistry):
        raise TypeError(
            "registry must be a WatchedRunRegistry, got"
            f" {type(registry).__name__}"
        )
    if not isinstance(watched_at, str):
        raise TypeError(
            f"watched_at must be a str, got {type(watched_at).__name__}"
        )
    if run_reader is not None and not callable(run_reader):
        raise TypeError(
            f"run_reader must be callable, got {type(run_reader).__name__}"
        )
    run = run_reader(run_id) if run_reader is not None else read_run(root, run_id)
    if not isinstance(run, Run):
        raise TypeError(
            f"run_reader must return a Run, got {type(run).__name__}"
        )
    if run.lifecycle_state not in WATCHABLE_RUN_STATES:
        raise UnwatchableRunStateError(
            f"run {run_id!r} cannot be watched in lifecycle state"
            f" {run.lifecycle_state.value!r}: only DISPATCHED and"
            " RUNNING_EXTERNAL runs have been dispatched to an external"
            " backend; dispatch the run before watching it"
        )
    record = WatchedRunRecord(
        run_id=run_id,
        external=external,
        watched_at=watched_at,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
    )
    return registry.watch(record)
