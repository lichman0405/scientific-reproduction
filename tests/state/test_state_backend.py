"""Tests for the StateBackend interface and filesystem implementation
(DEV-M1-G02, acceptance AC-01/AC-02/AC-03).

Covered behaviors:
  * write/read/exists/list_ids/delete round-trips for every normative
    object type (valid documents reused from tests/core/fixtures.py);
  * per-object file layout ``base_dir/<obj_type>/<object_id>.json`` with
    **no monolithic state file** (AC-01);
  * schema-valid content round-trips exactly and stays schema-valid on
    read (AC-03);
  * schema-invalid content raises ``SchemaValidationError`` and nothing
    is persisted (AC-03); non-dict content is refused;
  * unknown object types are rejected with a clear error on every
    operation; object IDs that could escape the type directory are
    rejected;
  * an interrupted write (simulated crash before rename) keeps the last
    valid object intact (AC-02);
  * stored files are canonical, deterministic JSON.
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from scientific_reproduction.core import atomic as atomic_module
from scientific_reproduction.core import models as m
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_object,
)
from scientific_reproduction.core.state_backend import (
    FilesystemStateBackend,
    StateBackend,
    UnknownObjectTypeError,
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


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


def test_state_backend_is_abstract() -> None:
    with pytest.raises(TypeError):
        StateBackend()


def test_filesystem_backend_is_a_state_backend(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    assert isinstance(backend, StateBackend)


def test_accepts_str_base_dir(tmp_path) -> None:
    backend = FilesystemStateBackend(str(tmp_path / "state"))
    doc = copy.deepcopy(VALID_DOCS["run"])
    backend.write("run", doc["run_id"], doc)
    assert backend.read("run", doc["run_id"]) == doc


# ---------------------------------------------------------------------------
# Round-trips per object type (AC-03)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("obj_type", sorted(VALID_DOCS))
def test_schema_valid_docs_round_trip(obj_type: str, tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS[obj_type])
    object_id = doc[_id_field(obj_type)]

    backend.write(obj_type, object_id, doc)

    assert backend.exists(obj_type, object_id)
    assert backend.list_ids(obj_type) == [object_id]
    # AC-03: read-after-write returns the exact schema-valid content.
    stored = backend.read(obj_type, object_id)
    assert stored == doc
    assert validate_object(obj_type, stored) == []


def test_model_serialized_content_round_trips(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    model = m.Project.from_dict(copy.deepcopy(VALID_DOCS["project"]))
    doc = model.to_dict()
    backend.write("project", doc["project_id"], doc)
    stored = backend.read("project", doc["project_id"])
    assert stored == doc
    assert validate_object("project", stored) == []


def test_rewrite_keeps_last_valid_content(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["project"])
    doc2 = copy.deepcopy(doc1)
    doc2["title"] = "revised title"

    backend.write("project", doc1["project_id"], doc1)
    backend.write("project", doc1["project_id"], doc2)

    assert backend.read("project", doc1["project_id"]) == doc2


def test_list_ids_returns_sorted_ids(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    for object_id in ["EV-2", "EV-1", "EV-10"]:
        doc = copy.deepcopy(VALID_DOCS["event"])
        doc["event_id"] = object_id
        backend.write("event", object_id, doc)
    assert backend.list_ids("event") == ["EV-1", "EV-10", "EV-2"]


# ---------------------------------------------------------------------------
# Per-object file layout, no monolithic state file (AC-01)
# ---------------------------------------------------------------------------


def test_per_object_layout_and_no_monolithic_state_file(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    project = copy.deepcopy(VALID_DOCS["project"])
    plan = copy.deepcopy(VALID_DOCS["plan"])
    event = copy.deepcopy(VALID_DOCS["event"])
    backend.write("project", project["project_id"], project)
    backend.write("plan", plan["plan_id"], plan)
    backend.write("event", event["event_id"], event)

    base = tmp_path / "state"
    assert (base / "project" / f"{project['project_id']}.json").is_file()
    assert (base / "plan" / f"{plan['plan_id']}.json").is_file()
    assert (base / "event" / f"{event['event_id']}.json").is_file()

    # AC-01: no monolithic mutable state blob -- every entry at the base
    # dir root is a per-type directory, and every file is a per-object
    # <object_id>.json inside one of them.
    assert {entry.name for entry in base.iterdir()} == {"project", "plan", "event"}
    for entry in base.iterdir():
        assert entry.is_dir(), f"unexpected file at base_dir root: {entry}"
        for file_ in entry.iterdir():
            assert file_.is_file() and file_.suffix == ".json"


def test_types_are_isolated_in_separate_directories(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    run_doc = copy.deepcopy(VALID_DOCS["run"])
    event_doc = copy.deepcopy(VALID_DOCS["event"])
    backend.write("run", run_doc["run_id"], run_doc)
    backend.write("event", event_doc["event_id"], event_doc)

    assert backend.list_ids("run") == [run_doc["run_id"]]
    assert backend.list_ids("event") == [event_doc["event_id"]]
    assert not backend.exists("run", event_doc["event_id"])
    assert not backend.exists("event", run_doc["run_id"])


def test_list_ids_on_unknown_type_dir_is_empty(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    assert backend.list_ids("plan") == []


# ---------------------------------------------------------------------------
# Canonical JSON content (AC-03)
# ---------------------------------------------------------------------------


def test_stored_file_is_canonical_json(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["event"])
    backend.write("event", doc["event_id"], doc)
    raw = (tmp_path / "state" / "event" / f"{doc['event_id']}.json").read_text(
        encoding="utf-8"
    )
    assert json.loads(raw) == doc
    # Canonical deterministic serialization: sorted keys, two-space indent.
    assert raw == json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)


def test_rewrite_produces_byte_identical_content(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["project"])
    path = tmp_path / "state" / "project" / f"{doc['project_id']}.json"
    backend.write("project", doc["project_id"], doc)
    before = path.read_bytes()
    backend.write("project", doc["project_id"], doc)
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# Schema gate: invalid content is rejected before persistence (AC-03)
# ---------------------------------------------------------------------------

INVALID_DOCS: dict[str, tuple[str, dict, str]] = {
    "bad_enum": (
        "project",
        {
            "project_id": "P1",
            "primary_target": {"source_type": "doi", "identifier": "x"},
            "project_phase": "BOGUS_PHASE",
            "reproduction_outcome": "UNDETERMINED",
            "current_plan_version": "v1",
        },
        "project_phase",
    ),
    "missing_required": (
        "plan",
        {"plan_id": "P1", "version": "v1", "status": "DRAFT"},
        "inventory_audit",
    ),
    "numeric_range_exceeded": (
        "evidence",
        {
            "evidence_id": "E1",
            "source_id": "S1",
            "claim_id": "C1",
            "finding": "f",
            "assessment": {
                "authority": 9,
                "reliability": 2,
                "directness": 3,
                "reliability_checklist_ref": "R",
            },
        },
        "authority",
    ),
}


@pytest.mark.parametrize(
    "obj_type,doc,error_fragment",
    list(INVALID_DOCS.values()),
    ids=list(INVALID_DOCS),
)
def test_schema_invalid_content_is_rejected_and_not_persisted(
    obj_type: str, doc: dict, error_fragment: str, tmp_path
) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    object_id = doc[_id_field(obj_type)]

    with pytest.raises(SchemaValidationError) as exc_info:
        backend.write(obj_type, object_id, doc)
    assert exc_info.value.obj_type == obj_type
    assert exc_info.value.errors
    assert any(error_fragment in e for e in exc_info.value.errors)

    # Nothing was persisted and no directory structure was created.
    assert not backend.exists(obj_type, object_id)
    assert backend.list_ids(obj_type) == []
    assert not (tmp_path / "state").exists()


def test_non_dict_content_is_refused(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(TypeError, match="must be a dict"):
        backend.write("project", "P1", ["not", "a", "dict"])
    assert not backend.exists("project", "P1")
    assert not (tmp_path / "state").exists()


# ---------------------------------------------------------------------------
# Unknown object types and invalid object IDs
# ---------------------------------------------------------------------------


def test_unknown_object_type_is_rejected_on_all_operations(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        backend.write("no-such-type", "X1", {"a": 1})
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        backend.read("no-such-type", "X1")
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        backend.exists("no-such-type", "X1")
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        backend.list_ids("no-such-type")
    with pytest.raises(UnknownObjectTypeError, match="unknown object type"):
        backend.delete("no-such-type", "X1")
    assert list(tmp_path.iterdir()) == []


def test_unknown_object_type_error_lists_known_types(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(UnknownObjectTypeError) as exc_info:
        backend.write("no-such-type", "X1", {})
    assert "project" in str(exc_info.value)
    assert "event" in str(exc_info.value)


def test_object_id_must_be_a_plain_stem(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    for bad_id in ["", ".", "..", "../escape", "a/b", "a\\b", "id\x00x"]:
        with pytest.raises(ValueError):
            backend.write("project", bad_id, {"project_id": bad_id})
    # Nothing escaped base_dir and nothing at all was created.
    assert not (tmp_path / "escape.json").exists()
    assert not (tmp_path / "state").exists()


# ---------------------------------------------------------------------------
# Delete semantics
# ---------------------------------------------------------------------------


def test_delete_removes_object(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["run"])
    backend.write("run", doc["run_id"], doc)
    backend.delete("run", doc["run_id"])
    assert not backend.exists("run", doc["run_id"])
    assert backend.list_ids("run") == []
    assert not (tmp_path / "state" / "run" / f"{doc['run_id']}.json").exists()


def test_delete_missing_object_raises(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(FileNotFoundError, match="no object"):
        backend.delete("run", "RUN-NOPE")


def test_read_missing_object_raises(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    with pytest.raises(FileNotFoundError, match="no object"):
        backend.read("run", "RUN-NOPE")


# ---------------------------------------------------------------------------
# Interrupted write keeps the last valid object (AC-02)
# ---------------------------------------------------------------------------


def test_interrupted_write_keeps_last_valid_object(tmp_path, monkeypatch) -> None:
    """Crash between temp-write and rename, observed through the backend:
    the previously persisted object must survive byte-identically.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["project"])
    backend.write("project", doc1["project_id"], doc1)
    path = tmp_path / "state" / "project" / f"{doc1['project_id']}.json"
    original = path.read_bytes()

    doc2 = copy.deepcopy(doc1)
    doc2["title"] = "updated title"

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(atomic_module.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash before rename"):
        backend.write("project", doc1["project_id"], doc2)
    monkeypatch.undo()

    # AC-02: the last valid object is intact, both on disk and via read().
    assert path.read_bytes() == original
    assert backend.read("project", doc1["project_id"]) == doc1
    assert backend.list_ids("project") == [doc1["project_id"]]
