"""Integration: large raw artifacts stay out of Git while manifests and
audit remain traceable (DEV-M3-G03, AC-01/AC-02/AC-03).

End-to-end scenario the unit suites (tests/audit/, tests/artifacts/)
cannot express on their own:

* AC-01 -- a large raw artifact is registered through its manifest but is
  never committed to Git: the dummy artifact lives OUTSIDE the repository
  (14-STATE-GIT-ARTIFACTS.md SS6), so exclusion is structural, `git
  ls-files` never lists it, the object store never receives its blob, and
  read_file_at cannot read it back;
* AC-02 -- the manifest IS committed at the ``inventory.audit.passed``
  governance checkpoint (SS5 "Inventory audit passed"), readable from the
  Git object store (not the working copy) with the injected deterministic
  identity;
* AC-03 -- the manifest records the artifact's SHA-256 via the artifacts
  checksum module, and a deterministic post-registration modification is
  detected while the registered audit record stays intact.

The audit API is exercised exactly as the audit suite does: pinned
identity ``Audit Bot <audit@example.org>`` and fixed commit time
``2026-01-01T00:00:00Z`` (tests/audit/git_helpers.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from artifact_git_helpers import (
    ARTIFACT_FILENAME,
    ARTIFACT_SIZE_BYTES,
    MANIFEST_REL_PATH,
    git_blob_sha1,
    run_git,
    write_dummy_artifact,
    write_manifest,
)

from scientific_reproduction.artifacts.checksum import compute_sha256, verify_sha256
from scientific_reproduction.artifacts.verification import (
    VerificationStatus,
    verify_manifest,
)
from scientific_reproduction.audit.git import (
    GitAuditError,
    commit_checkpoint,
    count_commits,
    current_head,
    read_file_at,
)
from tests.audit.git_helpers import COMMIT_TIME, IDENTITY, make_repo, show_commit

# ---------------------------------------------------------------------------
# AC-01: the dummy large artifact is not committed to Git
# ---------------------------------------------------------------------------


def test_large_artifact_is_registered_but_not_in_git_history(tmp_path: Path) -> None:
    """AC-01: the large artifact is registered but never committed to Git."""
    repo = make_repo(tmp_path / "store")
    artifact_path = tmp_path / "raw" / ARTIFACT_FILENAME
    write_dummy_artifact(artifact_path)
    write_manifest(repo, artifact_path)

    commit_checkpoint(
        repo,
        kind="inventory.audit.passed",
        files=[repo / MANIFEST_REL_PATH],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    # The artifact is registered: it exists on disk at the manifest's uri.
    assert artifact_path.is_file()
    assert artifact_path.stat().st_size == ARTIFACT_SIZE_BYTES
    # ... but it lives outside the repository, so exclusion is structural.
    assert not artifact_path.resolve().is_relative_to(repo.resolve())
    # git ls-files lists the committed manifest, never the artifact.
    tracked = run_git(repo, "ls-files").stdout.splitlines()
    assert tracked == [MANIFEST_REL_PATH.as_posix()]
    # The artifact path does not exist in the object store at HEAD either.
    with pytest.raises(GitAuditError):
        read_file_at(repo, str(artifact_path))
    # The working tree is clean: nothing about the artifact can leak.
    assert run_git(repo, "status", "--porcelain").stdout == ""


def test_large_artifact_blob_is_absent_from_git_object_store(tmp_path: Path) -> None:
    """AC-01: the artifact content never entered the git object database.

    The strongest negative proof: even the raw blob object that a commit
    of the artifact would have created does not exist in the fixture
    repository, so no ref, reflog or GC sweep could ever have recorded it.
    The manifest's own blob serves as a positive control for the probe.
    """
    repo = make_repo(tmp_path / "store")
    artifact_path = tmp_path / "raw" / ARTIFACT_FILENAME
    payload = write_dummy_artifact(artifact_path)
    write_manifest(repo, artifact_path)
    commit_checkpoint(
        repo,
        kind="inventory.audit.passed",
        files=[repo / MANIFEST_REL_PATH],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    artifact_blob = git_blob_sha1(payload)
    probe = run_git(repo, "cat-file", "-e", artifact_blob, check=False)
    assert probe.returncode != 0
    # Positive control: the committed manifest IS in the store, so a
    # failing probe really means "the object does not exist" (tree-path
    # form, independent of any checkout-time line-ending normalization).
    probe = run_git(repo, "cat-file", "-e", f"HEAD:{MANIFEST_REL_PATH.as_posix()}", check=False)
    assert probe.returncode == 0


# ---------------------------------------------------------------------------
# AC-02: the manifest is committed and auditable at the checkpoint
# ---------------------------------------------------------------------------


def test_manifest_is_committed_at_inventory_checkpoint(tmp_path: Path) -> None:
    """AC-02: the manifest is committed and auditable at the checkpoint."""
    repo = make_repo(tmp_path / "store")
    artifact_path = tmp_path / "raw" / ARTIFACT_FILENAME
    write_dummy_artifact(artifact_path)
    manifest = write_manifest(repo, artifact_path)

    record = commit_checkpoint(
        repo,
        kind="inventory.audit.passed",
        files=[repo / MANIFEST_REL_PATH],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    assert record.kind == "inventory.audit.passed"
    assert record.message == "inventory audit passed"
    assert record.commit_sha == current_head(repo)
    assert count_commits(repo) == 1
    # The exact manifest content is in the object store at the commit.
    committed = json.loads(read_file_at(repo, MANIFEST_REL_PATH))
    assert committed == manifest.to_dict()
    # Deterministic identity and date, independent of any git config.
    assert show_commit(repo, "%an|%ae|%cn|%ce|%aI") == (
        "Audit Bot|audit@example.org|Audit Bot|audit@example.org|"
        "2026-01-01T00:00:00Z"
    )


def test_committed_manifest_reads_from_object_store_not_working_copy(
    tmp_path: Path,
) -> None:
    """AC-02: the committed manifest survives working-copy modification.

    ``read_file_at`` reads the Git object store, not the working copy: the
    audit record is exactly what the checkpoint committed, so later edits
    of the manifest file on disk cannot change what was audited.
    """
    repo = make_repo(tmp_path / "store")
    artifact_path = tmp_path / "raw" / ARTIFACT_FILENAME
    write_dummy_artifact(artifact_path)
    manifest = write_manifest(repo, artifact_path)

    commit_checkpoint(
        repo,
        kind="inventory.audit.passed",
        files=[repo / MANIFEST_REL_PATH],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    original = manifest.to_dict()
    working_copy = repo / MANIFEST_REL_PATH
    tampered = json.loads(working_copy.read_text(encoding="utf-8"))
    tampered["metadata"]["tampered"] = True
    working_copy.write_text(json.dumps(tampered), encoding="utf-8")

    assert json.loads(read_file_at(repo, MANIFEST_REL_PATH)) == original
    assert count_commits(repo) == 1
    # The modification is visible to git but changes nothing committed.
    assert run_git(repo, "status", "--porcelain").stdout != ""


# ---------------------------------------------------------------------------
# AC-03: the checksum detects post-registration modification
# ---------------------------------------------------------------------------


def test_checksum_detects_post_registration_modification(tmp_path: Path) -> None:
    """AC-03: the registered SHA-256 detects a tampered artifact."""
    repo = make_repo(tmp_path / "store")
    artifact_path = tmp_path / "raw" / ARTIFACT_FILENAME
    payload = write_dummy_artifact(artifact_path)
    # A pristine fixture copy of the exact registered bytes.
    pristine_path = tmp_path / "pristine" / ARTIFACT_FILENAME
    pristine_path.parent.mkdir(parents=True, exist_ok=True)
    pristine_path.write_bytes(payload)
    manifest = write_manifest(repo, artifact_path)
    registered_sha256 = manifest.sha256

    commit_checkpoint(
        repo,
        kind="inventory.audit.passed",
        files=[repo / MANIFEST_REL_PATH],
        identity=IDENTITY,
        commit_time=COMMIT_TIME,
    )

    # Before modification: artifact and pristine copy both match.
    assert verify_sha256(artifact_path, registered_sha256) is True
    assert verify_sha256(pristine_path, registered_sha256) is True

    # Deterministic post-registration modification: append a fixed marker.
    with artifact_path.open("ab") as handle:
        handle.write(b"\x00TAMPERED\x00" * 32)

    # The recomputed checksum differs from the registered one ...
    assert compute_sha256(artifact_path) != registered_sha256
    assert verify_sha256(artifact_path, registered_sha256) is False
    # ... and artifact verification reports CHANGED against the manifest.
    assert verify_manifest(manifest).status is VerificationStatus.CHANGED
    # The registered audit record stays intact: the recorded checksum still
    # matches the ORIGINAL bytes (the pristine fixture copy).
    assert verify_sha256(pristine_path, registered_sha256) is True
    assert compute_sha256(pristine_path) == registered_sha256


# ---------------------------------------------------------------------------
# Determinism of the fixture itself
# ---------------------------------------------------------------------------


def test_dummy_artifact_generation_is_deterministic(tmp_path: Path) -> None:
    """The dummy artifact is fixed bytes: same content on every platform."""
    first_path = tmp_path / "one" / ARTIFACT_FILENAME
    second_path = tmp_path / "two" / ARTIFACT_FILENAME
    first = write_dummy_artifact(first_path)
    second = write_dummy_artifact(second_path)
    assert first == second
    assert len(first) == ARTIFACT_SIZE_BYTES
    assert compute_sha256(first_path) == compute_sha256(second_path)
