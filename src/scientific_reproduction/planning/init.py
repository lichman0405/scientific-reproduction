"""Deterministic project initialization primitives (DEV-M4-G01).

Implements the API/CLI primitives that initialize a one-paper reproduction
project and register its Primary Target Paper (``01-PRODUCT-REQUIREMENTS.md``
SS4: "User invokes ``/reproduce <paper>``" -> "Skill initializes a new
Git-backed project workspace").

Public API
----------
* ``initialize_project`` -- the project initializer: creates the one-paper
  workspace tree (``templates/PROJECT-TREE.template.txt``), writes the
  project state record ``project.yaml``, appends the ``project.initialized``
  event as a first-class log record (``ProjectEventLog`` over the
  workspace root, sequence 1), initializes the Git repository through the
  M3-G01 helpers (``audit.git.init_project_repo``) and records the
  "project initialized" audit checkpoint commit
  (``audit.git.commit_checkpoint``, ``14-STATE-GIT-ARTIFACTS.md`` SS5).
* ``register_primary_target`` -- primary target registration: the
  one-primary enforcement point. AC-01: exactly ONE Primary Target Paper
  per project; a second registration raises ``TargetAlreadyRegisteredError``
  with a stable message. At initialization the primary target is always
  registered (the frozen ``Project`` schema makes ``primary_target`` a
  required single object, ``schemas/project.schema.yaml``), so on an
  unmodified project this API always rejects -- it exists to make the
  one-primary invariant an enforced behavior, not just a schema property.
* ``parse_target_form`` / ``detect_target_form`` -- AC-03: the three target
  forms ``pdf`` (local path), ``doi`` and ``url`` (values frozen in
  ``TargetSourceType``, ``core/models.py``) are detected deterministically
  and mapped losslessly into the frozen ``PrimaryTarget`` model. Malformed
  forms -- bad DOI syntax, unparsable/non-http(s) URLs, non-``.pdf`` PDF
  paths -- are rejected with stable messages (``TargetValidationError``).
* ``read_project_state`` -- read the persisted ``project.yaml`` record back
  as a typed ``Project`` model.

Deliverables mapping
--------------------
* project initializer -- ``initialize_project`` (Git + state structures,
  AC-02);
* primary target registration -- ``register_primary_target`` + the target
  recorded at init (AC-01);
* initial project directories/state -- the ``INIT_DIRECTORIES`` tree plus
  ``project.yaml`` and the init event (AC-02).

Determinism
-----------
No wall-clock, randomness or network anywhere on the deterministic path:
``project_id`` is derived from the target via ``core.ids.generate_id``,
timestamps are injectable (``timestamp``/``commit_time``; a testable path
passes a fixed value, production default is now-UTC only for the state
records the frozen model requires), the Git commit reuses the M3-G01
helpers with their explicit identity and injectable commit time, and the
state/event records are written with canonical JSON through
``core.atomic.atomic_write``. ``project.yaml`` is JSON content: JSON is a
valid YAML document, so the file honors the ``project.yaml`` name of
``templates/PROJECT-TREE.template.txt`` while staying stdlib-serializable
and consistent with the JSON state convention of ``14-STATE-GIT-ARTIFACTS.md``
SS2/SS3 and ``core/state_backend.py``.

No lab/HPC inventory is required (AC-02): initialization never discovers
inventory, never reads configuration and never touches the network; the
``inventory/`` directory is created empty and the full state is
deterministic from the inputs alone.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypeAlias
from urllib.parse import urlparse

from scientific_reproduction.audit.git import (
    AuditIdentity,
    CommitRecord,
    commit_checkpoint,
    init_project_repo,
)
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    PrimaryTarget,
    Project,
    ProjectEvent,
    ProjectPhase,
    ReproductionOutcome,
    TargetSourceType,
)
from scientific_reproduction.core.schema_validation import validate_and_reject

__all__ = [
    "DEFAULT_AUDIT_IDENTITY",
    "DEFAULT_DOMAIN_PACK",
    "DEFAULT_STATE_BACKEND",
    "INIT_DIRECTORIES",
    "INIT_EVENT_ACTOR",
    "INIT_EVENT_TYPE",
    "INITIAL_PLAN_VERSION",
    "PROJECT_STATE_FILENAME",
    "PlanningError",
    "ProjectAlreadyInitializedError",
    "ProjectInitResult",
    "ProjectNotInitializedError",
    "TargetAlreadyRegisteredError",
    "TargetValidationError",
    "TargetInput",
    "detect_target_form",
    "initialize_project",
    "parse_target_form",
    "read_project_state",
    "register_primary_target",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanningError(Exception):
    """Base class for all planning subsystem errors."""


class TargetValidationError(PlanningError, ValueError):
    """Raised when a target paper value is not a valid PDF/DOI/URL form."""


class ProjectAlreadyInitializedError(PlanningError, ValueError):
    """Raised when ``initialize_project`` runs on an already-initialized root."""


class ProjectNotInitializedError(PlanningError, ValueError):
    """Raised when an operation requires an initialized project and none exists."""


class TargetAlreadyRegisteredError(PlanningError, ValueError):
    """Raised when a second primary target registration is attempted (AC-01)."""


# ---------------------------------------------------------------------------
# Frozen constants (spec sources cited in the module docstring)
# ---------------------------------------------------------------------------

#: Name of the project state record at the workspace root
#: (``templates/PROJECT-TREE.template.txt``).
PROJECT_STATE_FILENAME: str = "project.yaml"

#: The event type recorded for project initialization; matches
#: ``audit.git.EVENT_TYPE_TO_CHECKPOINT["project.initialized"]`` and the
#: "project initialized" audit checkpoint of ``14-STATE-GIT-ARTIFACTS.md`` SS5.
INIT_EVENT_TYPE: str = "project.initialized"

#: Actor of the initialization event: the user invoking ``/reproduce``
#: (``01-PRODUCT-REQUIREMENTS.md`` SS4 step 1).
INIT_EVENT_ACTOR: str = "user"

#: Initial project phase of a fresh project (``schemas/project.schema.yaml``).
INITIAL_PHASE: ProjectPhase = ProjectPhase.INITIALIZING

#: Initial reproduction outcome (``schemas/project.schema.yaml``).
INITIAL_OUTCOME: ReproductionOutcome = ReproductionOutcome.UNDETERMINED

#: Initial plan version: plan versions start at ``v1`` and drafts are the
#: pre-freeze state (``examples/fdm-201/project.example.yaml`` uses
#: ``v1-draft``; ``17-FDM201-REFERENCE-CASE.md``).
INITIAL_PLAN_VERSION: str = "v1-draft"

#: Default domain pack for v0.1 (``16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md``).
DEFAULT_DOMAIN_PACK: str = "materials-chemistry"

#: The v0.1 state backend (``schemas/project.schema.yaml`` const).
DEFAULT_STATE_BACKEND: Literal["filesystem"] = "filesystem"

#: Deterministic default author/committer identity for the init audit
#: commit; callers may pass their own ``AuditIdentity``.
DEFAULT_AUDIT_IDENTITY: AuditIdentity = AuditIdentity(
    name="Scientific Reproduction", email="repro@example.org"
)

#: The one-paper workspace tree (``templates/PROJECT-TREE.template.txt``).
#: Git itself (``.git``) is created by ``audit.git.init_project_repo``.
INIT_DIRECTORIES: tuple[str, ...] = (
    "sources",
    "knowledge",
    "inventory",
    "plans",
    "work-packages",
    "requirements",
    "goals",
    "runs",
    "evidence",
    "assumptions",
    "protocols",
    "analysis",
    "decisions",
    "events",
    "resources",
    "human-gates",
    "manifests",
    "locks",
    "lab/outgoing",
    "lab/incoming",
    "reports",
)

#: DOI syntax: ``10.<registrant>/<suffix>`` with 4-9 registrant digits and
#: a suffix of the characters DOI permits (ISO 26324 style).
DOI_PATTERN: re.Pattern[str] = re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$")

#: A user-supplied target: the raw string form or an already-parsed model.
TargetInput: TypeAlias = str | PrimaryTarget

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

# ---------------------------------------------------------------------------
# Target form parsing (AC-03)
# ---------------------------------------------------------------------------


def detect_target_form(value: str) -> TargetSourceType:
    """Detect the target form of ``value``: pdf / doi / url.

    Detection is deterministic and order-stable: a DOI matches the DOI
    pattern first, then an http(s) URL, then a ``.pdf``-suffixed local
    path. Any other value raises ``TargetValidationError``.

    Args:
        value: the raw target string.

    Raises:
        TypeError: ``value`` is not a string.
        TargetValidationError: the value matches none of the three forms.
    """
    if not isinstance(value, str):
        raise TypeError(f"target must be a str, got {type(value).__name__}")
    if _is_doi(value):
        return TargetSourceType.DOI
    if _is_http_url(value):
        return TargetSourceType.URL
    if value.lower().endswith(".pdf"):
        return TargetSourceType.PDF
    raise TargetValidationError(
        f"cannot determine target form of {value!r}: expected a local PDF path"
        " (ending in '.pdf'), a DOI (10.<registrant>/<suffix>), or an http(s) URL"
    )


def parse_target_form(
    value: str,
    *,
    title: str | None = None,
    form: TargetSourceType | None = None,
) -> PrimaryTarget:
    """Parse a raw target paper into the frozen ``PrimaryTarget`` model.

    With ``form`` given the value is validated strictly against that form;
    without it the form is detected (``detect_target_form``). The parsed
    model preserves the form without loss (AC-03): the identifier carries
    the value as given, and for the DOI form ``doi`` mirrors the
    identifier (``examples/fdm-201/project.example.yaml``).

    Args:
        value: the raw target: a local PDF path, a DOI, or an http(s) URL.
        title: optional paper title recorded on the ``PrimaryTarget``.
        form: optional explicit form; when given, values that do not match
            it are rejected even when they match another form.

    Raises:
        TypeError: any argument has the wrong type.
        TargetValidationError: the value is malformed for its form, the
            explicit ``form`` does not match the value, or the form cannot
            be determined.
    """
    if not isinstance(value, str):
        raise TypeError(f"target must be a str, got {type(value).__name__}")
    if title is not None and not isinstance(title, str):
        raise TypeError(f"title must be a str, got {type(title).__name__}")
    if form is not None and not isinstance(form, TargetSourceType):
        raise TypeError(
            f"form must be a TargetSourceType, got {type(form).__name__}"
        )
    if form == TargetSourceType.DOI:
        if not _is_doi(value):
            raise TargetValidationError(
                f"malformed DOI target {value!r}: expected 10.<4-9 digits>/<suffix>"
            )
        return PrimaryTarget(
            source_type=TargetSourceType.DOI, identifier=value, doi=value, title=title
        )
    if form == TargetSourceType.URL:
        if not _is_http_url(value):
            raise TargetValidationError(
                f"unparsable URL target {value!r}: expected an http(s) URL with a host"
            )
        return PrimaryTarget(source_type=TargetSourceType.URL, identifier=value, title=title)
    if form == TargetSourceType.PDF:
        if not value.lower().endswith(".pdf"):
            raise TargetValidationError(
                f"invalid PDF target {value!r}: a PDF target must be a local path"
                " ending in '.pdf'"
            )
        return PrimaryTarget(source_type=TargetSourceType.PDF, identifier=value, title=title)
    detected = detect_target_form(value)
    if detected == TargetSourceType.DOI:
        return PrimaryTarget(
            source_type=TargetSourceType.DOI, identifier=value, doi=value, title=title
        )
    if detected == TargetSourceType.URL:
        return PrimaryTarget(source_type=TargetSourceType.URL, identifier=value, title=title)
    return PrimaryTarget(source_type=TargetSourceType.PDF, identifier=value, title=title)


def _is_doi(value: str) -> bool:
    return DOI_PATTERN.fullmatch(value) is not None


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc != ""
        and parsed.hostname is not None
    )


# ---------------------------------------------------------------------------
# Project initialization (AC-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectInitResult:
    """The full, deterministic result of ``initialize_project``.

    Attributes:
        project_root: the resolved workspace root.
        project: the typed ``Project`` state record written to disk.
        project_state_path: the ``project.yaml`` record path.
        event: the ``project.initialized`` event appended to the workspace.
        commit: the audit checkpoint commit (kind ``project.initialized``)
            that records the initial state in Git.
    """

    project_root: Path
    project: Project
    project_state_path: Path
    event: ProjectEvent
    commit: CommitRecord


def initialize_project(
    root: str | Path,
    target: TargetInput,
    *,
    project_id: str | None = None,
    title: str | None = None,
    domain_pack: str = DEFAULT_DOMAIN_PACK,
    timestamp: datetime | None = None,
    identity: AuditIdentity = DEFAULT_AUDIT_IDENTITY,
    commit_time: datetime | None = None,
) -> ProjectInitResult:
    """Initialize a one-paper reproduction project and register its primary target.

    Creates the workspace tree (``INIT_DIRECTORIES``), writes
    ``project.yaml``, appends the ``project.initialized`` event through
    ``ProjectEventLog`` (a first-class log record with sequence 1 in the
    canonical ``events/`` directory), initializes the Git repository and
    creates the "project initialized" audit commit (AC-02: no lab/HPC
    inventory is required anywhere on this path). The primary target is
    registered exactly once, as the single required ``primary_target`` of
    the project record (AC-01, AC-03).

    Args:
        root: workspace root directory; created (with parents) when missing.
        target: the primary target paper as a raw string (PDF path, DOI, or
            http(s) URL) or an already-parsed ``PrimaryTarget``.
        project_id: explicit project id; defaults to a deterministic id
            derived from the target (``core.ids.generate_id``).
        title: optional project title.
        domain_pack: domain pack identifier (default
            ``DEFAULT_DOMAIN_PACK``).
        timestamp: timezone-aware timestamp for the state/event records;
            defaults to now-UTC. State-content tests pass a fixed value.
        identity: author/committer identity for the audit commit.
        commit_time: timezone-aware commit time; defaults to ``timestamp``.

    Returns:
        A ``ProjectInitResult`` carrying the root, the typed project
        record, the state path, the init event, and the audit commit.

    Raises:
        TypeError: any argument has the wrong type.
        ValueError: ``timestamp``/``commit_time`` are naive, or the target
            is malformed (subclass ``TargetValidationError``).
        ProjectAlreadyInitializedError: ``root`` already contains a
            ``project.yaml`` record.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(domain_pack, str):
        raise TypeError(f"domain_pack must be a str, got {type(domain_pack).__name__}")
    if project_id is not None and not isinstance(project_id, str):
        raise TypeError(
            f"project_id must be a str, got {type(project_id).__name__}"
        )
    if title is not None and not isinstance(title, str):
        raise TypeError(f"title must be a str, got {type(title).__name__}")
    if not isinstance(identity, AuditIdentity):
        raise TypeError(
            f"identity must be an AuditIdentity, got {type(identity).__name__}"
        )
    if commit_time is not None and not isinstance(commit_time, datetime):
        raise TypeError(
            f"commit_time must be a datetime, got {type(commit_time).__name__}"
        )

    project_root = Path(root).resolve()
    state_path = project_root / PROJECT_STATE_FILENAME
    if state_path.is_file():
        raise ProjectAlreadyInitializedError(
            f"project already initialized at {project_root}:"
            f" {PROJECT_STATE_FILENAME} exists"
        )

    primary_target = _coerce_target(target, title=title)
    effective_time = _resolve_timestamp(timestamp, name="timestamp")
    effective_commit_time = (
        _resolve_timestamp(commit_time, name="commit_time")
        if commit_time is not None
        else effective_time
    )
    if project_id is None:
        project_id = generate_id(
            "project", primary_target.source_type.value, primary_target.identifier
        )

    project = Project(
        project_id=project_id,
        primary_target=primary_target,
        project_phase=INITIAL_PHASE,
        reproduction_outcome=INITIAL_OUTCOME,
        current_plan_version=INITIAL_PLAN_VERSION,
        title=title,
        domain_pack=domain_pack,
        state_backend=DEFAULT_STATE_BACKEND,
        created_at=_format_iso(effective_time),
        updated_at=_format_iso(effective_time),
    )
    validate_and_reject("project", project.to_dict())

    for rel_dir in INIT_DIRECTORIES:
        (project_root / rel_dir).mkdir(parents=True, exist_ok=True)
    atomic_write(state_path, _canonical_json(project.to_dict()))

    event = ProjectEvent(
        event_id=generate_id("event", INIT_EVENT_TYPE, project_id),
        timestamp=_format_iso(effective_time),
        actor=INIT_EVENT_ACTOR,
        event_type=INIT_EVENT_TYPE,
        object_id=project_id,
        to=INITIAL_PHASE.value,
    )
    validate_and_reject("event", event.to_dict())
    # The init event goes through ``ProjectEventLog`` so it is a first-
    # class log record (sequence 1) in the canonical ``events/`` tree
    # directory -- the same directory a ``ProjectEventLog`` over the
    # workspace root reads, and the same file the state backend resolves
    # for obj_type ``event`` (``SCHEMA_TO_STATE_DIR``).
    event_path = project_root / "events" / f"{event.event_id}.json"
    ProjectEventLog(project_root).append(event)

    init_project_repo(project_root, identity=identity)
    commit = commit_checkpoint(
        project_root,
        kind=INIT_EVENT_TYPE,
        files=[state_path, event_path],
        identity=identity,
        commit_time=effective_commit_time,
    )
    return ProjectInitResult(
        project_root=project_root,
        project=project,
        project_state_path=state_path,
        event=event,
        commit=commit,
    )


