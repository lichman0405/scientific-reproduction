"""Role-facing state authoring helpers for the Research role (issue #92).

Implements the **official research state authoring facade** over the
existing primitives (``FilesystemStateBackend``, ``ProjectEventLog``,
``core.ids.generate_id``, the research rule layers of
``research/requests.py`` / ``research/evidence.py`` /
``research/sources.py``): the Research agent registers sources, evidence
records and research requests, advances request lifecycles and links
results, without hand-rolling canonical JSON, event ids, idempotency
keys or lifecycle plumbing. The frozen spec grounds this module:

* ``agent-contracts/RESEARCH.md``: the research role *builds the project
  evidence base using traceable sources and claim-specific evidence
  assessments* and must *store findings ... as source and evidence
  records in the state backend* (the state-as-truth rule of the role
  contracts, ``adapters/platform/contracts/base.py`` AC-02);
* ``06-EVIDENCE-SYSTEM.md`` section 7 ("Search deduplication"): mirrors
  of one paper are not independent evidence -- ``register_source``
  enforces the canonical-identity uniqueness of
  ``research.sources.canonical_identity`` at authoring time;
* ``09-RESEARCH-SUBSYSTEM.md`` section 3: only the Supervisor issues
  formal Research Requests -- registration enforces the schema's
  ``requested_by == "supervisor"`` constant and the issued state
  ``OPEN``, and lifecycle moves go through the normative rule table of
  ``research.requests`` (R-REQ-S0/C1/E1; no-op transitions are never
  legal, R-REQ-D1);
* ``schemas/source.schema.yaml``, ``schemas/evidence.schema.yaml``,
  ``schemas/research-request.schema.yaml``: records are persisted
  through the schema-validating state backend as canonical JSON.

Workspace layout (normative)
----------------------------
Records live one file per object, under the workspace root:

* sources at ``sources/source/<source_id>.json``;
* evidence at ``evidence/evidence/<evidence_id>.json``;
* research requests at ``research-requests/research-request/<id>.json``
  (the directory is created on demand by the atomic write);
* events at ``events/event/<event_id>.json`` (a ``ProjectEventLog``
  bound to the workspace ``events/`` directory; the sequence counter and
  idempotency claims live under ``events/_event_log/``).

Everything is deterministic: ids are generated with
``core.ids.generate_id`` (event ids are pure functions of the record /
transition), timestamps and actors are injected by the caller, every
append carries a deterministic idempotency key, and every write goes
through the schema-validating, atomic state backend. ``TypeError`` at
the public type boundaries; ``ValueError`` subclasses with stable
messages otherwise.

Exactly-once and crash-window convergence
-----------------------------------------
Registration is exactly once per record id (immutable records; a
duplicate raises). Event bookkeeping follows the monitoring pattern of
``monitoring/reconcile.py``: the event id and its idempotency key are
deterministic functions of the operation, so a crash between the record
write and the event append converges on re-run -- the idempotent
re-append returns the single original record (``replayed=True``) and
the sequence never advances twice. A re-run that finds the record
already present appends the missing deterministic event (convergence)
instead of raising, and reports ``replayed=True``; a genuinely new
duplicate (record **and** event both present) raises the stable
duplicate error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from scientific_reproduction.core.events import EventRecord, ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    ProjectEvent,
    ResearchRequest,
    ResearchRequestStatus,
    ResearchSource,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)
from scientific_reproduction.research.evidence import (
    EvidenceDuplicateError,
    EvidenceRegistrationError,
    EvidenceRegistryError,
    validate_evidence_record,
)
from scientific_reproduction.research.requests import (
    IllegalRequestTransitionError,
    RequestIssuanceError,
    RequestLinkageError,
    ResearchRequestError,
    ResultLinkageRecord,
    apply_request_transition,
    attach_result_to_request,
)
from scientific_reproduction.research.sources import (
    SourceIdentity,
    SourceNormalizationError,
    canonical_identity,
)

__all__ = [
    "DuplicateRequestError",
    "DuplicateSourceError",
    "EVENTS_STATE_DIR",
    "EVIDENCE_RECORDED_EVENT_TYPE",
    "EVIDENCE_STATE_DIR",
    "EvidenceDuplicateError",
    "EvidenceNotFoundError",
    "EvidenceRegistrationError",
    "EvidenceRegistration",
    "RESEARCH_REQUEST_RECORDED_EVENT_TYPE",
    "RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE",
    "RESEARCH_REQUEST_STATE_DIR",
    "RESEARCH_REQUEST_TRANSITION_EVENT_TYPE",
    "REQUEST_PREDECESSOR_STATUS",
    "RequestAdvancement",
    "RequestLinkageResult",
    "RequestNotFoundError",
    "RequestRegistration",
    "SOURCE_RECORDED_EVENT_TYPE",
    "SOURCE_STATE_DIR",
    "SourceNotFoundError",
    "SourceRegistration",
    "StateHelperError",
    "advance_research_request",
    "link_result_to_request",
    "list_evidence",
    "list_research_requests",
    "list_sources",
    "read_evidence",
    "read_research_request",
    "read_source",
    "register_evidence",
    "register_research_request",
    "register_source",
]

# ---------------------------------------------------------------------------
# Frozen constants (workspace layout and event vocabulary)
# ---------------------------------------------------------------------------

#: State directory of the durable source registry, relative to the
#: workspace root (``sources/source/<source_id>.json``).
SOURCE_STATE_DIR: str = "sources"

#: State directory of the durable evidence registry (``evidence/evidence/
#: <evidence_id>.json``).
EVIDENCE_STATE_DIR: str = "evidence"

#: State directory of the durable research-request registry
#: (``research-requests/research-request/<request_id>.json``; the
#: directory is created on demand by the atomic write).
RESEARCH_REQUEST_STATE_DIR: str = "research-requests"

#: The project event-log directory of a workspace (``planning.init``
#: ``INIT_DIRECTORIES``); the default event log binds here
#: (``events/event/<event_id>.json``).
EVENTS_STATE_DIR: str = "events"

#: Event type of a source registration (one ``source.recorded`` event
#: per source, appended under the deterministic key
#: ``source.recorded:<source_id>``).
SOURCE_RECORDED_EVENT_TYPE: str = "source.recorded"

#: Event type of an evidence registration (key
#: ``evidence.recorded:<evidence_id>``).
EVIDENCE_RECORDED_EVENT_TYPE: str = "evidence.recorded"

#: Event type of a research-request registration (key
#: ``research-request.recorded:<request_id>``).
RESEARCH_REQUEST_RECORDED_EVENT_TYPE: str = "research-request.recorded"

#: Event type of a research-request lifecycle transition (key
#: ``research-request.transition:<request_id>:<from>:<to>``); the event
#: carries ``from``/``to`` and the stable ``reason``.
RESEARCH_REQUEST_TRANSITION_EVENT_TYPE: str = "research-request.transition"

#: Event type of a result linkage (key
#: ``research-request.result_linked:<request_id>:<evidence_id>``); the
#: event carries ``to`` = the linked evidence id and ``from`` = the
#: request status at linkage time.
RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE: str = "research-request.result_linked"

#: The unique normative predecessor of each research-request status
#: (``research.requests`` R-REQ-S0/C1/E1): the only way a request can be
#: in a non-initial status is through the recorded transition from the
#: status's unique predecessor, which makes the transition event of a
#: crash-window re-run reconstructible (see the module docstring).
#: ``OPEN`` is the initial (issued) status and has no predecessor.
REQUEST_PREDECESSOR_STATUS: dict[ResearchRequestStatus, ResearchRequestStatus | None] = {
    ResearchRequestStatus.OPEN: None,
    ResearchRequestStatus.SEARCHING: ResearchRequestStatus.OPEN,
    ResearchRequestStatus.COMPLETE: ResearchRequestStatus.SEARCHING,
    ResearchRequestStatus.EXHAUSTED: ResearchRequestStatus.SEARCHING,
}


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class StateHelperError(ValueError):
    """Base error of the research state helpers (argument contract)."""


class SourceRegistryError(StateHelperError):
    """Base error of the durable source registry."""


class DuplicateSourceError(SourceRegistryError):
    """Raised when a source is registered a second time.

    Also raised when a record's canonical identity collides with an
    already-registered source of a different id (a mirror of the same
    work -- 06-EVIDENCE-SYSTEM.md section 7: mirrors are never
    independent evidence).
    """


class SourceNotFoundError(SourceRegistryError):
    """Raised when reading a source that is not registered."""


class EvidenceNotFoundError(EvidenceRegistryError):
    """Raised when reading an evidence record that is not registered."""


class RequestNotFoundError(ResearchRequestError):
    """Raised when reading a research request that is not registered."""


class DuplicateRequestError(ResearchRequestError):
    """Raised when a research request is registered a second time."""


# ---------------------------------------------------------------------------
# User-supplied records: the typed model or a schema-shaped dict
# ---------------------------------------------------------------------------

SourceInput: TypeAlias = ResearchSource | Mapping[str, Any]
EvidenceInput: TypeAlias = ClaimSpecificEvidence | Mapping[str, Any]
RequestInput: TypeAlias = ResearchRequest | Mapping[str, Any]


# ---------------------------------------------------------------------------
# Registration results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRegistration:
    """The outcome of one source registration.

    ``source`` is the frozen record persisted at
    ``sources/source/<source_id>.json``; ``identity`` is the canonical
    mirror identity derived by ``research.sources.canonical_identity``;
    ``event_record`` is the appended ``source.recorded`` event (None
    when no event log was given); ``replayed`` is True when the
    registration converged an earlier interrupted registration (the
    record already existed and only the missing event was appended).
    """

    source: ResearchSource
    identity: SourceIdentity
    event_record: EventRecord | None = None
    replayed: bool = False


@dataclass(frozen=True)
class EvidenceRegistration:
    """The outcome of one evidence registration.

    ``evidence`` is the frozen record persisted at
    ``evidence/evidence/<evidence_id>.json``; ``event_record`` is the
    appended ``evidence.recorded`` event (None without an event log);
    ``replayed`` marks a converged earlier registration.
    """

    evidence: ClaimSpecificEvidence
    event_record: EventRecord | None = None
    replayed: bool = False


@dataclass(frozen=True)
class RequestRegistration:
    """The outcome of one research-request registration.

    ``request`` is the frozen record persisted at
    ``research-requests/research-request/<request_id>.json``;
    ``event_record`` is the appended ``research-request.recorded`` event
    (None without an event log); ``replayed`` marks a converged earlier
    registration.
    """

    request: ResearchRequest
    event_record: EventRecord | None = None
    replayed: bool = False


@dataclass(frozen=True)
class RequestAdvancement:
    """The outcome of advancing one research request.

    ``request`` is the advanced frozen record (persisted state);
    ``event_record`` is the appended ``research-request.transition``
    event; ``replayed`` is True when the call converged an already
    recorded transition (the record was already at ``to_status`` and the
    deterministic transition event was already appended -- the idempotent
    re-append returns the single original record, exactly-once).
    """

    request: ResearchRequest
    event_record: EventRecord | None = None
    replayed: bool = False


@dataclass(frozen=True)
class RequestLinkageResult:
    """The outcome of linking one result (evidence) to a request.

    ``request`` is the frozen record with ``evidence_id`` appended to
    ``result_evidence_ids``; ``linkage`` is the
    ``research.requests.ResultLinkageRecord`` of the link (audit
    details); ``event_record`` is the appended
    ``research-request.result_linked`` event; ``replayed`` is True when
    the link was already recorded and the call only re-resolved the
    original event.
    """

    request: ResearchRequest
    linkage: ResultLinkageRecord
    event_record: EventRecord | None = None
    replayed: bool = False


# ---------------------------------------------------------------------------
# Registration APIs
# ---------------------------------------------------------------------------


def register_source(
    root: str | Path,
    source: SourceInput,
    *,
    actor: str,
    recorded_at: str,
    event_log: ProjectEventLog | None = None,
) -> SourceRegistration:
    """Register one source record at ``sources/source/<source_id>.json``.

    The research role's source authoring entry: the record is
    schema-shaped (``schemas/source.schema.yaml``), canonical-JSON
    persisted through the atomic state backend, and audited with one
    ``source.recorded`` event (when an event log is given). The
    canonical mirror identity (``research.sources.canonical_identity``)
    is derived at authoring time -- a malformed DOI is surfaced loudly
    (``SourceNormalizationError``) -- and a record whose identity
    collides with an already-registered source is rejected with
    ``DuplicateSourceError`` (06-EVIDENCE-SYSTEM.md section 7: mirrors
    of one paper are never independent evidence).

    Registration is exactly once per ``source_id``: source records are
    immutable, and a re-registration of the same id -- even with
    different content -- is rejected with ``DuplicateSourceError`` and
    the original file is never rewritten. With an event log, a re-run
    after a crash between the record write and the event append
    converges instead: the missing deterministic event is appended
    (``replayed=True``) and the original record stays untouched.

    Args:
        root: the initialized workspace root.
        source: the source as a typed :class:`ResearchSource` or a
            schema-shaped mapping.
        actor: the recording actor (role agent identity) stamped on the
            event.
        recorded_at: the injected deterministic recording timestamp.
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`SourceRegistration` (record, canonical identity,
        event record, replayed flag).

    Raises:
        TypeError: ``root`` is not a str/Path, ``source`` is neither a
            ``ResearchSource`` nor a mapping, or ``actor`` /
            ``recorded_at`` is not a str.
        StateHelperError: ``actor`` / ``recorded_at`` is empty.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        SourceNormalizationError: the record's DOI cannot be normalized.
        DuplicateSourceError: the ``source_id`` is already registered,
            or the canonical identity collides with a registered source
            of another id (mirror dedupe).
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_source(source)
    _require_actor_stamp(actor, recorded_at)
    store = _source_store(project_root)
    event_id = generate_id("event", SOURCE_RECORDED_EVENT_TYPE, model.source_id)
    if store.exists("source", model.source_id):
        if event_log is None or event_log.get(event_id) is not None:
            # Record and its deterministic event both present (or no log
            # to prove either way): a true duplicate -- never a silent
            # re-registration.
            raise DuplicateSourceError(
                f"source {model.source_id!r} is already registered; source"
                " records are immutable and each source_id is written"
                " exactly once"
            )
        # Crash window: the record write landed but the event append did
        # not -- heal the log with the deterministic event and report the
        # original record (replayed convergence).
        stored = _read_source_record(store, model.source_id)
        record = _append(
            event_log,
            _source_recorded_event(model.source_id, actor, recorded_at),
            idempotency_key=f"{SOURCE_RECORDED_EVENT_TYPE}:{model.source_id}",
        )
        return SourceRegistration(
            source=stored,
            identity=canonical_identity(stored),
            event_record=record,
            replayed=True,
        )
    identity = canonical_identity(model)
    _reject_mirror_collision(store, model, identity)
    store.write("source", model.source_id, model.to_dict())
    record = _append(
        event_log,
        _source_recorded_event(model.source_id, actor, recorded_at),
        idempotency_key=f"{SOURCE_RECORDED_EVENT_TYPE}:{model.source_id}",
    )
    return SourceRegistration(source=model, identity=identity, event_record=record)


