"""Shared test helpers for the artifacts subsystem (DEV-M3-G02).

``manifest_for_file`` builds a manifest that truthfully describes an
existing file (real SHA-256 and byte size), which is what the acceptance
criteria need: register -> verify must come back OK. ``plain_manifest``
builds a manifest with caller-chosen values for corrupt/malformed cases.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.models import ArtifactManifest


def manifest_for_file(
    artifact_id: str,
    path: Path,
    *,
    uri: str | None = None,
    run_id: str | None = "RUN-001",
    analysis_id: str | None = None,
    mime_type: str | None = None,
    producer: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> ArtifactManifest:
    """Build a truthful manifest for ``path`` (real checksum and size)."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=uri if uri is not None else str(path),
        sha256=compute_sha256(path),
        size_bytes=path.stat().st_size,
        created_at=created_at,
        run_id=run_id,
        analysis_id=analysis_id,
        mime_type=mime_type,
        producer=producer,
        metadata=metadata or {},
    )


def plain_manifest(
    artifact_id: str = "ART-001",
    *,
    uri: str = "file:///data/isotherm.csv",
    sha256: str = "a" * 64,
    size_bytes: int = 1024,
    run_id: str | None = "RUN-001",
    analysis_id: str | None = None,
    mime_type: str | None = None,
    producer: str | None = None,
    metadata: dict[str, Any] | None = None,
    created_at: str = "2026-01-01T00:00:00Z",
) -> ArtifactManifest:
    """Build a manifest with caller-chosen values (no file access)."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=uri,
        sha256=sha256,
        size_bytes=size_bytes,
        created_at=created_at,
        run_id=run_id,
        analysis_id=analysis_id,
        mime_type=mime_type,
        producer=producer,
        metadata=metadata or {},
    )
