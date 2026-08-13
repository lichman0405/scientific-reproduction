"""Clean-environment install smoke test (DEV-M0-G04, acceptance AC-01).

Proves the fresh-environment install path end to end: create a throwaway
virtual environment under a temp directory, pip-install the repository
into it in editable mode, then import the installed package from an
unrelated working directory and check its version.

The test requires network access (pip fetches the hatchling build
backend from PyPI) and is deliberately not skipped when the network is
unavailable -- hiding a failure here would defeat the purpose of the
check. It is hermetic in the sense that the target environment is
created and destroyed within the test; no repository-level state is
mutated.

Works on Windows (``Scripts/python.exe``) and POSIX (``bin/python``).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _venv_python(venv_dir: Path) -> str:
    """Return the interpreter path inside a venv, Windows or POSIX."""
    if os.name == "nt":
        return str(venv_dir / "Scripts" / "python.exe")
    return str(venv_dir / "bin" / "python")


def _clean_env() -> dict[str, str]:
    """Copy the current environment without a PYTHONPATH override.

    A stray PYTHONPATH (e.g. pointing at the repo's ``src/``) could make
    the import succeed even if the editable install failed; stripping it
    keeps the check focused on the installed package.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def test_clean_environment_install() -> None:
    with tempfile.TemporaryDirectory(prefix="sr-bootstrap-venv-") as tmp:
        venv_dir = Path(tmp) / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_dir)
        python = _venv_python(venv_dir)

        install = subprocess.run(
            [python, "-m", "pip", "install", "-e", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            env=_clean_env(),
        )
        assert install.returncode == 0, (
            f"pip install -e failed with exit code {install.returncode}\n"
            f"stdout:\n{install.stdout}\n"
            f"stderr:\n{install.stderr}"
        )

        # Run the import from the temp directory, not the repo, so the
        # package can only be found through the editable install.
        import_check = subprocess.run(
            [
                python,
                "-c",
                "import scientific_reproduction; "
                "assert scientific_reproduction.__version__ == '0.1.0'",
            ],
            capture_output=True,
            text=True,
            env=_clean_env(),
            cwd=Path(tmp),
        )
        assert import_check.returncode == 0, (
            f"import check failed with exit code {import_check.returncode}\n"
            f"stdout:\n{import_check.stdout}\n"
            f"stderr:\n{import_check.stderr}"
        )
