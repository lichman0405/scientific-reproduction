"""DEV-M11-G01: purity and determinism proofs of the synthesis pack.

The synthesis templates module is pure by construction: no randomness, no
wall clock, no network, no I/O; same inputs -> identical outputs on every
call and platform; safe ids only (FND-M9-G02-01); AC-03: the module and
its rule tables never hardcode FDM-201 (or any) material chemistry.

Every test name contains "synth" (DEV-M11-G01 naming rule).
"""

from __future__ import annotations

import ast
import inspect
import json

from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ActivationTemplate,
    SolventExchangeTemplate,
    SynthesisUnitProcessKind,
    SynthesisUnitProcessTemplate,
    assumptions_for_missing_parameters,
    capture_protocol,
    plan_independent_batches,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    templates as synthesis_templates_module,
)


def _module_source() -> str:
    """The full source of the synthesis templates module."""
    return inspect.getsource(synthesis_templates_module)


def test_synth_module_imports_are_deterministic_only() -> None:
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


def test_synth_module_has_no_randomness_clock_network_or_io() -> None:
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


def test_synth_module_contains_no_material_chemistry_tokens() -> None:
    """AC-03: no FDM-201 (or any) material chemistry in the module.

    Reagent names and the reference conditions exist only as instance data
    in test fixtures, never in the templates module.
    """
    source = _module_source()
    for forbidden in (
        "PyBC",
        "zinc",
        "DMF",
        "methanol",
        "D5TA00771B",
        "393",
        "298.0 K",
    ):
        assert forbidden not in source, forbidden


def test_synth_pack_init_reexports_module_names_only() -> None:
    """The pack __init__ re-exports the module surface without new behavior."""
    from scientific_reproduction.domain_packs.materials_chemistry import synthesis

    init_source = inspect.getsource(synthesis)
    # The __init__ defines no functions or classes of its own.
    tree = ast.parse(init_source)
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    # Everything it exports is also exported by the templates module.
    for name in synthesis.__all__:
        if name != "templates":
            assert name in synthesis_templates_module.__all__, name


def test_synth_capture_is_byte_identical_across_calls() -> None:
    """capture_protocol is a pure function: byte-identical snapshots."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-1-thermal-90-adsorption",
        title="FDM-201 MOF solvothermal synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
        parameters={
            "metal_source": "zinc acetate dihydrate",
            "organic_linker": "PyBC",
            "solvent": "DMF",
            "temperature_K": 393.0,
            "duration_h": 72.0,
            "stoichiometry": 1.0,
        },
    )
    snapshots = {json.dumps(capture_protocol(template), sort_keys=True) for _ in range(3)}
    assert len(snapshots) == 1


def test_synth_assessment_and_plan_are_deterministic() -> None:
    """Every evaluation surface is a pure function of its inputs."""
    activation = ActivationTemplate(
        template_id="activation-1",
        title="Thermal activation",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    first_routing = assumptions_for_missing_parameters(activation)
    second_routing = assumptions_for_missing_parameters(activation)
    assert first_routing == second_routing
    first_plan = plan_independent_batches(activation, n=3)
    second_plan = plan_independent_batches(activation, n=3)
    assert first_plan == second_plan


def test_synth_all_generated_ids_are_safe() -> None:
    """Run and assumption ids are safe single registry path segments."""
    from scientific_reproduction.core.ids import ID_PATTERN, is_valid_id

    activation = ActivationTemplate(
        template_id="activation-1",
        title="Thermal activation",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
        },
    )
    plan = plan_independent_batches(activation, n=3)
    for run_id in plan.batch_run_ids:
        assert ID_PATTERN.match(run_id), run_id
        assert is_valid_id(run_id, "run")
    routing = assumptions_for_missing_parameters(activation)
    for ref in routing.assumption_refs:
        assert ID_PATTERN.match(ref), ref
        assert is_valid_id(ref, "assumption")


def test_synth_fixtures_freeze_pinned_values() -> None:
    """Instance-data values are pinned literals, not random or computed."""
    exchange = SolventExchangeTemplate(
        template_id="exchange-1",
        title="Solvent exchange",
        parameters={
            "solvent": "methanol",
            "exchange_cycles": 3,
            "temperature_K": 298.0,
            "soaking_duration_h": 24.0,
        },
    )
    assert exchange.parameters["exchange_cycles"] == 3
    assert exchange.parameters["temperature_K"] == 298.0
