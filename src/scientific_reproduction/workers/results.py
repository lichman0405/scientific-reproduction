"""Worker Result Package records with typed sections (DEV-M6-G02).

Implements the **worker result package** deliverable of DEV-M6-G02: the
normalized experiment/computation/analysis worker return package
registration -- the counterpart of the DEV-M6-G01 Goal Execution Context
Package. The context generator hands the worker the frozen context
(``workers/context.py``, ``core.models.GoalExecutionContextPackage``,
``05-GOAL-RUN-SCHEMA.md`` SS8); the worker returns a result package for
that context, and this module registers it exactly once, grounded in:

* ``05-GOAL-RUN-SCHEMA.md`` SS7 (Run lifecycle): *"Scientific PASS/FAIL is
  not a Run lifecycle state; it is a review decision stored separately"* --
  the normative basis for AC-02: the worker result package can never
  encode an authoritative requirement-level outcome; requirement PASS/FAIL
  stays with the Supervisor decision layer;
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS4 (operator result requirements):
  *"Operators must not return only 'success/failure'"* -- the Result
  Package contains actual quantities/conditions (facts), raw instrument
  files / structured outputs (data), and "all deviations from protocol;
  failures/interruptions" (deviations);
* ``11-COMPUTATION-SUBSYSTEM.md`` SS4: computation workers "report
  convergence/runtime facts";
* ``12-ANALYSIS-SUBSYSTEM.md`` SS5: the Analysis Result Package contents;
* ``14-STATE-GIT-ARTIFACTS.md`` SS7: the report traceability chain (Report
  -> Decision/Requirement outcome -> Analysis Result -> Run(s) -> Raw
  Artifact manifest(s)) -- the worker result package is the worker-side
  return of that chain: artifacts link through the registered
  ``ArtifactManifest`` records of the DEV-M3-G02 artifact registry.

Typed sections (AC-01)
----------------------
The package has distinct, typed sections: ``facts``
(:class:`WorkerFact` -- measurements/values), ``data``
(:class:`WorkerData` -- structured outputs) and ``deviations``
(:class:`WorkerDeviation` -- what the worker did differently or could not
do). It is structurally impossible to confuse them with
``core.models.SupervisorDecision`` records: the sections are their own
frozen classes; the package carries no Supervisor-decision field (no
``decision_type``, ``actor``, ``rationale``, ``affected_refs``,
``timestamp``) and accepts no ``SupervisorDecision`` value anywhere; the
only contact is the ``decision_refs`` linkage -- opaque ids of Supervisor
decisions, never decision semantics. ``completed_at`` is an injectable
timestamp: no wall clock, no randomness, no network anywhere.

No requirement outcome (AC-02)
------------------------------
The package's facts/deviations may **reference** Requirements by id
(``requirement_refs`` -- pure linkage, never resolved or interpreted
here), but no field can encode an authoritative requirement-level outcome:
there is no outcome-typed field, no verdict enum on requirement refs, and
this module never imports the requirement outcome/closure layers
(``core.rules``, ``planning.inventory``) -- requirement PASS/FAIL stays
with the Supervisor decision layer. ``DeviationType`` is the factual
engineering vocabulary of the docs (``protocol_deviation`` /
``failure`` / ``interruption``); "failure" here is an execution fact
(``10-EXPERIMENT-SUBSYSTEM.md`` SS4 mandates reporting "failures/
interruptions"), not a requirement verdict.

Artifact linkage (AC-03)
------------------------
Every artifact reference (``input_artifact_ids`` /
``output_artifact_ids``) resolves against the **real** DEV-M3-G02
``ArtifactRegistry`` (``manifests/``) at registration: an unregistered
ref is rejected with a stable ``UnresolvedWorkerResultReferenceError``
and nothing is written. Every artifact id is validated as a safe registry
id (no ``/``, no ``\\``, not ``.``/``..``, no glob metacharacters
``* ? [ ]``) at the record boundary (``__post_init__``) **and** re-checked
at the resolution gate before any registry path is constructed
(defense-in-depth -- the hard-won lesson of FND-M9-G02-01). Registration
returns the package together with the :class:`ResultManifest`, which
records exactly the linked references (context, run, artifacts,
requirements, decisions) in deterministic sorted order -- "artifacts are
linked through manifests".

Registry model (locked reading)
-------------------------------
Result records live one file per result at
``workers/results/<result_id>.json`` (mirroring the M9-G02
``analysis/results/`` registry; the directory is created on demand by the
atomic write). The registry is id-keyed and written **exactly once**:
records are immutable, a duplicate ``result_id`` is rejected with a stable
``DuplicateWorkerResultError`` and the original file is never rewritten.
Result ids are validated as safe single path segments so the registry
glob can never escape its directory. ``run_ref`` is shape-validated only:
no Run registry exists in v0.1 (the DEV-M9-G02 reading). Requirement refs
are pure linkage and are never resolved (AC-02); decision refs are pure
linkage -- no decision registry exists in v0.1.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the injectable
inputs: no randomness, no wall clock, no network. ``TypeError`` at the
public boundaries; errors follow the ``ValueError``-subclass convention
with stable messages; ``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from scientific_reproduction.artifacts.exceptions import ArtifactNotFoundError
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import is_valid_id
from scientific_reproduction.core.models import WorkerRole
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)

__all__ = [
    "ARTIFACTS_STATE_DIR",
    "WORKER_RESULT_MANIFEST_VERSION",
    "WORKER_RESULTS_STATE_DIR",
    "DeviationType",
    "DuplicateWorkerResultError",
    "InvalidWorkerResultIdError",
    "ResultManifest",
    "ResultReference",
    "ResultReferenceKind",
    "UnresolvedWorkerResultReferenceError",
    "WorkerData",
    "WorkerDeviation",
    "WorkerFact",
    "WorkerResultError",
    "WorkerResultNotFoundError",
    "WorkerResultPackage",
    "WorkerResultRecordError",
    "WorkerResultRegistration",
    "build_result_manifest",
    "list_worker_results",
    "read_worker_result",
    "register_worker_result",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class WorkerResultError(ValueError):
    """Base class for all worker result registry errors."""


class WorkerResultRecordError(WorkerResultError):
    """Raised when a result package violates the frozen record shape.

    Covers empty/malformed id and reference fields, non-empty-list
    violations, duplicate references, unsafe artifact id entries (not
    safe registry ids) and versions that are not formal ``v<N>``.
    """


class DuplicateWorkerResultError(WorkerResultError):
    """Raised when a result id is registered a second time (no clobbering)."""


class InvalidWorkerResultIdError(WorkerResultError):
    """Raised when a result id is not a safe single registry path segment."""


class WorkerResultNotFoundError(WorkerResultError):
    """Raised when reading a result record that is not registered."""


class UnresolvedWorkerResultReferenceError(WorkerResultError):
    """Raised when a result reference does not resolve to a registered entity.

    AC-01/AC-03: the package names the exact context/artifact refs -- an
    artifact id that is not a registered manifest (or an artifact id that
    is not a safe registry id, rejected at the resolution gate before any
    registry path is constructed) is rejected instead of silently
    drifting.
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Registry directory of the worker result records, relative to the
#: workspace root (``workers/results/<result_id>.json``).
WORKER_RESULTS_STATE_DIR: str = "workers/results"

