"""Result manifest validation for returned lab packages (DEV-M7-G01).

Implements the **manifest validation** deliverable: the pure, frozen
decision layer that decides whether a returned Result Package can be
collected. Grounded in:

* ``10-EXPERIMENT-SUBSYSTEM.md`` SS4 (operator result requirements): the
  Result Package contains actual quantities/conditions, raw instrument
  files, operator notes, deviations -- and a *manifest/checksums where
  practical*; the result manifest is the operator-side declaration of
  what was returned;
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS3: the dispatched Execution Package
  declares its **required raw-data exports** (``required_return``); the
  returned package must cover them;
* ``10-EXPERIMENT-SUBSYSTEM.md`` SS2/SS1: the returned package is
  collected per Run (``lab/incoming/<RUN_ID>/``); a package whose run
  reference is absent or does not match the Run of the dispatch belongs
  to no or another Run and is **refused, never guessed, never silently
  matched** (AC-02).

Decision records, not silent defaults (AC-03)
---------------------------------------------
:func:`evaluate_result_manifest` is a pure function of the returned
manifest mapping, the expected run/package identity, the dispatched
package's required returns and the files actually present in the
incoming handoff. It returns the full :class:`ResultManifestAssessment`
decision record: which required manifest entries are missing
(``missing_fields``), which declared data files are absent
(``missing_files``), which required returns are uncovered
(``unmet_required_returns``) and whether the run/package association
matched (``run_matched`` / ``package_matched``). The I/O layer
(``filesystem.py``) refuses a collection iff the assessment is not
``valid``; the assessment itself is what the caller sees -- a stable,
specific record of WHAT is missing, never a silent default.

Boundaries
----------
The result manifest vocabulary is this module's own auditable shape
(no frozen product schema exists for the returned lab Result Package,
mirroring ``workers/results.py``'s own manifest vocabulary); the
execution package on the way out is schema-gated by the real
``core.schema_validation.validate_and_reject``. ``TypeError`` at the
public boundaries; ``ValueError``-subclass errors with stable messages
otherwise. No I/O: everything here is pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "RESULT_MANIFEST_REQUIRED_FIELDS",
    "RESULT_MANIFEST_RULESET_VERSION",
    "RESULT_MANIFEST_VERSION",
    "LabResultManifest",
    "ResultManifestAssessment",
    "ResultManifestError",
    "evaluate_result_manifest",
]

#: Version of the returned result manifest schema (``manifest_version``
#: key of :class:`LabResultManifest`).
RESULT_MANIFEST_VERSION: str = "1.0"

#: Version of the result-manifest evaluation rule set; recorded in every
#: assessment.
RESULT_MANIFEST_RULESET_VERSION: str = "1.0"

#: The required entries of a returned result manifest. ``files`` declares
#: the returned data files; the identity entries name the exact project,
#: goal, execution package and -- the Run association (AC-02) -- run the
#: package answers.
RESULT_MANIFEST_REQUIRED_FIELDS: tuple[str, ...] = (
    "manifest_version",
    "package_id",
    "project_id",
    "goal_id",
    "run_id",
    "files",
)


class ResultManifestError(ValueError):
    """Raised when a result manifest mapping violates the frozen record shape."""


@dataclass(frozen=True)
class LabResultManifest:
    """The typed record of one returned Result Package manifest.

    ``run_id`` is the Run association of the returned package (AC-02):
    exactly the ``run_id`` of the dispatched execution package's Run;
    ``files`` is the canonical sorted declaration of the returned data
    files (AC-03: every declared file must be present in the incoming
    handoff to be collected); ``notes`` carries optional operator notes.
    """

    manifest_version: str
    package_id: str
    project_id: str
    goal_id: str
    run_id: str
    files: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("manifest_version", "package_id", "project_id", "goal_id", "run_id"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(
                    f"LabResultManifest.{name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise ResultManifestError(
                    f"LabResultManifest.{name} must be a non-empty string, got"
                    f" {value!r}"
                )
        if not isinstance(self.files, tuple):
            raise TypeError(
                "LabResultManifest.files must be a tuple, got"
                f" {type(self.files).__name__}"
            )
        for name in self.files:
            if not isinstance(name, str):
                raise TypeError(
                    "LabResultManifest.files entries must be str, got"
                    f" {type(name).__name__}"
                )
            if not name.strip():
                raise ResultManifestError(
                    "LabResultManifest.files entries must be non-empty"
                    " strings"
                )
        if not isinstance(self.notes, tuple):
            raise TypeError(
                "LabResultManifest.notes must be a tuple, got"
                f" {type(self.notes).__name__}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result manifest in canonical field order."""
        data: dict[str, Any] = {
            "manifest_version": self.manifest_version,
            "package_id": self.package_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "run_id": self.run_id,
            "files": list(self.files),
        }
        data["notes"] = list(self.notes)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LabResultManifest:
        """Build a result manifest from a plain dict.

        ``files`` entries are sorted (canonical, deterministic records).
        Missing required entries raise :class:`ResultManifestError` with
        a stable message; wrong types raise ``TypeError``.

        Raises:
            TypeError: ``data`` is not a mapping, or a field value has the
                wrong type.
            ResultManifestError: a required entry is missing or empty.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "LabResultManifest.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        missing = [
            name for name in RESULT_MANIFEST_REQUIRED_FIELDS if name not in data
        ]
        if missing:
            raise ResultManifestError(
                "result manifest is missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        kwargs: dict[str, Any] = {
            name: data[name] for name in RESULT_MANIFEST_REQUIRED_FIELDS
        }
        kwargs["files"] = tuple(sorted(set(data["files"])))
        if "notes" in data:
            kwargs["notes"] = tuple(data["notes"])
        return cls(**kwargs)


@dataclass(frozen=True)
class ResultManifestAssessment:
    """Full decision record of one result-manifest evaluation (AC-02/AC-03).

    ``valid`` is False iff anything below failed; each category records
    exactly WHAT is missing or mismatched:

    * ``missing_fields`` -- the required result-manifest entries that are
      absent or malformed;
    * ``missing_files`` -- declared data files that are absent from the
      incoming handoff;
    * ``unmet_required_returns`` -- the dispatched package's
      ``required_return`` entries not covered by the returned files;
    * ``run_matched`` / ``package_matched`` -- the returned package's
      run/package reference is exactly the dispatch's, never guessed and
      never silently matched.

    ``declared_files`` / ``present_files`` / ``required_returns`` record
    the exact inputs the decision was computed from.
    """

    valid: bool
    run_id: str | None
    package_id: str | None
    expected_run_id: str | None
    expected_package_id: str | None
    run_matched: bool
    package_matched: bool
    missing_fields: tuple[str, ...]
    missing_files: tuple[str, ...]
    unmet_required_returns: tuple[str, ...]
    declared_files: tuple[str, ...]
    present_files: tuple[str, ...]
    ruleset_version: str = RESULT_MANIFEST_RULESET_VERSION


def evaluate_result_manifest(
    manifest: Mapping[str, Any],
    *,
    expected_run_id: str,
    expected_package_id: str,
    required_returns: Sequence[str],
    present_files: Sequence[str],
) -> ResultManifestAssessment:
    """Decide whether a returned Result Package manifest can be collected.

    Pure and deterministic: the decision is a pure function of the
    manifest mapping, the expected run/package identity, the dispatched
    package's required returns and the files actually present in the
    incoming handoff. Nothing is guessed and nothing is silently
    defaulted: an absent or mismatched run reference fails the
    association (AC-02) and every missing entry/file/return is recorded
    by name in the assessment (AC-03).

    Args:
        manifest: the returned result manifest (raw mapping read from
            the incoming handoff).
        expected_run_id: the ``run_id`` of the dispatch's Run (AC-02).
        expected_package_id: the ``package_id`` of the dispatched
            execution package.
        required_returns: the dispatched package's ``required_return``
            entries (the required raw-data exports).
        present_files: the file names actually present in the incoming
            handoff (the manifest file itself excluded).

    Returns:
        The :class:`ResultManifestAssessment` decision record.

    Raises:
        TypeError: ``manifest`` is not a mapping, ``expected_run_id`` /
            ``expected_package_id`` are not str, or
            ``required_returns`` / ``present_files`` are not sequences.
    """
    if not isinstance(manifest, Mapping):
        raise TypeError(
            "evaluate_result_manifest expects a Mapping manifest, got"
            f" {type(manifest).__name__}"
        )
    if not isinstance(expected_run_id, str):
        raise TypeError(
            "expected_run_id must be a str, got"
            f" {type(expected_run_id).__name__}"
        )
    if not isinstance(expected_package_id, str):
        raise TypeError(
            "expected_package_id must be a str, got"
            f" {type(expected_package_id).__name__}"
        )
    if not isinstance(required_returns, Sequence) or isinstance(
        required_returns, (str, bytes)
    ):
        raise TypeError(
            "required_returns must be a sequence of strings, got"
            f" {type(required_returns).__name__}"
        )
    if not isinstance(present_files, Sequence) or isinstance(
        present_files, (str, bytes)
    ):
        raise TypeError(
            "present_files must be a sequence of strings, got"
            f" {type(present_files).__name__}"
        )

    missing_fields: list[str] = []
    for name in RESULT_MANIFEST_REQUIRED_FIELDS:
        if not _field_shape_ok(name, manifest.get(name)):
            missing_fields.append(name)

    run_id = _string_entry(manifest, "run_id")
    package_id = _string_entry(manifest, "package_id")
    run_matched = run_id == expected_run_id
    package_matched = package_id == expected_package_id

    declared_files = tuple(sorted(set(_file_entries(manifest))))
    present = set(present_files)
    missing_files = tuple(
        sorted(name for name in declared_files if name not in present)
    )
    uncovered = set(required_returns) - set(declared_files)
    unmet_required_returns = tuple(sorted(uncovered))

    valid = (
        not missing_fields
        and run_matched
        and package_matched
        and not missing_files
        and not unmet_required_returns
    )
    return ResultManifestAssessment(
        valid=valid,
        run_id=run_id,
        package_id=package_id,
        expected_run_id=expected_run_id,
        expected_package_id=expected_package_id,
        run_matched=run_matched,
        package_matched=package_matched,
        missing_fields=tuple(missing_fields),
        missing_files=missing_files,
        unmet_required_returns=unmet_required_returns,
        declared_files=declared_files,
        present_files=tuple(sorted(set(present_files))),
    )


def _field_shape_ok(name: str, value: Any) -> bool:
    """True iff a required manifest entry is present with the right shape.

    String entries must be non-empty strings; ``files`` must be a list
    of non-empty strings (an empty declaration is a valid declaration:
    the returned package simply covers no data files).
    """
    if name == "files":
        return isinstance(value, list) and all(
            isinstance(entry, str) and entry.strip() for entry in value
        )
    return isinstance(value, str) and bool(value.strip())


def _string_entry(manifest: Mapping[str, Any], name: str) -> str | None:
    """The non-empty string value of one entry, or None when malformed."""
    value = manifest.get(name)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _file_entries(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """The declared file names, or () when the ``files`` entry is malformed."""
    value = manifest.get("files")
    if isinstance(value, list) and all(
        isinstance(entry, str) and entry.strip() for entry in value
    ):
        return tuple(value)
    return ()
