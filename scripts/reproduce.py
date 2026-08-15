#!/usr/bin/env python3
"""Zero-install ``/reproduce`` CLI wrapper.

Runs ``python -m scientific_reproduction.cli.reproduce`` from the bundled
runtime under ``src/`` by prepending it to ``PYTHONPATH``. No pip install
and no venv are required (the runtime is stdlib-only); Python 3.11+ is the
only requirement.

Usage:
    python scripts/reproduce.py init <target> [options]

Equivalent to (after any install of the package):
    python -m scientific_reproduction.cli.reproduce init <target> [options]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = SKILL_ROOT / "src"


def main() -> int:
    """Run the runtime CLI with the bundled ``src/`` on ``PYTHONPATH``."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(SRC_DIR) if not existing else f"{SRC_DIR}{os.pathsep}{existing}"
    )
    return subprocess.call(
        [sys.executable, "-m", "scientific_reproduction.cli.reproduce", *sys.argv[1:]],
        env=env,
    )


if __name__ == "__main__":
    raise SystemExit(main())
