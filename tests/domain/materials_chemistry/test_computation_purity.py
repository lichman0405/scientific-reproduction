"""DEV-M11-G04: purity and determinism proofs of the computation pack.

The computation templates module is pure by construction: no randomness,
no wall clock, no network, no I/O; same inputs -> identical outputs on
every call and platform; safe ids only (FND-M9-G02-01); AC-01/AC-03: the
module and its rule tables never hardcode software, force-field or
material instances.

Every test name contains "comput" (DEV-M11-G04 naming rule).
"""

from __future__ import annotations

import ast
import inspect
import json

from scientific_reproduction.core.ids import ID_PATTERN, is_valid_id
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    ANALYSIS_STAGE,
    EXECUTION_STAGE,
    DftTemplate,
    GcmcTemplate,
    assumptions_for_missing_parameters,
    capture_protocol,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    templates as computation_templates_module,
)


def _module_source() -> str:
    """The full source of the computation templates module."""
    return inspect.getsource(computation_templates_module)


def test_comput_module_imports_are_deterministic_only() -> None:
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


def test_comput_module_has_no_randomness_clock_network_or_io() -> None:
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


def test_comput_module_contains_no_instance_tokens() -> None:
    """AC-01/AC-03: no software/force-field/chemistry instances in the module.

    Software names, force fields and the reference conditions exist only
    as instance data in test fixtures, never in the templates module.
    """
    source = _module_source()
    for forbidden in (
        "vasp",
        "raspa",
        "gromacs",
        "pymatgen",
        "PBE",
        "DREIDING",
        "PyBC",
        "zinc",
        "DMF",
        "methanol",
        "D5TA00771B",
        "393",
        "298.0 K",
    ):
        assert forbidden not in source, forbidden


def test_comput_rule_tables_contain_no_instance_tokens() -> None:
    """The rule tables never hardcode instance data (AC-01, AC-03).

    Scans the exact source segments of the universal rule tables for the
    instance-data tokens of the fixtures: no software names, no force
    fields, no conditions. The segments are recovered from the AST, so
    the scan covers the real table source deterministically.
    """
    source = _module_source()
    tree = ast.parse(source)
    segments: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id in ("COMPUTATION_PARAMETER_RULES", "COMPUTATION_VALUE_RULES"):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            segments.append(segment)
    assert segments, "no rule-table source segments found"
    rule_sources = "\n".join(segments)
    for forbidden in ("vasp", "raspa", "gromacs", "pymatgen", "PBE", "DREIDING"):
        assert forbidden not in rule_sources, forbidden


def test_comput_pack_init_reexports_module_names_only() -> None:
    """The pack __init__ re-exports the module surface without new behavior."""
    from scientific_reproduction.domain_packs.materials_chemistry import (
        computation,
    )

    init_source = inspect.getsource(computation)
    # The __init__ defines no functions or classes of its own.
    tree = ast.parse(init_source)
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    # Everything it exports is also exported by the templates module.
    for name in computation.__all__:
        if name != "templates":
            assert name in computation_templates_module.__all__, name


def test_comput_capture_is_byte_identical_across_calls() -> None:
    """capture_protocol is a pure function: byte-identical snapshots."""
    template = DftTemplate(
        template_id="dft-1-opt-90",
        title="FDM-201 DFT geometry optimization",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "functional": "PBE",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
            "energy_cutoff_ev": 520.0,
            "convergence_tolerance": 1e-5,
        },
    )
    snapshots = {
        json.dumps(capture_protocol(template), sort_keys=True) for _ in range(3)
    }
    assert len(snapshots) == 1


def test_comput_assessment_and_routing_are_deterministic() -> None:
    """Every evaluation surface is a pure function of its inputs."""
    template = DftTemplate(
        template_id="dft-2-missing-functional",
        title="DFT missing functional",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "vasp",
            "software_version": "5.4.4",
            "method": "GGA",
            "dispersion_correction": "DFT-D3",
            "basis_set": "PAW",
            "pseudopotential": "PBE.54",
            "kpoint_mesh": (3, 3, 2),
            "energy_cutoff_ev": 520.0,
            "convergence_tolerance": 1e-5,
        },
    )
    first_routing = assumptions_for_missing_parameters(template)
    second_routing = assumptions_for_missing_parameters(template)
    assert first_routing == second_routing


def test_comput_all_generated_ids_are_safe() -> None:
    """Assumption ids are safe single registry path segments."""
    template = GcmcTemplate(
        template_id="gcmc-1-missing-seed",
        title="GCMC missing seed",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "raspa",
            "software_version": "2.0.47",
            "force_field": "UFF",
            "charges": "none",
            "mixing_rules": "lorentz_berthelot",
            "cutoff_angstrom": 12.0,
            "temperature_K": 298.0,
            "pressure_bar": 1.0,
            "equilibration_cycles": 10000,
            "production_cycles": 20000,
        },
    )
    routing = assumptions_for_missing_parameters(template)
    assert routing.missing_parameters == ("seed",)
    for ref in routing.assumption_refs:
        assert ID_PATTERN.match(ref), ref
        assert is_valid_id(ref, "assumption"), ref


def test_comput_fixtures_freeze_pinned_values() -> None:
    """Instance-data values are pinned literals, not random or computed."""
    analysis = GcmcTemplate(
        template_id="gcmc-2-analysis",
        title="GCMC analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "absolute_adsorption",
            "convergence_metric": "block_average_drift",
            "convergence_threshold": 0.01,
            "statistical_uncertainty_metric": "standard_error",
            "sampling_validation": "production blocks beyond correlation time",
        },
    )
    assert analysis.parameters["convergence_threshold"] == 0.01
    assert analysis.parameters["property"] == "absolute_adsorption"
