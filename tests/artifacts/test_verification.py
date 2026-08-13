"""Acceptance tests for artifact verification (DEV-M3-G02).

Maps directly onto the frozen acceptance criteria:

* AC-01 -- registered artifact checksum validates: a truthful manifest
  verifies OK; corrupted content reports CHANGED;
* AC-02 -- missing or changed artifact is detected: a deleted file reports
  MISSING, modified content reports CHANGED;
* AC-03 -- the manifest can point outside the Git repository: files created
  under ``tmp_path`` (outside the worktree) verify the same via plain
  absolute path and ``file://`` URI.

Edge cases: empty files, unreadable paths (directories), remote URI
schemes, relative paths, uppercase digests, run linkage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import manifest_for_file, plain_manifest

from scientific_reproduction.artifacts.exceptions import ArtifactNotFoundError
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.artifacts.verification import VerificationStatus

#: The worktree root: tests/artifacts/test_verification.py -> repo root.
WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def make_registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "artifacts")


def assert_outside_repo(path: Path) -> None:
    """Fail the test if ``path`` accidentally lives inside the Git worktree."""
    try:
        path.resolve().relative_to(WORKTREE_ROOT)
    except ValueError:
        return
    raise AssertionError(f"{path} must live outside the repository for AC-03")


# ---------------------------------------------------------------------------
# AC-01: registered artifact checksum validates
# ---------------------------------------------------------------------------


def test_ac01_truthful_manifest_verifies_ok(tmp_path: Path) -> None:
    data_file = tmp_path / "isotherm.csv"
    data_file.write_text("T,P\n300,1.0\n300.5,1.0\n", encoding="utf-8")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    result = registry.verify("ART-001")
    assert result.ok
    assert result.status is VerificationStatus.OK
    assert result.detail


def test_ac01_empty_file_verifies_ok(tmp_path: Path) -> None:
    data_file = tmp_path / "empty.bin"
    data_file.write_bytes(b"")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    assert registry.verify("ART-001").ok


def test_ac01_uppercase_digest_still_verifies(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"payload")
    registry = make_registry(tmp_path)
    manifest = manifest_for_file("ART-001", data_file)
    # The frozen schema accepts [a-fA-F0-9]{64}; uppercase must verify too.
    registry.register(
        plain_manifest(
            "ART-001",
            uri=str(data_file),
            sha256=manifest.sha256.upper(),
            size_bytes=manifest.size_bytes,
        )
    )
    assert registry.verify("ART-001").ok


def test_ac01_corrupted_content_reports_changed(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"original bytes")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    # Corrupt the artifact after registration, keeping the same byte size so
    # the SHA-256 mismatch branch is exercised (not the size branch).
    data_file.write_bytes(b"0riginal bytes")
    result = registry.verify("ART-001")
    assert not result.ok
    assert result.status is VerificationStatus.CHANGED
    assert "sha256 mismatch" in result.detail


# ---------------------------------------------------------------------------
# AC-02: missing or changed artifact is detected
# ---------------------------------------------------------------------------


def test_ac02_deleted_file_reports_missing(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"gone soon")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    data_file.unlink()
    result = registry.verify("ART-001")
    assert not result.ok
    assert result.status is VerificationStatus.MISSING
    assert "no file at" in result.detail


def test_ac02_resized_file_reports_changed(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"abc")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    data_file.write_bytes(b"abcd")  # size change -> size mismatch branch
    result = registry.verify("ART-001")
    assert not result.ok
    assert result.status is VerificationStatus.CHANGED
    assert "size mismatch" in result.detail


def test_ac02_same_size_different_content_reports_changed(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"abc")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    data_file.write_bytes(b"abd")  # same size, different content
    result = registry.verify("ART-001")
    assert not result.ok
    assert result.status is VerificationStatus.CHANGED


# ---------------------------------------------------------------------------
# AC-03: manifest can point outside the Git repository
# ---------------------------------------------------------------------------


def test_ac03_absolute_path_outside_repo_verifies(tmp_path: Path) -> None:
    data_file = tmp_path / "external-data.csv"
    data_file.write_text("run1,0.5\nrun2,0.7\n", encoding="utf-8")
    assert_outside_repo(data_file)
    registry = make_registry(tmp_path)
    # uri is a plain absolute path -- no repository-relative resolution.
    registry.register(manifest_for_file("ART-001", data_file, uri=str(data_file)))
    assert registry.verify("ART-001").ok


def test_ac03_file_uri_outside_repo_verifies(tmp_path: Path) -> None:
    data_file = tmp_path / "external-raw.h5"
    data_file.write_bytes(b"\x89HDF\x00\x01raw bytes")
    assert_outside_repo(data_file)
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file, uri=data_file.as_uri()))
    result = registry.verify("ART-001")
    assert result.ok
    assert result.uri.startswith("file://")


def test_ac03_missing_file_outside_repo_is_detected(tmp_path: Path) -> None:
    target = tmp_path / "never-written.bin"
    assert_outside_repo(target)
    registry = make_registry(tmp_path)
    registry.register(
        plain_manifest("ART-001", uri=str(target), sha256="b" * 64, size_bytes=0)
    )
    assert registry.verify("ART-001").status is VerificationStatus.MISSING


# ---------------------------------------------------------------------------
# verify_all and edge cases
# ---------------------------------------------------------------------------


def test_verify_all_reports_every_manifest_in_order(tmp_path: Path) -> None:
    ok_file = tmp_path / "ok.bin"
    ok_file.write_bytes(b"fine")
    changed_file = tmp_path / "changed.bin"
    changed_file.write_bytes(b"v1")
    missing_file = tmp_path / "missing.bin"
    missing_file.write_bytes(b"v1")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-A", ok_file))
    registry.register(manifest_for_file("ART-B", changed_file))
    registry.register(manifest_for_file("ART-C", missing_file))
    changed_file.write_bytes(b"v2")  # corrupt after registration
    missing_file.unlink()  # delete after registration

    results = registry.verify_all()
    assert [r.artifact_id for r in results] == ["ART-A", "ART-B", "ART-C"]
    assert [r.status for r in results] == [
        VerificationStatus.OK,
        VerificationStatus.CHANGED,
        VerificationStatus.MISSING,
    ]


def test_verify_unknown_artifact_raises(tmp_path: Path) -> None:
    with pytest.raises(ArtifactNotFoundError):
        make_registry(tmp_path).verify("ART-NOPE")


def test_directory_as_target_reports_unreadable(tmp_path: Path) -> None:
    target_dir = tmp_path / "a-directory"
    target_dir.mkdir()
    registry = make_registry(tmp_path)
    registry.register(
        plain_manifest("ART-001", uri=str(target_dir), sha256="c" * 64, size_bytes=0)
    )
    result = registry.verify("ART-001")
    assert result.status is VerificationStatus.UNREADABLE
    assert "not a regular file" in result.detail


def test_remote_uri_scheme_reports_remote(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.register(
        plain_manifest("ART-001", uri="https://example.com/data/isotherm.csv")
    )
    result = registry.verify("ART-001")
    assert result.status is VerificationStatus.REMOTE
    assert not result.ok


def test_relative_path_that_does_not_exist_reports_missing(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.register(
        plain_manifest("ART-001", uri="relative/data.txt", sha256="d" * 64, size_bytes=0)
    )
    # Resolved exactly as given -- no repository-relative interpretation.
    assert registry.verify("ART-001").status is VerificationStatus.MISSING


def test_manifest_with_run_linkage_verifies(tmp_path: Path) -> None:
    data_file = tmp_path / "result.csv"
    data_file.write_text("a,b\n", encoding="utf-8")
    registry = make_registry(tmp_path)
    registry.register(
        manifest_for_file(
            "ART-001",
            data_file,
            run_id="RUN-042",
            analysis_id="ANL-007",
            producer="computation_worker",
            mime_type="text/csv",
        )
    )
    manifest = registry.get("ART-001")
    assert manifest.run_id == "RUN-042"
    assert manifest.analysis_id == "ANL-007"
    assert registry.verify("ART-001").ok
