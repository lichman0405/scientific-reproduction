"""Tests for SHA-256 calculation and verification (DEV-M3-G02 deliverable).

Covered: known digests (empty file, ``b"abc"``), chunked hashing of large
files, missing-file and directory errors for ``compute_sha256``, and the
predicate semantics of ``verify_sha256`` (never raises, ``False`` for
missing/unreadable/malformed/mismatched).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scientific_reproduction.artifacts.checksum import compute_sha256, verify_sha256
from scientific_reproduction.artifacts.exceptions import ArtifactFileError

#: Known SHA-256 digest of the empty file.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
#: Known SHA-256 digest of b"abc".
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_compute_sha256_known_vector(tmp_path: Path) -> None:
    file = tmp_path / "abc.txt"
    file.write_bytes(b"abc")
    assert compute_sha256(file) == ABC_SHA256
    assert compute_sha256(file) == hashlib.sha256(b"abc").hexdigest()


def test_compute_sha256_empty_file(tmp_path: Path) -> None:
    file = tmp_path / "empty.bin"
    file.write_bytes(b"")
    assert compute_sha256(file) == EMPTY_SHA256


def test_compute_sha256_large_file(tmp_path: Path) -> None:
    # 2 MiB + 17 bytes crosses the 1 MiB internal read-chunk boundary.
    payload = b"x" * (2 * 1024 * 1024 + 17)
    file = tmp_path / "large.bin"
    file.write_bytes(payload)
    assert compute_sha256(file) == hashlib.sha256(payload).hexdigest()


def test_compute_sha256_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ArtifactFileError, match="not a regular file"):
        compute_sha256(tmp_path / "does-not-exist.bin")


def test_compute_sha256_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(ArtifactFileError, match="not a regular file"):
        compute_sha256(tmp_path)


def test_verify_sha256_match(tmp_path: Path) -> None:
    file = tmp_path / "a.bin"
    file.write_bytes(b"abc")
    assert verify_sha256(file, ABC_SHA256) is True


def test_verify_sha256_expected_is_case_insensitive(tmp_path: Path) -> None:
    file = tmp_path / "a.bin"
    file.write_bytes(b"abc")
    assert verify_sha256(file, ABC_SHA256.upper()) is True


def test_verify_sha256_content_mismatch(tmp_path: Path) -> None:
    file = tmp_path / "a.bin"
    file.write_bytes(b"abc")
    assert verify_sha256(file, EMPTY_SHA256) is False


def test_verify_sha256_missing_file_is_false(tmp_path: Path) -> None:
    assert verify_sha256(tmp_path / "nope.bin", ABC_SHA256) is False


def test_verify_sha256_directory_is_false(tmp_path: Path) -> None:
    assert verify_sha256(tmp_path, ABC_SHA256) is False


def test_verify_sha256_malformed_expected_is_false(tmp_path: Path) -> None:
    file = tmp_path / "a.bin"
    file.write_bytes(b"abc")
    assert verify_sha256(file, "not-a-digest") is False
    assert verify_sha256(file, "") is False
    assert verify_sha256(file, "a" * 63) is False
    assert verify_sha256(file, "z" + "a" * 63) is False
