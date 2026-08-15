"""Shared test helpers for the execution-sheet tests (issue #106).

The workspace installers use the **real** registration and dispatch
APIs -- ``initialize_project``, ``register_goal`` /
``register_acceptance`` (goal-contract family), the run store
(``FilesystemStateBackend`` over the workspace root) and the real
``FilesystemLabAdapter.dispatch`` (schema-gated outgoing handoff) --
with fixed identities/timestamps, so every test exercises the
deterministic path. The FDM-201 benchmark register
(``benchmarks/fdm201/``) is read live, never copied; the computation
fixtures derive durable job records **through the real record
contracts** (``JobRecord`` / ``SSHJobRecord`` / ``SlurmJobRecord``
``to_dict``/``from_dict``), because the benchmark packages are
execution packages, not durable job records. Nothing here is mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from reporting_helpers import (
    GOAL_ID,
    make_run,
)
from reporting_helpers import (
    init_project as init_project_once,
)

from scientific_reproduction.adapters.compute.local import (
    JOBS_STATE_DIR,
    JobRecord,
    JobState,
)
from scientific_reproduction.adapters.compute.slurm_ssh import SlurmJobRecord
from scientific_reproduction.adapters.compute.ssh import SSHJobRecord
from scientific_reproduction.adapters.lab.filesystem import FilesystemLabAdapter
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    Confidence,
    DecisionMode,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    MarginBasis,
    StatisticalDesign,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import INITIAL_PLAN_VERSION
from scientific_reproduction.planning.plan import (
    register_acceptance,
    register_goal,
)

#: Fixed lifecycle timestamps of every durable job record fixture
#: (deterministic path: no wall clock anywhere).
CREATED_AT = "2026-01-02T00:00:00Z"
SUBMITTED_AT = "2026-01-02T01:00:00Z"
COMPLETED_AT = "2026-01-02T03:00:00Z"
DISPATCHED_AT = "2026-01-02T00:00:00Z"

#: The frozen FDM-201 benchmark register (read live, never copied).
BENCHMARK_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "fdm201"
EXPERIMENT_PACKAGES_DIR = (
    BENCHMARK_ROOT / "execution_packages" / "experiment"
)
COMPUTATION_PACKAGES_DIR = (
    BENCHMARK_ROOT / "execution_packages" / "computation"
)
GOALS_YAML = BENCHMARK_ROOT / "goals" / "goals.yaml"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``.

    ``reporting_helpers.init_project`` already injects the fixed
    DOI/timestamp/identity of the deterministic path.
    """
    init_project_once(root)
    return root


# ---------------------------------------------------------------------------
# Package fixtures (schema-valid lab-execution-package manifests)
# ---------------------------------------------------------------------------


def make_package(
    *,
    run_id: str = "RUN-001",
    goal_id: str = "GOAL-001",
    package_id: str = "PKG-001",
    track: str | None = "STRICT_REPRODUCTION",
    **overrides: object,
) -> dict[str, Any]:
    """Build a schema-valid lab-execution-package manifest with compact
    defaults covering every rendered section (reagents, instruments,
    critical control variables, prohibited changes, safety notes,
    operator records, required returns, an extra package key).
    ``overrides`` replace any default key (e.g. ``objective``)."""
    package = {
        "package_id": package_id,
        "project_id": "PROJ-001",
        "goal_id": goal_id,
        "run_id": run_id,
        "objective": "Synthesize the FDM-201 batch and record the run",
        "procedure": [
            {
                "step": 1,
                "id": "S1",
                "title": "Weigh the precursor",
                "action": "Weigh 5 mg of precursor A on the analytical"
                " balance",
                "inputs": ["precursor A", "vial"],
                "outputs": ["weighed vial"],
                "trace_refs": ["INV-0601"],
            },
            {
                "step": 2,
                "id": "S2",
                "title": "Synthesize",
                "action": "React at 120 C for 24 h",
                "inputs": ["weighed vial"],
                "outputs": ["raw batch"],
                "command": "oven --temp 120 --hours 24",
            },
        ],
        "required_return": ["raw-data", "batch-log", "photos"],
        "track": track,
        "reagents": [
            {"name": "precursor A", "amount": "5 mg", "role": "reactant"},
            {"name": "solvent B", "amount": "10 mL", "role": "medium"},
        ],
        "instruments": [
            {"name": "analytical balance", "description": "0.1 mg"},
        ],
        "critical_control_variables": [
            {"name": "reaction temperature", "value": "120 C",
             "tolerance": "+/-2 C"},
        ],
        "prohibited_changes": [
            "do not substitute precursor A",
            "do not change the reaction time",
        ],
        "safety_notes": ["wear gloves", "run in a fume hood"],
        "required_operator_records": ["batch weight", "operator initials"],
        "parameter_note": {"kind": "fixture", "source": "tests"},
    }
    package.update(overrides)
    return package


# ---------------------------------------------------------------------------
# Benchmark register helpers (read live, never copied)
# ---------------------------------------------------------------------------