def register_evidence(
    root: str | Path,
    evidence: EvidenceInput,
    *,
    actor: str,
    recorded_at: str,
    event_log: ProjectEventLog | None = None,
) -> EvidenceRegistration:
    """Register one evidence record at ``evidence/evidence/<id>.json``.

    The research role's evidence authoring entry: the record is
    validated against the frozen evidence shape
    (:func:`research.evidence.validate_evidence_record` -- the same
    checks the in-memory ``EvidenceRegistry`` applies: non-empty
    ids/claim/finding, A/R/D axes within the 0-4 rubric, non-empty
    ``reliability_checklist_ref``, non-empty ``used_by`` entries),
    schema-validated and canonical-JSON persisted through the atomic
    state backend, and audited with one ``evidence.recorded`` event.

    Registration is exactly once per ``evidence_id`` (immutable
    records; a duplicate raises ``EvidenceDuplicateError``, reusing the
    in-memory registry's duplicate family); with an event log, a
    re-run after a crash between the write and the event append
    converges (``replayed=True``).

    Args:
        root: the initialized workspace root.
        evidence: the record as a typed :class:`ClaimSpecificEvidence`
            or a schema-shaped mapping.
        actor: the recording actor stamped on the event.
        recorded_at: the injected deterministic recording timestamp.
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`EvidenceRegistration` (record, event record,
        replayed flag).

    Raises:
        TypeError: ``root`` is not a str/Path, ``evidence`` is neither
            a ``ClaimSpecificEvidence`` nor a mapping, or ``actor`` /
            ``recorded_at`` is not a str.
        StateHelperError: ``actor`` / ``recorded_at`` is empty.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        EvidenceRegistrationError: the record violates the frozen
            evidence shape.
        EvidenceDuplicateError: the ``evidence_id`` is already
            registered.
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_evidence(evidence)
    validate_evidence_record(model)
    _require_actor_stamp(actor, recorded_at)
    store = _evidence_store(project_root)
    event_id = generate_id("event", EVIDENCE_RECORDED_EVENT_TYPE, model.evidence_id)
    if store.exists("evidence", model.evidence_id):
        if event_log is None or event_log.get(event_id) is not None:
            # Record and its deterministic event both present (or no log
            # to prove either way): a true duplicate -- never a silent
            # re-registration.
            raise EvidenceDuplicateError(
                f"evidence {model.evidence_id!r} is already registered;"
                " evidence records are immutable and each evidence_id is"
                " written exactly once"
            )
        # Crash window: the record write landed but the event append did
        # not -- heal the log with the deterministic event and report the
        # original record (replayed convergence).
        stored = _read_evidence_record(store, model.evidence_id)
        record = _append(
            event_log,
            _evidence_recorded_event(model.evidence_id, actor, recorded_at),
            idempotency_key=f"{EVIDENCE_RECORDED_EVENT_TYPE}:{model.evidence_id}",
        )
        return EvidenceRegistration(
            evidence=stored, event_record=record, replayed=True
        )
    store.write("evidence", model.evidence_id, model.to_dict())
    record = _append(
        event_log,
        _evidence_recorded_event(model.evidence_id, actor, recorded_at),
        idempotency_key=f"{EVIDENCE_RECORDED_EVENT_TYPE}:{model.evidence_id}",
    )
    return EvidenceRegistration(evidence=model, event_record=record)


def register_research_request(
    root: str | Path,
    request: RequestInput,
    *,
    actor: str,
    recorded_at: str,
    event_log: ProjectEventLog | None = None,
) -> RequestRegistration:
    """Register one research request at ``research-requests/.../<id>.json``.

    The persistence entry of the formally issued request (the issuance
    itself stays with ``research.requests.issue_research_request``, the
    Supervisor-facing API): only the schema's issued state may be
    persisted -- ``requested_by == "supervisor"`` (the schema constant)
    and ``status == OPEN`` -- so a record can never bypass the
    lifecycle rule table into a later status. The record is
    schema-validated, canonical-JSON persisted through the atomic state
    backend, and audited with one ``research-request.recorded`` event.

    Registration is exactly once per ``request_id`` (immutable
    records; a duplicate raises ``DuplicateRequestError``); with an
    event log, a re-run after a crash between the write and the event
    append converges (``replayed=True``).

    Args:
        root: the initialized workspace root.
        request: the issued request as a typed :class:`ResearchRequest`
            or a schema-shaped mapping.
        actor: the recording actor stamped on the event.
        recorded_at: the injected deterministic recording timestamp.
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`RequestRegistration` (record, event record, replayed
        flag).

    Raises:
        TypeError: ``root`` is not a str/Path, ``request`` is neither a
            ``ResearchRequest`` nor a mapping, or ``actor`` /
            ``recorded_at`` is not a str.
        StateHelperError: ``actor`` / ``recorded_at`` is empty.
        RequestIssuanceError: the record is not in the issued state
            (``requested_by != "supervisor"`` or ``status != OPEN``) --
            lifecycle moves go through :func:`advance_research_request`.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        DuplicateRequestError: the ``request_id`` is already
            registered.
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_request(request)
    if model.requested_by != "supervisor":
        raise RequestIssuanceError(
            "register_research_request: requested_by must be 'supervisor'"
            f" (schema const), got {model.requested_by!r}"
        )
    if model.status is not ResearchRequestStatus.OPEN:
        raise RequestIssuanceError(
            "register_research_request: only OPEN requests can be"
            " registered (the issued state); lifecycle moves go through"
            f" advance_research_request, got {model.status.value!r}"
        )
    _require_actor_stamp(actor, recorded_at)
    store = _request_store(project_root)
    event_id = generate_id(
        "event", RESEARCH_REQUEST_RECORDED_EVENT_TYPE, model.request_id
    )
    if store.exists("research-request", model.request_id):
        if event_log is None or event_log.get(event_id) is not None:
            # Record and its deterministic event both present (or no log
            # to prove either way): a true duplicate -- never a silent
            # re-registration.
            raise DuplicateRequestError(
                f"research request {model.request_id!r} is already"
                " registered; research-request records are immutable and"
                " each request_id is written exactly once"
            )
        # Crash window: the record write landed but the event append did
        # not -- heal the log with the deterministic event and report the
        # original record (replayed convergence).
        stored = _read_request_record(store, model.request_id)
        record = _append(
            event_log,
            _request_recorded_event(model.request_id, actor, recorded_at),
            idempotency_key=(
                f"{RESEARCH_REQUEST_RECORDED_EVENT_TYPE}:{model.request_id}"
            ),
        )
        return RequestRegistration(
            request=stored, event_record=record, replayed=True
        )
    store.write("research-request", model.request_id, model.to_dict())
    record = _append(
        event_log,
        _request_recorded_event(model.request_id, actor, recorded_at),
        idempotency_key=(
            f"{RESEARCH_REQUEST_RECORDED_EVENT_TYPE}:{model.request_id}"
        ),
    )
    return RequestRegistration(request=model, event_record=record)


