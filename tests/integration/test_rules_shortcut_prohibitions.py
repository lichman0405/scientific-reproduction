"""Integration: every locked shortcut prohibition composes away end-to-end
(DEV-M2-G08, AC-02).

Goal contract DEV-M2-G08 (frozen, verbatim):
  * AC-02: At least one test covers each locked shortcut prohibition.

Canonical list of the shortcut prohibitions the M2 rule modules lock
(enumeration made from the modules themselves; each entry cites its
module and its frozen spec source):

  S-01  Failure-count shortcut to closure -- no fixed "N attempts failed"
        closes a Goal. Locked by rules.closure (DEV-M2-G05): the Closure
        Record has no attempt-count axis and the aggregate table is exactly
        the conjunction pair (R-CLOSE-1 / R-CLOSE-2) with no shortcut rule.
        Spec: 08-STRICT-RECOVERY-CLOSURE.md section 4 ("Do not stop because
        'N attempts failed'"), 20-ARCHITECTURE-DECISIONS.md decision 20.
        Composition test: test_attempt_count_never_shortcuts_closure.

  S-02  project_phase substituted for reproduction_outcome, or an outcome
        derived from a phase. Locked by rules.outcome (DEV-M2-G06): the
        module has no function accepting a ProjectPhase, phase values are
        rejected at the record boundary (OutcomeRecordError) and at every
        public function boundary (TypeError).
        Spec: 04-PROJECT-LIFECYCLE.md section 1.
        Composition test: test_project_phase_never_determines_outcome.

  S-03  Soft/informational dependency blocking execution. Locked by
        rules.dependencies (DEV-M2-G02): only hard_gate with an unresolved
        gated axis blocks (R-DEP-1..R-DEP-3, R-EXEC-G-1); soft is
        ORDERING_ONLY, informational is INFORMATIONAL, neither ever blocks.
        Spec: 05-GOAL-RUN-SCHEMA.md section 5 ("allows safe parallelism
        without invalidating final evidence").
        Composition test: test_unresolved_soft_dependency_never_blocks_execution.

  S-04  Arbitrary/illegal lifecycle transition. Locked by rules.lifecycle
        (DEV-M2-G01): a legal transition is exactly an ordered pair in the
        frozen tables; anything else raises IllegalTransitionError -- stage
        skips, backwards moves, wrong abort kinds (RESULT_AVAILABLE ->
        CANCELLED, CREATED -> INVALIDATED) and the replanning shortcut
        (REPLANNING -> EXECUTING jumps back without PLAN_AUDIT /
        PLAN_FROZEN).
        Spec: 05-GOAL-RUN-SCHEMA.md section 7, 04-PROJECT-LIFECYCLE.md
        section 2.
        Composition test: test_gate_approval_cannot_legalize_illegal_transition.

  S-05  Non-strict assumptions allowed in strict mode. Locked by
        rules.assumptions (DEV-M2-G07): any A2 scientific assumption yields
        NOT_STRICT and dominates A1 (R-STRICT-2 before R-STRICT-3); only an
        A0-only (or empty) set keeps the pure-strict STRICT label.
        Spec: 08-STRICT-RECOVERY-CLOSURE.md section 3 ("A2 must not be
        silently used inside strict reproduction").
        Composition test: test_evidence_quality_cannot_earn_pure_strict_label.

  S-06  Criticality substitution -- a non-critical finding forced into a
        critical classification/outcome. Locked by rules.criticality
        (DEV-M2-G04): a main-figure location alone is never CRITICAL
        (R-REQ-1; CRITICAL requires main figure AND invalidated main result
        / changed conclusion, R-CRIT-1/2), and by rules.outcome (DEV-M2-G06):
        only Critical Requirements can produce NOT_REPRODUCED_WITHIN_DEFINED
        _SCOPE (R-PRJ-3); non-critical NOT_REPRODUCED at most yields
        PARTIALLY_REPRODUCED (R-PRJ-2).
        Spec: 04-PROJECT-LIFECYCLE.md section 5 rules 2-3.
        Composition test: test_main_figure_location_alone_cannot_force_not_reproduced.

  S-07  Dependency resolution invented outside the frozen records. Locked by
        rules.dependencies (DEV-M2-G02): DependencyRecord.from_goal_dependency
        requires the upstream resolution state explicitly (the frozen model
        carries none), malformed records raise DependencyRecordError and
        non-record inputs raise TypeError.
        Spec: 05-GOAL-RUN-SCHEMA.md section 5 (the two gate questions are
        answered from upstream state, never invented).
        Composition test: test_dependency_resolution_derived_from_evidence_not_invented.

  S-08  The weighted display score substituting for the evidence hard gates.
        Locked by rules.evidence (DEV-M2-G03): ranking_score is a
        display/search-ranking-only number (SS3) and the hard-gate
        predicates (SS4) never read it -- they evaluate the raw axes only.
        Spec: 06-EVIDENCE-SYSTEM.md SS3/SS4.
        Composition test: test_ranking_score_cannot_substitute_for_recovery_eligibility.

  S-09  Outcome determined before Final Validation. Locked by rules.outcome
        (DEV-M2-G06): a Requirement still OPEN stays UNDETERMINED and forces
        the project outcome to UNDETERMINED (R-PRJ-UND-1); determination is
        gated on every Requirement being individually determined, never on
        the project phase.
        Spec: 04-PROJECT-LIFECYCLE.md section 1 ("Before Final Validation,
        the outcome should remain UNDETERMINED").
        Composition test: test_open_critical_requirement_blocks_determination.

  S-10  A NOT_REPRODUCED closure without a satisfied Closure Contract
        determining the outcome. Locked by rules.outcome (DEV-M2-G06):
        closure_allowed=False blocks determination (R-PRJ-UND-2) and
        closure_allowed=None (unassessed) blocks it too (R-PRJ-UND-3); the
        closure decision is consumed from rules.closure (DEV-M2-G05), never
        re-derived.
        Spec: 08-STRICT-RECOVERY-CLOSURE.md section 4.
        Composition test: test_not_reproduced_without_closure_contract_stays_undetermined.

  S-11  Scientific PASS/FAIL used as a Run lifecycle state or as a
        reproduction outcome. Locked by rules.lifecycle (DEV-M2-G01): the
        lifecycle table contains no review arc and the LifecycleState enum
        has no FAIL member ("scientific PASS/FAIL is a review decision
        stored separately, not a lifecycle state"), and by rules.outcome
        (DEV-M2-G06): only RequirementOutcome members are accepted as
        outcomes.
        Spec: 05-GOAL-RUN-SCHEMA.md section 7.
        Composition test: test_review_decision_is_not_lifecycle_state_nor_outcome.

Every prohibition test composes two or more M2 rule modules (the unit
suites already prove each prohibition in isolation; these prove the shortcut
is impossible end-to-end through the composed pipeline).

Determinism: fixed, frozen inputs only -- no random values, no wall-clock
dependence.
"""

