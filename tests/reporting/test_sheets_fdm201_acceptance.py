"""FDM-201 benchmark acceptance of the execution-sheet renderers
(issue #106 acceptance: "renders for every dispatch in a real project
(wave-1 synthesis + downstream characterization packages);
content matches manifest 1:1; PDF or PDF-convertible").

The whole frozen benchmark register is read live
(``benchmarks/fdm201/``) -- never copied -- and every package of both
kinds is dispatched through the **real** adapter / record contracts
(``FilesystemLabAdapter.dispatch`` for experiment packages; the
``JobRecord`` / ``SlurmJobRecord`` ``to_dict`` contract for the compute
jobs, because the computation packages are execution packages, not
durable job records -- see ``sheets_helpers``). Assertions are
1:1 per package: objective, every procedure step id/title/action,
every ``required_return`` token, the goal title, and the PDF-convertible
markers (``<!DOCTYPE html>``, A4 ``@page``), plus byte-identical
determinism.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from sheets_helpers import (
    GOALS_YAML,
    benchmark_job_id,
    dispatch_experiment_packages,
    init_project,
    install_compute_run,
    load_benchmark_packages,
    register_benchmark_goals,
    slurm_record_from_package,
    write_job_record,
)

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.reporting.sheets import (
    build_computation_sheet,
    build_experiment_sheet,
)
from scientific_reproduction.reporting.sheets.html import html_escape

#: All frozen goal ids referenced by the benchmark packages.
EXPERIMENT_GOAL_IDS = [f"GOAL-EXE-{n:02d}" for n in range(10, 71, 10)]
COMPUTATION_GOAL_IDS = ["GOAL-EXE-80", "GOAL-EXE-81", "GOAL-EXE-82"]
ALL_GOAL_IDS = sorted(EXPERIMENT_GOAL_IDS + COMPUTATION_GOAL_IDS)


def _installed_root(tmp_path: Path) -> Path:
    root = init_project(tmp_path)
    register_benchmark_goals(root, ALL_GOAL_IDS)
    return root


def _assert_pdf_convertible(html: str) -> None:
    # PDF-convertible: a well-formed full document with the A4 print
    # stylesheet (browser print path).
    assert html.startswith("<!DOCTYPE html>")
    assert "@page { size: A4" in html
    assert html.strip().endswith("</html>")


# ---------------------------------------------------------------------------
# Experiment sheets: every dispatched wave-1 / downstream package
# ---------------------------------------------------------------------------


def test_sheets_fdm201_experiment_every_package_renders(tmp_path):
    # Every frozen experiment package of the real project dispatches
    # through the real adapter and renders 1:1 (objective, every
    # procedure step, every required-return token, goal title).
    root = _installed_root(tmp_path)
    dispatched = dispatch_experiment_packages(root)
    assert len(dispatched) == 7

    for run_id, manifest in dispatched.items():
        sheet = build_experiment_sheet(root, run_id)
        html = sheet.to_html()
        # Identity: package id, run id, goal title from the registry.
        assert manifest["package_id"] in html
        assert run_id in html
        goal_id = manifest["goal_id"]
        goal_title = next(
            g["title"] for g in _goals_yaml() if g["goal_id"] == goal_id
        )
        assert goal_title in html
        # Objective and every procedure step 1:1 (content reaches the
        # sheet escaped -- the comparison uses the renderer's own escape
        # helper, so an apostrophe in a benchmark action stays exact).
        assert html_escape(manifest["objective"]) in html
        for step in manifest["procedure"]:
            assert html_escape(str(step["id"])) in html
            assert html_escape(str(step["title"])) in html
            assert html_escape(str(step["action"])) in html
        # Every required-return token (issue #85 names) 1:1.
        for token in manifest["required_return"]:
            assert token in html
        _assert_pdf_convertible(html)


def test_sheets_fdm201_experiment_deterministic(tmp_path):
    # Renders are byte-identical across passes and independent of render
    # order (the pure-function guarantee over the whole register).
    root = _installed_root(tmp_path)
    dispatched = dispatch_experiment_packages(root)
    for run_id in dispatched:
        first = build_experiment_sheet(root, run_id).to_html()
        second = build_experiment_sheet(root, run_id).to_html()
        assert first == second
        assert build_experiment_sheet(root, run_id).to_canonical_json() == (
            build_experiment_sheet(root, run_id).to_canonical_json()
        )


def test_sheets_fdm201_experiment_safety_and_prohibited(tmp_path):
    # The safety and prohibited blocks render for the real packages
    # (the visually dominant blocks of the design bar).
    root = _installed_root(tmp_path)
    dispatched = dispatch_experiment_packages(root)
    for run_id, manifest in dispatched.items():
        html = build_experiment_sheet(root, run_id).to_html()
        assert "Safety notes" in html
        for note in manifest["safety_notes"]:
            assert html_escape(str(note))[:80] in html
        assert "Prohibited changes" in html
        for entry in manifest["prohibited_changes"]:
            assert html_escape(str(entry))[:80] in html
        for record in manifest["required_operator_records"]:
            assert html_escape(str(record))[:80] in html


# ---------------------------------------------------------------------------
# Computation sheets: every frozen computation package
# ---------------------------------------------------------------------------


def test_sheets_fdm201_computation_every_package_renders(tmp_path):
    # Every frozen computation package renders a computation sheet with
    # the goal title, the verbatim step invocation, the modules, every
    # declared output artifact with its deterministic artifact id, and
    # the acceptance criteria of the registered goal (the statistical
    # design of the benchmark is not registered -- the sheet says so).
    root = _installed_root(tmp_path)
    packages = load_benchmark_packages("computation")
    assert len(packages) == 3

    for index, package in enumerate(packages):
        install_compute_run(root, package)
        job_id = benchmark_job_id(package)
        write_job_record(root, slurm_record_from_package(package, external_id=423500 + index))
        html = build_computation_sheet(root, job_id).to_html()

        compute = package["compute"]
        # Goal title and objective of the computation.
        goal_title = next(
            g["title"] for g in _goals_yaml() if g["goal_id"] == package["goal_id"]
        )
        assert goal_title in html
        # Verbatim command: the environment contract's step invocation.
        first_step = package["procedure"][0]
        assert f"./run-command.sh {first_step['id']}" in html
        # Modules and every declared output artifact with its id.
        for module in compute["modules"]:
            assert module in html
        for name in compute["output_artifacts"]:
            assert name.rstrip("/") in html
            expected = generate_id("artifact", job_id, name.rstrip("/"))
            assert expected in html
        # Acceptance criteria of the registered goal render; the design
        # is not registered (never guessed).
        assert "Acceptance criteria" in html
        assert "decision mode" in html
        assert "not registered (07-SS9)" in html
        _assert_pdf_convertible(html)


def test_sheets_fdm201_computation_deterministic(tmp_path):
    # Byte-identical renders across passes for every computation sheet.
    root = _installed_root(tmp_path)
    for index, package in enumerate(load_benchmark_packages("computation")):
        install_compute_run(root, package)
        job_id = benchmark_job_id(package)
        write_job_record(root, slurm_record_from_package(package, external_id=423500 + index))
        first = build_computation_sheet(root, job_id).to_html()
        second = build_computation_sheet(root, job_id).to_html()
        assert first == second


def _goals_yaml() -> list[dict]:
    """The frozen goals.yaml entries (read live once per module use)."""
    return yaml.safe_load(GOALS_YAML.read_text(encoding="utf-8"))["goals"]
