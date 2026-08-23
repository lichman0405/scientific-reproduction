# 05 — Goal / Run / Requirement Model

## 1. Hierarchy

```text
Project
  -> Work Package
     -> Reproduction Requirement
        -> Goal (/goal = Unit Process)
           -> Run (execution instance)
              -> Artifact(s)
              -> Analysis Result(s)
```

## 2. Reproduction Requirement

A Requirement represents one formally reported result/procedure obligation from the target paper inventory. It may map to one or more Goals.

Each Requirement contains:

- source inventory item(s);
- scientific statement/result to reproduce;
- criticality (`CRITICAL`, `REQUIRED`, `SUPPORTING`);
- Goal mappings;
- final Requirement outcome;
- method reproducibility outcome;
- evidence trace.

## 3. Goal definition

A Goal must be a Unit Process that is:

- independently executable;
- independently observable;
- independently analyzable;
- independently reviewable;
- associated with explicit inputs and outputs.

Do not create umbrella Goals such as “reproduce all adsorption experiments”. Split into distinct measurement/analysis Unit Processes when different inputs, outputs or acceptance logic apply.

## 4. Goal Contract required fields

- Goal ID
- title
- domain/type
- Work Package/Requirement parent(s)
- track (`STRICT_REPRODUCTION`, `RECOVERY`, `METHOD_REDESIGN`)
- objective
- inputs
- procedure source(s)
- execution constraints
- dependencies
- resource requirements
- Assumption references
- replication design
- Acceptance Criteria
- Primary Analysis Protocol reference
- Closure Contract reference
- evidence requirements
- automatic engineering retry policy
- allowed outputs
- forbidden modifications
- version/freeze metadata

The version/freeze metadata carries the contract's revision lineage. Freeze
stamps the draft to its formal version (`v1-draft` -> `v1`) and persists the
contract in place; a plan-wide revision re-opens the whole goal-contract
family as the next draft version (`v1` -> `v2-draft`). A goal may also be
revised on its own (`revise_goal`): the single goal contract re-opens as the
next draft version with `parent_goal_id` set to the goal it was revised
from (goal-level lineage), while sibling goals and the
acceptance/design/analysis/closure records stay frozen and byte-untouched.
Runs keep referencing the `goal_version` they executed against, so past
results are unaffected.

## 5. Dependencies

Dependency semantics must include at least:

- `hard_gate`
- `soft_dependency`
- `informational`

A dependency may specify separately:

- `execution_gate`: must upstream state be reached before execution starts?
- `acceptance_gate`: must upstream evidence be valid before this Goal may close?

This allows safe parallelism without invalidating final evidence.

## 6. Run definition

A Run is one actual execution instance of a Goal.

Run types include:

- `independent_replicate`
- `technical_replicate`
- `instrument_repeat`
- `retry`
- `additional_replicate`

Independent replication is mandatory for experimental Goals unless a domain-specific rule explicitly provides a justified exception.

## 7. Recommended Run lifecycle

- `CREATED`
- `READY`
- `DISPATCHED`
- `RUNNING_EXTERNAL`
- `RESULT_AVAILABLE`
- `ANALYZING`
- `SUBMITTED_FOR_REVIEW`
- `CLOSED`
- `CANCELLED`
- `INVALIDATED`

Scientific PASS/FAIL is not a Run lifecycle state; it is a review decision stored separately.

## 8. Context Package

Each dynamic worker receives a Goal Execution Context Package containing only the minimum necessary project context:

- Goal Contract version/hash;
- relevant target-paper excerpts/source records;
- relevant evidence records;
- required upstream results;
- execution environment;
- resource information;
- frozen analysis/acceptance references;
- allowed engineering retries;
- required return artifacts;
- explicit prohibitions.

Workers must not read the entire repository unless specifically authorized.
