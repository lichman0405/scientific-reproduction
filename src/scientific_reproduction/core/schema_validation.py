"""Schema validation adapter against the frozen product schemas (DEV-M1-G01).

Validates plain objects (dicts) against the normative JSON Schemas in
``schemas/<name>.schema.yaml`` using ``jsonschema`` (draft 2020-12). This is
the persistence gate: schema-invalid objects are rejected before they are
written (AC-03 of DEV-M1-G01).

Schema location / resolution
----------------------------
Schemas are part of the installed repository. The schemas directory is
resolved relative to this package (``<repo>/schemas``), which works for
both the source checkout and an installed package; an explicit override is
available via the ``SCIENTIFIC_REPRODUCTION_SCHEMAS_DIR`` environment
variable. Loading is **lazy**: the YAML schema and the ``jsonschema``
library are imported on first use, and loaded schemas are cached.

Missing files are not silently ignored: a missing schema raises
``SchemaNotFoundError`` with the expected path in the message.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

# Imported lazily inside the functions that need them so that a missing
# optional dependency never breaks importing this module.
_yaml: Any = None
_jsonschema: Any = None

SCHEMAS_DIR_ENV = "SCIENTIFIC_REPRODUCTION_SCHEMAS_DIR"

#: schema stem -> filename, e.g. "project" -> "project.schema.yaml".
#: All 21 normative object types from schemas/*.schema.yaml.
KNOWN_OBJECT_TYPES = frozenset(
    {
        "acceptance-criteria",
        "analysis",
        "artifact-manifest",
        "assumption",
        "closure-contract",
        "decision",
        "event",
        "evidence",
        "goal",
        "human-gate",
        "inventory-item",
        "lab-execution-package",
        "plan",
        "project",
        "requirement",
        "research-request",
        "resource",
        "retry-policy",
        "run",
        "source",
        "worker-context",
    }
)


class SchemaNotFoundError(KeyError):
    """Raised when the schema file for an object type cannot be located."""


class SchemaValidationError(ValueError):
    """Raised by ``validate_and_reject`` when an object fails its schema.

    Attributes:
        obj_type: the schema name the object was validated against.
        errors: the list of validation error messages (never empty here).
    """

    def __init__(self, obj_type: str, errors: list[str]) -> None:
        self.obj_type = obj_type
        self.errors = list(errors)
        super().__init__(f"{obj_type}: {len(self.errors)} schema validation error(s)")


def schemas_dir() -> Path:
    """Return the directory holding ``*.schema.yaml`` files.

    Resolution order:
      1. ``SCIENTIFIC_REPRODUCTION_SCHEMAS_DIR`` environment variable;
      2. the ``schemas/`` directory next to the repository root, derived
         from this file's location (``<pkg>/core/ -> <repo>/schemas``).
    """
    override = os.environ.get(SCHEMAS_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "schemas"


def _load_yaml_schema(obj_type: str) -> dict[str, Any]:
    global _yaml
    if _yaml is None:
        import yaml  # type: ignore[import-untyped]

        _yaml = yaml
    schema_path = schemas_dir() / f"{obj_type}.schema.yaml"
    if not schema_path.is_file():
        raise SchemaNotFoundError(
            f"no schema for object type {obj_type!r}: expected file at "
            f"{schema_path} (set {SCHEMAS_DIR_ENV} to override the schemas dir)"
        )
    try:
        loaded = _yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - malformed schema file path
        raise SchemaNotFoundError(
            f"could not load schema {schema_path}: {exc}"
        ) from exc
    if not isinstance(loaded, dict):
        raise SchemaNotFoundError(
            f"schema file {schema_path} does not contain a mapping"
        )
    return loaded


@lru_cache(maxsize=None)
def load_schema(obj_type: str) -> dict[str, Any]:
    """Load and cache the JSON Schema for ``obj_type``.

    Raises:
        SchemaNotFoundError: if the schema file is missing or unparseable.
        ValueError: if ``obj_type`` is not a known normative object type.
    """
    if obj_type not in KNOWN_OBJECT_TYPES:
        known = ", ".join(sorted(KNOWN_OBJECT_TYPES))
        raise ValueError(
            f"unknown object type {obj_type!r}; expected one of: {known}"
        )
    return _load_yaml_schema(obj_type)


def _validator_for(schema: dict[str, Any]) -> Any:
    global _jsonschema
    if _jsonschema is None:
        import jsonschema  # type: ignore[import-untyped]

        _jsonschema = jsonschema
    return _jsonschema.Draft202012Validator(schema)


def validate_object(obj_type: str, data: dict[str, Any]) -> list[str]:
    """Validate ``data`` against ``schemas/<obj_type>.schema.yaml``.

    Returns a list of human-readable validation error messages; an empty
    list means the object is valid. Raises ``SchemaNotFoundError`` when the
    schema file is missing and ``ValueError`` for unknown object types.
    """
    schema = load_schema(obj_type)
    validator = _validator_for(schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(data),
        key=lambda e: (len(e.absolute_path), str(list(e.absolute_path))),
    ):
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{path}: {error.message}")
    return errors


def validate_and_reject(obj_type: str, data: dict[str, Any]) -> None:
    """Validate ``data`` and raise ``SchemaValidationError`` if invalid.

    This is the persistence gate: call before writing an object so that
    schema-invalid objects are rejected before persistence (AC-03).
    """
    errors = validate_object(obj_type, data)
    if errors:
        raise SchemaValidationError(obj_type, errors)
