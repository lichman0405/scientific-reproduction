"""Role-facing state authoring helpers for the Worker/Monitor roles (issue #92).

Implements the **official run authoring facade** over the existing
primitives (``FilesystemStateBackend``, ``ProjectEventLog``,
``core.ids.generate_id``, ``core.transitions``): the worker roles
register Run records and drive Run lifecycle transitions without
hand-rolling canonical JSON, event ids, idempotency keys or lifecycle
plumbing. The frozen spec grounds this module:

* ``05-GOAL-RUN-SCHEMA.md`` SS7: the Run lifecycle is the frozen
  ``CREATED -> READY -> DISPATCHED -> RUNNING_EXTERNAL -> RESULT_AVAILABLE
  -> ANALYZING -> SUBMITTED_FOR_REVIEW -> CLOSED`` mainline plus the
  pre-result ``CANCELLED`` and result-bearing ``INVALIDATED`` arcs --
  every move goes through the normative rule table of
  ``core.rules.lifecycle`` / ``core.transitions.transition``, never a
  hand-rolled check;
* ``13-EXECUTION-MONITOR.md`` section 5: the monitor records execution
  events ("execution events into project state transitions", role
  contract ``execution_monitor``) -- the run events here use the same
  event vocabulary discipline as ``monitoring/reconcile.py``;
* ``14-STATE-GIT-ARTIFACTS.md`` SS7: the report traceability chain --
  Run records are durable state, one file per run;
* the role contracts of ``adapters/platform/contracts/base.py`` AC-02:
  run records and the append-only event log are the roles' core state
  truth (``state_object_types`` includes ``run`` and ``event``).

Workspace layout (normative)
----------------------------
Run records live one file per run at ``runs/<run_id>.json``: the run
store is a ``FilesystemStateBackend`` over the workspace root, whose
canonical tree resolution (``core.state_backend.SCHEMA_TO_STATE_DIR``)
puts runs in the same ``runs/`` directory the reporting subsystem
reads through ``reporting.audit.py`` and the monitoring subsystem
writes through its injected run store; events live at
``events/<event_id>.json`` (a ``ProjectEventLog`` bound to the
workspace root).

Determinism and discipline
--------------------------
Event ids are pure functions of the transition (``core.ids.generate_id``
over the run id and the from/to states); timestamps and actors are
injected by the caller; every append carries a deterministic idempotency
key; every write goes through the schema-validating, atomic state
backend. ``TypeError`` at the public type boundaries; ``ValueError``
subclasses with stable messages otherwise.

Exactly-once and crash-window convergence
-----------------------------------------
Registration is exactly once per ``run_id`` (immutable records; a
duplicate raises). Transition bookkeeping follows the monitoring
pattern of ``monitoring/reconcile.py`` (AC-01/AC-03): the transition
event id and its idempotency key are deterministic functions of the
move, so a crash between the record write and the event append
converges on re-run -- the idempotent re-append returns the single
original record and the sequence never advances twice. A re-run whose
record is already at the target state is a no-op and is rejected
(no-op transitions are never in the normative table) **unless** the
deterministic event of the unique normative arc into that state is
missing from the log, which proves an earlier interrupted call; the
missing event is then appended idempotently (``replayed=True``).
States with several legal predecessors (``CANCELLED``, ``INVALIDATED``)
have no reconstructible arc: a re-run at those states without the
recorded event is rejected, because the from-state of the lost event
cannot be proven.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, TypeAlias, cast

from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import LifecycleState, ProjectEvent, Run
from scientific_reproduction.core.rules.lifecycle import IllegalTransitionError
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.core.transitions import transition
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)

__all__ = [
    "DuplicateRunError",
    "EVENTS_STATE_DIR",
    "RUN_LIFECYCLE_CHANGE_EVENT_TYPE",
    "RUN_PREDECESSOR_STATE",
    "RUN_RECORDED_EVENT_TYPE",
    "RUNS_STATE_DIR",
    "RunNotFoundError",
    "RunRegistration",
    "RunRegistryError",
    "RunTransition",
    "list_runs",
    "read_run",
    "register_run",
    "transition_run",
]

# ---------------------------------------------------------------------------
# Frozen constants (workspace layout and event vocabulary)
# ---------------------------------------------------------------------------

#: Canonical tree directory of the durable Run registry, relative to
#: the workspace root (``runs/<run_id>.json`` -- resolved by
#: ``SCHEMA_TO_STATE_DIR`` for the ``run`` object type; the same layout
#: ``reporting.audit.py`` reads).
RUNS_STATE_DIR: str = "runs"

#: Canonical event-log directory of a workspace (``planning.init``
#: ``INIT_DIRECTORIES``; ``events/<event_id>.json`` through a
#: ``ProjectEventLog`` bound to the workspace root).
EVENTS_STATE_DIR: str = "events"

#: Event type of a run registration (one ``run.recorded`` event per
#: run, appended under the deterministic key ``run.recorded:<run_id>``).
RUN_RECORDED_EVENT_TYPE: str = "run.recorded"

#: Event type of a run lifecycle transition (key
#: ``run.lifecycle_change:<run_id>:<from>:<to>``); the event carries
#: ``from``/``to``, the ``run_id`` and the stable ``reason``.
RUN_LIFECYCLE_CHANGE_EVENT_TYPE: str = "run.lifecycle_change"

#: The unique normative predecessor of each Run lifecycle state
#: (``core.rules.lifecycle``): every mainline state is reached through
#: exactly one arc, so the transition event of a crash-window re-run is
#: reconstructible (see the module docstring). ``CREATED`` is the
#: initial state; ``CANCELLED`` and ``INVALIDATED`` each have several
#: legal predecessors and are therefore not reconstructible (None).
RUN_PREDECESSOR_STATE: dict[LifecycleState, LifecycleState | None] = {
    LifecycleState.CREATED: None,
    LifecycleState.READY: LifecycleState.CREATED,
    LifecycleState.DISPATCHED: LifecycleState.READY,
    LifecycleState.RUNNING_EXTERNAL: LifecycleState.DISPATCHED,
    LifecycleState.RESULT_AVAILABLE: LifecycleState.RUNNING_EXTERNAL,
    LifecycleState.ANALYZING: LifecycleState.RESULT_AVAILABLE,
    LifecycleState.SUBMITTED_FOR_REVIEW: LifecycleState.ANALYZING,
    LifecycleState.CLOSED: LifecycleState.SUBMITTED_FOR_REVIEW,
    LifecycleState.CANCELLED: None,
    LifecycleState.INVALIDATED: None,
}


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class RunRegistryError(ValueError):
    """Base error of the durable Run registry."""


class DuplicateRunError(RunRegistryError):
    """Raised when a run is registered a second time (no clobbering)."""


class RunNotFoundError(RunRegistryError):
    """Raised when reading a run that is not registered."""


# ---------------------------------------------------------------------------
# User-supplied records: the typed model or a schema-shaped dict
# ---------------------------------------------------------------------------

RunInput: TypeAlias = Run | Mapping[str, Any]


# ---------------------------------------------------------------------------
# Registration results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRegistration:
    """The outcome of one run registration.

    ``run`` is the frozen record persisted at ``runs/<run_id>.json``;
    ``event_record`` is the appended ``run.recorded`` event (None when
    no event log was given); ``replayed`` is True when the registration
    converged an earlier interrupted registration (the record already
    existed and only the missing event was appended).
    """

    run: Run
    event_record: EventRecord | None = None
    replayed: bool = False


@dataclass(frozen=True)
class RunTransition:
    """The outcome of one run lifecycle transition.

    ``previous_state`` is the persisted state before the move;
    ``run`` is the persisted record afterwards (``lifecycle_state``
    advanced, ``updated_at`` stamped with the injected ``at``);
    ``event_record`` is the appended ``run.lifecycle_change`` event;
    ``replayed`` is True when the call converged an already recorded
    transition (the record was already at the target state and the
    deterministic transition event was already appended -- the
    idempotent re-append returns the single original record,
    exactly-once).
    """

    previous_state: LifecycleState
    run: Run
    event_record: EventRecord | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------
# Registration API
# ---------------------------------------------------------------------------


def register_run(
    root: str | Path,
    run: RunInput,
    *,
    actor: str,
    recorded_at: str,
    event_log: ProjectEventLog | None = None,
) -> RunRegistration:
    """Register one Run record at ``runs/<run_id>.json``.

    The worker/monitor run authoring entry: the record is
    schema-shaped (``schemas/run.schema.yaml``), canonical-JSON
    persisted through the atomic state backend, and audited with one
    ``run.recorded`` event (when an event log is given). The record's
    own timestamps (``created_at`` / ``updated_at``) are the record's
    fields; ``recorded_at`` stamps the event.

    Registration is exactly once per ``run_id``: Run records are
    immutable, and a re-registration of the same id -- even with
    different content -- is rejected with ``DuplicateRunError`` and the
    original file is never rewritten. With an event log, a re-run after
    a crash between the record write and the event append converges
    instead: the missing deterministic event is appended
    (``replayed=True``) and the original record stays untouched.

    Args:
        root: the initialized workspace root.
        run: the run as a typed :class:`Run` or a schema-shaped mapping.
        actor: the recording actor (role agent identity) stamped on the
            event.
        recorded_at: the injected deterministic recording timestamp.
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`RunRegistration` (record, event record, replayed
        flag).

    Raises:
        TypeError: ``root`` is not a str/Path, ``run`` is neither a
            ``Run`` nor a mapping, or ``actor`` / ``recorded_at`` is not
            a str.
        RunRegistryError: ``actor`` / ``recorded_at`` is empty.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        DuplicateRunError: the ``run_id`` is already registered.
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_run(run)
    _require_actor_stamp(actor, recorded_at)
    store = _run_store(project_root)
    event_id = generate_id("event", RUN_RECORDED_EVENT_TYPE, model.run_id)
    if store.exists("run", model.run_id):
        if event_log is None or event_log.get(event_id) is not None:
            # Record and its deterministic event both present (or no log
            # to prove either way): a true duplicate -- never a silent
            # re-registration.
            raise DuplicateRunError(
                f"run {model.run_id!r} is already registered; run records"
                " are immutable and each run_id is written exactly once"
            )
        # Crash window: the record write landed but the event append did
        # not -- heal the log with the deterministic event and report the
        # original record (replayed convergence).
        stored = _read_run_record(store, model.run_id)
        record = _append(
            event_log,
            _run_recorded_event(model.run_id, actor, recorded_at),
            idempotency_key=f"{RUN_RECORDED_EVENT_TYPE}:{model.run_id}",
        )
        return RunRegistration(run=stored, event_record=record, replayed=True)
    store.write("run", model.run_id, model.to_dict())
    record = _append(
        event_log,
        _run_recorded_event(model.run_id, actor, recorded_at),
        idempotency_key=f"{RUN_RECORDED_EVENT_TYPE}:{model.run_id}",
    )
    return RunRegistration(run=model, event_record=record)


