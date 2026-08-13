"""Tests for the atomic write helper (DEV-M1-G02, acceptance AC-02).

Covered behaviors:
  * ``atomic_write`` creates a file with the given content;
  * overwriting an existing file replaces its content atomically;
  * missing parent directories are created on demand;
  * text (UTF-8) and binary content both round-trip;
  * a failure between temp-write and rename (simulated crash) leaves the
    previous target content intact and cleans up the temp file;
  * stale temp files left by a previous crash are never mistaken for
    real content and never clobber the target;
  * ``file_mode`` semantics: default (None) keeps mkstemp's 0o600 and
    performs no chmod; an explicit mode is applied; a failing chmod
    propagates and leaves no target behind.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from scientific_reproduction.core import atomic as atomic_module
from scientific_reproduction.core.atomic import atomic_write


def test_atomic_write_creates_file(tmp_path) -> None:
    target = tmp_path / "obj.json"
    atomic_write(target, "hello world")
    assert target.read_text(encoding="utf-8") == "hello world"
    # No staging temp file is left behind.
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_overwrites_existing_file(tmp_path) -> None:
    target = tmp_path / "obj.json"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_write_creates_nested_parent_directories(tmp_path) -> None:
    target = tmp_path / "deep" / "nested" / "path" / "obj.json"
    atomic_write(target, "x")
    assert target.read_text(encoding="utf-8") == "x"


def test_atomic_write_binary_content(tmp_path) -> None:
    target = tmp_path / "blob.bin"
    atomic_write(target, b"\x00\x01\xfe\xff")
    assert target.read_bytes() == b"\x00\x01\xfe\xff"


def test_atomic_write_text_content_is_utf8(tmp_path) -> None:
    target = tmp_path / "note.txt"
    text = "héllo 世界 – ünïcode"
    atomic_write(target, text)
    assert target.read_bytes() == text.encode("utf-8")
    assert target.read_text(encoding="utf-8") == text


def test_atomic_write_accepts_str_path(tmp_path) -> None:
    target = tmp_path / "str.txt"
    atomic_write(str(target), "via str path")
    assert target.read_text(encoding="utf-8") == "via str path"


def test_failed_write_keeps_previous_content_and_cleans_temp(
    tmp_path, monkeypatch
) -> None:
    """Simulate a crash after the temp file is fully written but before the
    rename: ``os.replace`` raises, the previous target content must be
    intact, and no temp file may be left behind (AC-02).
    """
    target = tmp_path / "obj.json"
    previous = json.dumps({"version": 1, "state": "valid"})
    atomic_write(target, previous)

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(atomic_module.os, "replace", boom)
    with pytest.raises(OSError, match="simulated crash before rename"):
        atomic_write(target, json.dumps({"version": 2, "state": "invalid"}))
    monkeypatch.undo()

    assert target.read_text(encoding="utf-8") == previous
    assert list(tmp_path.iterdir()) == [target]


def test_first_write_failure_leaves_no_target(tmp_path, monkeypatch) -> None:
    """A crash before the first rename must not create a half-written
    target: the file either does not exist (this case) or is complete.
    """
    target = tmp_path / "obj.json"

    def boom(src, dst):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(atomic_module.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write(target, "partial")
    monkeypatch.undo()

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_stale_temp_file_from_previous_crash_never_clobbers_target(
    tmp_path,
) -> None:
    """A leftover truncated temp file (simulating a crashed write) must
    never be read as the object and must not interfere with later writes.
    """
    target = tmp_path / "obj.json"
    atomic_write(target, "previous valid content")

    # Simulate the crash state: a truncated temp file next to the target.
    stale = tmp_path / ".obj.json.deadbeef1234.tmp"
    stale.write_text('{"truncat', encoding="utf-8")

    # The stale file is invisible to readers and a new write ignores it.
    assert target.read_text(encoding="utf-8") == "previous valid content"
    atomic_write(target, "new valid content")
    assert target.read_text(encoding="utf-8") == "new valid content"


# ---------------------------------------------------------------------------
# file_mode parameter (security hardening: no implicit permission widening)
# ---------------------------------------------------------------------------


def test_default_mode_does_no_chmod_at_all(tmp_path, monkeypatch) -> None:
    """file_mode=None must not call chmod: mkstemp's 0o600 stays as the
    umask-respecting single-user default.
    """
    calls: list[tuple] = []
    real_chmod = atomic_module.os.chmod

    def spy(path, mode, *args, **kwargs):
        calls.append((path, mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(atomic_module.os, "chmod", spy)
    atomic_write(tmp_path / "obj.json", "x")
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only mode semantics")
def test_default_file_mode_is_0600(tmp_path) -> None:
    target = tmp_path / "obj.json"
    atomic_write(target, "x")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only mode semantics")
def test_explicit_file_mode_is_applied(tmp_path) -> None:
    target = tmp_path / "obj.json"
    atomic_write(target, "x", file_mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    atomic_write(target, "y", file_mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_explicit_file_mode_does_not_error_on_windows(tmp_path) -> None:
    # On Windows chmod is largely a no-op; requesting a mode must still
    # complete the write without error.
    target = tmp_path / "obj.json"
    atomic_write(target, "x", file_mode=0o644)
    assert target.read_text(encoding="utf-8") == "x"


def test_chmod_failure_propagates_and_leaves_no_target(tmp_path, monkeypatch) -> None:
    target = tmp_path / "obj.json"

    def boom(path, mode):
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(atomic_module.os, "chmod", boom)
    with pytest.raises(OSError, match="simulated chmod failure"):
        atomic_write(target, "x", file_mode=0o644)
    monkeypatch.undo()

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
