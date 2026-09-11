"""Distribution packaging and pre-check for the local skill.

The skill is distributed as a ZIP archive (not via GitHub): recipients
open it on other machines, with other agents and other models. This
script does two things:

1. **Pre-check** (always runs): fail on things that must not ship --
   hard-coded developer/server absolute paths, version drift between
   ``pyproject.toml`` and ``scientific_reproduction.__version__``,
   leftover debug/scratch directories, or a dirty git worktree (a zip
   built from uncommitted state is unreproducible).
2. **Zip build** (``--zip``): assemble the release archive with an
   explicit exclusion list, so ``.git`` / ``.venv`` / ``__pycache__`` /
   ``dist`` never enter the archive.

Stdlib only (``tomllib``, Python 3.11+) -- the recipient and the
packager share that one requirement.

Usage::

    python scripts/package_dist.py                # pre-check only
    python scripts/package_dist.py --zip          # pre-check + build zip
    python scripts/package_dist.py --zip --include-tests
"""
from __future__ import annotations

import re as _re
import sys
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = REPO_ROOT / "src" / "scientific_reproduction"
ZIP_TOP_DIR = "scientific-reproduction"  # folder name inside the archive

#: Path/dir names that never enter the archive.
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "dist",
    "tmp_analysis",  # debug-probe leftovers
    "*.egg-info",
}
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo"}

#: Pattern-based leakage markers (generic — no real identifiers stored):
#: IPv4 addresses, user@host forms, /home/<user> & /share/<user> data roots,
#: and WSL host user-dir paths. Present in a release file => release blocker.
LEAK_MARKERS: tuple[str, ...] = (
    r"(?:\d{1,3}\.){3}\d{1,3}",
    r"\b[\w.-]+@[\w.-]+\b",
    r"/home/[\w.-]+",
    r"/share/[\w.-]+",
    r"/mnt/c/Users/[\w.-]+",
)

#: Directory names that must never appear inside src/.
DEBUG_DIR_NAMES = {"tmp_analysis", "probe", "probes", "probe2", "_scratch"}


def _release_files(*, include_tests: bool) -> list[Path]:
    """All files that would enter the archive, sorted (deterministic)."""
    out: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        rel = path.relative_to(REPO_ROOT)
        if any(
            part in EXCLUDE_DIRS or part.endswith(".egg-info")
            for part in rel.parts
        ):
            continue
        if path.suffix in EXCLUDE_FILE_SUFFIXES:
            continue
        if not include_tests and (
            "tests" in rel.parts or rel.parts[0] == "tests"
        ):
            continue
        if rel.parts[0] == "dist":
            continue
        if rel.is_dir():
            continue
        out.append(path)
    return sorted(out)


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]


def _package_version() -> str:
    init = SRC_PKG / "__init__.py"
    text = init.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("__version__"):
            return stripped.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"could not find __version__ in {init}")


def _git_dirty() -> bool | None:
    """True = dirty, False = clean, None = not a git repo (tolerated)."""
    import subprocess

    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return bool(res.stdout.strip())


def precheck(*, include_tests: bool) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). errors non-empty => do not ship."""
    errors: list[str] = []
    warnings: list[str] = []

    py_ver = _pyproject_version()
    pkg_ver = _package_version()
    if py_ver != pkg_ver:
        errors.append(
            f"version drift: pyproject.toml is {py_ver} but"
            f" scientific_reduction.__version__ is {pkg_ver}"
        )

    # entry-point headers must carry the package version (recipients read
    # README.md / 00-README.md first; a stale v0.x header is a release lie)
    for doc_name in ("README.md", "00-README.md"):
        header = (REPO_ROOT / doc_name).read_text(encoding="utf-8").splitlines()[0]
        if f"v{pkg_ver}" not in header:
            errors.append(
                f"version drift: {doc_name} header is {header!r};"
                f" expected a v{pkg_ver} marker"
            )

    dirty = _git_dirty()
    if dirty is None:
        warnings.append("not a git worktree; dirty-state check skipped")
    elif dirty:
        warnings.append(
            "git worktree is dirty (zip would include uncommitted state);"
            " build from a clean checkout for reproducibility"
        )

    # debug/scratch directories inside the shipped package
    for part in DEBUG_DIR_NAMES:
        if (SRC_PKG / part).exists():
            errors.append(f"debug/scratch directory shipped: src/.../{part}")

    # absolute-path leakage (checked over every release file; errors only
    # for non-test files -- tests use anonymous tmp_path fixtures)
    for path in _release_files(include_tests=include_tests):
        # the marker table lives in this script; it must not report itself
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix not in {".py", ".md", ".toml", ".txt", ".yaml", ".json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = []
        for _pat in LEAK_MARKERS:
            _m = _re.search(_pat, text)
            if _m:
                hits.append(_m.group(0))
        if not hits:
            continue
        in_tests = "tests" in path.relative_to(REPO_ROOT).parts
        item = (
            f"{path.relative_to(REPO_ROOT)}: absolute-path marker(s)"
            f" {hits}"
        )
        (warnings if in_tests else errors).append(item)

    return errors, warnings


def build_zip(*, include_tests: bool, out_dir: Path) -> Path:
    ver = _package_version()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"scientific-reproduction-v{ver}.zip"
    files = _release_files(include_tests=include_tests)
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as zf:
        for path in files:
            arc = Path(ZIP_TOP_DIR) / path.relative_to(REPO_ROOT)
            zf.write(path, arcname=str(arc).replace("\\", "/"))
    return target


def main(argv: list[str]) -> int:
    include_tests = "--include-tests" in argv
    build = "--zip" in argv
    out_dir = REPO_ROOT / "dist"

    errors, warnings = precheck(include_tests=include_tests)
    for w in warnings:
        print(f"[warning] {w}")
    if errors:
        for e in errors:
            print(f"[BLOCKED] {e}")
        print("package_dist: pre-check FAILED -- fix these and re-run.")
        return 1
    print(f"package_dist: pre-check OK (version {_package_version()})")

    if build:
        target = build_zip(include_tests=include_tests, out_dir=out_dir)
        print(
            f"package_dist: archive written: {target}"
            f" ({target.stat().st_size // 1024} KiB)"
        )
        print("  contents top-level folder: 'scientific-reproduction/'")
        if not include_tests:
            print("  tests/ excluded (re-add with --include-tests)")
    else:
        print("package_dist: pre-check only. Use --zip to build the archive.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