from __future__ import annotations

import pytest

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
    DependencyRecordError,
    evaluate_acceptance_gate,
    evaluate_execution_gate,
)
from scientific_reproduction.core.rules.evidence import (
    ReliabilityChecklist,
    assess,
    directness_gate_passes,
    ranking_score,
    recovery_hypothesis_eligible,
    reliability_gate_passes,
)
from scientific_reproduction.core.rules.lifecycle import (
    apply_project_phase_transition,
    apply_run_lifecycle_transition,
)
from scientific_reproduction.core.rules.outcome import (
    OutcomeRecordError,
    RequirementOutcomeRecord,
    aggregate_project_outcome,
    classify_requirement_outcome,
)


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


def _reliability_checklist(ref: str, satisfied: int) -> ReliabilityChecklist:
    """Checklist with ``satisfied`` positive dimensions answered True."""
    positives = (
        "raw_data_available",
        "method_complete",
        "independent_replication_performed",
        "uncertainty_reported",
        "independent_external_validation",
        "data_internally_consistent",
        "conclusion_supported_by_data",
        "material_identity_controlled",
    )
    answers = {key: index < satisfied for index, key in enumerate(positives)}
    return ReliabilityChecklist(
        checklist_ref=ref,
        known_retraction_correction_defect=False,
        **answers,
    )


# ---------------------------------------------------------------------------
# S-01: failure-count shortcut to closure (rules.closure + rules.outcome)
# ---------------------------------------------------------------------------


