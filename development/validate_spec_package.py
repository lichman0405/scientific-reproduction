from pathlib import Path
import yaml
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "development" / "schemas"

def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))

# Schema meta-validation
schemas = {}
for p in SCHEMA_DIR.glob("*.schema.yaml"):
    obj = load_yaml(p)
    jsonschema.Draft202012Validator.check_schema(obj)
    schemas[p.name] = obj

goal_schema = schemas["development-goal.schema.yaml"]
milestone_schema = schemas["development-milestone.schema.yaml"]
plan_schema = schemas["development-plan.schema.yaml"]

goals = {}
for p in (ROOT/"development"/"goals").glob("DEV-*.yaml"):
    obj = load_yaml(p)
    jsonschema.validate(obj, goal_schema)
    goals[obj["goal_id"]] = obj

milestones = {}
for p in (ROOT/"development"/"milestones").glob("M*.yaml"):
    obj = load_yaml(p)
    jsonschema.validate(obj, milestone_schema)
    milestones[obj["milestone_id"]] = obj

plan = load_yaml(ROOT/"development"/"development-plan.v1.yaml")
jsonschema.validate(plan, plan_schema)

assert len(milestones) == 14, len(milestones)
assert len(goals) == 80, len(goals)
assert set(plan["milestones"]) == set(milestones)

# Required goal existence and unique milestone ownership
required = []
for m in milestones.values():
    for gid in m["required_goals"]:
        assert gid in goals, (m["milestone_id"], gid)
        assert goals[gid]["milestone"] == m["milestone_id"]
        required.append(gid)
assert len(required) == len(set(required)) == 80

# Goal DAG dependency existence + cycle check
for gid,g in goals.items():
    for dep in g.get("depends_on", []):
        assert dep in goals, (gid, dep)

visiting, visited = set(), set()
def visit(gid):
    if gid in visited:
        return
    if gid in visiting:
        raise AssertionError(f"cycle at {gid}")
    visiting.add(gid)
    for dep in goals[gid].get("depends_on", []):
        visit(dep)
    visiting.remove(gid)
    visited.add(gid)
for gid in goals:
    visit(gid)

# Git governance pin
assert plan["repository"]["full_name"] == "lichman0405/scientific-reproduction"
assert plan["git_governance"]["branch_per_goal"] is True
assert plan["git_governance"]["pr_per_goal"] is True
assert plan["git_governance"]["default_merge_method"] == "squash"

print(f"OK: {len(schemas)} development schemas, {len(milestones)} milestones, {len(goals)} goals, acyclic DAG, Git governance pinned.")
