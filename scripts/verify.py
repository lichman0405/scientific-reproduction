#!/usr/bin/env python3
"""Cross-platform repository verification entry point (DEV-M0-G01).

Usage:
    python scripts/verify.py [--reuse-venv]

Behavior:
    1. Creates a fresh virtual environment in ``.verify-venv`` at the
       repository root (removed and recreated unless --reuse-venv is
       given, in which case an existing one is reused).
    2. Installs the package in editable mode with dev extras
       (``python -m pip install -e ".[dev]"``).
    3. Runs the test suite (``python -m pytest -q``).
    4. Prints ``[verify] PASS`` and exits 0 on success; prints
       ``[verify] FAIL`` and exits non-zero on failure.

Works on Windows (PowerShell / cmd), Git Bash, and POSIX shells.
Requires only the Python standard library plus a working ``python``
interpreter; pip, venv, and network access for the dev extras are needed
at runtime.

Example (PowerShell / bash):
    python scripts/verify.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR_NAME = ".verify-venv"


def venv_python(venv_dir: Path) -> Path:
    """Return the interpreter path inside a virtual environment."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command with the repository root as cwd; raise on failure."""
    display = subprocess.list2cmdline(cmd) if cmd else "<empty>"
    print(f"$ {display}")
    return subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description="Verify the scientific-reproduction repository end to end.",
    )
    parser.add_argument(
        "--reuse-venv",
        action="store_true",
        help="reuse an existing .verify-venv instead of creating a fresh one",
    )
    args = parser.parse_args()

    venv_dir = REPO_ROOT / VENV_DIR_NAME
    if venv_dir.exists() and not args.reuse_venv:
        print(f"[verify] removing existing {VENV_DIR_NAME} for a fresh environment")
        shutil.rmtree(venv_dir)
    if not venv_dir.exists():
        print(f"[verify] creating fresh virtual environment in {VENV_DIR_NAME}")
        run([sys.executable, "-m", "venv", str(venv_dir)])

    python = venv_python(venv_dir)
    print(f"[verify] using venv interpreter: {python}")

    steps: list[tuple[str, list[str]]] = [
        (
            "install package (editable, dev extras)",
            [str(python), "-m", "pip", "install", "-e", ".[dev]"],
        ),
        (
            "run test suite",
            [str(python), "-m", "pytest", "-q"],
        ),
    ]
    for name, cmd in steps:
        print(f"\n[verify] step: {name}")
        try:
            run(cmd)
        except subprocess.CalledProcessError as exc:
            print(f"\n[verify] FAIL: step '{name}' exited with code {exc.returncode}")
            return exc.returncode

    print("\n[verify] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