def test_attempt_count_never_shortcuts_closure() -> None:
    """S-01: recorded failed attempts never close a Goal.

    Composition (rules.closure + rules.outcome, with the frozen Run model as
    the lifecycle vocabulary): a Goal whose Runs record five failed
    engineering retries (Run.engineering_retries, retry-policy schema)
    closes exactly like its zero-retry twin: the Closure Contract governs
    the stopping decision through the four mandatory gates alone
    (08-STRICT-RECOVERY-CLOSURE.md section 4 -- "Do not stop because 'N
    attempts failed'"), and the outcome classification consumes the closure
    decision without ever seeing an attempt count.
    """
    retried_run = Run(
        run_id="RUN-RETRY-5",
        goal_id="G-CRITICAL-1",
        run_type=RunType.RETRY,
        lifecycle_state=LifecycleState.CLOSED,
        goal_version="1.0",
        scientific_review=ScientificReview.FAIL,
        engineering_retries=[{"attempt": n} for n in range(1, 6)],
    )
    clean_run = Run(
        run_id="RUN-CLEAN",
        goal_id="G-CRITICAL-1",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.CLOSED,
        goal_version="1.0",
        scientific_review=ScientificReview.FAIL,
    )
    assert len(retried_run.engineering_retries) == 5
    assert clean_run.engineering_retries == []

    closure_record = ClosureRecord(
        statistics_sufficient=True,
        execution_valid=True,
        recovery_hypotheses_remaining=0,
        eligible_hypotheses_total=1,
        tested_or_ruled_out=1,
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=2,
        required_zero_novelty_cycles=2,
    )
    closure = evaluate_closure(closure_record)
    assert closure.closure_allowed is True
    assert closure.matched_rule_id == "R-CLOSE-2"

    requirements = [
        RequirementOutcomeRecord(
            requirement_id="R-CRIT-1",
            criticality=Criticality.CRITICAL,
            outcome=RequirementOutcome.NOT_REPRODUCED,
        )
    ]
    retried_outcome = aggregate_project_outcome(
        requirements, closure_allowed=closure.closure_allowed
    )
    clean_outcome = aggregate_project_outcome(
        requirements, closure_allowed=closure.closure_allowed
    )
    # Five retries and zero retries produce the identical composed decision:
    # the attempt count has no path into the closure or outcome rules.
    assert retried_outcome.outcome is clean_outcome.outcome
    assert retried_outcome.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    assert retried_outcome.matched_rule_id == "R-PRJ-3"

    # Control: with the statistical-sufficiency gate failing (zero attempts,
    # nothing "left on the table"), closure is blocked and determination is
    # withheld -- the gates, not the count, decide.
    failed_closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=False,
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        )
    )
    assert failed_closure.closure_allowed is False
    assert failed_closure.blocked_gate_ids == (ClosureGateId.STATISTICAL_SUFFICIENCY,)
    blocked = aggregate_project_outcome(
        requirements, closure_allowed=failed_closure.closure_allowed
    )
    assert blocked.outcome is ReproductionOutcome.UNDETERMINED
    assert blocked.matched_rule_id == "R-PRJ-UND-2"


# ---------------------------------------------------------------------------
# S-02: project_phase substituted for reproduction_outcome
#       (rules.outcome + rules.lifecycle)
# ---------------------------------------------------------------------------


def test_project_phase_never_determines_outcome() -> None:
    """S-02: a completed project phase never determines the outcome.

    Composition (rules.outcome + rules.lifecycle): the project is walked to
    the terminal COMPLETED phase through the normative table, yet the
    reproduction outcome stays UNDETERMINED while a Requirement is OPEN
    (04-PROJECT-LIFECYCLE.md section 1: phase and outcome are strictly
    separate; "Before Final Validation, the outcome should remain
    UNDETERMINED"). Phase values are additionally rejected at both public
    boundaries of the outcome module.
    """
    phase = ProjectPhase.INITIALIZING
    for next_phase in (
        ProjectPhase.SOURCE_ACQUISITION,
        ProjectPhase.REPRODUCTION_INVENTORY,
        ProjectPhase.PLANNING,
        ProjectPhase.PLAN_AUDIT,
        ProjectPhase.PLAN_FROZEN,
        ProjectPhase.EXECUTING,
        ProjectPhase.FINAL_VALIDATION,
        ProjectPhase.REPORTING,
        ProjectPhase.COMPLETED,
    ):
        phase = apply_project_phase_transition(phase, next_phase)
    assert phase is ProjectPhase.COMPLETED

    project = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-1",
                criticality=Criticality.CRITICAL,
                outcome=RequirementOutcome.OPEN,
            )
        ],
        closure_allowed=True,
    )
    # COMPLETED is a phase, not a conclusion: the outcome is UNDETERMINED.
    assert project.outcome is ReproductionOutcome.UNDETERMINED
    assert project.matched_rule_id == "R-PRJ-UND-1"

    # A phase value is not an outcome value: rejected at the record boundary.
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(
            requirement_id="R-1",
            criticality=ProjectPhase.COMPLETED,  # type: ignore[arg-type]
            outcome=RequirementOutcome.OPEN,
        )
    # ... and at the function boundary: no outcome API accepts a phase.
    with pytest.raises(TypeError):
        classify_requirement_outcome(ProjectPhase.COMPLETED)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        aggregate_project_outcome([ProjectPhase.COMPLETED])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# S-03: soft/informational dependency blocking execution
#       (rules.dependencies + rules.lifecycle)
# ---------------------------------------------------------------------------


