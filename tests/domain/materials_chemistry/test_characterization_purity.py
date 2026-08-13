"""DEV-M11-G02: purity and determinism proofs of the characterization pack.

The characterization templates module is pure by construction: no
randomness, no wall clock, no network, no I/O; same inputs -> identical
outputs on every call and platform; safe ids only (FND-M9-G02-01); the
module and its rule tables never hardcode FDM-201 (or any) material
chemistry or instrument (SS5: templates, never universal thresholds).

Every test name contains "character" (DEV-M11-G02 naming rule).
"""

from __future__ import annotations

import ast
import inspect
import json

from scientific_reproduction.core.ids import ID_PATTERN, is_valid_id
from scientific_reproduction.core.permissions import Role
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    AnalysisPlan,
    PXRDCharacterizationTemplate,
    assumptions_for_missing_metadata,
    capture_characterization,
    evaluate_acceptance,
    evaluate_identity_checks,
    freeze_analysis_plan,
)
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    templates as characterization_templates_module,
)


def _module_source() -> str:
    """The full source of the characterization templates module."""
    return inspect.getsource(characterization_templates_module)


def test_character_module_imports_are_deterministic_only() -> None:
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


def test_character_module_has_no_randomness_clock_network_or_io() -> None:
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


def test_character_module_contains_no_material_chemistry_or_instrument_tokens() -> None:
    """AC-03: no FDM-201 (or any) material chemistry or instrument.

    Material names, instrument names and the reference conditions exist
    only as instance data in test fixtures, never in the templates module:
    the rules stay universal (16-MATERIALS-CHEMISTRY-DOMAIN-PACK.md SS5).
    """
    source = _module_source()
    for forbidden in (
        "PyBC",
        "zinc",
        "DMF",
        "methanol",
        "D5TA00771B",
        "solvothermal",
        "Bruker",
        "SmartLab",
        "393",
        "298.0",
    ):
        assert forbidden not in source, forbidden


def test_character_pack_init_reexports_module_names_only() -> None:
    """The pack __init__ re-exports the module surface without new behavior."""
    from scientific_reproduction.domain_packs.materials_chemistry import (
        characterization,
    )

    init_source = inspect.getsource(characterization)
    # The __init__ defines no functions or classes of its own.
    tree = ast.parse(init_source)
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []
    # Everything it exports is also exported by the templates module.
    for name in characterization.__all__:
        if name != "templates":
            assert name in characterization_templates_module.__all__, name


def test_character_capture_is_byte_identical_across_calls() -> None:
    """capture_characterization is a pure function: byte-identical snapshots."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-1",
        title="FDM-201 reference PXRD",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 50.0,
            "step_size_deg": 0.02,
            "scan_temperature_K": 298.0,
        },
    )
    snapshots = {
        json.dumps(capture_characterization(template), sort_keys=True) for _ in range(3)
    }
    assert len(snapshots) == 1


def test_character_assessments_are_deterministic() -> None:
    """Every evaluation surface is a pure function of its inputs."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-2",
        title="PXRD purity check",
        analysis=AnalysisPlan(
            protocol="peak-position agreement",
            protocol_steps=("compare peaks",),
            acceptance_parameters={"pxrd_peak_tolerance_deg": 0.2},
        ),
    )
    facts = {"max_peak_position_deviation_deg": 0.08}
    assert evaluate_acceptance(template, facts) == evaluate_acceptance(template, facts)
    assert evaluate_identity_checks(template, facts) == evaluate_identity_checks(
        template, facts
    )
    assert assumptions_for_missing_metadata(template) == assumptions_for_missing_metadata(
        template
    )
    frozen = freeze_analysis_plan(template, role=Role.SUPERVISOR)
    assert frozen == freeze_analysis_plan(template, role=Role.SUPERVISOR)
    assert repr(frozen) == repr(freeze_analysis_plan(template, role=Role.SUPERVISOR))


def test_character_all_generated_assumption_ids_are_safe() -> None:
    """Assumption ids are safe single registry path segments."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-3",
        title="Incomplete PXRD capture",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
        },
    )
    routing = assumptions_for_missing_metadata(template)
    assert routing.assumption_refs
    for ref in routing.assumption_refs:
        assert ID_PATTERN.match(ref), ref
        assert is_valid_id(ref, "assumption")


def test_character_fixtures_freeze_pinned_values() -> None:
    """Instance-data thresholds and facts are pinned literals, not random."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-4",
        title="Pinned contract",
        analysis=AnalysisPlan(
            protocol="peak-position agreement",
            protocol_steps=("compare peaks",),
            acceptance_parameters={"pxrd_peak_tolerance_deg": 0.2},
        ),
    )
    facts = {"max_peak_position_deviation_deg": 0.08}
    first = evaluate_acceptance(template, facts)
    second = evaluate_acceptance(template, facts)
    assert first == second
    assert first.pending_measurements == ()
    # The recorded fact is inside the recorded tolerance; the other three
    # PXRD thresholds are simply not recorded on the plan: a pinned
    # PENDING, decided by the same contract on every call.
    assert first.matched_rule_id == "R-CHA-O2"
    assert first.matched_item_id == "R-CHA-A1"
