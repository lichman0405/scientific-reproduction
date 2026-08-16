"""Computation execution sheet renderer tests (issue #106).

Every test renders from a **durable job record** written through the
real record contracts (``JobRecord`` / ``SSHJobRecord`` /
``SlurmJobRecord`` ``to_dict``) and the real planning registries. The
``layout``/``context``/``determinism``/``errors`` sections map to the
issue requirements:

* ``layout`` -- identity and job state, the verbatim command (argv
  shell-joined) and working directory, resource requests (Slurm
  modules / environment overrides; honest "no resource requests"
  statements for the local/ssh backends), required outputs with the
  deterministic artifact-id rule, and the convergence/validation
  criteria section (07 SS7/SS9);
* ``context`` -- the run -> goal -> acceptance -> statistical-design
  chain renders when registered and "not registered" markers when an
  optional link is absent (never guessed);
* ``determinism`` -- byte-identical double renders;
* ``errors`` -- ``SheetNotInitializedError`` / ``SheetNotFoundError`` /
  ``SheetCorruptError`` / ``TypeError`` at the public boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from reporting_helpers import (
    ACCEPTANCE_ID,
    GOAL_ID,
    make_acceptance,
    make_goal,
)
from sheets_helpers import (
    COMPLETED_AT,
    CREATED_AT,
    make_local_job_record,
    make_slurm_job_record,
    make_ssh_job_record,
    make_statistical_design,
    write_job_record,
)

from scientific_reproduction.adapters.compute.local import (
    JOBS_STATE_DIR,
    JobState,
)
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.plan import (
    register_acceptance,
    register_goal,
    register_statistical_design,
)
from scientific_reproduction.reporting.sheets import (
    COMPUTATION_SHEET_VERSION,
    ComputationSheet,
    SheetCorruptError,
    SheetNotFoundError,
    SheetNotInitializedError,
    build_computation_sheet,
    render_computation_sheet,
)

LOCAL_RUN_ID = "sr_run_" + "b" * 32
LOCAL_JOB_ID = "sr_job_" + "a" * 32


def _install_goal_chain(root: Path) -> None:
    """Register the full goal context chain of a job (goal, acceptance
    with its statistical-design ref, and the design itself) through the
    real registries."""
    register_goal(root, make_goal())
    register_acceptance(root, make_acceptance(statistical_design_ref="DESIGN-001"))
    register_statistical_design(root, make_statistical_design())


def _install_run(root: Path, run_id: str = LOCAL_RUN_ID) -> None:
    run = make_run_for_job(run_id)
    FilesystemStateBackend(root).write("run", run_id, run.to_dict())


def make_run_for_job(run_id: str):
    from reporting_helpers import make_run

    return make_run(run_id=run_id, goal_id=GOAL_ID)


# ---------------------------------------------------------------------------
# layout: the designed print-ready sheet
# ---------------------------------------------------------------------------


def test_sheets_computation_layout_local_job(tmp_path):
    # A local-backend job renders identity/job state, the verbatim
    # command (shell-joined argv), the working directory, the outputs
    # with deterministic artifact ids and the validation section.
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    html = build_computation_sheet(root, LOCAL_JOB_ID).to_html()

    assert "Computation Execution Sheet" in html
    assert f"job {LOCAL_JOB_ID}" in html and f"run {LOCAL_RUN_ID}" in html
    assert "local" in html
    assert "completed" in html
    assert "python -c" in html and "print(1)" in html
    assert "/scratch/fdm201" in html
    assert "result.dat" in html
    # Deterministic artifact naming (the adapter's own id rule).
    expected_id = generate_id("artifact", LOCAL_JOB_ID, "result.dat")
    assert expected_id in html
    assert "Artifact id rule" in html
    # Local backend persists no resource requests -- stated honestly.
    assert "No resource requests are persisted by the local backend" in html
    # Validation section with the not-registered markers.
    assert "Convergence and validation criteria" in html
    assert "Acceptance criteria" in html and "not registered" in html
    assert "Statistical design" in html and "not registered" in html


def test_sheets_computation_layout_ssh_job(tmp_path):
    # An ssh-backend job renders its remote identity fields and the
    # honest no-resource-requests statement.
    root = init_project(tmp_path)
    job_id = "sr_job_" + "c" * 32
    run_id = "sr_run_" + "d" * 32
    record = make_ssh_job_record(job_id=job_id, run_id=run_id)
    write_job_record(root, record)
    html = build_computation_sheet(root, job_id).to_html()

    assert "ssh" in html
    assert "Remote pid" in html and "4242" in html
    assert "python simulate.py" in html
    assert "No resource requests are persisted by the ssh backend" in html
    assert "not collected yet" in html


def test_sheets_computation_layout_slurm_job(tmp_path):
    # A slurm job renders the modules table, the environment overrides,
    # the external id and the scheduler state (the durable Slurm/SSH
    # parameters of the issue).
    root = init_project(tmp_path)
    job_id = "sr_job_" + "e" * 32
    run_id = "sr_run_" + "f" * 32
    record = make_slurm_job_record(job_id=job_id, run_id=run_id)
    write_job_record(root, record)
    html = build_computation_sheet(root, job_id).to_html()

    assert "slurm_ssh" in html
    assert "Modules" in html and "simulation-code" in html
    assert "structure-toolkit" in html
    assert "Environment overrides" in html
    assert "OMP_NUM_THREADS" in html and "4" in html
    assert "External id" in html and "423554" in html
    assert "Scheduler state" in html and "COMPLETED" in html
    assert "./run-command.sh cmp01-s01" in html
    # The declared output artifacts render with their deterministic ids.
    assert "frozen-model" in html and "checklist" in html


def test_sheets_computation_layout_failed_job_shows_error(tmp_path):
    # A failed job's error and failure class render (never hidden).
    root = init_project(tmp_path)
    job_id = "sr_job_" + "a" * 32
    record = make_local_job_record(
        job_id=job_id, state=JobState.FAILED, exit_code=1
    )
    write_job_record(root, record)
    html = build_computation_sheet(root, job_id).to_html()
    assert "failed" in html
    assert "Exit code" in html and "1" in html


# ---------------------------------------------------------------------------
# context: the run -> goal -> acceptance -> design chain
# ---------------------------------------------------------------------------


def test_sheets_computation_context_full_chain_registered(tmp_path):
    # With the run record and the full goal family registered, the sheet
    # renders the goal title, the acceptance criteria table (decision
    # mode) and the frozen statistical design (07 SS9) values.
    root = init_project(tmp_path)
    _install_goal_chain(root)
    _install_run(root)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    sheet = build_computation_sheet(root, LOCAL_JOB_ID)
    html = sheet.to_html()

    assert "Reproduce the FDM-201 batch-level uptake" in html
    assert "equivalence" in html
    assert "batch_level_uptake" in html
    assert "0.05" in html
    assert "Primary method" in html and "two_one_sided_t_test" in html
    assert "Metrics" in html and "batch_level_uptake" in html
    assert "Margin basis" in html and "domain_threshold" in html
    assert "0.95" in html
    # The structured surface carries the resolved context records.
    assert sheet.goal is not None and sheet.goal.goal_id == GOAL_ID
    assert sheet.acceptance is not None
    assert sheet.acceptance.acceptance_id == ACCEPTANCE_ID
    assert sheet.statistical_design is not None


def test_sheets_computation_context_partial_chain_markers(tmp_path):
    # Only the run record registered: goal/acceptance/design render
    # "not registered" markers; the sheet never guesses.
    root = init_project(tmp_path)
    _install_run(root)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    html = build_computation_sheet(root, LOCAL_JOB_ID).to_html()
    assert "not registered in the project registry" in html
    assert "not registered" in html


def test_sheets_computation_context_no_run_record_marker(tmp_path):
    # No run record registered: the goal chain is absent and marked.
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    html = build_computation_sheet(root, LOCAL_JOB_ID).to_html()
    assert "not registered" in html
    assert "Inputs" in html


# ---------------------------------------------------------------------------
# determinism: pure function of the registered state
# ---------------------------------------------------------------------------


def test_sheets_computation_determinism_byte_identical(tmp_path):
    # Same state, same inputs -> byte-identical HTML and canonical JSON.
    root = init_project(tmp_path)
    _install_goal_chain(root)
    _install_run(root)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    first = build_computation_sheet(root, LOCAL_JOB_ID)
    second = build_computation_sheet(root, LOCAL_JOB_ID)
    assert first.to_html() == second.to_html()
    assert first.to_canonical_json() == second.to_canonical_json()
    assert render_computation_sheet(root, LOCAL_JOB_ID) == first.to_html()


def test_sheets_computation_determinism_state_dir_explicit(tmp_path):
    # An explicit state_dir renders identically to the default (the
    # default state_dir is the workspace root).
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    default = build_computation_sheet(root, LOCAL_JOB_ID).to_html()
    explicit = build_computation_sheet(
        root, LOCAL_JOB_ID, state_dir=root
    ).to_html()
    assert default == explicit


def test_sheets_computation_layout_injected_generated_at(tmp_path):
    # ``generated_at`` is caller-injected and appears in the footer.
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    html = build_computation_sheet(
        root, LOCAL_JOB_ID, generated_at="2026-08-15T00:00:00Z"
    ).to_html()
    assert "2026-08-15T00:00:00Z" in html


# ---------------------------------------------------------------------------
# language packs: explicit language input (issue #122)
# ---------------------------------------------------------------------------


def test_sheets_computation_language_zh_renders_chinese(tmp_path):
    # The zh pack renders the template strings in Chinese; the job record
    # content (command, outputs, module names) is data and stays verbatim.
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    html = build_computation_sheet(root, LOCAL_JOB_ID, language="zh").to_html()

    assert "计算执行单" in html
    assert "操作员执行单" in html
    assert f"作业 {LOCAL_JOB_ID}" in html
    assert "身份与作业状态" in html and "后端" in html and "状态" in html
    assert "输入" in html and "命令" in html
    assert "必需输出与工件命名" in html
    assert "收敛与验证标准" in html and "验收标准" in html
    assert "统计设计" in html
    assert 'lang="zh"' in html
    # Job record content is never translated.
    assert "python -c" in html and "result.dat" in html


def test_sheets_computation_language_default_is_english_byte_identical(tmp_path):
    # ``language="en"`` is the explicit default and renders byte-identical
    # to the implicit default (the pre-pack renderer).
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    default = build_computation_sheet(root, LOCAL_JOB_ID)
    explicit = build_computation_sheet(root, LOCAL_JOB_ID, language="en")
    assert default.to_html() == explicit.to_html()
    assert default.to_canonical_json() == explicit.to_canonical_json()
    assert render_computation_sheet(
        root, LOCAL_JOB_ID, language="en"
    ) == default.to_html()


def test_sheets_computation_language_unknown_raises_stable_error(tmp_path):
    # Unknown languages and non-string inputs raise the stable boundary
    # errors of ``resolve_pack`` (never silently fall back).
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    with pytest.raises(ValueError, match="available languages: en, zh"):
        build_computation_sheet(root, LOCAL_JOB_ID, language="fr")
    with pytest.raises(TypeError, match="language must be a non-empty string"):
        build_computation_sheet(root, LOCAL_JOB_ID, language=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# errors: stable error surface
# ---------------------------------------------------------------------------


def test_sheets_computation_errors_not_initialized(tmp_path):
    with pytest.raises(SheetNotInitializedError):
        build_computation_sheet(tmp_path, LOCAL_JOB_ID)


def test_sheets_computation_errors_no_job_record(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(SheetNotFoundError):
        build_computation_sheet(root, LOCAL_JOB_ID)


def test_sheets_computation_errors_corrupt_json(tmp_path):
    root = init_project(tmp_path)
    path = root / JOBS_STATE_DIR / f"{LOCAL_JOB_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SheetCorruptError):
        build_computation_sheet(root, LOCAL_JOB_ID)


def test_sheets_computation_errors_unknown_backend(tmp_path):
    root = init_project(tmp_path)
    path = root / JOBS_STATE_DIR / f"{LOCAL_JOB_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "record_version": "1.0",
                "backend": "quantum",
                "job_id": LOCAL_JOB_ID,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SheetCorruptError, match="not a supported compute backend"):
        build_computation_sheet(root, LOCAL_JOB_ID)


def test_sheets_computation_errors_invalid_record(tmp_path):
    # A record violating its real contract (missing required field) is a
    # loud SheetCorruptError.
    root = init_project(tmp_path)
    path = root / JOBS_STATE_DIR / f"{LOCAL_JOB_ID}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"record_version": "1.0", "backend": "local", "job_id": LOCAL_JOB_ID}
        ),
        encoding="utf-8",
    )
    with pytest.raises(SheetCorruptError):
        build_computation_sheet(root, LOCAL_JOB_ID)


def test_sheets_computation_errors_type_error_boundaries(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(TypeError):
        build_computation_sheet(123, LOCAL_JOB_ID)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_computation_sheet(root, "")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        build_computation_sheet(root, LOCAL_JOB_ID, state_dir=42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the structured sheet surface
# ---------------------------------------------------------------------------


def test_sheets_computation_surface_canonical_json(tmp_path):
    root = init_project(tmp_path)
    record = make_local_job_record(job_id=LOCAL_JOB_ID, run_id=LOCAL_RUN_ID)
    write_job_record(root, record)
    sheet = build_computation_sheet(root, LOCAL_JOB_ID)
    data = json.loads(sheet.to_canonical_json())
    assert data["sheet_version"] == COMPUTATION_SHEET_VERSION
    assert data["kind"] == "computation-execution-sheet"
    assert data["job_id"] == LOCAL_JOB_ID
    assert data["run_id"] == LOCAL_RUN_ID
    assert data["backend"] == "local"
    assert data["state"] == "completed"
    assert data["outputs"] == ["result.dat"]
    assert data["artifact_ids"] == [generate_id("artifact", LOCAL_JOB_ID, "result.dat")]
    assert isinstance(sheet, ComputationSheet)
    # Timestamps of the durable record surface in the identity table.
    html = sheet.to_html()
    assert CREATED_AT in html and COMPLETED_AT in html


def test_sheets_computation_surface_slurm_canonical_json(tmp_path):
    root = init_project(tmp_path)
    job_id = "sr_job_" + "e" * 32
    run_id = "sr_run_" + "f" * 32
    record = make_slurm_job_record(job_id=job_id, run_id=run_id)
    write_job_record(root, record)
    data = json.loads(build_computation_sheet(root, job_id).to_canonical_json())
    assert data["backend"] == "slurm_ssh"
    assert data["external_id"] == 423554
    assert data["scheduler_state"] == "COMPLETED"


def init_project(root: Path) -> Path:
    from sheets_helpers import init_project as _init

    return _init(root)
