# FDM-201 Simulated Execution Scenarios

These scenarios are test fixtures, not scientific claims.

## S1 Strict synthesis PASS
Three valid independent synthesis batches meet identity and downstream acceptance.
Expected: Requirement reproduced; method directly reproducible if no A2 assumptions/recovery used.

## S2 Strict synthesis FAIL -> Recovery PASS
Three valid strict batches fail phase/pore acceptance. Diagnosis reports consistent batch pattern. Research identifies one eligible recovery hypothesis. Supervisor versions Recovery Goal. Recovery independent batches pass.
Expected: `REPRODUCED_WITH_RECOVERY`; method reproducibility reflects recovery level.

## S3 Adsorption statistically inconclusive
Three batch results have wide uncertainty overlapping equivalence bounds.
Expected: no PASS/FAIL; dynamic additional runs generated according to frozen sample-size policy.

## S4 Slurm node failure
GCMC job dies because allocated node fails.
Expected: Monitor triggers identical resubmission; no scientific replan.

## S5 DFT convergence requires parameter change
Job technically runs but scientific convergence fails; changing smearing/mixing/convergence policy would alter method.
Expected: Worker reports facts; Supervisor decides diagnosis/research/recovery.

## S6 Recovery exhausted
All eligible recovery hypotheses processed, search saturation reached, execution valid and statistics sufficient.
Expected: Closure Contract allows NOT_REPRODUCED conclusion; Termination Gate generated before project-level closure.