#: The artifact registry base directory of a project workspace
#: (``14-STATE-GIT-ARTIFACTS.md`` SS6: manifests live under ``manifests/``;
#: ``planning.init.INIT_DIRECTORIES``).
ARTIFACTS_STATE_DIR: str = "manifests"

#: Version of the result-manifest schema (``manifest_version`` key of
#: :class:`ResultManifest`).
WORKER_RESULT_MANIFEST_VERSION: str = "1.0"

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2

#: Formal frozen-goal version syntax (the frozen Goal Contract of the
#: DEV-M6-G01 context always carries a formal ``v<N>`` version).
_FORMAL_VERSION_RE = re.compile(r"^v\d+$")

#: Required record fields (schema keys of the result package).
_REQUIRED_PACKAGE_FIELDS: tuple[str, ...] = (
    "result_id",
    "context_id",
    "worker_role",
    "goal_id",
    "goal_version",
)

#: Optional record fields (schema keys of the result package).
_OPTIONAL_PACKAGE_FIELDS: tuple[str, ...] = (
    "run_ref",
    "facts",
    "data",
    "deviations",
    "input_artifact_ids",
    "output_artifact_ids",
    "decision_refs",
    "environment",
    "completed_at",
)


def _is_safe_registry_id(value: str) -> bool:
    """True iff ``value`` is a safe single registry path segment."""
    return (
        value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
        and not any(char in value for char in "*?[]")
    )


def _validate_result_id(value: str) -> None:
    """Reject result ids that would escape the registry or glob.

    Ids map to ``<result_id>.json`` files under ``workers/results/``, so
    path separators and ``.``/``..`` segments are rejected; glob
    metacharacters (``*``, ``?``, ``[``, ``]``) are rejected as well (they
    cannot select foreign records here -- the listing globs all ``*.json``
    without interpolating the id -- but ids must stay safe for every
    future keyed flow, mirroring the protocol registry of DEV-M9-G01).
    """
    if not _is_safe_registry_id(value):
        raise InvalidWorkerResultIdError(
            f"invalid worker result id {value!r}: ids must be non-empty"
            " single path segments (no '/', no '\\', not '.' or '..')"
            " without glob metacharacters '*', '?', '[' or ']'"
        )


# ---------------------------------------------------------------------------
# The typed sections (AC-01: facts / data / deviations)
# ---------------------------------------------------------------------------


class DeviationType(StrEnum):
    """The factual engineering deviation vocabulary of the result package.

    Values follow the frozen subsystem docs (``10-EXPERIMENT-SUBSYSTEM.md``
    SS4: report "all deviations from protocol; failures/interruptions"):
    what the worker did differently (``protocol_deviation``) or could not
    do (``failure``, ``interruption``). This is execution-fact vocabulary
    -- never a requirement verdict (AC-02).
    """

    PROTOCOL_DEVIATION = "protocol_deviation"
    FAILURE = "failure"
    INTERRUPTION = "interruption"


