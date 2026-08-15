"""Audit package validator over the real project records (DEV-M13-G01).

Implements the **audit package validator** deliverable of DEV-M13-G01:
the machine-auditable package that validates the frozen report-traceability
chain (``14-STATE-GIT-ARTIFACTS.md`` SS7) for the key report claims, and
the v0.1 release gate 8 of ``18-TEST-AND-ACCEPTANCE-PLAN.md`` SS4 ("Final
machine-auditable package validates traceability"). The validator reads
**every** record through the real registration APIs:

* claims and their evidence -- the in-memory
  ``research.evidence.EvidenceRegistry`` (``records_for_claim``), taken as
  an explicit input (no v0.1 evidence store exists);
* analysis results -- ``analysis.results.list_results`` /
  ``read_result`` (``analysis/results/<result_id>.json``);
* Runs -- the schema-validated ``core.models.Run`` records of the real
  ``core.state_backend.FilesystemStateBackend`` run store over the
  workspace root (``runs/<run_id>.json``, the canonical tree directory).
  **Every** run is assembled into the package, failed runs included
  (AC-03): the frozen Run vocabulary has no
  "failed" lifecycle state -- scientific PASS/FAIL is a review decision
  stored separately (``05-GOAL-RUN-SCHEMA.md`` SS7) and a run that was
  abandoned or invalidated is recorded by the terminal states
  ``CANCELLED`` / ``INVALIDATED`` (``core.rules.lifecycle``) -- so the
  validator's :class:`RunStatus` classification maps those real states
  (plus ``ScientificReview.FAIL``) to ``FAILED`` and the failed runs stay
  visible in the package and its machine-auditable JSON;
* raw artifact manifests -- ``artifacts.registry.ArtifactRegistry``
  (``manifests/<artifact_id>.json``);
* acceptance criteria and requirement outcomes --
  ``planning.plan.list_acceptance`` / ``planning.inventory.list_requirements``.

Validation model (the AC-01/AC-02/AC-03 mapping)
------------------------------------------------
``build_audit_package`` assembles the package (total: it never raises for
missing data -- unresolvable run-artifact refs are recorded per run and
fail validation, and unresolved trace links surface as
:class:`TraceGap` items on the claim traces). ``validate_package`` then
applies the machine rules:

* AC-01 -- every key claim must trace to Analysis -> Run -> Artifact/
  Evidence: the claim's trace must contain at least one ANALYSIS, one RUN
  and one ARTIFACT node (``TRACE_INCOMPLETE`` otherwise).
* AC-02 -- every missing link fails validation: a claim backed by no
  registered evidence record is a ``CLAIM_NOT_FOUND`` error, every
  ``TraceGap`` of a claim trace is a ``TRACE_GAP`` error, and every
  run-artifact ref that resolves to no registered manifest is an
  ``UNRESOLVED_RUN_ARTIFACT`` error.
* AC-03 -- failed Runs remain visible: the package and its JSON run table
  carry every run of the run store, with its real ``lifecycle_state`` /
  ``scientific_review`` and the derived ``status``; a failed run
  (``CANCELLED`` / ``INVALIDATED`` / review ``FAIL``) is never filtered
  out, and a package with a valid claim chain stays valid while the
  failed run remains visible.

Validation failures are data, not exceptions: ``validate_package``
returns an :class:`AuditValidationResult` with ``passed`` and the sorted
error list. Structural failures (uninitialized workspace, corrupt stored
records, wrong argument types) raise the module errors. Nothing is ever
written: the validator only reads, and creates no durable records (no
atomic writes are needed).

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the given
inputs: no wall clock, no randomness, no network. All collections are
sorted by stable keys, so identical state always yields byte-identical
canonical JSON. ``TypeError`` at the public boundaries; errors follow the
``ValueError``-subclass convention with stable messages;
``from __future__ import annotations``; ``__all__``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

from scientific_reproduction.analysis.results import ResultRecord, list_results
from scientific_reproduction.artifacts.exceptions import ArtifactNotFoundError
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ArtifactManifest,
    ClaimSpecificEvidence,
    LifecycleState,
    ReproductionRequirement,
    Run,
    ScientificReview,
)
from scientific_reproduction.core.rules.lifecycle import TERMINAL_RUN_STATES
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import PROJECT_STATE_FILENAME
from scientific_reproduction.planning.inventory import list_requirements
from scientific_reproduction.planning.plan import list_acceptance
from scientific_reproduction.reporting.traceability import (
    ClaimTrace,
    TraceCorruptError,
    TraceKind,
    TraceNotInitializedError,
    trace_claim,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

__all__ = [
    "PACKAGE_VERSION",
    "AuditCorruptError",
    "AuditError",
    "AuditNotInitializedError",
    "AuditPackage",
    "AuditValidationResult",
    "FAILED_RUN_LIFECYCLE_STATES",
    "RunEntry",
    "RunStatus",
    "ValidationError",
    "ValidationErrorKind",
    "build_audit_package",
    "run_status",
    "validate_package",
]

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2

#: Version of the audit package serialization (``package_version`` key of
#: :class:`AuditValidationResult`).
PACKAGE_VERSION: str = "1.0"

#: The run store object type of the state backend (resolved through
#: ``SCHEMA_TO_STATE_DIR`` to the canonical ``runs/<id>.json`` records).
_RUN_OBJECT_TYPE: str = "run"

#: The artifact registry base directory of a project workspace
#: (``14-STATE-GIT-ARTIFACTS.md`` SS6: manifests live under ``manifests/``).
_ARTIFACTS_STATE_DIR: str = "manifests"

#: The frozen Run states that record a failed execution (AC-03): the
#: terminal states of ``core.rules.lifecycle`` minus ``CLOSED`` -- a run
#: abandoned before any result (``CANCELLED``) or invalidated after
#: producing untrustworthy results (``INVALIDATED``). Deriving the set
#: from the real rule table keeps the vocabulary frozen.
FAILED_RUN_LIFECYCLE_STATES: frozenset[LifecycleState] = frozenset(
    TERMINAL_RUN_STATES - {LifecycleState.CLOSED}
)


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class AuditError(ValueError):
    """Base class for all audit package validator errors."""


class AuditNotInitializedError(AuditError):
    """Raised when validation is attempted on a workspace without a project
    state record (no ``project.yaml`` at the root)."""


class AuditCorruptError(AuditError):
    """Raised when a stored record the package reads is corrupt.

    The registered state is read through the real registry read APIs;
    those APIs surface corruption as ``ValueError``, which this module
    re-raises as ``AuditCorruptError`` with the same message so the
    validator's error surface stays stable.
    """


# ---------------------------------------------------------------------------
# The run-status vocabulary (AC-03: failed runs stay visible)
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    """The validator's run classification.

    Values match no frozen schema enum (the classification is this
    module's own auditable vocabulary over the frozen Run vocabulary):
    ``SUCCEEDED`` for a closed run, ``FAILED`` for a run in a failed
    lifecycle state (``CANCELLED`` / ``INVALIDATED`` -- the terminal
    failure states of ``core.rules.lifecycle``) or with a FAIL scientific
    review, ``UNRESOLVED`` otherwise.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class RunEntry:
    """One run of the audit package with its derived status and artifacts.

    The AC-03 visibility surface: every run of the run store is assembled
    into the package, failed runs included, with its real
    ``lifecycle_state`` / ``scientific_review`` and the derived
    :class:`RunStatus`. ``artifacts`` holds the registered manifests the
    run's ``Run.artifacts`` refs resolve to; refs that resolve to no
    registered manifest are kept in ``unresolved_artifact_ids`` -- never
    silently dropped -- and fail validation (AC-02).
    """

    run: Run
    status: RunStatus
    artifacts: tuple[ArtifactManifest, ...] = field(default_factory=tuple)
    unresolved_artifact_ids: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the run entry (the run table row)."""
        return {
            "run_id": self.run.run_id,
            "goal_id": self.run.goal_id,
            "run_type": self.run.run_type.value,
            "lifecycle_state": self.run.lifecycle_state.value,
            "scientific_review": self.run.scientific_review.value,
            "status": self.status.value,
            "artifacts": list(self.run.artifacts),
            "unresolved_artifact_ids": list(self.unresolved_artifact_ids),
        }


def run_status(run: Run) -> RunStatus:
    """Classify one run with the validator's :class:`RunStatus` vocabulary.

    The classification is a pure function of the frozen Run record fields:
    a FAIL scientific review marks the run failed first (a review decision
    stored separately, ``05-GOAL-RUN-SCHEMA.md`` SS7 -- it can only be a
    verdict on a result-bearing run); then the terminal failure lifecycle
    states (``CANCELLED`` / ``INVALIDATED``, AC-03); a closed run succeeds;
    anything else is unresolved.

    Raises:
        TypeError: ``run`` is not a ``Run``.
    """
    if not isinstance(run, Run):
        raise TypeError(f"run_status expects a Run, got {type(run).__name__}")
    if run.scientific_review is ScientificReview.FAIL:
        return RunStatus.FAILED
    if run.lifecycle_state in FAILED_RUN_LIFECYCLE_STATES:
        return RunStatus.FAILED
    if run.lifecycle_state is LifecycleState.CLOSED:
        return RunStatus.SUCCEEDED
    return RunStatus.UNRESOLVED


# ---------------------------------------------------------------------------
# Validation failures (data, not exceptions)
# ---------------------------------------------------------------------------


class ValidationErrorKind(StrEnum):
    """The kinds of machine-auditable validation failures.

    ``CLAIM_NOT_FOUND`` -- a key claim is backed by no registered evidence
    record (AC-02); ``TRACE_GAP`` -- a missing link of a claim trace
    (AC-02); ``TRACE_INCOMPLETE`` -- the claim cannot be traced through the
    Analysis -> Run -> Artifact/Evidence chain (AC-01/AC-02);
    ``UNRESOLVED_RUN_ARTIFACT`` -- a run references an artifact that is
    not registered (AC-02).
    """

    CLAIM_NOT_FOUND = "claim_not_found"
    TRACE_GAP = "trace_gap"
    TRACE_INCOMPLETE = "trace_incomplete"
    UNRESOLVED_RUN_ARTIFACT = "unresolved_run_artifact"


@dataclass(frozen=True)
class ValidationError:
    """One machine-auditable validation failure.

    Attributes:
        kind: the failure kind (see :class:`ValidationErrorKind`).
        claim_id: the key claim (or run id for
            ``UNRESOLVED_RUN_ARTIFACT``) the failure belongs to.
        message: a stable message naming the offending record and the
            reason.
    """

    kind: ValidationErrorKind
    claim_id: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the validation failure."""
        data: dict[str, Any] = {
            "kind": self.kind.value,
            "message": self.message,
        }
        if self.claim_id is not None:
            data["claim_id"] = self.claim_id
        return data


# ---------------------------------------------------------------------------
# The audit package and its validation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditPackage:
    """The machine-auditable package assembled from the real records.

    Attributes:
        claims: the key claims with their resolved traces, sorted by
            ``claim_id`` (``reporting.traceability.ClaimTrace``).
        analyses: every registered analysis result record, sorted by
            ``result_id``.
        runs: every run of the run store with its derived status --
            failed runs included (AC-03), sorted by ``run_id``.
        artifacts: every registered artifact manifest, sorted by
            ``artifact_id``.
        evidence: every registered claim-specific evidence record, in
            registry (registration) order.
        acceptances: every registered acceptance record, sorted by
            ``acceptance_id``.
        requirements: every registered requirement record, sorted by
            ``requirement_id``.
    """

    claims: tuple[ClaimTrace, ...]
    analyses: tuple[ResultRecord, ...]
    runs: tuple[RunEntry, ...]
    artifacts: tuple[ArtifactManifest, ...]
    evidence: tuple[ClaimSpecificEvidence, ...]
    acceptances: tuple[AcceptanceCriteria, ...]
    requirements: tuple[ReproductionRequirement, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the package in canonical field order."""
        return {
            "claims": [trace.to_dict() for trace in self.claims],
            "runs": [entry.to_dict() for entry in self.runs],
            "analyses": [result.result_id for result in self.analyses],
            "artifacts": [manifest.artifact_id for manifest in self.artifacts],
            "evidence": [record.evidence_id for record in self.evidence],
            "acceptances": [record.acceptance_id for record in self.acceptances],
            "requirements": [
                record.requirement_id for record in self.requirements
            ],
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


@dataclass(frozen=True)
class AuditValidationResult:
    """The outcome of one package validation (AC-01/AC-02/AC-03).

    ``package`` is the assembled machine-auditable package (failed runs
    always visible, AC-03); ``passed`` is True iff ``errors`` is empty;
    ``errors`` is the deterministic, sorted list of validation failures
    (AC-02: every missing link is a failure, never a silent skip).
    """

    package: AuditPackage
    passed: bool
    errors: tuple[ValidationError, ...]

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the validation result in canonical field order."""
        return {
            "package_version": PACKAGE_VERSION,
            "passed": self.passed,
            "errors": [error.to_dict() for error in self.errors],
            "package": self.package.to_dict(),
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Package assembly and validation (pure, deterministic)
# ---------------------------------------------------------------------------


def build_audit_package(
    root: str | Path,
    evidence: EvidenceRegistry,
    key_claims: Sequence[str],
) -> AuditPackage:
    """Assemble the machine-auditable package from the real records.

    Reads every record through the real registration APIs: the run store
    (``FilesystemStateBackend`` over the workspace root, resolving the
    canonical ``runs/`` tree directory -- **all** runs, failed runs
    included, AC-03), the analysis result registry, the artifact
    manifest registry, the acceptance and requirement registries, and the
    given claim-specific evidence registry. Each key claim is traced with
    ``reporting.traceability.trace_claim`` (the trace is total: missing
    links are ``TraceGap`` items on the trace, never exceptions).

    The assembly is total for data problems: a run-artifact ref that
    resolves to no registered manifest is recorded in the entry's
    ``unresolved_artifact_ids`` (visible, never dropped) and fails
    validation (AC-02). Structural failures raise.

    Args:
        root: the initialized workspace root.
        evidence: the real claim-specific evidence registry of the
            project.
        key_claims: the report's key claims (the opaque ``claim_id``
            strings to validate the traceability chain for). Entries are
            deduplicated and sorted by id; an empty sequence assembles a
            package with no claims (nothing to validate).

    Returns:
        The deterministic :class:`AuditPackage`.

    Raises:
        TypeError: ``root`` is not a str/Path, ``evidence`` is not an
            ``EvidenceRegistry``, or ``key_claims`` is not a sequence of
            non-empty strings.
        AuditNotInitializedError: no ``project.yaml`` exists at ``root``.
        AuditCorruptError: a stored record the package reads is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(evidence, EvidenceRegistry):
        raise TypeError(
            "evidence must be an EvidenceRegistry, got"
            f" {type(evidence).__name__}"
        )
    claims = _normalize_key_claims(key_claims)
    project_root = Path(root).resolve()
    _require_initialized(project_root)

    run_store = FilesystemStateBackend(project_root)
    artifact_registry = ArtifactRegistry(project_root / _ARTIFACTS_STATE_DIR)

    run_entries = tuple(
        _build_run_entry(run_store, artifact_registry, run_id)
        for run_id in run_store.list_ids(_RUN_OBJECT_TYPE)
    )
    try:
        analyses = list_results(project_root)
        artifacts = tuple(artifact_registry.list())
        acceptances = list_acceptance(project_root)
        requirements = list_requirements(project_root)
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc

    claim_traces = tuple(
        _trace_claim_wrapped(project_root, claim_id, evidence)
        for claim_id in claims
    )

    return AuditPackage(
        claims=claim_traces,
        analyses=tuple(sorted(analyses, key=lambda r: r.result_id)),
        runs=tuple(sorted(run_entries, key=lambda e: e.run.run_id)),
        artifacts=tuple(sorted(artifacts, key=lambda m: m.artifact_id)),
        evidence=tuple(evidence),
        acceptances=tuple(sorted(acceptances, key=lambda a: a.acceptance_id)),
        requirements=tuple(
            sorted(requirements, key=lambda r: r.requirement_id)
        ),
    )


def validate_package(
    root: str | Path,
    evidence: EvidenceRegistry,
    key_claims: Sequence[str],
) -> AuditValidationResult:
    """Validate the audit package of the workspace (AC-01/AC-02/AC-03).

    Assembles the package (``build_audit_package``) and applies the
    machine rules:

    * AC-01 -- every key claim must trace to Analysis -> Run -> Artifact/
      Evidence: a claim whose trace reaches no ANALYSIS, RUN or ARTIFACT
      node fails with ``TRACE_INCOMPLETE``;
    * AC-02 -- every missing link fails: a claim backed by no registered
      evidence record fails with ``CLAIM_NOT_FOUND``, every ``TraceGap``
      of a claim trace fails with ``TRACE_GAP``, and every run-artifact
      ref that resolves to no registered manifest fails with
      ``UNRESOLVED_RUN_ARTIFACT``;
    * AC-03 -- failed runs remain visible: the package (and its JSON) is
      returned with every run of the run store, failed runs included;
      validation never filters runs, so a package with a valid claim
      chain passes while the failed runs stay visible.

    Failures are data, not exceptions: the returned result carries
    ``passed`` and the deterministic, sorted error list.

    Args:
        root: the initialized workspace root.
        evidence: the real claim-specific evidence registry of the
            project.
        key_claims: the report's key claims to validate (deduplicated and
            sorted by id; an empty sequence validates nothing and passes).

    Returns:
        The deterministic :class:`AuditValidationResult`.

    Raises:
        TypeError: ``root`` is not a str/Path, ``evidence`` is not an
            ``EvidenceRegistry``, or ``key_claims`` is not a sequence of
            non-empty strings.
        AuditNotInitializedError: no ``project.yaml`` exists at ``root``.
        AuditCorruptError: a stored record the package reads is corrupt.
    """
    package = build_audit_package(root, evidence, key_claims)
    errors: list[ValidationError] = []
    for trace in package.claims:
        if not trace.has_node(TraceKind.EVIDENCE):
            errors.append(
                ValidationError(
                    kind=ValidationErrorKind.CLAIM_NOT_FOUND,
                    claim_id=trace.claim_id,
                    message=(
                        f"key claim {trace.claim_id!r} is backed by no"
                        " registered ClaimSpecificEvidence record; the"
                        " report-traceability chain of"
                        " 14-STATE-GIT-ARTIFACTS.md SS7 requires the claim"
                        " to be backed by claim-specific evidence"
                    ),
                )
            )
        for gap in trace.gaps:
            errors.append(
                ValidationError(
                    kind=ValidationErrorKind.TRACE_GAP,
                    claim_id=trace.claim_id,
                    message=gap.reason,
                )
            )
        for kind, label in (
            (TraceKind.ANALYSIS, "Analysis"),
            (TraceKind.RUN, "Run"),
            (TraceKind.ARTIFACT, "Artifact/Evidence"),
        ):
            if not trace.has_node(kind):
                errors.append(
                    ValidationError(
                        kind=ValidationErrorKind.TRACE_INCOMPLETE,
                        claim_id=trace.claim_id,
                        message=(
                            f"key claim {trace.claim_id!r} cannot be traced"
                            f" to a {label} record; the claim must be"
                            " traceable through Analysis -> Run ->"
                            " Artifact/Evidence (AC-01)"
                        ),
                    )
                )
    for entry in package.runs:
        for artifact_id in entry.unresolved_artifact_ids:
            errors.append(
                ValidationError(
                    kind=ValidationErrorKind.UNRESOLVED_RUN_ARTIFACT,
                    claim_id=entry.run.run_id,
                    message=(
                        f"run {entry.run.run_id!r} references artifact"
                        f" {artifact_id!r}, which is not registered in the"
                        " artifact registry (manifests/); the run's"
                        " artifact links must resolve (AC-02)"
                    ),
                )
            )
    ordered = tuple(
        sorted(errors, key=lambda e: (e.claim_id or "", e.kind.value, e.message))
    )
    return AuditValidationResult(
        package=package,
        passed=not ordered,
        errors=ordered,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject validation on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise AuditNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _wrap_corrupt(exc: ValueError) -> AuditCorruptError:
    """Re-raise a stored-record corruption as ``AuditCorruptError``."""
    return AuditCorruptError(f"corrupt registered state: {exc}")


def _normalize_key_claims(key_claims: Sequence[str]) -> tuple[str, ...]:
    """Validate, deduplicate and sort the key claims (deterministic)."""
    if isinstance(key_claims, (str, bytes)) or not isinstance(
        key_claims, Sequence
    ):
        raise TypeError(
            "key_claims must be a sequence of claim id strings, got"
            f" {type(key_claims).__name__}"
        )
    normalized: list[str] = []
    for claim_id in key_claims:
        if not isinstance(claim_id, str):
            raise TypeError(
                "key_claims entries must be str claim ids, got"
                f" {type(claim_id).__name__}"
            )
        if not claim_id.strip():
            raise ValueError(
                "key_claims entries must be non-empty strings, got an empty"
                " string"
            )
        if claim_id not in normalized:
            normalized.append(claim_id)
    return tuple(sorted(normalized))


def _trace_claim_wrapped(
    root: Path, claim_id: str, evidence: EvidenceRegistry
) -> ClaimTrace:
    """Trace one key claim, re-raising structural failures as audit errors."""
    try:
        return trace_claim(root, claim_id, evidence)
    except TraceNotInitializedError as exc:
        raise AuditNotInitializedError(str(exc)) from exc
    except TraceCorruptError as exc:
        raise AuditCorruptError(str(exc)) from exc


def _build_run_entry(
    run_store: FilesystemStateBackend,
    artifact_registry: ArtifactRegistry,
    run_id: str,
) -> RunEntry:
    """Read one run and resolve its artifact refs (total for missing data).

    A run-artifact ref that resolves to no registered manifest is kept in
    ``unresolved_artifact_ids`` -- visible in the package and failed by
    ``validate_package`` (AC-02) -- never silently dropped. A corrupt
    stored run or manifest raises ``AuditCorruptError``.
    """
    try:
        run = Run.from_dict(run_store.read(_RUN_OBJECT_TYPE, run_id))
    except (TypeError, ValueError) as exc:
        raise AuditCorruptError(
            f"corrupt run record {run_id!r} in the run store: {exc}"
        ) from exc
    resolved: list[ArtifactManifest] = []
    unresolved: list[str] = []
    for artifact_id in run.artifacts:
        try:
            resolved.append(artifact_registry.get(artifact_id))
        except ArtifactNotFoundError:
            unresolved.append(artifact_id)
    return RunEntry(
        run=run,
        status=run_status(run),
        artifacts=tuple(resolved),
        unresolved_artifact_ids=tuple(unresolved),
    )