def load_benchmark_packages(kind: str) -> list[dict[str, Any]]:
    """Load the frozen benchmark execution packages of one kind
    (``"experiment"`` or ``"computation"``), in sorted file order."""
    directory = (
        EXPERIMENT_PACKAGES_DIR if kind == "experiment" else COMPUTATION_PACKAGES_DIR
    )
    packages: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        with path.open(encoding="utf-8") as handle:
            packages.append(yaml.safe_load(handle))
    return packages


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def frozen_goal(goal_id: str) -> dict:
    """The frozen goals.yaml entry of one goal (read live)."""
    return next(
        g for g in _load_yaml(GOALS_YAML)["goals"] if g["goal_id"] == goal_id
    )


def make_goal(frozen: dict) -> GoalContract:
    """Rebuild one frozen goal contract through the registry
    ``GoalContract`` shape (the reload pattern of the benchmark
    suites)."""
    return GoalContract(
        goal_id=frozen["goal_id"],
        title=frozen["title"],
        unit_process_type=frozen["unit_process_type"],
        track=GoalTrack.STRICT_REPRODUCTION,
        objective=frozen["objective"],
        requirement_ids=list(frozen["requirement_ids"]),
        dependencies=[
            GoalDependency(
                goal_id=dep["goal_id"],
                type=dep["type"],
                execution_gate=bool(dep.get("execution_gate", False)),
                acceptance_gate=bool(dep.get("acceptance_gate", False)),
            )
            for dep in frozen.get("dependencies", [])
        ],
        acceptance=GoalAcceptance(
            criteria_ref=f"ACC-{frozen['goal_id']}", frozen=False
        ),
        analysis_protocol_ref=frozen["analysis_protocol_ref"],
        replication=GoalReplication(
            independent_required=frozen["replication"]["independent_required"],
            planned_n_policy=frozen["replication"]["planned_n_policy"],
            minimum_n=frozen["replication"].get("minimum_n"),
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref=frozen["closure_contract_ref"],
        resource_ids=list(frozen.get("resource_ids", [])),
        assumption_ids=list(frozen.get("assumption_ids", [])),
    )


def make_acceptance(frozen: dict) -> AcceptanceCriteria:
    """The reloaded acceptance record of one frozen goal (the frozen
    inline AC text preserved verbatim as criteria metrics)."""
    return AcceptanceCriteria(
        acceptance_id=f"ACC-{frozen['goal_id']}",
        goal_id=frozen["goal_id"],
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[
            {"metric": text, "rule": "frozen_benchmark_acceptance"}
            for text in frozen["acceptance_criteria"]
        ],
        target={"metric": "frozen_benchmark_acceptance", "source": "goals.yaml"},
        confidence=Confidence.LOW,
    )


def register_benchmark_goals(root: Path, goal_ids: list[str]) -> None:
    """Register the frozen goal contracts (and their acceptance records)
    of ``goal_ids`` through the real registry APIs."""
    for goal_id in sorted(set(goal_ids)):
        frozen = frozen_goal(goal_id)
        register_goal(root, make_goal(frozen))
        register_acceptance(root, make_acceptance(frozen))


def make_statistical_design() -> StatisticalDesign:
    """A schema-valid statistical design for the one-goal fixture
    (``GOAL-001`` / ``ACC-001``), frozen-before-data-generation values
    (07 SS9) with compact defaults."""
    return StatisticalDesign(
        design_id="DESIGN-001",
        goal_id=GOAL_ID,
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        metrics=["batch_level_uptake"],
        replication=GoalReplication(
            independent_required=True, planned_n_policy="n=1 per condition"
        ),
        primary_method="two_one_sided_t_test",
        margin={"relative": 0.05},
        margin_basis=MarginBasis.DOMAIN_THRESHOLD,
        alpha=0.05,
        confidence_level=0.95,
        preprocessing_exclusion_rules=["drop incomplete replicates"],
        outlier_rules=["grubbs"],
        failed_run_handling="re-run once, else record deviation",
    )


# ---------------------------------------------------------------------------
# Experiment handoff installer (the real FilesystemLabAdapter)
# ---------------------------------------------------------------------------


def dispatch_package(root: Path, manifest: dict[str, Any]) -> str:
    """Dispatch one package through the real FilesystemLabAdapter (the
    real schema gate on the way out); return the run id."""
    adapter = FilesystemLabAdapter(root / "lab")
    record = adapter.dispatch(manifest, dispatched_at=DISPATCHED_AT)
    return record.run_id


def dispatch_experiment_packages(
    root: Path,
) -> dict[str, dict[str, Any]]:
    """Dispatch every frozen FDM-201 experiment package through the real
    adapter; return ``{run_id: manifest}`` in sorted run-id order."""
    dispatched: dict[str, dict[str, Any]] = {}
    for manifest in load_benchmark_packages("experiment"):
        run_id = dispatch_package(root, manifest)
        dispatched[run_id] = manifest
    return dispatched


# ---------------------------------------------------------------------------
# Computation job-record installers (the real record contracts)
# ---------------------------------------------------------------------------


def write_job_record(root: Path, record: object) -> None:
    """Persist one durable job record at ``<root>/jobs/<job_id>.json``
    through the record's own ``to_dict`` contract, in canonical JSON
    (sorted keys, 2-space indent, trailing newline -- the adapter's
    serialization)."""
    data = record.to_dict()  # type: ignore[attr-defined]
    path = root / JOBS_STATE_DIR / f"{data['job_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def make_local_job_record(
    *,
    job_id: str = "sr_job_" + "a" * 32,
    run_id: str = "sr_run_" + "b" * 32,
    state: JobState = JobState.COMPLETED,
    command: tuple[str, ...] = ("python", "-c", "print(1)"),
    outputs: tuple[str, ...] = ("result.dat",),
    exit_code: int | None = 0,
) -> JobRecord:
    """A durable local JobRecord with fixed lifecycle timestamps."""
    return JobRecord(
        job_id=job_id,
        run_id=run_id,
        state=state,
        command=command,
        working_directory="/scratch/fdm201",
        outputs=outputs,
        created_at=CREATED_AT,
        submitted_at=SUBMITTED_AT,
        exit_code=exit_code,
        completed_at=COMPLETED_AT,
        artifact_ids=tuple(
            generate_id("artifact", job_id, name) for name in outputs
        ),
    )