@dataclass(frozen=True)
class WorkerFact:
    """One worker-produced fact: a measurement or observed value (AC-01).

    ``fact_id`` is a safe id unique within the package's ``facts`` section;
    ``name`` names the measured quantity; ``value`` is a scalar
    (int/float/str/bool); ``unit`` the measurement unit when applicable;
    ``requirement_refs`` is pure linkage (AC-02: a fact may reference a
    Requirement by id but can never declare its outcome).
    """

    fact_id: str
    name: str
    value: int | float | str | bool
    unit: str | None = None
    requirement_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_str(self.fact_id, "fact_id")
        if not _is_safe_registry_id(self.fact_id):
            raise WorkerResultRecordError(
                f"WorkerFact.fact_id {self.fact_id!r} is not a safe registry"
                " id (no '/', no '\\', not '.' or '..', no glob"
                " metacharacters)"
            )
        _require_str(self.name, "name")
        if not self.name.strip():
            raise WorkerResultRecordError(
                f"WorkerFact.name must be a non-empty string, got"
                f" {self.name!r}"
            )
        if not isinstance(self.value, (int, float, str, bool)):
            raise TypeError(
                "WorkerFact.value must be an int, float, str or bool, got"
                f" {type(self.value).__name__}"
            )
        if self.unit is not None:
            _require_str(self.unit, "unit")
            if not self.unit.strip():
                raise WorkerResultRecordError(
                    "WorkerFact.unit must be a non-empty string when set,"
                    " got an empty string"
                )
        _require_ref_list(self, "requirement_refs")

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the fact in canonical field order."""
        data: dict[str, Any] = {
            "fact_id": self.fact_id,
            "name": self.name,
            "value": self.value,
        }
        if self.unit is not None:
            data["unit"] = self.unit
        data["requirement_refs"] = list(self.requirement_refs)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkerFact:
        """Build a fact from a plain dict (schema key names)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                f"WorkerFact.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("fact_id", "name", "value") if name not in data]
        if missing:
            raise WorkerResultRecordError(
                "worker fact is missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {name: data[name] for name in ("fact_id", "name", "value")}
        for name in ("unit", "requirement_refs"):
            if name in data:
                kwargs[name] = data[name]
        return cls(**kwargs)


@dataclass(frozen=True)
class WorkerData:
    """One structured output of the worker (AC-01).

    ``data_id`` is a safe id unique within the package's ``data`` section;
    ``name`` names the structured output; ``format`` its encoding
    (e.g. ``csv``, ``json``); ``summary`` carries optional descriptive
    statistics (numbers/strings), never an outcome.
    """

    data_id: str
    name: str
    format: str
    summary: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_str(self.data_id, "data_id")
        if not _is_safe_registry_id(self.data_id):
            raise WorkerResultRecordError(
                f"WorkerData.data_id {self.data_id!r} is not a safe registry"
                " id (no '/', no '\\', not '.' or '..', no glob"
                " metacharacters)"
            )
        _require_str(self.name, "name")
        if not self.name.strip():
            raise WorkerResultRecordError(
                f"WorkerData.name must be a non-empty string, got"
                f" {self.name!r}"
            )
        _require_str(self.format, "format")
        if not self.format.strip():
            raise WorkerResultRecordError(
                f"WorkerData.format must be a non-empty string, got"
                f" {self.format!r}"
            )
        if not isinstance(self.summary, dict):
            raise TypeError(
                f"WorkerData.summary must be a dict, got"
                f" {type(self.summary).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the data entry in canonical field order."""
        return {
            "data_id": self.data_id,
            "name": self.name,
            "format": self.format,
            "summary": dict(self.summary),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkerData:
        """Build a data entry from a plain dict (schema key names)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                f"WorkerData.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in ("data_id", "name", "format") if name not in data]
        if missing:
            raise WorkerResultRecordError(
                "worker data entry is missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {
            name: data[name] for name in ("data_id", "name", "format")
        }
        if "summary" in data:
            kwargs["summary"] = data["summary"]
        return cls(**kwargs)


@dataclass(frozen=True)
class WorkerDeviation:
    """One deviation: what the worker did differently or could not do (AC-01).

    ``deviation_id`` is a safe id unique within the package's
    ``deviations`` section; ``kind`` is the factual engineering
    vocabulary of :class:`DeviationType` (never a verdict);
    ``description`` states what was done differently or could not be done;
    ``requirement_refs`` is pure linkage (AC-02: a deviation may reference
    a Requirement by id but can never declare its outcome).
    """

    deviation_id: str
    kind: DeviationType
    description: str
    requirement_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_str(self.deviation_id, "deviation_id")
        if not _is_safe_registry_id(self.deviation_id):
            raise WorkerResultRecordError(
                f"WorkerDeviation.deviation_id {self.deviation_id!r} is not"
                " a safe registry id (no '/', no '\\', not '.' or '..', no"
                " glob metacharacters)"
            )
        if not isinstance(self.kind, DeviationType):
            raise TypeError(
                "WorkerDeviation.kind must be a DeviationType member, got"
                f" {self.kind!r}"
            )
        _require_str(self.description, "description")
        if not self.description.strip():
            raise WorkerResultRecordError(
                f"WorkerDeviation.description must be a non-empty string,"
                f" got {self.description!r}"
            )
        _require_ref_list(self, "requirement_refs")

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the deviation in canonical field order."""
        data: dict[str, Any] = {
            "deviation_id": self.deviation_id,
            "kind": self.kind.value,
            "description": self.description,
        }
        data["requirement_refs"] = list(self.requirement_refs)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkerDeviation:
        """Build a deviation from a plain dict (schema key names).

        Raises:
            TypeError: ``data`` is not a mapping.
            WorkerResultRecordError: a required field is missing, or the
                ``kind`` value is not a DeviationType member value.
            TypeError: a field value has the wrong type.
            WorkerResultRecordError: a field value violates the record shape.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"WorkerDeviation.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in ("deviation_id", "kind", "description") if name not in data
        ]
        if missing:
            raise WorkerResultRecordError(
                "worker deviation is missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {}
        for name in ("deviation_id", "description"):
            kwargs[name] = data[name]
        kind = data["kind"]
        if not isinstance(kind, DeviationType):
            try:
                kind = DeviationType(kind)
            except ValueError:
                raise WorkerResultRecordError(
                    "invalid deviation kind"
                    f" {kind!r}: expected a DeviationType member"
                ) from None
        kwargs["kind"] = kind
        if "requirement_refs" in data:
            kwargs["requirement_refs"] = data["requirement_refs"]
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# The frozen result package record (strict __post_init__ validation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerResultPackage:
    """One worker result package record (AC-01/AC-02/AC-03).

    Frozen and hashable: "same package -> same persisted bytes" is
    directly testable and the record cannot be mutated after construction.
    The record is pure data: every check here is a pure function of the
    record itself (``TypeError`` for wrong types at the construction
    boundary, ``WorkerResultRecordError`` for wrong values); resolution of
    the artifact references against the registered manifests happens at
    registration time (:func:`register_worker_result`, AC-03).

    The package names the **exact context it answers**: ``context_id`` is
    the ``context_id`` of the ``GoalExecutionContextPackage`` the worker
    was given (shape-validated as a well-formed generated id of kind
    ``context``; no context registry exists in v0.1) and ``goal_id`` /
    ``goal_version`` echo the frozen goal identity of that context
    (``goal_version`` is the formal ``v<N>`` the frozen contract carries).
    ``run_ref`` is the ``run_id`` of the Run the worker executed, when one
    exists (shape-validated only -- no Run registry exists in v0.1).

    The typed sections (AC-01) hold the worker-produced content:
    ``facts`` (measurements/values), ``data`` (structured outputs) and
    ``deviations`` (what the worker did differently or could not do).
    ``input_artifact_ids`` / ``output_artifact_ids`` are the exact
    ``artifact_id`` values of the registered ``ArtifactManifest`` records
    the worker consumed/produced (AC-03; every entry a safe registry id).
    ``decision_refs`` is the package's only contact with Supervisor
    decisions: opaque ids, never decision semantics (AC-01).
    ``requirement_refs`` on facts/deviations is pure linkage (AC-02).
    ``completed_at`` is an injectable completion timestamp (a string; the
    caller passes it -- no wall clock here).

    Raises:
        TypeError: a field has the wrong type.
        WorkerResultRecordError: a field value violates the record shape
            (empty id/ref, malformed context id or version, duplicate
            reference, unsafe id shape, unsafe artifact id entry).
    """

    result_id: str
    context_id: str
    worker_role: WorkerRole
    goal_id: str
    goal_version: str
    run_ref: str | None = None
    facts: list[WorkerFact] = field(default_factory=list)
    data: list[WorkerData] = field(default_factory=list)
    deviations: list[WorkerDeviation] = field(default_factory=list)
    input_artifact_ids: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    decision_refs: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    completed_at: str | None = None

    def __post_init__(self) -> None:
        _require_str(self.result_id, "result_id")
        if not self.result_id.strip():
            raise WorkerResultRecordError(
                "WorkerResultPackage.result_id must be a non-empty string,"
                f" got {self.result_id!r}"
            )
        # ``result_id`` safety is enforced at the registry boundary
        # (``InvalidWorkerResultIdError`` in register/read/list), mirroring
        # the M9-G02 result registry; the record itself only requires a
        # non-empty id.
        _require_str(self.context_id, "context_id")
        if not is_valid_id(self.context_id, "context"):
            raise WorkerResultRecordError(
                "WorkerResultPackage.context_id"
                f" {self.context_id!r} is not a well-formed generated"
                " context id (sr_context_<32 hex chars>); the ref must name"
                " the exact context_id of the GoalExecutionContextPackage"
                " the worker was given"
            )
        if not isinstance(self.worker_role, WorkerRole):
            raise TypeError(
                "WorkerResultPackage.worker_role must be a WorkerRole"
                f" member, got {self.worker_role!r}"
            )
        _require_str(self.goal_id, "goal_id")
        if not self.goal_id.strip():
            raise WorkerResultRecordError(
                "WorkerResultPackage.goal_id must be a non-empty string, got"
                f" {self.goal_id!r}"
            )
        if not _is_safe_registry_id(self.goal_id):
            raise WorkerResultRecordError(
                f"WorkerResultPackage.goal_id {self.goal_id!r} is not a safe"
                " registry id (no '/', no '\\', not '.' or '..', no glob"
                " metacharacters)"
            )
        _require_str(self.goal_version, "goal_version")
        if _FORMAL_VERSION_RE.fullmatch(self.goal_version) is None:
            raise WorkerResultRecordError(
                f"WorkerResultPackage.goal_version {self.goal_version!r} is"
                " not a formal version 'v<N>' (the frozen Goal Contract of"
                " the answered context carries a formal version)"
            )
        if self.run_ref is not None:
            if not isinstance(self.run_ref, str):
                raise TypeError(
                    "WorkerResultPackage.run_ref must be a str or None, got"
                    f" {type(self.run_ref).__name__}"
                )
            if not self.run_ref.strip():
                raise WorkerResultRecordError(
                    "WorkerResultPackage.run_ref must be a non-empty string"
                    " when set, got an empty string"
                )
            if not _is_safe_registry_id(self.run_ref):
                raise WorkerResultRecordError(
                    f"WorkerResultPackage.run_ref {self.run_ref!r} is not a"
                    " safe Run id (no '/', no '\\', not '.' or '..', no"
                    " glob metacharacters); the ref must name the exact"
                    " run_id of the Run the worker executed"
                )
        _require_section(self, "facts", WorkerFact, "fact_id")
        _require_section(self, "data", WorkerData, "data_id")
        _require_section(self, "deviations", WorkerDeviation, "deviation_id")
        _require_ref_list(self, "input_artifact_ids")
        if not self.input_artifact_ids:
            raise WorkerResultRecordError(
                "WorkerResultPackage.input_artifact_ids must name at least"
                " one input artifact (the manifest ids the worker consumed)"
            )
        # Artifact refs are registry ids: every entry must be a safe single
        # path segment, or resolution at registration time could escape the
        # ``manifests/`` directory (the DEV-M3-G02 artifact registry
        # validates ids only at registration, never at ``get``).
        # ``requirement_refs``/``decision_refs`` stay pure linkage
        # (AC-01/AC-02) and need no safe-id check.
        _require_safe_registry_id_entries(self, "input_artifact_ids")
        _require_ref_list(self, "output_artifact_ids")
        _require_safe_registry_id_entries(self, "output_artifact_ids")
        _require_ref_list(self, "decision_refs")
        if not isinstance(self.environment, dict):
            raise TypeError(
                "WorkerResultPackage.environment must be a dict, got"
                f" {type(self.environment).__name__}"
            )
        if self.completed_at is not None:
            if not isinstance(self.completed_at, str):
                raise TypeError(
                    "WorkerResultPackage.completed_at must be a str or None,"
                    f" got {type(self.completed_at).__name__}"
                )
            if not self.completed_at.strip():
                raise WorkerResultRecordError(
                    "WorkerResultPackage.completed_at must be a non-empty"
                    " string when set, got an empty string"
                )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the package in canonical field order.

        ``None`` optional values are omitted (the ``to_dict()`` convention
        of ``core.models.CoreModel``); every other field -- including empty
        collections -- is emitted, so the persisted bytes are canonical and
        deterministic.
        """
        data: dict[str, Any] = {
            "result_id": self.result_id,
            "context_id": self.context_id,
            "worker_role": self.worker_role.value,
            "goal_id": self.goal_id,
            "goal_version": self.goal_version,
        }
        if self.run_ref is not None:
            data["run_ref"] = self.run_ref
        data["facts"] = [fact.to_dict() for fact in self.facts]
        data["data"] = [entry.to_dict() for entry in self.data]
        data["deviations"] = [deviation.to_dict() for deviation in self.deviations]
        data["input_artifact_ids"] = list(self.input_artifact_ids)
        data["output_artifact_ids"] = list(self.output_artifact_ids)
        data["decision_refs"] = list(self.decision_refs)
        data["environment"] = dict(self.environment)
        if self.completed_at is not None:
            data["completed_at"] = self.completed_at
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkerResultPackage:
        """Build a package from a plain dict (schema key names).

        Every required field must be present (``WorkerResultRecordError``
        otherwise); optional fields are absent-in-the-dict = default.
        String enum values are coerced to ``WorkerRole`` members and
        section entries to their typed classes (an unknown enum value is a
        stable ``WorkerResultRecordError``). Type/value violations are
        rejected by the constructor with the usual ``TypeError`` /
        ``WorkerResultRecordError`` split.

        Raises:
            TypeError: ``data`` is not a mapping.
            WorkerResultRecordError: a required field is missing, or an
                enum value is not a member value.
            TypeError: a field value has the wrong type.
            WorkerResultRecordError: a field value violates the record shape.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "WorkerResultPackage.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in _REQUIRED_PACKAGE_FIELDS if name not in data]
        if missing:
            raise WorkerResultRecordError(
                "worker result package is missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {}
        for name in _REQUIRED_PACKAGE_FIELDS:
            value = data[name]
            if name == "worker_role" and not isinstance(value, WorkerRole):
                try:
                    value = WorkerRole(value)
                except ValueError:
                    raise WorkerResultRecordError(
                        "invalid worker_role value"
                        f" {value!r}: expected a WorkerRole member"
                    ) from None
            kwargs[name] = value
        for name in _OPTIONAL_PACKAGE_FIELDS:
            if name not in data:
                continue
            value = data[name]
            if name in ("facts", "data", "deviations"):
                kwargs[name] = [
                    _coerce_section_entry(name, item) for item in value
                ]
            else:
                kwargs[name] = value
        return cls(**kwargs)


#: A user-supplied result package: the typed model or a schema-shaped dict.
WorkerResultInput: TypeAlias = WorkerResultPackage | Mapping[str, Any]


def _require_str(value: Any, field_name: str) -> None:
    """Reject a non-str value at the record boundary (stable TypeError)."""
    if not isinstance(value, str):
        raise TypeError(
            f"WorkerResultRecord.{field_name} must be a str, got"
            f" {type(value).__name__}"
        )


def _require_ref_list(record: object, field_name: str) -> None:
    """Validate a reference-list field (refs are exact, unique, non-empty)."""
    values = getattr(record, field_name)
    if not isinstance(values, list):
        raise TypeError(
            f"{type(record).__name__}.{field_name} must be a list, got"
            f" {type(values).__name__}"
        )
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise TypeError(
                f"{type(record).__name__}.{field_name} entries must be str,"
                f" got {type(item).__name__}"
            )
        if not item.strip():
            raise WorkerResultRecordError(
                f"{type(record).__name__}.{field_name} entries must be"
                f" non-empty strings, got {item!r}"
            )
        if item in seen:
            raise WorkerResultRecordError(
                f"{type(record).__name__}.{field_name} contains duplicate"
                f" reference {item!r}"
            )
        seen.add(item)


def _require_safe_registry_id_entries(record: object, field_name: str) -> None:
    """Reject reference-list entries that are not safe registry ids.

    Artifact refs resolve to ``<artifact_id>.json`` files under the project
    ``manifests/`` registry at registration time, and that registry
    (DEV-M3-G02) validates ids only at registration, not at ``get``: an
    entry with a path separator, a ``.``/``..`` segment or a glob
    metacharacter could escape the registry directory or select foreign
    records. Every entry is therefore validated here with the module's
    shared safe-id rule (``_is_safe_registry_id``), mirroring the DEV-M9-G01
    protocol-registry fix at the result boundary (FND-M9-G02-01).
    """
    for value in getattr(record, field_name):
        if not _is_safe_registry_id(value):
            raise WorkerResultRecordError(
                f"{type(record).__name__}.{field_name} entry {value!r} is"
                " not a safe registry id (no '/', no '\\', not '.' or '..',"
                " no glob metacharacters '*', '?', '[' or ']')"
            )


def _require_section(
    record: object, field_name: str, entry_type: type, id_field: str
) -> None:
    """Validate a typed section: a list of the entry class with unique ids."""
    values = getattr(record, field_name)
    if not isinstance(values, list):
        raise TypeError(
            f"{type(record).__name__}.{field_name} must be a list, got"
            f" {type(values).__name__}"
        )
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, entry_type):
            raise TypeError(
                f"{type(record).__name__}.{field_name} entries must be"
                f" {entry_type.__name__} instances, got"
                f" {type(item).__name__}"
            )
        entry_id = getattr(item, id_field)
        if entry_id in seen:
            raise WorkerResultRecordError(
                f"{type(record).__name__}.{field_name} contains duplicate"
                f" {id_field} {entry_id!r}"
            )
        seen.add(entry_id)


def _coerce_section_entry(section: str, item: Any) -> Any:
    """Coerce one section dict to its typed entry class."""
    entry_types: dict[str, type[Any]] = {
        "facts": WorkerFact,
        "data": WorkerData,
        "deviations": WorkerDeviation,
    }
    entry_type = entry_types[section]
    if isinstance(item, entry_type):
        return item
    if not isinstance(item, Mapping):
        raise TypeError(
            f"WorkerResultPackage.{section} entries must be"
            f" {entry_type.__name__} instances or mappings, got"
            f" {type(item).__name__}"
        )
    return entry_type.from_dict(item)


# ---------------------------------------------------------------------------
# The result manifest (AC-03: exactly which references were linked)
# ---------------------------------------------------------------------------


class ResultReferenceKind(StrEnum):
    """The manifest's reference-vocabulary: the kind of each linked item.

    Values match no frozen schema enum (the manifest is this module's own
    auditable vocabulary); the CONTEXT kind is the answered context
    package, ARTIFACT_INPUT/ARTIFACT_OUTPUT the linked artifact manifests,
    REQUIREMENT/ DECISION the pure-linkage refs of facts/deviations and of
    the package.
    """

    CONTEXT = "context"
    RUN = "run"
    ARTIFACT_INPUT = "artifact_input"
    ARTIFACT_OUTPUT = "artifact_output"
    REQUIREMENT = "requirement"
    DECISION = "decision"


@dataclass(frozen=True)
class ResultReference:
    """One linked item of a result manifest: kind + id."""

    kind: ResultReferenceKind
    ref_id: str

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the reference."""
        return {"kind": self.kind.value, "ref_id": self.ref_id}


@dataclass(frozen=True)
class ResultManifest:
    """The deterministic reference manifest of one result package (AC-03).

    The manifest records **exactly** which references the package links,
    as ``(kind, ref_id)`` entries sorted by ``(kind, ref_id)``; artifact
    refs only appear after they resolved against the registered
    ``ArtifactManifest`` records of the DEV-M3-G02 registry.
    ``result_hash()`` fingerprints the manifest's canonical JSON, so the
    hash changes iff the linked reference set changes.
    """

    manifest_version: str
    result_id: str
    context_id: str
    goal_id: str
    goal_version: str
    worker_role: WorkerRole
    references: tuple[ResultReference, ...]

    def references_for(
        self, kind: ResultReferenceKind
    ) -> tuple[ResultReference, ...]:
        """The manifest's references of one kind, in stored (sorted) order.

        Raises:
            TypeError: ``kind`` is not a ``ResultReferenceKind``.
        """
        if not isinstance(kind, ResultReferenceKind):
            raise TypeError(
                f"kind must be a ResultReferenceKind, got {type(kind).__name__}"
            )
        return tuple(r for r in self.references if r.kind is kind)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the manifest in canonical field order."""
        return {
            "manifest_version": self.manifest_version,
            "result_id": self.result_id,
            "context_id": self.context_id,
            "goal_id": self.goal_id,
            "goal_version": self.goal_version,
            "worker_role": self.worker_role.value,
            "references": [r.to_dict() for r in self.references],
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"

    def result_hash(self) -> str:
        """SHA-256 hex digest of the manifest's canonical JSON (deterministic)."""
        return hashlib.sha256(
            self.to_canonical_json().encode("utf-8")
        ).hexdigest()


def build_result_manifest(package: WorkerResultPackage) -> ResultManifest:
    """Derive the reference manifest of one result package (AC-03).

    Pure and deterministic: the manifest is a pure function of the
    package. Artifact refs are the package's input/output artifact ids
    (resolved against the registered manifests at registration time);
    requirement refs are the union of the facts'/deviations' pure-linkage
    refs (AC-02); the context ref is the package's ``context_id``.

    Raises:
        TypeError: ``package`` is not a ``WorkerResultPackage``.
    """
    if not isinstance(package, WorkerResultPackage):
        raise TypeError(
            "build_result_manifest expects a WorkerResultPackage, got"
            f" {type(package).__name__}"
        )
    references: list[ResultReference] = [
        ResultReference(ResultReferenceKind.CONTEXT, package.context_id)
    ]
    if package.run_ref is not None:
        references.append(ResultReference(ResultReferenceKind.RUN, package.run_ref))
    references.extend(
        ResultReference(ResultReferenceKind.ARTIFACT_INPUT, artifact_id)
        for artifact_id in package.input_artifact_ids
    )
    references.extend(
        ResultReference(ResultReferenceKind.ARTIFACT_OUTPUT, artifact_id)
        for artifact_id in package.output_artifact_ids
    )
    requirement_ids = {
        ref
        for fact in package.facts
        for ref in fact.requirement_refs
    } | {
        ref
        for deviation in package.deviations
        for ref in deviation.requirement_refs
    }
    references.extend(
        ResultReference(ResultReferenceKind.REQUIREMENT, ref_id)
        for ref_id in sorted(requirement_ids)
    )
    references.extend(
        ResultReference(ResultReferenceKind.DECISION, ref_id)
        for ref_id in package.decision_refs
    )
    return ResultManifest(
        manifest_version=WORKER_RESULT_MANIFEST_VERSION,
        result_id=package.result_id,
        context_id=package.context_id,
        goal_id=package.goal_id,
        goal_version=package.goal_version,
        worker_role=package.worker_role,
        references=tuple(
            sorted(references, key=lambda r: (r.kind.value, r.ref_id))
        ),
    )


# ---------------------------------------------------------------------------
# Registration (exact-reference resolution at the persistence gate)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerResultRegistration:
    """The outcome of one result package registration.

    ``package`` is the frozen record (what is persisted at
    ``workers/results/<result_id>.json``); ``manifest`` records exactly
    which references were linked (AC-03) -- artifact refs appear only
    after they resolved against the registered manifests.
    """

    package: WorkerResultPackage
    manifest: ResultManifest

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the registration (package + manifest)."""
        return {
            "package": self.package.to_dict(),
            "manifest": self.manifest.to_dict(),
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text of the whole registration."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


def register_worker_result(
    root: str | Path, result: WorkerResultInput
) -> WorkerResultRegistration:
    """Register one worker result package at ``workers/results/<id>.json``.

    The worker result package registration entry: the package is
    schema-shaped, id path-escape validated, resolved against the
    registered entities (AC-03) and persisted as canonical JSON via
    ``core.atomic.atomic_write``. Registration is **exactly once** per
    result id: result records are immutable, and a second registration of
    the same id -- even with different content -- is rejected with
    ``DuplicateWorkerResultError`` and the original file is never
    rewritten.

    Reference resolution (AC-03 exactness, in this order): every
    ``input_artifact_ids`` / ``output_artifact_ids`` entry must resolve to
    a registered ``ArtifactManifest`` in the project artifact registry
    (``manifests/``); each id is validated as a safe registry id at the
    record boundary and re-checked at the resolution gate before any
    registry path is constructed (defense-in-depth -- FND-M9-G02-01).
    Unresolved references raise ``UnresolvedWorkerResultReferenceError``
    with a stable message before anything is written. Requirement refs are
    pure linkage (AC-02): never resolved, never interpreted, never closed.
    Decision refs are pure linkage (AC-01): the package never carries
    decision semantics.

    Args:
        root: the initialized workspace root.
        result: the result package as a typed :class:`WorkerResultPackage`
            or a schema-shaped mapping.

    Returns:
        The :class:`WorkerResultRegistration` with the registered package
        and its reference manifest (AC-03).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``result`` is neither a
            ``WorkerResultPackage`` nor a mapping.
        WorkerResultRecordError: the package violates the frozen record
            shape (including unsafe artifact id entries).
        InvalidWorkerResultIdError: the ``result_id`` is not a safe single
            path segment.
        DuplicateWorkerResultError: a result with the same id is already
            registered (stable message, original bytes untouched).
        UnresolvedWorkerResultReferenceError: an artifact reference does
            not resolve to a registered entity, or is not a safe registry
            id at the resolution gate (AC-03).
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored artifact record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_package(result)
    _validate_result_id(model.result_id)
    state_path = _result_path(project_root, model.result_id)
    if state_path.is_file():
        raise DuplicateWorkerResultError(
            f"worker result {model.result_id!r} is already registered;"
            " result records are immutable and each result_id is written"
            " exactly once"
        )
    _resolve_artifact_refs(project_root, model)
    manifest = build_result_manifest(model)
    atomic_write(state_path, _canonical_json(model.to_dict()))
    return WorkerResultRegistration(package=model, manifest=manifest)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_worker_result(root: str | Path, result_id: str) -> WorkerResultPackage:
    """Read one registered result package as a typed record.

    The returned record is the exact stored record (bytes -> model);
    stored files are never rewritten, so this read is stable across
    repeated registration attempts of the same id.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``result_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidWorkerResultIdError: ``result_id`` is not a safe id.
        WorkerResultNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(result_id, str):
        raise TypeError(
            f"result_id must be a str, got {type(result_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_result_id(result_id)
    state_path = _result_path(project_root, result_id)
    if not state_path.is_file():
        raise WorkerResultNotFoundError(
            f"no worker result registered with id {result_id!r} at"
            f" {project_root}"
        )
    return _read_result_file(state_path)


def list_worker_results(root: str | Path) -> tuple[WorkerResultPackage, ...]:
    """List every registered result package, sorted by ``result_id``.

    Deterministic ordering: the registry glob returns files sorted by
    name, and each stored file's ``result_id`` must match its file name
    (a mismatch is a corrupt record).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    results_dir = project_root / WORKER_RESULTS_STATE_DIR
    records: list[WorkerResultPackage] = []
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*.json")):
            records.append(_read_result_file(path))
    return tuple(records)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _coerce_package(result: WorkerResultInput) -> WorkerResultPackage:
    """Return a typed package from either input form."""
    if isinstance(result, WorkerResultPackage):
        return result
    if isinstance(result, Mapping):
        return WorkerResultPackage.from_dict(result)
    raise TypeError(
        "worker result must be a WorkerResultPackage or a mapping, got"
        f" {type(result).__name__}"
    )


def _result_path(root: Path, result_id: str) -> Path:
    """The registry path of one result package record."""
    return root / WORKER_RESULTS_STATE_DIR / f"{result_id}.json"


def _resolve_artifact_refs(root: Path, package: WorkerResultPackage) -> None:
    """Resolve every artifact reference against the artifact registry.

    AC-03: every referenced artifact must be a registered
    ``ArtifactManifest`` in the project ``manifests/`` registry (DEV-M3-G02);
    the exact ids are resolved in sorted order (deterministic error when
    several references are unresolved). Defense-in-depth: each id is
    re-checked as a safe registry id before ``ArtifactRegistry.get`` (the
    artifact registry validates ids only at registration, never at ``get``),
    so the resolution loop never constructs a registry path from an unsafe
    id even if a record somehow bypassed the record-boundary check
    (FND-M9-G02-01).
    """
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    artifact_ids = sorted(
        set(package.input_artifact_ids) | set(package.output_artifact_ids)
    )
    for artifact_id in artifact_ids:
        if not _is_safe_registry_id(artifact_id):
            raise UnresolvedWorkerResultReferenceError(
                f"worker result {package.result_id!r} references artifact"
                f" {artifact_id!r}, which is not a safe registry id (no"
                " '/', no '\\', not '.' or '..', no glob metacharacters"
                " '*', '?', '[' or ']'); the ref must name the exact"
                " artifact_id of a registered artifact manifest"
            )
        try:
            registry.get(artifact_id)
        except ArtifactNotFoundError as exc:
            raise UnresolvedWorkerResultReferenceError(
                f"worker result {package.result_id!r} references artifact"
                f" {artifact_id!r}, which is not registered in the artifact"
                f" registry ({ARTIFACTS_STATE_DIR}/); register the artifact"
                " manifest first"
            ) from exc


def _read_result_file(state_path: Path) -> WorkerResultPackage:
    """Parse one result package file.

    The filename is ``<result_id>.json``; the stored ``result_id`` must
    match the file name (a mismatch is a corrupt record). An unparseable
    or shape-invalid file raises ``ValueError`` with a stable message.
    """
    name = state_path.name
    if not name.endswith(".json"):
        raise ValueError(
            f"corrupt worker result record at {state_path}: expected"
            " '<result_id>.json'"
        )
    result_id = name.removesuffix(".json")
    raw = _read_json_object(state_path, "worker result")
    try:
        record = WorkerResultPackage.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"corrupt worker result record at {state_path}: {exc}"
        ) from exc
    if record.result_id != result_id:
        raise ValueError(
            f"corrupt worker result record at {state_path}: stored"
            f" result_id {record.result_id!r} does not match the file name"
            f" {result_id!r}"
        )
    return record


def _read_json_object(path: Path, kind: str) -> dict[str, Any]:
    """Load and type a record file, rejecting corrupt state with a stable error."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt {kind} record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt {kind} record at {path}: expected a JSON object"
        )
    return raw


def _canonical_json(data: dict[str, Any]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"
