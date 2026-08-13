"""Platform neutrality: the Core never imports Claude-specific task
APIs, and the platform orchestration surface is the only platform
contact point (DEV-M10-G02 AC-01).

The frozen 15-ADAPTER-SPEC.md SS5 rule is "The Core must not embed
platform-specific command syntax"; AC-01 lifts that to the import
level. These tests enforce it mechanically with an AST import scan
(deterministic, no I/O beyond reading the module sources):

* ``adapters/platform/base.py`` -- the orchestration interface module
  itself imports no Claude-specific (anthropic/claude-code) module and
  no third-party module at all: only stdlib and
  ``scientific_reproduction`` packages;
* ``core/**`` -- the Core never imports a Claude-specific module and
  never imports the platform adapter subsystem at all (the platform
  surface is the interface, and only the interface);
* the interface itself is the abstract ``PlatformAdapter`` with exactly
  the operations of the locked orchestration contract
  (15-ADAPTER-SPEC.md SS5) and no concrete platform behavior.

The suite is pure: no wall clock, no randomness.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

from scientific_reproduction.adapters.platform.base import (
    PlatformAdapter,
    PlatformOperation,
)

#: Repository root: tests/platform/ -> parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Root module names that are Claude-specific task APIs (the Agent
#: SDK's ``claude_code``/``anthropic`` packages and the ``claude`` CLI
#: package, including the ``us.anthropic`` vendored namespace). Any
#: import whose root is one of these is a direct Core -> platform-API
#: coupling and violates AC-01.
CLAUDE_SPECIFIC_IMPORT_ROOTS: tuple[str, ...] = (
    "anthropic",
    "claude_code",
    "claude",
    "us.anthropic",
)

PLATFORM_BASE_PATH = (
    REPO_ROOT
    / "src"
    / "scientific_reproduction"
    / "adapters"
    / "platform"
    / "base.py"
)
CORE_DIR = REPO_ROOT / "src" / "scientific_reproduction" / "core"


def _iter_imported_modules(source: str) -> list[str]:
    """All module names this source imports (import/from-import)."""
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return sorted(set(modules))


def _import_root(module: str) -> str:
    return module.split(".")[0]


def _claude_specific(modules: list[str]) -> list[str]:
    return [
        module
        for module in modules
        if _import_root(module) in CLAUDE_SPECIFIC_IMPORT_ROOTS
    ]


def _py_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py") if path.is_file())


def test_ac01_platform_interface_module_imports_no_claude_specific_apis():
    # The orchestration interface module is the only platform surface;
    # it must import no Claude-specific task API directly (AC-01).
    imports = _iter_imported_modules(PLATFORM_BASE_PATH.read_text(encoding="utf-8"))
    assert _claude_specific(imports) == []


def test_ac01_platform_interface_module_imports_only_stdlib_and_core_packages():
    # Stronger neutrality: base.py imports no third-party module at all
    # -- only stdlib and scientific_reproduction packages (which are
    # themselves platform-neutral). No anthropic/claude-code SDK can
    # reach the interface through an indirect first-party dependency.
    imports = _iter_imported_modules(PLATFORM_BASE_PATH.read_text(encoding="utf-8"))
    for module in imports:
        root = _import_root(module)
        assert root in sys.stdlib_module_names or root == "scientific_reproduction", (
            f"adapters/platform/base.py must import only stdlib or"
            f" scientific_reproduction modules, imported {module!r}"
        )


def test_ac01_core_package_imports_no_claude_specific_modules():
    # AC-01: the Core never imports Claude-specific task APIs directly.
    # Every module under core/ is scanned; a Claude-specific import root
    # anywhere in the Core is a direct coupling and fails the scan.
    offenders: list[str] = []
    for path in _py_files(CORE_DIR):
        modules = _iter_imported_modules(path.read_text(encoding="utf-8"))
        for module in _claude_specific(modules):
            offenders.append(f"{path.relative_to(REPO_ROOT)} imports {module!r}")
    assert offenders == []


def test_ac01_core_package_imports_no_platform_adapter_subsystem():
    # "The rest of the Core must not import any platform-specific API
    # either": the Core never imports the platform adapter subsystem at
    # all -- the platform surface is the PlatformAdapter interface and
    # nothing else.
    offenders: list[str] = []
    for path in _py_files(CORE_DIR):
        modules = _iter_imported_modules(path.read_text(encoding="utf-8"))
        for module in modules:
            if module.startswith("scientific_reproduction.adapters"):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)} imports adapter subsystem"
                    f" {module!r}"
                )
    assert offenders == []


def test_ac01_platform_adapter_is_abstract_and_exposes_only_the_locked_surface():
    # The orchestration interface is abstract (no concrete platform
    # behavior may live here) and exposes exactly the operations of
    # 15-ADAPTER-SPEC.md SS5 plus the capability query.
    assert inspect.isabstract(PlatformAdapter)
    with pytest.raises(TypeError):
        PlatformAdapter()  # type: ignore[abstract]
    assert PlatformAdapter.__abstractmethods__ == frozenset(
        {
            "capabilities",
            "spawn_persistent_role",
            "spawn_worker",
            "resume_session",
            "terminate_session",
            "is_session_alive",
            "expose_command",
            "fallback_subagent",
        }
    )


def test_ac01_operation_vocabulary_matches_the_interface_surface():
    # Every PlatformOperation member is one interface capability and
    # vice versa: the frozen vocabulary of 15-ADAPTER-SPEC.md SS5 is
    # exactly the abstract surface, so no capability exists without an
    # interface answer and no interface method exists without a
    # vocabulary member.
    vocabulary = {operation.value for operation in PlatformOperation}
    assert vocabulary == {
        "spawn_persistent_role",
        "spawn_worker",
        "resume_session",
        "terminate_session",
        "is_session_alive",
        "expose_command",
        "fallback_subagent",
    }


def test_ac01_interface_module_has_no_wall_clock_and_no_randomness():
    # Determinism hygiene of the platform-neutral surface: the interface
    # module imports no randomness/wall-clock facilities and its
    # abstract methods carry no concrete timestamp/random behavior.
    source = PLATFORM_BASE_PATH.read_text(encoding="utf-8")
    imports = _iter_imported_modules(source)
    for forbidden in ("random", "uuid", "time", "datetime"):
        assert forbidden not in imports
