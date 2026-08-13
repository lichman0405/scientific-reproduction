"""Tests for the schema validation adapter (DEV-M1-G01, acceptance AC-03).

Covered behaviors:
  * valid example documents (from examples/fdm-201/ plus minimal fixtures)
    validate cleanly against the frozen schemas;
  * schema-invalid objects -- wrong type, missing required field, bad enum
    value, const violation, minItems, numeric range, pattern -- are rejected
    with non-empty error lists (rejected before persistence);
  * ``validate_and_reject`` raises ``SchemaValidationError`` on invalid
    objects and returns cleanly on valid ones;
  * missing/unknown schema files produce clear errors; loading is lazy and
    cached; the schemas directory is overridable via the environment.
"""

from __future__ import annotations

import copy

import pytest
from fixtures import FDM201_EXAMPLES_DIR, VALID_DOCS

from scientific_reproduction.core import models as m
from scientific_reproduction.core.schema_validation import (
    SCHEMAS_DIR_ENV,
    SchemaNotFoundError,
    SchemaValidationError,
    load_schema,
    schemas_dir,
    validate_and_reject,
    validate_object,
)


@pytest.mark.parametrize("obj_type", sorted(VALID_DOCS))
def test_valid_documents_pass_validation(obj_type: str) -> None:
    assert validate_object(obj_type, VALID_DOCS[obj_type]) == []


@pytest.mark.parametrize("obj_type", sorted(VALID_DOCS))
def test_model_serialization_remains_schema_valid(obj_type: str) -> None:
    # Every model's to_dict() output must itself be schema-valid (AC-01 +
    # AC-03: serialized canonical content is what gets persisted).
    model = m.MODEL_REGISTRY[obj_type].from_dict(copy.deepcopy(VALID_DOCS[obj_type]))
    assert validate_object(obj_type, model.to_dict()) == []


def test_example_documents_from_fdm201_validate() -> None:
    # The seven frozen reference-case example files are valid fixtures.
    for obj_type in [
        "project",
        "goal",
        "evidence",
        "assumption",
        "inventory-item",
        "acceptance-criteria",
        "research-request",
    ]:
        assert validate_object(obj_type, VALID_DOCS[obj_type]) == []


def test_raw_goal_example_inconsistency_is_caught_by_validation() -> None:
    # Known frozen-spec inconsistency: examples/fdm-201/goal.example.yaml
    # lists bare strings under "outputs", while schemas/goal.schema.yaml
    # requires items: {type: object}. The schema is normative, so the raw
    # example must FAIL validation on exactly that field (and the fixtures
    # module normalizes it for use as a valid document).
    import yaml as _yaml

    raw = _yaml.safe_load(
        (
            FDM201_EXAMPLES_DIR / "goal.example.yaml"
        ).read_text(encoding="utf-8")
    )
    errors = validate_object("goal", raw)
    assert errors
    assert any("outputs" in error for error in errors)


