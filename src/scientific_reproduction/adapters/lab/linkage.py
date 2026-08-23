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

Every arc actually performed is audited in the project event log as one
deterministic ``run.lifecycle_change`` event (the transition vocabulary
of ``workers.run_helpers``, under the same deterministic idempotency
key), appended **before** the record write: a run starting at ``READY``
appends both mainline arcs, a stale ``DISPATCHED`` run the second, and
an idempotent re-link appends none -- the record write stays the commit
point and the existing crash / idempotent-re-link convergence is
preserved exactly.

Errors follow the house paradigm: ``TypeError`` at type boundaries,
``LabAdapterDataError`` (a ``ValueError`` subclass) for linkage
conflicts with stable messages, and the real
:class:`IllegalTransitionError` from ``core.transitions`` for a run
whose lifecycle cannot carry the dispatch (a result-bearing or
terminal Run can never be re-linked to a dispatch).

Concurrency (issue #145)
------------------------
The linkage is a read -> validate -> append -> write authoring path.
To keep the durable Run record from diverging from the ordered event
log under concurrent writers (worker + execution monitor), the whole
sequence runs under the **per-run lease** of the lease layer
(DEV-M1-G03, ``core.leases``) acquired for
``("run", dispatch.run_id)`` before the record is read and released on
every exit path. A concurrent writer whose critical section overlaps
the linkage is refused loudly with ``LeaseHeldError`` instead of
overwriting a record its read may have gone stale on; the surviving
record always agrees with the ordered event log. The lease is bounded
(``RUN_LINKAGE_LEASE_TTL``) so a crashed holder's lease expires and the
run is never blocked forever. The idempotent re-link / crash-window
convergence semantics are unchanged: a genuinely idempotent re-link
still succeeds when no other writer holds the lease.
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
from scientific_reproduction.core.events import ProjectEvent, ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.leases import LeaseStore
from scientific_reproduction.core.models import LifecycleState, Run, RunExternal
from scientific_reproduction.core.state_backend import StateBackend
from scientific_reproduction.core.transitions import transition

__all__ = [
    "DISPATCH_LINKAGE_REASON",
    "FILESYSTEM_BACKEND_NAME",
    "LINKAGE_ACTOR",
    "LinkageClock",
    "RUN_LIFECYCLE_CHANGE_EVENT_TYPE",
    "link_run_to_dispatch",
]

#: The backend name of the v0.1 filesystem/manual handoff reference
#: adapter (recorded as ``run.external.backend`` by the linkage helper
#: when no other backend is named; matches ``FilesystemLabAdapter.adapter_id``).
FILESYSTEM_BACKEND_NAME: str = "filesystem"

#: Event type of a run lifecycle transition, idempotency key
#: ``run.lifecycle_change:<run_id>:<from>:<to>`` -- the same transition
#: vocabulary ``workers.run_helpers.transition_run`` appends for the
#: same arcs (``RUN_LIFECYCLE_CHANGE_EVENT_TYPE`` there); the linkage
#: audits one such event per arc it actually performs, so the
#: external-dispatch phase stays visible in the project event log.
RUN_LIFECYCLE_CHANGE_EVENT_TYPE: str = "run.lifecycle_change"

#: The stable actor stamped on the linkage's lifecycle events: the
#: dispatching orchestrator (the Experiment Worker of
#: 10-EXPERIMENT-SUBSYSTEM.md SS1) that owns the Run-record linkage.
LINKAGE_ACTOR: str = "experiment-worker"

#: The stable reason stamped on every linkage lifecycle event.
DISPATCH_LINKAGE_REASON: str = "external dispatch linkage"

#: Time-to-live (seconds) of the per-run authoring lease
#: ``link_run_to_dispatch`` holds for the duration of one read ->
#: validate -> append -> write sequence (the lease layer of DEV-M1-G03,
#: ``core.leases``): a concurrent writer for the same run is refused
#: with ``LeaseHeldError`` instead of overwriting the record. The lease
#: is bounded, so a crashed holder's lease expires after this TTL
#: (deterministic recovery) and a run is never blocked forever.
RUN_LINKAGE_LEASE_TTL: float = 60.0

#: The injectable clock of the linkage helper: a callable producing a
#: timestamp string (mirrors the adapters' caller-injected timestamps).
LinkageClock: TypeAlias = Callable[[], str]


def _utc_now() -> str:
    """The default clock: current UTC time as an ISO-8601 timestamp
    string (``YYYY-MM-DDTHH:MM:SS+00:00``)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lifecycle_change_event(
    run_id: str,
    from_state: LifecycleState,
    to_state: LifecycleState,
    at: str,
) -> ProjectEvent:
    """The deterministic transition event of one linkage arc.

    Mirrors ``workers.run_helpers._lifecycle_change_event``: the event
    id is a pure function of (run id, from, to) -- a crash-window
    re-link re-appends the same event under the same idempotency key
    and resolves to the single original record (exactly-once).
    """
    return ProjectEvent(
        event_id=generate_id(
            "event",
            RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
            run_id,
            from_state.value,
            to_state.value,
        ),
        timestamp=at,
        actor=LINKAGE_ACTOR,
        event_type=RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
        object_id=run_id,
        run_id=run_id,
        from_=from_state.value,
        to=to_state.value,
        reason=DISPATCH_LINKAGE_REASON,
    )


def link_run_to_dispatch(
    run_store: StateBackend,
    dispatch: DispatchRecord,
    *,
    backend: str = FILESYSTEM_BACKEND_NAME,
    now: LinkageClock | None = None,
    event_log: ProjectEventLog | None = None,
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
    the store's real ``run`` schema gate. Every arc actually performed
    is audited in the event log as one deterministic
    ``run.lifecycle_change`` event under its stable idempotency key
    (the transition vocabulary of ``workers.run_helpers``) -- appended
    **before** the record write, so the record write stays the commit
    point and a crash-window re-link converges exactly: re-performed
    arcs re-append the same events idempotently, and an idempotent
    re-link appends none. A run whose external identity already names
    a **different** dispatch is refused loudly, never silently
    re-linked; a run whose lifecycle cannot carry the dispatch
    (result-bearing or terminal) is refused by the transition machinery.

    Concurrency (issue #145): the read -> validate -> append -> write
    sequence runs under the **per-run lease** (``core.leases``,
    DEV-M1-G03) acquired for ``("run", dispatch.run_id)`` before the
    record is read and released on every exit path. A concurrent writer
    whose critical section overlaps the linkage is refused loudly with
    ``LeaseHeldError`` instead of overwriting a record its read may
    have gone stale on -- the surviving record always agrees with the
    ordered event log, and no phantom transition can enter the audit
    record.

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
        event_log: the append-only event log to audit through (default:
            a :class:`ProjectEventLog` over the run store's
            ``base_dir`` -- the workspace root, whose canonical log
            lives at ``events/``; the same default ``register_run`` and
            ``transition_run`` use).

    Returns:
        The updated :class:`Run` (the persisted record).

    Raises:
        TypeError: ``run_store`` is not a ``StateBackend``, ``dispatch``
            is not a ``DispatchRecord``, ``now`` is not callable, or
            ``event_log`` is not a ``ProjectEventLog``.
        LabAdapterDataError: ``backend`` is not a non-empty string, or
            the Run record's external identity already names a different
            ``dispatch_id`` (the run is linked to another dispatch).
        FileNotFoundError: no Run record exists for ``dispatch.run_id``.
        ValueError: the stored Run record is corrupt (from the store's
            ``read`` / ``Run.from_dict``).
        IllegalTransitionError: the Run's lifecycle state cannot carry
            the dispatch (the real transition rules refuse it).
        LeaseHeldError: the per-run authoring lease is held by another
            principal (a concurrent writer is mid-critical-section on
            this run); the linkage is refused loudly and nothing is
            persisted -- retry once the holder releases.
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
    if event_log is not None and not isinstance(event_log, ProjectEventLog):
        raise TypeError(
            "event_log must be a ProjectEventLog, got"
            f" {type(event_log).__name__}"
        )
    if event_log is None:
        # Default audit target: a ProjectEventLog over the run store's
        # ``base_dir`` -- the workspace root of the v0.1 filesystem
        # backend, whose canonical log lives at ``events/`` (mirrors
        # workers.run_helpers._resolve_event_log). ``base_dir`` is
        # public on the concrete backend, not on the abstract
        # ``StateBackend`` interface.
        event_log = ProjectEventLog(getattr(run_store, "base_dir"))
    stamp = (now if now is not None else _utc_now)()

    # The per-run authoring lease (DEV-M1-G03, ``core.leases``): the
    # whole read -> validate -> append -> write sequence runs under it,
    # so a concurrent writer for this run is refused loudly with
    # ``LeaseHeldError`` instead of overwriting a record whose read it
    # may have gone stale on (issue #145). The lease is released on
    # every exit path -- a refused or failed linkage never blocks the
    # run beyond the bounded TTL.
    leases = LeaseStore(getattr(run_store, "base_dir"))
    lease = leases.acquire(
        "run", dispatch.run_id, LINKAGE_ACTOR, RUN_LINKAGE_LEASE_TTL
    )
    try:
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
        # cannot carry the dispatch is refused loudly. ``arcs`` collects
        # the moves actually performed (an idempotent re-link: none).
        arcs: list[tuple[LifecycleState, LifecycleState]] = []
        if run.lifecycle_state is LifecycleState.READY:
            transition(run.lifecycle_state, LifecycleState.DISPATCHED)
            arcs = [
                (LifecycleState.READY, LifecycleState.DISPATCHED),
                (LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL),
            ]
        elif run.lifecycle_state is LifecycleState.DISPATCHED:
            transition(run.lifecycle_state, LifecycleState.RUNNING_EXTERNAL)
            arcs = [(LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL)]
        elif run.lifecycle_state is not LifecycleState.RUNNING_EXTERNAL:
            transition(run.lifecycle_state, LifecycleState.RUNNING_EXTERNAL)

        # One deterministic audit event per arc, appended under the stable
        # idempotency key BEFORE the record write: a crash between the
        # appends and the write leaves the record stale, and the re-link
        # re-performs the same arcs and re-appends the same events (the
        # log converges to the single original records -- no duplicates).
        for from_state, to_state in arcs:
            event_log.append(
                _lifecycle_change_event(run.run_id, from_state, to_state, stamp),
                idempotency_key=(
                    f"{RUN_LIFECYCLE_CHANGE_EVENT_TYPE}:{run.run_id}:"
                    f"{from_state.value}:{to_state.value}"
                ),
            )

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
    finally:
        leases.release(lease)