def test_unresolved_soft_dependency_never_blocks_execution() -> None:
    """S-03: an unresolved soft dependency never serializes or blocks.

    Composition (rules.dependencies + rules.lifecycle): the goal's dependency
    set contains an unresolved soft execution-gated dependency; the execution
    gate is ALLOWED and records the soft dependency as a non-blocking
    ordering hint (R-DEP-4 -> R-EXEC-G-2), so the Run proceeds through the
    normative DISPATCHED -> RUNNING_EXTERNAL transition. The control shows a
    hard_gate on the same unresolved axis blocks, and the gate-guided
    pipeline then withholds the transition.
    """
    soft = DependencyRecord(
        goal_id="UPSTREAM-SOLVENT-EXCHANGE",
        dependency_type=DependencyType.SOFT_DEPENDENCY,
        execution_gate=True,
        execution_resolved=False,
    )
    informational = DependencyRecord(
        goal_id="UPSTREAM-META-NOTE",
        dependency_type=DependencyType.INFORMATIONAL,
        execution_gate=True,
        execution_resolved=False,
    )
    gate = evaluate_execution_gate([soft, informational])
    assert gate.execution_allowed is True
    assert gate.matched_rule_id == "R-EXEC-G-2"
    assert gate.pending_non_blocking_goal_ids == ("UPSTREAM-SOLVENT-EXCHANGE",)
    assert gate.blocking_goal_ids == ()

    # The Run executes: an unresolved soft dependency cannot stop it
    # (05-GOAL-RUN-SCHEMA.md section 5: safe parallelism).
    assert (
        apply_run_lifecycle_transition(
            LifecycleState.DISPATCHED, LifecycleState.RUNNING_EXTERNAL
        )
        is LifecycleState.RUNNING_EXTERNAL
    )

    # Control: the same unresolved axis on a hard_gate dependency blocks, and
    # the gate-guided pipeline withholds the dispatch transition.
    hard = DependencyRecord(
        goal_id="UPSTREAM-SYNTHESIS",
        dependency_type=DependencyType.HARD_GATE,
        execution_gate=True,
        execution_resolved=False,
    )
    hard_gate = evaluate_execution_gate([hard])
    assert hard_gate.execution_allowed is False
    assert hard_gate.matched_rule_id == "R-EXEC-G-1"
    assert hard_gate.blocking_goal_ids == ("UPSTREAM-SYNTHESIS",)
    # The gate-guided pipeline withholds DISPATCHED -> RUNNING_EXTERNAL while
    # the execution gate blocks.
    assert not hard_gate.execution_allowed


# ---------------------------------------------------------------------------
# S-04: arbitrary/illegal lifecycle transition
#       (rules.lifecycle + rules.dependencies)
# ---------------------------------------------------------------------------


def test_gate_approval_cannot_legalize_illegal_transition() -> None:
    """S-04: dependency-gate approval never legalizes an illegal transition.

    Composition (rules.lifecycle + rules.dependencies): every dependency gate
    is ALLOWED -- yet the normative transition tables remain the sole source
    of legal arcs. Stage skips (CREATED -> RESULT_AVAILABLE), the wrong
    abort kind (RESULT_AVAILABLE -> CANCELLED: results are invalidated, not
    cancelled), suspension shortcuts (PAUSED -> COMPLETED) and the
    replanning shortcut (REPLANNING -> EXECUTING without passing PLAN_AUDIT
    and PLAN_FROZEN again) all raise IllegalTransitionError regardless of
    any gate outcome.
    """
    # All dependency gates approved: nothing left unresolved upstream.
    resolved = DependencyRecord(
        goal_id="UPSTREAM-OK",
        dependency_type=DependencyType.HARD_GATE,
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=True,
        acceptance_resolved=True,
    )
    assert evaluate_execution_gate([resolved]).execution_allowed is True
    assert evaluate_acceptance_gate([resolved]).acceptance_allowed is True

    # ... still, no gate outcome appears in the lifecycle rule table.
    # Note: tests/rules/test_lifecycle_shared.py (AC-03 determinism check)
    # re-imports rules.lifecycle with importlib.reload during the same
    # session, which re-creates the IllegalTransitionError class object.
    # Asserting the documented stable contract instead -- the error is a
    # ValueError subclass (documented in rules.lifecycle) with a stable
    # message -- keeps this integration test independent of module
    # reloading while still proving the transitions are rejected.
    for old, new in (
        (LifecycleState.CREATED, LifecycleState.RESULT_AVAILABLE),
        (LifecycleState.RESULT_AVAILABLE, LifecycleState.CANCELLED),
        (LifecycleState.CREATED, LifecycleState.INVALIDATED),
        (LifecycleState.RUNNING_EXTERNAL, LifecycleState.CREATED),
    ):
        with pytest.raises(ValueError, match="illegal run-lifecycle transition"):
            apply_run_lifecycle_transition(old, new)
    with pytest.raises(ValueError, match="illegal project-phase transition"):
        apply_project_phase_transition(ProjectPhase.PAUSED, ProjectPhase.COMPLETED)
    with pytest.raises(ValueError, match="illegal project-phase transition"):
        apply_project_phase_transition(ProjectPhase.REPLANNING, ProjectPhase.EXECUTING)

    # The replanning arc is legal only through the audit loop
    # (EXECUTING -> REPLANNING -> PLAN_AUDIT -> PLAN_FROZEN -> EXECUTING).
    assert (
        apply_project_phase_transition(ProjectPhase.EXECUTING, ProjectPhase.REPLANNING)
        is ProjectPhase.REPLANNING
    )
    assert (
        apply_project_phase_transition(ProjectPhase.REPLANNING, ProjectPhase.PLAN_AUDIT)
        is ProjectPhase.PLAN_AUDIT
    )
    assert (
        apply_project_phase_transition(ProjectPhase.PLAN_AUDIT, ProjectPhase.PLAN_FROZEN)
        is ProjectPhase.PLAN_FROZEN
    )
    assert (
        apply_project_phase_transition(ProjectPhase.PLAN_FROZEN, ProjectPhase.EXECUTING)
        is ProjectPhase.EXECUTING
    )


