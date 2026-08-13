"""Tests for the filesystem-backed artifact manifest registry (DEV-M3-G02).

Covered: register/get/list round-trips (including run linkage and other
optional fields), persistence across registry instances, duplicate and
missing-id errors, schema-validated persistence (both at registration and
on load), per-object JSON files with atomic writes, and rejection of
unsafe ``artifact_id`` values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import manifest_for_file, plain_manifest

from scientific_reproduction.artifacts.exceptions import (
    ArtifactExistsError,
    ArtifactNotFoundError,
    InvalidManifestError,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.core.schema_validation import validate_object


def make_registry(tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(tmp_path / "artifacts")


def test_register_round_trips_all_fields(tmp_path: Path) -> None:
    data_file = tmp_path / "isotherm.csv"
    data_file.write_text("T,P\n300,1.0\n", encoding="utf-8")
    manifest = manifest_for_file(
        "ART-001",
        data_file,
        run_id="RUN-042",
        analysis_id="ANL-007",
        mime_type="text/csv",
        producer="experiment_worker",
        metadata={"format": "csv", "columns": ["T", "P"]},
        created_at="2026-02-03T04:05:06Z",
    )
    registry = make_registry(tmp_path)
    registry.register(manifest)
    assert registry.get("ART-001") == manifest


def test_register_persists_across_registry_instances(tmp_path: Path) -> None:
    data_file = tmp_path / "x.csv"
    data_file.write_text("a,b\n", encoding="utf-8")
    base_dir = tmp_path / "artifacts"
    ArtifactRegistry(base_dir).register(manifest_for_file("ART-001", data_file))
    reloaded = ArtifactRegistry(base_dir)
    assert reloaded.get("ART-001").uri == str(data_file)
    assert [m.artifact_id for m in reloaded.list()] == ["ART-001"]


def test_list_is_empty_for_missing_or_empty_base_dir(tmp_path: Path) -> None:
    assert make_registry(tmp_path).list() == []
    assert ArtifactRegistry(tmp_path / "not-created-yet").list() == []


def test_list_is_sorted_by_artifact_id(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"payload")
    registry = make_registry(tmp_path)
    for artifact_id in ("ART-B", "ART-A", "ART-C"):
        registry.register(manifest_for_file(artifact_id, data_file))
    assert [m.artifact_id for m in registry.list()] == ["ART-A", "ART-B", "ART-C"]


def test_get_missing_artifact_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(ArtifactNotFoundError):
        make_registry(tmp_path).get("ART-NOPE")


def test_register_duplicate_raises_and_keeps_original(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"payload")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    with pytest.raises(ArtifactExistsError):
        registry.register(manifest_for_file("ART-001", data_file))
    # The originally registered manifest is untouched.
    assert registry.get("ART-001").uri == str(data_file)


def test_register_rejects_bad_sha256_pattern(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    with pytest.raises(InvalidManifestError, match="artifact-manifest schema"):
        registry.register(plain_manifest(sha256="not-hex"))
    assert registry.list() == []


def test_register_rejects_negative_size(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    with pytest.raises(InvalidManifestError):
        registry.register(plain_manifest(size_bytes=-1))
    assert registry.list() == []


@pytest.mark.parametrize("artifact_id", ["../evil", "a/b", "a\\b", ".", "..", ""])
def test_register_rejects_unsafe_artifact_ids(tmp_path: Path, artifact_id: str) -> None:
    registry = make_registry(tmp_path)
    with pytest.raises(InvalidManifestError):
        registry.register(plain_manifest(artifact_id=artifact_id))
    assert registry.list() == []


def test_stored_files_are_one_json_per_artifact(tmp_path: Path) -> None:
    data_file = tmp_path / "x.bin"
    data_file.write_bytes(b"payload")
    registry = make_registry(tmp_path)
    registry.register(manifest_for_file("ART-001", data_file))
    registry.register(manifest_for_file("ART-002", data_file, run_id=None))
    stored = sorted(path.name for path in registry.base_dir.glob("*"))
    assert stored == ["ART-001.json", "ART-002.json"]


def test_stored_json_is_schema_valid_and_round_trippable(tmp_path: Path) -> None:
    data_file = tmp_path / "x.csv"
    data_file.write_text("1,2,3\n", encoding="utf-8")
    manifest = manifest_for_file("ART-001", data_file, run_id="RUN-1", metadata={"k": "v"})
    registry = make_registry(tmp_path)
    registry.register(manifest)
    stored = json.loads(
        (registry.base_dir / "ART-001.json").read_text(encoding="utf-8")
    )
    assert validate_object("artifact-manifest", stored) == []
    assert manifest.to_dict() == stored


def test_get_corrupt_stored_json_raises_invalid_manifest(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.base_dir.mkdir(parents=True, exist_ok=True)
    (registry.base_dir / "ART-001.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(InvalidManifestError, match="corrupt"):
        registry.get("ART-001")


def test_get_schema_invalid_stored_json_raises(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.base_dir.mkdir(parents=True, exist_ok=True)
    # Missing required field ``sha256``.
    (registry.base_dir / "ART-001.json").write_text(
        json.dumps(
            {"artifact_id": "ART-001", "uri": "file:///x.bin", "size_bytes": 1,
             "created_at": "2026-01-01T00:00:00Z"},
            indent=2,
        ),
        encoding="utf-8",
    )
    with pytest.raises(InvalidManifestError):
        registry.get("ART-001")


def test_list_fails_loudly_on_corrupt_stored_json(tmp_path: Path) -> None:
    registry = make_registry(tmp_path)
    registry.base_dir.mkdir(parents=True, exist_ok=True)
    (registry.base_dir / "ART-001.json").write_text("garbage", encoding="utf-8")
    with pytest.raises(InvalidManifestError):
        registry.list()
