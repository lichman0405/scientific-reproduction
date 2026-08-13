# 17 — FDM-201 Reference Reproduction Case

## 1. Target

Primary paper:

- Title: *A highly connected metal–organic framework with stretched inorganic units for propylene/ethylene separation*
- DOI: `10.1039/D5TA00771B`
- Journal/year: Journal of Materials Chemistry A, 2025
- Target MOF: FDM-201

This case is the v0.1 benchmark. The Skill must use the actual main paper plus acquired SI/CIF/repository material during benchmark execution. The facts below are **seed facts to verify**, not a substitute for source acquisition.

## 2. Seed facts to verify from authoritative sources

Reported structural/performance facts include approximately:

- FDM-201 uses a stretched `[Zn8SiO4]` inorganic unit and PyBC-type mixed coordination linker motif.
- 12-connected fcu framework.
- BET surface area around `1965 m² g⁻¹`.
- reported pore sizes around `7.9 Å` and `13.0 Å`.
- 298 K, 1 bar uptake approximately:
  - C3H6: `180.5 cm³ g⁻¹`
  - C2H4: `68.2 cm³ g⁻¹`
- 50/50 C3H6/C2H4 IAST selectivity around `8.6` at 298 K / 100 kPa.
- zero-coverage Qst approximately:
  - C3H6: `27.1 kJ mol⁻¹`
  - C2H4: `19.1 kJ mol⁻¹`
- reported static binding energies around:
  - C3H6: `48.72 kJ mol⁻¹`
  - C2H4: `38.61 kJ mol⁻¹`
- breakthrough example for 50/50 mixture at 298 K: C2H4 around 23.5 min, C3H6 around 64.8 min, separation window ~41.3 min.
- additional 328 K / diluted-mixture breakthrough conditions are reported and must be inventoried from the paper/SI.

Every number above must be revalidated against the fetched primary source during benchmark execution.

## 3. Required bootstrap source acquisition

At minimum the benchmark must locate/register:

1. main paper;
2. SI;
3. FDM-201 deposited crystal structure/CIF;
4. any related deposited structure(s) used by the paper;
5. data-availability resources;
6. key references for `[Zn8SiO4]` SBU chemistry;
7. key references for adsorption/IAST/Qst methods used;
8. computational method/force-field references;
9. relevant same-author and analogous MOF work.

## 4. Example Work Packages

### WP-00 Source acquisition and inventory

- GOAL-RES-001 Acquire/verify main paper
- GOAL-RES-002 Acquire SI
- GOAL-RES-003 Acquire CIF/deposition files
- GOAL-RES-004 Acquire linked public data
- GOAL-INV-001 Extract all experimental reported items
- GOAL-INV-002 Extract all computational reported items
- GOAL-INV-003 Map all main-text figures/tables to Requirements
- GOAL-INV-004 Map all SI figures/tables to Requirements
- GOAL-AUD-001 Completeness Audit; must reach 100% mapped, zero unresolved ambiguity before Plan freeze

### WP-10 Ligand / precursor reproduction

Final exact Goals must be generated from SI. Candidate Unit Processes:

- ligand precursor synthesis step 1
- ligand precursor purification/identity check
- PyBC synthesis
- PyBC purification
- PyBC identity/purity characterization
- any reported precursor/control synthesis

Each chemical synthesis Goal must define independent batch logic if the paper makes reproducibility-sensitive use of the compound.

### WP-20 FDM-201 synthesis and activation

Candidate Unit Processes:

- prepare reaction mixture exactly per SI;
- solvothermal synthesis of FDM-201;
- isolate/wash material;
- solvent exchange;
- activation;
- sample storage/handling;
- independent batch synthesis Runs;
- process-level batch reproducibility analysis.

Strict Runs must use exact public protocol and register any assumptions.

### WP-30 Structure and composition verification

Candidate Unit Processes:

- PXRD measurement for each independent batch;
- PXRD phase analysis;
- SCXRD/sample selection if formally reported and practically reproducible;
- structure/model comparison;
- TGA;
- any spectroscopy/compositional characterization formally reported;
- stability PXRD tests under all formally reported conditions.

### WP-40 Porosity

Candidate Unit Processes:

- activation QC before adsorption;
- N2 isotherm acquisition at reported temperature;
- BET analysis using frozen selection rules;
- pore-size analysis using the method actually reported;
- independent batch porosity statistics.

### WP-50 Single-component C3H6/C2H4 adsorption

Separate Goals by gas and temperature/condition when distinct reported datasets exist:

- C3H6 adsorption isotherm at each reported temperature;
- C2H4 adsorption isotherm at each reported temperature;
- desorption branch if formally reported;
- repeat/batch plan;
- uptake comparison statistics.

### WP-60 Derived thermodynamic/selectivity analysis

- fit/interpolate isotherms using frozen model/protocol;
- calculate IAST selectivity for every formally reported composition/condition;
- calculate Qst using the same reported method;
- uncertainty/sensitivity analysis appropriate to the method.

### WP-70 Breakthrough

Separate Unit Processes for every formally reported gas composition/temperature/cycle condition. Required pre-execution inventory should recover:

- column geometry;
- adsorbent mass;
- packing density if reported;
- flow rate;
- gas composition/purity;
- temperature/pressure;
- detector calibration;
- dead-volume correction;
- regeneration protocol;
- cycling protocol.

Any missing scientifically consequential parameter is registered as A2 and prevents pure strict classification.

### WP-80 Computational model construction

Candidate Unit Processes:

- acquire published CIF;
- resolve disorder/model choices;
- construct simulation cell;
- verify geometry and pore access;
- reproduce any linker rotation/dynamic pore argument if computationally reported;
- register all assumed atom types/charges/force-field settings.

### WP-81 Binding-site / energy reproduction

- reproduce C2H4 binding-site calculation;
- reproduce C3H6 binding-site calculation;
- reproduce binding-energy calculation method;
- verify convergence/model assumptions;
- compare recovered energy difference to paper under frozen equivalence criteria.

### WP-82 GCMC adsorption reproduction

- reproduce C2H4 adsorption simulation;
- reproduce C3H6 adsorption simulation;
- simulate all reported temperatures/pressures required by Inventory;
- sampling/convergence analysis;
- compare simulated and experimental trends/data as formally reported.

### WP-90 Final integration

- Requirement closure matrix;
- scientific-outcome aggregation;
- method-reproducibility aggregation;
- traceability audit;
- final report.

## 5. Example DAG semantics

Possible parallelism:

- Literature bootstrap, structure acquisition and computational model preparation can proceed while procurement planning occurs.
- Once FDM-201 synthesis Runs produce samples, PXRD can begin on one portion while solvent exchange/activation preparation proceeds as a soft dependency.
- BET acceptance may require a hard acceptance gate on sample identity even if measurement execution was started earlier.
- Computations based on published CIF can run independently of wet-lab completion, but final comparison may depend on experimental evidence.

## 6. Benchmark requirements

The v0.1 system passes the FDM planning benchmark only if:

- all formally reported paper/SI items are inventoried;
- 100% are mapped to Requirements/Goals;
- Goal DAG passes schema and dependency validation;
- criticality is produced by checklist/rule mapping;
- each quantitative Goal has a frozen acceptance/statistics design or an explicit justified non-statistical validation plan;
- assumptions are classified A0/A1/A2;
- resource/procurement blockers are represented;
- experiment/computation execution packages can be generated;
- simulated pass/fail/recovery/closure scenarios are correctly handled.
