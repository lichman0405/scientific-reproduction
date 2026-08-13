# 03 — Roles and Permissions

## 1. Governance principle

**Workers report facts. Supervisor makes scientific decisions.**

No worker may lower acceptance criteria, reinterpret a failure into a pass, or silently modify a scientific protocol.

## 2. Supervisor permissions

Supervisor alone may:

- create and freeze Plan versions;
- create, modify and version formal Goals;
- define and freeze Acceptance Criteria;
- define replication/sample-size design;
- define and freeze Primary Analysis Protocols;
- classify Requirements using frozen criticality rules;
- create Workers;
- request Research;
- decide strict/recovery/redesign transitions;
- approve or reject candidate explanations;
- decide retries beyond preauthorized engineering retries;
- close Goals/Requirements;
- assign final `reproduction_outcome` and project-level `method_reproducibility`;
- trigger Human Escalation Gates.

## 3. Research permissions

Research may:

- search public/open sources;
- use configured commercial adapters;
- acquire and index papers, SI, structures and datasets;
- create source records;
- extract claim-specific evidence;
- execute evidence checklists and compute evidence assessments;
- record search saturation cycles;
- respond to Supervisor Research Requests.

Research may not:

- change Goals;
- change acceptance criteria;
- decide Recovery actions;
- directly dispatch Workers;
- contact authors autonomously.

## 4. Execution Monitor permissions

Monitor may:

- inspect external Run status;
- change Run operational lifecycle according to deterministic state rules;
- validate arrival of Result Packages;
- execute preauthorized engineering retries;
- spawn follow-up collection/analysis workers when the frozen workflow requires it;
- maintain heartbeat/checkpoint/event records;
- resume itself after failure.

Monitor may not:

- change scientific parameters;
- classify a Goal as scientifically PASS/FAIL;
- enter Recovery autonomously;
- alter statistical design.

## 5. Experiment Worker permissions

Allowed:

- read one Goal Execution Context Package;
- prepare an Experiment Execution Package;
- record actual reagent/sample/instrument/procedure metadata;
- ingest returned Result Package;
- register raw artifacts;
- report deviations and anomalies;
- execute deterministic engineering steps that do not alter scientific meaning.

Forbidden:

- propose or implement scientific protocol changes;
- interpret root cause as a formal decision;
- create Goals;
- change track (`STRICT`, `RECOVERY`, `METHOD_REDESIGN`);
- decide retries except explicitly whitelisted engineering retries;
- declare PASS/FAIL.

## 6. Computation Worker permissions

Same governance as Experiment Worker. Scientific input parameters (functional, force field, convergence thresholds, k-point mesh, smearing policy where scientifically relevant, etc.) are frozen inputs unless the Supervisor versions the Goal/Run policy.

## 7. Analysis Worker permissions

May:

- run frozen Primary Analysis Protocol;
- calculate descriptive/inferential statistics;
- produce diagnostic metrics;
- run explicitly marked exploratory analyses separately;
- produce an Analysis Result Package with uncertainty, model assumptions and QC findings.

May not:

- replace the Primary Analysis with a result-friendlier method;
- alter equivalence margins after data are seen;
- decide Goal acceptance.

## 8. Diagnosis Worker permissions

May:

- inspect failed/abnormal Runs;
- identify observed failure patterns;
- evaluate candidate causes against available evidence;
- rank unresolved causes by evidence strength;
- report missing information.

May not:

- create a Recovery protocol;
- recommend formal changes directly to execution workers;
- contact Research itself;
- declare root cause as final without Supervisor review.

## 9. Human / PI permissions

The Human Gate is for major boundary decisions, not routine micro-management. Human approval is required for:

- major resources/procurement/outsource decisions;
- new access credentials/paid sources where required;
- material safety/scope changes;
- transition into Method Redesign if the defined policy requires explicit PI approval;
- external contact;
- formal project termination under defined non-reproduction scope.
