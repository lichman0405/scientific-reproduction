"""Verify-command smoke test (DEV-M0-G04, acceptance AC-02).

Runs the canonical repository verification command (``python
scripts/verify.py``) from the repository root and asserts it exits 0.

The verify script removes and recreates ``.verify-venv`` at the
repository root and pip-installs the dev extras over the network, so
this test is marked ``slow`` and excluded from the default ``pytest``
run (see ``addopts`` in pyproject.toml). Run it explicitly with
``python -m pytest -q -m slow``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_verify_command_succeeds() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/verify.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"verify.py exited with code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
