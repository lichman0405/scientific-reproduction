"""Filesystem-backed artifact manifest registry (DEV-M3-G02, deliverable).

Mirrors the M1 state philosophy (``14-STATE-GIT-ARTIFACTS.md``): one JSON
file per object under a base directory (``<base>/<artifact_id>.json``),
atomic writes via temp file + rename, schema validation on the persistence
gate, deterministic ordering -- and no monolithic state file.

The manifest model is the frozen ``ArtifactManifest`` from ``core.models``,
which mirrors ``schemas/artifact-manifest.schema.yaml`` (the same model
that ``core.schema_validation`` validates against). ``uri`` values are
stored verbatim and never interpreted relative to the repository: an
artifact may live anywhere, including outside the Git tree (AC-03).

Note: ``core.state_backend`` is not used here -- it does not exist yet in
this milestone (DEV-M1-G02 runs concurrently), so the registry carries its
own minimal JSON-per-object store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List

from scientific_reproduction.artifacts.exceptions import (
    ArtifactExistsError,
    ArtifactNotFoundError,
    InvalidManifestError,
)
from scientific_reproduction.artifacts.verification import (
    ArtifactVerificationResult,
    verify_manifest,
)
from scientific_reproduction.core.models import ArtifactManifest
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_and_reject,
)


class ArtifactRegistry:
    """Filesystem-backed registry storing one JSON manifest per artifact.

    The registry is a plain directory of ``<artifact_id>.json`` files.
    Registering validates the manifest against the frozen artifact-manifest
    schema before writing (persistence gate) and writes atomically
    (temp file + ``os.replace``), so a partially written manifest is never
    visible to ``get``/``list``.
    """

    SCHEMA_NAME = "artifact-manifest"
    _SUFFIX = ".json"

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    @property
    def base_dir(self) -> Path:
        """The directory in which manifest JSON files are stored."""
        return self._base_dir

    def register(self, manifest: ArtifactManifest) -> None:
        """Persist ``manifest`` under its ``artifact_id``.

        Raises:
            InvalidManifestError: if the manifest fails the frozen
                artifact-manifest schema or has an unsafe ``artifact_id``.
            ArtifactExistsError: if an artifact with the same
                ``artifact_id`` is already registered.
        """
        self._validate(manifest)
        if self._path_for(manifest.artifact_id).exists():
            raise ArtifactExistsError(
                f"artifact {manifest.artifact_id!r} is already registered"
            )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._path_for(manifest.artifact_id), manifest.to_dict())

    def get(self, artifact_id: str) -> ArtifactManifest:
        """Return the registered manifest for ``artifact_id``.

        Raises:
            ArtifactNotFoundError: if nothing is registered under that id.
            InvalidManifestError: if the stored manifest file is corrupt or
                schema-invalid (fail loudly, never silently skip).
        """
        path = self._path_for(artifact_id)
        if not path.exists():
            raise ArtifactNotFoundError(
                f"no artifact registered with id {artifact_id!r}"
            )
        data = self._read_json(path)
        return self._from_dict(data, source=str(path))

    def list(self) -> List[ArtifactManifest]:
        """Return all registered manifests, ordered by ``artifact_id``.

        Uses ``typing.List`` (not the builtin) in the annotation because the
        method name shadows the builtin ``list`` inside the class scope.
        """
        manifests: list[ArtifactManifest] = []
        for path in sorted(self._base_dir.glob(f"*{self._SUFFIX}")):
            if not path.is_file():
                continue
            manifests.append(self._from_dict(self._read_json(path), source=str(path)))
        return sorted(manifests, key=lambda m: m.artifact_id)

    def verify(self, artifact_id: str) -> ArtifactVerificationResult:
        """Verify the artifact registered under ``artifact_id``.

        Raises:
            ArtifactNotFoundError: if nothing is registered under that id.
        """
        return verify_manifest(self.get(artifact_id))

    def verify_all(self) -> List[ArtifactVerificationResult]:
        """Verify every registered artifact, ordered by ``artifact_id``."""
        return [verify_manifest(manifest) for manifest in self.list()]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for(self, artifact_id: str) -> Path:
        return self._base_dir / f"{artifact_id}{self._SUFFIX}"

    def _validate(self, manifest: ArtifactManifest) -> None:
        artifact_id = manifest.artifact_id
        if not artifact_id or artifact_id in {".", ".."}:
            raise InvalidManifestError(f"invalid artifact_id {artifact_id!r}")
        # Backslash is rejected on EVERY platform, not only Windows: it is a
        # path separator on Windows (and so a traversal vector there), while
        # ``Path(artifact_id).name`` treats it as a plain filename character
        # on POSIX. The per-object store is shared between OSes (e.g. a
        # cloned state dir), so id validation must behave identically.
        if Path(artifact_id).name != artifact_id or "\\" in artifact_id:
            raise InvalidManifestError(
                f"artifact_id {artifact_id!r} must not contain path separators"
            )
        try:
            validate_and_reject(self.SCHEMA_NAME, manifest.to_dict())
        except SchemaValidationError as exc:
            raise InvalidManifestError(
                f"manifest {artifact_id!r} fails the artifact-manifest "
                f"schema: {'; '.join(exc.errors)}"
            ) from exc

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        tmp_path = path.with_name(f"{path.name}.tmp")
        tmp_path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp_path, path)

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise InvalidManifestError(
                f"stored manifest {path} is corrupt: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise InvalidManifestError(f"stored manifest {path} is not a mapping")
        return data

    def _from_dict(self, data: dict[str, Any], *, source: str) -> ArtifactManifest:
        try:
            validate_and_reject(self.SCHEMA_NAME, data)
        except SchemaValidationError as exc:
            raise InvalidManifestError(
                f"stored manifest {source} fails the artifact-manifest "
                f"schema: {'; '.join(exc.errors)}"
            ) from exc
        try:
            return ArtifactManifest.from_dict(data)
        except (TypeError, ValueError) as exc:
            raise InvalidManifestError(
                f"stored manifest {source} is malformed: {exc}"
            ) from exc