def make_ssh_job_record(
    *,
    job_id: str = "sr_job_" + "c" * 32,
    run_id: str = "sr_run_" + "d" * 32,
) -> SSHJobRecord:
    """A durable SSH job record with fixed lifecycle timestamps."""
    return SSHJobRecord(
        job_id=job_id,
        run_id=run_id,
        state=JobState.COMPLETED,
        command=("python", "simulate.py"),
        working_directory="/home/fdm201",
        outputs=("output.h5",),
        created_at=CREATED_AT,
        submitted_at=SUBMITTED_AT,
        remote_pid=4242,
        exit_code=0,
        completed_at=COMPLETED_AT,
    )


def make_slurm_job_record(
    *,
    job_id: str = "sr_job_" + "e" * 32,
    run_id: str = "sr_run_" + "f" * 32,
    modules: tuple[str, ...] = ("simulation-code", "structure-toolkit"),
    environment: tuple[tuple[str, str], ...] = (("OMP_NUM_THREADS", "4"),),
    external_id: int = 423554,
    scheduler_state: str = "COMPLETED",
    command: tuple[str, ...] = ("./run-command.sh", "cmp01-s01"),
    outputs: tuple[str, ...] = ("frozen-model", "checklist"),
) -> SlurmJobRecord:
    """A durable Slurm job record with fixed lifecycle timestamps."""
    return SlurmJobRecord(
        job_id=job_id,
        run_id=run_id,
        state=JobState.COMPLETED,
        command=command,
        working_directory="/cluster/fdm201/cmp01",
        outputs=outputs,
        created_at=CREATED_AT,
        modules=modules,
        environment=environment,
        submitted_at=SUBMITTED_AT,
        external_id=external_id,
        scheduler_state=scheduler_state,
        exit_code=0,
        completed_at=COMPLETED_AT,
    )


def benchmark_run_id(package: dict[str, Any]) -> str:
    """The generated run id a benchmark computation package maps to
    (deterministic pure function of the package identity -- the package's
    own run id is a benchmark id, not a generated registry id)."""
    return generate_id("run", package["package_id"])


def benchmark_job_id(package: dict[str, Any]) -> str:
    """The job id of a benchmark computation package (the adapter's own
    rule: ``generate_id("job", run_id)``)."""
    return generate_id("job", benchmark_run_id(package))


def install_compute_run(root: Path, package: dict[str, Any]) -> None:
    """Register the run record of one benchmark computation package
    through the real run store, linking to the package's goal."""
    run_id = benchmark_run_id(package)
    run = make_run(run_id=run_id, goal_id=package["goal_id"])
    FilesystemStateBackend(root).write("run", run_id, run.to_dict())


def slurm_record_from_package(
    package: dict[str, Any], *, external_id: int
) -> SlurmJobRecord:
    """Derive a durable SlurmJobRecord from a benchmark computation
    package (the real record contract; values read from the package:
    command from the environment contract's step invocation, modules
    from ``compute.modules``, outputs from ``compute.output_artifacts``
    with trailing slashes stripped, the external id injected)."""
    first_step = package["procedure"][0]
    compute = package["compute"]
    outputs = tuple(
        sorted(name.rstrip("/") for name in compute["output_artifacts"])
    )
    return make_slurm_job_record(
        job_id=benchmark_job_id(package),
        run_id=benchmark_run_id(package),
        modules=tuple(compute["modules"]),
        environment=(),
        external_id=external_id,
        scheduler_state="COMPLETED",
        command=("./run-command.sh", str(first_step["id"])),
        outputs=outputs,
    )
