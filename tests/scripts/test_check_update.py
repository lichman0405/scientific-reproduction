"""Pre-flight update check contract tests (issue #117).

Covers the stable exit-code and output contract of
``scripts/check-update.py`` against local git fixtures (bare remote +
clone, no network):

- exit ``0`` — up to date;
- exit ``1`` — update available, with the local version, the latest
  version, the N-behind commit count and the apply instructions printed;
- exit ``2`` — cannot check (not a clone, no origin, offline, missing
  release branch, unreadable version).

Also asserts the check never mutates the working tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check-update.py"

UP_TO_DATE_LINE = (
    "[update-check] skill version {version} is up to date "
    "with origin/release/skill-v0.2.0"
)
BEHIND_LINE = (
    "[update-check] update available: local {local}, latest {latest}, "
    "behind by {n} commit{plural} on origin/release/skill-v0.2.0"
)
APPLY_LINES = (
    "[update-check] apply only after the user's explicit confirmation "
    "(clean worktree required):",
    "[update-check]   git merge --ff-only origin/release/skill-v0.2.0",
    "[update-check]   python scripts/smoke.py   # must print [smoke] PASS",
)


def test_check_update_script_exists() -> None:
    assert CHECK_SCRIPT.is_file()


def test_exit_0_when_up_to_date(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0)
    result = repo.run_check()
    assert result.returncode == 0
    assert result.stdout.strip() == UP_TO_DATE_LINE.format(
        version=repo.local_version
    )


def test_exit_1_when_behind_reports_gap(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=1)
    result = repo.run_check()
    assert result.returncode == 1
    expected = "\n".join(
        [
            BEHIND_LINE.format(
                local=repo.local_version,
                latest=repo.latest_version,
                n=1,
                plural="",
            ),
            *APPLY_LINES,
        ]
    )
    assert result.stdout.strip() == expected


def test_behind_two_commits_uses_plural(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=2)
    result = repo.run_check()
    assert result.returncode == 1
    assert (
        "behind by 2 commits on origin/release/skill-v0.2.0" in result.stdout
    )
    assert f"latest {repo.latest_version}" in result.stdout


def test_exit_0_when_local_ahead_of_remote(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0)
    (repo.work / "local-only.txt").write_text("local\n", encoding="utf-8")
    repo.commit("local-only commit")
    result = repo.run_check()
    assert result.returncode == 0
    assert "is up to date" in result.stdout


def test_check_never_mutates_the_worktree(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=1)
    pyproject_before = repo.pyproject.read_text(encoding="utf-8")
    result = repo.run_check()
    assert result.returncode == 1
    assert repo.pyproject.read_text(encoding="utf-8") == pyproject_before
    status = subprocess.run(
        ["git", "-C", str(repo.work), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_dirty_worktree_notes_the_blocker(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=1)
    repo.dirty()
    result = repo.run_check()
    assert result.returncode == 1
    assert "[update-check] note: the worktree is dirty;" in result.stdout
    assert "git merge --ff-only origin/release/skill-v0.2.0" in result.stdout


def test_exit_2_not_a_clone(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT), "--root", str(plain)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout.startswith("[update-check] cannot check: ")


def test_exit_2_no_origin(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0, with_origin=False,
                                  with_release_branch=False)
    result = repo.run_check()
    assert result.returncode == 2
    assert (
        "cannot check: no 'origin' remote configured" in result.stdout
    )


def test_exit_2_fetch_failure_offline(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0)
    repo.break_remote()
    result = repo.run_check()
    assert result.returncode == 2
    assert result.stdout.startswith(
        "[update-check] cannot check: fetch from origin failed"
    )


def test_exit_2_missing_release_branch(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0, with_release_branch=False)
    result = repo.run_check()
    assert result.returncode == 2
    assert "[update-check] cannot check:" in result.stdout


def test_exit_2_unreadable_local_version(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0)
    repo.pyproject.unlink()
    result = repo.run_check()
    assert result.returncode == 2
    assert (
        "cannot check: cannot read the local skill version" in result.stdout
    )


def test_exit_2_unreadable_remote_version(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=0)
    repo.pyproject.write_text("this is not toml\n", encoding="utf-8")
    repo.commit("broken release")
    repo.push()
    # restore a valid local file: only the remote copy stays unreadable
    repo.pyproject.write_text(
        '[project]\nname = "x"\nversion = "0.2.1"\n', encoding="utf-8"
    )
    result = repo.run_check()
    assert result.returncode == 2
    assert (
        "cannot check: cannot read the latest skill version" in result.stdout
    )
