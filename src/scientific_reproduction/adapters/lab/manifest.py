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

Required-return coverage (result-manifest v1.1)
----------------------------------------------
The dispatched package's ``required_return`` entries are raw-data-export
tokens (``10-EXPERIMENT-SUBSYSTEM.md`` SS3), not file names. A returned
file covers a required return when the result manifest explicitly maps
the token to the returned file name via the optional
``required_return_files`` entry (``{token: file name}`` -- the
operator's declaration of coverage; AC-03: coverage is never guessed
from file stems or extensions). The mapped file must be declared in
``files`` and present in the incoming handoff. A required return without
an explicit mapping entry stays on the v1.0 rule: it is covered only by
a declared file whose name is exactly the token. A ``required_return_files``
entry that is present but malformed is recorded as a missing manifest
field, and the assessment carries the exact mapping the decision was
computed from.

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
    "RESULT_MANIFEST_RETURN_MAPPING_KEY",
    "RESULT_MANIFEST_RULESET_VERSION",
    "RESULT_MANIFEST_VERSION",
    "LabResultManifest",
    "ResultManifestAssessment",
    "ResultManifestError",
    "evaluate_result_manifest",
]

#: Version of the returned result manifest schema (``manifest_version``
#: key of :class:`LabResultManifest`). ``1.1`` adds the optional
#: ``required_return_files`` mapping (explicit required-return coverage
#: declarations); ``1.0`` manifests remain valid -- the mapping is
#: optional.
RESULT_MANIFEST_VERSION: str = "1.1"

#: Version of the result-manifest evaluation rule set; recorded in every
#: assessment. ``1.1`` adds explicit required-return coverage via the
#: manifest's ``required_return_files`` mapping; the v1.0 exact-name
#: rule remains the fallback for unmapped required returns.
RESULT_MANIFEST_RULESET_VERSION: str = "1.1"

#: The optional result-manifest entry mapping each ``required_return``
#: token to the returned file name that covers it (``{token: file name}``).
RESULT_MANIFEST_RETURN_MAPPING_KEY: str = "required_return_files"

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
    handoff to be collected); ``required_return_files`` is the canonical
    sorted ``(required return, file name)`` mapping declaring which
    returned file covers which dispatched ``required_return`` entry
    (empty when the manifest declares no mapping); ``notes`` carries
    optional operator notes.
    """

    manifest_version: str
    package_id: str
    project_id: str
    goal_id: str
    run_id: str
    files: tuple[str, ...]
    required_return_files: tuple[tuple[str, str], ...] = ()
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
        if not isinstance(self.required_return_files, tuple):
            raise TypeError(
                "LabResultManifest.required_return_files must be a tuple"
                " of (required return, file name) pairs, got"
                f" {type(self.required_return_files).__name__}"
            )
        for entry in self.required_return_files:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise TypeError(
                    "LabResultManifest.required_return_files entries must"
                    " be (required return, file name) pairs, got"
                    f" {type(entry).__name__}"
                )
            for value in entry:
                if not isinstance(value, str):
                    raise TypeError(
                        "LabResultManifest.required_return_files entries"
                        " must hold str values, got"
                        f" {type(value).__name__}"
                    )
                if not value.strip():
                    raise ResultManifestError(
                        "LabResultManifest.required_return_files entries"
                        " must be (required return, file name) pairs of"
                        " non-empty strings"
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
        data[RESULT_MANIFEST_RETURN_MAPPING_KEY] = dict(self.required_return_files)
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
        if RESULT_MANIFEST_RETURN_MAPPING_KEY in data:
            kwargs["required_return_files"] = _canonical_return_mapping(
                data[RESULT_MANIFEST_RETURN_MAPPING_KEY]
            )
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
    the exact inputs the decision was computed from;
    ``required_return_files`` records the manifest's explicit
    ``{required return: file name}`` coverage mapping (canonical sorted
    pairs, empty when the manifest declares none).
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
    required_return_files: tuple[tuple[str, str], ...] = ()
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
    by name in the assessment (AC-03). A required return is covered by
    the manifest's explicit ``required_return_files`` mapping entry when
    one exists (the mapped file must be declared and present), else by
    a declared file whose name is exactly the token (the v1.0 rule).

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

    # The optional required_return_files mapping: the operator's explicit
    # declaration of which returned file covers which required return
    # (AC-03 -- coverage is never guessed from file stems or extensions).
    # A mapping that is present but malformed is corrupt operator data:
    # recorded as a missing manifest field, never silently dropped.
    return_mapping: dict[str, str] = {}
    if RESULT_MANIFEST_RETURN_MAPPING_KEY in manifest:
        mapping_value = manifest[RESULT_MANIFEST_RETURN_MAPPING_KEY]
        if not _return_mapping_shape_ok(mapping_value):
            missing_fields.append(RESULT_MANIFEST_RETURN_MAPPING_KEY)
        else:
            return_mapping = dict(mapping_value)

    unmet_required_returns = tuple(
        sorted(
            required
            for required in set(required_returns)
            if not _required_return_covered(
                required, declared_files, present, return_mapping
            )
        )
    )

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
        required_return_files=tuple(sorted(return_mapping.items())),
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


def _return_mapping_shape_ok(value: Any) -> bool:
    """True iff an optional ``required_return_files`` entry is a mapping
    of non-empty strings to non-empty strings.

    A malformed entry is corrupt operator data: it is recorded as a
    missing manifest field rather than silently dropped.
    """
    return isinstance(value, Mapping) and all(
        isinstance(token, str)
        and token.strip()
        and isinstance(filename, str)
        and filename.strip()
        for token, filename in value.items()
    )


def _required_return_covered(
    required: str,
    declared_files: Sequence[str],
    present_files: set[str],
    return_mapping: Mapping[str, str],
) -> bool:
    """True iff one required return is covered by the returned files.

    An explicit ``required_return_files`` mapping entry is the
    operator's declaration of coverage (AC-03 -- never guessed from file
    stems or extensions): the mapped file must be declared in ``files``
    and actually present in the incoming handoff. Without an explicit
    entry, the v1.0 rule applies: the required return is covered only by
    a declared file whose name is exactly the token.
    """
    mapped_file = return_mapping.get(required)
    if mapped_file is not None:
        return mapped_file in declared_files and mapped_file in present_files
    return required in declared_files


def _canonical_return_mapping(value: Any) -> tuple[tuple[str, str], ...]:
    """Canonical sorted ``(required return, file name)`` pairs of an
    optional ``required_return_files`` mapping.

    A malformed mapping is corrupt operator data and is refused (never
    silently dropped from the typed record).

    Raises:
        TypeError: ``value`` is not a mapping.
        ResultManifestError: a key or value is not a non-empty string.
    """
    if not isinstance(value, Mapping):
        raise TypeError(
            "LabResultManifest.required_return_files must be a mapping of"
            f" required returns to file names, got {type(value).__name__}"
        )
    entries: list[tuple[str, str]] = []
    for token, filename in value.items():
        if not isinstance(token, str) or not token.strip():
            raise ResultManifestError(
                "result manifest required_return_files keys must be"
                " non-empty strings"
            )
        if not isinstance(filename, str) or not filename.strip():
            raise ResultManifestError(
                "result manifest required_return_files values must be"
                " non-empty strings"
            )
        entries.append((token, filename))
    return tuple(sorted(entries))
