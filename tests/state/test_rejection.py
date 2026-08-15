"""Schema-invalid write rejection tests (DEV-M1-G05, AC-02).

AC-02 requires that a schema-invalid write raises
``SchemaValidationError`` and NOTHING becomes canonical state. Coverage,
over a representative set of object types:

* rejection classes: invalid enum values, missing required fields (at
  the root and inside a nested sub-object), wrong property types, a
  string-pattern violation, and a minItems violation;
* nothing persisted: no record file, no partial file, no type directory
  structure at all;
* an earlier valid write of the SAME object_id stays untouched and
  byte-identical, ``list_ids`` unchanged;
* non-dict content raises ``TypeError`` (the interface contract) and
  persists nothing;
* unknown extra keys: the frozen schemas all declare
  ``additionalProperties: true``, so an extra key is schema-valid today
  and must round-trip unchanged (never silently stripped, never a
  rejection). A schema scan pins this posture so that tightening a
  schema to ``additionalProperties: false`` fails loudly instead of
  silently changing behavior.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from scientific_reproduction.core import models as m
from scientific_reproduction.core.schema_validation import SchemaValidationError
from scientific_reproduction.core.state_backend import (
    SCHEMA_TO_STATE_DIR,
    FilesystemStateBackend,
)
from tests.core.fixtures import VALID_DOCS


def _id_field(obj_type: str) -> str:
    """Name of the identity field of the model for ``obj_type``.

    Every model declares its ID as its first dataclass field, named
    ``<something>_id`` (mirroring the schema's identity property). The
    assertion pins that assumption so a future reorder fails loudly.
    """
    field_name = dataclasses.fields(m.MODEL_REGISTRY[obj_type])[0].name
    assert field_name.endswith("_id"), field_name
    return field_name


def _derive(obj_type: str, **changes: Any) -> dict[str, Any]:
    """A copy of the valid fixture doc for ``obj_type`` with overrides."""
    doc = copy.deepcopy(VALID_DOCS[obj_type])
    doc.update(changes)
    return doc


def _drop(obj_type: str, key: str) -> dict[str, Any]:
    """A copy of the valid fixture doc for ``obj_type`` minus ``key``."""
    doc = copy.deepcopy(VALID_DOCS[obj_type])
    del doc[key]
    return doc


#: name -> (obj_type, schema-invalid document, expected error fragment).
#: Every invalid document is derived from the VALID fixture so the
#: object_id matches the valid write it is tested against.
INVALID_WRITES: dict[str, tuple[str, dict[str, Any], str]] = {
    "bad_enum_run_type": (
        "run",
        _derive("run", run_type="bogus"),
        "run_type",
    ),
    "bad_enum_lifecycle_state": (
        "run",
        _derive("run", lifecycle_state="BOGUS"),
        "lifecycle_state",
    ),
    "bad_enum_criticality": (
        "requirement",
        _derive("requirement", criticality="BOGUS"),
        "criticality",
    ),
    "bad_enum_resource_type": (
        "resource",
        _derive("resource", resource_type="bogus"),
        "resource_type",
    ),
    "bad_enum_plan_status": (
        "plan",
        _derive("plan", status="NOT_A_STATUS"),
        "status",
    ),
    "missing_required_timestamp": (
        "event",
        _drop("event", "timestamp"),
        "timestamp",
    ),
    "missing_required_objective": (
        "goal",
        _drop("goal", "objective"),
        "objective",
    ),
    "missing_required_nested_assessment": (
        "evidence",
        _drop("evidence", "assessment"),
        "assessment",
    ),
    "wrong_type_size_bytes": (
        "artifact-manifest",
        _derive("artifact-manifest", size_bytes="1024"),
        "size_bytes",
    ),
    "wrong_type_plan_status": (
        "plan",
        _derive("plan", status=7),
        "status",
    ),
    "pattern_violation_sha256": (
        "artifact-manifest",
        _derive("artifact-manifest", sha256="not-a-sha256"),
        "sha256",
    ),
    "empty_inventory_items": (
        "requirement",
        _derive("requirement", inventory_items=[]),
        "inventory_items",
    ),
}


@pytest.mark.parametrize(
    "obj_type,doc,error_fragment",
    list(INVALID_WRITES.values()),
    ids=list(INVALID_WRITES),
)
def test_schema_invalid_write_is_rejected_and_nothing_persisted(
    obj_type: str, doc: dict, error_fragment: str, tmp_path
) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    object_id = doc[_id_field(obj_type)]

    with pytest.raises(SchemaValidationError) as exc_info:
        backend.write(obj_type, object_id, doc)
    assert exc_info.value.obj_type == obj_type
    assert exc_info.value.errors
    assert any(error_fragment in e for e in exc_info.value.errors)

    # AC-02: nothing becomes canonical state -- no record file, no
    # partial file, no directory structure at all.
    assert not backend.exists(obj_type, object_id)
    assert backend.list_ids(obj_type) == []
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "obj_type,doc,error_fragment",
    list(INVALID_WRITES.values()),
    ids=list(INVALID_WRITES),
)
def test_schema_invalid_rewrite_leaves_previous_object_byte_identical(
    obj_type: str, doc: dict, error_fragment: str, tmp_path
) -> None:
    """AC-02 with a prior valid write of the same object_id: the invalid
    write is rejected and the earlier valid object is untouched
    byte-identically.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    valid_doc = copy.deepcopy(VALID_DOCS[obj_type])
    object_id = valid_doc[_id_field(obj_type)]

    backend.write(obj_type, object_id, valid_doc)
    path = (
        tmp_path / "state" / SCHEMA_TO_STATE_DIR[obj_type] / f"{object_id}.json"
    )
    before = path.read_bytes()

    with pytest.raises(SchemaValidationError) as exc_info:
        backend.write(obj_type, object_id, doc)
    assert exc_info.value.obj_type == obj_type
    assert exc_info.value.errors
    assert any(error_fragment in e for e in exc_info.value.errors)

    # The earlier valid write is byte-identical, list_ids unchanged, and
    # no partial or temp file appeared next to it.
    assert path.read_bytes() == before
    assert backend.read(obj_type, object_id) == valid_doc
    assert backend.list_ids(obj_type) == [object_id]
    type_dir = tmp_path / "state" / SCHEMA_TO_STATE_DIR[obj_type]
    assert sorted(p.name for p in type_dir.iterdir()) == [f"{object_id}.json"]


@pytest.mark.parametrize(
    "non_dict",
    [["not", "a", "dict"], ("tuple", "payload"), "plain string", 42, None],
    ids=["list", "tuple", "str", "int", "none"],
)
def test_non_dict_content_raises_type_error_and_persists_nothing(
    tmp_path, non_dict
) -> None:
    """Non-dict content is refused with TypeError (the interface contract)
    before anything touches the filesystem.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(TypeError, match="must be a dict"):
        backend.write("project", "P1", non_dict)  # type: ignore[arg-type]
    assert not backend.exists("project", "P1")
    assert backend.list_ids("project") == []
    assert not (tmp_path / "state").exists()


def test_extra_unknown_key_is_permitted_and_round_trips(tmp_path) -> None:
    """The frozen schemas declare ``additionalProperties: true``, so an
    extra unknown key is schema-valid: it must be written and returned
    as-is -- never silently stripped (and never rejected). This pins the
    v0.1 posture; tightening a schema to ``additionalProperties: false``
    turns this into an unknown-key rejection for that type.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["event"])
    doc["trace_tag"] = {"worker": "w1", "attempt": 1}
    object_id = doc["event_id"]

    backend.write("event", object_id, doc)
    stored = backend.read("event", object_id)
    assert stored == doc
    assert stored["trace_tag"] == {"worker": "w1", "attempt": 1}


