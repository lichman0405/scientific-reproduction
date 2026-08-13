"""Shared helpers for the large-artifact audit integration suite (DEV-M3-G03).

The suite proves, end to end across the audit and artifacts subsystems,
the artifact-store contract of ``14-STATE-GIT-ARTIFACTS.md``:

* AC-01 -- a large raw artifact is registered (its manifest records it)
  but never enters Git history: the dummy artifact lives OUTSIDE the
  repository, in the test's temp dir, so exclusion is structural
  (SS6: "Large artifacts may reside on project storage/NAS/HPC/object
  store");
* AC-02 -- the artifact manifest IS committed at the
  ``inventory.audit.passed`` governance checkpoint (SS5 "Inventory audit
  passed") and remains readable from the Git object store;
* AC-03 -- the manifest's SHA-256 makes post-registration modification of
  the artifact detectable.

``IDENTITY`` / ``COMMIT_TIME`` pin every deterministic input, exactly as
the audit suite does (tests/audit/git_helpers.py): identity
"Audit Bot <audit@example.org>", commit time 2026-01-01T00:00:00Z. The
dummy artifact is a fixed repeated byte pattern -- no RNG -- so its
content is identical on every platform and every run.

This module is deliberately NOT named ``helpers.py``: pytest's
module-name import mode would collide with tests/artifacts/helpers.py.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.models import ArtifactManifest
from tests.audit.git_helpers import COMMIT_TIME, IDENTITY, make_repo, show_commit

#: Fixed artifact identity used across the suite.
ARTIFACT_ID = "ART-RAW-001"

#: The dummy artifact's file name (matches its id, like the registry store).
ARTIFACT_FILENAME = f"{ARTIFACT_ID}.bin"

#: A few megabytes: large enough to be meaningful for the git-exclusion
#: claim, small enough to keep the suite fast and deterministic.
ARTIFACT_SIZE_BYTES = 4 * 1024 * 1024

#: Deterministic repeated byte pattern; the artifact is exactly this
#: pattern repeated to ``ARTIFACT_SIZE_BYTES``.
ARTIFACT_PATTERN = b"\xa5\x5a\x3c\xc3"

#: Where the manifest lives inside the repository (relative POSIX path).
MANIFEST_REL_PATH = Path("artifacts") / f"{ARTIFACT_ID}.json"

__all__ = [
    "ARTIFACT_FILENAME",
    "ARTIFACT_ID",
    "ARTIFACT_PATTERN",
    "ARTIFACT_SIZE_BYTES",
    "COMMIT_TIME",
    "IDENTITY",
    "MANIFEST_REL_PATH",
    "git_blob_sha1",
    "make_repo",
    "run_git",
    "show_commit",
    "write_dummy_artifact",
    "write_manifest",
]


def write_dummy_artifact(path: Path) -> bytes:
    """Write the deterministic dummy large artifact and return its bytes.

    The payload is the fixed 4-byte ``ARTIFACT_PATTERN`` repeated to
    exactly ``ARTIFACT_SIZE_BYTES``, so every run produces identical
    bytes on every platform. The parent directory is created if needed.
    """
    payload = ARTIFACT_PATTERN * (ARTIFACT_SIZE_BYTES // len(ARTIFACT_PATTERN))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def write_manifest(repo_root: Path, artifact_path: Path) -> ArtifactManifest:
    """Write the manifest JSON for ``artifact_path`` inside ``repo_root``.

    The manifest truthfully describes the artifact -- real SHA-256 and
    byte size computed through the artifacts subsystem -- and points its
    ``uri`` at the artifact's actual location outside the repository
    (DEV-M3-G02 AC-03: a manifest may point outside the Git tree).
    """
    manifest = ArtifactManifest(
        artifact_id=ARTIFACT_ID,
        uri=str(artifact_path),
        sha256=compute_sha256(artifact_path),
        size_bytes=artifact_path.stat().st_size,
        created_at="2026-01-01T00:00:00Z",
        run_id="RUN-001",
        mime_type="application/octet-stream",
        producer="experiment_worker",
        metadata={"kind": "raw-isotherm", "pattern": "a55a3cc3"},
    )
    manifest_path = repo_root / MANIFEST_REL_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def run_git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <repo> <args>`` with explicit args (no shell)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def git_blob_sha1(payload: bytes) -> str:
    """The git object id of a blob holding ``payload`` (deterministic).

    Git hashes blobs as ``sha1("blob <size>\\x00<content>")``; the result
    is stable for fixed input on every platform, so the test can probe the
    fixture repository's object store for the artifact's blob object.
    """
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()
