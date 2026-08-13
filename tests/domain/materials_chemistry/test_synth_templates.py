"""DEV-M11-G01: synthesis unit-process template construction and rules.

Covers AC-01 (strict/recovery track labels on templates, independent-batch
defaults), AC-03 (universal rule tables -- parametrized over many
materials, FDM-201 chemistry only as instance data), rule-table integrity,
safe template ids and the deterministic protocol capture.

Every test name contains "synth" (DEV-M11-G01 naming rule).
"""

from __future__ import annotations

import json

import pytest

from scientific_reproduction.analysis.replication import DEFAULT_MIN_INDEPENDENT
from scientific_reproduction.core.models import GoalReplication, GoalTrack
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    CAPTURE_KEYS,
    INDEPENDENT_FLOOR,
    SYNTHESIS_RULESET_VERSION,
    TEMPLATE_PARAMETER_RULES,
    TEMPLATE_VALUE_RULES,
    ActivationTemplate,
    BatchReplicationDefaults,
    InvalidBatchReplicationError,
    InvalidTemplateError,
    SolventExchangeTemplate,
    SynthesisUnitProcessKind,
    SynthesisUnitProcessTemplate,
    capture_protocol,
    missing_parameters,
    validate_synthesis_rulesets,
    validate_template_values,
)

# ---------------------------------------------------------------------------
# Fixtures: instance data. FDM-201 reference chemistry (17-FDM201-REFERENCE-CASE.md)
# appears ONLY here, as instance data -- never in the rule tables (AC-03).
# ---------------------------------------------------------------------------


@pytest.fixture
def fdm201_mof_synth_template() -> SynthesisUnitProcessTemplate:
    """The FDM-201 solvothermal MOF synthesis, as a template instance."""
    return SynthesisUnitProcessTemplate(
        template_id="mof-1-thermal-90-adsorption",
        title="FDM-201 MOF solvothermal synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
        track=GoalTrack.STRICT_REPRODUCTION,
        parameters={
            "metal_source": "zinc acetate dihydrate",
            "organic_linker": "PyBC",
            "solvent": "DMF",
            "temperature_K": 393.0,
            "duration_h": 72.0,
            "stoichiometry": 1.0,
        },
    )


@pytest.fixture
def fictional_oxide_synth_template() -> SynthesisUnitProcessTemplate:
    """A fictional oxide material -- proof the rules are not FDM-201-specific."""
    return SynthesisUnitProcessTemplate(
        template_id="oxide-7-oxor-44",
        title="Beta-oxor-44 oxide sinter synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MATERIAL_SYNTHESIS,
        track=GoalTrack.RECOVERY,
        parameters={
            "precursor": "oxor-7 tetrahydrate",
            "solvent": "butanediol",
            "temperature_K": 873.0,
            "duration_h": 48.0,
            "stoichiometry": 2.5,
        },
    )


# ---------------------------------------------------------------------------
# Rule-table integrity
# ---------------------------------------------------------------------------


def test_synth_ruleset_table_integrity_and_version() -> None:
    """The ordered rule tables are valid and the version is recorded."""
    rule_ids = validate_synthesis_rulesets()
    assert SYNTHESIS_RULESET_VERSION == "1.0"
    assert len(rule_ids) == len(TEMPLATE_PARAMETER_RULES) + len(TEMPLATE_VALUE_RULES)
    # The parameter table's trailing rule is the total default.
    assert TEMPLATE_PARAMETER_RULES[-1].rule_id == "R-TPL-P0"
    # Every value rule table entry applies to exactly one named parameter.
    parameters = [rule.parameter for rule in TEMPLATE_VALUE_RULES]
    assert len(parameters) == len(set(parameters))
    assert TEMPLATE_VALUE_RULES[0].rule_id == "R-TPL-V1"


def test_synth_parameter_rule_table_covers_every_kind() -> None:
    """Every kind has a deciding rule: first match wins, total default last."""
    for kind in SynthesisUnitProcessKind:
        matched = [
            rule
            for rule in TEMPLATE_PARAMETER_RULES
            if rule.predicate(kind)
        ]
        assert matched, kind
        # First match wins; the trailing rule is the total default.
        assert matched[0] is TEMPLATE_PARAMETER_RULES[-1] or (
            matched[0].rule_id != "R-TPL-P0"
        )
    # The five real kinds are decided by their own declared rule.
    for kind, expected in (
        (SynthesisUnitProcessKind.LIGAND_SYNTHESIS, "R-TPL-P1"),
        (SynthesisUnitProcessKind.MATERIAL_SYNTHESIS, "R-TPL-P2"),
        (SynthesisUnitProcessKind.MOF_SYNTHESIS, "R-TPL-P3"),
        (SynthesisUnitProcessKind.ACTIVATION, "R-TPL-P4"),
        (SynthesisUnitProcessKind.SOLVENT_EXCHANGE, "R-TPL-P5"),
    ):
        deciding = next(
            rule for rule in TEMPLATE_PARAMETER_RULES if rule.predicate(kind)
        )
        assert deciding.rule_id == expected