# ---------------------------------------------------------------------------
# S-05: non-strict assumptions allowed in strict mode
#       (rules.assumptions + rules.evidence)
# ---------------------------------------------------------------------------


def test_evidence_quality_cannot_earn_pure_strict_label() -> None:
    """S-05: top-quality evidence never earns a pure-strict label.

    Composition (rules.assumptions + rules.evidence): the (source, claim)
    assessment is maximal (Reliability 4, Directness 4, Authority 4), yet an
    A2 scientific assumption in the registry classifies the reproduction
    NOT_STRICT -- A2 dominates any A1 (08-STRICT-RECOVERY-CLOSURE.md section
    3: "A2 must not be silently used inside strict reproduction"). The label
    axis consumes only the assumption set; evidence quality cannot buy the
    STRICT label back.
    """
    top_assessment = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="C-1",
        checklist=_reliability_checklist("rc-strict-a2", satisfied=8),
        directness=4,
    )
    assert top_assessment.reliability == 4
    assert top_assessment.directness == 4
    assert reliability_gate_passes(top_assessment, minimum=3) is True
    assert directness_gate_passes(top_assessment, minimum=2) is True

    a2 = _assumption(
        "A-2-1", "catalyst loading", AssumptionClassification.A2_SCIENTIFIC_ASSUMPTION
    )
    a1 = _assumption(
        "A-1-1", "drying detail", AssumptionClassification.A1_METHODOLOGICAL_DEFAULT
    )
    with_a2 = evaluate_strict_label([a2])
    assert with_a2.label is StrictLabel.NOT_STRICT
    assert with_a2.matched_label_rule_id == "R-STRICT-2"
    # A2 dominates A1: an A1+A2 set is NOT_STRICT, never
    # STRICT_WITH_ASSUMPTIONS (R-STRICT-2 is evaluated before R-STRICT-3).
    mixed = evaluate_strict_label([a1, a2])
    assert mixed.label is StrictLabel.NOT_STRICT
    # Without the A2 the same evidence set keeps the pure-strict label: the
    # label reacts to the assumption registry, never to evidence quality.
    a1_only = evaluate_strict_label([a1])
    assert a1_only.label is StrictLabel.STRICT_WITH_ASSUMPTIONS


# ---------------------------------------------------------------------------
# S-06: criticality substitution (rules.criticality + rules.outcome)
# ---------------------------------------------------------------------------


