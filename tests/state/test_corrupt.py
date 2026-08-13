"""Corrupt-object error behavior tests (DEV-M1-G05, AC-03).

Out-of-band corruption of a persisted object must surface a
deterministic error -- never a silent repair, never a guess. Coverage:

* unparseable content (truncated JSON, malformed JSON, invalid UTF-8)
  raises ``ValueError`` with the documented "is corrupt" message; the
  exact message format is pinned for stability, and the corrupt file is
  neither consumed nor rewritten;
* valid JSON that is not a JSON object (``[1, 2]``, ``"x"``, ``42``,
  ``null``) raises ``ValueError`` ("is not a JSON object") with a pinned
  message;
* a schema-violating but parseable object (a hand edit that makes the
  content schema-invalid) is returned **as-is**: ``read`` is not a
  re-validation gate -- the schema gate lives on ``write`` -- and the
  stored bytes are never rewritten (no silent repair, AC-03);
* corrupt records stay visible to ``exists``/``list_ids`` (corruption
  surfaces on the ``read`` that touches it) and remain deletable for
  operational repair;
* a stray non-JSON file in a type directory is not a record and is
  ignored by ``list_ids``;
* an entry at an object path that is not a regular file (a directory
  planted by an external edit) raises a documented, platform-independent
  error from ``read`` and ``delete``.
"""

from __future__ import annotations

import copy
import json

import pytest

from scientific_reproduction.core.schema_validation import validate_object
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from tests.core.fixtures import VALID_DOCS


def _write_raw(path, content: str | bytes) -> None:
    """Write raw (possibly corrupt) bytes to an object path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unparseable content: deterministic "is corrupt" errors
# ---------------------------------------------------------------------------


def test_truncated_json_raises_value_error_with_stable_message(tmp_path) -> None:
    """A truncated JSON file (simulated external edit) raises ValueError
    with the exact documented message; the corrupt file is not consumed,
    repaired, or rewritten.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    raw = '{"project_id": "P1", "project_phase": "PLAN'
    path = tmp_path / "state" / "project" / "P1.json"
    _write_raw(path, raw)

    with pytest.raises(ValueError) as exc_info:
        backend.read("project", "P1")

    # Message stability: any reword of the format fails this test.
    try:
        json.loads(raw)
    except json.JSONDecodeError as inner:
        assert str(exc_info.value) == (
            f"stored object 'project'/'P1' at {path} is corrupt: {inner}"
        )
    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)
    # The corrupt file is still there, byte-for-byte (read() neither
    # consumes nor silently repairs).
    assert path.read_bytes() == raw.encode("utf-8")


def test_malformed_json_raises_value_error(tmp_path) -> None:
    backend = FilesystemStateBackend(tmp_path / "state")
    path = tmp_path / "state" / "run" / "RUN-1.json"
    _write_raw(path, "{not json at all")

    with pytest.raises(ValueError, match="is corrupt"):
        backend.read("run", "RUN-1")


def test_invalid_utf8_raises_value_error(tmp_path) -> None:
    """Content that is not decodable as UTF-8 raises the same documented
    "is corrupt" ValueError (UnicodeDecodeError path).
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    path = tmp_path / "state" / "run" / "RUN-1.json"
    _write_raw(path, b"\xff\xfe\x00\x01")

    with pytest.raises(ValueError) as exc_info:
        backend.read("run", "RUN-1")
    assert "is corrupt" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)


# ---------------------------------------------------------------------------
# Valid JSON that is not an object
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ['[1, 2]', '"x"', "42", "null", "true"], ids=["list", "str", "int", "null", "bool"]
)
def test_valid_json_non_object_raises_value_error(tmp_path, raw: str) -> None:
    """Valid JSON that is not a JSON object raises ValueError with the
    documented message, pinned exactly.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    path = tmp_path / "state" / "event" / "EV-1.json"
    _write_raw(path, raw)

    with pytest.raises(ValueError) as exc_info:
        backend.read("event", "EV-1")
    assert str(exc_info.value) == (
        f"stored object 'event'/'EV-1' at {path} is not a JSON object"
    )


# ---------------------------------------------------------------------------
# Schema-violating but parseable objects: read returns as-is, never repairs
# ---------------------------------------------------------------------------


