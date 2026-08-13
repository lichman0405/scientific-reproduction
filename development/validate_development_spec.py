#!/usr/bin/env python3
"""Validate the autonomous M0-M13 development specification.

Requires PyYAML and jsonschema. This validates only this specification package;
it is not part of the Scientific Reproduction runtime implementation.
"""
from __future__ import annotations

from collections import defaultdict, deque
from pathlib import Path
import sys

try:
    import yaml
    import jsonschema
except ImportError as exc:
    raise SystemExit("Install PyYAML and jsonschema to validate the development spec") from exc

ROOT = Path(__file__).resolve().parent


def load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> int:
    goal_schema = load(ROOT / "schemas/development-goal.schema.yaml")
    milestone_schema = load(ROOT / "schemas/development-milestone.schema.yaml")
    plan_schema = load(ROOT / "schemas/development-plan.schema.yaml")

    for schema_path in (ROOT / "schemas").glob("*.yaml"):
        jsonschema.Draft202012Validator.check_schema(load(schema_path))

    goals = {}
    for path in sorted((ROOT / "goals").glob("*.yaml")):
        obj = load(path)
        jsonschema.validate(obj, goal_schema)
        goals[obj["goal_id"]] = obj

    if len(goals) != 79:
        raise AssertionError(f"Expected 79 development goals, found {len(goals)}")

    for goal_id, obj in goals.items():
        for dep in obj["depends_on"]:
            if dep not in goals:
                raise AssertionError(f"{goal_id}: unknown dependency {dep}")

    indegree = {goal_id: 0 for goal_id in goals}
    edges = defaultdict(list)
    for goal_id, obj in goals.items():
        for dep in obj["depends_on"]:
            edges[dep].append(goal_id)
            indegree[goal_id] += 1
    queue = deque([k for k, v in indegree.items() if v == 0])
    seen = []
    while queue:
        node = queue.popleft()
        seen.append(node)
        for nxt in edges[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(seen) != len(goals):
        raise AssertionError("Development goal DAG contains a cycle")

    milestones = {}
    for path in sorted((ROOT / "milestones").glob("*.yaml")):
        obj = load(path)
        jsonschema.validate(obj, milestone_schema)
        milestones[obj["milestone_id"]] = obj
        for goal_id in obj["required_goals"]:
            if goal_id not in goals:
                raise AssertionError(f"{obj['milestone_id']}: missing goal {goal_id}")

    if len(milestones) != 14:
        raise AssertionError(f"Expected 14 milestones, found {len(milestones)}")

    # Validate milestone DAG.
    m_indegree = {mid: 0 for mid in milestones}
    m_edges = defaultdict(list)
    for mid, obj in milestones.items():
        for dep in obj["depends_on_milestones"]:
            if dep not in milestones:
                raise AssertionError(f"{mid}: unknown milestone dependency {dep}")
            m_edges[dep].append(mid)
            m_indegree[mid] += 1
    m_queue = deque([k for k, v in m_indegree.items() if v == 0])
    m_seen = []
    while m_queue:
        node = m_queue.popleft()
        m_seen.append(node)
        for nxt in m_edges[node]:
            m_indegree[nxt] -= 1
            if m_indegree[nxt] == 0:
                m_queue.append(nxt)
    if len(m_seen) != len(milestones):
        raise AssertionError("Milestone DAG contains a cycle")

    plan = load(ROOT / "development-plan.v1.yaml")
    jsonschema.validate(plan, plan_schema)
    if set(plan["milestones"]) != set(milestones):
        raise AssertionError("Development plan milestones differ from milestone files")

    print(f"PASS: {len(milestones)} milestones, {len(goals)} goals, acyclic DAG, schemas valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
