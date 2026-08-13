"""Artifact registration, storage and integrity verification (DEV-M3-G02).

Public surface:

* ``compute_sha256`` / ``verify_sha256`` -- SHA-256 calculation and
  verification for artifact files;
* ``ArtifactRegistry`` -- filesystem-backed artifact manifest registry
  (per-object JSON files, atomic writes, schema-validated persistence);
* ``verify_manifest`` / ``ArtifactVerificationResult`` /
  ``VerificationStatus`` -- artifact integrity verification that resolves
  manifest URIs exactly as given (artifacts may live outside the Git repo);
* the error hierarchy rooted at ``ArtifactError``.
"""

from scientific_reproduction.artifacts.checksum import compute_sha256, verify_sha256
from scientific_reproduction.artifacts.exceptions import (
    ArtifactError,
    ArtifactExistsError,
    ArtifactFileError,
    ArtifactNotFoundError,
    InvalidManifestError,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.artifacts.verification import (
    ArtifactVerificationResult,
    VerificationStatus,
    verify_manifest,
)

__all__ = [
    "ArtifactError",
    "ArtifactExistsError",
    "ArtifactFileError",
    "ArtifactNotFoundError",
    "ArtifactRegistry",
    "ArtifactVerificationResult",
    "InvalidManifestError",
    "VerificationStatus",
    "compute_sha256",
    "verify_manifest",
    "verify_sha256",
]
