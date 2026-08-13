"""Analysis Result Package records with trace links (DEV-M9-G02).

Implements the **analysis result package** deliverable of DEV-M9-G02
over the frozen ``core.models`` analysis vocabulary and the DEV-M9-G01
protocol lineage registry (``analysis/protocols.py``): analysis outputs
linked to Runs, raw artifacts, the protocol version and the acceptance
criterion, grounded in:

* ``12-ANALYSIS-SUBSYSTEM.md`` SS5 (the Analysis Result Package must
  include input artifact IDs/checksums, analysis protocol ID/version/hash,
  software/environment, QC findings, derived metrics, uncertainty/
  statistics, plots/tables artifacts, warnings/assumptions, the primary/
  exploratory label and reproducible scripts where appropriate);
* ``14-STATE-GIT-ARTIFACTS.md`` SS7 (report traceability: Report claim
  -> Decision / Requirement outcome -> Analysis Result -> Run(s) -> Raw
  Artifact manifest(s));
* ``05-GOAL-RUN-SCHEMA.md`` SS1 (Run -> Artifact(s) / Analysis Result(s))
  and SS6 (a Run is one execution instance of a Goal).

Registry model (normative reading, locked here)
-----------------------------------------------
Result records live one file per result at
``analysis/results/<result_id>.json`` (the ``analysis/`` workspace
directory created by ``planning.init.initialize_project``). The registry
is id-keyed and written **exactly once**: result records are immutable
(AC-02), a duplicate ``result_id`` is rejected with a stable
``DuplicateResultError`` and the original file is never rewritten. The
registry is invisible to the DEV-M4-G04 ``protocols/`` globs and to the
DEV-M9-G01 versioned listing (both glob in the sibling ``protocols/``
tree), so protocol and result registries stay isolated; result ids are
validated as safe single path segments (no path separators, not
``.``/``..``, no glob metacharacters) so the registry glob can never
escape its directory or select foreign records.

Exact input references (AC-01)
------------------------------
The record names the **exact** input Run ref (``run_ref`` -- the
``run_id`` of the Run record the analysis consumed; no Run registry exists
in v0.1, so the ref is shape-validated at the record boundary and
documented as the exact ``run_id``) and the **exact** raw artifact refs
(``input_artifact_ids`` -- the ``artifact_id`` values of the registered
``ArtifactManifest`` records the analysis consumed, resolved against the
project artifact registry of DEV-M3-G02, the ``manifests/`` directory).
Registration resolves every reference against the registered entities:
the protocol version must be a registered PROTOCOL record of the
DEV-M9-G01 lineage, every input/output artifact id must be a registered
manifest, and the acceptance ref must be a registered acceptance record.
A drifted string that does not resolve to a registered entity is rejected
with a stable ``UnresolvedResultReferenceError`` (exactness -- the
references cannot silently point at nothing).

Immutable protocol version (AC-02)
----------------------------------
The record is a frozen dataclass (the ``protocol_version`` field cannot be
mutated after construction) and the registered file is written exactly
once: re-registration of the same ``result_id`` -- even with a different
``protocol_version`` -- is rejected with ``DuplicateResultError`` and the
original bytes stay untouched.

No requirement closure (AC-03)
------------------------------
The result package only **links** Requirements (``requirement_refs`` on
the record: the linkage a later requirement-closure flow consumes). This
module never reads or writes requirement state -- no requirement record is
touched by registration -- and Requirement close semantics stay with the
requirement/outcome layer (``core/rules/outcome.py``,
``planning/inventory.py``).

Determinism and boundaries
--------------------------
All checks and derived records are pure functions of the registered state;
no wall clock, no randomness, no network. ``TypeError`` at the public
boundaries; errors follow the ``ValueError``-subclass convention with
stable messages; ``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, TypeAlias

from scientific_reproduction.analysis.protocols import (
    ProtocolNotFoundError,
    read_protocol_version,
)
from scientific_reproduction.artifacts.exceptions import ArtifactNotFoundError
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import AnalysisKind, PrimaryOrExploratory
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)
from scientific_reproduction.planning.plan import (
    AcceptanceNotFoundError,
    read_acceptance,
)

__all__ = [
    "ARTIFACTS_STATE_DIR",
    "RESULTS_STATE_DIR",
    "DuplicateResultError",
    "InvalidResultIdError",
    "ResultNotFoundError",
    "ResultRecord",
    "ResultRecordError",
    "ResultRegistryError",
    "UnresolvedResultReferenceError",
    "list_results",
    "read_result",
    "register_result",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ResultRegistryError(ValueError):
    """Base class for all analysis result registry errors."""


class ResultRecordError(ResultRegistryError):
    """Raised when a result record violates the frozen record shape.

    Covers empty/malformed id and reference fields, non-empty-list
    violations, duplicate references and protocol versions that are not
    ``v<N>`` / ``v<N>-draft`` (the protocol version vocabulary of
    DEV-M9-G01).
    """


class DuplicateResultError(ResultRegistryError):
    """Raised when a result id is registered a second time (no clobbering)."""


class InvalidResultIdError(ResultRegistryError):
    """Raised when a result id is not a safe single registry path segment."""


class ResultNotFoundError(ResultRegistryError):
    """Raised when reading a result record that is not registered."""


class UnresolvedResultReferenceError(ResultRegistryError):
    """Raised when a result reference does not resolve to a registered entity.

    AC-01: the result names the exact input Run/artifact refs -- a run or
    artifact id (or protocol/acceptance reference) that is not a registered
    entity is rejected instead of silently drifting.
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Registry directory of the result records, relative to the workspace root
#: (``analysis/results/<result_id>.json``).
RESULTS_STATE_DIR: str = "analysis/results"

