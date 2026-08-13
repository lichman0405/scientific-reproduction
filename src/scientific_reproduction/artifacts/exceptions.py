"""Error types for the artifact subsystem (DEV-M3-G02).

All errors raised by ``scientific_reproduction.artifacts`` derive from
``ArtifactError`` so callers can catch the subsystem surface with one
``except`` clause.
"""

from __future__ import annotations


class ArtifactError(Exception):
    """Base class for all artifact subsystem errors."""


class ArtifactNotFoundError(ArtifactError, KeyError):
    """Raised when no manifest with the requested ``artifact_id`` is registered."""


class ArtifactExistsError(ArtifactError, ValueError):
    """Raised when registering an ``artifact_id`` that is already registered."""


class InvalidManifestError(ArtifactError, ValueError):
    """Raised when a manifest fails schema or format validation.

    Used both at the persistence gate (invalid manifests are rejected
    before they are written) and when a stored manifest file on disk turns
    out to be corrupt or schema-invalid.
    """


class ArtifactFileError(ArtifactError):
    """Raised when an artifact target file is missing or cannot be read."""
