# 08 — Strict Reproduction, Recovery, Method Redesign and Closure

## 1. Tracks

### STRICT_REPRODUCTION
Use the publicly reported protocol as faithfully as possible. A0 technical defaults and explicitly governed A1 methodological defaults may be used and recorded. A2 scientific assumptions disqualify a Run from being considered pure strict reproduction.

### RECOVERY
Entered only after a formal strict failure/inconclusive state and Supervisor decision. Recovery is evidence-backed and versioned.

### METHOD_REDESIGN
A scientifically new or substantially reworked route used to obtain the target result after the published method cannot be directly recovered. It must never be reported as successful reproduction of the original method.

## 2. Recovery levels

### Level 1 — Minor/tacit detail recovery
Examples: drying detail, order of operations, brand/purity constraints, standard handling detail plausibly omitted from paper.

### Level 2 — Limited parameter adjustment
Examples: modest changes to time, temperature, activation conditions, ratios within the same method family.

### Level 3 — Method composition adjustment
Examples: changing precursor salt, modulator, solvent system or other substantive but still recognizably related method elements.

### Level 4 — Method redesign
Must be labeled `METHOD_REDESIGN`.

## 3. Assumption Registry

Every non-explicit parameter is registered:

- `A0_TECHNICAL_DEFAULT`
- `A1_METHODOLOGICAL_DEFAULT`
- `A2_SCIENTIFIC_ASSUMPTION`

A2 must not be silently used inside strict reproduction.

## 4. Closure Contract

Do not stop because “N attempts failed”. A Goal may close `NOT_REPRODUCED_WITHIN_DEFINED_SCOPE` only when the frozen Closure Contract is satisfied.

Required categories:

### Statistical sufficiency
The evidence is adequate to distinguish failure/non-equivalence from insufficient precision.

### Execution validity
Required Runs are valid; unresolved engineering/QC failures do not explain the result.

### Diagnosis completion
A formal diagnosis has been performed when appropriate.

### Recovery-space exhaustion
All eligible high-confidence recovery hypotheses have been tested or ruled out.

Default v0.1 Recovery eligibility:

- Reliability >= 3
- Directness >= 2
- actionable = true

### Research saturation
All required search families have been completed and the configured saturation rule is satisfied.

Default v0.1 operational saturation rule:

- two consecutive expansion search cycles produce zero new eligible Recovery hypotheses.

This is a governance rule, not a universal scientific constant; it must be configurable and frozen.

## 5. Closure outcomes

- `REPRODUCED`
- `REPRODUCED_WITH_RECOVERY`
- `NOT_REPRODUCED`
- `INCONCLUSIVE`

`NOT_REPRODUCED` means “not reproduced within the pre-defined and sufficiently explored scope”; it must not be phrased as proof that the original paper is false.

## 6. Human termination gate

Final project closure into `NOT_REPRODUCED_WITHIN_DEFINED_SCOPE` should trigger a `TERMINATION_GATE` for PI/user awareness and formal sign-off in v0.1.
