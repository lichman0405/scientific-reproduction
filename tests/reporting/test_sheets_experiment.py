"""Experiment execution sheet renderer tests (issue #106).

Every test renders through the real outgoing handoff of the real
``FilesystemLabAdapter`` (schema-gated dispatch) and the real planning
registries. The ``ac``/``layout``/``fidelity`` sections map to the issue
requirements:

* ``layout`` -- the designed print-ready A4 layout: header banner with
  run/dispatch ids, identity block with project/paper identity and goal
  title/track, objective, reagents and instruments tables, numbered
  procedure, critical control variables, the visually dominant
  prohibited-changes block (STRICT emphasis), the safety block, fill-in
  operator record fields, the exact ``required_return`` checklist,
  signatures, the fixed footer and the A4 ``@page`` stylesheet;
* ``fidelity`` -- content matches the manifest 1:1: every procedure
  step key, every ``required_return`` token, every item key of
  reagents/instruments/control variables, and unknown top-level package
  keys in the "Additional package data" section;
* ``determinism`` -- the renderer is a pure function of the state:
  byte-identical double renders, caller-injected ``generated_at``;
* ``errors`` -- ``SheetNotInitializedError`` / ``SheetNotFoundError`` /
  ``SheetCorruptError`` / ``TypeError`` at the public boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from reporting_helpers import (
    GOAL_ID,
    make_goal,
)
from sheets_helpers import (
    dispatch_package,
    init_project,
    make_package,
)

from scientific_reproduction.adapters.lab.filesystem import (
    DISPATCH_RECORD_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    OUTGOING_DIR_NAME,
)
from scientific_reproduction.core.state_backend import SCHEMA_TO_STATE_DIR
from scientific_reproduction.planning.plan import register_goal
from scientific_reproduction.reporting.sheets import (
    EXPERIMENT_SHEET_VERSION,
    ExperimentSheet,
    SheetCorruptError,
    SheetNotFoundError,
    SheetNotInitializedError,
    build_experiment_sheet,
    render_experiment_sheet,
)


def _install_goal(root: Path) -> None:
    register_goal(root, make_goal())


def _dispatch(root: Path, **overrides: object) -> str:
    manifest = make_package(**overrides)
    return dispatch_package(root, manifest)


def _handoff_dir(root: Path, run_id: str) -> Path:
    return (
        root
        / SCHEMA_TO_STATE_DIR["lab-execution-package"]
        / OUTGOING_DIR_NAME
        / run_id
    )


# ---------------------------------------------------------------------------
# layout: the designed print-ready sheet
# ---------------------------------------------------------------------------


def test_sheets_experiment_layout_render_full_design(tmp_path):
    # The full designed layout renders from a real dispatch: banner,
    # identity (project/paper/goal/track), objective, reagents and
    # instruments tables, numbered procedure, critical controls, the
    # red prohibited block with STRICT emphasis, the amber safety block,
    # fill-in operator record fields, the required-return checklist,
    # signatures and the fixed footer.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    html = build_experiment_sheet(root, run_id).to_html()

    assert "Experiment Execution Sheet" in html
    assert f"run {run_id}" in html
    assert f"dispatch {_dispatch_id(root, run_id)}" in html
    # Identity: project id, paper identity, goal title, track.
    assert "Identity" in html
    assert "10.1039/D5TA00771B" in html
    assert "Reproduce the FDM-201 batch-level uptake" in html
    assert "STRICT_REPRODUCTION" in html
    # Objective, reagents, instruments, procedure, controls.
    assert "Objective" in html
    assert "Reagents" in html and "precursor A" in html
    assert "Instruments" in html and "analytical balance" in html
    assert "Step 1" in html and "S1" in html and "Weigh the precursor" in html
    assert "Step 2" in html and "oven --temp 120" in html
    assert "Critical control variables" in html
    assert "reaction temperature" in html and "+/-2 C" in html
    # Prohibited block with the STRICT-track emphasis.
    assert "Prohibited changes" in html
    assert "do not substitute precursor A" in html
    assert "Strict reproduction track" in html
    # Safety block, operator record fields, required-return checklist.
    assert "Safety notes" in html and "wear gloves" in html
    assert "Operator record" in html and "batch weight" in html
    assert "Required returns" in html
    assert "raw-data" in html and "batch-log" in html and "photos" in html
    # Signatures and the A4 print stylesheet / footer.
    assert "Operator signature" in html and "Supervisor signature" in html
    assert "@page { size: A4" in html
    assert "deterministic render" in html


def test_sheets_experiment_layout_no_strict_emphasis_on_recovery_track(tmp_path):
    # A RECOVERY-track package still renders the prohibited block but
    # without the STRICT-only emphasis line.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root, track="RECOVERY")
    html = build_experiment_sheet(root, run_id).to_html()
    assert "Prohibited changes" in html
    assert "do not substitute precursor A" in html
    assert "Strict reproduction track" not in html


def test_sheets_experiment_layout_goal_version_from_manifest(tmp_path):
    # The frozen Goal version the package carries (10-EXPERIMENT-SUBSYSTEM
    # SS3) renders in the identity block -- and, as a known manifest key,
    # not in the "Additional package data" section.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root, goal_version="v1")
    html = build_experiment_sheet(root, run_id).to_html()
    assert '<td class="label">Goal version</td><td class="value">v1</td>' in html
    assert "Additional package data" in html
    assert '<td class="label">goal_version</td>' not in html


def test_sheets_experiment_layout_goal_version_fallback_to_registered_goal(tmp_path):
    # A package written before the goal_version field existed renders the
    # registered goal's version as the goal context (the manifest-first /
    # registry-fallback rule of the track row), never guessed.
    root = init_project(tmp_path)
    _install_goal(root)
    manifest = make_package()
    del manifest["goal_version"]
    run_id = dispatch_package(root, manifest)
    html = build_experiment_sheet(root, run_id).to_html()
    assert '<td class="label">Goal version</td>' in html
    assert "v1-draft" in html


def test_sheets_experiment_layout_goal_version_not_recorded_marker(tmp_path):
    # No goal_version in the manifest and no registered goal -> the
    # "not recorded" marker (never guessed).
    root = init_project(tmp_path)
    manifest = make_package(goal_id="GOAL-UNKNOWN")
    del manifest["goal_version"]
    run_id = dispatch_package(root, manifest)
    html = build_experiment_sheet(root, run_id).to_html()
    assert '<td class="label">Goal version</td><td class="value">not recorded</td>' in html


def test_sheets_experiment_layout_goal_not_registered_marker(tmp_path):
    # A package whose goal id is not registered in the project registry
    # renders the "not registered" marker -- never guessed.
    root = init_project(tmp_path)
    run_id = _dispatch(root, goal_id="GOAL-UNKNOWN")
    html = build_experiment_sheet(root, run_id).to_html()
    assert "GOAL-UNKNOWN" in html
    assert "not registered in the project registry" in html


def test_sheets_experiment_layout_html_escaping(tmp_path):
    # Package content is escaped: script-like content never becomes
    # markup.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(
        root, objective='<script>alert("x")</script> & "quoted"'
    )
    html = build_experiment_sheet(root, run_id).to_html()
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert" in html


def test_sheets_experiment_layout_generated_at_injected_footer(tmp_path):
    # ``generated_at`` is caller-injected and appears in the footer; the
    # default footer says "deterministic render" (no wall clock).
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    stamped = build_experiment_sheet(
        root, run_id, generated_at="2026-08-15T00:00:00Z"
    ).to_html()
    assert "2026-08-15T00:00:00Z" in stamped
    assert "deterministic render" not in stamped


# ---------------------------------------------------------------------------
# fidelity: content matches the manifest 1:1
# ---------------------------------------------------------------------------


def test_sheets_experiment_fidelity_manifest_1to1(tmp_path):
    # Every procedure step key, every required_return token and every
    # item key of the reagent/instrument/control tables render on the
    # sheet; unknown top-level package keys render in the "Additional
    # package data" section.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    sheet = build_experiment_sheet(root, run_id)
    html = sheet.to_html()

    # Required return tokens appear verbatim (issue #85 tokens).
    for token in ("raw-data", "batch-log", "photos"):
        assert token in html
    # Every procedure step id/title/action.
    for step in make_package()["procedure"]:
        assert step["id"] in html
        assert step["title"] in html
        assert step["action"] in html
    # Reagent/instrument/control item keys and values.
    for reagent in make_package()["reagents"]:
        assert reagent["name"] in html and reagent["amount"] in html
    assert make_package()["instruments"][0]["description"] in html
    control = make_package()["critical_control_variables"][0]
    assert control["name"] in html and control["tolerance"] in html
    # Unknown top-level keys render in the additional-data section.
    assert "Additional package data" in html
    assert "parameter_note" in html
    assert "fixture" in html
    # The structured surface carries the manifest itself.
    assert sheet.to_dict()["manifest"] == make_package()
    assert sheet.dispatch.package_id == "PKG-001"
    assert sheet.goal is not None
    assert sheet.goal.goal_id == GOAL_ID


def test_sheets_experiment_fidelity_procedure_unknown_step_keys(tmp_path):
    # A procedure step carrying a key the designed layout does not
    # display elsewhere still renders it (never dropped).
    manifest = make_package()
    manifest["procedure"][0]["custom_step_key"] = {"note": "step extra"}
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = dispatch_package(root, manifest)
    html = build_experiment_sheet(root, run_id).to_html()
    assert "custom_step_key" in html
    assert "step extra" in html


# ---------------------------------------------------------------------------
# determinism: pure function of the registered state
# ---------------------------------------------------------------------------


def test_sheets_experiment_determinism_byte_identical(tmp_path):
    # Same state, same inputs -> byte-identical HTML and canonical JSON.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    first = build_experiment_sheet(root, run_id)
    second = build_experiment_sheet(root, run_id)
    assert first.to_html() == second.to_html()
    assert first.to_canonical_json() == second.to_canonical_json()
    assert render_experiment_sheet(root, run_id) == first.to_html()


def test_sheets_experiment_determinism_render_order_independent(tmp_path):
    # A second dispatch in the same workspace does not change the sheet
    # of the first (rendering reads only its own handoff directory).
    root = init_project(tmp_path)
    _install_goal(root)
    first_run = _dispatch(root, package_id="PKG-001")
    _dispatch(root, package_id="PKG-002", run_id="RUN-002")
    html = build_experiment_sheet(root, first_run).to_html()
    assert "RUN-002" not in html


# ---------------------------------------------------------------------------
# errors: stable error surface
# ---------------------------------------------------------------------------


def test_sheets_experiment_errors_not_initialized(tmp_path):
    # No project state record -> SheetNotInitializedError.
    with pytest.raises(SheetNotInitializedError):
        build_experiment_sheet(tmp_path, "RUN-001")


def test_sheets_experiment_errors_no_dispatch(tmp_path):
    # A run id without an outgoing handoff -> SheetNotFoundError.
    root = init_project(tmp_path)
    with pytest.raises(SheetNotFoundError):
        build_experiment_sheet(root, "RUN-MISSING")


def test_sheets_experiment_errors_corrupt_manifest(tmp_path):
    # A corrupt manifest file -> SheetCorruptError.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    manifest_path = (
        _handoff_dir(root, run_id) / EXECUTION_MANIFEST_FILENAME
    )
    manifest_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SheetCorruptError):
        build_experiment_sheet(root, run_id)


def test_sheets_experiment_errors_corrupt_dispatch_record(tmp_path):
    # A dispatch record violating the DispatchRecord contract ->
    # SheetCorruptError.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    record_path = _handoff_dir(root, run_id) / DISPATCH_RECORD_FILENAME
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    del raw["dispatch_id"]
    record_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SheetCorruptError):
        build_experiment_sheet(root, run_id)


def test_sheets_experiment_errors_type_error_boundaries(tmp_path):
    # TypeError at the public boundaries, stable messages.
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        build_experiment_sheet(123, "RUN-001")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_experiment_sheet(root, "")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_experiment_sheet(root, "RUN-001", generated_at="")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the structured sheet surface
# ---------------------------------------------------------------------------


def test_sheets_experiment_surface_canonical_json_roundtrip(tmp_path):
    # The canonical JSON surface is stable and self-describing.
    root = init_project(tmp_path)
    _install_goal(root)
    run_id = _dispatch(root)
    sheet = build_experiment_sheet(root, run_id)
    data = json.loads(sheet.to_canonical_json())
    assert data["sheet_version"] == EXPERIMENT_SHEET_VERSION
    assert data["kind"] == "experiment-execution-sheet"
    assert data["run_id"] == run_id
    assert data["dispatch_id"] == sheet.dispatch.dispatch_id
    assert data["goal_title"] == "Reproduce the FDM-201 batch-level uptake"
    assert data["goal_track"] == "STRICT_REPRODUCTION"
    assert isinstance(sheet, ExperimentSheet)


def _dispatch_id(root: Path, run_id: str) -> str:
    record_path = _handoff_dir(root, run_id) / DISPATCH_RECORD_FILENAME
    return json.loads(record_path.read_text(encoding="utf-8"))["dispatch_id"]
