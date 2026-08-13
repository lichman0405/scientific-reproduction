"""SHA-256 checksum calculation and verification (DEV-M3-G02, deliverable).

``compute_sha256`` hashes a file on disk; a missing or unreadable file
raises ``ArtifactFileError`` so a caller cannot mistake a failed read for a
digest. ``verify_sha256`` is a pure predicate: it returns ``False`` (never
raises) for missing files, unreadable files, malformed ``expected``
digests, and checksum mismatches; ``True`` only on an exact match.

Files are read in 1 MiB chunks, so memory use stays bounded for large
artifacts (raw experimental data, images, HDF5 stores, ...).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from scientific_reproduction.artifacts.exceptions import ArtifactFileError

#: Read size in bytes for hashing; large artifacts are chunked, not slurped.
_CHUNK_SIZE = 1024 * 1024

#: A SHA-256 digest as hex: exactly 64 hex characters (case-insensitive,
#: matching the ``sha256`` pattern in schemas/artifact-manifest.schema.yaml).
_HEX_DIGEST_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")


def compute_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 hex digest of the file at ``path``.

    Raises:
        ArtifactFileError: if ``path`` is missing, is not a regular file
            (e.g. a directory), or cannot be read (e.g. permission denied).
    """
    if not path.is_file():
        raise ArtifactFileError(
            f"cannot compute SHA-256 of {str(path)!r}: not a regular file"
        )
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
                hasher.update(chunk)
    except OSError as exc:
        raise ArtifactFileError(
            f"cannot read {str(path)!r} for SHA-256: {exc}"
        ) from exc
    return hasher.hexdigest()


def verify_sha256(path: Path, expected: str) -> bool:
    """Return True iff the file at ``path`` hashes to ``expected``.

    ``expected`` must be a 64-character hex digest; the comparison is
    case-insensitive. Missing or unreadable files and malformed digests all
    evaluate to ``False`` -- this is a predicate, not a validator, so I/O
    problems are never raised.
    """
    if not isinstance(expected, str) or not _HEX_DIGEST_PATTERN.fullmatch(expected):
        return False
    try:
        actual = compute_sha256(path)
    except ArtifactFileError:
        return False
    return actual == expected.lower()
