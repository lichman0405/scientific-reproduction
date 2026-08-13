"""DEV-M11-G02: characterization template construction and rule tables.

Covers AC-01 (templates define required raw data / instrument metadata --
the ordered requirement rule table per characterization kind), AC-03
(universal rule tables -- parametrized over many materials, FDM-201
chemistry only as instance data), rule-table integrity, safe template ids
and the deterministic protocol capture.

Every test name contains "character" (DEV-M11-G02 naming rule).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scientific_reproduction.core.models import GoalTrack
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    ACCEPTANCE_PARAMETER_RULES,
    ANALYSIS_ACCEPTANCE_RULES,
    CAPTURE_KEYS,
    CHARACTERIZATION_REQUIREMENT_RULES,
    CHARACTERIZATION_RULESET_VERSION,
    CHARACTERIZATION_VALUE_RULES,
    CHECK_OUTCOME_RULES,
    PXRD_IDENTITY_CHECKS,
    CharacterizationKind,
    CharacterizationTemplateBase,
    InvalidCharacterizationTemplateError,
    MetadataValueAssessment,
    PXRDCharacterizationTemplate,
    SCXRDCharacterizationTemplate,
    SpectroscopyCharacterizationTemplate,
    TGACharacterizationTemplate,
    capture_characterization,
    missing_metadata,
    validate_characterization_rulesets,
    validate_metadata_values,
)

# ---------------------------------------------------------------------------
# Fixtures: instance data. FDM-201 reference chemistry (17-FDM201-REFERENCE-CASE.md
# WP-30) appears ONLY here, as instance data -- never in the rule tables (AC-03).
# ---------------------------------------------------------------------------


@pytest.fixture
def fdm201_pxrd_template() -> PXRDCharacterizationTemplate:
    """The FDM-201 activated-material PXRD, as a template instance."""
    return PXRDCharacterizationTemplate(
        template_id="pxrd-fdm201-activated-298k",
        title="FDM-201 activated PXRD",
        track=GoalTrack.STRICT_REPRODUCTION,
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


@pytest.fixture
def fictional_oxide_pxrd_template() -> PXRDCharacterizationTemplate:
    """A fictional oxide material -- proof the rules are not FDM-201-specific."""
    return PXRDCharacterizationTemplate(
        template_id="pxrd-oxor-44-sintered",
        title="Beta-oxor-44 sintered PXRD",
        track=GoalTrack.RECOVERY,
        parameters={
            "instrument": "Rigaku SmartLab",
            "radiation_type": "Co K-alpha",
            "wavelength_A": 1.7903,
            "two_theta_min_deg": 3.0,
            "two_theta_max_deg": 80.0,
            "step_size_deg": 0.01,
            "scan_temperature_K": 293.0,
        },
    )


# ---------------------------------------------------------------------------
# Rule-table integrity
# ---------------------------------------------------------------------------


def test_character_ruleset_table_integrity_and_version() -> None:
    """The ordered rule tables are valid and the version is recorded."""
    rule_ids = validate_characterization_rulesets()
    assert CHARACTERIZATION_RULESET_VERSION == "1.0"
    expected_count = (
        len(CHARACTERIZATION_REQUIREMENT_RULES)
        + len(CHARACTERIZATION_VALUE_RULES)
        + len(ACCEPTANCE_PARAMETER_RULES)
        + len(ANALYSIS_ACCEPTANCE_RULES)
        + len(CHECK_OUTCOME_RULES)
    )
    assert len(rule_ids) == expected_count
    # The requirement table's trailing rule is the total default.
    assert CHARACTERIZATION_REQUIREMENT_RULES[-1].rule_id == "R-CHA-P0"
    # Every metadata value rule applies to exactly one named parameter.
    parameters = [rule.parameter for rule in CHARACTERIZATION_VALUE_RULES]
    assert len(parameters) == len(set(parameters))
    assert CHARACTERIZATION_VALUE_RULES[0].rule_id == "R-CHA-V1"
    # The PXRD identity checks are the PXRD slice of the acceptance table.
    pxrd_ids = [rule.rule_id for rule in PXRD_IDENTITY_CHECKS]
    assert pxrd_ids == ["R-CHA-A1", "R-CHA-A2", "R-CHA-A3", "R-CHA-A4"]
    assert all(
        rule in ANALYSIS_ACCEPTANCE_RULES for rule in PXRD_IDENTITY_CHECKS
    )


def test_character_requirement_rule_table_covers_every_kind() -> None:
    """Every kind has a deciding rule: first match wins, total default last."""
    for kind in CharacterizationKind:
        matched = [
            rule
            for rule in CHARACTERIZATION_REQUIREMENT_RULES
            if rule.predicate(kind)
        ]
        assert matched, kind
        # First match wins; the trailing rule is the total default.
        assert matched[0] is CHARACTERIZATION_REQUIREMENT_RULES[-1] or (
            matched[0].rule_id != "R-CHA-P0"
        )
    # The four real kinds are decided by their own declared rule.
    for kind, expected in (
        (CharacterizationKind.PXRD, "R-CHA-P1"),
        (CharacterizationKind.SCXRD, "R-CHA-P2"),
        (CharacterizationKind.TGA, "R-CHA-P3"),
        (CharacterizationKind.SPECTROSCOPY, "R-CHA-P4"),
    ):
        deciding = next(
            rule
            for rule in CHARACTERIZATION_REQUIREMENT_RULES
            if rule.predicate(kind)
        )
        assert deciding.rule_id == expected


def test_character_ruleset_parameter_names_match_value_rules() -> None:
    """The requirement table names exactly the value-ruled parameters."""
    value_ruled = {rule.parameter for rule in CHARACTERIZATION_VALUE_RULES}
    for rule in CHARACTERIZATION_REQUIREMENT_RULES:
        for parameter in rule.required_parameters:
            assert parameter in value_ruled, parameter


def test_character_acceptance_rules_cover_every_kind() -> None:
    """Every characterization kind has at least one acceptance contract."""
    covered: set[CharacterizationKind] = set()
    for rule in ANALYSIS_ACCEPTANCE_RULES:
        covered.update(rule.kinds)
    assert covered == set(CharacterizationKind)


# ---------------------------------------------------------------------------
# Construction, AC-01 required raw data / instrument metadata
# ---------------------------------------------------------------------------


def test_character_pxrd_template_construction_with_fdm201_instance_data(
    fdm201_pxrd_template: PXRDCharacterizationTemplate,
) -> None:
    """FDM-201 PXRD chemistry constructs fine as instance data (AC-01/03)."""
    template = fdm201_pxrd_template
    assert template.template_id == "pxrd-fdm201-activated-298k"
    assert template.characterization_kind is CharacterizationKind.PXRD
    assert template.track is GoalTrack.STRICT_REPRODUCTION
    assert template.frozen is False
    assert missing_metadata(template) == ()


def test_character_pxrd_template_requires_raw_data_and_instrument_metadata() -> None:
    """AC-01: PXRD declares its required raw data + instrument metadata."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-partial",
        title="Partial PXRD capture",
        parameters={
            "instrument": "Bruker D8 Advance",
            "radiation_type": "Cu K-alpha",
        },
    )
    assert missing_metadata(template) == (
        "wavelength_A",
        "two_theta_min_deg",
        "two_theta_max_deg",
        "step_size_deg",
        "scan_temperature_K",
    )