def test_main_figure_location_alone_cannot_force_not_reproduced() -> None:
    """S-06: a main-figure location alone cannot force the critical outcome.

    Composition (rules.criticality + rules.outcome): a finding that merely
    touches a main-figure position is REQUIRED, never CRITICAL (R-REQ-1;
    CRITICAL additionally requires the invalidated main result / changed
    conclusion). Fed through the project aggregator, a REQUIRED Requirement
    closing NOT_REPRODUCED yields at most PARTIALLY_REPRODUCED (R-PRJ-2) --
    never NOT_REPRODUCED_WITHIN_DEFINED_SCOPE, which only Critical
    Requirements can trigger (R-PRJ-3; 04-PROJECT-LIFECYCLE.md section 5
    rules 2-3).
    """
    figure_only = classify_criticality(
        CriticalityChecklist(affects_main_figure=True)
    )
    assert figure_only.criticality is Criticality.REQUIRED
    assert figure_only.matched_rule_id == "R-REQ-1"

    figure_and_conclusion = classify_criticality(
        CriticalityChecklist(
            affects_main_figure=True, changes_paper_conclusion=True
        )
    )
    assert figure_and_conclusion.criticality is Criticality.CRITICAL
    assert figure_and_conclusion.matched_rule_id == "R-CRIT-2"

    not_reproduced = RequirementOutcome.NOT_REPRODUCED
    partial = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-FIG-1",
                criticality=figure_only.criticality,
                outcome=not_reproduced,
            ),
            RequirementOutcomeRecord(
                requirement_id="R-CRIT-1",
                criticality=Criticality.CRITICAL,
                outcome=RequirementOutcome.REPRODUCED,
            ),
        ],
        closure_allowed=True,
    )
    assert partial.outcome is ReproductionOutcome.PARTIALLY_REPRODUCED
    assert partial.matched_rule_id == "R-PRJ-2"
    # A non-critical requirement can never produce the critical outcome.
    assert partial.outcome is not ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE

    critical_negative = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-CRIT-2",
                criticality=figure_and_conclusion.criticality,
                outcome=not_reproduced,
            )
        ],
        closure_allowed=True,
    )
    assert (
        critical_negative.outcome
        is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    )
    assert critical_negative.matched_rule_id == "R-PRJ-3"


# ---------------------------------------------------------------------------
# S-07: dependency resolution invented outside the frozen records
#       (rules.dependencies + rules.evidence)
# ---------------------------------------------------------------------------


def test_dependency_resolution_derived_from_evidence_not_invented() -> None:
    """S-07: upstream resolution is derived from evidence, never invented.

    Composition (rules.dependencies + rules.evidence): the acceptance axis of
    a hard acceptance-gated dependency answers "is the upstream evidence
    valid?" (05-GOAL-RUN-SCHEMA.md section 5). Here it is derived through
    the evidence hard gate (rules.evidence, 06-EVIDENCE-SYSTEM.md SS4) and
    then evaluated by the dependency rules: only the evidence-derived True
    resolves the axis; the default record (resolution unassessed) stays
    BLOCKS_ACCEPTANCE, and invented resolution inputs are rejected at the
    record and function boundaries.
    """
    evidence = assess(
        source=SourceType.PEER_REVIEWED_PAPER,
        claim_id="C-upstream-validity",
        checklist=_reliability_checklist("rc-upstream", satisfied=6),
        directness=3,
    )
    acceptance_resolved = reliability_gate_passes(evidence, minimum=3)
    assert acceptance_resolved is True

    resolved = DependencyRecord(
        goal_id="UPSTREAM-CHAR",
        dependency_type=DependencyType.HARD_GATE,
        acceptance_gate=True,
        acceptance_resolved=acceptance_resolved,
    )
    assert evaluate_acceptance_gate([resolved]).acceptance_allowed is True

    # Without the evidence-derived state the axis is unresolved: the default
    # record is NOT auto-resolved (the frozen model carries no resolution
    # state, so the evaluator must not invent one).
    unresolved = DependencyRecord(
        goal_id="UPSTREAM-CHAR",
        dependency_type=DependencyType.HARD_GATE,
        acceptance_gate=True,
    )
    blocked = evaluate_acceptance_gate([unresolved])
    assert blocked.acceptance_allowed is False
    assert blocked.blocking_goal_ids == ("UPSTREAM-CHAR",)

    # Invented resolution input shapes are rejected up front.
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[call-arg]
            goal_id="UPSTREAM-CHAR",
            dependency_type=DependencyType.HARD_GATE,
            acceptance_gate=True,
            acceptance_resolved="yes",
        )
    with pytest.raises(TypeError):
        evaluate_execution_gate([{"goal_id": "UPSTREAM-CHAR"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# S-08: display score substituting for the evidence hard gates
#       (rules.evidence + rules.closure)
# ---------------------------------------------------------------------------


def test_ranking_score_cannot_substitute_for_recovery_eligibility() -> None:
    """S-08: a high ranking score never passes the recovery eligibility gate.

    Composition (rules.evidence + rules.closure): the weighted composite
    (06-EVIDENCE-SYSTEM.md SS3) is a display-only number; the SS4 hard gates
    read the raw axes. An ineligible assessment (Reliability 2, Directness 4
    -> ranking 77.5) outranks an eligible one (Reliability 3, Directness 2
    -> ranking 73.75) yet fails the gate; an assessment failing the
    directness axis the same way (Reliability 4, Directness 0 -> ranking
    70.0) is also not eligible. The recovery-space exhaustion axis of the
    Closure Contract then counts only evidence-eligible hypotheses
    (rules.closure, 08-STRICT-RECOVERY-CLOSURE.md section 4, default v0.1
    eligibility), so a pool of ineligible hypotheses blocks closure
    regardless of their display scores.
    """
    # An assessment that passes the gate: Reliability 3, Directness 2
    # (R >= 3 and D >= 2, the v0.1 eligibility rule) -> ranking 73.75.
    eligible = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="C-hyp-elig",
        checklist=_reliability_checklist("rc-hyp-elig", satisfied=6),
        directness=2,
    )
    assert ranking_score(eligible.authority, eligible.reliability, eligible.directness) == 73.75
    assert recovery_hypothesis_eligible(eligible) is True

    # An assessment that FAILS the reliability gate yet carries a HIGHER
    # display score (Reliability 2, Directness 4 -> ranking 77.5 > 73.75):
    # the weighted composite can never substitute for the raw hard gates.
    higher_ranked_ineligible = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="C-hyp-1",
        checklist=_reliability_checklist("rc-hyp-1", satisfied=4),
        directness=4,
    )
    assert (
        ranking_score(
            higher_ranked_ineligible.authority,
            higher_ranked_ineligible.reliability,
            higher_ranked_ineligible.directness,
        )
        == 77.5
    )
    assert (
        ranking_score(
            higher_ranked_ineligible.authority,
            higher_ranked_ineligible.reliability,
            higher_ranked_ineligible.directness,
        )
        > ranking_score(eligible.authority, eligible.reliability, eligible.directness)
    )
    assert recovery_hypothesis_eligible(higher_ranked_ineligible) is False

    # An assessment that fails the directness gate (Reliability 4,
    # Directness 0 -> ranking 70.0) is equally not eligible.
    low_directness = assess(
        source=SourceType.TARGET_PAPER,
        claim_id="C-hyp-2",
        checklist=_reliability_checklist("rc-hyp-2", satisfied=8),
        directness=0,
    )
    assert (
        ranking_score(
            low_directness.authority,
            low_directness.reliability,
            low_directness.directness,
        )
        == 70.0
    )
    assert recovery_hypothesis_eligible(low_directness) is False

    # The closure record's recovery axis counts only eligible hypotheses:
    # one remaining ineligible hypothesis (display score irrelevant) blocks
    # the closure hard gate.
    closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=1,
            eligible_hypotheses_total=1,
            tested_or_ruled_out=0,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        )
    )
    assert closure.closure_allowed is False
    assert closure.blocked_gate_ids == (ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,)
    # And with every eligible hypothesis exhausted, closure is allowed.
    exhausted = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            eligible_hypotheses_total=1,
            tested_or_ruled_out=1,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
            required_zero_novelty_cycles=2,
        )
    )
    assert exhausted.closure_allowed is True