# ---------------------------------------------------------------------------
# Research-request lifecycle APIs (through the normative rule table)
# ---------------------------------------------------------------------------


def advance_research_request(
    root: str | Path,
    request_id: str,
    to_status: ResearchRequestStatus,
    *,
    actor: str,
    reason: str,
    at: str,
    event_log: ProjectEventLog | None = None,
) -> RequestAdvancement:
    """Advance one research request to ``to_status`` through the rule table.

    Reads the **persisted** request, validates the move against the
    normative lifecycle rule table of ``research.requests``
    (R-REQ-S0/C1/E1; any other pair -- including no-op transitions,
    R-REQ-D1 -- raises ``IllegalRequestTransitionError``), persists the
    advanced record and appends one ``research-request.transition``
    event (``from``/``to``/``reason``) under a deterministic idempotency
    key.

    Crash-window convergence (monitoring pattern): a re-run whose
    record is already at ``to_status`` is a no-op and is rejected --
    no-op transitions must never enter the audit record -- **unless**
    the deterministic transition event of the unique normative arc into
    ``to_status`` is missing from the log, which proves an earlier
    interrupted call (the record write landed, the event append did
    not); the missing event is then appended idempotently and the call
    returns ``replayed=True``. Without an event log no convergence is
    possible and the no-op guard always wins.

    Args:
        root: the initialized workspace root.
        request_id: the id of the registered request to advance.
        to_status: the target status (a ``ResearchRequestStatus``
            member).
        actor: the acting role agent identity stamped on the event.
        reason: the stable reason for the transition (the event's
            ``reason``).
        at: the injected deterministic transition timestamp.
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`RequestAdvancement` (advanced record, event record,
        replayed flag).

    Raises:
        TypeError: ``root`` is not a str/Path, ``request_id`` /
            ``actor`` / ``reason`` / ``at`` is not a str, or
            ``to_status`` is not a ``ResearchRequestStatus``.
        StateHelperError: ``actor`` / ``reason`` / ``at`` is empty.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        RequestNotFoundError: no request with that id is registered.
        IllegalRequestTransitionError: the pair is not in the normative
            rule table (including no-op transitions).
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(request_id, str):
        raise TypeError(
            f"request_id must be a str, got {type(request_id).__name__}"
        )
    if not isinstance(to_status, ResearchRequestStatus):
        raise TypeError(
            "to_status must be a ResearchRequestStatus, got"
            f" {type(to_status).__name__}"
        )
    _require_nonempty(request_id, "request_id")
    _require_transition_args(actor, reason, at)
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _request_store(project_root)
    current = _read_request(store, project_root, request_id)
    if current.status == to_status:
        if event_log is None:
            raise IllegalRequestTransitionError(current.status, to_status)
        predecessor = REQUEST_PREDECESSOR_STATUS[to_status]
        if predecessor is None:
            raise IllegalRequestTransitionError(current.status, to_status)
        record = _append(
            event_log,
            _request_transition_event(
                request_id, predecessor, to_status, actor, reason, at
            ),
            idempotency_key=(
                f"{RESEARCH_REQUEST_TRANSITION_EVENT_TYPE}:{request_id}:"
                f"{predecessor.value}:{to_status.value}"
            ),
        )
        return RequestAdvancement(
            request=current, event_record=record, replayed=True
        )
    advanced = apply_request_transition(current, to_status)
    store.write("research-request", request_id, advanced.to_dict())
    record = _append(
        event_log,
        _request_transition_event(
            request_id, current.status, to_status, actor, reason, at
        ),
        idempotency_key=(
            f"{RESEARCH_REQUEST_TRANSITION_EVENT_TYPE}:{request_id}:"
            f"{current.status.value}:{to_status.value}"
        ),
    )
    return RequestAdvancement(request=advanced, event_record=record)


def link_result_to_request(
    root: str | Path,
    request_id: str,
    evidence_id: str,
    *,
    linked_by: str,
    linked_at: str,
    event_log: ProjectEventLog | None = None,
) -> RequestLinkageResult:
    """Link one result (evidence) to a registered request.

    Reads the **persisted** request and applies the normative linkage
    rule table of ``research.requests`` (R-LINK-S1: results may only be
    linked while the request is ``SEARCHING``; R-LINK-D1: an evidence id
    may only be linked once). The linked record is persisted and one
    ``research-request.result_linked`` event is appended under the
    deterministic key ``...:<request_id>:<evidence_id>``; the event
    carries ``from`` = the request status at linkage time and ``to`` =
    the linked evidence id, so the audit trail of the link is
    reconstructible from the event alone.

    The link is idempotent on (request, evidence): a re-run that finds
    ``evidence_id`` already linked re-resolves the original event
    (``replayed=True``) when an event log is given -- including after a
    crash between the record write and the event append -- and raises
    ``RequestLinkageError`` (R-LINK-D1) when no event log can prove the
    earlier link.

    Args:
        root: the initialized workspace root.
        request_id: the id of the registered request.
        evidence_id: the id of the evidence (result) to link.
        linked_by: the linking actor stamped on the linkage record and
            event.
        linked_at: the injected deterministic linkage timestamp.
        event_log: the append-only event log to audit through (default:
            a ``ProjectEventLog`` bound to the workspace ``events/``
            directory).

    Returns:
        The :class:`RequestLinkageResult` (linked record, linkage
        record, event record, replayed flag).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``request_id`` /
            ``evidence_id`` / ``linked_by`` / ``linked_at`` is not a
            str.
        StateHelperError: an argument string is empty.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        RequestNotFoundError: no request with that id is registered.
        RequestLinkageError: the linkage rule table rejected the link
            (stable message naming the rejecting rule).
        ValueError: the stored state is corrupt, or the id is not a
            safe object id (state backend).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    for name, value in (
        ("request_id", request_id),
        ("evidence_id", evidence_id),
        ("linked_by", linked_by),
        ("linked_at", linked_at),
    ):
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a str, got {type(value).__name__}")
        _require_nonempty(value, name)
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _request_store(project_root)
    current = _read_request(store, project_root, request_id)
    if evidence_id in current.result_evidence_ids:
        if event_log is None:
            raise RequestLinkageError(
                f"link_result_to_request: cannot link evidence"
                f" {evidence_id!r} to request {request_id!r}: rule"
                " R-LINK-D1 rejected the linkage (an evidence id may only"
                " be linked once)"
            )
        # The deterministic re-append resolves the original event
        # (``replayed=True``); the link's audit record is reconstructed
        # from the event alone.
        replayed_record = event_log.append(
            _result_linked_event(
                request_id, evidence_id, current.status, linked_by, linked_at
            ),
            idempotency_key=(
                f"{RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE}:{request_id}:"
                f"{evidence_id}"
            ),
        )
        linkage = _linkage_from_event(replayed_record)
        return RequestLinkageResult(
            request=current,
            linkage=linkage,
            event_record=replayed_record,
            replayed=True,
        )
    linked, linkage = attach_result_to_request(
        current, evidence_id, linked_by, linked_at
    )
    store.write("research-request", request_id, linked.to_dict())
    record = _append(
        event_log,
        _result_linked_event(
            request_id, evidence_id, current.status, linked_by, linked_at
        ),
        idempotency_key=(
            f"{RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE}:{request_id}:"
            f"{evidence_id}"
        ),
    )
    return RequestLinkageResult(request=linked, linkage=linkage, event_record=record)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_source(root: str | Path, source_id: str) -> ResearchSource:
    """Read one registered source as a typed record.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``source_id`` is not
            a str.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        SourceNotFoundError: no source with that id is registered.
        ValueError: the stored record is corrupt, or the id is not a
            safe object id (state backend).
    """
    _require_root(root)
    _require_nonempty_str(source_id, "source_id")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _source_store(project_root)
    if not store.exists("source", source_id):
        raise SourceNotFoundError(
            f"no source registered with id {source_id!r} at {project_root}"
        )
    return _read_source_record(store, source_id)


