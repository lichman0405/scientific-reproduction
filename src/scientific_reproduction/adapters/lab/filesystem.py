"""Filesystem LabAdapter reference implementation (DEV-M7-G01).

The v0.1 reference LabAdapter: filesystem/manual handoff
(``15-ADAPTER-SPEC.md`` SS2, ``10-EXPERIMENT-SUBSYSTEM.md`` SS2). The
handoff root (``base_dir``) and the outgoing/incoming directory names
are **injected** by the caller -- the adapter never touches any path
outside them.

Handoff layout
--------------
::

    <base_dir>/
      outgoing/<RUN_ID>/
        dispatch.json          the DispatchRecord (AC-01 record)
        manifest.json          the schema-gated execution package manifest
        <artifact files>       optional companion artifacts
        missing-result-request.json   (after request_missing_result)
      incoming/<RUN_ID>/
        result-manifest.json   the returned Result Package manifest
        <returned data files>  the declared data files

Outgoing flow (AC-01)
---------------------
``dispatch`` writes the package to ``outgoing/<RUN_ID>/`` **after** the
real schema gate (``core.schema_validation.validate_and_reject`` against
``schemas/lab-execution-package.schema.yaml``) accepts it: a malformed
package is refused loudly and nothing is written. The dispatch is
exactly-once (a second dispatch of the same package is refused) and the
``dispatch_id`` is a deterministic pure function of the package identity.
``dispatched_at`` is caller-injected; no wall clock.

Incoming flow (AC-02/AC-03)
---------------------------
``status`` reports ``RUNNING_EXTERNAL`` after dispatch and
``RESULT_AVAILABLE`` once a result manifest appears in the incoming
handoff. ``collect`` reads the returned result manifest from
``incoming/<RUN_ID>/``, evaluates it with the pure
:func:`~scientific_reproduction.adapters.lab.manifest.evaluate_result_manifest`
decision record, and refuses the collection iff anything is missing or
mismatched: the run reference is absent or does not match the dispatch's
Run (AC-02 -- never guessed, never silently matched), required manifest
entries are missing, declared data files are absent, or the dispatched
package's required returns are uncovered (AC-03 -- the
``CollectionRejectedError`` carries the assessment with WHAT is missing).

Determinism and boundaries
--------------------------
Everything is a pure function of the injected paths and inputs: no
randomness, no wall clock, no network. ``TypeError`` at the public
boundaries; ``ValueError``-subclass errors with stable messages;
adapter-written state is canonical JSON (sorted keys, 2-space indent,
trailing newline) via ``core.atomic.atomic_write``.

The adapter never touches the Run record: the dispatching orchestrator
owns the dispatch-to-Run linkage (``run.external.dispatch_id`` +
``RUNNING_EXTERNAL``) and performs it with the bundled helper
:func:`~scientific_reproduction.adapters.lab.linkage.link_run_to_dispatch`
(15-ADAPTER-SPEC.md SS2 "Run record linkage").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from scientific_reproduction.adapters.lab.base import (
    CollectionRejectedError,
    CollectionResult,
    DispatchNotFoundError,
    DispatchRecord,
    DispatchState,
    DispatchStatus,
    DuplicateDispatchError,
    LabAdapter,
    LabAdapterDataError,
    LabExecutionPackageInput,
    MissingResultRequest,
    ResultNotAvailableError,
    is_safe_path_segment,
)
from scientific_reproduction.adapters.lab.manifest import (
    LabResultManifest,
    evaluate_result_manifest,
)
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import LabExecutionPackage
from scientific_reproduction.core.schema_validation import validate_and_reject

__all__ = [
    "DISPATCH_RECORD_FILENAME",
    "EXECUTION_MANIFEST_FILENAME",
    "FilesystemLabAdapter",
    "INCOMING_DIR_NAME",
    "MISSING_RESULT_REQUEST_FILENAME",
    "OUTGOING_DIR_NAME",
    "RESULT_MANIFEST_FILENAME",
]

#: Default name of the outgoing handoff directory (relative to base_dir).
OUTGOING_DIR_NAME: str = "outgoing"

#: Default name of the incoming handoff directory (relative to base_dir).
INCOMING_DIR_NAME: str = "incoming"

#: Filename of the dispatch record inside ``outgoing/<RUN_ID>/``.
DISPATCH_RECORD_FILENAME: str = "dispatch.json"

#: Filename of the schema-gated execution package manifest inside
#: ``outgoing/<RUN_ID>/``.
EXECUTION_MANIFEST_FILENAME: str = "manifest.json"

#: Filename of the returned Result Package manifest inside
#: ``incoming/<RUN_ID>/``.
RESULT_MANIFEST_FILENAME: str = "result-manifest.json"

#: Filename of a missing-result request inside ``outgoing/<RUN_ID>/``.
MISSING_RESULT_REQUEST_FILENAME: str = "missing-result-request.json"

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2


class FilesystemLabAdapter(LabAdapter):
    """Filesystem/manual handoff LabAdapter (v0.1 reference implementation).

    All paths are injected: ``base_dir`` is the handoff root (the
    ``lab/`` directory of ``10-EXPERIMENT-SUBSYSTEM.md`` SS2) and the
    outgoing/incoming directory names are constructor parameters. The
    adapter derives ``<base_dir>/<outgoing>/<RUN_ID>/`` and
    ``<base_dir>/<incoming>/<RUN_ID>/`` from them and never touches any
    other location.
    """

    adapter_id = "filesystem"
    version = "1.0"
    description = (
        "filesystem/manual handoff reference implementation"
        " (10-EXPERIMENT-SUBSYSTEM.md SS2)"
    )

    def __init__(
        self,
        base_dir: str | Path,
        *,
        outgoing: str = OUTGOING_DIR_NAME,
        incoming: str = INCOMING_DIR_NAME,
    ) -> None:
        if not isinstance(base_dir, (str, Path)):
            raise TypeError(
                f"base_dir must be a str or Path, got {type(base_dir).__name__}"
            )
        if not isinstance(outgoing, str):
            raise TypeError(
                f"outgoing must be a str, got {type(outgoing).__name__}"
            )
        if not isinstance(incoming, str):
            raise TypeError(
                f"incoming must be a str, got {type(incoming).__name__}"
            )
        if not is_safe_path_segment(outgoing):
            raise LabAdapterDataError(
                f"outgoing directory name {outgoing!r} is not a safe path"
                " segment (no '/', no '\\', not '.' or '..', no glob"
                " metacharacters)"
            )
        if not is_safe_path_segment(incoming):
            raise LabAdapterDataError(
                f"incoming directory name {incoming!r} is not a safe path"
                " segment (no '/', no '\\', not '.' or '..', no glob"
                " metacharacters)"
            )
        self._base_dir = Path(base_dir)
        self._outgoing = outgoing
        self._incoming = incoming

    # ------------------------------------------------------------------
    # Outgoing flow (AC-01)
    # ------------------------------------------------------------------

    def dispatch(
        self,
        execution_package: LabExecutionPackageInput,
        *,
        artifacts: Mapping[str, str | bytes] | None = None,
        dispatched_at: str | None = None,
    ) -> DispatchRecord:
        """Dispatch one Experiment Execution Package to the outgoing path.

        The package is schema-gated on the way out: ``validate_and_reject``
        against ``schemas/lab-execution-package.schema.yaml`` (the real
        schema validation API); a malformed package raises
        ``SchemaValidationError`` loudly and **nothing** is written.
        The dispatch directory is ``<base_dir>/<outgoing>/<RUN_ID>/``
        with the dispatch record, the execution manifest and the optional
        artifact files, all written via ``core.atomic.atomic_write``.
        Dispatches are exactly-once per package (deterministic
        ``dispatch_id``); re-dispatching raises
        :class:`DuplicateDispatchError` and never overwrites the original
        handoff.

        Returns:
            The :class:`DispatchRecord` of the dispatch (AC-01). The
            adapter never touches the Run record: the caller owns the
            linkage and must record the returned ``dispatch_id`` on the
            Run (``run.external.dispatch_id``) and advance it to
            ``RUNNING_EXTERNAL`` -- perform it with
            :func:`scientific_reproduction.adapters.lab.linkage.link_run_to_dispatch`
            (15-ADAPTER-SPEC.md SS2 "Run record linkage").

        Raises:
            TypeError: ``execution_package`` is neither a
                ``LabExecutionPackage`` nor a mapping, ``artifacts`` is
                not a mapping, or an artifact value is neither str nor
                bytes.
            LabAdapterDataError: the ``run_id`` is not a safe handoff
                path segment, an artifact name is not a safe path
                segment, or ``dispatched_at`` is malformed.
            SchemaValidationError: the package fails the real
                lab-execution-package schema (nothing is written).
            DuplicateDispatchError: the package was already dispatched.
        """
        if not isinstance(execution_package, (LabExecutionPackage, Mapping)):
            raise TypeError(
                "dispatch expects a LabExecutionPackage or a mapping, got"
                f" {type(execution_package).__name__}"
            )
        if artifacts is not None and not isinstance(artifacts, Mapping):
            raise TypeError(
                f"artifacts must be a mapping or None, got"
                f" {type(artifacts).__name__}"
            )
        if dispatched_at is not None:
            if not isinstance(dispatched_at, str) or not dispatched_at.strip():
                raise LabAdapterDataError(
                    "dispatched_at must be a non-empty string when set"
                )
        data = (
            execution_package.to_dict()
            if isinstance(execution_package, LabExecutionPackage)
            else dict(execution_package)
        )
        # AC-01: the real schema gate on the way out -- a malformed
        # package is refused loudly before anything is written.
        validate_and_reject("lab-execution-package", data)
        package_id = data["package_id"]
        project_id = data["project_id"]
        goal_id = data["goal_id"]
        run_id = data["run_id"]
        if not isinstance(package_id, str) or not package_id.strip():
            raise LabAdapterDataError(
                "execution package package_id must be a non-empty string"
            )
        if not isinstance(run_id, str) or not run_id.strip():
            raise LabAdapterDataError(
                "execution package run_id must be a non-empty string"
            )
        if not is_safe_path_segment(run_id):
            raise LabAdapterDataError(
                f"execution package run_id {run_id!r} is not a safe handoff"
                " path segment (no '/', no '\\', not '.' or '..', no glob"
                " metacharacters); the Run id maps to the handoff"
                " directory name"
            )
        if artifacts is not None:
            for name, content in artifacts.items():
                if not isinstance(name, str) or not is_safe_path_segment(name):
                    raise LabAdapterDataError(
                        f"artifact name {name!r} is not a safe path segment"
                        " (no '/', no '\\', not '.' or '..', no glob"
                        " metacharacters)"
                    )
                if not isinstance(content, (str, bytes)):
                    raise TypeError(
                        f"artifact {name!r} content must be str or bytes, got"
                        f" {type(content).__name__}"
                    )
        dispatch_id = generate_id("dispatch", package_id, run_id)
        outgoing_dir = self._base_dir / self._outgoing / run_id
        record_path = outgoing_dir / DISPATCH_RECORD_FILENAME
        manifest_path = outgoing_dir / EXECUTION_MANIFEST_FILENAME
        if record_path.is_file() or manifest_path.is_file():
            raise DuplicateDispatchError(
                f"execution package {package_id!r} (run {run_id!r}) is"
                f" already dispatched at {outgoing_dir}; dispatches are"
                " exactly-once and the original handoff is never"
                " overwritten"
            )
        record = DispatchRecord(
            dispatch_id=dispatch_id,
            package_id=package_id,
            project_id=str(project_id),
            goal_id=str(goal_id),
            run_id=run_id,
            outgoing_path=str(outgoing_dir),
            dispatched_at=dispatched_at,
        )
        atomic_write(record_path, _canonical_json(record.to_dict()))
        atomic_write(manifest_path, _canonical_json(data))
        if artifacts is not None:
            for name in sorted(artifacts):
                atomic_write(outgoing_dir / name, artifacts[name])
        return record

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self, dispatch_id: str) -> DispatchStatus:
        """Return the observable state of one dispatch.

        ``RUNNING_EXTERNAL`` after the outgoing handoff; once a Result
        Package manifest is detected in the incoming handoff,
        ``RESULT_AVAILABLE`` (10-EXPERIMENT-SUBSYSTEM.md SS1).

        Raises:
            TypeError: ``dispatch_id`` is not a str.
            DispatchNotFoundError: no dispatch with that id is recorded.
        """
        if not isinstance(dispatch_id, str):
            raise TypeError(
                f"dispatch_id must be a str, got {type(dispatch_id).__name__}"
            )
        record = self._find_dispatch(dispatch_id)
        result_manifest_path = (
            self._base_dir
            / self._incoming
            / record.run_id
            / RESULT_MANIFEST_FILENAME
        )
        state = (
            DispatchState.RESULT_AVAILABLE
            if result_manifest_path.is_file()
            else DispatchState.RUNNING_EXTERNAL
        )
        return DispatchStatus(
            dispatch_id=dispatch_id, run_id=record.run_id, state=state
        )

    # ------------------------------------------------------------------
    # Incoming flow (AC-02/AC-03)
    # ------------------------------------------------------------------

    def collect(self, dispatch_id: str) -> CollectionResult:
        """Collect the returned Result Package of one dispatch.

        The returned package is associated with the correct Run: its
        result manifest must carry a run reference exactly matching the
        Run of the dispatch (AC-02) -- an absent or mismatched reference
        refuses the collection, never guessed, never silently matched.
        Required result-manifest entries, declared data files and the
        dispatched package's required returns are verified; anything
        missing raises :class:`CollectionRejectedError` carrying the
        :class:`ResultManifestAssessment` decision record of WHAT is
        missing (AC-03).

        Raises:
            TypeError: ``dispatch_id`` is not a str.
            DispatchNotFoundError: no dispatch with that id is recorded.
            ResultNotAvailableError: no Result Package has been returned
                yet.
            CollectionRejectedError: the returned package fails the
                collection checks (AC-02/AC-03; carries the assessment).
            LabAdapterDataError: the returned result manifest is corrupt
                (unparseable or not a JSON object).
            ValueError: a dispatch record or the execution manifest is
                corrupt.
        """
        if not isinstance(dispatch_id, str):
            raise TypeError(
                f"dispatch_id must be a str, got {type(dispatch_id).__name__}"
            )
        record = self._find_dispatch(dispatch_id)
        incoming_dir = self._base_dir / self._incoming / record.run_id
        result_manifest_path = incoming_dir / RESULT_MANIFEST_FILENAME
        if not result_manifest_path.is_file():
            raise ResultNotAvailableError(
                f"no result package returned for dispatch {dispatch_id!r}"
                f" (run {record.run_id!r}); expected the result manifest at"
                f" {result_manifest_path}"
            )
        try:
            raw = _read_json_object(result_manifest_path, "lab result manifest")
        except ValueError as exc:
            raise LabAdapterDataError(str(exc)) from exc
        present_files = tuple(
            sorted(
                name
                for name in _list_files(incoming_dir)
                if name != RESULT_MANIFEST_FILENAME
            )
        )
        outgoing_manifest_path = (
            self._base_dir / self._outgoing / record.run_id / EXECUTION_MANIFEST_FILENAME
        )
        if not outgoing_manifest_path.is_file():
            raise LabAdapterDataError(
                f"dispatch {dispatch_id!r} has no execution manifest at"
                f" {outgoing_manifest_path}; the handoff state is corrupt"
            )
        outgoing_raw = _read_json_object(
            outgoing_manifest_path, "lab execution manifest"
        )
        required_raw = outgoing_raw.get("required_return")
        if not isinstance(required_raw, list) or not all(
            isinstance(entry, str) for entry in required_raw
        ):
            raise LabAdapterDataError(
                f"dispatch {dispatch_id!r} carries a malformed execution"
                " manifest: required_return must be a list of strings"
            )
        assessment = evaluate_result_manifest(
            raw,
            expected_run_id=record.run_id,
            expected_package_id=record.package_id,
            required_returns=tuple(required_raw),
            present_files=present_files,
        )
        if not assessment.valid:
            raise CollectionRejectedError(
                dispatch_id=dispatch_id, run_id=record.run_id, assessment=assessment
            )
        return CollectionResult(
            dispatch_id=dispatch_id,
            run_id=record.run_id,
            manifest=LabResultManifest.from_dict(raw),
            collected_files=present_files,
            result_path=str(incoming_dir),
        )

    # ------------------------------------------------------------------
    # Missing-result requests
    # ------------------------------------------------------------------

    def request_missing_result(
        self,
        dispatch_id: str,
        manifest_requirements: Sequence[str],
        *,
        requested_at: str | None = None,
    ) -> MissingResultRequest:
        """Request the missing result manifest/data from the lab.

        The request is written to ``outgoing/<RUN_ID>/missing-result-request.json``
        (the operator reads requests from the dispatch directory); the
        requirements are canonicalized to a sorted, deduplicated tuple.
        Writing a request is the current request: a later request for the
        same dispatch replaces the file (the request channel is not an
        immutable registry).

        Raises:
            TypeError: ``dispatch_id`` is not a str,
                ``manifest_requirements`` is not a sequence (a bare str is
                rejected), or an entry is not a str.
            LabAdapterDataError: a requirement is empty or duplicated, or
                ``requested_at`` is malformed.
            DispatchNotFoundError: no dispatch with that id is recorded.
        """
        if not isinstance(dispatch_id, str):
            raise TypeError(
                f"dispatch_id must be a str, got {type(dispatch_id).__name__}"
            )
        if not isinstance(manifest_requirements, Sequence) or isinstance(
            manifest_requirements, (str, bytes)
        ):
            raise TypeError(
                "manifest_requirements must be a sequence of strings, got"
                f" {type(manifest_requirements).__name__}"
            )
        if requested_at is not None:
            if not isinstance(requested_at, str) or not requested_at.strip():
                raise LabAdapterDataError(
                    "requested_at must be a non-empty string when set"
                )
        requirements: list[str] = []
        seen: set[str] = set()
        for requirement in manifest_requirements:
            if not isinstance(requirement, str):
                raise TypeError(
                    "manifest_requirements entries must be str, got"
                    f" {type(requirement).__name__}"
                )
            if not requirement.strip():
                raise LabAdapterDataError(
                    "manifest_requirements entries must be non-empty strings"
                )
            if requirement in seen:
                raise LabAdapterDataError(
                    "manifest_requirements contains duplicate requirement"
                    f" {requirement!r}"
                )
            seen.add(requirement)
            requirements.append(requirement)
        record = self._find_dispatch(dispatch_id)
        request_path = (
            self._base_dir
            / self._outgoing
            / record.run_id
            / MISSING_RESULT_REQUEST_FILENAME
        )
        request = MissingResultRequest(
            dispatch_id=dispatch_id,
            manifest_requirements=tuple(sorted(requirements)),
            request_path=str(request_path),
            requested_at=requested_at,
        )
        atomic_write(request_path, _canonical_json(request.to_dict()))
        return request

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_dispatch(self, dispatch_id: str) -> DispatchRecord:
        """Resolve one dispatch_id to its recorded dispatch.

        Dispatch records live one per run directory at
        ``<base_dir>/<outgoing>/<RUN_ID>/dispatch.json``; the scan is
        sorted (deterministic) and only reads files the adapter itself
        wrote.

        Raises:
            DispatchNotFoundError: no dispatch with that id is recorded.
            ValueError: a dispatch record file is corrupt.
        """
        outgoing_root = self._base_dir / self._outgoing
        if outgoing_root.is_dir():
            for run_dir in sorted(p for p in outgoing_root.iterdir() if p.is_dir()):
                record_path = run_dir / DISPATCH_RECORD_FILENAME
                if not record_path.is_file():
                    continue
                raw = _read_json_object(record_path, "lab dispatch record")
                if raw.get("dispatch_id") == dispatch_id:
                    return DispatchRecord.from_dict(raw)
        raise DispatchNotFoundError(
            f"no dispatch with id {dispatch_id!r} is recorded under"
            f" {outgoing_root}; dispatch the execution package first"
        )


def _list_files(directory: Path) -> tuple[str, ...]:
    """The file names directly inside ``directory``, sorted."""
    if not directory.is_dir():
        return ()
    return tuple(sorted(p.name for p in directory.iterdir() if p.is_file()))


def _read_json_object(path: Path, kind: str) -> dict[str, Any]:
    """Load and type a handoff record file, rejecting corrupt state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt {kind} record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"corrupt {kind} record at {path}: expected a JSON object")
    return raw


def _canonical_json(data: dict[str, Any]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"