def test_schema_violating_object_returned_as_is_without_repair(tmp_path) -> None:
    """A hand-edited object that is valid JSON but violates the schema is
    returned as-is. read() is not a re-validation gate (the schema gate
    lives on write), and the stored bytes are never rewritten -- no
    silent repair (AC-03).
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["run"])
    backend.write("run", doc["run_id"], doc)
    path = tmp_path / "state" / "run" / f"{doc['run_id']}.json"

    # Out-of-band hand edit: bogus enum value plus an unknown key.
    tampered = copy.deepcopy(doc)
    tampered["lifecycle_state"] = "BOGUS_EXTERNAL_EDIT"
    tampered["tampered"] = True
    raw_bytes = json.dumps(
        tampered, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    path.write_bytes(raw_bytes)

    stored = backend.read("run", doc["run_id"])

    assert stored == tampered
    assert stored["lifecycle_state"] == "BOGUS_EXTERNAL_EDIT"
    assert stored["tampered"] is True
    # Still schema-invalid after read: the read gate does not validate.
    assert validate_object("run", stored) != []
    # No silent repair: the bytes on disk were not rewritten by read().
    assert path.read_bytes() == raw_bytes


def test_schema_violating_object_is_visible_to_exists(tmp_path) -> None:
    """exists() is content-agnostic: a persisted (but hand-corrupted)
    record is still an existing object -- corruption surfaces on read,
    never hidden by existence checks.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["run"])
    backend.write("run", doc["run_id"], doc)
    path = tmp_path / "state" / "run" / f"{doc['run_id']}.json"
    path.write_text('{"run_id": "RUN-001", "bogus', encoding="utf-8")

    assert backend.exists("run", doc["run_id"])
    with pytest.raises(ValueError, match="is corrupt"):
        backend.read("run", doc["run_id"])


# ---------------------------------------------------------------------------
# Corrupt records: visibility, listing, and operational repair
# ---------------------------------------------------------------------------


def test_corrupt_record_is_deletable_for_operational_repair(tmp_path) -> None:
    """delete() never parses the file, so a corrupt record is removable
    (operational repair) -- with a deterministic error before that for
    anyone who tries to read it.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    path = tmp_path / "state" / "event" / "EV-CORRUPT.json"
    _write_raw(path, '{"event_id": "EV-CORRUPT", "ti')

    with pytest.raises(ValueError, match="is corrupt"):
        backend.read("event", "EV-CORRUPT")
    assert backend.list_ids("event") == ["EV-CORRUPT"]

    backend.delete("event", "EV-CORRUPT")
    assert not backend.exists("event", "EV-CORRUPT")
    assert backend.list_ids("event") == []
    with pytest.raises(FileNotFoundError, match="no object"):
        backend.read("event", "EV-CORRUPT")


def test_list_ids_reports_corrupt_records_but_ignores_stray_non_json_files(
    tmp_path,
) -> None:
    """list_ids reports every *.json record regardless of content (the
    corruption surfaces on read, never hidden); non-JSON stray files are
    not records and are ignored.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["event"])
    backend.write("event", doc["event_id"], doc)
    type_dir = tmp_path / "state" / "event"
    (type_dir / "EV-CORRUPT.json").write_text('{"broken', encoding="utf-8")
    (type_dir / "notes.txt").write_text("not an object", encoding="utf-8")
    (type_dir / ".stale.tmp").write_text('{"truncat', encoding="utf-8")

    assert backend.list_ids("event") == [doc["event_id"], "EV-CORRUPT"]
    with pytest.raises(ValueError, match="is corrupt"):
        backend.read("event", "EV-CORRUPT")
    # The valid record is untouched.
    assert backend.read("event", doc["event_id"]) == doc


# ---------------------------------------------------------------------------
# Entry exists but is not a regular file (e.g. a planted directory)
# ---------------------------------------------------------------------------


def test_read_of_directory_at_object_path_raises_deterministic_error(tmp_path) -> None:
    """A directory planted at <id>.json is not a regular file: read
    raises the documented ValueError instead of a misleading
    FileNotFoundError or a platform-dependent OSError.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    dir_path = tmp_path / "state" / "run" / "RUN-DIR.json"
    dir_path.mkdir(parents=True)

    with pytest.raises(ValueError, match="not a regular file"):
        backend.read("run", "RUN-DIR")
    assert dir_path.is_dir()  # read() never mutates the entry


def test_delete_of_directory_at_object_path_raises_deterministic_error(
    tmp_path,
) -> None:
    """A plain unlink of a directory raises IsADirectoryError on POSIX but
    PermissionError on Windows; delete refuses with one documented error
    so behavior is identical everywhere.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    dir_path = tmp_path / "state" / "run" / "RUN-DIR.json"
    dir_path.mkdir(parents=True)

    with pytest.raises(ValueError, match="not a regular file"):
        backend.delete("run", "RUN-DIR")
    assert dir_path.is_dir()  # nothing was removed


def test_exists_is_false_for_directory_at_object_path(tmp_path) -> None:
    """A directory is not a persisted object: exists() stays False while
    read() raises the documented error -- the two never contradict the
    definition of a persisted object.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    (tmp_path / "state" / "run" / "RUN-DIR.json").mkdir(parents=True)

    assert not backend.exists("run", "RUN-DIR")
    with pytest.raises(ValueError, match="not a regular file"):
        backend.read("run", "RUN-DIR")
