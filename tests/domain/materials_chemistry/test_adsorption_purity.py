"""DEV-M11-G03: purity and determinism proofs of the adsorption pack.

The adsorption templates module is pure by construction: no randomness,
no wall clock, no network, no I/O; same inputs -> identical outputs on
every call and platform; safe ids only (FND-M9-G02-01); AC-01/AC-03: the
module and its rule tables never hardcode gas, chemistry or condition
instances -- specific chemistry and reported values appear only as
instance data in test fixtures.

Every test name contains "adsorption" (DEV-M11-G03 naming rule).
"""

from __future__ import annotations

import ast
import inspect
import json

from scientific_reproduction.core.ids import ID_PATTERN, is_valid_id
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    BetTemplate,
    BreakthroughTemplate,
    assumptions_for_missing_parameters,
    capture_protocol,
)
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    templates as adsorption_templates_module,
)


def _module_source() -> str:
    """The full source of the adsorption templates module."""
    return inspect.getsource(adsorption_templates_module)


def test_adsorption_module_imports_are_deterministic_only() -> None:
    """The module imports only the deterministic core/analysis APIs.

    The import roots are read off the AST (deterministic), so the proof
    covers the real source, not a scanned string.
    """
    tree = ast.parse(_module_source())
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
        "collections",
        "re",
        "itertools",
    }
    assert import_roots <= allowed, sorted(import_roots - allowed)


def test_adsorption_module_has_no_randomness_clock_network_or_io() -> None:
    """No nondeterminism or I/O surface anywhere in the module source."""
    source = _module_source()
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


def test_adsorption_module_contains_no_instance_tokens() -> None:
    """AC-01/AC-03: no gas or condition instances in the module.

    Gas names, reported values and reference-case chemistry exist only as
    instance data in test fixtures, never in the templates module.
    """
    source = _module_source()
    for forbidden in (
        "propene",
        "ethene",
        "C3H6",
        "C2H4",
        "1965",
        "8.6",
        "23.5",
        "64.8",
        "41.3",
        "27.1",
        "19.1",
        "180.5",
        "68.2",
        "77.4",
        "393",
        "298.0 K",
    ):
        assert forbidden not in source, forbidden


def test_adsorption_rule_tables_contain_no_instance_tokens() -> None:
    """The rule tables never hardcode instance data (AC-01, AC-03).

    Scans the exact source segments of the universal rule tables for the
    instance-data tokens of the fixtures: no gas names, no conditions, no
    reported values. The segments are recovered from the AST, so the scan
    covers the real table source deterministically.
    """
    source = _module_source()
    tree = ast.parse(source)
    segments: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id in (
            "ADSORPTION_PARAMETER_RULES",
            "ADSORPTION_VALUE_RULES",
        ):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            segments.append(segment)
    assert segments, "no rule-table source segments found"
    rule_sources = "\n".join(segments)
    for forbidden in ("propene", "ethene", "C3H6", "C2H4", "1965", "8.6"):
        assert forbidden not in rule_sources, forbidden


def test_adsorption_pack_init_reexports_module_names_only() -> None:
    """The pack __init__ re-exports the module surface without new behavior."""
    from scientific_reproduction.domain_packs.materials_chemistry import (
        adsorption,
    )

    init_source = inspect.getsource(adsorption)
    # The __init__ defines no functions or classes of its own.
    tree = ast.parse(init_source)
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    # Everything it exports is also exported by the templates module.
    for name in adsorption.__all__:
        if name != "templates":
            assert name in adsorption_templates_module.__all__, name


def test_adsorption_capture_is_byte_identical_across_calls() -> None:
    """capture_protocol is a pure function: byte-identical snapshots."""
    template = BetTemplate(
        template_id="bet-1-isotherm-77",
        title="BET isotherm acquisition",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
    )
    snapshots = {
        json.dumps(capture_protocol(template), sort_keys=True) for _ in range(3)
    }
    assert len(snapshots) == 1


def test_adsorption_assessment_and_routing_are_deterministic() -> None:
    """Every evaluation surface is a pure function of its inputs."""
    template = BreakthroughTemplate(
        template_id="breakthrough-1-missing-flow",
        title="Breakthrough missing flow rate",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "adsorbent_mass_mg": 1200.0,
            "column_length_mm": 220.0,
            "column_diameter_mm": 4.0,
            "dead_volume_ml": 1.2,
            "detector": "gas_chromatograph",
            "regeneration_protocol": "vacuum at elevated temperature",
            "cycle_count": 5,
        },
    )
    first_routing = assumptions_for_missing_parameters(template)
    second_routing = assumptions_for_missing_parameters(template)
    assert first_routing == second_routing


def test_adsorption_all_generated_ids_are_safe() -> None:
    """Assumption ids are safe single registry path segments."""
    template = BreakthroughTemplate(
        template_id="breakthrough-2-missing-flow",
        title="Breakthrough missing flow rate",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "adsorbent_mass_mg": 1200.0,
            "column_length_mm": 220.0,
            "column_diameter_mm": 4.0,
            "dead_volume_ml": 1.2,
            "detector": "gas_chromatograph",
            "regeneration_protocol": "vacuum at elevated temperature",
            "cycle_count": 5,
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ("flow_rate_ml_min",)
    for ref in routing.assumption_refs:
        assert ID_PATTERN.match(ref), ref
        assert is_valid_id(ref, "assumption"), ref


def test_adsorption_analysis_fixtures_freeze_pinned_values() -> None:
    """Instance-data values are pinned literals, not random or computed."""
    analysis = BetTemplate(
        template_id="bet-2-analysis",
        title="BET analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "surface_area",
            "model": "bet",
            "relative_pressure_min": 0.05,
            "relative_pressure_max": 0.3,
        },
    )
    assert analysis.parameters["relative_pressure_min"] == 0.05
    assert analysis.parameters["relative_pressure_max"] == 0.3
    assert analysis.parameters["model"] == "bet"