def test_synth_ruleset_matches_parameter_names_verbatim() -> None:
    """The required-parameter table names exactly the value-ruled parameters."""
    value_ruled = {rule.parameter for rule in TEMPLATE_VALUE_RULES}
    for rule in TEMPLATE_PARAMETER_RULES:
        for parameter in rule.required_parameters:
            assert parameter in value_ruled, parameter


# ---------------------------------------------------------------------------
# Construction, value rules, kind narrowing
# ---------------------------------------------------------------------------


def test_synth_template_construction_with_fdm201_instance_data(
    fdm201_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """FDM-201 chemistry constructs fine as instance data (AC-03)."""
    template = fdm201_mof_synth_template
    assert template.template_id == "mof-1-thermal-90-adsorption"
    assert template.unit_process_kind is SynthesisUnitProcessKind.MOF_SYNTHESIS
    assert template.track is GoalTrack.STRICT_REPRODUCTION
    assert template.frozen is False
    assert missing_parameters(template) == ()


def test_synth_template_records_recovery_track_label() -> None:
    """Templates record the recovery label (AC-01) without freezing anything."""
    template = SynthesisUnitProcessTemplate(
        template_id="ligand-9-repro",
        title="Pyridine ligand recovery synthesis",
        unit_process_kind=SynthesisUnitProcessKind.LIGAND_SYNTHESIS,
        track=GoalTrack.RECOVERY,
        parameters={
            "precursor": "pyridine-2-aldehyde",
            "solvent": "ethanol",
            "temperature_K": 351.0,
            "duration_h": 24.0,
            "stoichiometry": 1.0,
        },
    )
    assert template.track is GoalTrack.RECOVERY
    assert template.frozen is False
    capture = capture_protocol(template)
    assert capture["track"] == "RECOVERY"


def test_synth_activation_template_kind_is_fixed() -> None:
    """The activation template accepts only the activation kind."""
    template = ActivationTemplate(
        template_id="activation-fdm201-298k",
        title="FDM-201 activation at 298 K",
        parameters={
            "activation_temperature_K": 298.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
            "pressure_mbar": 1e-3,
        },
    )
    assert template.unit_process_kind is SynthesisUnitProcessKind.ACTIVATION
    with pytest.raises(InvalidTemplateError):
        ActivationTemplate(
            template_id="bad-kind",
            title="Not an activation",
            unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
            parameters={"metal_source": "zinc", "temperature_K": 393.0},
        )


def test_synth_solvent_exchange_template_kind_is_fixed() -> None:
    """The solvent-exchange template accepts only the solvent-exchange kind."""
    template = SolventExchangeTemplate(
        template_id="exchange-fdm201-dmf-to-methanol",
        title="FDM-201 solvent exchange",
        parameters={
            "solvent": "methanol",
            "exchange_cycles": 3,
            "temperature_K": 298.0,
            "soaking_duration_h": 24.0,
        },
    )
    assert template.unit_process_kind is SynthesisUnitProcessKind.SOLVENT_EXCHANGE
    with pytest.raises(InvalidTemplateError):
        SolventExchangeTemplate(
            template_id="bad-kind",
            title="Not an exchange",
            unit_process_kind=SynthesisUnitProcessKind.ACTIVATION,
            parameters={"solvent": "methanol"},
        )


@pytest.mark.parametrize(
    ("parameter_name", "bad_value"),
    [
        ("temperature_K", -5.0),          # R-TPL-V1
        ("temperature_K", float("inf")),  # R-TPL-V1
        ("duration_h", 0),                # R-TPL-V2
        ("stoichiometry", -1.0),          # R-TPL-V3
        ("activation_temperature_K", 0),  # R-TPL-V4
        ("activation_duration_h", -2.0),  # R-TPL-V5
        ("atmosphere", "oxygen"),         # R-TPL-V6 (not controlled vocabulary)
        ("pressure_mbar", -0.5),          # R-TPL-V7
        ("exchange_cycles", 0),           # R-TPL-V8
        ("exchange_cycles", 2.5),         # R-TPL-V8
        ("soaking_duration_h", -1.0),     # R-TPL-V9
        ("solvent", "   "),               # R-TPL-V10
        ("precursor", ""),                # R-TPL-V11
        ("metal_source", ""),             # R-TPL-V12
        ("organic_linker", ""),           # R-TPL-V13
    ],
)
def test_synth_value_rules_reject_invalid_parameter_values(
    parameter_name: str, bad_value: object
) -> None:
    """The universal value rules reject every violation with a stable error."""
    parameters = {
        "metal_source": "zinc acetate dihydrate",
        "organic_linker": "PyBC",
        "solvent": "DMF",
        "temperature_K": 393.0,
        "duration_h": 72.0,
        "stoichiometry": 1.0,
        "activation_temperature_K": 298.0,
        "activation_duration_h": 12.0,
        "atmosphere": "vacuum",
        "pressure_mbar": 1e-3,
        "exchange_cycles": 3,
        "soaking_duration_h": 24.0,
        "precursor": "zinc acetate",
    }
    parameters[parameter_name] = bad_value
    with pytest.raises(InvalidTemplateError, match="R-TPL-V"):
        SynthesisUnitProcessTemplate(
            template_id="mof-bad-value",
            title="Invalid value template",
            unit_process_kind=SynthesisUnitProcessKind.MOF_SYNTHESIS,
            parameters=parameters,
        )


def test_synth_value_validation_assessment_is_auditable(
    fdm201_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """validate_template_values records every rule decision and no violations."""
    assessment = validate_template_values(fdm201_mof_synth_template)
    assert assessment.violations == ()
    assert assessment.matched_rule_id is None
    assert len(assessment.decisions) == len(TEMPLATE_VALUE_RULES)
    applied = [d for d in assessment.decisions if d.applied]
    assert len(applied) == len(fdm201_mof_synth_template.parameters)


def test_synth_type_boundaries_raise_type_error() -> None:
    """Non-string ids and non-GoalTrack labels are TypeError at the boundary."""
    with pytest.raises(TypeError):
        SynthesisUnitProcessTemplate(
            template_id=123,  # type: ignore[arg-type]
            title="x",
        )
    with pytest.raises(TypeError):
        SynthesisUnitProcessTemplate(
            template_id="t1",
            title="x",
            track="STRICT",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        SynthesisUnitProcessTemplate(
            template_id="t1",
            title="x",
            unit_process_kind="mof_synthesis",  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        SynthesisUnitProcessTemplate(
            template_id="t1",
            title="x",
            parameters=["metal_source"],  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Safe ids (FND-M9-G02-01 lesson)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_id",
    ["", ".", "..", "a/b", "a\\b", "a*b", "a?b", "a[b]", "has space"],
)
def test_synth_template_rejects_unsafe_ids(unsafe_id: str) -> None:
    """Template ids must be safe single registry path segments."""
    with pytest.raises(InvalidTemplateError):
        SynthesisUnitProcessTemplate(
            template_id=unsafe_id,
            title="unsafe id",
        )


def test_synth_template_accepts_safe_ids() -> None:
    """Safe registry-style ids construct without error."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-1-thermal-90-adsorption",
        title="safe id",
    )
    assert template.template_id == "mof-1-thermal-90-adsorption"


# ---------------------------------------------------------------------------
# Independent-batch replication defaults (AC-01)
# ---------------------------------------------------------------------------


def test_synth_batch_defaults_follow_frozen_replication_shape() -> None:
    """Defaults mirror GoalReplication with the frozen n >= 3 floor."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-defaults",
        title="Defaults",
    )
    defaults = template.replication
    assert defaults.independent_required is True
    assert defaults.minimum_n == INDEPENDENT_FLOOR == DEFAULT_MIN_INDEPENDENT == 3
    goal_replication = defaults.to_goal_replication()
    assert isinstance(goal_replication, GoalReplication)
    assert goal_replication.minimum_n == defaults.minimum_n
    assert goal_replication.independent_required is True
    assert goal_replication.planned_n_policy == defaults.planned_n_policy


def test_synth_batch_defaults_reject_weakened_floors() -> None:
    """The floor can never be weakened below 1; values are type-checked."""
    with pytest.raises(InvalidBatchReplicationError):
        SynthesisUnitProcessTemplate(
            template_id="mof-bad-floor",
            title="Bad floor",
            replication=BatchReplicationDefaults(minimum_n=0),
        )
    with pytest.raises(InvalidBatchReplicationError):
        SynthesisUnitProcessTemplate(
            template_id="mof-bad-repeats",
            title="Bad repeats",
            replication=BatchReplicationDefaults(technical_repeats=-1),
        )
    with pytest.raises(TypeError):
        SynthesisUnitProcessTemplate(
            template_id="mof-wrong-type",
            title="Wrong type",
            replication="independent",  # type: ignore[arg-type]
        )


def test_synth_template_records_explicit_floor() -> None:
    """An explicit floor is recorded on the template and survives capture."""
    template = SynthesisUnitProcessTemplate(
        template_id="mof-floor-5",
        title="Explicit floor",
        replication=BatchReplicationDefaults(minimum_n=5),
    )
    assert template.replication.minimum_n == 5
    assert capture_protocol(template)["replication"]["minimum_n"] == 5


# ---------------------------------------------------------------------------
# AC-03: universality -- the rules are templates, never FDM-201 thresholds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "material",
    [
        "FDM-201 zinc MOF",           # the reference case (instance data)
        "beta-oxor-44 oxide",         # fictional oxide
        "alpha-silicate glass",       # fictional glass
        "boron nitride ceramic",      # fictional ceramic
        "poly(ethylene oxide) composite",  # fictional polymer
        "lithium orthosilicate",      # fictional battery material
    ],
)
def test_synth_universal_rules_apply_to_any_material(material: str) -> None:
    """The same rule tables serve every material; no universal thresholds."""
    template = SynthesisUnitProcessTemplate(
        template_id=f"material-{len(material)}",
        title=f"{material} synthesis",
        unit_process_kind=SynthesisUnitProcessKind.MATERIAL_SYNTHESIS,
        parameters={
            "precursor": f"{material} precursor",
            "solvent": "water",
            "temperature_K": 298.0,
            "duration_h": 6.0,
            "stoichiometry": 1.0,
        },
    )
    assert missing_parameters(template) == ()
    assert validate_template_values(template).violations == ()


def test_synth_rule_tables_contain_no_instance_chemistry_tokens() -> None:
    """The rule tables never hardcode FDM-201 (or any) chemistry (AC-03).

    Scans the exact source segments of the universal rule tables for the
    instance-data tokens of the reference case: no reagent names, no
    conditions, no temperatures. The segments are recovered from the AST,
    so the scan covers the real table source deterministically.
    """
    import ast
    import inspect

    from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
        templates as synthesis_templates_module,
    )

    source = inspect.getsource(synthesis_templates_module)
    tree = ast.parse(source)
    segments: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id in ("TEMPLATE_PARAMETER_RULES", "TEMPLATE_VALUE_RULES"):
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            segments.append(segment)
    assert segments, "no rule-table source segments found"
    rule_sources = "\n".join(segments)
    for forbidden in ("PyBC", "zinc", "DMF", "393", "298", "solvothermal", "D5TA00771B"):
        assert forbidden not in rule_sources, forbidden


def test_synth_capture_protocol_is_deterministic_snapshot(
    fdm201_mof_synth_template: SynthesisUnitProcessTemplate,
) -> None:
    """The protocol capture is a stable, byte-identical deterministic snapshot."""
    capture = capture_protocol(fdm201_mof_synth_template)
    assert set(capture) == set(CAPTURE_KEYS)
    assert capture["unit_process_kind"] == "mof_synthesis"
    assert capture["frozen"] is False
    assert capture["assumption_refs"] == []
    snapshot = json.dumps(capture, sort_keys=True)
    assert snapshot == json.dumps(capture_protocol(fdm201_mof_synth_template), sort_keys=True)
    # The captured parameter table is sorted by parameter name.
    names = [row["parameter"] for row in capture["parameter_table"]]
    assert names == sorted(names)
    assert capture["parameter_table"][0] == {"parameter": "duration_h", "value": 72.0}


def test_synth_package_exports_are_stable() -> None:
    """The pack wiring exports the public template surface."""
    from scientific_reproduction.domain_packs.materials_chemistry import synthesis

    for name in (
        "SynthesisUnitProcessTemplate",
        "ActivationTemplate",
        "SolventExchangeTemplate",
        "plan_independent_batches",
        "assumptions_for_missing_parameters",
        "freeze_synthesis_template",
        "capture_protocol",
    ):
        assert name in synthesis.__all__, name
        assert hasattr(synthesis, name), name
