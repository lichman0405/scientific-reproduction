# ADR-0002 — Runtime dependency contract: JSON contract schemas and a single runtime dependency

- **Status:** proposed
- **Date:** 2026-09-11
- **Reviewer:** PR reviewer (assigned at review)
- **Related PR:** this PR (v0.3.1 upgrade)
- **Supersedes:** none
- **Superseded by:** none

## Context

The v0.1 package declared "stdlib-only" across `pyproject.toml`
(`dependencies = []`), the installation guide, `SKILL.md` and code comments,
but `core.schema_validation` imported `jsonschema` and `yaml` at runtime: a
clean install failed with `ModuleNotFoundError` the first time a record was
validated. The frozen contract schemas also lived in `schemas/*.schema.yaml`,
so reading a schema document required PyYAML even though the runtime has no
other YAML need.

Two alternatives were considered:

- **(a) keep the YAML schemas and declare both `jsonschema` and `PyYAML` as
  runtime dependencies.** Rejected: PyYAML would be a runtime dependency
  whose only purpose is deserializing documents this repository owns and can
  store in any format, and the declared dependency set would stay wider than
  the actual need.
- **(b) store the contract schemas as JSON and declare `jsonschema` only.**
  Chosen: the standard library reads JSON, the schemas are machine-written
  artifacts that no human edits by hand, and the runtime dependency set then
  matches exactly what the code imports.

## Decision

Contract schemas are stored as JSON (`schemas/*.schema.json`) and loaded with
the standard library `json` module. `jsonschema` becomes the single declared
runtime dependency (`dependencies = ["jsonschema>=4.18"]`, pure Python, no
build dependencies). PyYAML stays a dev-only tool (frozen benchmark data and
YAML test fixtures/examples, installed through the `dev` extra).

Validation semantics are unchanged: the same `jsonschema` validator
(JSON Schema draft 2020-12, declared by `$schema` in every schema document)
is applied to the same field definitions, and
`core.schema_validation.validate_and_reject` keeps its signature and its
error contract — the 14 `src` modules that reference it are unaffected.

## Consequences

Positive: clean installs work (`pip install .` pulls exactly one pure-Python
dependency); the declared dependency set matches what the code imports; the
"stdlib-only" claim that contradicted the code is removed everywhere it
appeared.

Negative / trade-offs:

- Downstream tooling that reads `schemas/*.yaml` must switch to the JSON
  files — the YAML files are removed in the same change, and
  `docs/spec/index.md` plus every internal reference is updated with them.
- The project is no longer installable with zero third-party packages;
  environments that cannot install anything must vendor `jsonschema`.
- Contributors adding YAML-specific *runtime* code must move it to the dev
  extra or reconsider it; YAML remains dev-only by construction.

## Verification notes

- `tests/core/test_schema_validation.py` — the JSON loading path and the
  validation/rejection contract.
- The migration preserved each document's `$schema` declaration, `required`
  set and property definitions; the migrated documents validate the same
  records that the YAML originals validated (round-trip comparison, see the
  `0.2.4` entry in `CHANGELOG.md`).
- `python scripts/verify.py` (pytest + ruff + mypy — the same gates CI runs)
  passes on this change.
