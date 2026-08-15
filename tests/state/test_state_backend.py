"""Tests for the StateBackend interface and filesystem implementation
(DEV-M1-G02, acceptance AC-01/AC-02/AC-03).

Covered behaviors:
  * write/read/exists/list_ids/delete round-trips for every normative
    object type (valid documents reused from tests/core/fixtures.py);
  * per-object file layout ``base_dir/<tree_dir>/<object_id>.json`` with
    the canonical tree directories of ``SCHEMA_TO_STATE_DIR`` (the same
    plural directories the planning registries resolve, AC-02) and
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
import os
import stat

import pytest

from scientific_reproduction.core import atomic as atomic_module
from scientific_reproduction.core import models as m
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import SCHEMA_NAMES
from scientific_reproduction.core.schema_validation import (
    SchemaValidationError,
    validate_object,
)
from scientific_reproduction.core.state_backend import (
    SCHEMA_TO_STATE_DIR,
    FilesystemStateBackend,
    StateBackend,
    UnknownObjectTypeError,
)
from scientific_reproduction.planning import init as planning_init
from scientific_reproduction.planning import inventory as planning_inventory
from scientific_reproduction.planning import plan as planning_plan
from scientific_reproduction.planning import resources as planning_resources
from tests.core.fixtures import VALID_DOCS


def _make_symlink(
    link_path, target_path, *, target_is_directory: bool = False
) -> None:
    """Create a symlink or skip the test where symlinks are unavailable."""
    try:
        os.symlink(
            target_path, link_path, target_is_directory=target_is_directory
        )
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this platform: {exc}")


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
    assert (base / "plans" / f"{plan['plan_id']}.json").is_file()
    assert (base / "events" / f"{event['event_id']}.json").is_file()

    # AC-01: no monolithic mutable state blob -- every entry at the base
    # dir root is a per-type tree directory, and every file is a
    # per-object <object_id>.json inside one of them.
    assert {entry.name for entry in base.iterdir()} == {
        "project",
        "plans",
        "events",
    }
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
    raw = (tmp_path / "state" / "events" / f"{doc['event_id']}.json").read_text(
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
    assert not (tmp_path / "state" / "runs" / f"{doc['run_id']}.json").exists()


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


# ---------------------------------------------------------------------------
# Symlink / TOCTOU hardening
# ---------------------------------------------------------------------------


def test_read_refuses_symlinked_object_file(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["project"])
    backend.write("project", doc["project_id"], doc)

    path = tmp_path / "state" / "project" / f"{doc['project_id']}.json"
    decoy = tmp_path / "decoy.json"
    decoy.write_text(json.dumps(doc), encoding="utf-8")
    path.unlink()
    _make_symlink(path, decoy)

    with pytest.raises(ValueError, match="symlink"):
        backend.read("project", doc["project_id"])


def test_read_refuses_type_dir_symlinking_outside_base(tmp_path) -> None:
    # The object file itself is a regular file, but its type directory is
    # a symlink pointing outside base_dir: the resolved path must be
    # rejected before the content is read.
    outside = tmp_path / "outside"
    outside.mkdir()
    doc = copy.deepcopy(VALID_DOCS["project"])
    (outside / f"{doc['project_id']}.json").write_text(
        json.dumps(doc), encoding="utf-8"
    )
    backend = FilesystemStateBackend(tmp_path / "state")
    _make_symlink(
        tmp_path / "state" / "project", outside, target_is_directory=True
    )

    with pytest.raises(ValueError, match="escape"):
        backend.read("project", doc["project_id"])


def test_write_refuses_type_dir_symlinking_outside_base(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    backend = FilesystemStateBackend(tmp_path / "state")
    _make_symlink(
        tmp_path / "state" / "project", outside, target_is_directory=True
    )
    doc = copy.deepcopy(VALID_DOCS["project"])

    with pytest.raises(ValueError, match="escape"):
        backend.write("project", doc["project_id"], doc)
    # Nothing escaped base_dir.
    assert not (outside / f"{doc['project_id']}.json").exists()
    assert backend.list_ids("project") == []


# ---------------------------------------------------------------------------
# Case-insensitive ID collision policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first,second",
    [("EV-1", "ev-1"), ("ev-1", "EV-1")],
    ids=["upper-then-lower", "lower-then-upper"],
)
def test_case_colliding_object_ids_are_rejected(
    tmp_path, first: str, second: str
) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["event"])
    doc1["event_id"] = first
    backend.write("event", first, doc1)

    doc2 = copy.deepcopy(doc1)
    doc2["event_id"] = second
    doc2["reason"] = "case variant"
    with pytest.raises(ValueError, match="case-insensitively"):
        backend.write("event", second, doc2)

    # The original object is untouched and still the only one present.
    assert backend.list_ids("event") == [first]
    assert backend.read("event", first) == doc1


def test_exact_case_rewrite_of_same_id_still_works(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["event"])
    doc1["event_id"] = "EV-1"
    backend.write("event", "EV-1", doc1)
    doc2 = copy.deepcopy(doc1)
    doc2["reason"] = "updated"
    backend.write("event", "EV-1", doc2)
    assert backend.read("event", "EV-1") == doc2
    assert backend.list_ids("event") == ["EV-1"]


def test_canonical_generate_id_object_ids_work(tmp_path) -> None:
    # Object IDs are expected to follow the core.ids.generate_id pattern.
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["event"])
    object_id = generate_id("event", doc["event_id"])
    doc["event_id"] = object_id
    backend.write("event", object_id, doc)
    assert backend.read("event", object_id) == doc


# ---------------------------------------------------------------------------
# file_mode threading through the backend
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only mode semantics")
def test_backend_explicit_file_mode_is_applied(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state", file_mode=0o644)
    doc = copy.deepcopy(VALID_DOCS["project"])
    backend.write("project", doc["project_id"], doc)
    path = tmp_path / "state" / "project" / f"{doc['project_id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_backend_explicit_file_mode_does_not_error_on_windows(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state", file_mode=0o644)
    doc = copy.deepcopy(VALID_DOCS["project"])
    backend.write("project", doc["project_id"], doc)
    assert backend.read("project", doc["project_id"]) == doc


def test_backend_default_mode_does_not_chmod(tmp_path, monkeypatch) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    calls: list[tuple] = []
    real_chmod = atomic_module.os.chmod

    def spy(path, mode, *args, **kwargs):
        calls.append((path, mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(atomic_module.os, "chmod", spy)
    doc = copy.deepcopy(VALID_DOCS["project"])
    backend.write("project", doc["project_id"], doc)
    assert calls == []


# ---------------------------------------------------------------------------
# Canonical tree layout (SCHEMA_TO_STATE_DIR, AC-02 truth-source contract)
# ---------------------------------------------------------------------------


def test_schema_to_state_dir_covers_exactly_the_schema_names() -> None:
    """Every schema name maps to exactly one canonical tree directory."""
    assert set(SCHEMA_TO_STATE_DIR) == set(SCHEMA_NAMES)
    assert len(SCHEMA_TO_STATE_DIR) == len(SCHEMA_NAMES)
    # Every target is a single safe relative path segment (mirrors the
    # object-id stem policy: the map must never produce an escaping path).
    assert len(set(SCHEMA_TO_STATE_DIR.values())) == len(SCHEMA_NAMES)
    for tree_dir in SCHEMA_TO_STATE_DIR.values():
        assert tree_dir not in (".", "..")
        assert "/" not in tree_dir and "\\" not in tree_dir
        assert tree_dir and not tree_dir.startswith(".")


def test_schema_to_state_dir_matches_the_planning_registries() -> None:
    """The backend resolves exactly the directories the registries write.

    AC-02 truth-source contract: a worker reading Core state through the
    backend sees the same files the planning registries persist. The
    registry ``*_STATE_DIR`` constants are the same strings the backend
    map uses for those schema names, and every tree directory is the
    canonical workspace tree (``planning.init.INIT_DIRECTORIES``) except
    the documented on-demand dirs and ``project`` (whose canonical single
    record is ``project.yaml`` at the workspace root).
    """
    assert SCHEMA_TO_STATE_DIR["goal"] == planning_plan.GOALS_STATE_DIR
    assert SCHEMA_TO_STATE_DIR["plan"] == planning_plan.PLANS_STATE_DIR
    assert (
        SCHEMA_TO_STATE_DIR["acceptance-criteria"]
        == planning_plan.ACCEPTANCE_STATE_DIR
    )
    assert SCHEMA_TO_STATE_DIR["analysis"] == planning_plan.PROTOCOLS_STATE_DIR
    assert (
        SCHEMA_TO_STATE_DIR["closure-contract"]
        == planning_plan.CLOSURE_STATE_DIR
    )
    assert (
        SCHEMA_TO_STATE_DIR["inventory-item"]
        == planning_inventory.INVENTORY_STATE_DIR
    )
    assert (
        SCHEMA_TO_STATE_DIR["requirement"]
        == planning_inventory.REQUIREMENTS_STATE_DIR
    )
    assert SCHEMA_TO_STATE_DIR["resource"] == planning_resources.RESOURCES_STATE_DIR
    assert (
        SCHEMA_TO_STATE_DIR["statistical-design"]
        == planning_plan.DESIGNS_STATE_DIR
    )

    # Tree directories created on demand by adapters/registries rather
    # than at init: ``acceptance/``/``closure/`` follow the registries
    # that created them on demand (``ACCEPTANCE_STATE_DIR``,
    # ``CLOSURE_STATE_DIR``), ``research-requests/``/``retry-policies/``
    # extend the plural-of-schema-name convention for their on-demand
    # kinds, and ``lab/``/``human-gates/``/``manifests/`` are template
    # tree dirs (``templates/PROJECT-TREE.template.txt``) created by the
    # lab and manifest adapters on first use.
    on_demand = {
        "acceptance",
        "closure",
        "designs",
        "research-requests",
        "retry-policies",
        "lab",
        "human-gates",
        "manifests",
    }
    for schema_name, tree_dir in SCHEMA_TO_STATE_DIR.items():
        if tree_dir == "project" or tree_dir in on_demand:
            continue
        assert tree_dir in planning_init.INIT_DIRECTORIES, (
            f"{schema_name!r} maps to {tree_dir!r}, which is not a canonical"
            " workspace tree directory"
        )


def test_backend_record_lands_in_the_registry_tree_directory(tmp_path) -> None:
    """A backend write lands exactly where the registries would read it.

    The issue scenario in reverse: ``backend.write("goal", ...)`` must
    produce ``<root>/goals/<id>.json`` -- the file the planning goal
    registry resolves (``GOALS_STATE_DIR``) -- so a worker that reads
    Core state through the backend and a supervisor using the registry
    observe the same records (AC-02).
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["goal"])
    goal_id = doc["goal_id"]
    backend.write("goal", goal_id, doc)

    path = tmp_path / "state" / planning_plan.GOALS_STATE_DIR / f"{goal_id}.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == doc
    # And a fresh backend over the same base sees it (no cache anywhere).
    assert FilesystemStateBackend(tmp_path / "state").list_ids("goal") == [
        goal_id
    ]
