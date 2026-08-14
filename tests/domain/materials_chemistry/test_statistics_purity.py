"""DEV-M11-G05: purity, determinism and AC-01 no-percentage proofs.

The statistics templates module is pure by construction: no randomness,
no wall clock, no network, no I/O; same inputs -> identical proposals,
assessments and freeze records on every call and platform; safe ids only
(FND-M9-G02-01); AC-01: the module (and the whole statistics package)
contains NO universal fixed percent margin -- no percent literal, no
percentage-candidate numeric token, no module-level numeric constant
default, and the default acceptance capture carries no numeric
tolerance.

Every test name contains "statistic" (DEV-M11-G05 naming rule).
"""

from __future__ import annotations

import ast
import inspect
import json
import re

import pytest

from scientific_reproduction.core.ids import ID_PATTERN, is_valid_id
from scientific_reproduction.core.permissions import Role
from scientific_reproduction.domain_packs.materials_chemistry.statistics import (
    EvidenceClaim,
    EvidenceReference,
    construct_acceptance_proposal,
    default_acceptance_proposal,
    default_replicate_design_proposal,
    freeze_acceptance_proposal,
)
from scientific_reproduction.domain_packs.materials_chemistry.statistics import (
    templates as statistics_templates_module,
)

#: Percentage literals a universal fixed percent margin would hardcode
#: (the AC-01 pin: none may appear anywhere in the package source).
PERCENTAGE_TOKENS: tuple[str, ...] = (
    "%",
    "0.1",
    "0.10",
    "10.0",
    "±10",
    "+10",
    "-10",
)


def _package_sources() -> tuple[str, str]:
    """The full sources of the statistics templates and init modules."""
    return (
        inspect.getsource(statistics_templates_module),
        inspect.getsource(
            __import__(
                "scientific_reproduction.domain_packs.materials_chemistry.statistics",
                fromlist=["statistics"],
            )
        ),
    )


def test_statistic_module_imports_are_deterministic_only() -> None:
    """The module imports only the deterministic core/analysis APIs.

    The import roots are read off the AST (deterministic), so the proof
    covers the real source, not a scanned string.
    """
    tree = ast.parse(inspect.getsource(statistics_templates_module))
    import_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            import_roots.add(node.module.split(".")[0])
    allowed = {
        "__future__",
        "scientific_reproduction",
        "math",
        "dataclasses",
        "enum",
        "typing",
    }
    assert import_roots <= allowed, sorted(import_roots - allowed)


def test_statistic_module_has_no_randomness_clock_network_or_io() -> None:
    """No nondeterminism or I/O surface anywhere in the module source."""
    source = inspect.getsource(statistics_templates_module)
    for forbidden in (
        "import random",
        "random.",
        "import time",
        "time.",
        "datetime",
        "import os",
        "os.",
        "pathlib",
        "import secrets",
        "secrets.",
        "uuid",
        "import requests",
        "urllib",
        "socket",
        "open(",
        "read_text",
        "write_text",
        "input(",
        "print(",
    ):
        assert forbidden not in source, forbidden


def test_statistic_package_contains_no_universal_percentage_tolerance() -> None:
    """AC-01 (structural): no hardcoded percent tolerance anywhere in the
    statistics package -- no percent literal and no percentage-candidate
    numeric token in either the templates module or the init module."""
    for source in _package_sources():
        assert "%" not in source, source
        for token in PERCENTAGE_TOKENS:
            assert token not in source, token
        # No percent literal in any form (e.g. "10 %" with a space).
        assert not re.search(r"\b\d+(?:\.\d+)?\s*[%‰]", source)


def test_statistic_rule_tables_contain_no_percentage_tokens() -> None:
    """AC-01 (structural): the freeze-eligibility rule table source segments
    carry no percentage-candidate tokens. The segments are recovered from
    the AST, so the scan covers the real table source deterministically."""
    source = inspect.getsource(statistics_templates_module)
    tree = ast.parse(source)
    segments: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id == "FREEZE_ELIGIBILITY_RULES":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            segments.append(segment)
    assert segments, "no freeze-eligibility rule-table source segment found"
    rule_sources = "\n".join(segments)
    for token in PERCENTAGE_TOKENS:
        assert token not in rule_sources, token


