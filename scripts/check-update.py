#!/usr/bin/env python3
"""Pre-flight skill update check for the Scientific Reproduction skill.

The skill directory *is* a git clone of the release branch
``release/skill-v0.2.0``; any push to it makes every installed copy
outdated. This script compares the local checkout against the remote
branch without mutating the working tree:

1. **Check the environment** — ``git`` is available and the skill root is
   a git clone with an ``origin`` remote; otherwise the check cannot run.
2. **Fetch the release branch** — ``git fetch origin
   release/skill-v0.2.0`` (updates only the remote-tracking ref, never the
   working tree; the only network touch of the whole skill).
3. **Compare** — reads the local skill version from ``pyproject.toml`` and
   the latest available version from the remote ``pyproject.toml``, and
   counts the commits the local ``HEAD`` is behind
   (``git rev-list --count HEAD..origin/release/skill-v0.2.0``).

Exit codes (stable contract, covered by tests):

- ``0`` — up to date: the local checkout already contains the remote tip.
- ``1`` — update available: the version gap and the N-behind commit count
  are printed, plus the apply instructions (user confirmation and a clean
  worktree required).
- ``2`` — cannot check: offline, not a git clone, or any other condition
  that makes the comparison impossible.

The check is read-only and never applies an update; applying is a separate,
explicitly confirmed step (see SKILL.md "Pre-flight"). ``--root`` points
the check at a different skill checkout (used by the tests and useful to
inspect another installed copy); it defaults to the checkout containing
this script.

Usage:
    python scripts/check-update.py [--root <skill-checkout>]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tomllib
from pathlib import Path

RELEASE_BRANCH = "release/skill-v0.2.0"
RELEASE_REF = f"origin/{RELEASE_BRANCH}"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
FETCH_TIMEOUT_SECONDS = 60

PREFIX = "[update-check]"


def _git(root: Path, args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``git -C <root> <args>``; capture stdout/stderr as text."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_detail(result: subprocess.CompletedProcess[str]) -> str:
    """First non-empty error line, for stable, short diagnostic output."""
    for line in (result.stderr or result.stdout).splitlines():
        if line.strip():
            return line.strip()
    return f"exit code {result.returncode}"


def _version_from_pyproject(text: str) -> str:
    """Parse the ``project.version`` field from a ``pyproject.toml`` text."""
    return tomllib.loads(text)["project"]["version"]


def _cannot_check(reason: str) -> int:
    print(f"{PREFIX} cannot check: {reason}")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-update.py",
        description="Pre-flight skill update check: compare the local skill "
        "checkout with the remote release branch.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_ROOT,
        help=(
            "skill checkout to check (default: the checkout containing "
            "this script)"
        ),
    )
    args = parser.parse_args(argv)
    root = args.root

    if shutil.which("git") is None:
        return _cannot_check("git executable not found")

    repo = _git(root, ["rev-parse", "--is-inside-work-tree"])
    if repo.returncode != 0:
        return _cannot_check(f"not a git clone at {root}")
    remote = _git(root, ["remote", "get-url", "origin"])
    if remote.returncode != 0:
        return _cannot_check("no 'origin' remote configured")

    try:
        fetch = _git(
            root,
            ["fetch", "origin", RELEASE_BRANCH],
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _cannot_check(f"fetch from origin timed out after {FETCH_TIMEOUT_SECONDS}s")
    if fetch.returncode != 0:
        return _cannot_check(f"fetch from origin failed: {_git_detail(fetch)}")

    ref = _git(root, ["rev-parse", "--verify", RELEASE_REF])
    if ref.returncode != 0:
        return _cannot_check(f"remote branch '{RELEASE_REF}' not found")

    try:
        local_version = _version_from_pyproject(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        return _cannot_check(f"cannot read the local skill version: {exc}")

    show = _git(root, ["show", f"{RELEASE_REF}:pyproject.toml"])
    if show.returncode != 0:
        return _cannot_check(
            f"cannot read the latest skill version: {_git_detail(show)}"
        )
    try:
        latest_version = _version_from_pyproject(show.stdout)
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        return _cannot_check(f"cannot read the latest skill version: {exc}")

    gap = _git(root, ["rev-list", "--count", f"HEAD..{RELEASE_REF}"])
    if gap.returncode != 0:
        return _cannot_check(f"cannot compute the commit gap: {_git_detail(gap)}")
    behind = int(gap.stdout.strip())

    if behind == 0:
        print(
            f"{PREFIX} skill version {local_version} is up to date "
            f"with {RELEASE_REF}"
        )
        return 0

    plural = "" if behind == 1 else "s"
    print(
        f"{PREFIX} update available: local {local_version}, latest "
        f"{latest_version}, behind by {behind} commit{plural} on {RELEASE_REF}"
    )
    status = _git(root, ["status", "--porcelain"])
    if status.stdout.strip():
        print(
            f"{PREFIX} note: the worktree is dirty; the fast-forward "
            f"merge will be refused until it is clean"
        )
    print(
        f"{PREFIX} apply only after the user's explicit confirmation "
        f"(clean worktree required):"
    )
    print(f"{PREFIX}   git merge --ff-only {RELEASE_REF}")
    print(f"{PREFIX}   python scripts/smoke.py   # must print [smoke] PASS")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
