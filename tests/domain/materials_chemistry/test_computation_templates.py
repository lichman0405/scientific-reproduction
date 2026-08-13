"""DEV-M11-G04: computation template construction and rules.

Covers AC-01 (software/method/force-field/functional/convergence inputs
explicitly captured on execution templates), AC-02 (execution and
post-processing are separate surfaces with disjoint required parameter
sets, each independently freezable), AC-03 (validated Slurm/Modules
scheduler metadata), rule-table integrity, safe template ids and the
deterministic protocol capture.

Every test name contains "comput" (DEV-M11-G04 naming rule: the
verification command selects this goal with ``-k comput``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scientific_reproduction.core.models import GoalTrack
from scientific_reproduction.core.permissions import Role
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    ANALYSIS_STAGE,
    CAPTURE_KEYS,
    COMPUTATION_PARAMETER_RULES,
    COMPUTATION_RULESET_VERSION,
    COMPUTATION_VALUE_RULES,
    EXECUTION_STAGE,
    ComputationKind,
    ComputationStage,
    DftTemplate,
    GcmcTemplate,
    InvalidComputationTemplateError,
    InvalidSchedulerOptionsError,
    MdTemplate,
    SchedulerOptions,
    StructurePreparationTemplate,
    capture_protocol,
    freeze_computation_template,
    missing_parameters,
    validate_computation_rulesets,
    validate_template_values,
)

# ---------------------------------------------------------------------------
# Fixtures: instance data. Software/functional/force-field/convergence values
# appear ONLY here, as instance data -- never in the rule tables (AC-01/AC-03).
# ---------------------------------------------------------------------------


@pytest.fixture
def dft_execution_template() -> DftTemplate:
    """A complete DFT execution template with Slurm/Modules metadata."""
    return DftTemplate(
        template_id="dft-1-opt-90",
        title="FDM-201 DFT geometry optimization",
        stage=EXECUTION_STAGE,
        track=GoalTrack.STRICT_REPRODUCTION,
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
        scheduler=SchedulerOptions(
            partition="compute",
            account="materials-ads",
            qos="normal",
            nodes=2,
            tasks_per_node=32,
            walltime_hours=48.0,
            modules=("vasp/5.4.4", "intel/2021.4.0"),
        ),
    )


@pytest.fixture
def gcmc_analysis_template() -> GcmcTemplate:
    """A complete GCMC post-processing (analysis) template."""
    return GcmcTemplate(
        template_id="gcmc-1-co2-ads",
        title="FDM-201 GCMC adsorption post-processing",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "absolute_adsorption",
            "convergence_metric": "block_average_drift",
            "convergence_threshold": 0.01,
            "statistical_uncertainty_metric": "standard_error",
            "sampling_validation": "production blocks beyond correlation time",
        },
    )


# ---------------------------------------------------------------------------
# Rule-table integrity
# ---------------------------------------------------------------------------


def test_comput_ruleset_table_integrity_and_version() -> None:
    """The ordered rule tables are valid and the version is recorded."""
    rule_ids = validate_computation_rulesets()
    assert COMPUTATION_RULESET_VERSION == "1.0"
    assert len(rule_ids) == len(COMPUTATION_PARAMETER_RULES) + len(
        COMPUTATION_VALUE_RULES
    )
    # The parameter table's trailing rule is the total default.
    assert COMPUTATION_PARAMETER_RULES[-1].rule_id == "R-COM-P0"
    # Every value rule table entry applies to exactly one named parameter.
    parameters = [rule.parameter for rule in COMPUTATION_VALUE_RULES]
    assert len(parameters) == len(set(parameters))
    assert COMPUTATION_VALUE_RULES[0].rule_id == "R-COM-V1"


def test_comput_parameter_rule_table_covers_every_kind_stage_pair() -> None:
    """Every (kind, stage) pair has a deciding rule: first match, total default."""
    pairs = [
        (kind, stage)
        for kind in ComputationKind
        for stage in ComputationStage
    ]
    for pair in pairs:
        matched = [
            rule
            for rule in COMPUTATION_PARAMETER_RULES
            if rule.predicate(*pair)
        ]
        assert matched, pair
        # First match wins; the trailing rule is the total default.
        assert matched[0] is COMPUTATION_PARAMETER_RULES[-1] or (
            matched[0].rule_id != "R-COM-P0"
        )
    # The eight real pairs are decided by their own declared rule.
    for pair, expected in (
        ((ComputationKind.STRUCTURE_PREPARATION, ComputationStage.EXECUTION), "R-COM-P1"),
        ((ComputationKind.DFT, ComputationStage.EXECUTION), "R-COM-P2"),
        ((ComputationKind.GCMC, ComputationStage.EXECUTION), "R-COM-P3"),
        ((ComputationKind.MD, ComputationStage.EXECUTION), "R-COM-P4"),
        ((ComputationKind.STRUCTURE_PREPARATION, ComputationStage.ANALYSIS), "R-COM-P5"),
        ((ComputationKind.DFT, ComputationStage.ANALYSIS), "R-COM-P6"),
        ((ComputationKind.GCMC, ComputationStage.ANALYSIS), "R-COM-P7"),
        ((ComputationKind.MD, ComputationStage.ANALYSIS), "R-COM-P8"),
    ):
        deciding = next(
            rule
            for rule in COMPUTATION_PARAMETER_RULES
            if rule.predicate(*pair)
        )
        assert deciding.rule_id == expected


def test_comput_execution_and_analysis_require_disjoint_parameter_sets() -> None:
    """AC-02: the execution and analysis required sets are disjoint surfaces."""
    required_by_stage: dict[ComputationStage, set[str]] = {
        stage: set()
        for stage in ComputationStage
    }
    for rule in COMPUTATION_PARAMETER_RULES:
        for pair in [
            (kind, stage)
            for kind in ComputationKind
            for stage in ComputationStage
        ]:
            if rule.predicate(*pair) and rule.rule_id != "R-COM-P0":
                required_by_stage[pair[1]].update(rule.required_parameters)
    # The execution surface captures software/method/settings; the analysis
    # surface captures property/convergence/uncertainty validation. No
    # parameter is required on both surfaces.
    assert required_by_stage[ComputationStage.EXECUTION].isdisjoint(
        required_by_stage[ComputationStage.ANALYSIS]
    )


def test_comput_ruleset_matches_parameter_names_verbatim() -> None:
    """The required-parameter table names exactly the value-ruled parameters."""
    value_ruled = {rule.parameter for rule in COMPUTATION_VALUE_RULES}
    for rule in COMPUTATION_PARAMETER_RULES:
        for parameter in rule.required_parameters:
            assert parameter in value_ruled, parameter


# ---------------------------------------------------------------------------
# Construction: AC-01 explicit capture on the four execution surfaces
# ---------------------------------------------------------------------------


def test_comput_dft_execution_captures_software_method_functional_convergence(
    dft_execution_template: DftTemplate,
) -> None:
    """AC-01: DFT software/method/functional/dispersion/basis/convergence captured."""
    template = dft_execution_template
    assert template.kind is ComputationKind.DFT
    assert template.stage is EXECUTION_STAGE
    assert template.track is GoalTrack.STRICT_REPRODUCTION
    assert template.frozen is False
    assert missing_parameters(template) == ()
    assert template.parameters["software"] == "vasp"
    assert template.parameters["functional"] == "PBE"
    assert template.parameters["kpoint_mesh"] == (3, 3, 2)
    assert template.parameters["energy_cutoff_ev"] == 520.0
    assert template.parameters["convergence_tolerance"] == 1e-5


def test_comput_gcmc_execution_captures_force_field_and_cycles(
) -> None:
    """AC-01: GCMC force-field/charges/mixing/cutoff/cycles/seed captured."""
    template = GcmcTemplate(
        template_id="gcmc-1-co2-ads",
        title="FDM-201 GCMC adsorption",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "raspa",
            "software_version": "2.0.47",
            "force_field": "DREIDING",
            "charges": "qeq",
            "mixing_rules": "lorentz_berthelot",
            "cutoff_angstrom": 12.0,
            "temperature_K": 298.0,
            "pressure_bar": 1.0,
            "equilibration_cycles": 20000,
            "production_cycles": 40000,
            "seed": 42,
        },
    )
    assert template.kind is ComputationKind.GCMC
    assert template.parameters["force_field"] == "DREIDING"
    assert missing_parameters(template) == ()


def test_comput_md_execution_captures_integration_and_thermostat() -> None:
    """AC-01: MD force-field/ensemble/thermostat/timestep/steps captured."""
    template = MdTemplate(
        template_id="md-1-diffusion",
        title="FDM-201 methane diffusion MD",
        stage=EXECUTION_STAGE,
        parameters={
            "software": "gromacs",
            "software_version": "2023.2",
            "force_field": "OPLS-AA",
            "charges": "from_force_field",
            "ensemble": "NVT",
            "thermostat": "v-rescale",
            "barostat": "none",
            "temperature_K": 298.0,
            "pressure_bar": 1.0,
            "timestep_fs": 1.0,
            "n_steps": 1000000,
            "cutoff_angstrom": 10.0,
            "seed": 7,
        },
    )
    assert template.kind is ComputationKind.MD
    assert template.parameters["thermostat"] == "v-rescale"
    assert template.parameters["n_steps"] == 1000000
    assert missing_parameters(template) == ()


def test_comput_structure_preparation_captures_disorder_handling() -> None:
    """AC-01: structure preparation source/disorder/method/software captured."""
    template = StructurePreparationTemplate(
        template_id="prep-1-disorder",
        title="FDM-201 structure model preparation",
        stage=EXECUTION_STAGE,
        parameters={
            "structure_source": "scxrd_solution_model",
            "disorder_treatment": "occupancy_partial",
            "method": "supercell_construction",
            "software": "pymatgen",
            "software_version": "2024.5.1",
        },
    )
    assert template.kind is ComputationKind.STRUCTURE_PREPARATION
    assert template.parameters["disorder_treatment"] == "occupancy_partial"
    assert missing_parameters(template) == ()


def test_comput_analysis_template_requires_validation_parameters() -> None:
    """AC-02: the analysis surface requires property/convergence/sampling."""
    template = DftTemplate(
        template_id="dft-1-analysis",
        title="FDM-201 DFT analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "binding_energy",
            "convergence_metric": "energy_difference",
        },
    )
    assert template.stage is ANALYSIS_STAGE
    assert missing_parameters(template) == (
        "convergence_threshold",
        "statistical_uncertainty_metric",
        "sampling_validation",
        "finite_size_correction",
    )
    complete = DftTemplate(
        template_id="dft-1-analysis-full",
        title="FDM-201 DFT analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "binding_energy",
            "convergence_metric": "energy_difference",
            "convergence_threshold": 0.001,
            "statistical_uncertainty_metric": "standard_error",
            "sampling_validation": "k-point and cutoff convergence series",
            "finite_size_correction": "single_k_point_extrapolation",
        },
    )
    assert missing_parameters(complete) == ()


def _construct_dft_via_kwargs(**kwargs: Any) -> DftTemplate:
    """Construct through Any kwargs (runtime TypeError when stage is missing)."""
    return DftTemplate(**kwargs)


def test_comput_stage_is_explicit_on_every_template() -> None:
    """AC-02: the surface is never implied -- stage must always be named."""
    with pytest.raises(TypeError):
        _construct_dft_via_kwargs(template_id="dft-x", title="No stage")


# ---------------------------------------------------------------------------
# Kind narrowing
# ---------------------------------------------------------------------------


def test_comput_template_kinds_are_fixed_per_class() -> None:
    """Each class fixes its computation kind (default) and rejects others."""
    assert DftTemplate(
        template_id="dft-1", title="DFT", stage=EXECUTION_STAGE
    ).kind is ComputationKind.DFT
    assert GcmcTemplate(
        template_id="gcmc-1", title="GCMC", stage=EXECUTION_STAGE
    ).kind is ComputationKind.GCMC
    assert MdTemplate(
        template_id="md-1", title="MD", stage=EXECUTION_STAGE
    ).kind is ComputationKind.MD
    assert StructurePreparationTemplate(
        template_id="prep-1", title="Prep", stage=EXECUTION_STAGE
    ).kind is ComputationKind.STRUCTURE_PREPARATION
    with pytest.raises(InvalidComputationTemplateError):
        DftTemplate(
            template_id="dft-bad",
            title="Not DFT",
            stage=EXECUTION_STAGE,
            kind=ComputationKind.GCMC,
        )
    with pytest.raises(InvalidComputationTemplateError):
        GcmcTemplate(
            template_id="gcmc-bad",
            title="Not GCMC",
            stage=EXECUTION_STAGE,
            kind=ComputationKind.MD,
        )


# ---------------------------------------------------------------------------
# Universal value rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parameter_name", "bad_value"),
    [
        ("software", "   "),                       # R-COM-V1
        ("software_version", ""),                  # R-COM-V2
        ("method", ""),                            # R-COM-V3
        ("functional", ""),                        # R-COM-V4
        ("dispersion_correction", ""),             # R-COM-V5
        ("basis_set", ""),                         # R-COM-V6
        ("pseudopotential", ""),                   # R-COM-V7
        ("force_field", ""),                       # R-COM-V8
        ("charges", ""),                           # R-COM-V9
        ("mixing_rules", ""),                      # R-COM-V10
        ("ensemble", ""),                          # R-COM-V11
        ("thermostat", ""),                        # R-COM-V12
        ("barostat", ""),                          # R-COM-V13
        ("kpoint_mesh", (1, 2)),                   # R-COM-V14 (not 3-vector)
        ("kpoint_mesh", (1, 2, 0)),                # R-COM-V14 (not positive)
        ("kpoint_mesh", (1.0, 2, 3)),              # R-COM-V14 (not ints)
        ("kpoint_mesh", "1 2 3"),                  # R-COM-V14 (not a vector)
        ("energy_cutoff_ev", 0),                   # R-COM-V15
        ("energy_cutoff_ev", float("inf")),        # R-COM-V15
        ("cutoff_angstrom", -1.0),                 # R-COM-V16
        ("convergence_tolerance", 0.0),            # R-COM-V17
        ("temperature_K", -5.0),                   # R-COM-V18
        ("pressure_bar", -0.5),                    # R-COM-V19
        ("timestep_fs", -1.0),                     # R-COM-V20
        ("n_steps", 0),                            # R-COM-V21
        ("n_steps", 2.5),                          # R-COM-V21
        ("seed", 0),                               # R-COM-V22
        ("equilibration_cycles", 0),               # R-COM-V23
        ("production_cycles", 1.5),                # R-COM-V24
        ("structure_source", ""),                  # R-COM-V25
        ("disorder_treatment", "  "),              # R-COM-V26
        ("property", ""),                          # R-COM-V27
        ("convergence_metric", ""),                # R-COM-V28
        ("convergence_threshold", -0.01),          # R-COM-V29
        ("statistical_uncertainty_metric", ""),    # R-COM-V30
        ("sampling_validation", ""),               # R-COM-V31
        ("finite_size_correction", ""),            # R-COM-V32
        ("reference_value", float("nan")),         # R-COM-V33 (not finite)
        ("reference_value", "x"),                  # R-COM-V33 (not a number)
        ("tolerance", 0),                          # R-COM-V34
    ],
)
def test_comput_value_rules_reject_invalid_parameter_values(
    parameter_name: str, bad_value: object
) -> None:
    """The universal value rules reject every violation with a stable error."""
    with pytest.raises(InvalidComputationTemplateError, match="R-COM-V"):
        DftTemplate(
            template_id="dft-bad-value",
            title="Invalid value template",
            stage=EXECUTION_STAGE,
            parameters={parameter_name: bad_value},
        )


def test_comput_value_rules_accept_negative_reference_values() -> None:
    """R-COM-V33: binding energies may be negative -- finite of any sign."""
    template = StructurePreparationTemplate(
        template_id="prep-1-validate",
        title="Structure validation",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "volume_rmsd",
            "convergence_metric": "geometry_rmsd",
            "convergence_threshold": 0.05,
            "reference_value": -12.5,
            "tolerance": 0.5,
        },
    )
    assert validate_template_values(template).violations == ()
    assert missing_parameters(template) == ()


def test_comput_value_validation_assessment_is_auditable(
    dft_execution_template: DftTemplate,
) -> None:
    """validate_template_values records every rule decision and no violations."""
    assessment = validate_template_values(dft_execution_template)
    assert assessment.violations == ()
    assert assessment.matched_rule_id is None
    assert len(assessment.decisions) == len(COMPUTATION_VALUE_RULES)
    applied = [d for d in assessment.decisions if d.applied]
    assert len(applied) == len(dft_execution_template.parameters)


def test_comput_type_boundaries_raise_type_error() -> None:
    """Non-string ids, non-enum labels and wrong shapes are TypeError."""
    bad_id: Any = 123
    with pytest.raises(TypeError):
        DftTemplate(template_id=bad_id, title="x", stage=EXECUTION_STAGE)
    bad_title: Any = 7
    with pytest.raises(TypeError):
        DftTemplate(template_id="t1", title=bad_title, stage=EXECUTION_STAGE)
    bad_stage: Any = "execution"
    with pytest.raises(TypeError):
        DftTemplate(template_id="t1", title="x", stage=bad_stage)
    bad_kind: Any = "dft"
    with pytest.raises(TypeError):
        DftTemplate(
            template_id="t1", title="x", stage=EXECUTION_STAGE, kind=bad_kind
        )
    bad_track: Any = "STRICT"
    with pytest.raises(TypeError):
        DftTemplate(
            template_id="t1", title="x", stage=EXECUTION_STAGE, track=bad_track
        )
    bad_parameters: Any = ["functional"]
    with pytest.raises(TypeError):
        DftTemplate(
            template_id="t1",
            title="x",
            stage=EXECUTION_STAGE,
            parameters=bad_parameters,
        )
    bad_refs: Any = "ref"
    with pytest.raises(TypeError):
        DftTemplate(
            template_id="t1",
            title="x",
            stage=EXECUTION_STAGE,
            assumption_refs=bad_refs,
        )
    bad_frozen: Any = "yes"
    with pytest.raises(TypeError):
        DftTemplate(
            template_id="t1", title="x", stage=EXECUTION_STAGE, frozen=bad_frozen
        )
    bad_scheduler: Any = "slurm"
    with pytest.raises(TypeError):
        DftTemplate(
            template_id="t1",
            title="x",
            stage=EXECUTION_STAGE,
            scheduler=bad_scheduler,
        )


# ---------------------------------------------------------------------------
# Safe ids (FND-M9-G02-01 lesson)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unsafe_id",
    ["", ".", "..", "a/b", "a\\b", "a*b", "a?b", "a[b]", "has space"],
)
def test_comput_template_rejects_unsafe_ids(unsafe_id: str) -> None:
    """Template ids must be safe single registry path segments."""
    with pytest.raises(InvalidComputationTemplateError):
        DftTemplate(
            template_id=unsafe_id, title="unsafe id", stage=EXECUTION_STAGE
        )


def test_comput_template_accepts_safe_ids() -> None:
    """Safe registry-style ids construct without error."""
    template = DftTemplate(
        template_id="dft-1-opt-90", title="safe id", stage=EXECUTION_STAGE
    )
    assert template.template_id == "dft-1-opt-90"


# ---------------------------------------------------------------------------
# Strict/recovery track labels (AC-01)
# ---------------------------------------------------------------------------


def test_comput_template_records_recovery_track_label() -> None:
    """Templates record the recovery label (AC-01) without freezing anything."""
    template = GcmcTemplate(
        template_id="gcmc-2-recovery",
        title="GCMC recovery adsorption",
        stage=EXECUTION_STAGE,
        track=GoalTrack.RECOVERY,
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
            "seed": 5,
        },
    )
    assert template.track is GoalTrack.RECOVERY
    assert template.frozen is False
    capture = capture_protocol(template)
    assert capture["track"] == "RECOVERY"


# ---------------------------------------------------------------------------
# AC-03: Slurm/Modules scheduler metadata
# ---------------------------------------------------------------------------


def test_comput_scheduler_options_are_validated_and_captured(
    dft_execution_template: DftTemplate,
) -> None:
    """AC-03: partition/account/QOS/nodes/walltime/modules are recorded."""
    scheduler = dft_execution_template.scheduler
    assert scheduler is not None
    assert scheduler.partition == "compute"
    assert scheduler.account == "materials-ads"
    assert scheduler.nodes == 2
    assert scheduler.tasks_per_node == 32
    assert scheduler.walltime_hours == 48.0
    assert scheduler.modules == ("vasp/5.4.4", "intel/2021.4.0")
    capture = capture_protocol(dft_execution_template)
    assert capture["scheduler"] == {
        "partition": "compute",
        "account": "materials-ads",
        "qos": "normal",
        "nodes": 2,
        "tasks_per_node": 32,
        "walltime_hours": 48.0,
        "modules": ["vasp/5.4.4", "intel/2021.4.0"],
    }


def test_comput_scheduler_none_when_absent() -> None:
    """No scheduler section is recorded when none was provided."""
    template = DftTemplate(
        template_id="dft-1",
        title="No scheduler",
        stage=EXECUTION_STAGE,
    )
    assert template.scheduler is None
    assert capture_protocol(template)["scheduler"] is None


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"partition": ""}, InvalidSchedulerOptionsError),
        ({"partition": "   "}, InvalidSchedulerOptionsError),
        ({"account": ""}, InvalidSchedulerOptionsError),
        ({"qos": ""}, InvalidSchedulerOptionsError),
        ({"nodes": 0}, InvalidSchedulerOptionsError),
        ({"nodes": -2}, InvalidSchedulerOptionsError),
        ({"tasks_per_node": 0}, InvalidSchedulerOptionsError),
        ({"walltime_hours": 0.0}, InvalidSchedulerOptionsError),
        ({"walltime_hours": -5.0}, InvalidSchedulerOptionsError),
        ({"walltime_hours": float("inf")}, InvalidSchedulerOptionsError),
        ({"modules": ("",)}, InvalidSchedulerOptionsError),
    ],
)
def test_comput_scheduler_options_reject_invalid_values(
    kwargs: dict[str, Any], error: type[Exception]
) -> None:
    """AC-03: the scheduler section enforces its own value rules."""
    with pytest.raises(error):
        SchedulerOptions(**kwargs)


def test_comput_scheduler_options_type_boundaries_raise_type_error() -> None:
    """Wrong scheduler field types are TypeError at the boundary."""
    bad_partition: Any = 3
    with pytest.raises(TypeError):
        SchedulerOptions(partition=bad_partition)
    bad_nodes: Any = "4"
    with pytest.raises(TypeError):
        SchedulerOptions(nodes=bad_nodes)
    bad_walltime: Any = "48"
    with pytest.raises(TypeError):
        SchedulerOptions(walltime_hours=bad_walltime)
    bad_modules: Any = ("gromacs", 3)
    with pytest.raises(TypeError):
        SchedulerOptions(modules=bad_modules)


def _setattr_through_instance(instance: object, name: str, value: object) -> None:
    """setattr through the dataclass __setattr__ (object-typed, ignore-free)."""
    setattr(instance, name, value)


def test_comput_scheduler_section_is_frozen_dataclass() -> None:
    """The scheduler section is an immutable value object."""
    from dataclasses import FrozenInstanceError, is_dataclass

    scheduler = SchedulerOptions(
        partition="compute",
        modules=("gromacs/2023.2",),
    )
    assert is_dataclass(scheduler)
    with pytest.raises(FrozenInstanceError):
        _setattr_through_instance(scheduler, "partition", "other")
    assert scheduler.as_dict()["modules"] == ["gromacs/2023.2"]
    assert scheduler.as_dict()["partition"] == "compute"


# ---------------------------------------------------------------------------
# AC-02: execution and analysis are separate, independently freezable surfaces
# ---------------------------------------------------------------------------


def test_comput_execution_and_analysis_surfaces_freeze_independently() -> None:
    """AC-02: freezing one surface never freezes the other."""
    execution = DftTemplate(
        template_id="dft-2-exec",
        title="DFT execution",
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
    analysis = DftTemplate(
        template_id="dft-2-analysis",
        title="DFT analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "binding_energy",
            "convergence_metric": "energy_difference",
            "convergence_threshold": 0.001,
            "statistical_uncertainty_metric": "standard_error",
            "sampling_validation": "k-point and cutoff convergence series",
            "finite_size_correction": "single_k_point_extrapolation",
        },
    )
    frozen_execution = freeze_computation_template(execution, role=Role.SUPERVISOR)
    assert frozen_execution.frozen is True
    # The analysis surface is a separate instance with its own freeze state.
    assert analysis.frozen is False
    frozen_analysis = freeze_computation_template(analysis, role=Role.SUPERVISOR)
    assert frozen_analysis.frozen is True
    assert frozen_execution.frozen is True
    assert frozen_execution is not frozen_analysis


# ---------------------------------------------------------------------------
# Deterministic protocol capture
# ---------------------------------------------------------------------------


def test_comput_capture_protocol_is_deterministic_snapshot(
    dft_execution_template: DftTemplate,
) -> None:
    """The protocol capture is a stable, byte-identical deterministic snapshot."""
    capture = capture_protocol(dft_execution_template)
    assert set(capture) == set(CAPTURE_KEYS)
    assert capture["stage"] == "execution"
    assert capture["kind"] == "dft"
    assert capture["frozen"] is False
    assert capture["assumption_refs"] == []
    snapshot = json.dumps(capture, sort_keys=True)
    assert snapshot == json.dumps(
        capture_protocol(dft_execution_template), sort_keys=True
    )
    # The captured parameter table is sorted by parameter name.
    names = [row["parameter"] for row in capture["parameter_table"]]
    assert names == sorted(names)
    assert capture["parameter_table"][0] == {
        "parameter": "basis_set",
        "value": "PAW",
    }


def test_comput_capture_distinguishes_execution_and_analysis_surfaces() -> None:
    """AC-02: the capture records which surface a template belongs to."""
    execution = DftTemplate(
        template_id="dft-3-exec",
        title="DFT execution",
        stage=EXECUTION_STAGE,
        parameters={"software": "vasp"},
    )
    analysis = DftTemplate(
        template_id="dft-3-analysis",
        title="DFT analysis",
        stage=ANALYSIS_STAGE,
        parameters={"property": "binding_energy"},
    )
    assert capture_protocol(execution)["stage"] == "execution"
    assert capture_protocol(analysis)["stage"] == "analysis"


def test_comput_package_exports_are_stable() -> None:
    """The pack wiring exports the public template surface."""
    from scientific_reproduction.domain_packs.materials_chemistry import (
        computation,
    )

    for name in (
        "DftTemplate",
        "GcmcTemplate",
        "MdTemplate",
        "StructurePreparationTemplate",
        "SchedulerOptions",
        "assumptions_for_missing_parameters",
        "freeze_computation_template",
        "capture_protocol",
    ):
        assert name in computation.__all__, name
        assert hasattr(computation, name), name