def test_statistic_module_defines_no_numeric_constant_defaults() -> None:
    """AC-01 (structural): no module-level numeric literal constant exists --
    there is no hardcoded universal numeric default anywhere (the proposed
    replicate floor is a named reference to the frozen
    ``DEFAULT_MIN_INDEPENDENT``, and the ruleset version is a string)."""
    tree = ast.parse(inspect.getsource(statistics_templates_module))
    numeric_constants: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target = node.targets[0].id if isinstance(node.targets[0], ast.Name) else "?"
                numeric_constants.append(f"{target} = {value!r}")
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.value, ast.Constant
        ):
            value = node.value.value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                target = node.target.id if isinstance(node.target, ast.Name) else "?"
                numeric_constants.append(f"{target} = {value!r}")
    assert numeric_constants == []


def test_statistic_pack_init_reexports_module_names_only() -> None:
    """The statistics pack __init__ re-exports the module surface without
    adding new behavior."""
    from scientific_reproduction.domain_packs.materials_chemistry import (
        statistics,
    )

    init_source = inspect.getsource(statistics)
    # The __init__ defines no functions or classes of its own.
    tree = ast.parse(init_source)
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    # Everything it exports is also exported by the templates module.
    for name in statistics.__all__:
        if name != "templates":
            assert name in statistics_templates_module.__all__, name


def test_statistic_proposals_and_freeze_are_deterministic() -> None:
    """Every proposal surface is a pure function: byte-identical captures."""
    margin_evidence = EvidenceReference(
        evidence_id="sr_evidence_margin_scan",
        source_id="method-source-scan",
        claim=EvidenceClaim.EQUIVALENCE_MARGIN,
    )
    acceptance = construct_acceptance_proposal(
        goal_id="GOAL-FDM201-ADS-001",
        replicate_design=default_replicate_design_proposal("GOAL-FDM201-ADS-001"),
        equivalence_margin=5.0,
        evidence_refs=(margin_evidence,),
    )
    frozen = freeze_acceptance_proposal(acceptance, role=Role.SUPERVISOR)
    captures = {
        json.dumps(capture, sort_keys=True)
        for capture in (acceptance.as_dict(), frozen.as_dict())
        for _ in range(2)
    }
    assert len(captures) == 2  # unfrozen vs frozen, each byte-identical


def test_statistic_default_acceptance_capture_has_no_numeric_tolerance() -> None:
    """AC-01 (behavioral): the default acceptance capture contains no
    percent token and no numeric tolerance of any kind."""
    capture = default_acceptance_proposal("GOAL-FDM201-ADS-001").as_dict()
    dumped = json.dumps(capture, sort_keys=True)
    assert "%" not in dumped
    assert capture["equivalence_margin"] is None
    assert capture["evidence_refs"] == []
    # The only numbers the default capture carries are the proposed
    # replicate floor and the ruleset bookkeeping -- no tolerance.
    assert capture["replicate_floor"] == 3
    assert capture["decision_mode"] == "equivalence"


def test_statistic_all_generated_ids_are_safe() -> None:
    """Proposal and evidence ids are safe single registry path segments."""
    replicate = default_replicate_design_proposal("GOAL-FDM201-ADS-001")
    acceptance = default_acceptance_proposal("GOAL-FDM201-ADS-001")
    assert ID_PATTERN.fullmatch(replicate.proposal_id)
    assert ID_PATTERN.fullmatch(acceptance.proposal_id)
    assert is_valid_id(replicate.proposal_id, "proposal")
    assert is_valid_id(acceptance.proposal_id, "proposal")


def test_statistic_records_are_frozen_dataclasses() -> None:
    """Every proposal record is a frozen dataclass: mutation is refused."""
    from dataclasses import FrozenInstanceError, is_dataclass

    assert is_dataclass(statistics_templates_module.ReplicateDesignProposal)
    assert is_dataclass(statistics_templates_module.MeasurementUncertaintyProposal)
    assert is_dataclass(statistics_templates_module.EvidenceReference)
    assert is_dataclass(statistics_templates_module.AcceptanceProposal)
    acceptance = default_acceptance_proposal("GOAL-FDM201-ADS-001")
    for field_name in acceptance.__dataclass_fields__:
        with pytest.raises(FrozenInstanceError):
            setattr(acceptance, field_name, None)