#: The artifact registry base directory of a project workspace
#: (``14-STATE-GIT-ARTIFACTS.md`` SS6: manifests live under ``manifests/``;
#: ``planning.init.INIT_DIRECTORIES``).
ARTIFACTS_STATE_DIR: str = "manifests"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: Protocol version syntax (mirrors the DEV-M9-G01 protocol vocabulary:
#: ``v<N>`` formal, ``v<N>-draft`` draft).
_VERSION_RE = re.compile(r"^v(?P<number>\d+)(?P<suffix>-draft)?$")

#: Required record fields (schema keys of the result package).
_REQUIRED_RECORD_FIELDS: tuple[str, ...] = (
    "result_id",
    "analysis_id",
    "protocol_version",
    "run_ref",
    "input_artifact_ids",
    "primary_or_exploratory",
)

#: Optional record fields (schema keys of the result package).
_OPTIONAL_RECORD_FIELDS: tuple[str, ...] = (
    "acceptance_ref",
    "requirement_refs",
    "output_artifact_ids",
    "environment",
    "qc_findings",
    "metrics",
    "uncertainty",
    "warnings",
    "scripts",
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

    Ids map to ``<result_id>.json`` files under ``analysis/results/``, so
    path separators and ``.``/``..`` segments are rejected; glob
    metacharacters (``*``, ``?``, ``[``, ``]``) are rejected as well (they
    cannot select foreign records here -- the listing globs all ``*.json``
    without interpolating the id -- but ids must stay safe for every
    future keyed flow, and rejecting them keeps the boundary uniform with
    the protocol registry of DEV-M9-G01).
    """
    if not _is_safe_registry_id(value):
        raise InvalidResultIdError(
            f"invalid result id {value!r}: ids must be non-empty single path"
            " segments (no '/', no '\\', not '.' or '..') without glob"
            " metacharacters '*', '?', '[' or ']'"
        )


# ---------------------------------------------------------------------------
# The frozen result record (strict __post_init__ validation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResultRecord:
    """One analysis result package record with its trace links (AC-01/02/03).

    Frozen and hashable: "same record -> same persisted bytes" is directly
    testable and the record cannot be mutated after construction (AC-02 --
    the ``protocol_version`` field, like every field, is immutable). The
    record is pure data: every check here is a pure function of the record
    itself (``TypeError`` for wrong types at the construction boundary,
    ``ResultRecordError`` for wrong values); resolution of the references
    against registered entities happens at registration time
    (:func:`register_result`, AC-01).

    Fields mirror the Analysis Result Package contents of
    ``12-ANALYSIS-SUBSYSTEM.md`` SS5 plus the trace links:

    * ``result_id`` -- the registry id of this result record;
    * ``analysis_id`` -- the protocol id the result was produced under;
    * ``protocol_version`` -- the exact protocol version the analysis used
      (immutable in the record, AC-02);
    * ``run_ref`` -- the exact ``run_id`` of the input Run record (AC-01);
    * ``input_artifact_ids`` -- the exact ``artifact_id`` values of the raw
      artifact manifests the analysis consumed (AC-01);
    * ``primary_or_exploratory`` -- the primary/exploratory label;
    * ``acceptance_ref`` -- the acceptance criteria the result was
      evaluated against (None when the result carries no acceptance link);
    * ``requirement_refs`` -- Requirements this result supports (pure
      linkage, AC-03: registering a result never closes a Requirement);
    * ``output_artifact_ids`` -- plots/tables artifact ids the analysis
      produced;
    * ``environment`` -- software/environment of the analysis run;
    * ``qc_findings`` -- QC findings;
    * ``metrics`` -- derived metrics;
    * ``uncertainty`` -- uncertainty/statistics;
    * ``warnings`` -- warnings/assumptions;
    * ``scripts`` -- reproducible scripts/notebooks (where appropriate).

    Raises:
        TypeError: a field has the wrong type.
        ResultRecordError: a field value violates the record shape (empty
            id/ref, malformed protocol version, empty required list,
            duplicate reference, unsafe id shape).
    """

    result_id: str
    analysis_id: str
    protocol_version: str
    run_ref: str
    input_artifact_ids: list[str]
    primary_or_exploratory: PrimaryOrExploratory
    acceptance_ref: str | None = None
    requirement_refs: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    qc_findings: list[str] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    uncertainty: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for field_name in (
            "result_id",
            "analysis_id",
            "protocol_version",
            "run_ref",
        ):
            _require_str(getattr(self, field_name), field_name)
        if not self.result_id.strip():
            raise ResultRecordError(
                "ResultRecord.result_id must be a non-empty string, got"
                f" {self.result_id!r}"
            )
        # ``result_id`` safety is enforced at the registry boundary
        # (``InvalidResultIdError`` in register/read/list), mirroring the
        # protocol registry; the record itself only requires a non-empty id.
        if not self.analysis_id.strip():
            raise ResultRecordError(
                "ResultRecord.analysis_id must be a non-empty string, got"
                f" {self.analysis_id!r}"
            )
        if not _is_safe_registry_id(self.analysis_id):
            raise ResultRecordError(
                f"ResultRecord.analysis_id {self.analysis_id!r} is not a"
                " safe registry id (no '/', no '\\', not '.' or '..', no"
                " glob metacharacters)"
            )
        if not self.protocol_version.strip():
            raise ResultRecordError(
                "ResultRecord.protocol_version must be a non-empty string,"
                f" got {self.protocol_version!r}"
            )
        if _VERSION_RE.fullmatch(self.protocol_version) is None:
            raise ResultRecordError(
                f"ResultRecord.protocol_version {self.protocol_version!r} is"
                " not a protocol version 'v<N>' or 'v<N>-draft'"
            )
        if not self.run_ref.strip():
            raise ResultRecordError(
                f"ResultRecord.run_ref must be a non-empty string, got"
                f" {self.run_ref!r}"
            )
        if not _is_safe_registry_id(self.run_ref):
            raise ResultRecordError(
                f"ResultRecord.run_ref {self.run_ref!r} is not a safe Run id"
                " (no '/', no '\\', not '.' or '..', no glob metacharacters);"
                " the ref must name the exact run_id of the input Run record"
            )
        if not isinstance(self.primary_or_exploratory, PrimaryOrExploratory):
            raise TypeError(
                "ResultRecord.primary_or_exploratory must be a"
                f" PrimaryOrExploratory member, got"
                f" {self.primary_or_exploratory!r}"
            )
        _require_ref_list(self, "input_artifact_ids")
        if not self.input_artifact_ids:
            raise ResultRecordError(
                "ResultRecord.input_artifact_ids must name at least one raw"
                " artifact (the input artifact IDs of the result package)"
            )
        _require_ref_list(self, "output_artifact_ids")
        _require_ref_list(self, "requirement_refs")
        if self.acceptance_ref is not None:
            if not isinstance(self.acceptance_ref, str):
                raise TypeError(
                    "ResultRecord.acceptance_ref must be a str or None, got"
                    f" {type(self.acceptance_ref).__name__}"
                )
            if not self.acceptance_ref.strip():
                raise ResultRecordError(
                    "ResultRecord.acceptance_ref must be a non-empty string"
                    " when set, got an empty string"
                )
            if not _is_safe_registry_id(self.acceptance_ref):
                raise ResultRecordError(
                    f"ResultRecord.acceptance_ref {self.acceptance_ref!r} is"
                    " not a safe registry id (no '/', no '\\', not '.' or"
                    " '..', no glob metacharacters)"
                )
        for field_name in ("environment", "uncertainty"):
            value = getattr(self, field_name)
            if not isinstance(value, dict):
                raise TypeError(
                    f"ResultRecord.{field_name} must be a dict, got"
                    f" {type(value).__name__}"
                )
        _require_string_list(self, "qc_findings")
        _require_string_list(self, "warnings")
        _require_string_list(self, "scripts")
        if not isinstance(self.metrics, list):
            raise TypeError(
                "ResultRecord.metrics must be a list, got"
                f" {type(self.metrics).__name__}"
            )
        for metric in self.metrics:
            if not isinstance(metric, dict):
                raise TypeError(
                    "ResultRecord.metrics entries must be dicts, got"
                    f" {type(metric).__name__}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the record in canonical field order.

        ``None`` optional values are omitted (the ``to_dict()`` convention
        of ``core.models.CoreModel``); every other field -- including empty
        collections -- is emitted, so the persisted bytes are canonical and
        deterministic.
        """
        data: dict[str, Any] = {
            "result_id": self.result_id,
            "analysis_id": self.analysis_id,
            "protocol_version": self.protocol_version,
            "run_ref": self.run_ref,
            "input_artifact_ids": list(self.input_artifact_ids),
            "primary_or_exploratory": self.primary_or_exploratory.value,
        }
        if self.acceptance_ref is not None:
            data["acceptance_ref"] = self.acceptance_ref
        data["requirement_refs"] = list(self.requirement_refs)
        data["output_artifact_ids"] = list(self.output_artifact_ids)
        data["environment"] = dict(self.environment)
        data["qc_findings"] = list(self.qc_findings)
        data["metrics"] = [dict(metric) for metric in self.metrics]
        data["uncertainty"] = dict(self.uncertainty)
        data["warnings"] = list(self.warnings)
        data["scripts"] = list(self.scripts)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ResultRecord:
        """Build a record from a plain dict (schema key names).

        Every required field must be present (``ResultRecordError``
        otherwise); optional fields are absent-in-the-dict = default.
        String enum values are coerced to ``PrimaryOrExploratory``
        members (an unknown value is a stable ``ResultRecordError``).
        Type/value violations are rejected by the constructor with the
        usual ``TypeError`` / ``ResultRecordError`` split.

        Raises:
            TypeError: ``data`` is not a mapping.
            ResultRecordError: a required field is missing, or the
                ``primary_or_exploratory`` value is not a member value.
            TypeError: a field value has the wrong type.
            ResultRecordError: a field value violates the record shape.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "ResultRecord.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [name for name in _REQUIRED_RECORD_FIELDS if name not in data]
        if missing:
            raise ResultRecordError(
                "result record is missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {}
        for name in _REQUIRED_RECORD_FIELDS:
            value = data[name]
            if name == "primary_or_exploratory" and not isinstance(
                value, PrimaryOrExploratory
            ):
                try:
                    value = PrimaryOrExploratory(value)
                except ValueError:
                    raise ResultRecordError(
                        "invalid primary_or_exploratory value"
                        f" {value!r}: expected a PrimaryOrExploratory member"
                    ) from None
            kwargs[name] = value
        for name in _OPTIONAL_RECORD_FIELDS:
            if name in data:
                kwargs[name] = data[name]
        return cls(**kwargs)


#: A user-supplied result record: the typed model or a schema-shaped dict.
ResultInput: TypeAlias = ResultRecord | Mapping[str, Any]


def _require_str(value: Any, field_name: str) -> None:
    """Reject a non-str value at the record boundary (stable TypeError)."""
    if not isinstance(value, str):
        raise TypeError(
            f"ResultRecord.{field_name} must be a str, got"
            f" {type(value).__name__}"
        )


def _require_ref_list(record: ResultRecord, field_name: str) -> None:
    """Validate a reference list field (refs are exact, unique, non-empty)."""
    values = getattr(record, field_name)
    if not isinstance(values, list):
        raise TypeError(
            f"ResultRecord.{field_name} must be a list, got"
            f" {type(values).__name__}"
        )
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise TypeError(
                f"ResultRecord.{field_name} entries must be str, got"
                f" {type(item).__name__}"
            )
        if not item.strip():
            raise ResultRecordError(
                f"ResultRecord.{field_name} entries must be non-empty"
                f" strings, got {item!r}"
            )
        if item in seen:
            raise ResultRecordError(
                f"ResultRecord.{field_name} contains duplicate reference"
                f" {item!r}"
            )
        seen.add(item)


def _require_string_list(record: ResultRecord, field_name: str) -> None:
    """Validate a plain string-list field (entries non-empty strings)."""
    values = getattr(record, field_name)
    if not isinstance(values, list):
        raise TypeError(
            f"ResultRecord.{field_name} must be a list, got"
            f" {type(values).__name__}"
        )
    for item in values:
        if not isinstance(item, str):
            raise TypeError(
                f"ResultRecord.{field_name} entries must be str, got"
                f" {type(item).__name__}"
            )
        if not item.strip():
            raise ResultRecordError(
                f"ResultRecord.{field_name} entries must be non-empty"
                f" strings, got {item!r}"
            )


# ---------------------------------------------------------------------------
# Registration (exact-reference resolution at the persistence gate)
# ---------------------------------------------------------------------------


def register_result(root: str | Path, result: ResultInput) -> ResultRecord:
    """Register one analysis result record at ``analysis/results/<id>.json``.

    The analysis result package registration entry: the record is
    schema-shaped, id path-escape validated, resolved against the
    registered entities (AC-01) and persisted as canonical JSON via
    ``core.atomic.atomic_write``. Registration is **exactly once** per
    result id: result records are immutable, and a second registration of
    the same id -- even with a different ``protocol_version`` -- is
    rejected with ``DuplicateResultError`` and the original file is never
    rewritten (AC-02). No requirement state is ever read or written:
    ``requirement_refs`` is a pure linkage (AC-03).

    Reference resolution (AC-01 exactness, in this order):
    ``analysis_id`` + ``protocol_version`` must resolve to a registered
    PROTOCOL record of the DEV-M9-G01 lineage; every ``input_artifact_ids``
    / ``output_artifact_ids`` entry must resolve to a registered
    ``ArtifactManifest`` in the project artifact registry
    (``manifests/``); ``acceptance_ref`` (when set) must resolve to a
    registered acceptance record. Unresolved references raise
    ``UnresolvedResultReferenceError`` with a stable message before
    anything is written. The referenced protocol record is not required to
    be frozen here: the data-analysis acceptance gate of DEV-M9-G01
    (``assert_acceptance_eligible``) remains the acceptance gate, and this
    module only pins the exact protocol version.

    Args:
        root: the initialized workspace root.
        result: the result record as a typed :class:`ResultRecord` or a
            schema-shaped mapping.

    Returns:
        The registered record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``result`` is neither a
            ``ResultRecord`` nor a mapping.
        ResultRecordError: the record violates the frozen record shape.
        InvalidResultIdError: the ``result_id`` is not a safe single path
            segment.
        DuplicateResultError: a result with the same id is already
            registered (stable message, original bytes untouched).
        UnresolvedResultReferenceError: a run/artifact/protocol/acceptance
            reference does not resolve to a registered entity (AC-01).
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored protocol/acceptance/artifact record is
            corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_result(result)
    _validate_result_id(model.result_id)
    state_path = _result_path(project_root, model.result_id)
    if state_path.is_file():
        raise DuplicateResultError(
            f"result {model.result_id!r} is already registered; result"
            " records are immutable and each result_id is written exactly"
            " once (AC-02)"
        )
    _resolve_protocol_ref(
        project_root, model.result_id, model.analysis_id, model.protocol_version
    )
    _resolve_artifact_refs(
        project_root,
        model.result_id,
        sorted(set(model.input_artifact_ids) | set(model.output_artifact_ids)),
    )
    if model.acceptance_ref is not None:
        _resolve_acceptance_ref(project_root, model.result_id, model.acceptance_ref)
    atomic_write(state_path, _canonical_json(model.to_dict()))
    return model


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def read_result(root: str | Path, result_id: str) -> ResultRecord:
    """Read one registered result record as a typed record.

    The returned record is the exact stored record (bytes -> model); stored
    files are never rewritten (AC-02), so this read is stable across
    repeated registration attempts of the same id.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``result_id`` is not a
            str.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        InvalidResultIdError: ``result_id`` is not a safe id.
        ResultNotFoundError: no record with that id is registered.
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
        raise ResultNotFoundError(
            f"no analysis result registered with id {result_id!r} at"
            f" {project_root}"
        )
    return _read_result_file(state_path)


def list_results(root: str | Path) -> tuple[ResultRecord, ...]:
    """List every registered result record, sorted by ``result_id``.

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
    results_dir = project_root / RESULTS_STATE_DIR
    records: list[ResultRecord] = []
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


def _coerce_result(result: ResultInput) -> ResultRecord:
    """Return a typed record from either input form."""
    if isinstance(result, ResultRecord):
        return result
    if isinstance(result, Mapping):
        return ResultRecord.from_dict(result)
    raise TypeError(
        "analysis result must be a ResultRecord or a mapping, got"
        f" {type(result).__name__}"
    )


def _result_path(root: Path, result_id: str) -> Path:
    """The registry path of one result record."""
    return root / RESULTS_STATE_DIR / f"{result_id}.json"


def _resolve_protocol_ref(
    root: Path, result_id: str, analysis_id: str, protocol_version: str
) -> None:
    """Resolve the referenced protocol version against the protocol registry.

    AC-01/AC-02: the result pins an exact registered protocol version of
    the DEV-M9-G01 lineage, and the referenced record must be a PROTOCOL
    record (not a result-kind record of the lineage).
    """
    try:
        version = read_protocol_version(root, analysis_id, protocol_version)
    except ProtocolNotFoundError as exc:
        raise UnresolvedResultReferenceError(
            f"result {result_id!r} references analysis protocol version"
            f" {protocol_version!r} of {analysis_id!r}, which is not"
            " registered; register the protocol version first"
        ) from exc
    if version.record.kind is not AnalysisKind.PROTOCOL:
        raise UnresolvedResultReferenceError(
            f"result {result_id!r} references version {protocol_version!r} of"
            f" {analysis_id!r}, which is a"
            f" {version.record.kind.value!r}-kind record, not an analysis"
            " protocol record"
        )


def _resolve_artifact_refs(
    root: Path, result_id: str, artifact_ids: list[str]
) -> None:
    """Resolve every artifact reference against the artifact registry.

    AC-01: every referenced raw artifact must be a registered
    ``ArtifactManifest`` in the project ``manifests/`` registry (DEV-M3-G02);
    the exact ids are resolved in sorted order (deterministic error when
    several references are unresolved).
    """
    registry = ArtifactRegistry(root / ARTIFACTS_STATE_DIR)
    for artifact_id in artifact_ids:
        try:
            registry.get(artifact_id)
        except ArtifactNotFoundError as exc:
            raise UnresolvedResultReferenceError(
                f"result {result_id!r} references artifact {artifact_id!r},"
                f" which is not registered in the artifact registry"
                f" ({ARTIFACTS_STATE_DIR}/); register the artifact manifest"
                " first"
            ) from exc


def _resolve_acceptance_ref(
    root: Path, result_id: str, acceptance_ref: str
) -> None:
    """Resolve the acceptance reference against the acceptance registry."""
    try:
        read_acceptance(root, acceptance_ref)
    except AcceptanceNotFoundError as exc:
        raise UnresolvedResultReferenceError(
            f"result {result_id!r} references acceptance criteria"
            f" {acceptance_ref!r}, which is not registered; register the"
            " acceptance criteria first"
        ) from exc


def _read_result_file(state_path: Path) -> ResultRecord:
    """Parse one result record file.

    The filename is ``<result_id>.json``; the stored ``result_id`` must
    match the file name (a mismatch is a corrupt record). An unparseable
    or shape-invalid file raises ``ValueError`` with a stable message.
    """
    name = state_path.name
    if not name.endswith(".json"):
        raise ValueError(
            f"corrupt analysis result record at {state_path}: expected"
            " '<result_id>.json'"
        )
    result_id = name.removesuffix(".json")
    raw = _read_json_object(state_path, "analysis result")
    try:
        record = ResultRecord.from_dict(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"corrupt analysis result record at {state_path}: {exc}"
        ) from exc
    if record.result_id != result_id:
        raise ValueError(
            f"corrupt analysis result record at {state_path}: stored"
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
