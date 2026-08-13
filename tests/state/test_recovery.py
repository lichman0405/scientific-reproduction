"""Failure-injection and partial-write recovery tests (DEV-M1-G05, AC-01).

AC-01 requires that the LAST VALID object survives a simulated
interrupted write at every layer, with crash-gap semantics proven by
byte-compare. Coverage:

* ``core.atomic.atomic_write`` level (failure injection into the atomic
  protocol itself): an ``os.fsync`` failure and a mid-write ``write``
  failure each leave the previous target content byte-intact, never a
  half-written mix; a failing first write leaves no target at all; no
  staging ``.tmp`` file is ever left behind by a *handled* failure.
* ``FilesystemStateBackend`` level: the same injected failures through
  ``backend.write`` keep the previous canonical object byte-identical
  (crash gap between staging and rename), and ``list_ids`` is unchanged.
* Hard-crash simulation (the on-disk state a real ``kill -9`` leaves
  behind -- a truncated ``.tmp`` staging file next to the object): the
  stale temp is NEVER read as an object (the backend reads only
  ``<id>.json``), never listed, never clobbers a later write, and the
  previous object bytes stay intact until a new write lands.

All failures are injected deterministically via ``monkeypatch`` (no
sleeps, no timing).
"""

from __future__ import annotations

import copy
import json

import pytest

from scientific_reproduction.core import atomic as atomic_module
from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from tests.core.fixtures import VALID_DOCS


class _PartialWriteHandle:
    """File-like wrapper that writes half the payload, then fails.

    Simulates a disk error that interrupts ``os.fdopen``'s ``write``
    mid-flight: the staging file holds truncated content at the moment of
    failure, exactly the state a crash can leave inside the temp file.
    """

    def __init__(self, real) -> None:
        self._real = real

    def write(self, data):
        mid = len(data) // 2
        self._real.write(data[:mid])
        self._real.flush()
        raise OSError("simulated disk error mid-write")

    def flush(self):
        return self._real.flush()

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _failing_fdopen(real_fdopen):
    """Wrap ``os.fdopen`` so every opened handle fails mid-write."""

    def wrapped(fd, mode, *args, **kwargs):
        return _PartialWriteHandle(real_fdopen(fd, mode, *args, **kwargs))

    return wrapped


# ---------------------------------------------------------------------------
# atomic_write level: fsync failure
# ---------------------------------------------------------------------------


def test_fsync_failure_keeps_previous_content_and_cleans_temp(
    tmp_path, monkeypatch
) -> None:
    """A failure during ``os.fsync`` (after the data was flushed to the
    page cache but before durability) must leave the previous target
    content intact and remove the staging file.
    """
    target = tmp_path / "obj.json"
    previous = json.dumps({"version": 1, "state": "valid"})
    atomic_write(target, previous)

    def boom_fsync(fd):
        raise OSError("simulated crash during fsync")

    monkeypatch.setattr(atomic_module.os, "fsync", boom_fsync)
    with pytest.raises(OSError, match="during fsync"):
        atomic_write(target, json.dumps({"version": 2, "state": "new"}))
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == previous
    assert list(tmp_path.iterdir()) == [target]


def test_fsync_failure_on_first_write_leaves_no_trace(tmp_path, monkeypatch) -> None:
    """A crash during fsync of the very first write leaves no target and
    no staging file: the object either does not exist or is complete.
    """
    target = tmp_path / "obj.json"

    def boom_fsync(fd):
        raise OSError("simulated crash during fsync")

    monkeypatch.setattr(atomic_module.os, "fsync", boom_fsync)
    with pytest.raises(OSError, match="during fsync"):
        atomic_write(target, "partial content")
    monkeypatch.undo()

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# atomic_write level: mid-write failure
# ---------------------------------------------------------------------------


def test_partial_write_failure_never_half_writes_target(tmp_path, monkeypatch) -> None:
    """A disk error interrupting the payload write must never surface
    half-written content at the target name: the reader sees either the
    complete old content or the complete new content, never a mix.
    """
    target = tmp_path / "obj.json"
    previous = json.dumps({"version": 1, "state": "valid"})
    atomic_write(target, previous)

    monkeypatch.setattr(
        atomic_module.os,
        "fdopen",
        _failing_fdopen(atomic_module.os.fdopen),
    )
    with pytest.raises(OSError, match="mid-write"):
        atomic_write(target, json.dumps({"version": 2, "state": "new"}))
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == previous
    assert list(tmp_path.iterdir()) == [target]


def test_partial_write_failure_on_first_write_leaves_no_trace(
    tmp_path, monkeypatch
) -> None:
    """A mid-write failure during the first write leaves no target and no
    staging file behind.
    """
    target = tmp_path / "obj.json"

    monkeypatch.setattr(
        atomic_module.os,
        "fdopen",
        _failing_fdopen(atomic_module.os.fdopen),
    )
    with pytest.raises(OSError, match="mid-write"):
        atomic_write(target, "partial content")
    monkeypatch.undo()

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Backend level: the same failures through FilesystemStateBackend.write
# ---------------------------------------------------------------------------


