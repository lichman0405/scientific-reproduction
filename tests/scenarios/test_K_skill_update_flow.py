"""Scenario K: pre-flight skill update flow (issue #117).

Demonstrates the full documented update flow against a local 'installed
skill' clone with real commands, no network:

1. **report** — ``scripts/check-update.py`` exits 1 and prints the local
   version, the latest available version and the N-behind commit count;
2. **no silent auto-update** — the check leaves the worktree untouched;
3. **confirm** — the human step; the machine-checkable part is that the
   check demands explicit user confirmation before applying;
4. **apply** — ``git merge --ff-only origin/release/skill-v0.2.0``
   fast-forwards the clean worktree to the release tip, and refuses a
   dirty one (never forced);
5. **smoke gate** — ``python scripts/smoke.py`` from the *updated*
   checkout must print ``[smoke] PASS`` before work proceeds.

The fixture seeds the clone from a ``git archive`` of this repository's
committed HEAD (via ``make_update_check_repo(full=True)``), so the whole
skill machinery is present and this test exercises the committed tree —
CI always runs it that way.
"""

from __future__ import annotations

import subprocess
import sys


def _merge_ff_only(repo) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo.work), "merge", "--ff-only",
         "origin/release/skill-v0.2.0"],
        capture_output=True,
        text=True,
    )


def test_skill_update_flow(make_update_check_repo) -> None:
    repo = make_update_check_repo(behind=1, full=True)

    # 1. report: the pre-flight check detects the gap...
    report = repo.run_check()
    assert report.returncode == 1
    assert f"local {repo.local_version}" in report.stdout
    assert f"latest {repo.latest_version}" in report.stdout
    assert "behind by 1 commit on origin/release/skill-v0.2.0" in report.stdout
    assert repo.local_version != repo.latest_version

    # 2. ...and never mutates the worktree (no silent auto-update).
    pyproject_before = repo.pyproject.read_text(encoding="utf-8")
    assert f'version = "{repo.latest_version}"' not in pyproject_before

    # 3. confirm: the apply instructions require explicit confirmation.
    assert "explicit confirmation" in report.stdout

    # 4. apply: a dirty worktree refuses the fast-forward; never force it.
    repo.dirty()
    refused = _merge_ff_only(repo)
    assert refused.returncode != 0
    repo.clean()
    merged = _merge_ff_only(repo)
    assert merged.returncode == 0, f"merge failed:\n{merged.stderr}"
    assert repo.local_version == repo.latest_version

    # 4b. the updated checkout is now up to date.
    updated = repo.run_check()
    assert updated.returncode == 0
    assert "is up to date" in updated.stdout

    # 5. smoke gate: the updated skill must still print [smoke] PASS.
    smoke = subprocess.run(
        [sys.executable, str(repo.work / "scripts" / "smoke.py")],
        cwd=repo.work,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert smoke.returncode == 0, (
        f"smoke.py exited {smoke.returncode}\nstdout:\n{smoke.stdout}\n"
        f"stderr:\n{smoke.stderr}"
    )
    assert "[smoke] PASS" in smoke.stdout