def list_sources(root: str | Path) -> tuple[ResearchSource, ...]:
    """List every registered source, sorted by ``source_id``.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        ValueError: a stored record is corrupt.
    """
    _require_root(root)
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _source_store(project_root)
    return tuple(
        _read_source_record(store, source_id) for source_id in store.list_ids("source")
    )


def read_evidence(root: str | Path, evidence_id: str) -> ClaimSpecificEvidence:
    """Read one registered evidence record as a typed record.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``evidence_id`` is
            not a str.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        EvidenceNotFoundError: no evidence with that id is registered.
        ValueError: the stored record is corrupt, or the id is not a
            safe object id (state backend).
    """
    _require_root(root)
    _require_nonempty_str(evidence_id, "evidence_id")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _evidence_store(project_root)
    if not store.exists("evidence", evidence_id):
        raise EvidenceNotFoundError(
            f"no evidence registered with id {evidence_id!r} at {project_root}"
        )
    return _read_evidence_record(store, evidence_id)


def list_evidence(root: str | Path) -> tuple[ClaimSpecificEvidence, ...]:
    """List every registered evidence record, sorted by ``evidence_id``.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        ValueError: a stored record is corrupt.
    """
    _require_root(root)
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _evidence_store(project_root)
    return tuple(
        _read_evidence_record(store, evidence_id)
        for evidence_id in store.list_ids("evidence")
    )


