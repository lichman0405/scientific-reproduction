"""Integration: the M2 rule modules compose into the normative lifecycle
(DEV-M2-G08, AC-01).

Goal contract DEV-M2-G08 (frozen, verbatim):
  * goal_id: DEV-M2-G08
  * milestone: M2
  * title: Run normative lifecycle and rule-engine integration suite
  * objective: Prove all M2 rule modules compose without scientific shortcut
    transitions.
  * AC-01: All M2 rule tests pass.

This module is the AC-01 end-to-end half of the suite: at least one scenario
that walks a Goal through the full frozen pipeline -- lifecycle transitions
(rules.lifecycle), dependency / execution / acceptance gating
(rules.dependencies), evidence hard gates (rules.evidence), criticality
classification (rules.criticality), the strict-label axis (rules.assumptions),
the Closure Contract hard gate (rules.closure) and outcome classification
(rules.outcome) -- and asserts the composed result matches the spec's
expected outcome.

The composition is deliberately *compositional*: each stage consumes the
decision of the previous stage through the exact public APIs the M2 modules
export (e.g. ``aggregate_project_outcome`` consumes
``ClosureAssessment.closure_allowed`` and never re-derives it, per the
outcome module's normative reading), so the scenarios prove the modules
compose end-to-end rather than re-proving any unit-level property.

Normative sources (all frozen):
  * 04-PROJECT-LIFECYCLE.md sections 1-5 -- project phases, strict
    phase/outcome separation, requirement outcomes, project outcome
    aggregation rule set (rules.lifecycle / rules.outcome).
  * 05-GOAL-RUN-SCHEMA.md sections 5 and 7 -- dependency semantics with
    separate execution/acceptance gates and the Recommended Run lifecycle
    (rules.dependencies / rules.lifecycle).
  * 06-EVIDENCE-SYSTEM.md SS1-SS4 -- Source x Claim assessment, Reliability
    from checklist inputs, hard gates independent of any display score
    (rules.evidence).
  * 08-STRICT-RECOVERY-CLOSURE.md sections 3-4 -- Assumption Registry and
    the Closure Contract ("Do not stop because 'N attempts failed'";
    recovery-space exhaustion; research saturation)
    (rules.assumptions / rules.closure).
  * 18-TEST-AND-ACCEPTANCE-PLAN.md Scenario C and
    examples/fdm-201/simulated-scenarios.md S6 -- the non-reproduced closure
    scenario: strict failure statistically sufficient, QC valid, all
    eligible hypotheses tested/ruled out, research saturation met ->
    Closure Contract satisfied -> NOT_REPRODUCED.
  * 17-FDM201-REFERENCE-CASE.md -- the BET acceptance-gate example
    (measurement execution started earlier while the hard acceptance gate on
    sample identity is still unresolved).

Determinism: every scenario uses fixed, frozen inputs only -- no random
values, no wall-clock dependence; the same scenario always yields the same
composed result on every platform and Python version.
"""

from __future__ import annotations

from scientific_reproduction.core.models import (
    Assumption,
    AssumptionClassification,
    Criticality,
    DependencyType,
    LifecycleState,
    ProjectPhase,
    ReproductionOutcome,
    RequirementOutcome,
    Run,
    RunType,
    ScientificReview,
    SourceType,
)
from scientific_reproduction.core.rules.assumptions import (
    StrictLabel,
    evaluate_strict_label,
)
from scientific_reproduction.core.rules.closure import (
    ClosureGateId,
    ClosureRecord,
    evaluate_closure,
)
from scientific_reproduction.core.rules.criticality import (
    CriticalityChecklist,
    classify_criticality,
)
from scientific_reproduction.core.rules.dependencies import (
    DependencyRecord,
    evaluate_acceptance_gate,
    evaluate_execution_gate,
)
from scientific_reproduction.core.rules.evidence import (
    ReliabilityChecklist,
    assess,
    recovery_hypothesis_eligible,
)
from scientific_reproduction.core.rules.lifecycle import (
    PROJECT_PHASE_MAINLINE,
    RUN_MAINLINE,
    apply_project_phase_transition,
    apply_run_lifecycle_transition,
)
from scientific_reproduction.core.rules.outcome import (
    RequirementOutcomeRecord,
    aggregate_project_outcome,
)

