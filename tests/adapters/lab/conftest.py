"""Shared fixtures for the filesystem LabAdapter test suite (DEV-M7-G01).

The default ``handoff`` fixture provides an injected handoff root
(``tmp_path/lab``) and the default :class:`FilesystemLabAdapter` over it;
every test path stays inside the test's own ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scientific_reproduction.adapters.lab.filesystem import FilesystemLabAdapter


@pytest.fixture
def handoff(tmp_path: Path) -> tuple[Path, FilesystemLabAdapter]:
    """(base_dir, adapter) over an injected tmp_path handoff root."""
    base = tmp_path / "lab"
    return base, FilesystemLabAdapter(base)