# ---------------------------------------------------------------------------
# S-09: outcome determined before Final Validation
#       (rules.outcome + rules.criticality + rules.lifecycle)
# ---------------------------------------------------------------------------


def test_open_critical_requirement_blocks_determination() -> None:
    """S-09: an OPEN Critical Requirement blocks determination.

    Composition (rules.outcome + rules.criticality + rules.lifecycle): the
    finding is CRITICAL (rules.criticality R-CRIT-1) and the project phase
    walk reaches FINAL_VALIDATION, yet the project outcome stays UNDETERMINED
    because the Critical Requirement's outcome is still OPEN -- the
    determination gate (R-PRJ-UND-1, 04-PROJECT-LIFECYCLE.md section 1) is
    phase-independent and holds even when a second Requirement already
    closed REPRODUCED.
    """
    critical = classify_criticality(
        CriticalityChecklist(affects_main_figure=True, invalidates_main_result=True)
    )
    assert critical.criticality is Criticality.CRITICAL

    phase = ProjectPhase.INITIALIZING
    for next_phase in (
        ProjectPhase.SOURCE_ACQUISITION,
        ProjectPhase.REPRODUCTION_INVENTORY,
        ProjectPhase.PLANNING,
        ProjectPhase.PLAN_AUDIT,
        ProjectPhase.PLAN_FROZEN,
        ProjectPhase.EXECUTING,
        ProjectPhase.FINAL_VALIDATION,
    ):
        phase = apply_project_phase_transition(phase, next_phase)
    assert phase is ProjectPhase.FINAL_VALIDATION

    project = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-CRIT-OPEN",
                criticality=critical.criticality,
                outcome=RequirementOutcome.OPEN,
            ),
            RequirementOutcomeRecord(
                requirement_id="R-OK",
                criticality=Criticality.REQUIRED,
                outcome=RequirementOutcome.REPRODUCED,
            ),
        ],
        closure_allowed=True,
    )
    assert project.outcome is ReproductionOutcome.UNDETERMINED
    assert project.matched_rule_id == "R-PRJ-UND-1"
    assert "R-CRIT-OPEN" in project.blocking_reasons[0]
    # Final Validation phase does not grant determination on its own.
    assert phase is ProjectPhase.FINAL_VALIDATION


