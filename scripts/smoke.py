#!/usr/bin/env python3
"""Platform-independent smoke verification for the Scientific Reproduction skill.

Checks, in order:

1. **Skill structure** — ``SKILL.md`` present with a matching
   ``name: scientific-reproduction`` frontmatter; ``AGENTS.md`` present;
   the four role agents under ``.claude/agents/``, each carrying a
   ``tools:`` allowlist in its frontmatter; the bundled runtime under
   ``src/scientific_reproduction/``; the frozen schemas under ``schemas/``
   and role contracts under ``agent-contracts/``.
2. **Runtime import** — ``scientific_reproduction`` imports from the bundled
   ``src/`` with no install step.
3. **``reproduce init``** — initializes a throwaway one-paper project from
   the official FDM-201 reference DOI (``10.1039/D5TA00771B``) in a
   temporary directory via ``scripts/reproduce.py``, then verifies the
   frozen workspace skeleton (``project.yaml``, the workspace directories,
   and the git checkpoint commit) exists.

Exit code 0: all checks passed. Exit code 1: any check failed.

Usage:
    python scripts/smoke.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DOI = "10.1039/D5TA00771B"

WORKSPACE_DIRS = (
    "sources",
    "knowledge",
    "inventory",
    "plans",
    "work-packages",
    "requirements",
    "goals",
    "runs",
    "evidence",
    "assumptions",
    "protocols",
    "analysis",
    "decisions",
    "events",
    "resources",
    "human-gates",
    "manifests",
    "locks",
    "lab",
    "reports",
)


def _fail(message: str) -> int:
    print(f"[smoke] FAIL: {message}", file=sys.stderr)
    return 1


def _check_structure() -> str | None:
    """Return an error string, or None when the skill structure is valid."""
    if not (SKILL_ROOT / "SKILL.md").is_file():
        return "SKILL.md missing at the skill root"
    if not (SKILL_ROOT / "AGENTS.md").is_file():
        return "AGENTS.md missing at the skill root"
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    if "name: scientific-reproduction" not in text:
        return "SKILL.md frontmatter name is not 'scientific-reproduction'"
    for role in ("supervisor", "worker", "research", "execution-monitor"):
        agent_path = SKILL_ROOT / ".claude" / "agents" / f"{role}.md"
        if not agent_path.is_file():
            return f"role agent missing: .claude/agents/{role}.md"
        text = agent_path.read_text(encoding="utf-8")
        head = text.split("---", 2)[1] if text.startswith("---") else ""
        if "tools:" not in head:
            return (
                f"role agent missing tools allowlist:"
                f" .claude/agents/{role}.md"
            )
    if not (SKILL_ROOT / "src" / "scientific_reproduction").is_dir():
        return "bundled runtime missing: src/scientific_reproduction/"
    if not (SKILL_ROOT / "schemas").is_dir():
        return "frozen schemas missing: schemas/"
    if not (SKILL_ROOT / "agent-contracts").is_dir():
        return "role contracts missing: agent-contracts/"
    return None


def _check_runtime_import() -> str | None:
    """Import the bundled runtime with no install step."""
    code = "import scientific_reproduction; print(scientific_reproduction.__version__)"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SKILL_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=SKILL_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return f"runtime import failed: {result.stderr.strip()}"
    print(f"[smoke] runtime imports (version {result.stdout.strip()})")
    return None


def _check_reproduce_init() -> str | None:
    """Initialize a throwaway FDM-201 project and verify the workspace."""
    with tempfile.TemporaryDirectory(prefix="sci-repro-smoke-") as tmp:
        root = Path(tmp) / "fdm201-demo"
        result = subprocess.run(
            [sys.executable, str(SKILL_ROOT / "scripts" / "reproduce.py"),
             "init", REFERENCE_DOI, "--root", str(root)],
            cwd=SKILL_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            return (
                f"reproduce init failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        if not (root / "project.yaml").is_file():
            return "project.yaml missing after reproduce init"
        missing = [d for d in WORKSPACE_DIRS if not (root / d).is_dir()]
        if missing:
            return f"workspace directories missing after init: {missing}"
        if not (root / ".git").is_dir():
            return ".git missing after reproduce init"
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if log.returncode != 0 or not log.stdout.strip():
            return "git checkpoint commit missing after reproduce init"
        print(f"[smoke] reproduce init OK ({log.stdout.strip()})")
    return None


def main() -> int:
    checks = (
        ("structure", _check_structure),
        ("runtime import", _check_runtime_import),
        ("reproduce init", _check_reproduce_init),
    )
    for name, check in checks:
        print(f"[smoke] checking {name} ...")
        error = check()
        if error:
            return _fail(error)
    print("[smoke] PASS: skill is structurally valid and runs end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
