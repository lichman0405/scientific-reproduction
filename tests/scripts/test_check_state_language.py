"""State-language lint contract tests (P22-C).

Covers the command-line contract of ``scripts/check_state_language.py``
documented in its own docstring and in
``docs/user/reproduction-delivery-norms.md``:

- both ``--expect zh`` (space form) and ``--expect=zh`` (equals form) are
  accepted and agree -- the space form previously raised ``IndexError``
  because ``"--expect".split("=", 1)[1]`` was read unconditionally;
- exit ``0`` -- display fields are in the expected language (or absent);
- exit ``1`` -- violations found, each listed with its source file;
- exit ``2`` -- missing workspace, unknown argument, or an unsupported
  ``--expect`` value.

The workspace is a minimal set of state records; the script is exercised
through its real command line (the convention of
``tests/scripts/test_check_update.py``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_state_language.py"

ZH_PROSE = (
    "该假设的成立依赖论文正文未披露的样品预处理步骤，这里按缺省条件处理，"
    "并在附录中记录了偏差来源。"
)
EN_PROSE = (
    "This assumption holds only when the sample pretreatment step that the "
    "paper omits is applied exactly as described in the supplementary note."
)


def _workspace(tmp_path: Path, prose: str) -> Path:
    """A workspace whose single assumption carries ``prose`` as rationale."""
    root = tmp_path / "proj"
    (root / "assumptions").mkdir(parents=True)
    (root / "assumptions" / "ASM-1.json").write_text(
        json.dumps({"assumption_id": "ASM-1", "rationale": prose}),
        encoding="utf-8",
    )
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )


def test_space_form_and_equals_form_agree(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ZH_PROSE)
    spaced = _run(root, "--expect", "zh")
    equals = _run(root, "--expect=zh")
    assert spaced.returncode == 0, spaced.stderr
    assert equals.returncode == 0, equals.stderr
    assert "Traceback" not in spaced.stderr
    assert spaced.stdout == equals.stdout


def test_expect_defaults_to_zh(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ZH_PROSE)
    assert _run(root).returncode == 0


def test_english_prose_in_zh_workspace_is_a_violation(tmp_path: Path) -> None:
    root = _workspace(tmp_path, EN_PROSE)
    result = _run(root, "--expect", "zh")
    assert result.returncode == 1
    assert "ASM-1.json" in result.stdout
    assert "[rationale]" in result.stdout


def test_unknown_argument_is_a_usage_error(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ZH_PROSE)
    result = _run(root, "--expct", "zh")
    assert result.returncode == 2
    assert "usage:" in result.stdout


def test_unsupported_value_is_a_usage_error(tmp_path: Path) -> None:
    root = _workspace(tmp_path, ZH_PROSE)
    result = _run(root, "--expect", "fr")
    assert result.returncode == 2
    assert "unsupported --expect value" in result.stdout


def test_missing_workspace_is_a_usage_error(tmp_path: Path) -> None:
    assert _run(tmp_path / "nope").returncode == 2
