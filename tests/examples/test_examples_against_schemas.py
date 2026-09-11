"""Guard: every shipped example instance must validate against its frozen schema.

The examples in ``examples/fdm-201/`` are the reference material users and
agents copy when authoring state records. A drift between an example and its
frozen schema makes the official reference fail validation at registration
time (a real regression: goal.example.yaml carried string ``outputs`` while
``schemas/goal.schema.json`` requires objects). This test pins every example
to its schema so any future drift fails here first.

Mapping: example file -> schema file (by record kind).
"""
import json
from pathlib import Path

import jsonschema
import pytest
import yaml

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "fdm-201"
SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"

# example file -> schema file; one entry per shipped example record kind
EXAMPLE_TO_SCHEMA = {
    "inventory.example.yaml": "inventory-item.schema.json",
    "requirement.example.yaml": "requirement.schema.json",
    "goal.example.yaml": "goal.schema.json",
    "acceptance.example.yaml": "acceptance-criteria.schema.json",
    "statistical-design.example.yaml": "statistical-design.schema.json",
    "evidence.example.yaml": "evidence.schema.json",
    "assumption.example.yaml": "assumption.schema.json",
    "research-request.example.yaml": "research-request.schema.json",
    "project.example.yaml": "project.schema.json",
}


def _iter_example_schema_pairs():
    for example_name, schema_name in EXAMPLE_TO_SCHEMA.items():
        example_path = EXAMPLES_DIR / example_name
        schema_path = SCHEMAS_DIR / schema_name
        if not example_path.exists() or not schema_path.exists():
            # A trimmed install may drop examples; skip the pair silently
            # rather than failing collection on a missing file.
            continue
        yield example_path, schema_path


@pytest.mark.parametrize(
    ("example_path", "schema_path"),
    [pytest.param(ep, sp, id=ep.name) for ep, sp in _iter_example_schema_pairs()],
)
def test_example_validates_against_schema(example_path, schema_path):
    record = yaml.safe_load(example_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    # Validate with full error detail on failure.
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(record), key=lambda e: list(e.path))
    assert not errors, (
        f"{example_path.name} does not validate against {schema_path.name}: "
        + "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
    )
