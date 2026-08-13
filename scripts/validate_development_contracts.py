#!/usr/bin/env python3
"""Canonical validator for frozen development contracts (DEV-GOAL and milestone YAMLs).

Scope: loads every goal YAML from development/goals/ and every milestone YAML
from development/milestones/ and verifies:

  * exact frozen v0.1.2 counts: 14 milestones and 80 goals;
  * goal structure: goal_id (DEV-M??-G??), milestone, title, objective,
    depends_on, allowed_scope, deliverables, acceptance_criteria,
    verification, worker_mode — with pattern/type checks that detect
    malformed contracts;
  * milestone structure: milestone_id (M??), required_goals,
    milestone_acceptance (plus depends_on_milestones/title);
  * goal-level dependency DAG acyclicity and depends_on references that
    resolve to existing goal_ids;
  * milestone required_goals that resolve to existing goal_ids and an acyclic
    milestone dependency DAG;
  * full structural conformance against the frozen JSON Schemas in
    development/schemas/ (development-goal.schema.yaml,
    development-milestone.schema.yaml).

SPEC-DEFECT-001: development/validate_development_spec.py (frozen spec content)
still asserts "Expected 79 development goals". This script is the canonical
80-goal validator for the frozen v0.1.2 contract set and does not modify that
frozen file.

Dependencies: Python 3 stdlib, PyYAML, jsonschema.
Exits 0 on success; exits non-zero with a per-file error report otherwise.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

try:
    import yaml
    import jsonschema
except ImportError as exc:  # pragma: no cover - environment error path
    raise SystemExit(
        "Install PyYAML and jsonschema to validate development contracts: "
        "pip install pyyaml jsonschema"
    ) from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
GOALS_DIR = REPO_ROOT / "development" / "goals"
MILESTONES_DIR = REPO_ROOT / "development" / "milestones"
SCHEMAS_DIR = REPO_ROOT / "development" / "schemas"

EXPECTED_MILESTONE_COUNT = 14
EXPECTED_GOAL_COUNT = 80

GOAL_ID_RE = re.compile(r"^DEV-M(0|[1-9]|1[0-3])-G[0-9]{2}$")
MILESTONE_ID_RE = re.compile(r"^M(0|[1-9]|1[0-3])$")
AC_ID_RE = re.compile(r"^AC-[0-9]{2}$")

GOAL_REQUIRED_FIELDS = (
    "goal_id",
    "milestone",
    "title",
    "objective",
    "depends_on",
    "allowed_scope",
    "deliverables",
    "acceptance_criteria",
    "verification",
    "worker_mode",
)
MILESTONE_REQUIRED_FIELDS = (
    "milestone_id",
    "title",
    "depends_on_milestones",
    "required_goals",
    "milestone_acceptance",
)
WORKER_MODES = ("agent_team_task", "detached_native_goal", "either")


def load_yaml(path: Path, errors: list[str]) -> Any:
    """Load a YAML file; record a malformed-contract error on parse failure."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"{path}: malformed YAML: {exc}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{path}: contract root is not a mapping")
        return None
    return data


