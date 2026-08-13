# 04 — Project Lifecycle

## 1. `project_phase` versus `reproduction_outcome`

These are strictly separate.

`project_phase` answers: **Where is the workflow now?**

`reproduction_outcome` answers: **What is the final scientific reproduction conclusion?**

Before Final Validation, the outcome should remain `UNDETERMINED`.

## 2. Project phases

Recommended enum:

- `INITIALIZING`
- `SOURCE_ACQUISITION`
- `REPRODUCTION_INVENTORY`
- `PLANNING`
- `PLAN_AUDIT`
- `PLAN_FROZEN`
- `EXECUTING`
- `REPLANNING`
- `FINAL_VALIDATION`
- `REPORTING`
- `COMPLETED`
- `PAUSED`
- `WAITING_HUMAN`
- `WAITING_RESOURCE`

## 3. Final reproduction outcomes

- `UNDETERMINED`
- `FULLY_REPRODUCED`
- `PARTIALLY_REPRODUCED`
- `NOT_REPRODUCED_WITHIN_DEFINED_SCOPE`
- `INCONCLUSIVE`

## 4. Requirement outcomes

Each Reproduction Requirement ultimately closes as:

- `REPRODUCED`
- `REPRODUCED_WITH_RECOVERY`
- `NOT_REPRODUCED`
- `INCONCLUSIVE`

Intermediate Run failures do not determine the final Requirement outcome by themselves.

## 5. Project outcome aggregation

Initial v0.1 rule set:

1. All formally reported Requirements close as `REPRODUCED` or `REPRODUCED_WITH_RECOVERY` -> `FULLY_REPRODUCED`.
2. All Critical Requirements reproduced, but one or more Required/Supporting Requirements close `NOT_REPRODUCED` -> `PARTIALLY_REPRODUCED`.
3. One or more Critical Requirements close `NOT_REPRODUCED` under a satisfied Closure Contract -> `NOT_REPRODUCED_WITHIN_DEFINED_SCOPE`.
4. Any unresolved Critical Requirement is `INCONCLUSIVE` -> project outcome cannot exceed `INCONCLUSIVE`.

The exact aggregator should be implemented as a versioned rule module, not hardcoded across agents.

## 6. Method reproducibility axis

Method reproducibility is separate from scientific outcome.

Per Requirement/Goal, recommended categories:

- `DIRECTLY_REPRODUCIBLE`
- `REPRODUCIBLE_WITH_MINOR_RECOVERY`
- `REPRODUCIBLE_WITH_METHOD_ADJUSTMENT`
- `ONLY_REPRODUCIBLE_AFTER_REDESIGN`
- `NOT_REPRODUCIBLE`
- `INCONCLUSIVE`

Project-level method reproducibility is an aggregate, not a subjective single rating.

## 7. Human Gate states

Use explicit gate objects rather than generic pauses:

- `RESOURCE_GATE`
- `ACCESS_GATE`
- `SAFETY_GATE`
- `SCOPE_GATE`
- `TERMINATION_GATE`
- `EXTERNAL_CONTACT_GATE`

A gate record must include trigger, requested decision, affected Goals, evidence and default safe action.