# ---------------------------------------------------------------------------
# Shared frozen scenario inputs (deterministic; no randomness, no wall clock)
# ---------------------------------------------------------------------------

#: Reliability checklist with all eight positive dimensions satisfied and no
#: disqualifying signal: Reliability 4 under reliability-rule-v1.
FULL_RELIABILITY_CHECKLIST = ReliabilityChecklist(
    checklist_ref="rc-fdm201-main",
    raw_data_available=True,
    method_complete=True,
    independent_replication_performed=True,
    uncertainty_reported=True,
    independent_external_validation=True,
    data_internally_consistent=True,
    conclusion_supported_by_data=True,
    material_identity_controlled=True,
    known_retraction_correction_defect=False,
)


def _walk_phases(*targets: ProjectPhase) -> None:
    """Apply normative phase transitions from INITIALIZING through ``targets``.

    Deterministic walk along the mainline (rules.lifecycle); every
    transition is applied through ``apply_project_phase_transition``, so an
    illegal arc raises ``IllegalTransitionError`` and fails the scenario.
    """
    phase = ProjectPhase.INITIALIZING
    for next_phase in PROJECT_PHASE_MAINLINE[1:]:
        phase = apply_project_phase_transition(phase, next_phase)
        if next_phase in targets:
            assert phase is next_phase


def _walk_run(*targets: LifecycleState) -> None:
    """Apply normative Run-lifecycle transitions through ``targets``."""
    state = LifecycleState.CREATED
    for next_state in RUN_MAINLINE[1:]:
        state = apply_run_lifecycle_transition(state, next_state)
        if next_state in targets:
            assert state is next_state


def _assumption(
    assumption_id: str,
    parameter: str,
    classification: AssumptionClassification,
) -> Assumption:
    """One frozen Assumption Registry entry (schemas/assumption.schema.yaml)."""
    return Assumption(
        assumption_id=assumption_id,
        parameter=parameter,
        classification=classification,
        rationale=(
            "registered per 08-STRICT-RECOVERY-CLOSURE.md section 3 "
            "(Assumption Registry)"
        ),
        source_refs=["08-STRICT-RECOVERY-CLOSURE.md section 3"],
    )


# ---------------------------------------------------------------------------
# AC-01 scenario 1: the full successful-reproduction pipeline
# ---------------------------------------------------------------------------