def validate_goal(path: Path, goal: dict[str, Any], errors: list[str]) -> None:
    """Explicit structural checks for a single DEV-GOAL contract."""
    label = path.name

    missing = [f for f in GOAL_REQUIRED_FIELDS if f not in goal]
    if missing:
        errors.append(f"{label}: missing required field(s): {', '.join(missing)}")

    gid = goal.get("goal_id")
    if not isinstance(gid, str) or not GOAL_ID_RE.match(gid):
        errors.append(
            f"{label}: goal_id {gid!r} does not match pattern {GOAL_ID_RE.pattern}"
        )

    milestone = goal.get("milestone")
    if not isinstance(milestone, str) or not MILESTONE_ID_RE.match(milestone):
        errors.append(
            f"{label}: milestone {milestone!r} does not match pattern "
            f"{MILESTONE_ID_RE.pattern}"
        )

    title = goal.get("title")
    if not isinstance(title, str) or len(title) < 4:
        errors.append(f"{label}: title must be a string of at least 4 characters")

    objective = goal.get("objective")
    if not isinstance(objective, str) or len(objective) < 12:
        errors.append(f"{label}: objective must be a string of at least 12 characters")

    depends_on = goal.get("depends_on")
    if not isinstance(depends_on, list):
        errors.append(f"{label}: depends_on must be a list")
    else:
        seen: set[str] = set()
        for dep in depends_on:
            if not isinstance(dep, str) or not GOAL_ID_RE.match(dep):
                errors.append(
                    f"{label}: depends_on entry {dep!r} does not match pattern "
                    f"{GOAL_ID_RE.pattern}"
                )
            elif dep in seen:
                errors.append(f"{label}: duplicate depends_on entry {dep!r}")
            seen.add(dep)

    allowed_scope = goal.get("allowed_scope")
    if not isinstance(allowed_scope, list) or not allowed_scope:
        errors.append(f"{label}: allowed_scope must be a non-empty list")
    elif not all(isinstance(item, str) for item in allowed_scope):
        errors.append(f"{label}: allowed_scope entries must be strings")

    deliverables = goal.get("deliverables")
    if not isinstance(deliverables, list) or not deliverables:
        errors.append(f"{label}: deliverables must be a non-empty list")
    elif not all(isinstance(item, str) for item in deliverables):
        errors.append(f"{label}: deliverables entries must be strings")

    acceptance_criteria = goal.get("acceptance_criteria")
    if not isinstance(acceptance_criteria, list) or not acceptance_criteria:
        errors.append(f"{label}: acceptance_criteria must be a non-empty list")
    else:
        for ac in acceptance_criteria:
            if not isinstance(ac, dict):
                errors.append(f"{label}: acceptance_criteria entry {ac!r} is not a mapping")
                continue
            ac_id = ac.get("id")
            if not isinstance(ac_id, str) or not AC_ID_RE.match(ac_id):
                errors.append(
                    f"{label}: acceptance criterion id {ac_id!r} does not match "
                    f"pattern {AC_ID_RE.pattern}"
                )
            criterion = ac.get("criterion")
            if not isinstance(criterion, str) or len(criterion) < 8:
                errors.append(
                    f"{label}: acceptance criterion {ac_id!r} needs a criterion "
                    f"string of at least 8 characters"
                )

    verification = goal.get("verification")
    if not isinstance(verification, list) or not verification:
        errors.append(f"{label}: verification must be a non-empty list")
    elif not all(isinstance(item, str) for item in verification):
        errors.append(f"{label}: verification entries must be strings")

    worker_mode = goal.get("worker_mode")
    if worker_mode not in WORKER_MODES:
        errors.append(
            f"{label}: worker_mode {worker_mode!r} must be one of {WORKER_MODES}"
        )


def validate_milestone(path: Path, milestone: dict[str, Any], errors: list[str]) -> None:
    """Explicit structural checks for a single milestone contract."""
    label = path.name

    missing = [f for f in MILESTONE_REQUIRED_FIELDS if f not in milestone]
    if missing:
        errors.append(f"{label}: missing required field(s): {', '.join(missing)}")

    mid = milestone.get("milestone_id")
    if not isinstance(mid, str) or not MILESTONE_ID_RE.match(mid):
        errors.append(
            f"{label}: milestone_id {mid!r} does not match pattern "
            f"{MILESTONE_ID_RE.pattern}"
        )

    title = milestone.get("title")
    if not isinstance(title, str) or len(title) < 4:
        errors.append(f"{label}: title must be a string of at least 4 characters")

    depends_on = milestone.get("depends_on_milestones")
    if not isinstance(depends_on, list):
        errors.append(f"{label}: depends_on_milestones must be a list")
    else:
        seen: set[str] = set()
        for dep in depends_on:
            if not isinstance(dep, str) or not MILESTONE_ID_RE.match(dep):
                errors.append(
                    f"{label}: depends_on_milestones entry {dep!r} does not match "
                    f"pattern {MILESTONE_ID_RE.pattern}"
                )
            elif dep in seen:
                errors.append(f"{label}: duplicate depends_on_milestones entry {dep!r}")
            seen.add(dep)

    required_goals = milestone.get("required_goals")
    if not isinstance(required_goals, list) or not required_goals:
        errors.append(f"{label}: required_goals must be a non-empty list")
    else:
        seen = set()
        for goal_id in required_goals:
            if not isinstance(goal_id, str) or not GOAL_ID_RE.match(goal_id):
                errors.append(
                    f"{label}: required_goal {goal_id!r} does not match pattern "
                    f"{GOAL_ID_RE.pattern}"
                )
            elif goal_id in seen:
                errors.append(f"{label}: duplicate required_goal {goal_id!r}")
            seen.add(goal_id)

    milestone_acceptance = milestone.get("milestone_acceptance")
    if not isinstance(milestone_acceptance, list) or not milestone_acceptance:
        errors.append(f"{label}: milestone_acceptance must be a non-empty list")
    elif not all(
        isinstance(item, str) and len(item) >= 8 for item in milestone_acceptance
    ):
        errors.append(
            f"{label}: milestone_acceptance entries must be strings of at least "
            f"8 characters"
        )