def test_character_scxrd_template_requires_structure_metadata() -> None:
    """AC-01: SCXRD declares its required raw data + instrument metadata."""
    template = SCXRDCharacterizationTemplate(
        template_id="scxrd-partial",
        title="Partial SCXRD capture",
    )
    assert missing_metadata(template) == (
        "instrument",
        "radiation_type",
        "wavelength_A",
        "collection_temperature_K",
        "resolution_limit_A",
        "detector",
    )


def test_character_tga_template_requires_thermal_metadata() -> None:
    """AC-01: TGA declares its required raw data + instrument metadata."""
    template = TGACharacterizationTemplate(
        template_id="tga-partial",
        title="Partial TGA capture",
    )
    assert missing_metadata(template) == (
        "instrument",
        "atmosphere",
        "heating_rate_K_min",
        "final_temperature_K",
        "sample_mass_mg",
        "gas_flow_ml_min",
        "scan_duration_h",
    )


def test_character_spectroscopy_template_requires_identity_metadata() -> None:
    """AC-01: spectroscopy declares its required raw data + instrument metadata."""
    template = SpectroscopyCharacterizationTemplate(
        template_id="spectra-partial",
        title="Partial spectroscopy capture",
    )
    assert missing_metadata(template) == (
        "instrument",
        "technique",
        "wavenumber_min_cm_1",
        "wavenumber_max_cm_1",
        "resolution_cm_1",
        "number_of_scans",
    )


