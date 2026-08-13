"""Deterministic fixtures for the filesystem LabAdapter test suite (DEV-M7-G01).

Every fixture is a fixed in-memory mapping written only into the injected
``tmp_path`` handoff roots -- no network, no wall clock, no access to any
path outside the test's own directories. The execution package mappings
are minimal schema-valid ``lab-execution-package`` objects (the real
schema gate runs at dispatch); the result manifests follow the adapter's
result-manifest vocabulary (``adapters/lab/manifest.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from scientific_reproduction.adapters.lab.manifest import RESULT_MANIFEST_VERSION

RUN_ID = "sr_run_reference_flow_0001"
PACKAGE_ID = "sr_package_reference_flow_0001"
PROJECT_ID = "sr_project_reference_flow_0001"
GOAL_ID = "sr_goal_reference_flow_0001"

#: A foreign Run id used by the AC-02 mismatch tests (never dispatched).
OTHER_RUN_ID = "sr_run_foreign_00000000000000000000000000000001"

#: A foreign package id used by the AC-02 mismatch tests.
OTHER_PACKAGE_ID = "sr_package_foreign_0000000000000000000000000000001"

DEFAULT_RETURNS = ("raw-data.csv",)
DEFAULT_FILES = ("raw-data.csv",)


def make_package(
    *,
    run_id: str = RUN_ID,
    package_id: str = PACKAGE_ID,
    project_id: str = PROJECT_ID,
    goal_id: str = GOAL_ID,
    objective: str = "synthesize the target compound per the frozen protocol",
    required_return: tuple[str, ...] = DEFAULT_RETURNS,
) -> dict[str, Any]:
    """A minimal schema-valid ``lab-execution-package`` mapping."""
    return {
        "package_id": package_id,
        "project_id": project_id,
        "goal_id": goal_id,
        "run_id": run_id,
        "objective": objective,
        "procedure": [{"step": 1, "action": "weigh the precursor"}],
        "required_return": list(required_return),
    }


def make_result_manifest(
    *,
    run_id: str = RUN_ID,
    package_id: str = PACKAGE_ID,
    project_id: str = PROJECT_ID,
    goal_id: str = GOAL_ID,
    files: tuple[str, ...] = DEFAULT_FILES,
    notes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A returned Result Package manifest for the reference flow."""
    data: dict[str, Any] = {
        "manifest_version": RESULT_MANIFEST_VERSION,
        "package_id": package_id,
        "project_id": project_id,
        "goal_id": goal_id,
        "run_id": run_id,
        "files": list(files),
    }
    data["notes"] = list(notes)
    return data


def write_result_package(
    base: Path,
    run_id: str,
    manifest: Mapping[str, Any],
    files: Mapping[str, str | bytes] | None = None,
    *,
    incoming_dir_name: str = "incoming",
) -> Path:
    """Write a returned Result Package into ``base/<incoming>/<run_id>/``.

    The result manifest is written as ``result-manifest.json`` (canonical
    JSON); declared data files are written as given. Returns the incoming
    run directory.
    """
    incoming = base / incoming_dir_name / run_id
    incoming.mkdir(parents=True, exist_ok=True)
    canonical = json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n"
    (incoming / "result-manifest.json").write_text(canonical, encoding="utf-8")
    for name, content in (files or {}).items():
        data = content.encode("utf-8") if isinstance(content, str) else content
        (incoming / name).write_bytes(data)
    return incoming


def read_json(path: Path) -> dict[str, Any]:
    """Read one handoff JSON record for assertions."""
    return json.loads(path.read_text(encoding="utf-8"))
