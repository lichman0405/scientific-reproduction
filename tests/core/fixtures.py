"""Shared valid example documents for the 22 normative object types.

Eight of the documents are loaded from the frozen FDM-201 reference-case
example files in ``examples/fdm-201/``; the remaining fourteen are minimal
hand-written documents that satisfy the corresponding frozen schema in
``schemas/`` (required fields present, enum values from the schema enums).

These documents are used by ``test_models.py`` (round-trip/serialization)
and ``test_schema_validation.py`` (valid documents pass validation).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FDM201_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "fdm-201"


def _load_example(filename: str) -> dict[str, Any]:
    loaded = yaml.safe_load((FDM201_EXAMPLES_DIR / filename).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise AssertionError(f"example {filename} is not a mapping")
    return loaded


def _goal_example_normalized() -> dict[str, Any]:
    """Load examples/fdm-201/goal.example.yaml and make it schema-conformant.

    Known frozen-spec inconsistency (noted in the DEV-M1-G01 PR): the
    example's ``outputs`` list contains bare strings, while
    ``schemas/goal.schema.yaml`` requires ``items: {type: object}``. The
    schema is normative, so the example items are wrapped as objects here;
    ``test_schema_validation.py`` documents that the *raw* example fails
    validation on exactly that field.
    """
    doc = _load_example("goal.example.yaml")
    doc["outputs"] = [{"name": item} for item in doc["outputs"]]
    return doc


VALID_DOCS: dict[str, dict[str, Any]] = {
    # --- documents loaded from the frozen FDM-201 example files ---
    "project": _load_example("project.example.yaml"),
    "goal": _goal_example_normalized(),
    "evidence": _load_example("evidence.example.yaml"),
    "assumption": _load_example("assumption.example.yaml"),
    "inventory-item": _load_example("inventory.example.yaml"),
    "acceptance-criteria": _load_example("acceptance.example.yaml"),
    "statistical-design": _load_example("statistical-design.example.yaml"),
    "research-request": _load_example("research-request.example.yaml"),
    # --- minimal hand-written documents for the remaining types ---
    "run": {
        "run_id": "RUN-001",
        "goal_id": "GOAL-001",
        "goal_version": "v1",
        "run_type": "independent_replicate",
        "lifecycle_state": "CREATED",
        "scientific_review": "UNREVIEWED",
        "artifacts": [],
    },
    "plan": {
        "plan_id": "PLAN-001",
        "version": "v1-draft",
        "status": "DRAFT",
        "inventory_audit": {
            "formally_reported_items": 5,
            "mapped_items": 5,
            "unmapped_items": 0,
            "ambiguous_items": 0,
            "coverage": 1.0,
            "status": "PASS",
        },
        "goal_ids": ["GOAL-001"],
        "requirement_ids": ["REQ-001"],
    },
    "closure-contract": {
        "closure_id": "CLC-001",
        "frozen": False,
        "statistical_sufficiency": {"note": "pending final analysis"},
        "execution_validity": {"note": "pending"},
        "diagnosis": {"note": "pending"},
        "recovery": {
            "eligibility_rule": {"rule": "rule-001"},
            "eligible_hypotheses_total": 2,
            "tested_or_ruled_out": 0,
            "remaining": 2,
        },
        "literature": {
            "required_search_families_completed": False,
            "consecutive_zero_novelty_cycles": 0,
            "required_zero_novelty_cycles": 2,
        },
        "closure_allowed": False,
    },
    "resource": {
        "resource_id": "RES-001",
        "name": "C3H6 gas cylinder",
        "resource_type": "reagent",
        "availability_state": "AVAILABLE",
        "human_gate_required": False,
    },
    "source": {
        "source_id": "SRC-001",
        "source_type": "target_paper",
        "title": "FDM-201 propylene/ethylene separation paper",
        "provenance": "seed record from project bootstrap",
        "doi": "10.1039/D5TA00771B",
        "access_class": "PUBLIC",
    },
    "worker-context": {
        "context_id": "CTX-001",
        "worker_role": "experiment_worker",
        "goal_id": "GOAL-001",
        "goal_version": "v1",
        "allowed_actions": ["record_result"],
        "forbidden_actions": ["close_run"],
    },
    "lab-execution-package": {
        "package_id": "PKG-001",
        "project_id": "RP-001",
        "goal_id": "GOAL-001",
        "run_id": "RUN-001",
        "track": "STRICT_REPRODUCTION",
        "objective": "measure single-component C3H6 isotherm at 298 K",
        "procedure": [{"step": "activate sample"}],
        "required_return": ["raw_isotherm_data"],
    },
    "analysis": {
        "analysis_id": "ANL-001",
        "kind": "protocol",
        "profile": "ROUTINE_ANALYSIS",
        "primary_or_exploratory": "PRIMARY",
        "protocol_version": "v1",
        "frozen": False,
        "methods": [{"name": "equivalence_test"}],
        "warnings": [],
    },
    "decision": {
        "decision_id": "DEC-001",
        "decision_type": "PLAN_FREEZE",
        "actor": "supervisor",
        "timestamp": "2026-01-01T00:00:00Z",
        "affected_refs": ["PLAN-001"],
        "rationale": "plan audited and frozen",
    },
    "human-gate": {
        "gate_id": "GATE-001",
        "gate_type": "SAFETY_GATE",
        "status": "OPEN",
        "trigger": "new solvent outside declared scope",
        "affected_refs": ["GOAL-001"],
        "default_safe_action": "pause",
    },
    "event": {
        "event_id": "EV-001",
        "timestamp": "2026-01-01T00:00:00Z",
        "actor": "supervisor",
        "event_type": "plan.frozen",
        "from": "DRAFT",
        "to": "FROZEN",
        "payload": {"plan_id": "PLAN-001"},
    },
    "retry-policy": {
        "policy_id": "RETRY-001",
        "allowed_engineering_failures": ["instrument_timeout"],
        "max_identical_retries": 1,
        "supervisor_required_changes": ["change_procedure"],
        "invalidate_run_on": ["data_corruption"],
    },
    "requirement": {
        "requirement_id": "REQ-001",
        "statement": "reproduce the reported 298 K C3H6 isotherm",
        "inventory_items": ["INV-001"],
        "criticality": "CRITICAL",
        "goal_ids": ["GOAL-001"],
        "outcome": "OPEN",
        "method_reproducibility": "UNDETERMINED",
    },
    "artifact-manifest": {
        "artifact_id": "ART-001",
        "run_id": "RUN-001",
        "uri": "file:///data/isotherm.csv",
        "sha256": "a" * 64,
        "size_bytes": 1024,
        "mime_type": "text/csv",
        "created_at": "2026-01-01T00:00:00Z",
        "producer": "experiment_worker",
        "metadata": {"format": "csv"},
    },
}