def test_no_frozen_schema_forbids_additional_properties() -> None:
    """Document why there is no 'unknown-key rejection' test: every frozen
    schema explicitly permits additional properties, so an unknown key is
    schema-valid today. If a future schema tightens to
    ``additionalProperties: false``, this test fails loudly and the
    unknown-key rejection path becomes exercisable through the write gate.
    """
    schemas_dir = Path(__file__).resolve().parents[2] / "schemas"
    schema_files = sorted(schemas_dir.glob("*.schema.yaml"))
    assert schema_files, f"no schemas found under {schemas_dir}"
    for schema_file in schema_files:
        loaded = yaml.safe_load(schema_file.read_text(encoding="utf-8"))
        for value in _collect_additional_properties(loaded):
            assert value is True, (
                f"{schema_file.name}: additionalProperties={value!r}; the"
                " write gate must reject unknown keys once a schema"
                " forbids them (DEV-M1-G05)"
            )


def _collect_additional_properties(node: Any) -> list[Any]:
    """All ``additionalProperties`` values anywhere in a schema document."""
    values: list[Any] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "additionalProperties":
                values.append(value)
            values.extend(_collect_additional_properties(value))
    elif isinstance(node, list):
        for item in node:
            values.extend(_collect_additional_properties(item))
    return values
