"""Dispatch-to-Run record linkage for the LabAdapter (DEV-M7-G01).

The LabAdapter performs the outgoing handoff and **never** touches the
Run record: it is a pure function of the injected handoff paths and has
no knowledge of the project's run store (15-ADAPTER-SPEC.md SS2 "Run
record linkage"). The **dispatching orchestrator** (the Experiment
Worker of 10-EXPERIMENT-SUBSYSTEM.md SS1) owns the linkage: after a
successful ``dispatch`` it must record the returned
:class:`DispatchRecord` on the durable Run record -- ``external.dispatch_id``
and ``external.backend`` -- and advance the Run to
``RUNNING_EXTERNAL`` through the real transition machinery, so the
handoff layer and the Run lifecycle never drift apart.

:func:`link_run_to_dispatch` performs that linkage as one validated
operation over an injected run store: it re-hydrates the Run record
from disk (the M1 recovery discipline), validates the lifecycle advance
through the **real** transition rules, refuses a run already linked to
a different dispatch (never silently re-linked), persists the updated
record through the injected store (which applies the real ``run``
schema gate), and returns the updated ``Run``. Re-linking an
already-external run with the same dispatch id is an idempotent no-op
(the dispatch recovery discipline: the worker may re-issue the linkage
after a crash without error). All timestamps come from the injected
clock (``now``); no wall clock in the tested path.

Errors follow the house paradigm: ``TypeError`` at type boundaries,
``LabAdapterDataError`` (a ``ValueError`` subclass) for linkage
conflicts with stable messages, and the real
:class:`IllegalTransitionError` from ``core.transitions`` for a run
whose lifecycle cannot carry the dispatch (a result-bearing or
terminal Run can never be re-linked to a dispatch).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import TypeAlias

from scientific_reproduction.adapters.lab.base import (
    DispatchRecord,
    LabAdapterDataError,
)
from scientific_reproduction.core.models import LifecycleState, Run, RunExternal
from scientific_reproduction.core.state_backend import StateBackend
from scientific_reproduction.core.transitions import transition

__all__ = [
    "FILESYSTEM_BACKEND_NAME",
    "LinkageClock",
    "link_run_to_dispatch",
]

#: The backend name of the v0.1 filesystem/manual handoff reference
#: adapter (recorded as ``run.external.backend`` by the linkage helper
#: when no other backend is named; matches ``FilesystemLabAdapter.adapter_id``).
FILESYSTEM_BACKEND_NAME: str = "filesystem"

#: The injectable clock of the linkage helper: a callable producing a
#: timestamp string (mirrors the adapters' caller-injected timestamps).
LinkageClock: TypeAlias = Callable[[], str]


def _utc_now() -> str:
    """The default clock: current UTC time as an ISO-8601 timestamp
    string (``YYYY-MM-DDTHH:MM:SS+00:00``)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def link_run_to_dispatch(
    run_store: StateBackend,
    dispatch: DispatchRecord,
    *,
    backend: str = FILESYSTEM_BACKEND_NAME,
    now: LinkageClock | None = None,
) -> Run:
    """Link one dispatch to its Run record and persist the linkage.

    The linkage is the orchestrator's half of the outgoing handoff
    (15-ADAPTER-SPEC.md SS2): the adapter writes the handoff, the caller
    performs the linkage immediately after a successful ``dispatch``.
    The Run record is re-hydrated from the injected ``run_store`` by
    ``dispatch.run_id``, the lifecycle is advanced through the **real**
    transition machinery (``READY`` walks the mainline through
    ``DISPATCHED``; ``DISPATCHED`` advances to ``RUNNING_EXTERNAL``;
    an already-``RUNNING_EXTERNAL`` run is an idempotent re-link), the
    external identity records the dispatch (``backend`` + the
    ``DispatchRecord.dispatch_id``, preserving any existing ``job_id`` /
    ``working_directory``), and the updated record is persisted through
    the store's real ``run`` schema gate. A run whose external identity
    already names a **different** dispatch is refused loudly, never
    silently re-linked; a run whose lifecycle cannot carry the dispatch
    (result-bearing or terminal) is refused by the transition machinery.

    Args:
        run_store: the injected run store (the ``runs/`` state backend
            of the project workspace; ``write`` applies the real ``run``
            schema gate).
        dispatch: the :class:`DispatchRecord` returned by a successful
            ``dispatch`` -- its ``run_id`` selects the Run record and
            its ``dispatch_id`` is the external identity recorded.
        backend: the external backend name recorded as
            ``run.external.backend`` (defaults to
            :data:`FILESYSTEM_BACKEND_NAME`, the v0.1 reference adapter).
        now: injectable clock producing the ``updated_at`` stamp
            (default: ``_utc_now`` -- tests inject a fixed clock).

    Returns:
        The updated :class:`Run` (the persisted record).

    Raises:
        TypeError: ``run_store`` is not a ``StateBackend``, ``dispatch``
            is not a ``DispatchRecord``, or ``now`` is not callable.
        LabAdapterDataError: ``backend`` is not a non-empty string, or
            the Run record's external identity already names a different
            ``dispatch_id`` (the run is linked to another dispatch).
        FileNotFoundError: no Run record exists for ``dispatch.run_id``.
        ValueError: the stored Run record is corrupt (from the store's
            ``read`` / ``Run.from_dict``).
        IllegalTransitionError: the Run's lifecycle state cannot carry
            the dispatch (the real transition rules refuse it).
    """
    if not isinstance(run_store, StateBackend):
        raise TypeError(
            "run_store must be a StateBackend, got"
            f" {type(run_store).__name__}"
        )
    if not isinstance(dispatch, DispatchRecord):
        raise TypeError(
            "dispatch must be a DispatchRecord, got"
            f" {type(dispatch).__name__}"
        )
    if not isinstance(backend, str) or not backend.strip():
        raise LabAdapterDataError(
            "backend must be a non-empty string when linking a dispatch"
        )
    if now is not None and not callable(now):
        raise TypeError(f"now must be callable, got {type(now).__name__}")
    stamp = (now if now is not None else _utc_now)()

    run = Run.from_dict(run_store.read("run", dispatch.run_id))
    old_external = run.external
    if (
        old_external is not None
        and old_external.dispatch_id not in (None, dispatch.dispatch_id)
    ):
        raise LabAdapterDataError(
            f"run {run.run_id!r} is already linked to dispatch"
            f" {old_external.dispatch_id!r}; a run is never silently"
            f" re-linked to dispatch {dispatch.dispatch_id!r}"
        )

    # The lifecycle advance through the REAL transition rules: the
    # mainline walks READY -> DISPATCHED -> RUNNING_EXTERNAL (a direct
    # READY -> RUNNING_EXTERNAL jump is not a legal transition), an
    # already-external run re-links idempotently, and any state that
    # cannot carry the dispatch is refused loudly.
    if run.lifecycle_state is LifecycleState.READY:
        transition(run.lifecycle_state, LifecycleState.DISPATCHED)
    elif run.lifecycle_state is LifecycleState.DISPATCHED:
        transition(run.lifecycle_state, LifecycleState.RUNNING_EXTERNAL)
    elif run.lifecycle_state is not LifecycleState.RUNNING_EXTERNAL:
        transition(run.lifecycle_state, LifecycleState.RUNNING_EXTERNAL)

    external = RunExternal(
        backend=backend,
        dispatch_id=dispatch.dispatch_id,
        job_id=old_external.job_id if old_external is not None else None,
        working_directory=(
            old_external.working_directory if old_external is not None else None
        ),
    )
    updated = replace(
        run,
        lifecycle_state=LifecycleState.RUNNING_EXTERNAL,
        external=external,
        updated_at=stamp,
    )
    run_store.write("run", run.run_id, updated.to_dict())
    return updated