def test_backend_fsync_crash_keeps_previous_object_byte_identical(
    tmp_path, monkeypatch
) -> None:
    """Crash-gap semantics at the backend: an injected fsync failure while
    rewriting an object leaves the previous canonical object intact
    byte-for-byte, unchanged in ``list_ids``, with no temp file visible.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["project"])
    backend.write("project", doc1["project_id"], doc1)
    path = tmp_path / "state" / "project" / f"{doc1['project_id']}.json"
    original = path.read_bytes()

    def boom_fsync(fd):
        raise OSError("simulated crash during fsync")

    monkeypatch.setattr(atomic_module.os, "fsync", boom_fsync)
    doc2 = copy.deepcopy(doc1)
    doc2["title"] = "updated title"
    with pytest.raises(OSError, match="during fsync"):
        backend.write("project", doc1["project_id"], doc2)
    monkeypatch.undo()

    # AC-01: the last valid object survives, byte-identical.
    assert path.read_bytes() == original
    assert backend.read("project", doc1["project_id"]) == doc1
    assert backend.list_ids("project") == [doc1["project_id"]]
    assert list((tmp_path / "state" / "project").iterdir()) == [path]


def test_backend_mid_write_disk_error_keeps_previous_object(
    tmp_path, monkeypatch
) -> None:
    """A mid-write disk error (partial content already in the staging
    file) raised through the backend must leave the previous object
    bytes intact and visible only as the complete previous object.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["run"])
    backend.write("run", doc1["run_id"], doc1)
    path = tmp_path / "state" / "run" / f"{doc1['run_id']}.json"
    original = path.read_bytes()

    monkeypatch.setattr(
        atomic_module.os,
        "fdopen",
        _failing_fdopen(atomic_module.os.fdopen),
    )
    doc2 = copy.deepcopy(doc1)
    doc2["lifecycle_state"] = "READY"
    with pytest.raises(OSError, match="mid-write"):
        backend.write("run", doc1["run_id"], doc2)
    monkeypatch.undo()

    assert path.read_bytes() == original
    assert backend.read("run", doc1["run_id"]) == doc1
    assert backend.list_ids("run") == [doc1["run_id"]]
    assert list((tmp_path / "state" / "run").iterdir()) == [path]


def test_backend_first_write_crash_gap_leaves_no_object(tmp_path, monkeypatch) -> None:
    """Crash-gap semantics for a first write through the backend: a
    failure before the rename leaves NO object (read raises
    FileNotFoundError), no temp file, and a later write succeeds.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc = copy.deepcopy(VALID_DOCS["run"])
    object_id = doc["run_id"]

    def boom(src, dst):
        raise OSError("simulated crash before first rename")

    monkeypatch.setattr(atomic_module.os, "replace", boom)
    with pytest.raises(OSError, match="before first rename"):
        backend.write("run", object_id, doc)
    monkeypatch.undo()

    with pytest.raises(FileNotFoundError, match="no object"):
        backend.read("run", object_id)
    assert backend.list_ids("run") == []
    # The type directory may exist (atomic_write creates parents on
    # demand) but must be empty -- no target, no temp.
    assert list((tmp_path / "state" / "run").iterdir()) == []

    backend.write("run", object_id, doc)
    assert backend.read("run", object_id) == doc


# ---------------------------------------------------------------------------
# Hard-crash simulation: stale .tmp staging files on disk
# ---------------------------------------------------------------------------


def test_backend_hard_crash_stale_temp_is_never_read_as_object(tmp_path) -> None:
    """A real crash (kill -9) leaves a truncated staging ``.tmp`` file
    next to the object. The backend reads only ``<id>.json``, so the
    stale temp is invisible: the previous canonical object stays intact
    byte-for-byte until a new write lands, and the new write ignores the
    temp.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    doc1 = copy.deepcopy(VALID_DOCS["event"])
    backend.write("event", doc1["event_id"], doc1)
    path = tmp_path / "state" / "event" / f"{doc1['event_id']}.json"
    original = path.read_bytes()

    stale = tmp_path / "state" / "event" / f".{doc1['event_id']}.json.abc123.tmp"
    stale.write_text('{"event_id": "EV-001", "time', encoding="utf-8")

    # The stale temp is never read as the object and never listed.
    assert backend.read("event", doc1["event_id"]) == doc1
    assert path.read_bytes() == original
    assert backend.list_ids("event") == [doc1["event_id"]]

    # A later write succeeds and the stale temp never clobbers the target.
    doc2 = copy.deepcopy(doc1)
    doc2["reason"] = "recovered after crash"
    backend.write("event", doc1["event_id"], doc2)
    assert backend.read("event", doc1["event_id"]) == doc2
    assert path.read_bytes() != original
    # The stale temp is not our garbage to collect; it is simply ignored.
    assert stale.is_file()


def test_backend_first_write_hard_crash_leaves_no_object(tmp_path) -> None:
    """A hard crash during the first write leaves only a truncated temp:
    no object exists (read raises FileNotFoundError -- the temp is never
    promoted), and a fresh write succeeds despite it.
    """
    backend = FilesystemStateBackend(tmp_path / "state")
    object_id = "EV-NEW-001"
    stale = tmp_path / "state" / "event" / f".{object_id}.json.deadbeef.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"event_id": "EV-NEW-001", "ti', encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="no object"):
        backend.read("event", object_id)
    assert backend.list_ids("event") == []
    assert not backend.exists("event", object_id)

    doc = copy.deepcopy(VALID_DOCS["event"])
    doc["event_id"] = object_id
    backend.write("event", object_id, doc)
    assert backend.read("event", object_id) == doc
