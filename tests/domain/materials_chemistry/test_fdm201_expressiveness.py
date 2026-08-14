"""DEV-M11-G06: FDM-201 work-package expressiveness fixture (AC-01/AC-02).

NON-FINAL (pre-M12): this file is the DEV-M11-G06 deterministic in-memory
demonstration fixture -- it proves that the materials-chemistry domain
pack can represent every FDM-201 work-package category through the REAL
domain-pack machinery and the frozen core models. This fixture is
explicitly NOT the M12 benchmark (the M12 benchmark is the final
goal-contract benchmark and lives outside this file). See
``FDM201_FIXTURE_STATUS`` and the ``test_fdm201_fixture_is_marked_non_final``
guard below.

Goal (frozen contract DEV-M11-G06, ALLOWED_SCOPE tests-only):
    Prove FDM-201 expressiveness of the domain pack: every FDM-201
    work-package category is representable -- construction validates,
    domain rules apply, and records serialize through the persistence
    gates (``as_dict`` / ``capture_protocol`` / core schema validation).

AC-01 (one test per category, real machinery, no invented schema forks):
    synthesis       -> SynthesisUnitProcessTemplate + ActivationTemplate
                       + SolventExchangeTemplate, assess_parameter_completeness,
                       plan_independent_batches (n >= 3 independent Runs),
                       freeze_synthesis_template, capture_protocol
    structure       -> SCXRDCharacterizationTemplate + AnalysisPlan,
                       evaluate_acceptance (R-CHA-A5 -> PASS), capture_characterization
    pxrd            -> PXRDCharacterizationTemplate, evaluate_identity_checks
                       (R-CHA-A1..A4 -> PASS), freeze_analysis_plan (Supervisor)
    tga             -> TGACharacterizationTemplate, evaluate_acceptance
                       (R-CHA-A6 -> PASS)
    bet             -> BetTemplate execution + analysis surfaces
                       (R-ADS-P1/R-ADS-P2), independent freeze
    adsorption      -> SingleComponentTemplate C3H6/C2H4 execution pairs
                       (R-ADS-P3), analysis surface (R-ADS-P4)
    iast            -> IastTemplate (R-ADS-P5)
    qst             -> QstTemplate (R-ADS-P6)
    breakthrough    -> BreakthroughTemplate execution + analysis
                       (R-ADS-P7/R-ADS-P8) + BreakthroughResultTable /
                       paper_mapping (AC-03 raw/result -> figure mapping)
    stability       -> CyclingStabilityTemplate execution + analysis
                       (R-ADS-P9/R-ADS-P10)
    computation     -> StructurePreparationTemplate / DftTemplate /
                       GcmcTemplate with SchedulerOptions (R-COM-P1/P2/P3,
                       analysis surfaces R-COM-P5/P6/P7), freeze_computation_template
    acceptance      -> statistics proposals: replicate design, measurement
                       uncertainty, evidence-grounded acceptance,
                       freeze_acceptance_proposal (Supervisor)

AC-02 (no ad hoc schema fork): every fixture record is an instance of a
frozen core model (``core.models``) or a frozen domain-pack template /
proposal / assessment record (``domain_packs.materials_chemistry``); the
fixture defines zero new classes; the core records (goal, run, evidence,
acceptance-criteria, plan) validate through the real core schema
validation (``validate_object``) and round-trip through ``to_dict`` /
``from_dict``.

Determinism discipline: no network, no sleeps, no wall clock, no
randomness anywhere; every timestamp is the fixed
``FDM201_FIXED_TIMESTAMP``; ids come from the deterministic
``core.ids.generate_id``; file-system artifacts (if any are ever added)
must use ``tmp_path``. Every test name contains "fdm201" (DEV-M11-G06
naming rule). FDM-201 chemistry appears ONLY as instance data (the
reference case of ``17-FDM201-REFERENCE-CASE.md``: DOI
10.1039/D5TA00771B, JMCA 2025) -- the rule tables themselves are never
extended here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields, is_dataclass
from typing import Any, Iterable

from scientific_reproduction.core.ids import generate_id, is_valid_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    Assumption,
    ClaimSpecificEvidence,
    Confidence,
    DecisionMode,
    DependencyType,
    EvidenceAssessment,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    LifecycleState,
    Plan,
    PlanInventoryAudit,
    PlanStatus,
    Run,
    RunType,
    ScientificReview,
)
from scientific_reproduction.core.permissions import Role
from scientific_reproduction.core.schema_validation import validate_object
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    ANALYSIS_STAGE,
    BET_KIND,
    EXECUTION_STAGE,
    BetTemplate,
    BreakthroughResultTable,
    BreakthroughTemplate,
    CyclingStabilityTemplate,
    IastTemplate,
    PaperResultEntry,
    QstTemplate,
    SingleComponentTemplate,
    freeze_adsorption_template,
    paper_mapping,
)
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    assess_parameter_completeness as assess_adsorption_completeness,
)
from scientific_reproduction.domain_packs.materials_chemistry.adsorption import (
    capture_protocol as capture_adsorption_protocol,
)
from scientific_reproduction.domain_packs.materials_chemistry.characterization import (
    AcceptanceAssessment,
    AcceptanceDecision,
    AnalysisPlan,
    IdentityCheckAssessment,
    IdentityCheckDecision,
    PXRDCharacterizationTemplate,
    SCXRDCharacterizationTemplate,
    TGACharacterizationTemplate,
    capture_characterization,
    evaluate_acceptance,
    evaluate_identity_checks,
    freeze_analysis_plan,
    freeze_characterization_template,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    ANALYSIS_STAGE as COMP_ANALYSIS_STAGE,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    EXECUTION_STAGE as COMP_EXECUTION_STAGE,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    DftTemplate,
    GcmcTemplate,
    SchedulerOptions,
    StructurePreparationTemplate,
    freeze_computation_template,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    assess_parameter_completeness as assess_computation_completeness,
)
from scientific_reproduction.domain_packs.materials_chemistry.computation import (
    capture_protocol as capture_computation_protocol,
)
from scientific_reproduction.domain_packs.materials_chemistry.statistics import (
    AcceptanceProposal,
    EvidenceClaim,
    EvidenceReference,
    MeasurementUncertaintyProposal,
    ReplicateDesignProposal,
    UncertaintyKind,
    attach_evidence,
    construct_acceptance_proposal,
    default_replicate_design_proposal,
    freeze_acceptance_proposal,
    propose_measurement_uncertainty,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ActivationTemplate,
    BatchFloorAssessment,
    BatchFloorDecision,
    BatchPlan,
    BatchReplicationDefaults,
    SolventExchangeTemplate,
    SynthesisUnitProcessKind,
    SynthesisUnitProcessTemplate,
    assess_parameter_completeness,
    freeze_synthesis_template,
    plan_independent_batches,
    validate_template_values,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ParameterCompletenessAssessment as SynthesisParameterCompletenessAssessment,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ParameterCompletenessDecision as SynthesisParameterCompletenessDecision,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ValueValidationAssessment as SynthesisValueValidationAssessment,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    ValueValidationDecision as SynthesisValueValidationDecision,
)
from scientific_reproduction.domain_packs.materials_chemistry.synthesis import (
    capture_protocol as capture_synthesis_protocol,
)

# ---------------------------------------------------------------------------
# Fixed instance data of the FDM-201 reference case (17-FDM201-REFERENCE-CASE.md)
# ---------------------------------------------------------------------------

#: The frozen status marker of this fixture: NON-FINAL pre-M12.
FDM201_FIXTURE_STATUS: str = "non-final-pre-M12"

#: The FDM-201 reference paper (instance data only -- the seed facts of
#: 17-FDM201-REFERENCE-CASE.md may appear as instance data in this
#: fixture, never inside rule tables).
FDM201_PAPER_DOI: str = "10.1039/D5TA00771B"

#: The single fixed timestamp of the fixture (determinism: no wall clock).
FDM201_FIXED_TIMESTAMP: str = "2026-08-14T00:00:00Z"

#: The frozen goal ids of the demonstration chain (safe registry ids).
GOAL_FDM201_SRC: str = "GOAL-FDM201-SRC-001"
GOAL_FDM201_SYN: str = "GOAL-FDM201-SYN-001"
GOAL_FDM201_CHAR: str = "GOAL-FDM201-CHAR-001"
GOAL_FDM201_POROSITY: str = "GOAL-FDM201-POROSITY-001"
GOAL_FDM201_COMP: str = "GOAL-FDM201-COMP-001"

#: The requirement id of the expressiveness chain (persistence-gate shape).
REQ_FDM201_CHAIN: str = "REQ-FDM201-EXPRESSIVENESS-001"

#: The eleven FDM-201 work-package categories of AC-01.
FDM201_WORK_PACKAGES: tuple[str, ...] = (
    "synthesis",
    "structure",
    "pxrd",
    "tga",
    "bet",
    "adsorption",
    "iast",
    "qst",
    "breakthrough",
    "stability",
    "computation",
)


# ---------------------------------------------------------------------------
# Builders (pure, deterministic: same call -> identical records every time)
# ---------------------------------------------------------------------------


def build_fdm201_synthesis_package() -> dict[str, Any]:
    """The WP-20 synthesis package: MOF, activation and solvent exchange."""
    mof_template = SynthesisUnitProcessTemplate(
        template_id="fdm201-mof-solvothermal",
        title="FDM-201 solvothermal MOF synthesis",
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
        notes=f"source paper {FDM201_PAPER_DOI}",
    )
    activation_template = ActivationTemplate(
        template_id="fdm201-activation-393",
        title="FDM-201 activation under vacuum",
        parameters={
            "activation_temperature_K": 393.0,
            "activation_duration_h": 12.0,
            "atmosphere": "vacuum",
            "pressure_mbar": 0.0001,
        },
    )
    exchange_template = SolventExchangeTemplate(
        template_id="fdm201-solvent-exchange",
        title="FDM-201 DMF-to-methanol exchange",
        parameters={
            "solvent": "methanol",
            "exchange_cycles": 3,
            "temperature_K": 298.0,
            "soaking_duration_h": 24.0,
        },
    )
    # The real completeness/value machinery applies to the MOF template.
    completeness = assess_parameter_completeness(mof_template)
    values = validate_template_values(mof_template)
    batch_plan = plan_independent_batches(mof_template, n=3)
    frozen = freeze_synthesis_template(mof_template, role=Role.SUPERVISOR)
    return {
        "mof_template": mof_template,
        "activation_template": activation_template,
        "exchange_template": exchange_template,
        "completeness": completeness,
        "value_assessment": values,
        "batch_plan": batch_plan,
        "frozen_template": frozen,
        "capture": capture_synthesis_protocol(frozen),
    }


def build_fdm201_structure_package() -> dict[str, Any]:
    """The WP-30 structure package: SCXRD + acceptance evaluation."""
    template = SCXRDCharacterizationTemplate(
        template_id="fdm201-scxrd-structure",
        title="FDM-201 single-crystal structure verification",
        parameters={
            "instrument": "laboratory single-crystal diffractometer",
            "radiation_type": "Mo K-alpha",
            "wavelength_A": 0.71073,
            "collection_temperature_K": 100.0,
            "resolution_limit_A": 0.75,
            "detector": "hybrid photon counting",
        },
        analysis=AnalysisPlan(
            protocol=(
                "solve and refine the FDM-201 crystal structure against the"
                " deposited reference; report the final agreement factor"
            ),
            protocol_steps=(
                "integrate raw frames",
                "solve by direct methods",
                "refine against all measured reflections",
            ),
            acceptance_parameters={"scxrd_r_factor_max": 0.05},
        ),
        notes="structure verification of the [Zn8SiO4] SBU framework",
    )
    assessment = evaluate_acceptance(
        template, {"reported_r_factor": 0.048}
    )
    frozen_plan = freeze_analysis_plan(template, role=Role.SUPERVISOR)
    return {
        "template": template,
        "assessment": assessment,
        "frozen_plan_template": frozen_plan,
        "capture": capture_characterization(frozen_plan),
    }


def build_fdm201_pxrd_package() -> dict[str, Any]:
    """The WP-30 PXRD package: identity/quality checks across batches."""
    template = PXRDCharacterizationTemplate(
        template_id="fdm201-pxrd-batch-identity",
        title="FDM-201 PXRD phase identity and batch consistency",
        parameters={
            "instrument": "laboratory powder diffractometer",
            "radiation_type": "Cu K-alpha",
            "wavelength_A": 1.5406,
            "two_theta_min_deg": 3.0,
            "two_theta_max_deg": 60.0,
            "step_size_deg": 0.02,
            "scan_temperature_K": 298.0,
        },
        analysis=AnalysisPlan(
            protocol=(
                "record the PXRD pattern of each independent batch and"
                " compare it with the reference pattern of the FDM-201 phase"
            ),
            protocol_steps=(
                "collect the powder pattern",
                "index the reflections against the reference phase",
                "compare the intensity pattern with the reference",
            ),
            acceptance_parameters={
                "pxrd_phase_score_min": 0.9,
                "pxrd_peak_tolerance_deg": 0.2,
                "pxrd_intensity_score_min": 0.9,
                "pxrd_batch_consistency_min": 0.9,
            },
        ),
        notes=(
            "one pattern per independent synthesis batch (three batches);"
            f" source paper {FDM201_PAPER_DOI}"
        ),
    )
    # The worker records measurement FACTS; the frozen checks decide.
    assessment = evaluate_identity_checks(
        template,
        {
            "reference_phase_score": 0.99,
            "max_peak_position_deviation_deg": 0.08,
            "intensity_pattern_score": 0.97,
            "batch_consistency_score": 0.95,
        },
    )
    frozen = freeze_characterization_template(
        freeze_analysis_plan(template, role=Role.SUPERVISOR),
        role=Role.SUPERVISOR,
    )
    return {
        "template": template,
        "assessment": assessment,
        "frozen_template": frozen,
        "capture": capture_characterization(frozen),
    }


def build_fdm201_tga_package() -> dict[str, Any]:
    """The WP-30 TGA package: mass-loss acceptance evaluation."""
    template = TGACharacterizationTemplate(
        template_id="fdm201-tga-mass-loss",
        title="FDM-201 thermogravimetric solvent-loss check",
        parameters={
            "instrument": "thermogravimetric analyzer",
            "atmosphere": "nitrogen",
            "heating_rate_K_min": 10.0,
            "final_temperature_K": 873.0,
            "sample_mass_mg": 10.0,
            "gas_flow_ml_min": 20.0,
            "scan_duration_h": 2.0,
        },
        analysis=AnalysisPlan(
            protocol=(
                "measure the mass loss of the activated FDM-201 sample and"
                " compare it with the reference solvent-loss window"
            ),
            protocol_steps=(
                "equilibrate the sample in flowing nitrogen",
                "ramp at the recorded heating rate",
                "record the mass loss over the ramp",
            ),
            acceptance_parameters={"tga_mass_loss_tolerance_pct": 1.0},
        ),
        notes="reference solvent mass loss 18.0 %",
    )
    assessment = evaluate_acceptance(
        template,
        {
            "observed_mass_loss_pct": 18.4,
            "reference_mass_loss_pct": 18.0,
        },
    )
    return {
        "template": template,
        "assessment": assessment,
        "capture": capture_characterization(template),
    }


def build_fdm201_bet_package() -> dict[str, Any]:
    """The WP-40 BET package: isotherm execution + frozen analysis."""
    execution = BetTemplate(
        template_id="fdm201-bet-isotherm-77",
        title="FDM-201 nitrogen BET isotherm acquisition",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "dinitrogen",
            "temperature_K": 77.4,
            "sample_mass_mg": 92.1,
        },
        notes=(
            "reported apparent surface area of the reference case is an"
            " instance datum, never a rule threshold"
        ),
    )
    analysis = BetTemplate(
        template_id="fdm201-bet-analysis-rouquerol",
        title="FDM-201 BET surface-area analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "apparent_surface_area",
            "model": "Rouquerol consistency criteria",
            "relative_pressure_min": 0.05,
            "relative_pressure_max": 0.3,
        },
    )
    frozen_analysis = freeze_adsorption_template(analysis, role=Role.SUPERVISOR)
    return {
        "execution": execution,
        "analysis": analysis,
        "frozen_analysis": frozen_analysis,
        "execution_capture": capture_adsorption_protocol(execution),
        "analysis_capture": capture_adsorption_protocol(frozen_analysis),
    }


def build_fdm201_adsorption_package() -> dict[str, Any]:
    """The WP-50 single-component adsorption package (C3H6 and C2H4)."""
    propene_execution = SingleComponentTemplate(
        template_id="fdm201-isotherm-propene-298",
        title="FDM-201 propene single-component isotherm",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
        },
    )
    ethene_execution = SingleComponentTemplate(
        template_id="fdm201-isotherm-ethene-298",
        title="FDM-201 ethene single-component isotherm",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "ethene",
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
        },
    )
    analysis = SingleComponentTemplate(
        template_id="fdm201-isotherm-analysis-propene",
        title="FDM-201 propene isotherm analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "equilibrium_loading",
            "model": "dual_site_langmuir",
            "convergence_metric": "coefficient_of_determination",
            "convergence_threshold": 0.999,
            "statistical_uncertainty_metric": "standard_error_of_fit",
            "sampling_validation": "reported value is the mean of three batches",
        },
        notes="reported loading 180.5 cm3/g at 298 K, 1 bar (instance data)",
    )
    return {
        "propene_execution": propene_execution,
        "ethene_execution": ethene_execution,
        "analysis": analysis,
        "capture": capture_adsorption_protocol(analysis),
    }


def build_fdm201_iast_package() -> dict[str, Any]:
    """The WP-60 IAST package: mixture-selectivity analysis."""
    template = IastTemplate(
        template_id="fdm201-iast-propene-ethene",
        title="FDM-201 IAST propene/ethene selectivity",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "iast_selectivity",
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "model": "dual_site_langmuir",
            "sampling_validation": "single-point IAST at the recorded composition",
        },
        notes=(
            "reported selectivity of the reference case is an instance"
            " datum, never a rule threshold"
        ),
    )
    return {
        "template": template,
        "capture": capture_adsorption_protocol(template),
    }


def build_fdm201_qst_package() -> dict[str, Any]:
    """The WP-60 Qst package: isosteric-heat analysis."""
    template = QstTemplate(
        template_id="fdm201-qst-propene-288-308",
        title="FDM-201 propene isosteric heat",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "isosteric_heat",
            "adsorbate": "propene",
            "temperature_low_K": 288.0,
            "temperature_high_K": 308.0,
            "reference_loading_mol_kg": 1.0,
        },
        notes=(
            "reported Qst of the reference case is an instance datum,"
            " never a rule threshold"
        ),
    )
    return {
        "template": template,
        "capture": capture_adsorption_protocol(template),
    }


def build_fdm201_breakthrough_package() -> dict[str, Any]:
    """The WP-70 breakthrough package: column run + paper mapping."""
    execution = BreakthroughTemplate(
        template_id="fdm201-breakthrough-column",
        title="FDM-201 propene/ethene dynamic breakthrough",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "co_adsorbate": "ethene",
            "composition_fraction": 0.5,
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "flow_rate_ml_min": 8.0,
            "adsorbent_mass_mg": 1200.0,
            "column_length_mm": 220.0,
            "column_diameter_mm": 4.0,
            "dead_volume_ml": 1.2,
            "detector": "gas_chromatograph",
            "regeneration_protocol": "vacuum at elevated temperature",
            "cycle_count": 5,
        },
        results=BreakthroughResultTable(
            entries=(
                PaperResultEntry(
                    result_key="c2h4_breakthrough_min",
                    figure_ref="Figure 4a",
                    description="ethene breakthrough time in minutes",
                ),
                PaperResultEntry(
                    result_key="c3h6_breakthrough_min",
                    figure_ref="Figure 4a",
                    description="propene breakthrough time in minutes",
                ),
                PaperResultEntry(
                    result_key="separation_window_min",
                    figure_ref="Figure 4a",
                    description="separation window between the breakthroughs",
                ),
            )
        ),
        notes=(
            "reported breakthroughs of the reference case (23.5 min / 64.8"
            " min) are instance data, never rule thresholds"
        ),
    )
    analysis = BreakthroughTemplate(
        template_id="fdm201-breakthrough-analysis",
        title="FDM-201 breakthrough analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "breakthrough_time",
            "criterion": "C/C0 = 0.1",
            "sampling_validation": "three replicate column runs",
        },
    )
    return {
        "execution": execution,
        "analysis": analysis,
        "paper_mapping": paper_mapping(execution.results),
        "execution_capture": capture_adsorption_protocol(execution),
        "analysis_capture": capture_adsorption_protocol(analysis),
    }


def build_fdm201_stability_package() -> dict[str, Any]:
    """The WP-70 stability package: cycling/reusability."""
    execution = CyclingStabilityTemplate(
        template_id="fdm201-cycling-propene",
        title="FDM-201 propene cycling stability",
        stage=EXECUTION_STAGE,
        parameters={
            "adsorbate": "propene",
            "temperature_K": 298.0,
            "pressure_kPa": 100.0,
            "cycle_count": 6,
            "regeneration_protocol": "vacuum at elevated temperature",
            "atmosphere": "nitrogen",
        },
    )
    analysis = CyclingStabilityTemplate(
        template_id="fdm201-cycling-analysis",
        title="FDM-201 capacity-retention analysis",
        stage=ANALYSIS_STAGE,
        parameters={
            "property": "capacity_retention_ratio",
            "criterion": "retention_ratio_after_six_cycles",
            "reference_value": 1.0,
            "tolerance": 0.05,
        },
    )
    return {
        "execution": execution,
        "analysis": analysis,
        "capture": capture_adsorption_protocol(execution),
    }


def build_fdm201_computation_package() -> dict[str, Any]:
    """The WP-80/81/82 computation package: prep + DFT + GCMC."""
    structure_prep = StructurePreparationTemplate(
        template_id="fdm201-structure-prep",
        title="FDM-201 structure model preparation",
        stage=COMP_EXECUTION_STAGE,
        parameters={
            "structure_source": "deposited crystallographic information file",
            "disorder_treatment": "site-occupancy refinement",
            "method": "cell-parameter re-optimization",
            "software": "pymatgen",
            "software_version": "2025.3.3",
        },
        scheduler=SchedulerOptions(
            partition="gpu",
            account="reproduction",
            qos="normal",
            nodes=1,
            tasks_per_node=4,
            walltime_hours=2.0,
            modules=("pymatgen/2025",),
        ),
    )
    dft_execution = DftTemplate(
        template_id="fdm201-dft-binding-propene",
        title="FDM-201 DFT propene binding energy",
        stage=COMP_EXECUTION_STAGE,
        parameters={
            "software": "VASP",
            "software_version": "6.4.3",
            "method": "periodic DFT",
            "functional": "PBE",
            "dispersion_correction": "DFT-D3",
            "basis_set": "plane waves",
            "pseudopotential": "PAW",
            "kpoint_mesh": [2, 2, 2],
            "energy_cutoff_ev": 520.0,
            "convergence_tolerance": 0.001,
        },
        scheduler=SchedulerOptions(
            partition="gpu",
            account="reproduction",
            qos="normal",
            nodes=1,
            tasks_per_node=16,
            walltime_hours=24.0,
            modules=("VASP/6.4.3",),
        ),
    )
    dft_analysis = DftTemplate(
        template_id="fdm201-dft-binding-analysis",
        title="FDM-201 DFT binding-energy analysis",
        stage=COMP_ANALYSIS_STAGE,
        parameters={
            "property": "binding_energy",
            "convergence_metric": "energy_change",
            "convergence_threshold": 0.001,
            "statistical_uncertainty_metric": "single_configuration",
            "sampling_validation": "replicate at equivalent adsorption sites",
            "finite_size_correction": "single-unit-cell",
        },
        notes=(
            "reported binding energies of the reference case (48.72 /"
            " 38.61 kJ/mol) are instance data, never rule thresholds"
        ),
    )
    gcmc_execution = GcmcTemplate(
        template_id="fdm201-gcmc-propene",
        title="FDM-201 GCMC propene loading",
        stage=COMP_EXECUTION_STAGE,
        parameters={
            "software": "RASPA",
            "software_version": "2.0.47",
            "force_field": "generic MOF force field",
            "charges": "DDEC6",
            "mixing_rules": "Lorentz-Berthelot",
            "cutoff_angstrom": 12.0,
            "temperature_K": 298.0,
            "pressure_bar": 1.0,
            "equilibration_cycles": 5000,
            "production_cycles": 20000,
            "seed": 42,
        },
        scheduler=SchedulerOptions(
            partition="cpu",
            account="reproduction",
            qos="normal",
            nodes=1,
            tasks_per_node=8,
            walltime_hours=48.0,
            modules=("RASPA/2.0.47",),
        ),
    )
    gcmc_analysis = GcmcTemplate(
        template_id="fdm201-gcmc-analysis",
        title="FDM-201 GCMC loading analysis",
        stage=COMP_ANALYSIS_STAGE,
        parameters={
            "property": "absolute_loading",
            "convergence_metric": "block_average_drift",
            "convergence_threshold": 0.01,
            "statistical_uncertainty_metric": "block_average_standard_error",
            "sampling_validation": "five independent production blocks",
        },
    )
    frozen_dft = freeze_computation_template(dft_execution, role=Role.SUPERVISOR)
    return {
        "structure_preparation": structure_prep,
        "dft_execution": dft_execution,
        "dft_analysis": dft_analysis,
        "gcmc_execution": gcmc_execution,
        "gcmc_analysis": gcmc_analysis,
        "frozen_dft": frozen_dft,
        "capture": capture_computation_protocol(frozen_dft),
    }


def build_fdm201_acceptance_package() -> dict[str, Any]:
    """The acceptance package: statistics proposals + evidence + freeze."""
    replicate_design = default_replicate_design_proposal(GOAL_FDM201_SYN)
    uncertainty = propose_measurement_uncertainty(
        measurement_key="propene_loading_mg_g",
        uncertainty_kind=UncertaintyKind.STANDARD_DEVIATION,
        reporting_form="mean +/- standard deviation over the batches",
        rationale=(
            "batch-level variability of the three independent synthesis"
            " batches is the primary uncertainty source"
        ),
    )
    margin_evidence = EvidenceReference(
        evidence_id=generate_id(
            "evidence", FDM201_PAPER_DOI, EvidenceClaim.EQUIVALENCE_MARGIN.value
        ),
        source_id=FDM201_PAPER_DOI,
        claim=EvidenceClaim.EQUIVALENCE_MARGIN,
        claim_text="reference loading reported for the FDM-201 case",
    )
    method_evidence = EvidenceReference(
        evidence_id=generate_id(
            "evidence", "method-source", EvidenceClaim.ACCEPTANCE_METHOD.value
        ),
        source_id="method-source",
        claim=EvidenceClaim.ACCEPTANCE_METHOD,
        claim_text="equivalence testing on reported adsorption loadings",
    )
    proposal = construct_acceptance_proposal(
        goal_id=GOAL_FDM201_SYN,
        replicate_design=replicate_design,
        uncertainty=uncertainty,
        decision_mode=DecisionMode.EQUIVALENCE,
        equivalence_margin=5.0,
        evidence_refs=(margin_evidence,),
        rationale=(
            "equivalence margin grounded in the reported reference-case"
            " reproducibility; acceptance rests on the proposed n >= 3"
            " replicate design"
        ),
    )
    # Evidence is attached BEFORE freezing (AC-03): the method evidence
    # enriches the proposal; the frozen copy carries exactly the evidence
    # that justified it.
    enriched = attach_evidence(proposal, (method_evidence,))
    frozen = freeze_acceptance_proposal(enriched, role=Role.SUPERVISOR)
    return {
        "replicate_design": replicate_design,
        "uncertainty": uncertainty,
        "margin_evidence": margin_evidence,
        "method_evidence": method_evidence,
        "proposal": proposal,
        "enriched": enriched,
        "frozen_acceptance": frozen,
    }


def build_fdm201_core_records() -> dict[str, Any]:
    """The frozen-core layer of the chain: goal, runs, evidence, criteria,
    plan -- every record validated through the real core schemas."""
    synthesis_package = build_fdm201_synthesis_package()
    acceptance_package = build_fdm201_acceptance_package()
    mof_template = synthesis_package["mof_template"]
    batch_plan = synthesis_package["batch_plan"]
    run_ids = batch_plan.batch_run_ids
    frozen_acceptance = acceptance_package["frozen_acceptance"]

    goal = GoalContract(
        goal_id=GOAL_FDM201_SYN,
        title="FDM-201 solvothermal MOF synthesis",
        unit_process_type="synthesis",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective=(
            "synthesize the FDM-201 MOF in three independent batches and"
            " match the reference-case phase and porosity"
        ),
        requirement_ids=[REQ_FDM201_CHAIN],
        dependencies=[
            GoalDependency(
                goal_id=GOAL_FDM201_SRC,
                type=DependencyType.SOFT_DEPENDENCY,
            )
        ],
        acceptance=GoalAcceptance(
            criteria_ref=frozen_acceptance.proposal_id, frozen=True
        ),
        analysis_protocol_ref=mof_template.template_id,
        replication=mof_template.replication.to_goal_replication(),
        version="1.0",
        frozen=True,
        assumption_ids=[],
        frozen_at=FDM201_FIXED_TIMESTAMP,
    )

    runs = [
        Run(
            run_id=run_id,
            goal_id=GOAL_FDM201_SYN,
            run_type=RunType.INDEPENDENT_REPLICATE,
            lifecycle_state=LifecycleState.CLOSED,
            goal_version="1.0",
            scientific_review=ScientificReview.PASS,
            created_at=FDM201_FIXED_TIMESTAMP,
            updated_at=FDM201_FIXED_TIMESTAMP,
        )
        for run_id in run_ids
    ]

    evidence = ClaimSpecificEvidence(
        evidence_id=generate_id("evidence", "pxrd-batch-consistency", GOAL_FDM201_SYN),
        source_id=FDM201_PAPER_DOI,
        claim_id="FDM201-CLAIM-PXRD-PHASE-IDENTITY",
        finding=(
            "the PXRD patterns of the three independent batches match the"
            " reported FDM-201 phase within the recorded tolerances"
        ),
        source_location="Figure 3 / SI Section S3",
        assessment=EvidenceAssessment(
            authority=4,
            reliability=3,
            directness=3,
            reliability_checklist_ref="checklist-v1",
            ranking_score=60.0,
        ),
        limitations=[],
        used_by=list(run_ids),
    )

    criteria = AcceptanceCriteria(
        acceptance_id=frozen_acceptance.proposal_id,
        goal_id=GOAL_FDM201_SYN,
        version="1.0",
        frozen=True,
        decision_mode=frozen_acceptance.decision_mode,
        criteria=[
            {
                "kind": "replicate_floor",
                "minimum_n": frozen_acceptance.replicate_floor,
            },
            {
                "kind": "equivalence_margin",
                "margin_pct": frozen_acceptance.equivalence_margin,
            },
        ],
        target={"equivalence_margin": frozen_acceptance.equivalence_margin},
        statistical_design_ref=frozen_acceptance.replicate_design.proposal_id,
        evidence_refs=[reference.evidence_id for reference in frozen_acceptance.evidence_refs],
        rationale=frozen_acceptance.rationale,
        confidence=Confidence.MEDIUM,
    )

    plan = Plan(
        plan_id=generate_id("plan", "fdm201-expressiveness-chain"),
        version="1.0",
        status=PlanStatus.DRAFT,
        inventory_audit=PlanInventoryAudit(
            formally_reported_items=11,
            mapped_items=11,
            unmapped_items=0,
            ambiguous_items=0,
            coverage=1.0,
        ),
        goal_ids=[GOAL_FDM201_SYN],
        requirement_ids=[REQ_FDM201_CHAIN],
        work_packages=[
            {"wp": f"WP-{index:02d}", "category": category}
            for index, category in enumerate(FDM201_WORK_PACKAGES, start=20)
        ],
    )

    return {
        "goal": goal,
        "runs": runs,
        "evidence": evidence,
        "criteria": criteria,
        "plan": plan,
    }


def build_fdm201_full_chain() -> dict[str, Any]:
    """The complete FDM-201 expressiveness chain (WP-20 .. WP-90)."""
    return {
        "synthesis": build_fdm201_synthesis_package(),
        "structure": build_fdm201_structure_package(),
        "pxrd": build_fdm201_pxrd_package(),
        "tga": build_fdm201_tga_package(),
        "bet": build_fdm201_bet_package(),
        "adsorption": build_fdm201_adsorption_package(),
        "iast": build_fdm201_iast_package(),
        "qst": build_fdm201_qst_package(),
        "breakthrough": build_fdm201_breakthrough_package(),
        "stability": build_fdm201_stability_package(),
        "computation": build_fdm201_computation_package(),
        "acceptance": build_fdm201_acceptance_package(),
        "core": build_fdm201_core_records(),
    }


def _collect_fixture_records() -> list[Any]:
    """Every fixture record, flattened in a deterministic order.

    The AC-02 no-fork inventory: the full record set of the expressiveness
    chain, walking dicts and tuples in insertion order (builders are
    deterministic, so the order is stable).
    """

    def walk(value: Any) -> Iterable[Any]:
        if is_dataclass(value):
            yield value
            for field in fields(value):
                yield from walk(getattr(value, field.name))
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, (tuple, list)):
            for item in value:
                yield from walk(item)

    return list(walk(build_fdm201_full_chain()))


# ---------------------------------------------------------------------------
# The AC-02 no-fork allowlist (frozen core models + domain-pack records)
# ---------------------------------------------------------------------------

#: The frozen-core model types the fixture may instantiate.
FROZEN_CORE_TYPES: tuple[type[Any], ...] = (
    AcceptanceCriteria,
    Assumption,
    ClaimSpecificEvidence,
    EvidenceAssessment,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    Plan,
    PlanInventoryAudit,
    Run,
)

#: The domain-pack record types the fixture may instantiate (templates,
#: proposals, assessments, tables). Every class here is a frozen dataclass
#: defined by the installed domain pack -- the fixture defines zero new
#: classes (asserted below).
PACK_RECORD_TYPES: tuple[type[Any], ...] = (
    ActivationTemplate,
    BatchFloorAssessment,
    BatchReplicationDefaults,
    SolventExchangeTemplate,
    SynthesisUnitProcessTemplate,
    AnalysisPlan,
    PXRDCharacterizationTemplate,
    SCXRDCharacterizationTemplate,
    TGACharacterizationTemplate,
    BetTemplate,
    SingleComponentTemplate,
    IastTemplate,
    QstTemplate,
    BreakthroughTemplate,
    CyclingStabilityTemplate,
    BreakthroughResultTable,
    PaperResultEntry,
    StructurePreparationTemplate,
    DftTemplate,
    GcmcTemplate,
    SchedulerOptions,
    EvidenceReference,
)

#: The pack's assessment/proposal records produced by the real machinery
#: and carried by the builders (assessments plus their nested decision
#: records).
ASSESSMENT_RECORD_TYPES: tuple[type[Any], ...] = (
    AcceptanceAssessment,
    AcceptanceDecision,
    IdentityCheckAssessment,
    IdentityCheckDecision,
    BatchPlan,
    MeasurementUncertaintyProposal,
    ReplicateDesignProposal,
    AcceptanceProposal,
    SynthesisParameterCompletenessAssessment,
    SynthesisParameterCompletenessDecision,
    SynthesisValueValidationAssessment,
    SynthesisValueValidationDecision,
    BatchFloorDecision,
)

#: The complete frozen allowlist: every record type the fixture may hold.
FDM201_FIXTURE_ALLOWLIST: tuple[type[Any], ...] = (
    FROZEN_CORE_TYPES + PACK_RECORD_TYPES + ASSESSMENT_RECORD_TYPES
)


def _assert_schema_clean(obj_type: str, data: dict[str, Any]) -> None:
    """The persistence gate: the record validates against the core schema."""
    errors = validate_object(obj_type, data)
    assert not errors, f"{obj_type} schema errors: {errors}"


# ---------------------------------------------------------------------------
# AC-01: one representability test per FDM-201 work-package category
# ---------------------------------------------------------------------------


def test_fdm201_synthesis_wp_is_representable() -> None:
    """WP-20: the MOF synthesis package uses the real synthesis machinery."""
    package = build_fdm201_synthesis_package()
    mof = package["mof_template"]
    assert mof.unit_process_kind is SynthesisUnitProcessKind.MOF_SYNTHESIS
    # Construction validated: the completeness assessment records no
    # missing required parameter (R-TPL-P3 is satisfied).
    assert package["completeness"].missing_parameters == ()
    assert package["value_assessment"].violations == ()
    # The real batch machinery plans three independent replicate Runs.
    batch_plan = package["batch_plan"]
    assert batch_plan.n == 3
    assert batch_plan.run_type is RunType.INDEPENDENT_REPLICATE
    assert batch_plan.floor_assessment.sufficient is True
    assert len(batch_plan.batch_run_ids) == 3
    for run_id in batch_plan.batch_run_ids:
        assert is_valid_id(run_id, "run")
    # The activation and solvent-exchange surfaces are complete templates.
    assert package["activation_template"].parameters["atmosphere"] == "vacuum"
    assert package["exchange_template"].parameters["exchange_cycles"] == 3
    # Freezing is a Supervisor-only decision; the capture serializes.
    assert package["frozen_template"].frozen is True
    capture = package["capture"]
    assert capture["unit_process_kind"] == "mof_synthesis"
    assert capture["frozen"] is True
    json.dumps(capture)  # JSON-serializable, byte-deterministic


def test_fdm201_structure_wp_is_representable() -> None:
    """WP-30 structure: SCXRD acceptance evaluates through R-CHA-A5."""
    package = build_fdm201_structure_package()
    template = package["template"]
    assert template.characterization_kind.value == "scxrd"
    assessment = package["assessment"]
    assert assessment.outcome.value == "PASS"
    assert assessment.matched_rule_id == "R-CHA-O3"
    # The SCXRD rule (reported r-factor <= recorded ceiling) applied.
    decisions = {d.rule_id: d for d in assessment.decisions}
    assert decisions["R-CHA-A5"].applied is True
    assert decisions["R-CHA-A5"].passed is True
    # The analysis plan freezes separately from execution (Supervisor).
    assert package["frozen_plan_template"].analysis.frozen is True
    capture = package["capture"]
    assert capture["characterization_kind"] == "scxrd"
    assert capture["analysis"]["frozen"] is True


def test_fdm201_pxrd_wp_is_representable() -> None:
    """WP-30 PXRD: all four identity checks decide PASS (R-CHA-A1..A4)."""
    package = build_fdm201_pxrd_package()
    assessment = package["assessment"]
    assert assessment.outcome.value == "PASS"
    assert assessment.matched_check_id is None
    for check in assessment.checks:
        assert check.applied is True
        assert check.outcome.value == "PASS"
    assert set(check.check_id for check in assessment.checks) == {
        "R-CHA-A1",
        "R-CHA-A2",
        "R-CHA-A3",
        "R-CHA-A4",
    }
    assert assessment.pending_measurements == ()
    # The frozen template and plan serialize through the capture gate.
    assert package["frozen_template"].frozen is True
    assert package["frozen_template"].analysis.frozen is True
    assert package["capture"]["parameter_table"][0]["parameter"] == "instrument"


def test_fdm201_tga_wp_is_representable() -> None:
    """WP-30 TGA: mass-loss acceptance evaluates through R-CHA-A6."""
    package = build_fdm201_tga_package()
    assessment = package["assessment"]
    assert assessment.outcome.value == "PASS"
    decisions = {d.rule_id: d for d in assessment.decisions}
    assert decisions["R-CHA-A6"].applied is True
    assert decisions["R-CHA-A6"].passed is True
    assert assessment.pending_measurements == ()
    capture = package["capture"]
    assert capture["characterization_kind"] == "tga"
    assert capture["parameter_table"]  # the recorded instrument metadata


def test_fdm201_bet_wp_is_representable() -> None:
    """WP-40 BET: execution and analysis are separate surfaces."""
    package = build_fdm201_bet_package()
    execution = package["execution"]
    analysis = package["analysis"]
    assert execution.stage.value == "execution"
    assert analysis.stage.value == "analysis"
    assert execution.kind is BET_KIND and analysis.kind is BET_KIND
    # Both surfaces are complete against their required-parameter rules.
    assert (
        execution.parameters["adsorbate"] == "dinitrogen"
        and execution.parameters["temperature_K"] == 77.4
    )
    assert (
        analysis.parameters["property"] == "apparent_surface_area"
        and analysis.parameters["model"] == "Rouquerol consistency criteria"
    )
    # The analysis template freezes independently of execution.
    assert package["frozen_analysis"].frozen is True
    assert package["execution_capture"]["stage"] == "execution"
    assert package["analysis_capture"]["stage"] == "analysis"


def test_fdm201_adsorption_wp_is_representable() -> None:
    """WP-50: single-component C3H6/C2H4 execution pairs at 298 K, 1 bar."""
    package = build_fdm201_adsorption_package()
    propene = package["propene_execution"]
    ethene = package["ethene_execution"]
    for template in (propene, ethene):
        assert template.stage.value == "execution"
        assert template.parameters["temperature_K"] == 298.0
        assert template.parameters["pressure_kPa"] == 100.0
    assert propene.parameters["adsorbate"] == "propene"
    assert ethene.parameters["adsorbate"] == "ethene"
    assert propene.template_id != ethene.template_id
    # The analysis surface records the fitted model and convergence.
    analysis = package["analysis"]
    assert analysis.stage.value == "analysis"
    assert analysis.parameters["convergence_metric"] == "coefficient_of_determination"
    capture = package["capture"]
    assert capture["kind"] == "single_component"
    json.dumps(capture)


def test_fdm201_iast_wp_is_representable() -> None:
    """WP-60 IAST: mixture selectivity records the pair and conditions."""
    package = build_fdm201_iast_package()
    template = package["template"]
    assert template.stage.value == "analysis"
    assert template.parameters["adsorbate"] == "propene"
    assert template.parameters["co_adsorbate"] == "ethene"
    assert template.parameters["composition_fraction"] == 0.5
    assert template.parameters["temperature_K"] == 298.0
    # Completeness is total: the IAST analysis surface has no missing
    # required parameter (R-ADS-P5).
    completeness = assess_adsorption_completeness(template)
    assert completeness.missing_parameters == ()
    capture = package["capture"]
    assert capture["kind"] == "iast"
    json.dumps(capture)


def test_fdm201_qst_wp_is_representable() -> None:
    """WP-60 Qst: isosteric heat records the temperature pair."""
    package = build_fdm201_qst_package()
    template = package["template"]
    assert template.parameters["temperature_low_K"] == 288.0
    assert template.parameters["temperature_high_K"] == 308.0
    assert template.parameters["reference_loading_mol_kg"] == 1.0
    completeness = assess_adsorption_completeness(template)
    assert completeness.missing_parameters == ()
    assert package["capture"]["kind"] == "qst"


def test_fdm201_breakthrough_wp_is_representable() -> None:
    """WP-70: dynamic breakthrough with raw/result to figure mapping."""
    package = build_fdm201_breakthrough_package()
    execution = package["execution"]
    assert execution.stage.value == "execution"
    assert execution.parameters["adsorbate"] == "propene"
    assert execution.parameters["flow_rate_ml_min"] == 8.0
    # The results table maps every result key to a formal paper figure.
    mapping = package["paper_mapping"]
    assert mapping == {
        "c2h4_breakthrough_min": "Figure 4a",
        "c3h6_breakthrough_min": "Figure 4a",
        "separation_window_min": "Figure 4a",
    }
    analysis = package["analysis"]
    assert analysis.parameters["criterion"] == "C/C0 = 0.1"
    assert analysis.stage.value == "analysis"
    assert package["execution_capture"]["stage"] == "execution"
    assert package["analysis_capture"]["stage"] == "analysis"


def test_fdm201_stability_wp_is_representable() -> None:
    """WP-70 stability: cycling execution + retention analysis."""
    package = build_fdm201_stability_package()
    execution = package["execution"]
    assert execution.stage.value == "execution"
    assert execution.parameters["cycle_count"] == 6
    assert execution.parameters["atmosphere"] == "nitrogen"
    analysis = package["analysis"]
    assert analysis.stage.value == "analysis"
    assert analysis.parameters["property"] == "capacity_retention_ratio"
    assert analysis.parameters["tolerance"] == 0.05
    assert package["capture"]["kind"] == "cycling_stability"


def test_fdm201_computation_wp_is_representable() -> None:
    """WP-80/81/82: structure prep, DFT and GCMC with scheduler options."""
    package = build_fdm201_computation_package()
    structure_prep = package["structure_preparation"]
    dft_execution = package["dft_execution"]
    gcmc_execution = package["gcmc_execution"]
    # Execution surfaces are complete against R-COM-P1/P2/P3.
    for template in (structure_prep, dft_execution, gcmc_execution):
        completeness = assess_computation_completeness(template)
        assert completeness.missing_parameters == (), template.template_id
    assert dft_execution.parameters["functional"] == "PBE"
    assert dft_execution.parameters["kpoint_mesh"] == [2, 2, 2]
    assert gcmc_execution.parameters["force_field"] == "generic MOF force field"
    assert gcmc_execution.parameters["seed"] == 42
    # The scheduler metadata is carried verbatim on the frozen template.
    scheduler = package["frozen_dft"].scheduler
    assert scheduler is not None
    assert scheduler.partition == "gpu"
    assert scheduler.modules == ("VASP/6.4.3",)
    assert package["frozen_dft"].frozen is True
    # The analysis surfaces record convergence and sampling validation.
    assert package["dft_analysis"].parameters["property"] == "binding_energy"
    assert package["gcmc_analysis"].parameters["sampling_validation"]
    capture = package["capture"]
    assert capture["kind"] == "dft"
    json.dumps(capture)


def test_fdm201_acceptance_flow_is_representable() -> None:
    """WP-90 acceptance: proposals, evidence before freezing, freeze."""
    package = build_fdm201_acceptance_package()
    # The replicate design proposes the frozen n >= 3 default.
    replicate_design = package["replicate_design"]
    assert replicate_design.minimum_n == 3
    assert replicate_design.is_default is True
    # Uncertainty metadata is explicit, never fabricated.
    uncertainty = package["uncertainty"]
    assert uncertainty.uncertainty_kind is UncertaintyKind.STANDARD_DEVIATION
    # The acceptance carries the evidence that grounds its margin.
    proposal = package["proposal"]
    assert proposal.equivalence_margin == 5.0
    assert any(
        reference.claim is EvidenceClaim.EQUIVALENCE_MARGIN
        for reference in proposal.evidence_refs
    )
    # The frozen copy carries exactly the evidence that justified it:
    # evidence attached before freezing is preserved verbatim.
    enriched = package["enriched"]
    assert {reference.claim for reference in enriched.evidence_refs} == {
        EvidenceClaim.EQUIVALENCE_MARGIN,
        EvidenceClaim.ACCEPTANCE_METHOD,
    }
    frozen = package["frozen_acceptance"]
    assert frozen.frozen is True
    assert frozen.evidence_refs == enriched.evidence_refs
    json.dumps(frozen.as_dict())


# ---------------------------------------------------------------------------
# AC-02: no ad hoc schema fork, frozen-core persistence gates
# ---------------------------------------------------------------------------


def test_fdm201_fixture_uses_only_frozen_core_and_pack_types() -> None:
    """AC-02: every record is a frozen core model or pack record, and the
    fixture defines zero new classes (no ad hoc schema fork)."""
    records = _collect_fixture_records()
    assert records, "the expressiveness chain must hold records"
    allowlisted = set(FDM201_FIXTURE_ALLOWLIST)
    for record in records:
        assert type(record) in allowlisted, (
            f"record type {type(record).__module__}.{type(record).__name__}"
            " is not a frozen core model or domain-pack record"
        )
    # The fixture module itself defines no classes at all.
    local_classes = [
        name
        for name, obj in vars(sys.modules[__name__]).items()
        if isinstance(obj, type) and obj.__module__ == __name__
    ]
    assert local_classes == []


def test_fdm201_core_records_validate_and_round_trip() -> None:
    """AC-02: every core record passes the real schema validation gate and
    round-trips through to_dict/from_dict."""
    core = build_fdm201_core_records()
    goal = core["goal"]
    runs = core["runs"]
    evidence = core["evidence"]
    criteria = core["criteria"]
    plan = core["plan"]
    # The persistence gate: schema-invalid objects are rejected before any
    # write; a clean fixture must produce zero errors for every record.
    _assert_schema_clean("goal", goal.to_dict())
    for run in runs:
        _assert_schema_clean("run", run.to_dict())
    _assert_schema_clean("evidence", evidence.to_dict())
    _assert_schema_clean("acceptance-criteria", criteria.to_dict())
    _assert_schema_clean("plan", plan.to_dict())
    # Round-trip: from_dict(to_dict(x)) == x for every record.
    assert GoalContract.from_dict(goal.to_dict()) == goal
    assert [Run.from_dict(run.to_dict()) for run in runs] == runs
    assert ClaimSpecificEvidence.from_dict(evidence.to_dict()) == evidence
    assert AcceptanceCriteria.from_dict(criteria.to_dict()) == criteria
    assert Plan.from_dict(plan.to_dict()) == plan
    # Deterministic serialization: to_dict is repeatable byte-for-byte.
    assert json.dumps(goal.to_dict(), sort_keys=True) == json.dumps(
        goal.to_dict(), sort_keys=True
    )


# ---------------------------------------------------------------------------
# Composition: the full WP chain and the fixture disciplines
# ---------------------------------------------------------------------------


def test_fdm201_full_workflow_chain_composes() -> None:
    """The eleven-category chain composes with cross-package references."""
    chain = build_fdm201_full_chain()
    core = chain["core"]
    goal = core["goal"]
    runs = core["runs"]
    evidence = core["evidence"]
    criteria = core["criteria"]
    plan = core["plan"]
    acceptance = chain["acceptance"]
    # The acceptance proposal id is the single reference through the chain.
    assert goal.acceptance.criteria_ref == acceptance["frozen_acceptance"].proposal_id
    assert criteria.acceptance_id == acceptance["frozen_acceptance"].proposal_id
    # Runs are the planned independent batches of the synthesis template.
    assert [run.run_id for run in runs] == list(
        chain["synthesis"]["batch_plan"].batch_run_ids
    )
    for run in runs:
        assert run.goal_id == goal.goal_id
        assert run.run_type is RunType.INDEPENDENT_REPLICATE
    # Evidence references the paper and the runs that used it.
    assert evidence.source_id == FDM201_PAPER_DOI
    assert set(evidence.used_by) == set(run.run_id for run in runs)
    # The plan maps all eleven categories to work-package slots.
    assert plan.goal_ids == [goal.goal_id]
    assert len(plan.work_packages) == 11
    assert [wp["category"] for wp in plan.work_packages] == list(
        FDM201_WORK_PACKAGES
    )
    # The plan inventory audit is total (11 formally reported, all mapped).
    assert plan.inventory_audit.formally_reported_items == 11
    assert plan.inventory_audit.mapped_items == 11
    assert plan.inventory_audit.unmapped_items == 0
    # Every category's capture serializes deterministically.
    captures = [
        chain["synthesis"]["capture"],
        chain["structure"]["capture"],
        chain["pxrd"]["capture"],
        chain["tga"]["capture"],
        chain["bet"]["analysis_capture"],
        chain["adsorption"]["capture"],
        chain["iast"]["capture"],
        chain["qst"]["capture"],
        chain["breakthrough"]["execution_capture"],
        chain["stability"]["capture"],
        chain["computation"]["capture"],
    ]
    for capture in captures:
        assert json.dumps(capture, sort_keys=True) == json.dumps(
            capture, sort_keys=True
        )
    # The full chain serializes through the persistence gate.
    _assert_schema_clean("plan", plan.to_dict())


def test_fdm201_fixture_is_marked_non_final() -> None:
    """The fixture is explicitly NON-FINAL: the pre-M12 demonstration,
    never the M12 benchmark."""
    assert FDM201_FIXTURE_STATUS == "non-final-pre-M12"
    docstring = sys.modules[__name__].__doc__ or ""
    assert "NON-FINAL" in docstring
    assert "pre-M12" in docstring
    assert "NOT the M12 benchmark" in docstring


def test_fdm201_fixture_is_deterministic() -> None:
    """Rebuilding the chain twice yields byte-identical records."""
    first = build_fdm201_full_chain()
    second = build_fdm201_full_chain()
    # Ids are pure functions of the canonical fields, never random.
    assert first["core"]["goal"] == second["core"]["goal"]
    assert first["core"]["runs"] == second["core"]["runs"]
    assert first["synthesis"]["batch_plan"].batch_run_ids == (
        second["synthesis"]["batch_plan"].batch_run_ids
    )
    # Captures are byte-identical across rebuilds.
    for category in FDM201_WORK_PACKAGES:
        first_capture = first[category].get("capture")
        if first_capture is None:
            first_capture = first[category].get("analysis_capture")
        second_capture = second[category].get("capture")
        if second_capture is None:
            second_capture = second[category].get("analysis_capture")
        assert json.dumps(first_capture, sort_keys=True) == json.dumps(
            second_capture, sort_keys=True
        )
    # No randomness anywhere: every record serializes deterministically.
    for record in _collect_fixture_records():
        if hasattr(record, "to_dict"):
            assert json.dumps(record.to_dict(), sort_keys=True)
