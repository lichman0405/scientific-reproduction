#!/usr/bin/env python
"""FDM-201 Plan v1 mapping audit self-check (DEV-M12-G03).

Deterministic, offline gate that validates the Plan v1 deliverable set
against the G02 inventory (source of truth for the 82 items):

- 100% mapping: every inventory requirement_mapping / resolves_in value
  resolves (GOAL-* directly, WP-* via the goals.yaml wp_alias table) to a
  goal defined in goals.yaml; 82/82 mapped, 0 unmapped; mapping_audit.yaml
  equals the recomputed mapping.
- Ambiguities: all 14 entries (AMB-0001..0014) resolve to defined goals;
  mapping_audit.yaml matches.
- Goals DAG: every dependency references a defined goal, all dependency
  types are known, the graph is acyclic (topological sort), dag_summary
  matches.
- Plan v1: version/status/freeze timestamp, the inventory_audit block, and
  the goal_ids / requirement_ids sets match the recomputed sets.
- Assumptions: 12 A2 inventory registrations (ASM-FDM-*,
  DISQUALIFIES_PURE_STRICT), exactly the 18 ambiguity-coverage references
  recomputed from the inventory self_check warning rule (ASM-CVR-*), and 15
  Plan v1 methodological defaults (ASM-A1-*); total 45.
- Evidence: 82 EV-INV-* records, one per item, A/R/D assessment matching the
  frozen basis mapping, used_by goals defined.
- Resources: 17 RES-* records, schema enums, G01 acquisition-log
  consistency, goal references defined.
- Analysis/closure objects: counts and ids match the referenced files.
- Placeholder scan and fixed-timestamp determinism across the deliverable
  data files.

No wall clock, no randomness, no network. Run from anywhere:

    python benchmarks/fdm201/goals/audit_check.py

Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
FDM = REPO_ROOT / "benchmarks" / "fdm201"
INV_PATH = FDM / "inventory" / "INVENTORY.yaml"
GOALS_PATH = FDM / "goals" / "goals.yaml"
AUDIT_PATH = FDM / "goals" / "mapping_audit.yaml"
PLAN_PATH = FDM / "plans" / "plan_v1.yaml"
ASSUMPTIONS_PATH = FDM / "plans" / "assumptions.yaml"
ANALYSIS_PATH = FDM / "plans" / "analysis_plan.yaml"
CLOSURE_PATH = FDM / "plans" / "closure.yaml"
EVIDENCE_MODEL_PATH = FDM / "evidence" / "evidence_model.yaml"
EVIDENCE_RECORDS_PATH = FDM / "evidence" / "evidence_records.yaml"
RESOURCES_PATH = FDM / "resources" / "resources.yaml"
ACQ_LOG_PATH = FDM / "sources" / "ACQUISITION_LOG.yaml"

FIXED_TIMESTAMP = "2026-08-14T00:00:00Z"
PLACEHOLDER_RE = re.compile(r"\b(TBD|TODO|FIXME|XXX)\b")
TIMESTAMP_FIELDS = {
    INV_PATH: "inventoried_at",
    ACQ_LOG_PATH: "acquired_at",
    GOALS_PATH: "frozen_at",
    AUDIT_PATH: "audited_at",
    PLAN_PATH: "frozen_at",
    ASSUMPTIONS_PATH: "frozen_at",
    ANALYSIS_PATH: "frozen_at",
    CLOSURE_PATH: "frozen_at",
    EVIDENCE_MODEL_PATH: "frozen_at",
    EVIDENCE_RECORDS_PATH: "recorded_at",
    RESOURCES_PATH: "frozen_at",
}

WP_ALIAS = {
    f"WP-{n}": f"GOAL-EXE-{n}"
    for n in ["10", "20", "30", "40", "50", "60", "70", "80", "81", "82", "90"]
}
BASIS_AR_D = {
    "CROSSREF-METADATA": (4, 4, 4),
    "ACQUISITION-LOG": (3, 3, 4),
    "REFERENCE-CASE-SPEC": (2, 3, 4),
    "PUBLIC-SEARCH": (2, 2, 3),
}
RESOURCE_TYPES = {"reagent", "consumable", "instrument", "external_service", "compute_access", "database_access", "safety_capability", "other"}
AVAILABILITY_STATES = {"AVAILABLE", "PROCURE", "OUTSOURCE", "CAPABILITY_GAP"}
DEP_TYPES = {"hard_gate", "soft_dependency", "informational"}
CATEGORY_IDS = {"00", "a", "b", "c", "d", "e", "f", "g"}
STATUS_ENUM = {"INVENTORIED", "INVENTORIED-DEFERRED", "DEFERRED-TO-EXECUTION"}
VALUE_STATUS_ENUM = {"ESTABLISHED", "REPORTED-NON-FINAL", "EXISTENCE-ONLY", "MISSING", "NOT-APPLICABLE"}


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {path} did not parse to a mapping")
    return data


def resolve(values: list[str] | None) -> list[str]:
    return [WP_ALIAS.get(v, v) for v in (values or [])]


def resolve_set(values: list[str] | None) -> set[str]:
    return set(resolve(values))


def checks_fail(failures: list[str]) -> None:
    if failures:
        print("\nFAILED CHECKS:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)


def main() -> int:
    inv = load_yaml(INV_PATH)
    goals = load_yaml(GOALS_PATH)
    audit = load_yaml(AUDIT_PATH)
    plan = load_yaml(PLAN_PATH)
    assumptions = load_yaml(ASSUMPTIONS_PATH)
    analysis = load_yaml(ANALYSIS_PATH)
    closure = load_yaml(CLOSURE_PATH)
    model = load_yaml(EVIDENCE_MODEL_PATH)
    evidence = load_yaml(EVIDENCE_RECORDS_PATH)
    resources = load_yaml(RESOURCES_PATH)
    acq = load_yaml(ACQ_LOG_PATH)

    failures: list[str] = []
    checks_run = 0

    def check(ok: bool, label: str) -> None:
        nonlocal checks_run
        checks_run += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    # ------------------------------------------------------------------
    # 1. Inventory structure (source of truth)
    # ------------------------------------------------------------------
    items = inv.get("items") or []
    item_ids = [i.get("item_id") for i in items]
    check(len(items) == 82, "inventory has 82 items")
    check(len(set(item_ids)) == len(item_ids), "inventory item ids are unique")
    check(
        all(i.get("category") in CATEGORY_IDS for i in items),
        "inventory categories are valid",
    )
    check(
        all(i.get("status") in STATUS_ENUM for i in items),
        "inventory statuses are valid",
    )
    check(
        all(i.get("value_status") in VALUE_STATUS_ENUM for i in items),
        "inventory value_statuses are valid",
    )
    ambs = inv.get("ambiguities") or []
    check(len(ambs) == 14, "inventory has 14 ambiguity entries")
    check(len({a.get("ambiguity_id") for a in ambs}) == 14, "ambiguity ids are unique")
    a2_ids = sorted(i["item_id"] for i in items if i.get("assumption_class") == "A2")
    check(a2_ids == ["INV-0511", "INV-0512", "INV-0513", "INV-0514", "INV-0515", "INV-0516", "INV-0517", "INV-0518", "INV-0519", "INV-0520", "INV-0521", "INV-0605"], "exactly 12 A2 inventory registrations (INV-0511..0521, INV-0605)")

    # ------------------------------------------------------------------
    # 2. Mapping coverage (every item resolves; 82/82, unmapped = 0)
    # ------------------------------------------------------------------
    goal_ids = {g["goal_id"] for g in goals["goals"]}
    check(len(goal_ids) == 20, "goals.yaml defines 20 goals")
    mapping_by_item: dict[str, set[str]] = {}
    for i in items:
        mapped = resolve_set(i.get("requirement_mapping")) | resolve_set(i.get("resolves_in"))
        mapping_by_item[i["item_id"]] = mapped
        check(bool(mapped), f"{i['item_id']}: requirement_mapping resolves non-empty")
        check(mapped <= goal_ids, f"{i['item_id']}: all resolved mappings are defined goals")
    mapped_count = sum(1 for i in items if mapping_by_item[i["item_id"]])
    check(mapped_count == 82, "mapped items = 82 (unmapped = 0)")

    # ------------------------------------------------------------------
    # 3. mapping_audit.yaml consistency
    # ------------------------------------------------------------------
    audit_items = audit.get("item_mappings") or []
    check(len(audit_items) == 82, "mapping_audit has 82 item_mappings")
    for entry in audit_items:
        item_id = entry["item_id"]
        recomputed = mapping_by_item[item_id]
        check(
            set(entry.get("mapped_to", [])) == recomputed,
            f"{item_id}: mapped_to equals recomputed mapping",
        )
    cov = audit.get("coverage") or {}
    check(cov.get("formally_reported_items") == 82, "audit coverage: formally_reported_items = 82")
    check(cov.get("mapped_items") == 82, "audit coverage: mapped_items = 82")
    check(cov.get("unmapped_items") == 0, "audit coverage: unmapped_items = 0")
    check(cov.get("ambiguous_items") == 0, "audit coverage: ambiguous_items = 0")
    check(cov.get("unresolved_ambiguities") == 0, "audit coverage: unresolved_ambiguities = 0")
    check(cov.get("coverage") == 1.0, "audit coverage: coverage = 1.0")
    check(cov.get("status") == "PASS", "audit coverage: status = PASS")
    asm_cov = audit.get("assumptions_coverage") or {}
    check(asm_cov.get("a2_registrations") == 12, "audit assumptions_coverage: a2_registrations = 12")
    check(asm_cov.get("ambiguity_coverage_references") == 18, "audit assumptions_coverage: ambiguity_coverage_references = 18")
    check(asm_cov.get("assumption_objects") == 45, "audit assumptions_coverage: assumption_objects = 45")

    # ------------------------------------------------------------------
    # 4. Ambiguity resolutions (14/14, all to defined goals)
    # ------------------------------------------------------------------
    res_by_amb: dict[str, set[str]] = {}
    for amb in ambs:
        resolved = resolve_set(amb.get("resolves_in"))
        res_by_amb[amb["ambiguity_id"]] = resolved
        check(bool(resolved), f"{amb['ambiguity_id']}: resolution goals non-empty")
        check(resolved <= goal_ids, f"{amb['ambiguity_id']}: resolution goals are defined")
    audit_res = audit.get("ambiguity_resolutions") or []
    check(len(audit_res) == 14, "mapping_audit has 14 ambiguity_resolutions")
    for entry in audit_res:
        check(
            set(entry.get("resolution_goals", [])) == res_by_amb[entry["ambiguity_id"]],
            f"{entry['ambiguity_id']}: resolution_goals equal recomputed set",
        )

    # ------------------------------------------------------------------
    # 5. Goals DAG: references, types, acyclicity, summary
    # ------------------------------------------------------------------
    edges: list[tuple[str, str]] = []
    for g in goals["goals"]:
        for dep in g.get("dependencies") or []:
            check(isinstance(dep, dict) and dep.get("goal_id") in goal_ids, f"{g['goal_id']}: dependency goal_id defined")
            check(dep.get("type") in DEP_TYPES, f"{g['goal_id']}: dependency type valid")
            if dep.get("type") == "hard_gate":
                check(
                    bool(dep.get("execution_gate") or dep.get("acceptance_gate")),
                    f"{g['goal_id']}: hard_gate carries an execution or acceptance gate",
                )
            edges.append((dep["goal_id"], g["goal_id"]))
        check(g.get("frozen_at") == FIXED_TIMESTAMP, f"{g['goal_id']}: frozen_at fixed timestamp")
    indegree = {gid: 0 for gid in goal_ids}
    out: dict[str, list[str]] = {gid: [] for gid in goal_ids}
    for src, dst in edges:
        out[src].append(dst)
        indegree[dst] += 1
    queue = [gid for gid, deg in indegree.items() if deg == 0]
    ordered: list[str] = []
    while queue:
        queue.sort()
        node = queue.pop(0)
        ordered.append(node)
        for nxt in out[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    check(len(ordered) == len(goal_ids), "goals DAG is acyclic (all 20 goals ordered)")
    check(goals.get("dag_summary", {}).get("total_goals") == 20, "dag_summary.total_goals = 20")

    # ------------------------------------------------------------------
    # 6. Plan v1 core fields
    # ------------------------------------------------------------------
    check(plan.get("version") == "v1", "plan_v1.yaml version = v1")
    check(plan.get("status") == "FROZEN", "plan_v1.yaml status = FROZEN")
    check(plan.get("frozen_at") == FIXED_TIMESTAMP, "plan_v1.yaml frozen_at fixed timestamp")
    check(plan.get("parent_plan_version") is None, "plan_v1.yaml parent_plan_version = null")
    check(
        set(plan.get("goal_ids", [])) == goal_ids,
        "plan_v1.yaml goal_ids equal the goals.yaml goal set",
    )
    check(
        set(plan.get("requirement_ids", [])) == set(item_ids),
        "plan_v1.yaml requirement_ids equal the 82 inventory items",
    )
    ia = plan.get("inventory_audit") or {}
    check(ia.get("formally_reported_items") == 82, "plan inventory_audit: formally_reported_items = 82")
    check(ia.get("mapped_items") == 82, "plan inventory_audit: mapped_items = 82")
    check(ia.get("unmapped_items") == 0, "plan inventory_audit: unmapped_items = 0")
    check(ia.get("ambiguous_items") == 0, "plan inventory_audit: ambiguous_items = 0")
    check(ia.get("coverage") == 1.0, "plan inventory_audit: coverage = 1.0")
    check(ia.get("status") == "PASS", "plan inventory_audit: status = PASS")
    check(plan.get("frozen_marker", {}).get("frozen_commit") is None, "plan frozen_marker.frozen_commit = null (recorded at merge)")

    # ------------------------------------------------------------------
    # 7. Assumptions (12 A2 + 18 CVR + 15 A1 = 45)
    # ------------------------------------------------------------------
    counts = assumptions.get("counts") or {}
    asm_list = assumptions.get("assumptions") or []
    asm_ids = [a.get("assumption_id") for a in asm_list]
    check(len(asm_list) == 45, "assumptions.yaml has 45 objects")
    check(len(set(asm_ids)) == len(asm_ids), "assumption ids are unique")
    check(counts.get("total") == 45, "assumptions counts.total = 45")
    check(counts.get("a2_inventory_registrations") == 12, "assumptions counts.a2 = 12")
    check(counts.get("ambiguity_coverage_references") == 18, "assumptions counts.cvr = 18")
    check(counts.get("plan_v1_methodological_defaults") == 15, "assumptions counts.a1 = 15")
    a2_asm = [a for a in asm_list if a["assumption_id"].startswith("ASM-FDM-")]
    check(len(a2_asm) == 12, "12 ASM-FDM-* A2 registrations")
    for a in a2_asm:
        check(a.get("classification") == "A2_SCIENTIFIC_ASSUMPTION", f"{a['assumption_id']}: classification A2")
        check(a.get("strict_status_effect") == "DISQUALIFIES_PURE_STRICT", f"{a['assumption_id']}: strict_status_effect DISQUALIFIES_PURE_STRICT")
        target = a["assumption_id"].replace("ASM-FDM-", "INV-")
        check(target in a.get("source_refs", []), f"{a['assumption_id']}: source_refs includes {target}")
    # Recompute the ambiguity-coverage warnings with the inventory
    # self_check.py rule: item with NO ambiguity_ref listed in an
    # ambiguity's affected_item_ids.
    amb_by_id = {a.get("ambiguity_id"): a for a in ambs}
    warnings: list[tuple[str, str]] = []
    for i in items:
        if i.get("ambiguity_ref"):
            continue
        for amb_id, amb in amb_by_id.items():
            if i["item_id"] in (amb.get("affected_item_ids") or []):
                warnings.append((i["item_id"], amb_id))
    expected_cvr = {f"ASM-CVR-{amb}-{item}" for item, amb in warnings}
    actual_cvr = {a["assumption_id"] for a in asm_list if a["assumption_id"].startswith("ASM-CVR-")}
    check(len(warnings) == 18, "inventory self_check rule yields exactly 18 ambiguity-coverage warnings")
    check(actual_cvr == expected_cvr, "ASM-CVR-* set equals the recomputed 18 warnings (1:1, none extra)")
    for a in asm_list:
        if a["assumption_id"].startswith("ASM-CVR-"):
            check(a.get("classification") == "A1_METHODOLOGICAL_DEFAULT", f"{a['assumption_id']}: classification A1")
            check(a.get("strict_status_effect") == "NONE", f"{a['assumption_id']}: strict_status_effect NONE")
    a1_asm = [a for a in asm_list if a["assumption_id"].startswith("ASM-A1-")]
    check(len(a1_asm) == 15, "15 ASM-A1-* Plan v1 methodological defaults")

    # ------------------------------------------------------------------
    # 8. Evidence model + records
    # ------------------------------------------------------------------
    for basis, ar_d in BASIS_AR_D.items():
        entry = (model.get("basis_to_ar_d") or {}).get(basis) or {}
        check(
            (entry.get("authority"), entry.get("reliability"), entry.get("directness")) == ar_d,
            f"evidence_model basis {basis} A/R/D matches frozen mapping",
        )
    ev_records = evidence.get("evidence_records") or []
    check(len(ev_records) == 82, "evidence_records.yaml has 82 records")
    ev_ids = [r.get("evidence_id") for r in ev_records]
    check(len(set(ev_ids)) == len(ev_ids), "evidence ids are unique")
    prov_by_item = {i["item_id"]: i.get("provenance", {}) for i in items}
    for r in ev_records:
        item_id = r.get("claim_id")
        check(r.get("evidence_id") == f"EV-{item_id}", f"{r.get('evidence_id')}: evidence_id is EV-<claim_id>")
        check(item_id in prov_by_item, f"{r.get('evidence_id')}: claim_id exists in inventory")
        basis = prov_by_item[item_id].get("evidence_basis")
        expected = BASIS_AR_D.get(basis)
        check(expected is not None, f"{r.get('evidence_id')}: inventory basis {basis} is in the frozen mapping")
        assessment = r.get("assessment") or {}
        check(
            (assessment.get("authority"), assessment.get("reliability"), assessment.get("directness")) == expected,
            f"{r.get('evidence_id')}: A/R/D matches the frozen basis mapping",
        )
        check(set(r.get("used_by", [])) <= goal_ids, f"{r.get('evidence_id')}: used_by goals are defined")

    # ------------------------------------------------------------------
    # 9. Resources
    # ------------------------------------------------------------------
    res_list = resources.get("resources") or []
    check(len(res_list) == 17, "resources.yaml has 17 records")
    res_ids = [r.get("resource_id") for r in res_list]
    check(len(set(res_ids)) == len(res_ids), "resource ids are unique")
    acq_items = {a.get("item"): a for a in acq.get("items") or []}
    for r in res_list:
        check(r.get("resource_type") in RESOURCE_TYPES, f"{r['resource_id']}: resource_type valid")
        check(r.get("availability_state") in AVAILABILITY_STATES, f"{r['resource_id']}: availability_state valid")
        check(set(r.get("used_by_goal_ids", [])) <= goal_ids, f"{r['resource_id']}: used_by goals defined")
        check(set(r.get("blocks_goal_ids", [])) <= goal_ids, f"{r['resource_id']}: blocks goals defined")
        res_goal = r.get("resolution_goal_id")
        check(res_goal in goal_ids or res_goal == "PROCURE", f"{r['resource_id']}: resolution goal defined or PROCURE token")
        item = r.get("g01_acquisition_item")
        if item is not None:
            log_entry = acq_items.get(item)
            check(log_entry is not None, f"{r['resource_id']}: G01 acquisition item {item} exists")
            if log_entry is not None:
                check(r.get("acquisition_status") == log_entry.get("status"), f"{r['resource_id']}: acquisition_status matches G01 log")
                check(set(r.get("source_ids", [])) <= set(log_entry.get("source_ids", [])), f"{r['resource_id']}: source_ids subset of G01 item")
        else:
            check(r.get("acquisition_status") == "not_registered", f"{r['resource_id']}: non-G01 resource marked not_registered")

    # ------------------------------------------------------------------
    # 10. Analysis and closure objects
    # ------------------------------------------------------------------
    protocols = analysis.get("protocols") or []
    check(analysis.get("analysis_objects") == 10, "analysis_plan.yaml analysis_objects = 10")
    check(len(protocols) == 10, "analysis_plan.yaml has 10 protocols")
    check(len({p.get("analysis_id") for p in protocols}) == 10, "analysis ids are unique")
    check(all(p.get("frozen") is True for p in protocols), "all analysis protocols frozen")
    contracts = closure.get("closure_contracts") or []
    check(closure.get("contracts") == 4, "closure.yaml contracts = 4")
    check(len(contracts) == 4, "closure.yaml has 4 contracts")
    check(len({c.get("closure_id") for c in contracts}) == 4, "closure ids are unique")
    for c in contracts:
        check(set(c.get("applies_to", [])) <= goal_ids, f"{c['closure_id']}: applies_to goals are defined")
        check(c.get("frozen") is True, f"{c['closure_id']}: frozen true")

    # ------------------------------------------------------------------
    # 11. Placeholder scan of the deliverable data files
    # ------------------------------------------------------------------
    for path in sorted(TIMESTAMP_FIELDS):
        text = path.read_text(encoding="utf-8")
        hits = sorted(set(PLACEHOLDER_RE.findall(text)))
        check(not hits, f"{path.name}: no placeholder tokens {hits}")

    # ------------------------------------------------------------------
    # 12. Fixed-timestamp determinism
    # ------------------------------------------------------------------
    for path, field in TIMESTAMP_FIELDS.items():
        data = load_yaml(path)
        check(data.get(field) == FIXED_TIMESTAMP, f"{path.name}: {field} = fixed timestamp")
    check(inv.get("determinism", {}).get("fixed_timestamp") == FIXED_TIMESTAMP, "inventory determinism.fixed_timestamp")
    check(goals.get("determinism", {}).get("fixed_timestamp") == FIXED_TIMESTAMP, "goals determinism.fixed_timestamp")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    print(f"Checks run: {checks_run}, failures: {len(failures)}")
    print("Audit summary (computed):")
    print("  mapped_items                = 82")
    print("  unmapped_items              = 0")
    print("  unresolved_ambiguities      = 0")
    print("  coverage                    = 1.0")
    print("  goals_defined               = 20")
    print("  dag_acyclic_ordering_count  = " + str(len(ordered)))
    print("  assumptions                 = 45 (12 A2 + 18 CVR + 15 A1)")
    print("  evidence_records            = 82")
    print("  resource_records            = 17")
    print("  analysis_protocols          = 10")
    print("  closure_contracts           = 4")
    checks_fail(failures)
    return 0


if __name__ == "__main__":
    sys.exit(main())
