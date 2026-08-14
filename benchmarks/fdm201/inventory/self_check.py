#!/usr/bin/env python
"""FDM-201 inventory integrity self-check (DEV-M12-G02).

Deterministic, offline gate that validates benchmarks/fdm201/inventory/INVENTORY.yaml
against the G01 source records and acquisition log. No wall clock, no
randomness, no network. Run from anywhere:

    python benchmarks/fdm201/inventory/self_check.py

Exits 0 when every check passes, 1 otherwise. Prints one PASS/FAIL line per
check plus the computed inventory summary.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
INVENTORY_PATH = REPO_ROOT / "benchmarks" / "fdm201" / "inventory" / "INVENTORY.yaml"
RECORDS_DIR = REPO_ROOT / "benchmarks" / "fdm201" / "sources" / "records"
ACQUISITION_LOG_PATH = REPO_ROOT / "benchmarks" / "fdm201" / "sources" / "ACQUISITION_LOG.yaml"

FIXED_TIMESTAMP = "2026-08-14T00:00:00Z"
PLACEHOLDER_RE = re.compile(r"\b(TBD|TODO|FIXME|XXX)\b", re.IGNORECASE)
PLACEHOLDER_WORDS = ("tbd", "todo", "fixme", "lorem", "placeholder")
MISSING_TEXT_MARKERS = ("fill in", "insert text", "to be filled")


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {path} did not parse to a mapping")
    return data


def checks_fail(failures: list[str]) -> None:
    if failures:
        print("\nFAILED CHECKS:")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)


def main() -> int:
    failures: list[str] = []
    checks_run = 0

    def check(ok: bool, label: str) -> None:
        nonlocal checks_run
        checks_run += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            failures.append(label)

    # ------------------------------------------------------------------
    print("FDM-201 inventory self-check")
    print(f"repo root: {REPO_ROOT}")
    print(f"inventory: {INVENTORY_PATH}")
    print()

    # --- 1. Parse the inventory ----------------------------------------
    inventory = load_yaml(INVENTORY_PATH)
    check(inventory.get("record_kind") == "benchmark_reported_item_inventory", "record_kind is benchmark_reported_item_inventory")
    check(inventory.get("goal_id") == "DEV-M12-G02", "goal_id is DEV-M12-G02")
    check(inventory.get("benchmark_id") == "fdm201", "benchmark_id is fdm201")
    check(inventory.get("inventoried_at") == FIXED_TIMESTAMP, f"inventoried_at is fixed timestamp {FIXED_TIMESTAMP}")
    check(inventory.get("determinism", {}).get("fixed_timestamp") == FIXED_TIMESTAMP, "determinism.fixed_timestamp matches")

    # --- 2. Source records: every referenced source id exists -----------
    record_ids = set()
    if RECORDS_DIR.is_dir():
        for record_path in sorted(RECORDS_DIR.glob("*.yaml")):
            record = load_yaml(record_path)
            sid = record.get("source_id")
            if sid:
                record_ids.add(sid)
    check(len(record_ids) == 13, f"13 source records loaded (found {len(record_ids)})")

    referenced_ids: set[str] = set()
    for item in inventory.get("items", []):
        for sid in item.get("provenance", {}).get("source_ids", []):
            referenced_ids.add(sid)
    unknown_ids = sorted(referenced_ids - record_ids)
    check(not unknown_ids, f"every referenced source id exists in records (unknown: {unknown_ids or 'none'})")

    # --- 3. Item structure / enum checks --------------------------------
    items = inventory.get("items", [])
    status_enum = inventory.get("status_enum", [])
    value_status_enum = inventory.get("value_status_enum", [])
    existence_basis_enum = inventory.get("existence_basis_enum", [])
    evidence_basis_enum = inventory.get("evidence_basis_enum", [])
    category_ids = [c["id"] for c in inventory.get("categories", [])]

    item_ids = [item.get("item_id") for item in items]
    check(len(item_ids) == len(set(item_ids)), "item ids are unique")

    for item in items:
        ok = item.get("status") in status_enum
        check(ok, f"{item.get('item_id')}: status '{item.get('status')}' in status_enum")
        if not ok:
            continue
        check(item.get("category") in category_ids, f"{item.get('item_id')}: category '{item.get('category')}' valid")
        check(item.get("value_status") in value_status_enum, f"{item.get('item_id')}: value_status in enum")
        check(item.get("existence_basis") in existence_basis_enum, f"{item.get('item_id')}: existence_basis in enum")
        check(item.get("provenance", {}).get("evidence_basis") in evidence_basis_enum, f"{item.get('item_id')}: provenance.evidence_basis in enum")
        check(item.get("provenance", {}).get("source_ids"), f"{item.get('item_id')}: provenance.source_ids non-empty")
        check(bool(item.get("requirement_mapping")), f"{item.get('item_id')}: requirement_mapping non-empty")

    # --- 4. Status semantics --------------------------------------------
    for item in items:
        item_id = item.get("item_id")
        status = item.get("status")
        if status == "INVENTORIED":
            check(item.get("revalidation_required") is False, f"{item_id}: INVENTORIED items require no revalidation")
        if status in ("INVENTORIED-DEFERRED", "DEFERRED-TO-EXECUTION"):
            check(item.get("revalidation_required") is True, f"{item_id}: deferred items require revalidation")
        if status == "DEFERRED-TO-EXECUTION":
            check(bool(item.get("missing_reason")) or bool(item.get("resolves_in")),
                  f"{item_id}: DEFERRED-TO-EXECUTION has missing_reason and/or resolves_in")
        if item.get("value_status") == "MISSING":
            check(bool(item.get("missing_reason")), f"{item_id}: MISSING value carries missing_reason")
            check(not re.search(r"\bA1\b|\bA0\b", str(item.get("assumption_class") or "")),
                  f"{item_id}: MISSING consequential parameter registered as A2 (found {item.get('assumption_class')})")
        if item.get("value_status") == "REPORTED-NON-FINAL":
            check(item.get("revalidation_required") is True, f"{item_id}: REPORTED-NON-FINAL value requires revalidation")

    # --- 5. Placeholder scan (whole file text) --------------------------
    raw = INVENTORY_PATH.read_text(encoding="utf-8")
    placeholder_hits = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        lowered = line.lower()
        if PLACEHOLDER_RE.search(line):
            placeholder_hits.append((line_no, "regex", line.strip()))
        for word in PLACEHOLDER_WORDS:
            if word in lowered:
                placeholder_hits.append((line_no, word, line.strip()))
        for marker in MISSING_TEXT_MARKERS:
            if marker in lowered:
                placeholder_hits.append((line_no, marker, line.strip()))
    check(not placeholder_hits, f"no placeholder text (TBD/TODO/FIXME/placeholder/lorem): {placeholder_hits or 'none'}")

    # --- 6. Ambiguity entries -------------------------------------------
    ambiguities = inventory.get("ambiguities", [])
    amb_ids = [a.get("ambiguity_id") for a in ambiguities]
    check(len(amb_ids) == len(set(amb_ids)), "ambiguity ids are unique")
    for amb in ambiguities:
        affected = amb.get("affected_item_ids", [])
        unknown = [i for i in affected if i not in item_ids]
        check(not unknown, f"{amb.get('ambiguity_id')}: affected item ids exist (unknown: {unknown or 'none'})")
        check(bool(amb.get("resolves_in")), f"{amb.get('ambiguity_id')}: resolves_in non-empty")
        check(bool(amb.get("ambiguity_note")), f"{amb.get('ambiguity_id')}: ambiguity_note present")

    # --- 6b. Bidirectional ambiguity integrity ---------------------------
    # Forward (hard): every item.ambiguity_ref must name an existing ambiguity
    # that lists the item in its affected_item_ids.
    # Backward, closed (hard): an item that carries ANY ambiguity_ref must
    # reference back every ambiguity that lists it.
    # Backward, open (warning only): an item with NO ambiguity_ref that is
    # listed in an ambiguity's affected_item_ids gets a non-fatal warning
    # (the ambiguity documents the item, the item does not document the
    # ambiguity; this is tolerated for coverage-style references).
    amb_by_id = {a.get("ambiguity_id"): a for a in ambiguities}
    warnings: list[str] = []
    for item in items:
        item_id = item.get("item_id")
        refs = item.get("ambiguity_ref") or []
        for ref in refs:
            amb = amb_by_id.get(ref)
            if amb is None:
                check(False, f"{item_id}: ambiguity_ref '{ref}' names a nonexistent ambiguity")
            else:
                check(item_id in amb.get("affected_item_ids", []),
                      f"{item_id}: ambiguity_ref '{ref}' must list {item_id} in affected_item_ids")
        if refs:
            for amb_id, amb in amb_by_id.items():
                if item_id in amb.get("affected_item_ids", []) and amb_id not in refs:
                    check(False, f"{item_id}: ambiguity '{amb_id}' lists {item_id} but is not referenced back")
        else:
            for amb_id, amb in amb_by_id.items():
                if item_id in amb.get("affected_item_ids", []):
                    warnings.append(f"{item_id} is affected by ambiguity '{amb_id}' but carries no ambiguity_ref")
    for warning in warnings:
        print(f"[WARN] {warning}")

    # --- 7. Summary block vs computed counts ----------------------------
    def count_items(pred):
        return sum(1 for item in items if pred(item))

    by_category = {cid: count_items(lambda i, c=cid: i.get("category") == c) for cid in category_ids}
    by_status = {s: count_items(lambda i, s=s: i.get("status") == s) for s in status_enum}
    by_existence = {b: count_items(lambda i, b=b: i.get("existence_basis") == b) for b in existence_basis_enum}
    by_evidence = {b: count_items(lambda i, b=b: i.get("provenance", {}).get("evidence_basis") == b) for b in evidence_basis_enum}
    seed_fact_count = count_items(lambda i: i.get("seed_fact_sourced") is True)
    quantitative_count = count_items(lambda i: i.get("value_status") == "REPORTED-NON-FINAL")
    non_final_count = count_items(lambda i: i.get("revalidation_required") is True)
    missing_count = count_items(lambda i: i.get("value_status") == "MISSING")

    summary = inventory.get("summary", {})
    check(len(items) == summary.get("total_items"), f"total items {len(items)} == summary.total_items {summary.get('total_items')}")
    check(by_category == summary.get("by_category"), f"by_category {by_category} matches summary")
    check(by_status == summary.get("by_status"), f"by_status {by_status} matches summary")
    check(by_existence == summary.get("by_existence_basis"), f"by_existence_basis {by_existence} matches summary")
    check(by_evidence == summary.get("by_evidence_basis"), f"by_evidence_basis {by_evidence} matches summary")
    check(len(ambiguities) == summary.get("ambiguities"), f"ambiguities {len(ambiguities)} == summary.ambiguities {summary.get('ambiguities')}")
    check(seed_fact_count == summary.get("seed_fact_sourced_items"), f"seed-fact items {seed_fact_count} == summary {summary.get('seed_fact_sourced_items')}")
    check(quantitative_count == summary.get("reported_non_final_items"), f"reported non-final items {quantitative_count} == summary {summary.get('reported_non_final_items')}")
    check(non_final_count == summary.get("non_final_items"), f"non-final items {non_final_count} == summary {summary.get('non_final_items')}")
    check(missing_count == summary.get("missing_value_parameters"), f"missing-value parameters {missing_count} == summary {summary.get('missing_value_parameters')}")

    # --- 8. G01 acquisition log consistency -----------------------------
    log = load_yaml(ACQUISITION_LOG_PATH)
    log_items = log.get("items", [])
    log_by_status = {}
    for log_entry in log_items:
        status = log_entry.get("status")
        log_by_status[status] = log_by_status.get(status, 0) + 1
    log_inventory = log.get("inventory", {})
    check(len(log_items) == log_inventory.get("total_items"), f"G01 log total {len(log_items)} == {log_inventory.get('total_items')}")
    check(log_by_status.get("REGISTERED") == log_inventory.get("REGISTERED"), "G01 log REGISTERED count consistent")
    check(log_by_status.get("PARTIAL") == log_inventory.get("PARTIAL"), "G01 log PARTIAL count consistent")
    check(log_by_status.get("UNAVAILABLE") == log_inventory.get("UNAVAILABLE"), "G01 log UNAVAILABLE count consistent")

    log_totals = inventory.get("source_set", {}).get("acquisition_log_totals", {})
    check(log_totals.get("total_items") == 9, "inventory declares G01 log total 9")
    check(log_totals.get("REGISTERED") == 2 and log_totals.get("PARTIAL") == 4 and log_totals.get("UNAVAILABLE") == 3,
          "inventory declares G01 log totals 2 REGISTERED / 4 PARTIAL / 3 UNAVAILABLE")

    g01_cross = inventory.get("g01_cross_reference", [])
    check(len(g01_cross) == 9, "g01_cross_reference covers all 9 acquisition-log items")
    for entry in g01_cross:
        check(entry.get("inventory_items"), f"g01_cross_reference item {entry.get('acquisition_item')}: inventory_items non-empty")
        known_items = entry.get("inventory_items", [])
        unknown = [i for i in known_items if i not in item_ids]
        check(not unknown, f"g01_cross_reference item {entry.get('acquisition_item')}: referenced item ids exist (unknown: {unknown or 'none'})")

    # --- 9. Determinism ---------------------------------------------------
    check(inventory.get("determinism", {}).get("wall_clock") == "none"
          and inventory.get("determinism", {}).get("randomness") == "none"
          and inventory.get("determinism", {}).get("network") == "none",
          "determinism block declares no wall clock / randomness / network")

    # ----------------------------------------------------------------------
    print()
    print(f"Checks run: {checks_run}, failures: {len(failures)}, warnings: {len(warnings)}")
    print()
    print("Inventory summary (computed):")
    print(f"  total_items            = {len(items)}")
    print(f"  by_category            = {by_category}")
    print(f"  by_status              = {by_status}")
    print(f"  by_existence_basis     = {by_existence}")
    print(f"  by_evidence_basis      = {by_evidence}")
    print(f"  ambiguities            = {len(ambiguities)}")
    print(f"  seed_fact_sourced      = {seed_fact_count}")
    print(f"  reported non-final     = {quantitative_count}")
    print(f"  non_final items        = {non_final_count}")
    print(f"  missing-value params   = {missing_count}")

    if failures:
        print("\nRESULT: FAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
