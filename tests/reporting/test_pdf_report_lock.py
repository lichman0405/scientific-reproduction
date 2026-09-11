"""Lock-degradation of report file writing (reporting.pdf_report).

A viewer holding the report PDF open (WPS/Acrobat on Windows) makes the
primary filename unwritable. The render must not fail: it degrades to a
numbered variant (``.v2``/``.v3``) and keeps the JSON sidecar consistent
with the actual file that was written.
"""
import json
from pathlib import Path

import pytest

from scientific_reproduction.reporting.pdf_report import (
    JSON_FILENAME,
    PDF_FILENAME,
    _write_report_files,
)


class _FakeReport:
    def to_canonical_json(self):
        return json.dumps({"probe": 1})


def test_normal_write_uses_primary_names(tmp_path: Path):
    target = _write_report_files(tmp_path, PDF_FILENAME, b"%PDF-1.4", _FakeReport())
    assert target.name == PDF_FILENAME
    assert (tmp_path / PDF_FILENAME).read_bytes() == b"%PDF-1.4"
    assert (tmp_path / JSON_FILENAME).exists()


def test_locked_write_degrades_to_v2_and_keeps_sidecar(tmp_path: Path, monkeypatch):
    _orig_write_bytes = Path.write_bytes

    def _locked_write_bytes(self: Path, data: bytes) -> int:
        if self.name == PDF_FILENAME:
            raise PermissionError(13, "Permission denied")
        return _orig_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _locked_write_bytes)
    target = _write_report_files(tmp_path, PDF_FILENAME, b"%PDF-1.4", _FakeReport())
    assert target.name == "reproduction-report.v2.pdf"
    assert target.read_bytes() == b"%PDF-1.4"
    assert (tmp_path / "reproduction-report.v2.json").exists()
    # primary untouched (still locked), sidecar name matches actual file
    assert not (tmp_path / PDF_FILENAME).exists()


def test_second_lock_degrades_further(tmp_path: Path, monkeypatch):
    # simulate an existing .v2 from a previous degraded render (before
    # the lock patch takes over writes)
    (tmp_path / "reproduction-report.v2.pdf").write_bytes(b"old")
    _orig_write_bytes = Path.write_bytes

    def _locked_write_bytes(self: Path, data: bytes) -> int:
        # primary and the already-existing .v2 are both unwritable
        if self.name in (PDF_FILENAME, "reproduction-report.v2.pdf"):
            raise PermissionError(13, "Permission denied")
        return _orig_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", _locked_write_bytes)
    target = _write_report_files(tmp_path, PDF_FILENAME, b"%PDF-1.4", _FakeReport())
    assert target.name == "reproduction-report.v3.pdf"
    assert (tmp_path / "reproduction-report.v3.json").exists()