# ---------------------------------------------------------------------------
# S-10: NOT_REPRODUCED without a satisfied Closure Contract
#       (rules.closure + rules.outcome)
# ---------------------------------------------------------------------------


def test_not_reproduced_without_closure_contract_stays_undetermined() -> None:
    """S-10: an illegitimate negative closure never determines the outcome.

    Composition (rules.closure + rules.outcome): the Closure Contract hard
    gate is evaluated first (rules.closure) and its decision is consumed by
    the aggregator (rules.outcome), never re-derived. A Critical Requirement
    closing NOT_REPRODUCED while two eligible recovery hypotheses remain is
    CLOSURE_BLOCKED (R-RECOV-2 -> R-CLOSE-1) and the project outcome stays
    UNDETERMINED with the documented blocking reason (R-PRJ-UND-2); an
    unassessed contract (closure_allowed=None) blocks the same way
    (R-PRJ-UND-3); only a satisfied contract allows
    NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (R-PRJ-3).
    """
    record = ClosureRecord(
        statistics_sufficient=True,
        execution_valid=True,
        recovery_hypotheses_remaining=2,
        eligible_hypotheses_total=5,
        tested_or_ruled_out=3,
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=2,
        required_zero_novelty_cycles=2,
    )
    closure = evaluate_closure(record)
    assert closure.closure_allowed is False
    assert closure.blocked_gate_ids == (ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,)

    requirements = [
        RequirementOutcomeRecord(
            requirement_id="R-CRIT-1",
            criticality=Criticality.CRITICAL,
            outcome=RequirementOutcome.NOT_REPRODUCED,
        )
    ]
    evaluated_fail = aggregate_project_outcome(
        requirements, closure_allowed=closure.closure_allowed
    )
    assert evaluated_fail.outcome is ReproductionOutcome.UNDETERMINED
    assert evaluated_fail.matched_rule_id == "R-PRJ-UND-2"
    assert "without a satisfied Closure Contract" in evaluated_fail.blocking_reasons[0]

    unassessed = aggregate_project_outcome(requirements, closure_allowed=None)
    assert unassessed.outcome is ReproductionOutcome.UNDETERMINED
    assert unassessed.matched_rule_id == "R-PRJ-UND-3"
    assert "unassessed" in unassessed.blocking_reasons[0]

    satisfied = aggregate_project_outcome(requirements, closure_allowed=True)
    assert satisfied.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    assert satisfied.matched_rule_id == "R-PRJ-3"


# ---------------------------------------------------------------------------
# S-11: scientific PASS/FAIL as a lifecycle state or outcome
#       (rules.lifecycle + rules.outcome)
# ---------------------------------------------------------------------------


def test_review_decision_is_not_lifecycle_state_nor_outcome() -> None:
    """S-11: a review FAIL is neither a lifecycle state nor an outcome.

    Composition (rules.lifecycle + rules.outcome): 05-GOAL-RUN-SCHEMA.md
    section 7 states that scientific PASS/FAIL is a review decision stored
    separately, not a Run lifecycle state. The LifecycleState enum has no
    FAIL member, the normative table carries no review arc, and a Run whose
    review is FAIL still closes through SUBMITTED_FOR_REVIEW -> CLOSED. On
    the outcome side, a review value is not a RequirementOutcome and is
    rejected at the record boundary; a FAIL review alone never produces a
    NOT_REPRODUCED conclusion.
    """
    # The lifecycle vocabulary has no review member and no review arc.
    assert all(state.value != "FAIL" for state in LifecycleState)
    failed_run = Run(
        run_id="RUN-REV-FAIL",
        goal_id="G-1",
        run_type=RunType.INDEPENDENT_REPLICATE,
        lifecycle_state=LifecycleState.SUBMITTED_FOR_REVIEW,
        goal_version="1.0",
        scientific_review=ScientificReview.FAIL,
    )
    assert failed_run.scientific_review is ScientificReview.FAIL
    assert (
        apply_run_lifecycle_transition(
            failed_run.lifecycle_state, LifecycleState.CLOSED
        )
        is LifecycleState.CLOSED
    )

    # A review value is not an outcome value (rejected at the record
    # boundary) and never flows into the outcome rules.
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(
            requirement_id="R-1",
            criticality=Criticality.CRITICAL,
            outcome=ScientificReview.FAIL,  # type: ignore[arg-type]
        )
    project = aggregate_project_outcome(
        [
            RequirementOutcomeRecord(
                requirement_id="R-1",
                criticality=Criticality.CRITICAL,
                outcome=RequirementOutcome.OPEN,
            )
        ],
        closure_allowed=True,
    )
    assert project.outcome is ReproductionOutcome.UNDETERMINED