def read_research_request(root: str | Path, request_id: str) -> ResearchRequest:
    """Read one registered research request as a typed record.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``request_id`` is not
            a str.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        RequestNotFoundError: no request with that id is registered.
        ValueError: the stored record is corrupt, or the id is not a
            safe object id (state backend).
    """
    _require_root(root)
    _require_nonempty_str(request_id, "request_id")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _request_store(project_root)
    if not store.exists("research-request", request_id):
        raise RequestNotFoundError(
            f"no research request registered with id {request_id!r} at"
            f" {project_root}"
        )
    return _read_request_record(store, request_id)


def list_research_requests(root: str | Path) -> tuple[ResearchRequest, ...]:
    """List every registered research request, sorted by ``request_id``.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        ValueError: a stored record is corrupt.
    """
    _require_root(root)
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    store = _request_store(project_root)
    return tuple(
        _read_request_record(store, request_id)
        for request_id in store.list_ids("research-request")
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_root(root: str | Path) -> None:
    """Reject a non-str/Path root at the public boundary (TypeError)."""
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")


def _require_nonempty_str(value: str, name: str) -> None:
    """Reject a non-str value at the public boundary (TypeError)."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str, got {type(value).__name__}")


def _require_nonempty(value: str, name: str) -> None:
    """Reject an empty argument string (stable StateHelperError)."""
    if not value:
        raise StateHelperError(f"{name} must not be empty")


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


def _source_store(root: Path) -> FilesystemStateBackend:
    """The durable source registry of a workspace."""
    return FilesystemStateBackend(root / SOURCE_STATE_DIR)


def _evidence_store(root: Path) -> FilesystemStateBackend:
    """The durable evidence registry of a workspace."""
    return FilesystemStateBackend(root / EVIDENCE_STATE_DIR)


def _request_store(root: Path) -> FilesystemStateBackend:
    """The durable research-request registry of a workspace."""
    return FilesystemStateBackend(root / RESEARCH_REQUEST_STATE_DIR)


def _coerce_source(source: SourceInput) -> ResearchSource:
    """Return a typed source from either input form."""
    if isinstance(source, ResearchSource):
        return source
    if isinstance(source, Mapping):
        return ResearchSource.from_dict(source)
    raise TypeError(
        "source must be a ResearchSource or a mapping, got"
        f" {type(source).__name__}"
    )


def _coerce_evidence(evidence: EvidenceInput) -> ClaimSpecificEvidence:
    """Return a typed evidence record from either input form."""
    if isinstance(evidence, ClaimSpecificEvidence):
        return evidence
    if isinstance(evidence, Mapping):
        return ClaimSpecificEvidence.from_dict(evidence)
    raise TypeError(
        "evidence must be a ClaimSpecificEvidence or a mapping, got"
        f" {type(evidence).__name__}"
    )


def _coerce_request(request: RequestInput) -> ResearchRequest:
    """Return a typed research request from either input form."""
    if isinstance(request, ResearchRequest):
        return request
    if isinstance(request, Mapping):
        return ResearchRequest.from_dict(request)
    raise TypeError(
        "request must be a ResearchRequest or a mapping, got"
        f" {type(request).__name__}"
    )


def _reject_mirror_collision(
    store: FilesystemStateBackend,
    source: ResearchSource,
    identity: SourceIdentity,
) -> None:
    """Reject a source whose canonical identity is already registered.

    06-EVIDENCE-SYSTEM.md section 7 / ``agent-contracts/RESEARCH.md``:
    mirrored copies of one paper are never independent evidence -- a
    mirror-collapsible source (same DOI / stable identifier / URL as an
    already-registered source of a different id) is rejected at
    authoring time instead of being persisted as a duplicate of the
    work. Record-scoped types (SI/dataset/structure depositions) keep
    their own ``record:<source_id>`` identity and never collide.

    Raises:
        DuplicateSourceError: the canonical identity key is already
            registered under a different source id.
        SourceNormalizationError: a registered source carries a DOI that
            cannot be normalized (re-raised with the colliding record's
            id in the message).
        ValueError: a stored record is corrupt.
    """
    for other_id in store.list_ids("source"):
        if other_id == source.source_id:
            continue
        other = _read_source_record(store, other_id)
        try:
            other_identity = canonical_identity(other)
        except SourceNormalizationError as exc:
            raise SourceNormalizationError(
                f"cannot dedupe source {source.source_id!r} against"
                f" registered source {other_id!r}: {exc}"
            ) from exc
        if other_identity.key == identity.key:
            raise DuplicateSourceError(
                f"source {source.source_id!r} mirrors registered source"
                f" {other_id!r}: both carry the canonical identity"
                f" {identity.key!r}; mirrors of one work are never"
                " independent evidence (06-EVIDENCE-SYSTEM.md section 7) --"
                " merge the records instead of registering the mirror"
            )


def _read_source_record(
    store: FilesystemStateBackend, source_id: str
) -> ResearchSource:
    """Parse one source record, rejecting corrupt state with a stable error."""
    data = store.read("source", source_id)
    try:
        return ResearchSource.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"corrupt source record for {source_id!r}: {exc}"
        ) from exc


def _read_evidence_record(
    store: FilesystemStateBackend, evidence_id: str
) -> ClaimSpecificEvidence:
    """Parse one evidence record, rejecting corrupt state with a stable error."""
    data = store.read("evidence", evidence_id)
    try:
        return ClaimSpecificEvidence.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"corrupt evidence record for {evidence_id!r}: {exc}"
        ) from exc


def _read_request_record(
    store: FilesystemStateBackend, request_id: str
) -> ResearchRequest:
    """Parse one research-request record, rejecting corrupt state."""
    data = store.read("research-request", request_id)
    try:
        return ResearchRequest.from_dict(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"corrupt research-request record for {request_id!r}: {exc}"
        ) from exc


def _read_request(
    store: FilesystemStateBackend, project_root: Path, request_id: str
) -> ResearchRequest:
    """Read one registered request; raise ``RequestNotFoundError`` when absent."""
    if not store.exists("research-request", request_id):
        raise RequestNotFoundError(
            f"no research request registered with id {request_id!r} at"
            f" {project_root}"
        )
    return _read_request_record(store, request_id)


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


def _source_recorded_event(
    source_id: str, actor: str, recorded_at: str
) -> ProjectEvent:
    """The deterministic ``source.recorded`` event of one source."""
    return ProjectEvent(
        event_id=generate_id("event", SOURCE_RECORDED_EVENT_TYPE, source_id),
        timestamp=recorded_at,
        actor=actor,
        event_type=SOURCE_RECORDED_EVENT_TYPE,
        object_id=source_id,
    )


def _evidence_recorded_event(
    evidence_id: str, actor: str, recorded_at: str
) -> ProjectEvent:
    """The deterministic ``evidence.recorded`` event of one record."""
    return ProjectEvent(
        event_id=generate_id(
            "event", EVIDENCE_RECORDED_EVENT_TYPE, evidence_id
        ),
        timestamp=recorded_at,
        actor=actor,
        event_type=EVIDENCE_RECORDED_EVENT_TYPE,
        object_id=evidence_id,
    )


def _request_recorded_event(
    request_id: str, actor: str, recorded_at: str
) -> ProjectEvent:
    """The deterministic ``research-request.recorded`` event of one request."""
    return ProjectEvent(
        event_id=generate_id(
            "event", RESEARCH_REQUEST_RECORDED_EVENT_TYPE, request_id
        ),
        timestamp=recorded_at,
        actor=actor,
        event_type=RESEARCH_REQUEST_RECORDED_EVENT_TYPE,
        object_id=request_id,
    )


def _request_transition_event(
    request_id: str,
    from_status: ResearchRequestStatus,
    to_status: ResearchRequestStatus,
    actor: str,
    reason: str,
    at: str,
) -> ProjectEvent:
    """The deterministic transition event of one lifecycle move.

    The event id is a pure function of (request id, from, to) -- the
    same pair re-appended under the same idempotency key resolves to the
    single original record (exactly-once, monitoring pattern).
    """
    return ProjectEvent(
        event_id=generate_id(
            "event",
            RESEARCH_REQUEST_TRANSITION_EVENT_TYPE,
            request_id,
            from_status.value,
            to_status.value,
        ),
        timestamp=at,
        actor=actor,
        event_type=RESEARCH_REQUEST_TRANSITION_EVENT_TYPE,
        object_id=request_id,
        from_=from_status.value,
        to=to_status.value,
        reason=reason,
    )


def _result_linked_event(
    request_id: str,
    evidence_id: str,
    request_status: ResearchRequestStatus,
    linked_by: str,
    linked_at: str,
) -> ProjectEvent:
    """The deterministic ``research-request.result_linked`` event.

    ``from`` = the request status at linkage time, ``to`` = the linked
    evidence id, so the link's audit trail (request id, evidence id,
    when, by whom, in which state) is reconstructible from the event
    alone -- the same trace the ``ResultLinkageRecord`` carries.
    """
    return ProjectEvent(
        event_id=generate_id(
            "event",
            RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE,
            request_id,
            evidence_id,
        ),
        timestamp=linked_at,
        actor=linked_by,
        event_type=RESEARCH_REQUEST_RESULT_LINKED_EVENT_TYPE,
        object_id=request_id,
        from_=request_status.value,
        to=evidence_id,
    )


def _linkage_from_event(record: EventRecord) -> ResultLinkageRecord:
    """Reconstruct the linkage record of a replayed event.

    The linkage event carries everything the ``ResultLinkageRecord``
    carries (request id, evidence id, timestamp, actor, request status
    at linkage time), so the audit record of an already-linked result is
    reconstructible from the resolved event alone.
    """
    event = record.event
    if event.from_ is None:
        raise ValueError(
            f"corrupt result-linked event {event.event_id!r}: missing"
            " 'from' status"
        )
    return ResultLinkageRecord(
        request_id=event.object_id or "",
        evidence_id=event.to or "",
        linked_at=event.timestamp,
        linked_by=event.actor,
        request_status=ResearchRequestStatus(event.from_),
    )
