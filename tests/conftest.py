"""Shared pytest fixtures for the scientific-reproduction test suite.

Issue #117 adds the ``make_update_check_repo`` factory fixture used by
the pre-flight skill update check tests (``tests/scripts/``,
``tests/scenarios/``): a local 'installed skill' git scenario built from
a bare remote plus a clone, with all git traffic over local paths — no
network.
"""

from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_BRANCH = "release/skill-v0.2.0"

_PYPROJECT_TEMPLATE = (
    '[project]\nname = "scientific-reproduction"\nversion = "{version}"\n'
)


def _run_git(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    """Run ``git [--] <args>`` and assert success."""
    cmd = ["git"] if cwd is None else ["git", "-C", str(cwd)]
    result = subprocess.run([*cmd, *args], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed (exit {result.returncode}): "
        f"{result.stderr}"
    )
    return result


def _commit_all(cwd: Path, message: str) -> None:
    """Stage everything and commit with a fixed identity (hermetic)."""
    _run_git(cwd, "add", "-A")
    _run_git(
        cwd,
        "-c", "user.name=test",
        "-c", "user.email=test@example.com",
        "commit", "-m", message,
    )


def _extract_archive(archive: bytes, dest: Path) -> None:
    """Extract a ``git archive`` tar stream safely.

    ``filter="data"`` (Python 3.12+) keeps 3.14's stricter default
    behavior explicit; 3.11 has no filter parameter.
    """
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tf:
        if sys.version_info >= (3, 12):
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)


def _read_version(root: Path) -> str:
    """Read the ``project.version`` from a checkout's ``pyproject.toml``."""
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return tomllib.loads(text)["project"]["version"]


def _bump_version(version: str) -> str:
    major, minor, patch = version.split(".")
    return f"{major}.{minor}.{int(patch) + 1}"


def _write_pyproject(root: Path, version: str) -> None:
    (root / "pyproject.toml").write_text(
        _PYPROJECT_TEMPLATE.format(version=version),
        encoding="utf-8",
    )


@dataclass
class UpdateCheckRepo:
    """A local 'installed skill' git scenario for the update check.

    ``work`` is a git clone of the bare ``remote``, on branch
    ``release/skill-v0.2.0``, pinned ``behind`` commits behind the remote
    tip. ``full=True`` seeds the clone from a ``git archive`` of this
    repository's committed HEAD so the complete skill machinery
    (``scripts/smoke.py`` and friends) is present — used by the
    update-flow scenario; uncommitted working-tree changes are never
    exercised.
    """

    work: Path
    remote: Path
    behind: int

    @property
    def pyproject(self) -> Path:
        return self.work / "pyproject.toml"

    @property
    def local_version(self) -> str:
        return _read_version(self.work)

    @property
    def latest_version(self) -> str:
        show = _run_git(self.remote, "show", f"{RELEASE_BRANCH}:pyproject.toml")
        return tomllib.loads(show.stdout)["project"]["version"]

    def run_check(self) -> subprocess.CompletedProcess[str]:
        """Run the real ``check-update.py`` against this checkout."""
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "check-update.py"),
             "--root", str(self.work)],
            capture_output=True,
            text=True,
        )

    def commit(self, message: str) -> None:
        _commit_all(self.work, message)

    def push(self) -> None:
        _run_git(self.work, "push", "origin", RELEASE_BRANCH)

    def dirty(self) -> None:
        """Mark the worktree dirty by touching a tracked file."""
        self.pyproject.write_text(
            self.pyproject.read_text(encoding="utf-8") + "\n# dirty\n",
            encoding="utf-8",
        )

    def clean(self) -> None:
        """Restore the worktree to the committed state."""
        _run_git(self.work, "checkout", "--", "pyproject.toml")

    def break_remote(self) -> None:
        """Point origin at a non-existent remote (simulates offline)."""
        _run_git(self.work, "remote", "set-url", "origin", "file:///nonexistent-origin")


@pytest.fixture
def make_update_check_repo(tmp_path: Path):
    """Factory fixture: build a local 'installed skill' git scenario.

    Returns an ``UpdateCheckRepo``: ``work`` is a clone on branch
    ``release/skill-v0.2.0`` pinned ``behind`` commits behind the bare
    ``remote`` (its ``origin``). ``full=True`` seeds the clone from a
    ``git archive`` of this repository's HEAD. All git traffic is local.
    """

    def _make(
        *,
        behind: int = 0,
        full: bool = False,
        with_origin: bool = True,
        with_release_branch: bool = True,
    ) -> UpdateCheckRepo:
        if not with_origin and with_release_branch:
            raise AssertionError("a release branch requires an origin")
        work = tmp_path / f"skill-behind-{behind}"
        remote = tmp_path / f"remote-{behind}.git"
        _run_git(None, "init", "--bare", str(remote))
        work.mkdir()
        if full:
            archive = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "HEAD"],
                capture_output=True,
                check=True,
            )
            _extract_archive(archive.stdout, work)
        else:
            _write_pyproject(work, _read_version(REPO_ROOT))
        _run_git(None, "-C", str(work), "init", "-b", RELEASE_BRANCH)
        _commit_all(work, f"skill snapshot {_read_version(work)}")
        if with_origin:
            _run_git(None, "-C", str(work), "remote", "add", "origin", str(remote))
        if with_release_branch:
            _run_git(None, "-C", str(work), "push", "-u", "origin", RELEASE_BRANCH)
            for _ in range(behind):
                version = _bump_version(_read_version(work))
                _write_pyproject(work, version)
                _commit_all(work, f"release {version}")
                _run_git(None, "-C", str(work), "push", "origin", RELEASE_BRANCH)
            if behind:
                _run_git(None, "-C", str(work), "reset", "--hard", f"HEAD~{behind}")
        return UpdateCheckRepo(work=work, remote=remote, behind=behind)

    return _make