def check_acyclic(nodes: dict[str, Any], edges_key: str, errors: list[str]) -> None:
    """Kahn's algorithm: ensure the dependency graph has no cycles."""
    indegree = {node: 0 for node in nodes}
    edges: dict[str, list[str]] = defaultdict(list)
    for node, obj in nodes.items():
        for dep in obj.get(edges_key, []):
            edges[dep].append(node)
            indegree[node] += 1
    queue = deque(node for node, degree in indegree.items() if degree == 0)
    seen: list[str] = []
    while queue:
        node = queue.popleft()
        seen.append(node)
        for nxt in edges[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(seen) != len(nodes):
        remaining = sorted(set(nodes) - set(seen))
        errors.append(f"dependency graph contains a cycle among: {remaining}")


def main() -> int:
    errors: list[str] = []

    if not GOALS_DIR.is_dir() or not MILESTONES_DIR.is_dir():
        errors.append(
            f"development contract directories not found: {GOALS_DIR} / "
            f"{MILESTONES_DIR} (run from the repository root)"
        )
    if not SCHEMAS_DIR.is_dir():
        errors.append(f"frozen schema directory not found: {SCHEMAS_DIR}")

    # --- load contracts -------------------------------------------------
    goals: dict[str, dict[str, Any]] = {}
    goal_paths: dict[str, Path] = {}
    for path in sorted(GOALS_DIR.glob("*.yaml")):
        data = load_yaml(path, errors)
        if data is not None:
            gid = data.get("goal_id")
            if isinstance(gid, str) and gid in goals:
                errors.append(
                    f"{path.name}: duplicate goal_id {gid} (already declared in "
                    f"{goal_paths[gid].name})"
                )
                continue
            goals[gid] = data if isinstance(gid, str) else {"goal_id": None}
            goal_paths[gid] = path if isinstance(gid, str) else path

    milestones: dict[str, dict[str, Any]] = {}
    for path in sorted(MILESTONES_DIR.glob("*.yaml")):
        data = load_yaml(path, errors)
        if data is not None:
            mid = data.get("milestone_id")
            if isinstance(mid, str) and mid in milestones:
                errors.append(
                    f"{path.name}: duplicate milestone_id {mid} "
                    f"(already declared elsewhere)"
                )
                continue
            milestones[mid] = data if isinstance(mid, str) else {"milestone_id": None}

    # --- exact frozen v0.1.2 counts --------------------------------------
    if len(goals) != EXPECTED_GOAL_COUNT:
        errors.append(
            f"expected exactly {EXPECTED_GOAL_COUNT} development goals, "
            f"found {len(goals)}"
        )
    if len(milestones) != EXPECTED_MILESTONE_COUNT:
        errors.append(
            f"expected exactly {EXPECTED_MILESTONE_COUNT} milestones, "
            f"found {len(milestones)}"
        )

    # --- per-goal structure ---------------------------------------------
    for gid in sorted(goals):
        validate_goal(goal_paths[gid], goals[gid], errors)

    # --- per-milestone structure -----------------------------------------
    for mid in sorted(milestones):
        validate_milestone(MILESTONES_DIR / f"{mid}.yaml", milestones[mid], errors)

    # --- frozen-schema conformance ---------------------------------------
    goal_schema_path = SCHEMAS_DIR / "development-goal.schema.yaml"
    milestone_schema_path = SCHEMAS_DIR / "development-milestone.schema.yaml"
    if goal_schema_path.is_file() and milestone_schema_path.is_file():
        goal_schema = yaml.safe_load(goal_schema_path.read_text(encoding="utf-8"))
        milestone_schema = yaml.safe_load(
            milestone_schema_path.read_text(encoding="utf-8")
        )
        for gid in sorted(goals):
            try:
                jsonschema.validate(goals[gid], goal_schema)
            except jsonschema.ValidationError as exc:
                errors.append(
                    f"{goal_paths[gid].name}: schema violation: {exc.message}"
                )
        for mid in sorted(milestones):
            try:
                jsonschema.validate(milestones[mid], milestone_schema)
            except jsonschema.ValidationError as exc:
                errors.append(
                    f"{mid}.yaml: schema violation: {exc.message}"
                )
    else:
        errors.append("frozen goal/milestone schemas not found in development/schemas/")

    # --- cross-references and DAG ----------------------------------------
    for gid in sorted(goals):
        for dep in goals[gid].get("depends_on", []):
            if dep not in goals:
                errors.append(f"{gid}: depends_on references unknown goal {dep}")
    for mid in sorted(milestones):
        for goal_id in milestones[mid].get("required_goals", []):
            if goal_id not in goals:
                errors.append(f"{mid}: required_goals references unknown goal {goal_id}")

    check_acyclic(goals, "depends_on", errors)
    check_acyclic(milestones, "depends_on_milestones", errors)

    # --- report -----------------------------------------------------------
    if errors:
        print(f"FAIL: {len(errors)} validation error(s):")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(
        f"PASS: {len(milestones)} milestones, {len(goals)} goals, "
        f"goal DAG acyclic, milestone DAG acyclic, all contracts well-formed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