def test_character_templates_record_recovery_track_label() -> None:
    """Templates record the recovery label (AC-01) without freezing anything."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-recovery-1",
        title="Recovery PXRD",
        track=GoalTrack.RECOVERY,
    )
    assert template.track is GoalTrack.RECOVERY
    assert template.frozen is False
    capture = capture_characterization(template)
    assert capture["track"] == "RECOVERY"


def test_character_template_kinds_are_fixed_per_class() -> None:
    """Each template class accepts only its own characterization kind."""
    for cls, kind in (
        (PXRDCharacterizationTemplate, CharacterizationKind.PXRD),
        (SCXRDCharacterizationTemplate, CharacterizationKind.SCXRD),
        (TGACharacterizationTemplate, CharacterizationKind.TGA),
        (SpectroscopyCharacterizationTemplate, CharacterizationKind.SPECTROSCOPY),
    ):
        assert cls(template_id="t1", title="x").characterization_kind is kind
        for other in CharacterizationKind:
            if other is kind:
                continue
            with pytest.raises(InvalidCharacterizationTemplateError):
                cls(
                    template_id="bad-kind",
                    title="Wrong kind",
                    characterization_kind=other,
                )


@pytest.mark.parametrize(
    ("parameter_name", "bad_value"),
    [
        ("wavelength_A", -1.0),              # R-CHA-V1
        ("wavelength_A", float("inf")),      # R-CHA-V1
        ("radiation_type", "   "),           # R-CHA-V2
        ("two_theta_min_deg", -0.5),         # R-CHA-V3
        ("two_theta_max_deg", 0),            # R-CHA-V4
        ("step_size_deg", 0),                # R-CHA-V5
        ("scan_temperature_K", 0),           # R-CHA-V6
        ("collection_temperature_K", -3.0),  # R-CHA-V7
        ("resolution_limit_A", 0.0),         # R-CHA-V8
        ("sample_mass_mg", -1.0),            # R-CHA-V9
        ("heating_rate_K_min", 0),           # R-CHA-V10
        ("final_temperature_K", -5.0),       # R-CHA-V11
        ("scan_duration_h", 0),              # R-CHA-V12
        ("gas_flow_ml_min", -1.0),           # R-CHA-V13
        ("atmosphere", "oxygen"),            # R-CHA-V14 (not controlled)
        ("detector", ""),                    # R-CHA-V15
        ("wavenumber_min_cm_1", -1.0),       # R-CHA-V16
        ("wavenumber_max_cm_1", 0),          # R-CHA-V17
        ("resolution_cm_1", -2.0),           # R-CHA-V18
        ("number_of_scans", 0),              # R-CHA-V19
        ("number_of_scans", 2.5),            # R-CHA-V19
        ("technique", ""),                   # R-CHA-V20
        ("instrument", "   "),               # R-CHA-V21
    ],
)
def test_character_value_rules_reject_invalid_metadata(
    parameter_name: str, bad_value: object
) -> None:
    """The universal metadata value rules reject every violation."""
    parameters: dict[str, Any] = {
        "instrument": "Bruker D8 Advance",
        "radiation_type": "Cu K-alpha",
        "wavelength_A": 1.5406,
        "two_theta_min_deg": 5.0,
        "two_theta_max_deg": 50.0,
        "step_size_deg": 0.02,
        "scan_temperature_K": 298.0,
        "collection_temperature_K": 298.0,
        "resolution_limit_A": 0.84,
        "sample_mass_mg": 8.0,
        "heating_rate_K_min": 5.0,
        "final_temperature_K": 873.0,
        "scan_duration_h": 3.0,
        "gas_flow_ml_min": 20.0,
        "atmosphere": "nitrogen",
        "detector": "PILATUS",
        "wavenumber_min_cm_1": 400.0,
        "wavenumber_max_cm_1": 4000.0,
        "resolution_cm_1": 4.0,
        "number_of_scans": 64,
        "technique": "FTIR ATR",
    }
    parameters[parameter_name] = bad_value
    with pytest.raises(InvalidCharacterizationTemplateError, match="R-CHA-V"):
        PXRDCharacterizationTemplate(
            template_id="pxrd-bad-value",
            title="Invalid value template",
            parameters=parameters,
        )


def test_character_metadata_value_assessment_is_auditable(
    fdm201_pxrd_template: PXRDCharacterizationTemplate,
) -> None:
    """validate_metadata_values records every rule decision and no violations."""
    assessment = validate_metadata_values(fdm201_pxrd_template)
    assert isinstance(assessment, MetadataValueAssessment)
    assert assessment.violations == ()
    assert assessment.matched_rule_id is None
    assert len(assessment.decisions) == len(CHARACTERIZATION_VALUE_RULES)
    applied = [decision for decision in assessment.decisions if decision.applied]
    assert len(applied) == len(fdm201_pxrd_template.parameters)


def test_character_type_boundaries_raise_type_error() -> None:
    """Non-string ids and non-GoalTrack labels are TypeError at the boundary."""
    bad_id: Any = 123
    bad_track: Any = "STRICT"
    bad_kind: Any = "pxrd"
    bad_parameters: Any = ["instrument"]
    with pytest.raises(TypeError):
        PXRDCharacterizationTemplate(template_id=bad_id, title="x")
    with pytest.raises(TypeError):
        PXRDCharacterizationTemplate(template_id="t1", title="x", track=bad_track)
    with pytest.raises(TypeError):
        PXRDCharacterizationTemplate(
            template_id="t1", title="x", characterization_kind=bad_kind
        )
    with pytest.raises(TypeError):
        PXRDCharacterizationTemplate(
            template_id="t1", title="x", parameters=bad_parameters
        )


# ---------------------------------------------------------------------------
# Safe ids (FND-M9-G02-01 lesson)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_id",
    ["", ".", "..", "a/b", "a\\b", "a*b", "a?b", "a[b]", "has space"],
)
def test_character_template_rejects_unsafe_ids(unsafe_id: str) -> None:
    """Template ids must be safe single registry path segments."""
    with pytest.raises(InvalidCharacterizationTemplateError):
        PXRDCharacterizationTemplate(template_id=unsafe_id, title="unsafe id")


def test_character_template_accepts_safe_ids() -> None:
    """Safe registry-style ids construct without error."""
    template = PXRDCharacterizationTemplate(
        template_id="pxrd-fdm201-activated-298k",
        title="safe id",
    )
    assert template.template_id == "pxrd-fdm201-activated-298k"


# ---------------------------------------------------------------------------
# AC-03: universality -- the rules are templates, never FDM-201 thresholds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "material",
    [
        "FDM-201 zinc MOF",               # the reference case (instance data)
        "beta-oxor-44 oxide",             # fictional oxide
        "alpha-silicate glass",           # fictional glass
        "boron nitride ceramic",          # fictional ceramic
        "poly(ethylene oxide) composite",  # fictional polymer
        "lithium orthosilicate",          # fictional battery material
    ],
)
def test_character_universal_rules_apply_to_any_material(material: str) -> None:
    """The same rule tables serve every material; no universal thresholds."""
    template = PXRDCharacterizationTemplate(
        template_id=f"pxrd-material-{len(material)}",
        title=f"{material} PXRD",
        parameters={
            "instrument": "general diffractometer",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 5.0,
            "two_theta_max_deg": 60.0,
            "step_size_deg": 0.02,
            "scan_temperature_K": 298.0,
        },
    )
    assert missing_metadata(template) == ()
    assert validate_metadata_values(template).violations == ()


def test_character_rule_tables_contain_no_instance_chemistry_tokens() -> None:
    """The rule tables never hardcode FDM-201 (or any) chemistry (AC-03).

    Scans the exact source segments of the universal rule tables for the
    instance-data tokens of the reference case: no reagent names, no
    instrument models, no condition values. The segments are recovered
    from the AST, so the scan covers the real table source
    deterministically.
    """
    import ast
    import inspect

    from scientific_reproduction.domain_packs.materials_chemistry import (
        characterization as characterization_pack,
    )
    from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
        templates as characterization_templates_module,
    )

    source = inspect.getsource(characterization_templates_module)
    tree = ast.parse(source)
    segments: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id in (
            "CHARACTERIZATION_VALUE_RULES",
            "CHARACTERIZATION_REQUIREMENT_RULES",
            "ACCEPTANCE_PARAMETER_RULES",
            "ANALYSIS_ACCEPTANCE_RULES",
            "PXRD_IDENTITY_CHECKS",
            "CHECK_OUTCOME_RULES",
        ):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            segments.append(segment)
    assert segments, "no rule-table source segments found"
    rule_sources = "\n".join(segments)
    for forbidden in (
        "PyBC",
        "zinc",
        "DMF",
        "Bruker",
        "SmartLab",
        "393",
        "298.0",
        "solvothermal",
        "D5TA00771B",
    ):
        assert forbidden not in rule_sources, forbidden
    # The pack-level __init__ defines no behavior of its own.
    init_source = inspect.getsource(characterization_pack)
    tree = ast.parse(init_source)
    defined = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert defined == []


def test_character_capture_protocol_is_deterministic_snapshot(
    fdm201_pxrd_template: PXRDCharacterizationTemplate,
) -> None:
    """The protocol capture is a stable, byte-identical deterministic snapshot."""
    capture = capture_characterization(fdm201_pxrd_template)
    assert set(capture) == set(CAPTURE_KEYS)
    assert capture["characterization_kind"] == "pxrd"
    assert capture["frozen"] is False
    assert capture["analysis"] is None
    assert capture["assumption_refs"] == []
    snapshot = json.dumps(capture, sort_keys=True)
    assert snapshot == json.dumps(
        capture_characterization(fdm201_pxrd_template), sort_keys=True
    )
    # The captured parameter table is sorted by parameter name.
    names = [row["parameter"] for row in capture["parameter_table"]]
    assert names == sorted(names)


def test_character_package_exports_are_stable() -> None:
    """The pack wiring exports the public template surface."""
    from scientific_reproduction.domain_packs.materials_chemistry import (
        characterization,
    )

    for name in (
        "PXRDCharacterizationTemplate",
        "SCXRDCharacterizationTemplate",
        "TGACharacterizationTemplate",
        "SpectroscopyCharacterizationTemplate",
        "AnalysisPlan",
        "evaluate_acceptance",
        "evaluate_identity_checks",
        "assumptions_for_missing_metadata",
        "freeze_analysis_plan",
        "capture_characterization",
    ):
        assert name in characterization.__all__, name
        assert hasattr(characterization, name), name


def test_character_base_class_is_abstract_by_kind_requirement() -> None:
    """The base requires an explicit kind; subclasses fix it."""
    bad_kind: Any = None
    with pytest.raises(TypeError):
        CharacterizationTemplateBase(
            template_id="t1", title="x", characterization_kind=bad_kind
        )
