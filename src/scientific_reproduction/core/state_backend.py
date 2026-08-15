"""StateBackend interface and filesystem implementation (DEV-M1-G02).

Layout (14-STATE-GIT-ARTIFACTS.md SS2/SS3)
------------------------------------------
``FilesystemStateBackend`` persists **one JSON file per object** under
``base_dir/<tree_dir>/<object_id>.json`` where ``<tree_dir>`` is the
canonical state-tree directory for the schema name
(``SCHEMA_TO_STATE_DIR``) -- there is deliberately **no monolithic
mutable project-state file** (AC-01). Each write is atomic (AC-02, via
``core.atomic.atomic_write``: unique same-directory temp file + fsync +
``os.replace``), so an interrupted write never clobbers the last valid
object.

The tree directories are the **same directories the planning registries
resolve** (plural per-type dirs: ``goals/``, ``runs/``, ``events/``
... per 14-STATE-GIT-ARTIFACTS.md SS3, ``planning.init.INIT_DIRECTORIES``
and the ``*_STATE_DIR`` constants): a worker that reads Core state
through the backend sees exactly the files the registries write, so
``list_ids``/``read`` never miss records the registries produced (AC-02
truth-source contract).

Schema gate (AC-03)
-------------------
``write`` validates the object against the frozen schema
``schemas/<obj_type>.schema.yaml`` through
``scientific_reproduction.core.schema_validation.validate_and_reject``
(DEV-M1-G01) and raises ``SchemaValidationError`` *before* anything is
persisted. Unknown ``obj_type`` values -- anything not in the normative
model registry -- are rejected with ``UnknownObjectTypeError``.

Security posture
----------------
* ``read`` refuses symlinked object files and any object path whose
  resolved location escapes the resolved ``base_dir`` (this also covers
  type directories that symlink outside the workspace). ``write``
  resolves the target's parent directory and refuses writes that would
  escape the resolved ``base_dir``, before any staging file is created.
* Object IDs that differ only by case are rejected on **every** platform
  (``"Foo"`` and ``"foo"`` collide on case-insensitive filesystems such
  as Windows/macOS but not on Linux; making the rejection a policy keeps
  behavior deterministic and testable everywhere).
* Type directories must **not** be writable by untrusted principals: the
  backend validates containment, but cannot defend a directory that an
  attacker is already able to write.
* Object IDs are expected to follow the canonical ``generate_id`` pattern
  from ``core.ids.py`` (``sr_<kind>_<32 hex chars>``); the backend still
  accepts any safe file stem and validates the shape defensively.

Events are ordinary objects of type ``event``; append-only policy on top
of per-object CRUD is a workflow-layer concern, not enforced here.

Corrupt-object posture (DEV-M1-G05)
-----------------------------------
``read`` is deliberately **not** a re-validation gate: a stored file
whose JSON parses into an object is returned as-is even when an external
hand edit made it schema-invalid. The schema gate lives on ``write``
(14-STATE-GIT-ARTIFACTS.md SS2 "state validation against schemas" is
satisfied at the persistence point), and ``read`` never silently repairs
or rewrites stored bytes -- corruption surfaces as a deterministic error
instead:

* unparseable JSON (truncated files, invalid UTF-8) raises ``ValueError``
  naming the stored object as corrupt;
* valid JSON that is not a JSON object (``[1, 2]``, ``"x"``, ``42``)
  raises ``ValueError``;
* an entry at the object path that is not a regular file (e.g. a
  directory planted at ``<id>.json``) raises ``ValueError`` from both
  ``read`` and ``delete`` (a plain ``unlink`` would raise
  ``IsADirectoryError`` on POSIX but ``PermissionError`` on Windows --
  not deterministic);
* ``delete`` of a corrupt *record* is deliberately allowed: deleting is
  operational repair and never parses the file, so a corrupt record is
  removable without reading it;
* ``list_ids`` reports every ``*.json`` entry regardless of content, so
  corruption surfaces on the ``read`` that touches it, never on
  ``list_ids``.

The interface exists so future backends (e.g. SQLite/PostgreSQL per
14-STATE-GIT-ARTIFACTS.md SS8) can be added without changing agent
governance logic. v0.1 ships exactly one implementation.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import SCHEMA_NAMES
from scientific_reproduction.core.schema_validation import validate_and_reject

#: Canonical state-tree directory for each schema name (``obj_type``).
#:
#: The state tree (14-STATE-GIT-ARTIFACTS.md SS3) uses **plural
#: per-type directories** (``goals/``, ``runs/``, ``events/`` ...), and
#: the planning registries already resolve exactly these directories
#: (e.g. ``planning.plan.GOALS_STATE_DIR == "goals"``). The backend
#: resolves each object to ``base_dir/<tree_dir>/<object_id>.json`` so
#: every reader of Core state sees the same files the registries write:
#: there is exactly one canonical layout (AC-02 truth-source contract).
#:
#: * values present in ``templates/PROJECT-TREE.template.txt``
#:   (``planning.init.INIT_DIRECTORIES``) are that exact directory;
#: * ``acceptance/`` and ``closure/`` follow the registries that created
#:   them on demand (``ACCEPTANCE_STATE_DIR``, ``CLOSURE_STATE_DIR``);
#:   ``research-requests/`` and ``retry-policies/`` extend the same
#:   plural-of-schema-name convention for their on-demand kinds;
#: * ``lab/`` is the lab subtree of the tree template (whose children
#:   ``lab/outgoing`` and ``lab/incoming`` the lab adapter already
#:   uses for handoffs);
#: * ``project`` is the one schema name without a tree directory: the
#:   canonical single Project record is ``project.yaml`` at the workspace
#:   root (written by ``planning.init``), so the backend keeps the
#:   per-type directory ``project/``.
SCHEMA_TO_STATE_DIR: dict[str, str] = {
    "project": "project",
    "run": "runs",
    "plan": "plans",
    "goal": "goals",
    "evidence": "evidence",
    "assumption": "assumptions",
    "closure-contract": "closure",
    "inventory-item": "inventory",
    "resource": "resources",
    "source": "sources",
    "worker-context": "work-packages",
    "lab-execution-package": "lab",
    "analysis": "protocols",
    "decision": "decisions",
    "human-gate": "human-gates",
    "event": "events",
    "requirement": "requirements",
    "artifact-manifest": "manifests",
    "acceptance-criteria": "acceptance",
    "research-request": "research-requests",
    "retry-policy": "retry-policies",
}

__all__ = [
    "StateBackend",
    "FilesystemStateBackend",
    "UnknownObjectTypeError",
    "SCHEMA_TO_STATE_DIR",
]


class UnknownObjectTypeError(ValueError):
    """Raised when ``obj_type`` is not a known normative object type.

    The known types are the schema names in the model registry
    (``models.SCHEMA_NAMES``), one file per type under ``schemas/``.
    """


class StateBackend(ABC):
    """Interface for persisting normative project objects.

    All methods take the schema name (``obj_type``) and the object's
    deterministic ID. Implementations must reject unknown object types,
    refuse non-dict content, and -- for ``write`` -- never persist
    schema-invalid objects (AC-03 gate).
    """

    @abstractmethod
    def write(self, obj_type: str, object_id: str, data: dict[str, Any]) -> None:
        """Persist ``data`` for ``obj_type``/``object_id`` atomically.

        Raises:
            UnknownObjectTypeError: unknown ``obj_type``.
            ValueError: invalid ``object_id``.
            TypeError: ``data`` is not a dict.
            SchemaValidationError: ``data`` fails the object's schema
                (nothing is persisted).
        """

    @abstractmethod
    def read(self, obj_type: str, object_id: str) -> dict[str, Any]:
        """Return the persisted object as a plain dict.

        ``read`` is not a re-validation gate: schema-invalid but
        parseable content is returned as-is, never repaired. Corruption
        that makes the content unparseable raises a deterministic
        ``ValueError`` (see the module docstring, "Corrupt-object
        posture").

        Raises:
            UnknownObjectTypeError: unknown ``obj_type``.
            ValueError: invalid ``object_id``.
            FileNotFoundError: no such object.
            ValueError: stored content is corrupt (not valid JSON, or
                valid JSON that is not an object), or the path exists
                but is not a regular file.
        """

    @abstractmethod
    def exists(self, obj_type: str, object_id: str) -> bool:
        """Return True if an object with ``object_id`` is persisted."""

    @abstractmethod
    def list_ids(self, obj_type: str) -> list[str]:
        """Return the sorted IDs of all persisted objects of ``obj_type``."""

    @abstractmethod
    def delete(self, obj_type: str, object_id: str) -> None:
        """Remove the persisted object.

        Deleting never parses the file, so a corrupt record is removable
        (operational repair); deleting an entry that is not a regular
        file (e.g. a directory at the object path) raises a documented
        error instead of a platform-dependent one.

        Raises:
            UnknownObjectTypeError: unknown ``obj_type``.
            ValueError: invalid ``object_id``.
            FileNotFoundError: no such object.
            ValueError: the path exists but is not a regular file.
        """


class FilesystemStateBackend(StateBackend):
    """Per-object, per-type JSON persistence on the filesystem.

    Layout: ``base_dir/<tree_dir>/<object_id>.json`` where ``<tree_dir>``
    is the canonical state-tree directory for the schema name
    (``SCHEMA_TO_STATE_DIR``, the same plural directories the planning
    registries resolve). All writes go through ``atomic_write`` (temp
    file + ``os.replace``), so partial writes never replace the last
    valid object (AC-02).

    Args:
        base_dir: root of the state tree. May be a ``str`` or ``Path``.
            If ``base_dir`` itself is a symlink it is followed and the
            *resolved* location is the containment anchor.
        file_mode: optional explicit mode for written object files,
            passed through to ``atomic_write`` (``None`` keeps
            ``mkstemp``'s 0o600 default; operators wanting a shared
            workspace should pass e.g. ``0o644`` explicitly).
    """

    #: Object IDs become file stems; anything that could escape the
    #: ``<obj_type>`` directory is rejected defensively.
    _FORBIDDEN_ID_CHARS = ("/", "\\", "\x00")

    def __init__(
        self, base_dir: str | Path, *, file_mode: int | None = None
    ) -> None:
        self.base_dir = Path(base_dir)
        #: Resolved once at construction: the containment anchor used by
        #: the symlink/escape checks in ``read`` and ``write``.
        self._base_dir_resolved = self.base_dir.resolve()
        self._file_mode = file_mode

    # -- path / input validation -------------------------------------------

    def _check_obj_type(self, obj_type: str) -> None:
        if obj_type not in SCHEMA_NAMES:
            known = ", ".join(sorted(SCHEMA_NAMES))
            raise UnknownObjectTypeError(
                f"unknown object type {obj_type!r}; expected one of: {known}"
            )

    def _check_object_id(self, object_id: str) -> None:
        if not isinstance(object_id, str) or not object_id:
            raise ValueError("object_id must be a non-empty string")
        if object_id in (".", "..") or any(
            c in self._FORBIDDEN_ID_CHARS for c in object_id
        ):
            raise ValueError(
                f"invalid object_id {object_id!r}: must be a plain file stem"
                " (no path separators, no '.', no '..')"
            )

    def _object_path(self, obj_type: str, object_id: str) -> Path:
        self._check_obj_type(obj_type)
        self._check_object_id(object_id)
        return self.base_dir / SCHEMA_TO_STATE_DIR[obj_type] / f"{object_id}.json"

    def _type_dir(self, obj_type: str) -> Path:
        self._check_obj_type(obj_type)
        return self.base_dir / SCHEMA_TO_STATE_DIR[obj_type]

    def _check_write_target(self, path: Path) -> None:
        """Refuse writes whose resolved parent escapes the resolved base.

        Resolving the parent before any staging file is created closes the
        symlinked-type-dir escape: a type directory that symlinks outside
        ``base_dir`` would otherwise let a write land outside the
        workspace. Type directories must not be writable by untrusted
        principals.
        """
        resolved_parent = path.parent.resolve()
        if not resolved_parent.is_relative_to(self._base_dir_resolved):
            raise ValueError(
                f"refusing to write {path}: resolved parent"
                f" {resolved_parent} escapes the state base directory"
                f" {self._base_dir_resolved}"
            )

    def _check_no_case_collision(self, obj_type: str, object_id: str) -> None:
        """Reject an ID that differs only by case from an existing entry.

        On case-insensitive filesystems (Windows/macOS) ``"Foo"`` and
        ``"foo"`` are the same file, while on Linux they are distinct --
        non-deterministic cross-platform behavior. To make this a
        deterministic, testable policy on every platform, the check runs
        on all platforms before writing. Exact-case rewrites of the same
        ID remain allowed.
        """
        type_dir = self._type_dir(obj_type)
        if not type_dir.is_dir():
            return
        target_name = f"{object_id}.json"
        target_folded = target_name.casefold()
        try:
            entries = os.listdir(type_dir)
        except OSError:
            # Unreadable directory: the write itself will surface the real
            # error; do not mask it with a collision guess.
            return
        for entry in entries:
            if entry != target_name and entry.casefold() == target_folded:
                raise ValueError(
                    f"object id {object_id!r} collides case-insensitively"
                    f" with existing entry {entry!r} in {obj_type!r}; object"
                    f" IDs are expected to follow the canonical"
                    f" generate_id pattern from core.ids.py"
                )

    # -- StateBackend ------------------------------------------------------

    def write(self, obj_type: str, object_id: str, data: dict[str, Any]) -> None:
        """Validate, serialize canonically, and persist atomically."""
        path = self._object_path(obj_type, object_id)
        if not isinstance(data, dict):
            # Deliberate runtime contract check for dynamically typed
            # callers (the interface promises TypeError for non-dict
            # content). Pyright flags this branch as unreachable because
            # the annotation already says dict -- it is not dead code.
            raise TypeError(
                f"object content for {obj_type!r}/{object_id!r} must be a dict,"
                f" got {type(data).__name__}"
            )
        # Persistence gate (AC-03): schema-invalid objects are rejected
        # before anything is written (DEV-M1-G01 AC-03 extends here).
        validate_and_reject(obj_type, data)
        try:
            # Canonical deterministic serialization: same dict always
            # produces byte-identical content, so read-after-write returns
            # the exact schema-valid canonical document.
            canonical = json.dumps(
                data, indent=2, sort_keys=True, ensure_ascii=False
            )
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"object content for {obj_type!r}/{object_id!r} is not"
                f" JSON-serializable: {exc}"
            ) from exc
        self._check_write_target(path)
        self._check_no_case_collision(obj_type, object_id)
        atomic_write(path, canonical, file_mode=self._file_mode)

    def read(self, obj_type: str, object_id: str) -> dict[str, Any]:
        path = self._object_path(obj_type, object_id)
        if path.is_symlink():
            raise ValueError(
                f"refusing to read symlinked object file: {path}"
            )
        resolved = path.resolve()
        if not resolved.is_relative_to(self._base_dir_resolved):
            raise ValueError(
                f"refusing to read {path}: resolved path {resolved} escapes"
                f" the state base directory {self._base_dir_resolved}"
            )
        if not path.is_file():
            if path.exists():
                # An entry exists at the object path but is not a regular
                # file (e.g. a directory planted by an external edit):
                # deterministic documented error, never a silent fallback.
                raise ValueError(
                    f"stored object {obj_type!r}/{object_id!r} at {path} is"
                    " not a regular file"
                )
            raise FileNotFoundError(
                f"no object of type {obj_type!r} with id {object_id!r}: {path}"
            )
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(
                f"stored object {obj_type!r}/{object_id!r} at {path} is"
                f" corrupt: {exc}"
            ) from exc
        if not isinstance(content, dict):
            raise ValueError(
                f"stored object {obj_type!r}/{object_id!r} at {path} is not a"
                f" JSON object"
            )
        return content

    def exists(self, obj_type: str, object_id: str) -> bool:
        return self._object_path(obj_type, object_id).is_file()

    def list_ids(self, obj_type: str) -> list[str]:
        type_dir = self._type_dir(obj_type)
        if not type_dir.is_dir():
            return []
        return sorted(path.stem for path in type_dir.glob("*.json"))

    def delete(self, obj_type: str, object_id: str) -> None:
        path = self._object_path(obj_type, object_id)
        if path.is_dir():
            # A plain unlink would raise IsADirectoryError on POSIX but
            # PermissionError on Windows -- non-deterministic across
            # platforms. Refuse with a documented error instead.
            raise ValueError(
                f"refusing to delete {path}: not a regular file"
            )
        try:
            path.unlink()
        except FileNotFoundError:
            raise FileNotFoundError(
                f"no object of type {obj_type!r} with id {object_id!r}: {path}"
            ) from None
