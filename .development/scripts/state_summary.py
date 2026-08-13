#!/usr/bin/env python3
"""Recompute .development/dag-state.yaml from live goal state files.

Usage: python .development/scripts/state_summary.py
Regenerates the ready/waiting/merged partition of the frozen DEV-GOAL DAG
from the .development/goals/*.yaml state files (SSOT).
"""
import os
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FROZEN_DIR = os.path.join(ROOT, "development", "goals")
LEDGER = os.path.join(ROOT, ".development", "state", "goal-ledger.yaml")
OUT = os.path.join(ROOT, ".development", "dag-state.yaml")

PASSED = {"MERGED", "PASS"}


def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    frozen = sorted(f for f in os.listdir(FROZEN_DIR) if f.endswith(".yaml"))
    ledger = load(LEDGER)["goals"]
    ready, waiting, passed, merged, blocked_human = [], [], [], [], []
    for f in frozen:
        contract = load(os.path.join(FROZEN_DIR, f))
        gid = contract["goal_id"]
        state = ledger.get(gid, {"status": "UNKNOWN"})
        status = state["status"]
        if status == "MERGED":
            merged.append(gid)
        elif status == "PASS":
            passed.append(gid)
        elif status == "BLOCKED_HUMAN":
            blocked_human.append(gid)
        elif status == "READY":
            deps = contract.get("depends_on", [])
            dep_states = {d: ledger.get(d, {"status": "UNKNOWN"})["status"] for d in deps}
            if all(s in PASSED for s in dep_states.values()):
                ready.append(gid)
            else:
                waiting.append(gid)
        else:  # WORKING / other
            waiting.append(gid)

    dag = {
        "dag_version": 1,
        "total_frozen_goals": len(frozen),
        "ready_to_dispatch": sorted(ready),
        "waiting_on_dependencies": sorted(waiting),
        "passed_not_merged": sorted(passed),
        "merged": sorted(merged),
        "blocked_human": sorted(blocked_human),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        yaml.safe_dump(dag, f, sort_keys=False, width=120)
    print(f"ready={len(ready)} waiting={len(waiting)} passed={len(passed)} "
          f"merged={len(merged)} blocked_human={len(blocked_human)}")


if __name__ == "__main__":
    main()