def test_end_to_end_full_reproduction_pipeline_reaches_fully_reproduced() -> None:
    """AC-01: the full frozen pipeline composes to FULLY_REPRODUCED.

    A Goal is walked through every stage of the frozen pipeline and the
    composed result is asserted against 04-PROJECT-LIFECYCLE.md section 5
    rule 1: all formally reported Requirements close REPRODUCED /
    REPRODUCED_WITH_RECOVERY -> FULLY_REPRODUCED.

    Pipeline stages, in order:
      1. lifecycle (rules.lifecycle): project phases INITIALIZING -> ... ->
         COMPLETED and the Run lifecycle CREATED -> ... -> CLOSED along the
         05-GOAL-RUN-SCHEMA.md section 7 mainline;
      2. dependency gating (rules.dependencies): an upstream hard_gate
         dependency is resolved on both axes, so the execution gate and the
         acceptance gate are both ALLOWED;
      3. evidence (rules.evidence): the (source, claim) assessment for the
         main claim yields Reliability 4 / Directness 4, so the v0.1
         recovery-hypothesis eligibility gate passes;
      4. criticality (rules.criticality): the main finding invalidates the
         main result in a main-figure position -> CRITICAL (R-CRIT-1); a
         required-step finding -> REQUIRED;
      5. strict label (rules.assumptions): only A0 technical defaults are
         registered, so the reproduction keeps the pure-strict STRICT label
         (08-STRICT-RECOVERY-CLOSURE.md section 3);
      6. closure (rules.closure): all four mandatory gates satisfied ->
         CLOSURE_ALLOWED;
      7. outcome (rules.outcome): both Requirements are individually
         determined and reproduced, closure_allowed is consumed (never
         re-derived) -> FULLY_REPRODUCED via R-PRJ-1.
    """
    # 1. lifecycle transitions (rules.lifecycle).
    _walk_phases(ProjectPhase.COMPLETED)
    _walk_run(LifecycleState.CLOSED)

    # 2. dependency gating (rules.dependencies): upstream preparation goal
    #    is resolved on both gate axes (05-GOAL-RUN-SCHEMA.md section 5).
    upstream = DependencyRecord(
        goal_id="UPSTREAM-PRECURSOR-PREP",
        dependency_type=DependencyType.HARD_GATE,
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=True,
        acceptance_resolved=True,
    )
    execution_gate = evaluate_execution_gate([upstream])
    acceptance_gate = evaluate_acceptance_gate([upstream])
    assert execution_gate.execution_allowed is True
    assert execution_gate.matched_rule_id == "R-EXEC-G-2"
    assert acceptance_gate.acceptance_allowed is True
    assert acceptance_gate.matched_rule_id == "R-ACC-G-2"

    # 3. evidence (rules.evidence): Source x Claim assessment of the main
    #    claim against the target paper (06-EVIDENCE-SYSTEM.md SS1/SS2).
    assessment = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="C-main-result",
        checklist=FULL_RELIABILITY_CHECKLIST,
        directness=4,
    )
    assert assessment.reliability == 4
    assert assessment.directness == 4
    assert recovery_hypothesis_eligible(assessment) is True

    # 4. criticality (rules.criticality): the finding invalidates the paper's
    #    main result in a main-figure position -> CRITICAL (R-CRIT-1); a
    #    second finding affects a required step -> REQUIRED (R-REQ-3).
    critical_finding = classify_criticality(
        CriticalityChecklist(
            affects_main_figure=True,
            invalidates_main_result=True,
        )
    )
    required_finding = classify_criticality(
        CriticalityChecklist(affects_required_step=True)
    )
    assert critical_finding.criticality is Criticality.CRITICAL
    assert critical_finding.matched_rule_id == "R-CRIT-1"
    assert required_finding.criticality is Criticality.REQUIRED
    assert required_finding.matched_rule_id == "R-REQ-3"

    # 5. strict label (rules.assumptions): A0-only set keeps the pure-strict
    #    STRICT label (AC-01 of DEV-M2-G07).
    label = evaluate_strict_label(
        [
            _assumption(
                "A-1",
                "drying temperature",
                AssumptionClassification.A0_TECHNICAL_DEFAULT,
            ),
            _assumption(
                "A-2",
                "stirring rate",
                AssumptionClassification.A0_TECHNICAL_DEFAULT,
            ),
        ]
    )
    assert label.label is StrictLabel.STRICT
    assert label.matched_label_rule_id == "R-STRICT-4"

    # 6. closure (rules.closure): all four mandatory gates satisfied
    #    (08-STRICT-RECOVERY-CLOSURE.md section 4).
    closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            eligible_hypotheses_total=2,
            tested_or_ruled_out=2,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        )
    )
    assert closure.closure_allowed is True
    assert closure.matched_rule_id == "R-CLOSE-2"

    # 7. outcome (rules.outcome): the project outcome consumes the closure
    #    decision and classifies every Requirement (04-PROJECT-LIFECYCLE.md
    #    section 5 rule 1 -> FULLY_REPRODUCED).
    project = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-1",
                criticality=critical_finding.criticality,
                outcome=RequirementOutcome.REPRODUCED,
            ),
            RequirementOutcomeRecord(
                requirement_id="R-2",
                criticality=required_finding.criticality,
                outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY,
            ),
        ],
        closure_allowed=closure.closure_allowed,
    )
    assert project.outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert project.matched_rule_id == "R-PRJ-1"
    assert project.closure_allowed is True
    assert project.determined is True
    assert project.blocking_reasons == ()


# ---------------------------------------------------------------------------
# AC-01 scenario 2: the legitimate NOT_REPRODUCED closure pipeline
# ---------------------------------------------------------------------------