# ---------------------------------------------------------------------------
# Lifecycle transition API (through the normative rule table)
# ---------------------------------------------------------------------------


def transition_run(
    root: str | Path,
    run_id: str,
    to_state: LifecycleState,
    *,
    actor: str,
    reason: str,
    at: str,
    event_log: ProjectEventLog | None = None,
) -> RunTransition:
    """Advance one Run to ``to_state`` through the normative rule table.

    Reads the **persisted** run, validates the move against the
    normative Run lifecycle table (``core.transitions.transition`` over
    ``core.rules.lifecycle``: the mainline chain plus the pre-result
    ``CANCELLED`` and result-bearing ``INVALIDATED`` arcs; any other
    pair -- including no-op transitions -- raises
    ``IllegalTransitionError``), persists the advanced record
    (``lifecycle_state`` advanced, ``updated_at`` stamped with ``at``)
    and appends one ``run.lifecycle_change`` event
    (``from``/``to``/``reason``) under a deterministic idempotency key.

    Crash-window convergence (monitoring pattern): a re-run whose
    record is already at ``to_state`` is a no-op and is rejected --
    no-op transitions must never enter the audit record -- **unless**
    the deterministic transition event of the unique normative arc into
    ``to_state`` is missing from the log, which proves an earlier
    interrupted call (the record write landed, the event append did
    not); the missing event is then appended idempotently and the call
    returns ``replayed=True``. States with several legal predecessors
    (``CANCELLED``, ``INVALIDATED``) have no reconstructible arc, and
    states without an event log can never converge: the no-op guard
    wins.

    Args:
        root: the initialized workspace root.
        run_id: the id of the registered run to advance.
        to_state: the target state (a ``LifecycleState`` member).
        actor: the acting role agent identity stamped on the event.
        reason: the stable reason for the transition (the event's
            ``reason``).
        at: the injected deterministic transition timestamp (also
            stamped as the record's ``updated_at``).
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`RunTransition` (previous state, advanced record,
        event record, replayed flag).

    Raises:
        TypeError: ``root`` is not a str/Path, ``run_id`` / ``actor`` /
            ``reason`` / ``at`` is not a str, or ``to_state`` is not a
            ``LifecycleState``.
        RunRegistryError: ``actor`` / ``reason`` / ``at`` is empty.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        RunNotFoundError: no run with that id is registered.
        IllegalTransitionError: the pair is not in the normative rule
            table (including no-op transitions).
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    if not isinstance(to_state, LifecycleState):
        raise TypeError(
            "to_state must be a LifecycleState, got" f" {type(to_state).__name__}"
        )
    _require_nonempty(run_id, "run_id")
    _require_transition_args(actor, reason, at)
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _run_store(project_root)
    current = _read_run(store, project_root, run_id)
    if current.lifecycle_state == to_state:
        if event_log is None:
            raise IllegalTransitionError(
                "run-lifecycle", current.lifecycle_state, to_state
            )
        predecessor = RUN_PREDECESSOR_STATE[to_state]
        if predecessor is None:
            raise IllegalTransitionError(
                "run-lifecycle", current.lifecycle_state, to_state
            )
        record = _append(
            event_log,
            _lifecycle_change_event(
                run_id, predecessor, to_state, actor, reason, at
            ),
            idempotency_key=(
                f"{RUN_LIFECYCLE_CHANGE_EVENT_TYPE}:{run_id}:"
                f"{predecessor.value}:{to_state.value}"
            ),
        )
        return RunTransition(
            previous_state=current.lifecycle_state,
            run=current,
            event_record=record,
            replayed=True,
        )
    new_state = cast(
        LifecycleState, transition(current.lifecycle_state, to_state)
    )
    updated = replace(
        current, lifecycle_state=new_state, updated_at=at
    )
    store.write("run", run_id, updated.to_dict())
    record = _append(
        event_log,
        _lifecycle_change_event(
            run_id, current.lifecycle_state, to_state, actor, reason, at
        ),
        idempotency_key=(
            f"{RUN_LIFECYCLE_CHANGE_EVENT_TYPE}:{run_id}:"
            f"{current.lifecycle_state.value}:{to_state.value}"
        ),
    )
    return RunTransition(
        previous_state=current.lifecycle_state,
        run=updated,
        event_record=record,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_run(root: str | Path, run_id: str) -> Run:
    """Read one registered run as a typed record.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``run_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        RunNotFoundError: no run with that id is registered.
        ValueError: the stored record is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str, got {type(run_id).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _run_store(project_root)
    if not store.exists("run", run_id):
        raise RunNotFoundError(
            f"no run registered with id {run_id!r} at {project_root}"
        )
    return _read_run_record(store, run_id)


def list_runs(root: str | Path) -> tuple[Run, ...]:
    """List every registered run, sorted by ``run_id``.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _run_store(project_root)
    return tuple(
        _read_run_record(store, run_id) for run_id in store.list_ids("run")
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_nonempty(value: str, name: str) -> None:
    """Reject an empty argument string (stable RunRegistryError)."""
    if not value:
        raise RunRegistryError(f"{name} must not be empty")


def _require_actor_stamp(actor: str, recorded_at: str) -> None:
    """Reject non-str / empty actor and timestamp arguments."""
    for name, value in (("actor", actor), ("recorded_at", recorded_at)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a str, got {type(value).__name__}")
        _require_nonempty(value, name)


def _require_transition_args(actor: str, reason: str, at: str) -> None:
    """Reject non-str / empty transition event arguments."""
    for name, value in (("actor", actor), ("reason", reason), ("at", at)):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a str, got {type(value).__name__}")
        _require_nonempty(value, name)


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _run_store(root: Path) -> FilesystemStateBackend:
    """The durable Run registry of a workspace.

    The backend is rooted at the workspace root, so the canonical tree
    resolution (``SCHEMA_TO_STATE_DIR``) puts runs at ``runs/``.
    """
    return FilesystemStateBackend(root)


def _coerce_run(run: RunInput) -> Run:
    """Return a typed run from either input form."""
    if isinstance(run, Run):
        return run
    if isinstance(run, Mapping):
        return Run.from_dict(run)
    raise TypeError(f"run must be a Run or a mapping, got {type(run).__name__}")


def _read_run_record(store: FilesystemStateBackend, run_id: str) -> Run:
    """Parse one run record, rejecting corrupt state with a stable error."""
    data = store.read("run", run_id)
    try:
        return Run.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"corrupt run record for {run_id!r}: {exc}") from exc


def _read_run(
    store: FilesystemStateBackend, project_root: Path, run_id: str
) -> Run:
    """Read one registered run; raise ``RunNotFoundError`` when absent."""
    if not store.exists("run", run_id):
        raise RunNotFoundError(
            f"no run registered with id {run_id!r} at {project_root}"
        )
    return _read_run_record(store, run_id)


def _append(
    event_log: ProjectEventLog | None,
    event: ProjectEvent,
    *,
    idempotency_key: str,
) -> EventRecord | None:
    """Append ``event`` idempotently; None when no event log is given."""
    if event_log is None:
        return None
    return event_log.append(event, idempotency_key=idempotency_key)


def _run_recorded_event(run_id: str, actor: str, recorded_at: str) -> ProjectEvent:
    """The deterministic ``run.recorded`` event of one run."""
    return ProjectEvent(
        event_id=generate_id("event", RUN_RECORDED_EVENT_TYPE, run_id),
        timestamp=recorded_at,
        actor=actor,
        event_type=RUN_RECORDED_EVENT_TYPE,
        object_id=run_id,
        run_id=run_id,
    )


def _lifecycle_change_event(
    run_id: str,
    from_state: LifecycleState,
    to_state: LifecycleState,
    actor: str,
    reason: str,
    at: str,
) -> ProjectEvent:
    """The deterministic transition event of one lifecycle move.

    The event id is a pure function of (run id, from, to) -- the same
    pair re-appended under the same idempotency key resolves to the
    single original record (exactly-once, monitoring pattern).
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
        actor=actor,
        event_type=RUN_LIFECYCLE_CHANGE_EVENT_TYPE,
        object_id=run_id,
        run_id=run_id,
        from_=from_state.value,
        to=to_state.value,
        reason=reason,
    )
