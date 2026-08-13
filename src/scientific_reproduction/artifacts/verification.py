"""Artifact verification: resolve a manifest URI and check the target file.

A manifest's ``uri`` is stored verbatim by the registry and resolved here
exactly as given -- ``file://`` URIs and plain absolute paths are treated
the same, so artifacts living **outside** the Git repository verify the
same way as in-repository ones (AC-03). No repository-relative
interpretation is ever applied.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.artifacts.exceptions import ArtifactFileError
from scientific_reproduction.core.models import ArtifactManifest


class VerificationStatus(StrEnum):
    """Outcome of a single artifact verification."""

    #: File present, byte size and SHA-256 both match the manifest.
    OK = "OK"
    #: Nothing exists at the resolved path.
    MISSING = "MISSING"
    #: File present but its byte size or SHA-256 differs from the manifest.
    CHANGED = "CHANGED"
    #: The path exists but cannot be read as a regular file (directory,
    #: permission denied, I/O error).
    UNREADABLE = "UNREADABLE"
    #: The URI has a scheme that is not ``file`` and is not a plain path
    #: (e.g. ``https://``), so no local check is possible.
    REMOTE = "REMOTE"


@dataclass(frozen=True)
class ArtifactVerificationResult:
    """Result of verifying the artifact a manifest points at."""

    artifact_id: str
    uri: str
    status: VerificationStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        """True iff the artifact is present and unchanged (status OK)."""
        return self.status is VerificationStatus.OK


#: Windows drive-letter prefix inside a ``file://`` URL path,
#: e.g. ``file:///C:/data.csv`` -> ``/C:/data.csv``.
_WINDOWS_DRIVE_PATH = re.compile(r"^/[A-Za-z]:")


def _looks_like_windows_path(value: str) -> bool:
    """True for plain Windows paths like ``C:\\data\\x.csv``.

    ``urllib.parse.urlparse`` misreads the drive letter as a URI scheme
    (``C:``), so these must be detected before scheme handling.
    """
    return len(value) >= 2 and value[0].isalpha() and value[1] == ":"


def resolve_artifact_path(uri: str) -> Path | None:
    """Resolve a manifest ``uri`` to a local filesystem path.

    Supported forms:

    * ``file://`` URIs (``file:///C:/data/isotherm.csv``,
      ``file:///data/isotherm.csv``);
    * plain paths, absolute or relative (``C:\\data\\isotherm.csv``,
      ``data/isotherm.csv``) -- used exactly as given.

    Returns ``None`` for URIs with any other scheme (``https://``, ``s3://``,
    ...), which cannot be verified against the local filesystem.
    """
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme == "file":
        path_str = urllib.parse.unquote(parsed.path)
        if os.name == "nt" and _WINDOWS_DRIVE_PATH.match(path_str):
            path_str = path_str[1:]
        return Path(path_str)
    if parsed.scheme and not _looks_like_windows_path(uri):
        return None
    return Path(uri)


def _result(
    manifest: ArtifactManifest, status: VerificationStatus, detail: str
) -> ArtifactVerificationResult:
    return ArtifactVerificationResult(
        artifact_id=manifest.artifact_id,
        uri=manifest.uri,
        status=status,
        detail=detail,
    )


def verify_manifest(manifest: ArtifactManifest) -> ArtifactVerificationResult:
    """Verify that the artifact a manifest points at is present and unchanged.

    The URI is resolved exactly as recorded (``file://`` or plain path,
    no repository-relative interpretation), so artifacts outside the Git
    repository verify the same way as in-repository ones (AC-03).

    Status mapping:

    * ``OK`` -- file present, byte size and SHA-256 both match the manifest;
    * ``MISSING`` -- nothing exists at the resolved path;
    * ``UNREADABLE`` -- the path exists but cannot be read as a regular file;
    * ``CHANGED`` -- file present but its byte size or SHA-256 differs;
    * ``REMOTE`` -- the URI scheme is not ``file`` and not a plain path, so
      no local check is possible.
    """
    path = resolve_artifact_path(manifest.uri)
    if path is None:
        return _result(
            manifest,
            VerificationStatus.REMOTE,
            f"URI scheme not locally verifiable: {manifest.uri}",
        )
    if not path.exists():
        return _result(
            manifest,
            VerificationStatus.MISSING,
            f"no file at {str(path)!r}",
        )
    if not path.is_file():
        return _result(
            manifest,
            VerificationStatus.UNREADABLE,
            f"not a regular file: {str(path)!r}",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _result(manifest, VerificationStatus.UNREADABLE, f"stat failed: {exc}")
    if size != manifest.size_bytes:
        return _result(
            manifest,
            VerificationStatus.CHANGED,
            f"size mismatch: manifest {manifest.size_bytes} bytes, "
            f"file {size} bytes",
        )
    try:
        actual = compute_sha256(path)
    except ArtifactFileError as exc:
        return _result(manifest, VerificationStatus.UNREADABLE, str(exc))
    if actual != manifest.sha256.lower():
        return _result(
            manifest,
            VerificationStatus.CHANGED,
            "sha256 mismatch: file content changed",
        )
    return _result(
        manifest,
        VerificationStatus.OK,
        "size and sha256 match the manifest",
    )