INVALID_CASES: dict[str, tuple[str, dict, str]] = {
    "wrong_type": (
        "project",
        {"project_id": 123, "primary_target": {"source_type": "doi", "identifier": "x"},
         "project_phase": "PLANNING", "reproduction_outcome": "UNDETERMINED",
         "current_plan_version": "v1"},
        "project_id",
    ),
    "missing_required_field": (
        "project",
        {"primary_target": {"source_type": "doi", "identifier": "x"},
         "project_phase": "PLANNING", "reproduction_outcome": "UNDETERMINED",
         "current_plan_version": "v1"},
        "project_id",
    ),
    "bad_enum_value": (
        "project",
        {"project_id": "P1", "primary_target": {"source_type": "doi", "identifier": "x"},
         "project_phase": "BOGUS_PHASE", "reproduction_outcome": "UNDETERMINED",
         "current_plan_version": "v1"},
        "project_phase",
    ),
    "nested_required_missing": (
        "project",
        {"project_id": "P1", "primary_target": {"source_type": "doi"},
         "project_phase": "PLANNING", "reproduction_outcome": "UNDETERMINED",
         "current_plan_version": "v1"},
        "identifier",
    ),
    "min_items_violation": (
        "goal",
        {"goal_id": "G1", "title": "t", "unit_process_type": "u",
         "track": "STRICT_REPRODUCTION", "objective": "o", "requirement_ids": [],
         "dependencies": [], "acceptance": {"criteria_ref": "A", "frozen": False},
         "analysis_protocol_ref": "P", "replication": {"independent_required": True,
                                                       "planned_n_policy": "p"},
         "version": "v1", "frozen": False},
        "requirement_ids",
    ),
    "numeric_range_exceeded": (
        "evidence",
        {"evidence_id": "E1", "source_id": "S1", "claim_id": "C1", "finding": "f",
         "assessment": {"authority": 9, "reliability": 2, "directness": 3,
                        "reliability_checklist_ref": "R"}},
        "authority",
    ),
    "numeric_range_negative": (
        "artifact-manifest",
        {"artifact_id": "A1", "uri": "file:///x", "sha256": "a" * 64,
         "size_bytes": -1, "created_at": "2026-01-01T00:00:00Z"},
        "size_bytes",
    ),
    "pattern_violation": (
        "artifact-manifest",
        {"artifact_id": "A1", "uri": "file:///x", "sha256": "not-a-sha256",
         "size_bytes": 10, "created_at": "2026-01-01T00:00:00Z"},
        "sha256",
    ),
    "const_violation_decision": (
        "decision",
        {"decision_id": "D1", "decision_type": "PLAN_FREEZE", "actor": "human",
         "timestamp": "2026-01-01T00:00:00Z", "affected_refs": [], "rationale": "r"},
        "actor",
    ),
    "const_violation_requested_by": (
        "research-request",
        {"request_id": "RR1", "requested_by": "user", "question": "q",
         "origin_refs": [], "status": "OPEN"},
        "requested_by",
    ),
    "maximum_exceeded": (
        "plan",
        {"plan_id": "P1", "version": "v1", "status": "DRAFT",
         "inventory_audit": {"formally_reported_items": 1, "mapped_items": 1,
                             "unmapped_items": 0, "ambiguous_items": 0,
                             "coverage": 1.5},
         "goal_ids": [], "requirement_ids": []},
        "coverage",
    ),
    "bad_enum_run_state": (
        "run",
        {"run_id": "R1", "goal_id": "G1", "goal_version": "v1",
         "run_type": "independent_replicate", "lifecycle_state": "DONE"},
        "lifecycle_state",
    ),
    "wrong_type_run_type": (
        "run",
        {"run_id": "R1", "goal_id": "G1", "goal_version": "v1",
         "run_type": 42, "lifecycle_state": "CREATED"},
        "run_type",
    ),
}


@pytest.mark.parametrize(
    "obj_type,doc,expected_fragment", list(INVALID_CASES.values()),
    ids=list(INVALID_CASES),
)
def test_schema_invalid_objects_are_rejected(
    obj_type: str, doc: dict, expected_fragment: str
) -> None:
    errors = validate_object(obj_type, doc)
    assert errors, f"{obj_type}: invalid document was accepted"
    assert any(expected_fragment in error for error in errors), errors


def test_validate_and_reject_raises_for_invalid_object() -> None:
    doc = copy.deepcopy(VALID_DOCS["project"])
    doc["project_phase"] = "BOGUS_PHASE"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_and_reject("project", doc)
    assert exc_info.value.obj_type == "project"
    assert exc_info.value.errors
    assert "project_phase" in " ".join(exc_info.value.errors)


def test_validate_and_reject_passes_for_valid_object() -> None:
    assert validate_and_reject("project", VALID_DOCS["project"]) is None


def test_validate_and_reject_passes_for_model_serialization() -> None:
    model = m.Project.from_dict(copy.deepcopy(VALID_DOCS["project"]))
    assert validate_and_reject("project", model.to_dict()) is None


def test_unknown_object_type_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="unknown object type"):
        load_schema("no-such-object")
    with pytest.raises(ValueError, match="unknown object type"):
        validate_object("no-such-object", {})


def test_missing_schema_file_raises_clear_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(tmp_path))
    load_schema.cache_clear()
    try:
        with pytest.raises(SchemaNotFoundError, match="project.schema.yaml"):
            validate_object("project", {})
    finally:
        load_schema.cache_clear()


def test_schemas_dir_env_override_redirects(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(SCHEMAS_DIR_ENV, str(tmp_path))
    load_schema.cache_clear()
    try:
        assert schemas_dir() == tmp_path
        with pytest.raises(SchemaNotFoundError):
            validate_object("project", {})
    finally:
        load_schema.cache_clear()


def test_schemas_dir_resolution_points_at_repo_schemas() -> None:
    assert schemas_dir().is_dir()
    assert (schemas_dir() / "project.schema.yaml").is_file()


def test_load_schema_is_cached() -> None:
    first = load_schema("project")
    second = load_schema("project")
    assert first is second


def test_schema_files_are_draft_2020_12() -> None:
    schema = load_schema("project")
    assert "$schema" in schema
    assert "2020-12" in schema["$schema"]