# ---------------------------------------------------------------------------
# Primary target registration (AC-01)
# ---------------------------------------------------------------------------


def register_primary_target(
    root: str | Path,
    target: TargetInput,
    *,
    title: str | None = None,
    timestamp: datetime | None = None,
) -> PrimaryTarget:
    """Register the primary target of an initialized project (AC-01).

    The one-primary enforcement point: a project already carrying a
    primary target rejects any registration with a stable
    ``TargetAlreadyRegisteredError``, and the frozen ``Project`` model
    (``schemas/project.schema.yaml``) structurally allows exactly one
    ``primary_target`` object. Because ``initialize_project`` always
    registers the primary target at creation, this API rejects on every
    unmodified project -- it exists to enforce the invariant; the write
    path only applies to state whose primary target has been removed by
    hand edit.

    Args:
        root: the initialized workspace root.
        target: the primary target paper (raw string or parsed model).
        title: optional paper title recorded on the ``PrimaryTarget``.
        timestamp: timezone-aware timestamp for the state update; defaults
            to now-UTC.

    Returns:
        The registered ``PrimaryTarget``.

    Raises:
        TypeError: any argument has the wrong type.
        ValueError: ``timestamp`` is naive, or the target is malformed
            (subclass ``TargetValidationError``).
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        TargetAlreadyRegisteredError: the project already has a primary
            target (AC-01).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if title is not None and not isinstance(title, str):
        raise TypeError(f"title must be a str, got {type(title).__name__}")
    if timestamp is not None and not isinstance(timestamp, datetime):
        raise TypeError(
            f"timestamp must be a datetime, got {type(timestamp).__name__}"
        )
    project_root = Path(root).resolve()
    state_path = project_root / PROJECT_STATE_FILENAME
    if not state_path.is_file():
        raise ProjectNotInitializedError(
            f"cannot register a primary target: no project state at {project_root}"
            f" ({PROJECT_STATE_FILENAME} missing); initialize the project first"
        )
    project = _read_project_state(state_path)
    if project.primary_target is not None:
        existing = project.primary_target
        raise TargetAlreadyRegisteredError(
            f"a primary target is already registered for project"
            f" {project.project_id}: {existing.source_type.value}"
            f" {existing.identifier!r}; a project has exactly one primary"
            " target paper (AC-01)"
        )
    primary_target = _coerce_target(target, title=title)
    effective_time = _resolve_timestamp(timestamp, name="timestamp")
    updated = replace(
        project, primary_target=primary_target, updated_at=_format_iso(effective_time)
    )
    validate_and_reject("project", updated.to_dict())
    atomic_write(state_path, _canonical_json(updated.to_dict()))
    return primary_target


# ---------------------------------------------------------------------------
# State reads
# ---------------------------------------------------------------------------


def read_project_state(root: str | Path) -> Project:
    """Read the persisted ``project.yaml`` record as a typed ``Project``.

    Args:
        root: the initialized workspace root.

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: the record is corrupt (unparseable or not an object).
        TypeError: the record is missing schema-required fields.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    state_path = project_root / PROJECT_STATE_FILENAME
    if not state_path.is_file():
        raise ProjectNotInitializedError(
            f"no project state at {project_root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )
    return _read_project_state(state_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_target(target: TargetInput, *, title: str | None = None) -> PrimaryTarget:
    """Return a form-validated ``PrimaryTarget`` from either input form."""
    if isinstance(target, str):
        return parse_target_form(target, title=title)
    if isinstance(target, PrimaryTarget):
        # Re-validate a hand-built model so registered targets are always
        # form-valid; the parsed copy is discarded (the caller's object is
        # preserved exactly, including its optional fields).
        parse_target_form(target.identifier, title=target.title, form=target.source_type)
        return target
    raise TypeError(
        f"target must be a str or PrimaryTarget, got {type(target).__name__}"
    )


def _resolve_timestamp(timestamp: datetime | None, *, name: str) -> datetime:
    """Return the injectable timestamp (default now-UTC); reject naive."""
    if timestamp is None:
        return datetime.now(timezone.utc)
    if not isinstance(timestamp, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(timestamp).__name__}")
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp


def _format_iso(value: datetime) -> str:
    """Format a timezone-aware datetime as git-style UTC ISO-8601 (``Z``)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json(data: dict[str, object]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _read_project_state(state_path: Path) -> Project:
    """Load and type a ``project.yaml`` record, rejecting corrupt state."""
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt project state at {state_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt project state at {state_path}: expected a JSON object"
        )
    return Project.from_dict(raw)
