# 16 — Materials Chemistry / Computational Materials Science Domain Pack

## 1. Objective

Provide domain-specific decomposition, evidence, statistics, resource and analysis rules while preserving domain-independent Core logic.

## 2. v0.1 experimental capability families

The domain pack should include rule modules/templates for at least:

- ligand/precursor synthesis;
- MOF/material synthesis;
- solvent exchange/activation;
- PXRD;
- SCXRD and structure verification;
- TGA/thermal analysis;
- spectroscopy/basic identity characterization;
- N2 adsorption / BET / pore analysis;
- single-component gas adsorption;
- multi-component selectivity calculation;
- Qst calculation;
- dynamic breakthrough;
- cycling/reusability;
- stability testing;
- sample handling and independent batch logic.

## 3. v0.1 computational capability families

- structure model preparation;
- disorder resolution/model assumptions;
- DFT geometry/energy calculations;
- binding-site/binding-energy calculations;
- GCMC adsorption;
- MD/diffusion where present;
- convergence and stochastic error analysis;
- structural pore analysis.

## 4. Domain-specific inventory extraction

Inventory scanner should detect from paper/SI:

- synthesis recipes;
- named compounds/materials;
- activation procedures;
- instrument model/method parameters;
- figure/table conditions;
- gas identity/purity/composition;
- temperature/pressure ranges;
- sample mass and column properties;
- calculation software/method/force field/charges/cutoffs;
- structure models and disorder treatments;
- supplementary controls and stability conditions.

## 5. Domain acceptance examples

These are templates, never universal thresholds.

### PXRD
Analysis may include peak-position agreement, phase identification, intensity-pattern comparison with caution for preferred orientation, and batch consistency.

### BET
Use a frozen fitting/selection protocol. Batch-to-batch sample activation quality must be treated as a process variable; one attractive BET result is not sufficient.

### Gas adsorption
Validate data quality, equilibration, units, temperature, pressure basis and sample activation. Use independent material batches where scientifically feasible.

### Breakthrough
Record flow rates, gas composition, column dimensions, packing mass/density, dead volume, detector calibration and temperature. Missing critical column parameters enter Assumption Registry.

### DFT
Record complete model, disorder resolution, functional, dispersion, basis/pseudopotential, convergence and finite-size choices. Missing scientifically meaningful settings are A2 unless reliable method evidence supports an A1 classification.

### GCMC
Record force fields, charges, mixing rules, cutoffs/Ewald treatment, initialization/equilibration/production cycles, seeds and uncertainty analysis.

## 6. Safety

Domain pack may flag hazardous chemistry/gases and trigger Safety Gates, but v0.1 is not a laboratory safety management system. It should require human/local SOP confirmation rather than inventing unsafe procedures.