def test_end_to_end_legitimate_not_reproduced_closure_pipeline() -> None:
    """AC-01: the full frozen pipeline composes to the legitimate negative.

    The FDM-201-style strict-failure scenario (18-TEST-AND-ACCEPTANCE-PLAN.md
    Scenario C; examples/fdm-201/simulated-scenarios.md S6): strict failure
    statistically sufficient, QC valid, every eligible recovery hypothesis
    tested or ruled out, research saturation met -> the Closure Contract is
    satisfied, so one or more Critical Requirements may close NOT_REPRODUCED
    -> NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (04-PROJECT-LIFECYCLE.md section
    5 rule 3).

    The recovery-space composition is explicit: hypothesis eligibility is
    decided by the evidence hard gate (rules.evidence, SS4) and the closure
    record's recovery axis is filled from the eligible/tested counts before
    the closure hard gate (rules.closure) evaluates them. The scientific
    review decision (FAIL) is stored separately (05-GOAL-RUN-SCHEMA.md
    section 7) and never moves the Run lifecycle; the A2 assumption axis
    (rules.assumptions) disqualifies the pure-strict label without changing
    the scientific outcome.
    """
    # 1. lifecycle: project reaches FINAL_VALIDATION -> REPORTING; the
    #    failing Runs are reviewed and closed through the normative table.
    _walk_phases(ProjectPhase.FINAL_VALIDATION, ProjectPhase.REPORTING)
    _walk_run(LifecycleState.SUBMITTED_FOR_REVIEW, LifecycleState.CLOSED)
    failing_run = Run(
        run_id="RUN-S6-1",
        goal_id="G-SAMPLE-CONSTITUTION",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.CLOSED,
        goal_version="1.0",
        scientific_review=ScientificReview.FAIL,
    )
    # The review FAIL is a separate field (05-GOAL-RUN-SCHEMA.md section 7):
    # the Run still closes through SUBMITTED_FOR_REVIEW -> CLOSED.
    assert failing_run.scientific_review is ScientificReview.FAIL
    assert failing_run.lifecycle_state is LifecycleState.CLOSED

    # 2. evidence: every recovery hypothesis that passed the eligibility hard
    #    gate was tested and ruled out (06-EVIDENCE-SYSTEM.md SS4).
    hypothesis_one = assess(
        source=SourceType.PEER_REVIEWED_PAPER,
        claim_id="C-recovery-h1",
        checklist=FULL_RELIABILITY_CHECKLIST,
        directness=3,
    )
    hypothesis_two = assess(
        source=SourceType.DATASET,
        claim_id="C-recovery-h2",
        checklist=FULL_RELIABILITY_CHECKLIST,
        directness=2,
    )
    eligible = [
        assessment
        for assessment in (hypothesis_one, hypothesis_two)
        if recovery_hypothesis_eligible(assessment)
    ]
    assert len(eligible) == 2
    eligible_total = len(eligible)
    tested_or_ruled_out = eligible_total

    # 3. closure: strict failure statistically sufficient, QC valid, all
    #    eligible hypotheses tested/ruled out, saturation rule met
    #    (08-STRICT-RECOVERY-CLOSURE.md section 4).
    closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            eligible_hypotheses_total=eligible_total,
            tested_or_ruled_out=tested_or_ruled_out,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        )
    )
    assert closure.closure_allowed is True
    assert closure.blocked_gate_ids == ()

    # 4. criticality + outcome: the Critical Requirement closes NOT_REPRODUCED
    #    under the satisfied Closure Contract -> NOT_REPRODUCED_WITHIN_DEFINED
    #    _SCOPE (04-PROJECT-LIFECYCLE.md section 5 rule 3).
    critical_finding = classify_criticality(
        CriticalityChecklist(
            affects_main_figure=True,
            changes_paper_conclusion=True,
        )
    )
    assert critical_finding.criticality is Criticality.CRITICAL
    project = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-1",
                criticality=critical_finding.criticality,
                outcome=RequirementOutcome.NOT_REPRODUCED,
            ),
            RequirementOutcomeRecord(
                requirement_id="R-2",
                criticality=Criticality.REQUIRED,
                outcome=RequirementOutcome.REPRODUCED,
            ),
        ],
        closure_allowed=closure.closure_allowed,
    )
    assert project.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    assert project.matched_rule_id == "R-PRJ-3"

    # 5. strict label: the A2 scientific assumption disqualifies the pure
    #    strict label; the label axis is orthogonal to the scientific outcome.
    label = evaluate_strict_label(
        [
            _assumption(
                "A-3",
                "activation temperature ramp",
                AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION,
            )
        ]
    )
    assert label.label is StrictLabel.NOT_STRICT
    assert label.matched_label_rule_id == "R-STRICT-2"
    # The NOT_REPRODUCED_WITHIN_DEFINED_SCOPE conclusion stands: an A2
    # assumption affects labeling, never the closure decision or outcome.
    assert project.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE


# ---------------------------------------------------------------------------
# AC-01 scenario 3: the pipeline holds every axis while gates are unresolved
# ---------------------------------------------------------------------------


def test_end_to_end_pipeline_holds_axes_while_gates_unresolved() -> None:
    """AC-01: the pipeline composes correctly while gates are unresolved.

    A Goal mid-flight (project at EXECUTING, Run at RUNNING_EXTERNAL): the
    upstream hard execution-gated dependency is resolved while its hard
    acceptance-gated axis is not (the 17-FDM201-REFERENCE-CASE.md BET case:
    measurement execution started earlier, the acceptance gate on sample
    identity is still unresolved). The composed pipeline must hold every
    axis independently:

      * the execution gate is ALLOWED, so the Run may proceed
        RUNNING_EXTERNAL -> RESULT_AVAILABLE (05-GOAL-RUN-SCHEMA.md section
        5: the axes are independent -- AC-03 of DEV-M2-G02);
      * the acceptance gate is BLOCKED, so closure cannot be granted;
      * the Closure Contract is unassessed (recovery pool unknown), so the
        closure hard gate blocks with the unresolved gates reported
        (08-STRICT-RECOVERY-CLOSURE.md section 4);
      * the Requirement outcome is still OPEN, so the project outcome stays
        UNDETERMINED before Final Validation (04-PROJECT-LIFECYCLE.md
        section 1; AC-02 of DEV-M2-G06).
    """
    # 1. lifecycle: project at EXECUTING, Run at RUNNING_EXTERNAL.
    _walk_phases(ProjectPhase.EXECUTING)
    _walk_run(LifecycleState.RUNNING_EXTERNAL)

    # 2. dependency gating: execution axis resolved, acceptance axis not.
    upstream = DependencyRecord(
        goal_id="UPSTREAM-BET-SAMPLE-IDENTITY",
        dependency_type=DependencyType.HARD_GATE,
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=True,
        acceptance_resolved=False,
    )
    execution_gate = evaluate_execution_gate([upstream])
    acceptance_gate = evaluate_acceptance_gate([upstream])
    assert execution_gate.execution_allowed is True
    assert execution_gate.blocking_goal_ids == ()
    assert acceptance_gate.acceptance_allowed is False
    assert acceptance_gate.blocking_goal_ids == ("UPSTREAM-BET-SAMPLE-IDENTITY",)

    # 3. closure: recovery pool unassessed -> the recovery gate is UNRESOLVED
    #    and the closure hard gate blocks (AC-01 of DEV-M2-G05).
    closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=None,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        )
    )
    assert closure.closure_allowed is False
    assert closure.blocked_gate_ids == (
        ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
    )

    # 4. outcome: the Requirement is still OPEN -> UNDETERMINED.
    project = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-1",
                criticality=Criticality.CRITICAL,
                outcome=RequirementOutcome.OPEN,
            )
        ],
        closure_allowed=closure.closure_allowed,
    )
    assert project.outcome is ReproductionOutcome.UNDETERMINED
    assert project.matched_rule_id == "R-PRJ-UND-1"
    assert "not individually determined" in project.blocking_reasons[0]

    # 5. composition: the axes stay independent end-to-end -- the Run may
    #    proceed to RESULT_AVAILABLE (execution allowed) while acceptance
    #    stays blocked, closure stays blocked and the outcome stays
    #    UNDETERMINED. The frozen pipeline holds each axis without any
    #    shortcut granting the others.
    assert apply_run_lifecycle_transition(
        LifecycleState.RUNNING_EXTERNAL, LifecycleState.RESULT_AVAILABLE
    ) is LifecycleState.RESULT_AVAILABLE
    assert acceptance_gate.acceptance_allowed is False
    assert closure.closure_allowed is False
    assert project.outcome is ReproductionOutcome.UNDETERMINED
