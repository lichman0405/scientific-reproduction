#!/usr/bin/env python
"""FDM-201 execution package validation gate.

Deterministic, offline gate that validates every package under
``benchmarks/fdm201/execution_packages/**`` against the frozen FDM-201
benchmark data (Plan v1 and its companion files):

- AC-01 (schema conformance): each ``lab-execution-package`` file is
  checked against the required/typed fields of
  ``schemas/lab-execution-package.schema.yaml``; each ``worker-context``
  file against ``schemas/worker-context.schema.yaml`` (the schemas are
  YAML; the checks below implement their required keys, value enums and
  element types). Compute packages additionally require a Slurm-oriented
  ``compute`` section whose procedure steps carry ``#SBATCH``-shaped
  scripts.
- AC-02 (traceability): every id referenced anywhere in a package
  (any list value under a ``*_refs`` key, the ``plan_ref`` value, and
  ``execution_package_refs``) must resolve to an id that exists in the
  frozen data files (GOAL-*, INV-*, EV-*, ASM-*, RES-*, ANL-*, AMB-*,
  WP-*, sr_source_*, sr_package_*). Unknown-prefix references fail.
- AC-03 (no unrelated project context): the raw text of every package
  file is scanned for forbidden patterns (development-tooling paths,
  repository source/test paths, absolute local paths, unrelated project
  markers). Any hit fails the package.
- Coverage: the execution packages (7 experiment + 3 computation) and
  the worker-context packages each cover exactly the ten execution
  goals GOAL-EXE-10..82, once each, and every package id is unique.

No wall clock, no randomness, no network. Run from anywhere:

    python benchmarks/fdm201/execution_packages/validate_packages.py

Exits 0 when every package passes, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
FDM = REPO_ROOT / "benchmarks" / "fdm201"
PKG_DIR = FDM / "execution_packages"

INVENTORY_PATH = FDM / "inventory" / "INVENTORY.yaml"
EVIDENCE_PATH = FDM / "evidence" / "evidence_records.yaml"
ASSUMPTIONS_PATH = FDM / "plans" / "assumptions.yaml"
RESOURCES_PATH = FDM / "resources" / "resources.yaml"
ANALYSIS_PATH = FDM / "plans" / "analysis_plan.yaml"
GOALS_PATH = FDM / "goals" / "goals.yaml"
PLAN_PATH = FDM / "plans" / "plan_v1.yaml"
SOURCE_RECORDS_DIR = FDM / "sources" / "records"

PLAN_ID = "sr_plan_fdm201_v1"
BENCHMARK_ID = "fdm201"
GOAL_VERSION = "v1"
TRACK_ENUM = {"STRICT_REPRODUCTION", "RECOVERY", "METHOD_REDESIGN"}
WORKER_ROLES = {
    "experiment_worker",
    "computation_worker",
    "analysis_worker",
    "diagnosis_worker",
}
LAB_KINDS = {"benchmark_lab_execution_package", "benchmark_compute_execution_package"}
WORKER_KIND = "benchmark_worker_context_package"

EXPECTED_DIRECTORY_KIND = {
    "experiment": "benchmark_lab_execution_package",
    "computation": "benchmark_compute_execution_package",
    "worker-context": "benchmark_worker_context_package",
}

#: The ten execution goals the packages cover (GOAL-EXE-10..82).
EXECUTION_GOALS = [
    "GOAL-EXE-10",
    "GOAL-EXE-20",
    "GOAL-EXE-30",
    "GOAL-EXE-40",
    "GOAL-EXE-50",
    "GOAL-EXE-60",
    "GOAL-EXE-70",
    "GOAL-EXE-80",
    "GOAL-EXE-81",
    "GOAL-EXE-82",
]

#: AC-03 forbidden patterns: development tooling, repository source/test
#: trees, absolute local paths and unrelated project markers. Checked
#: against the raw package text (backslash variants included for
#: Windows-style paths).
FORBIDDEN_PATTERNS = [
    ".development",
    r"\bsrc[/\\]",
    r"\btests[/\\]",
    r"[\\/]src[\\/]",
    r"[\\/]tests[\\/]",
    r"[/\\]development[\\/]",
    r"[/\\]\.worktrees[\\/]",
    r"[/\\]\.github[\\/]",
    r"\bpyproject\.toml\b",
    r"\bscientific_reproduction\b",
    r"\bC:[\\/]",
    r"\b[A-Za-z]:[\\/]",
    r"\\scripts\\",
    r"[/\\]scripts[\\/]",
]
FORBIDDEN_RE = [re.compile(p) for p in FORBIDDEN_PATTERNS]

#: A reference id resolves iff it starts with one of these prefixes and
#: the remainder exists in the frozen data (see load_reference_sets).
REF_PREFIXES = (
    "GOAL-",
    "INV-",
    "EV-",
    "ASM-",
    "RES-",
    "ANL-",
    "AMB-",
    "WP-",
    "sr_source_",
    "sr_package_",
    "sr_plan_",
)


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {path} did not parse to a mapping")
    return data


def load_reference_sets() -> dict[str, set[str]]:
    """Load the frozen id sets the packages may reference."""
    inv = load_yaml(INVENTORY_PATH)
    evidence = load_yaml(EVIDENCE_PATH)
    assumptions = load_yaml(ASSUMPTIONS_PATH)
    resources = load_yaml(RESOURCES_PATH)
    analysis = load_yaml(ANALYSIS_PATH)
    goals = load_yaml(GOALS_PATH)
    plan = load_yaml(PLAN_PATH)

    goal_ids = {g["goal_id"] for g in goals["goals"]}
    wp_ids = set(goals.get("wp_alias", {}))
    wp_ids.update(wp["wp_id"] for wp in plan.get("work_packages", []))
    source_ids: set[str] = set()
    for record in SOURCE_RECORDS_DIR.glob("*.yaml"):
        data = load_yaml(record)
        source_ids.add(data["source_id"])

    return {
        "GOAL-": goal_ids,
        "INV-": {i["item_id"] for i in inv["items"]},
        "AMB-": {a["ambiguity_id"] for a in inv.get("ambiguities", [])},
        "EV-": {r["evidence_id"] for r in evidence.get("evidence_records", [])},
        "ASM-": {a["assumption_id"] for a in assumptions.get("assumptions", [])},
        "RES-": {r["resource_id"] for r in resources.get("resources", [])},
        "ANL-": {p["analysis_id"] for p in analysis.get("protocols", [])},
        "WP-": wp_ids,
        "sr_source_": source_ids,
        "sr_plan_": {PLAN_ID},
    }


def iter_refs(data: object) -> list[str]:
    """Collect every string under a key ending in ``_refs`` (recursive)."""
    refs: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.endswith("_refs") and isinstance(value, (list, str)):
                    values = value if isinstance(value, list) else [value]
                    refs.extend(v for v in values if isinstance(v, str))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return refs


def classify_ref(ref: str) -> str:
    for prefix in REF_PREFIXES:
        if ref.startswith(prefix):
            return prefix
    return ""


def validate_schema(package: dict, kind: str) -> list[str]:
    """AC-01: required keys, value enums and element types."""
    failures: list[str] = []
    if kind in LAB_KINDS:
        for key in ("package_id", "project_id", "goal_id", "run_id", "objective"):
            if not isinstance(package.get(key), str) or not package[key]:
                failures.append(f"missing or non-string {key!r}")
        for key in ("procedure", "required_return"):
            if key not in package:
                failures.append(f"missing required key {key!r}")
            elif not isinstance(package[key], list):
                failures.append(f"{key} must be a list")
        procedure = package.get("procedure", [])
        if not all(isinstance(step, dict) for step in procedure):
            failures.append("procedure items must be objects")
        if not all(isinstance(item, str) for item in package.get("required_return", [])):
            failures.append("required_return items must be strings")
        track = package.get("track")
        if track is not None and track not in TRACK_ENUM:
            failures.append(f"track {track!r} not in {sorted(TRACK_ENUM)}")
        for key in ("reagents", "instruments", "critical_control_variables"):
            value = package.get(key)
            if value is not None and not all(isinstance(item, dict) for item in value):
                failures.append(f"{key} items must be objects")
        for key in ("prohibited_changes", "required_operator_records", "safety_notes"):
            value = package.get(key)
            if value is not None and not all(isinstance(item, str) for item in value):
                failures.append(f"{key} items must be strings")
        if not procedure:
            failures.append("procedure must be non-empty")
        if kind == "benchmark_compute_execution_package":
            compute = package.get("compute")
            if not isinstance(compute, dict) or compute.get("scheduler") != "slurm":
                failures.append("compute.scheduler must be 'slurm'")
            scripts = [step.get("sbatch_script", "") for step in procedure]
            if not all(isinstance(s, str) and "#SBATCH" in s for s in scripts):
                failures.append("every compute procedure step must carry an sbatch_script with #SBATCH directives")
        else:
            if not all(isinstance(step.get("action"), str) and step.get("action") for step in procedure):
                failures.append("every lab procedure step must carry a non-empty 'action'")
    elif kind == WORKER_KIND:
        for key in ("context_id", "worker_role", "goal_id", "goal_version"):
            if not isinstance(package.get(key), str) or not package[key]:
                failures.append(f"missing or non-string {key!r}")
        if package.get("worker_role") not in WORKER_ROLES:
            failures.append(f"worker_role {package.get('worker_role')!r} not in {sorted(WORKER_ROLES)}")
        for key in ("allowed_actions", "forbidden_actions"):
            value = package.get(key)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                failures.append(f"{key} must be a list of strings")
        for key in ("source_refs", "evidence_refs", "upstream_result_refs", "protocol_refs", "resource_refs", "required_outputs"):
            value = package.get(key)
            if value is not None and not all(isinstance(item, str) for item in value):
                failures.append(f"{key} must be a list of strings")
        environment = package.get("environment")
        if environment is not None and not isinstance(environment, dict):
            failures.append("environment must be an object")
        run_id = package.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            failures.append("run_id must be a string or null")
        context_hash = package.get("context_hash")
        if context_hash is not None and not isinstance(context_hash, str):
            failures.append("context_hash must be a string or null")
    else:
        failures.append(f"unknown record_kind {kind!r}")
    return failures


def validate_traceability(package: dict, ref_sets: dict[str, set[str]], package_ids: set[str]) -> list[str]:
    """AC-02: every referenced id resolves to an existing frozen id."""
    failures: list[str] = []
    refs = iter_refs(package)
    traceability = package.get("traceability")
    if isinstance(traceability, dict):
        plan_ref = traceability.get("plan_ref")
        if isinstance(plan_ref, str):
            refs.append(plan_ref)
    for ref in sorted(set(refs)):
        prefix = classify_ref(ref)
        if not prefix:
            failures.append(f"reference {ref!r} has no known id prefix")
            continue
        if prefix == "sr_package_":
            if ref not in package_ids:
                failures.append(f"reference {ref!r} does not resolve to a declared package id")
            continue
        if ref not in ref_sets.get(prefix, set()):
            failures.append(f"reference {ref!r} does not exist in the frozen data")
    return failures


def validate_forbidden_text(path: Path) -> list[str]:
    """AC-03: no unrelated project context inside package content."""
    text = path.read_text(encoding="utf-8")
    failures: list[str] = []
    for pattern, compiled in zip(FORBIDDEN_PATTERNS, FORBIDDEN_RE, strict=True):
        if compiled.search(text):
            failures.append(f"forbidden pattern {pattern!r} present")
    return failures


def check_goal_consistency(
    packages: list[tuple[Path, dict]],
    failures: list[str],
) -> None:
    lab_goals: list[str] = []
    worker_goals: list[str] = []
    for path, package in packages:
        kind = package.get("record_kind")
        goal_id = package.get("goal_id")
        if not isinstance(goal_id, str):
            continue
        if kind in LAB_KINDS:
            lab_goals.append(goal_id)
        elif kind == WORKER_KIND:
            worker_goals.append(goal_id)
        else:
            failures.append(f"{path.name}: unsupported record_kind {kind!r}")
    if sorted(lab_goals) != sorted(EXECUTION_GOALS):
        failures.append(
            "execution packages must cover GOAL-EXE-10..82 exactly once;"
            f" got {sorted(lab_goals)}"
        )
    if sorted(worker_goals) != sorted(EXECUTION_GOALS):
        failures.append(
            "worker-context packages must cover GOAL-EXE-10..82 exactly once;"
            f" got {sorted(worker_goals)}"
        )


def main() -> int:
    ref_sets = load_reference_sets()
    yaml_paths = sorted(PKG_DIR.rglob("*.yaml"))
    package_ids: set[str] = set()
    parsed: list[tuple[Path, dict]] = []
    for path in yaml_paths:
        package = load_yaml(path)
        parsed.append((path, package))
        package_id = package.get("package_id")
        if isinstance(package_id, str):
            package_ids.add(package_id)

    failures: list[str] = []
    global_failures: list[str] = []
    checks_run = 0
    per_package: list[tuple[str, bool, list[str]]] = []

    for path, package in parsed:
        checks_run += 1
        kind = package.get("record_kind")
        expected_kind = EXPECTED_DIRECTORY_KIND.get(path.parent.name)
        local_failures: list[str] = []
        if expected_kind is None:
            local_failures.append("package files must live in experiment/, computation/ or worker-context/")
        elif kind != expected_kind:
            local_failures.append(f"record_kind {kind!r} does not match directory {path.parent.name!r}")
        local_failures.extend(validate_schema(package, kind))
        local_failures.extend(validate_traceability(package, ref_sets, package_ids))
        local_failures.extend(validate_forbidden_text(path))
        if kind in LAB_KINDS:
            for key in ("benchmark_id", "plan_id"):
                if package.get(key) != (BENCHMARK_ID if key == "benchmark_id" else PLAN_ID):
                    local_failures.append(f"{key} must be {BENCHMARK_ID!r}/{PLAN_ID!r}")
            if package.get("goal_version") != GOAL_VERSION:
                local_failures.append(f"goal_version must be {GOAL_VERSION!r}")
            run_id = package.get("run_id")
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
                local_failures.append("run_id must be a non-empty safe path segment (letters, digits, '_', '-')")
        elif kind == WORKER_KIND:
            if package.get("goal_version") != GOAL_VERSION:
                local_failures.append(f"goal_version must be {GOAL_VERSION!r}")

        for failure in local_failures:
            failures.append(f"{path.relative_to(PKG_DIR).as_posix()}: {failure}")
        per_package.append((path.relative_to(PKG_DIR).as_posix(), not local_failures, local_failures))

    check_goal_consistency(parsed, global_failures)
    id_counts = {
        "GOAL-": len(ref_sets["GOAL-"]),
        "INV-": len(ref_sets["INV-"]),
        "EV-": len(ref_sets["EV-"]),
        "ASM-": len(ref_sets["ASM-"]),
        "RES-": len(ref_sets["RES-"]),
        "ANL-": len(ref_sets["ANL-"]),
        "AMB-": len(ref_sets["AMB-"]),
        "sr_source_": len(ref_sets["sr_source_"]),
    }

    print("FDM-201 execution package validation gate")
    print(f"  frozen reference ids: {id_counts}")
    print(f"  package files found: {len(parsed)}")
    print()
    for name, ok, local_failures in per_package:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        for failure in local_failures:
            print(f"      - {failure}")
    for failure in global_failures:
        print(f"[FAIL] {failure}")
        failures.append(failure)

    print()
    print(f"Checks run: {checks_run}, failures: {len(failures)}")
    if failures:
        print("FAILED CHECKS:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All packages validate: AC-01 schema conformance, AC-02 traceability,"
          " AC-03 context isolation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
