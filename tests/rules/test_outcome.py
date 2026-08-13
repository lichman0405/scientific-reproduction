"""Tests for the requirement/project outcome aggregation rules (DEV-M2-G06).

Acceptance coverage (goal contract DEV-M2-G06):
  * AC-01 -- ``project_phase`` never substitutes for ``reproduction_outcome``:
    the outcome vocabularies and the phase vocabulary are disjoint by value;
    the module source never mentions ``ProjectPhase``; no public API accepts
    a phase input (proved by introspection and by passing ``ProjectPhase``
    members into every public entry point -- rejected at the record boundary
    with ``OutcomeRecordError`` and at the function boundary with
    ``TypeError``); and there is no phase-derivation API.
  * AC-02 -- the final outcome remains UNDETERMINED before the final
    validation rules permit determination: a Requirement that has not
    individually passed (outcome OPEN) aggregates to UNDETERMINED and forces
    the project outcome to UNDETERMINED; determination is allowed exactly
    when every Requirement is individually determined (bi-implication over
    the exhaustive grids); the empty requirement set stays UNDETERMINED; the
    blocking reasons report exactly why determination is blocked.
  * AC-03 -- criticality affects project aggregation per the locked
    specification (04-PROJECT-LIFECYCLE.md section 5): only Critical
    Requirements closing NOT_REPRODUCED under a satisfied Closure Contract
    can produce NOT_REPRODUCED_WITHIN_DEFINED_SCOPE; non-critical
    NOT_REPRODUCED can at most produce PARTIALLY_REPRODUCED and never forces
    the critical outcome; a NOT_REPRODUCED closure without a satisfied
    Closure Contract blocks determination.

Also covered: the method reproducibility aggregation hook (deliverable 3,
04-PROJECT-LIFECYCLE.md section 6), composition with the closure rules
(rules.closure) without re-deriving them, determinism, audit-trail
self-consistency, and the TypeError / ValueError paths.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import itertools
import re

import pytest

from scientific_reproduction.core.models import (
    Criticality,
    MethodReproducibility,
    ProjectPhase,
    ReproductionOutcome,
    ReproductionRequirement,
    RequirementOutcome,
)
from scientific_reproduction.core.rules import outcome as outcome_module
from scientific_reproduction.core.rules.closure import (
    ClosureRecord,
    evaluate_closure,
)
from scientific_reproduction.core.rules.outcome import (
    METHOD_REPRODUCIBILITY_ORDER,
    METHOD_REPRODUCIBILITY_RULES,
    PROJECT_OUTCOME_RULES,
    REQUIREMENT_OUTCOME_RULES,
    RULESET_VERSION,
    MethodReproducibilityRecord,
    OutcomeRecordError,
    OutcomeRulesError,
    RequirementClosureState,
    RequirementOutcomeRecord,
    aggregate_method_reproducibility,
    aggregate_project_outcome,
    classify_requirement_outcome,
)

OUTCOMES: tuple[RequirementOutcome, ...] = tuple(RequirementOutcome)
CRITICALITIES: tuple[Criticality, ...] = (
    Criticality.CRITICAL,
    Criticality.REQUIRED,
    Criticality.SUPPORTING,
)
CLOSURE_VALUES: tuple[bool | None, ...] = (True, False, None)


def _record(
    requirement_id: str = "REQ-1",
    criticality: Criticality = Criticality.CRITICAL,
    outcome: RequirementOutcome = RequirementOutcome.REPRODUCED,
) -> RequirementOutcomeRecord:
    """Build a requirement outcome record with explicit, typed arguments."""
    return RequirementOutcomeRecord(
        requirement_id=requirement_id,
        criticality=criticality,
        outcome=outcome,
    )


def _mr_record(
    requirement_id: str = "REQ-1",
    reproducibility: MethodReproducibility = MethodReproducibility.DIRECTLY_REPRODUCIBLE,
) -> MethodReproducibilityRecord:
    """Build a method reproducibility record with explicit, typed arguments."""
    return MethodReproducibilityRecord(
        requirement_id=requirement_id,
        reproducibility=reproducibility,
    )


def _all_single_requirement_sets() -> list[tuple[RequirementOutcomeRecord, ...]]:
    """Every single-requirement input: 5 outcomes x 3 criticalities."""
    return [
        (RequirementOutcomeRecord(requirement_id="REQ-1", criticality=criticality, outcome=outcome),)
        for criticality, outcome in itertools.product(CRITICALITIES, OUTCOMES)
    ]


def _all_two_requirement_sets() -> list[tuple[RequirementOutcomeRecord, ...]]:
    """Every ordered pair of (criticality, outcome): (15 x 15) sets."""
    pairs = [
        (criticality, outcome)
        for criticality, outcome in itertools.product(CRITICALITIES, OUTCOMES)
    ]
    return [
        (
            RequirementOutcomeRecord(
                requirement_id="REQ-A", criticality=first_c, outcome=first_o
            ),
            RequirementOutcomeRecord(
                requirement_id="REQ-B", criticality=second_c, outcome=second_o
            ),
        )
        for (first_c, first_o), (second_c, second_o) in itertools.product(
            pairs, repeat=2
        )
    ]


def _all_three_requirement_criticality_sets() -> list[tuple[RequirementOutcomeRecord, ...]]:
    """Every (CRITICAL, REQUIRED, SUPPORTING) triple over all outcomes: 5^3.

    Covers the mixed-criticality patterns of 04-PROJECT-LIFECYCLE.md
    section 5 (all Criticals reproduced / one Critical NOT_REPRODUCED /
    non-critical NOT_REPRODUCED ...) with each criticality present.
    """
    return [
        (
            RequirementOutcomeRecord(
                requirement_id="REQ-C", criticality=Criticality.CRITICAL, outcome=c_outcome
            ),
            RequirementOutcomeRecord(
                requirement_id="REQ-R", criticality=Criticality.REQUIRED, outcome=r_outcome
            ),
            RequirementOutcomeRecord(
                requirement_id="REQ-S", criticality=Criticality.SUPPORTING, outcome=s_outcome
            ),
        )
        for c_outcome, r_outcome, s_outcome in itertools.product(
            OUTCOMES, repeat=3
        )
    ]


def _all_mr_sets() -> list[tuple[MethodReproducibilityRecord, ...]]:
    """Exhaustive per-requirement ratings grid (single and pairs)."""
    singles = [
        (MethodReproducibilityRecord(requirement_id="REQ-1", reproducibility=rating),)
        for rating in MethodReproducibility
    ]
    pairs = [
        (
            MethodReproducibilityRecord(
                requirement_id="REQ-A", reproducibility=first
            ),
            MethodReproducibilityRecord(
                requirement_id="REQ-B", reproducibility=second
            ),
        )
        for first, second in itertools.product(MethodReproducibility, repeat=2)
    ]
    return singles + pairs


def _expected_requirement_state(outcome: RequirementOutcome) -> RequirementClosureState:
    """The spec-expected requirement closure state (independent re-implementation).

    Encodes 04-PROJECT-LIFECYCLE.md sections 4-5 without consulting any rule
    table: REPRODUCED / REPRODUCED_WITH_RECOVERY are reproduced; NOT_REPRODUCED
    and INCONCLUSIVE map one-to-one; OPEN (not individually determined) stays
    UNDETERMINED.
    """
    if outcome in (
        RequirementOutcome.REPRODUCED,
        RequirementOutcome.REPRODUCED_WITH_RECOVERY,
    ):
        return RequirementClosureState.REPRODUCED
    if outcome is RequirementOutcome.NOT_REPRODUCED:
        return RequirementClosureState.NOT_REPRODUCED
    if outcome is RequirementOutcome.INCONCLUSIVE:
        return RequirementClosureState.INCONCLUSIVE
    return RequirementClosureState.UNDETERMINED  # OPEN


def _expected_project_outcome(
    requirements: tuple[RequirementOutcomeRecord, ...],
    closure_allowed: bool | None,
) -> ReproductionOutcome:
    """The spec-expected project outcome (independent re-implementation).

    Encodes the locked readings without consulting any rule table: no
    determination while a Requirement is OPEN (AC-02); NOT_REPRODUCED
    requires a satisfied Closure Contract; FULLY_REPRODUCED when all
    Requirements are reproduced; PARTIALLY_REPRODUCED when all Criticals are
    reproduced but a non-critical Requirement closes NOT_REPRODUCED;
    NOT_REPRODUCED_WITHIN_DEFINED_SCOPE when a Critical Requirement closes
    NOT_REPRODUCED under closure (AC-03, rule 3 before rule 4); otherwise
    INCONCLUSIVE when any Requirement is validly inconclusive; the empty set
    stays UNDETERMINED.
    """
    if not requirements:
        return ReproductionOutcome.UNDETERMINED
    states = [_expected_requirement_state(r.outcome) for r in requirements]
    if any(state is RequirementClosureState.UNDETERMINED for state in states):
        return ReproductionOutcome.UNDETERMINED
    if (
        any(state is RequirementClosureState.NOT_REPRODUCED for state in states)
        and closure_allowed is not True
    ):
        return ReproductionOutcome.UNDETERMINED
    if all(state is RequirementClosureState.REPRODUCED for state in states):
        return ReproductionOutcome.FULLY_REPRODUCED
    critical_states = [
        state
        for r, state in zip(requirements, states, strict=True)
        if r.criticality is Criticality.CRITICAL
    ]
    non_critical_states = [
        state
        for r, state in zip(requirements, states, strict=True)
        if r.criticality is not Criticality.CRITICAL
    ]
    if (
        all(state is RequirementClosureState.REPRODUCED for state in critical_states)
        and any(
            state is RequirementClosureState.NOT_REPRODUCED
            for state in non_critical_states
        )
    ):
        return ReproductionOutcome.PARTIALLY_REPRODUCED
    if any(
        state is RequirementClosureState.NOT_REPRODUCED for state in critical_states
    ):
        return ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    if any(state is RequirementClosureState.INCONCLUSIVE for state in states):
        return ReproductionOutcome.INCONCLUSIVE
    return ReproductionOutcome.UNDETERMINED


def _expected_method_reproducibility(
    records: tuple[MethodReproducibilityRecord, ...],
) -> MethodReproducibility:
    """The spec-expected method reproducibility (independent re-implementation)."""
    if not records:
        return MethodReproducibility.UNDETERMINED
    ratings = [r.reproducibility for r in records]
    if any(rating is MethodReproducibility.UNDETERMINED for rating in ratings):
        return MethodReproducibility.UNDETERMINED
    if any(rating is MethodReproducibility.INCONCLUSIVE for rating in ratings):
        return MethodReproducibility.INCONCLUSIVE
    return max(
        (rating for rating in ratings),
        key=METHOD_REPRODUCIBILITY_ORDER.index,
    )


# ---------------------------------------------------------------------------
# Rule table shape (deliverables: three rule tables)
# ---------------------------------------------------------------------------


def test_outcome_rulesets_are_versioned_and_total() -> None:
    assert isinstance(RULESET_VERSION, str)
    assert RULESET_VERSION == "1.0"
    all_rule_ids = (
        [rule.rule_id for rule in REQUIREMENT_OUTCOME_RULES]
        + [rule.rule_id for rule in PROJECT_OUTCOME_RULES]
        + [rule.rule_id for rule in METHOD_REPRODUCIBILITY_RULES]
    )
    assert len(all_rule_ids) == len(set(all_rule_ids)), "rule ids unique"
    # Requirement table: five rules (two reproduced, one per other outcome,
    # trailing default for OPEN); the default makes classification total.
    assert len(REQUIREMENT_OUTCOME_RULES) == 5
    assert REQUIREMENT_OUTCOME_RULES[-1].predicate(_record()) is True
    # Project table: two determination gates + the four spec rules + default.
    assert len(PROJECT_OUTCOME_RULES) == 8
    assert PROJECT_OUTCOME_RULES[-1].predicate((), True) is True
    # Method reproducibility table: two non-terminal states + worst-of default.
    assert len(METHOD_REPRODUCIBILITY_RULES) == 3
    assert METHOD_REPRODUCIBILITY_RULES[-1].predicate(()) is True


def test_outcome_rule_ids_are_stable() -> None:
    # Rule ids are part of the auditable contract; frozen by tests so the
    # audit trail is stable across refactors.
    assert [rule.rule_id for rule in REQUIREMENT_OUTCOME_RULES] == [
        "R-REQOUT-1",
        "R-REQOUT-2",
        "R-REQOUT-3",
        "R-REQOUT-4",
        "R-REQOUT-5",
    ]
    assert [rule.rule_id for rule in PROJECT_OUTCOME_RULES] == [
        "R-PRJ-UND-1",
        "R-PRJ-UND-2",
        "R-PRJ-UND-3",
        "R-PRJ-1",
        "R-PRJ-2",
        "R-PRJ-3",
        "R-PRJ-4",
        "R-PRJ-5",
    ]
    assert [rule.rule_id for rule in METHOD_REPRODUCIBILITY_RULES] == [
        "R-MR-1",
        "R-MR-2",
        "R-MR-3",
    ]


# ---------------------------------------------------------------------------
# Input models (frozen, validated, round-trippable)
# ---------------------------------------------------------------------------


def test_outcome_record_is_frozen() -> None:
    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.outcome = RequirementOutcome.NOT_REPRODUCED  # type: ignore[misc]


def test_outcome_record_rejects_empty_requirement_id() -> None:
    with pytest.raises(OutcomeRecordError):
        _record(requirement_id="")
    with pytest.raises(OutcomeRecordError):
        _record(requirement_id="   ")
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(
            requirement_id=123,  # type: ignore[arg-type]
            criticality=Criticality.CRITICAL,
            outcome=RequirementOutcome.REPRODUCED,
        )


def test_outcome_record_rejects_non_enum_values() -> None:
    # The record vocabulary is the frozen enum set: a plain string with the
    # right spelling is still rejected -- nothing is accepted that is not a
    # RequirementOutcome / Criticality member.
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(  # type: ignore[arg-type]
            requirement_id="REQ-1",
            criticality=Criticality.CRITICAL,
            outcome="REPRODUCED",
        )
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(  # type: ignore[arg-type]
            requirement_id="REQ-1",
            criticality="CRITICAL",
            outcome=RequirementOutcome.REPRODUCED,
        )


def test_outcome_record_to_dict_round_trips() -> None:
    for requirements in _all_single_requirement_sets() + _all_two_requirement_sets()[::9]:
        for record in requirements:
            plain = record.to_dict()
            assert set(plain) == {"requirement_id", "criticality", "outcome"}
            assert isinstance(plain["requirement_id"], str)
            assert plain["criticality"] in ("CRITICAL", "REQUIRED", "SUPPORTING")
            assert plain["outcome"] in tuple(o.value for o in OUTCOMES)
            rebuilt = RequirementOutcomeRecord(
                requirement_id=plain["requirement_id"],
                criticality=Criticality(plain["criticality"]),
                outcome=RequirementOutcome(plain["outcome"]),
            )
            assert rebuilt == record


def test_from_reproduction_requirement_maps_the_frozen_model() -> None:
    requirement = ReproductionRequirement(
        requirement_id="REQ-ADS-001",
        statement="Reproduce the 298 K C3H6 isotherm dataset",
        inventory_items=["ITM-1"],
        criticality=Criticality.CRITICAL,
        goal_ids=["GOAL-ADS-C3H6-298K-001"],
        outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY,
        method_reproducibility=MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY,
    )
    record = RequirementOutcomeRecord.from_reproduction_requirement(requirement)
    assert record == _record(
        requirement_id="REQ-ADS-001", outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY
    )
    mr = MethodReproducibilityRecord.from_reproduction_requirement(requirement)
    assert mr == _mr_record(
        requirement_id="REQ-ADS-001",
        reproducibility=MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY,
    )
    # The frozen model's optional method_reproducibility (None) is canonically
    # UNDETERMINED -- never an invented rating.
    unrated = ReproductionRequirement(
        requirement_id="REQ-2",
        statement="s",
        inventory_items=["ITM-2"],
        criticality=Criticality.REQUIRED,
        goal_ids=["GOAL-2"],
        outcome=RequirementOutcome.OPEN,
    )
    assert (
        MethodReproducibilityRecord.from_reproduction_requirement(unrated).reproducibility
        is MethodReproducibility.UNDETERMINED
    )


def test_from_reproduction_requirement_rejects_non_model() -> None:
    with pytest.raises(TypeError):
        RequirementOutcomeRecord.from_reproduction_requirement({"requirement_id": "x"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        MethodReproducibilityRecord.from_reproduction_requirement({"requirement_id": "x"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-01: project_phase never substitutes for reproduction_outcome
# ---------------------------------------------------------------------------


def test_outcome_phase_and_outcome_vocabularies_are_disjoint() -> None:
    # No ProjectPhase value is a valid outcome value (and vice versa): the
    # enums are separate classes with disjoint value sets, so no phase can
    # ever be mistaken for an outcome at the vocabulary level.
    phase_values = {phase.value for phase in ProjectPhase}
    for vocabulary in (ReproductionOutcome, RequirementOutcome, MethodReproducibility):
        assert phase_values & {member.value for member in vocabulary} == set()


def test_outcome_project_phase_rejected_at_record_boundary() -> None:
    # Passing a ProjectPhase where a RequirementOutcome is expected fails the
    # runtime contract at the record boundary (AC-01).
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(
            requirement_id="REQ-1",
            criticality=Criticality.CRITICAL,
            outcome=ProjectPhase.EXECUTING,  # type: ignore[arg-type]
        )
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(
            requirement_id="REQ-1",
            criticality=ProjectPhase.COMPLETED,  # type: ignore[arg-type]
            outcome=RequirementOutcome.REPRODUCED,
        )
    with pytest.raises(OutcomeRecordError):
        MethodReproducibilityRecord(
            requirement_id="REQ-1",
            reproducibility=ProjectPhase.FINAL_VALIDATION,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "outcome",
    [
        ProjectPhase.INITIALIZING,
        ProjectPhase.FINAL_VALIDATION,
        ProjectPhase.COMPLETED,
        ProjectPhase.REPORTING,
        ProjectPhase.PAUSED,
    ],
    ids=["initializing", "final-validation", "completed", "reporting", "paused"],
)
def test_outcome_every_phase_value_is_rejected_as_an_outcome(outcome: ProjectPhase) -> None:
    # No ProjectPhase member -- including FINAL_VALIDATION, the phase the
    # spec associates with determination -- is accepted where an outcome is
    # expected (AC-01). The final validation GATE is encoded through the
    # requirement closure states, never through the phase.
    with pytest.raises(OutcomeRecordError):
        RequirementOutcomeRecord(
            requirement_id="REQ-1",
            criticality=Criticality.CRITICAL,
            outcome=outcome,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "call",
    [
        lambda: classify_requirement_outcome(ProjectPhase.EXECUTING),  # type: ignore[arg-type]
        lambda: aggregate_project_outcome([ProjectPhase.EXECUTING]),  # type: ignore[list-item]
        lambda: aggregate_method_reproducibility([ProjectPhase.EXECUTING]),  # type: ignore[list-item]
    ],
    ids=["classify-requirement-outcome", "aggregate-project-outcome", "aggregate-method-reproducibility"],
)
def test_outcome_project_phase_rejected_at_public_api_boundary(call: object) -> None:
    # The public entry points reject a phase with TypeError: the aggregators
    # accept only their phase-independent record types (AC-01).
    with pytest.raises(TypeError):
        call()  # type: ignore[misc]


def test_outcome_module_never_mentions_project_phase() -> None:
    # The strongest form of type-level separation: no CODE in the module
    # references ProjectPhase or project_phase -- no import, no attribute
    # access, no variable, no call keyword. The check runs on the module AST
    # identifiers only (docstrings and error-message strings are not code and
    # may document the separation), so any occurrence would be executable
    # code that reads or writes a phase.
    tree = ast.parse(inspect.getsource(outcome_module))
    identifiers = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    keywords = {
        node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg is not None
    }
    code_identifiers = identifiers | attributes | imported | keywords
    assert "ProjectPhase" not in code_identifiers
    assert "project_phase" not in code_identifiers


def test_outcome_no_public_api_accepts_a_phase_input() -> None:
    # Introspection over every public function and dataclass: no parameter
    # name, field name or annotation mentions a phase.
    for name, obj in inspect.getmembers(outcome_module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj):
            for parameter in inspect.signature(obj).parameters.values():
                assert "phase" not in parameter.name.lower(), (name, parameter)
                assert "Phase" not in str(parameter.annotation), (name, parameter)
        if dataclasses.is_dataclass(obj):
            for field in dataclasses.fields(obj):
                assert "phase" not in field.name.lower(), (name, field)


def test_outcome_no_phase_derivation_api_exists() -> None:
    # No symbol that looks like "derive outcome from phase" exists.
    public_names = [name for name in dir(outcome_module) if not name.startswith("_")]
    assert not any("phase" in name.lower() for name in public_names)
    shortcut = re.compile(r"(?i)(from|of|by)_?(phase|phase_?to_)")
    for name in public_names:
        assert not shortcut.search(name), name


# ---------------------------------------------------------------------------
# Requirement outcome aggregator (deliverable 1)
# ---------------------------------------------------------------------------


def test_requirement_outcome_mapping_is_total_and_biimplicational() -> None:
    # Over the exhaustive outcome grid: each RequirementOutcome maps to
    # exactly the spec-expected closure state, and the deciding rule is the
    # rule whose proposed state matches.
    for (record,) in _all_single_requirement_sets():
        expected = _expected_requirement_state(record.outcome)
        assessment = classify_requirement_outcome(record)
        assert assessment.state is expected, record
        assert (assessment.matched_rule_id == "R-REQOUT-5") == (
            record.outcome is RequirementOutcome.OPEN
        ), record
        assert assessment.record == record
        assert assessment.ruleset_version == RULESET_VERSION


def test_requirement_outcome_first_matching_rule_wins() -> None:
    for (record,) in _all_single_requirement_sets():
        assessment = classify_requirement_outcome(record)
        matched = next(d for d in assessment.decisions if d.matched)
        assert matched.rule_id == assessment.matched_rule_id
        assert matched.state is assessment.state
        for earlier in assessment.decisions[: assessment.decisions.index(matched)]:
            assert earlier.matched is False
        rule = next(r for r in REQUIREMENT_OUTCOME_RULES if r.rule_id == matched.rule_id)
        assert rule.predicate(record) is True


def test_requirement_open_stays_undetermined_and_reproduced_covers_recovery() -> None:
    # AC-02: a Requirement that has not individually passed (OPEN) remains
    # UNDETERMINED. 04-PROJECT-LIFECYCLE.md section 5 rules 1-2: a
    # REPRODUCED_WITH_RECOVERY Requirement counts as reproduced at the
    # closure-state level.
    assert (
        classify_requirement_outcome(_record(outcome=RequirementOutcome.OPEN)).state
        is RequirementClosureState.UNDETERMINED
    )
    assert (
        classify_requirement_outcome(
            _record(outcome=RequirementOutcome.REPRODUCED_WITH_RECOVERY)
        ).state
        is RequirementClosureState.REPRODUCED
    )
    assert (
        classify_requirement_outcome(
            _record(outcome=RequirementOutcome.NOT_REPRODUCED)
        ).state
        is RequirementClosureState.NOT_REPRODUCED
    )
    assert (
        classify_requirement_outcome(
            _record(outcome=RequirementOutcome.INCONCLUSIVE)
        ).state
        is RequirementClosureState.INCONCLUSIVE
    )


def test_requirement_outcome_is_deterministic() -> None:
    for (record,) in _all_single_requirement_sets():
        reference = classify_requirement_outcome(record)
        for _ in range(25):
            assert classify_requirement_outcome(record) == reference


def test_classify_requirement_outcome_rejects_non_record() -> None:
    with pytest.raises(TypeError):
        classify_requirement_outcome({"outcome": "REPRODUCED"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        classify_requirement_outcome(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-02: final outcome remains UNDETERMINED before final validation rules
# ---------------------------------------------------------------------------


def test_outcome_project_stays_undetermined_while_any_requirement_is_open() -> None:
    # The final validation gate (01-PRODUCT-REQUIREMENTS.md item 15): while a
    # Requirement is still OPEN -- even with every other Requirement
    # reproduced -- the project outcome stays UNDETERMINED.
    assessment = aggregate_project_outcome(
        [
            _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-OPEN", Criticality.REQUIRED, RequirementOutcome.OPEN),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.UNDETERMINED
    assert assessment.determined is False
    assert assessment.matched_rule_id == "R-PRJ-UND-1"
    (reason,) = assessment.blocking_reasons
    assert reason == (
        "Requirement(s) not individually determined, final validation"
        " not reached: REQ-OPEN"
    )


def test_outcome_open_requirement_aggregates_to_undetermined() -> None:
    # AC-02: an OPEN Requirement stays UNDETERMINED at requirement level and
    # forces UNDETERMINED at project level -- the not-individually-passed
    # state propagates compositionally.
    requirement = _record("REQ-1", outcome=RequirementOutcome.OPEN)
    requirement_assessment = classify_requirement_outcome(requirement)
    assert requirement_assessment.state is RequirementClosureState.UNDETERMINED
    project = aggregate_project_outcome([requirement], closure_allowed=True)
    assert project.outcome is ReproductionOutcome.UNDETERMINED
    assert project.requirement_assessments == (requirement_assessment,)


def test_outcome_determination_requires_all_requirements_terminal_biimplication() -> None:
    # Over the exhaustive grids (single, pairs, criticality triples) x the
    # three closure values: the project outcome is determined exactly when no
    # Requirement is OPEN -- and when a NOT_REPRODUCED Requirement exists,
    # additionally only when the Closure Contract is satisfied.
    for requirements in (
        _all_single_requirement_sets()
        + _all_two_requirement_sets()
        + _all_three_requirement_criticality_sets()
    ):
        for closure_allowed in CLOSURE_VALUES:
            expected = _expected_project_outcome(requirements, closure_allowed)
            assessment = aggregate_project_outcome(
                requirements, closure_allowed=closure_allowed
            )
            assert assessment.outcome is expected, (requirements, closure_allowed)
            assert assessment.determined == (expected is not ReproductionOutcome.UNDETERMINED)


def test_outcome_empty_requirement_set_stays_undetermined() -> None:
    # A project with no formally reported Requirements has no basis for a
    # final scientific conclusion (normative reading).
    assessment = aggregate_project_outcome([], closure_allowed=True)
    assert assessment.outcome is ReproductionOutcome.UNDETERMINED
    assert assessment.matched_rule_id == "R-PRJ-5"
    assert assessment.blocking_reasons == ("no formally reported Requirements",)


def test_outcome_determined_outcomes_report_no_blocking_reasons() -> None:
    for requirements in _all_two_requirement_sets()[::17]:
        for closure_allowed in CLOSURE_VALUES:
            assessment = aggregate_project_outcome(
                requirements, closure_allowed=closure_allowed
            )
            if assessment.determined:
                assert assessment.blocking_reasons == ()
            else:
                assert assessment.blocking_reasons, assessment


def test_outcome_blocking_reason_strings_are_stable() -> None:
    # The blocking-reason wording is frozen by tests so downstream actors can
    # rely on the strings.
    open_reason = aggregate_project_outcome(
        [_record("REQ-X", outcome=RequirementOutcome.OPEN)]
    ).blocking_reasons
    assert open_reason == (
        "Requirement(s) not individually determined, final validation"
        " not reached: REQ-X",
    )
    unsatisfied = aggregate_project_outcome(
        [
            _record(
                "REQ-NR",
                Criticality.REQUIRED,
                RequirementOutcome.NOT_REPRODUCED,
            )
        ],
        closure_allowed=False,
    ).blocking_reasons
    assert unsatisfied == (
        "NOT_REPRODUCED Requirement(s) without a satisfied Closure"
        " Contract: REQ-NR",
    )
    unassessed = aggregate_project_outcome(
        [
            _record(
                "REQ-NR",
                Criticality.REQUIRED,
                RequirementOutcome.NOT_REPRODUCED,
            )
        ],
        closure_allowed=None,
    ).blocking_reasons
    assert unassessed == (
        "NOT_REPRODUCED Requirement(s) while the Closure Contract is"
        " unassessed: REQ-NR",
    )


def test_outcome_determination_is_compositional() -> None:
    # Requirement-level classification first, project-level aggregation
    # second: the project assessment's per-Requirement classifications are
    # exactly the requirement aggregator's outputs, in input order.
    requirements = (
        _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
        _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.OPEN),
        _record("REQ-3", Criticality.SUPPORTING, RequirementOutcome.NOT_REPRODUCED),
    )
    assessment = aggregate_project_outcome(requirements, closure_allowed=True)
    assert assessment.requirement_assessments == tuple(
        classify_requirement_outcome(requirement) for requirement in requirements
    )
    assert [a.record.requirement_id for a in assessment.requirement_assessments] == [
        "REQ-1",
        "REQ-2",
        "REQ-3",
    ]


def test_outcome_project_outcome_is_order_independent() -> None:
    # The outcome is a pure set function of the requirement records (the
    # audit trail preserves input order, the outcome does not depend on it).
    for requirements in _all_three_requirement_criticality_sets()[::7]:
        for closure_allowed in CLOSURE_VALUES:
            forward = aggregate_project_outcome(requirements, closure_allowed=closure_allowed)
            backward = aggregate_project_outcome(
                tuple(reversed(requirements)), closure_allowed=closure_allowed
            )
            assert forward.outcome is backward.outcome, requirements
            assert forward.matched_rule_id == backward.matched_rule_id, requirements


# ---------------------------------------------------------------------------
# AC-03: criticality affects project aggregation per the locked spec
# ---------------------------------------------------------------------------


def test_outcome_critical_not_reproduced_with_satisfied_closure_is_not_reproduced_within_scope() -> None:
    # 04-PROJECT-LIFECYCLE.md section 5 rule 3: one or more Critical
    # Requirements close NOT_REPRODUCED under a satisfied Closure Contract ->
    # NOT_REPRODUCED_WITHIN_DEFINED_SCOPE (AC-03).
    assessment = aggregate_project_outcome(
        [
            _record(
                "REQ-CRIT-1",
                Criticality.CRITICAL,
                RequirementOutcome.NOT_REPRODUCED,
            )
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    assert assessment.matched_rule_id == "R-PRJ-3"


def test_outcome_critical_not_reproduced_without_satisfied_closure_stays_undetermined() -> None:
    # 08-STRICT-RECOVERY-CLOSURE.md section 4: a Goal may close
    # NOT_REPRODUCED only under a satisfied Closure Contract. Without it
    # (evaluated failing or unassessed) determination is blocked (AC-02/03).
    for closure_allowed, expected_rule in ((False, "R-PRJ-UND-2"), (None, "R-PRJ-UND-3")):
        assessment = aggregate_project_outcome(
            [
                _record(
                    "REQ-CRIT-1",
                    Criticality.CRITICAL,
                    RequirementOutcome.NOT_REPRODUCED,
                )
            ],
            closure_allowed=closure_allowed,
        )
        assert assessment.outcome is ReproductionOutcome.UNDETERMINED
        assert assessment.matched_rule_id == expected_rule
        assert not assessment.determined


def test_outcome_non_critical_not_reproduced_cannot_force_critical_outcome() -> None:
    # AC-03: a non-critical NOT_REPRODUCED Requirement can never produce
    # NOT_REPRODUCED_WITHIN_DEFINED_SCOPE; with all Criticals reproduced it
    # yields PARTIALLY_REPRODUCED (section 5 rule 2).
    assessment = aggregate_project_outcome(
        [
            _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-REQ", Criticality.REQUIRED, RequirementOutcome.NOT_REPRODUCED),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.PARTIALLY_REPRODUCED
    assert assessment.matched_rule_id == "R-PRJ-2"


def test_outcome_partial_requires_all_criticals_reproduced() -> None:
    # The PARTIALLY_REPRODUCED rule fires only when every Critical
    # Requirement is reproduced; a non-reproduced Critical dominates.
    partial = aggregate_project_outcome(
        [
            _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-SUP", Criticality.SUPPORTING, RequirementOutcome.NOT_REPRODUCED),
        ],
        closure_allowed=True,
    )
    assert partial.outcome is ReproductionOutcome.PARTIALLY_REPRODUCED
    not_partial = aggregate_project_outcome(
        [
            _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.NOT_REPRODUCED),
            _record("REQ-SUP", Criticality.SUPPORTING, RequirementOutcome.NOT_REPRODUCED),
        ],
        closure_allowed=True,
    )
    assert not_partial.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    inconclusive_critical = aggregate_project_outcome(
        [
            _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.INCONCLUSIVE),
            _record("REQ-SUP", Criticality.SUPPORTING, RequirementOutcome.NOT_REPRODUCED),
        ],
        closure_allowed=True,
    )
    assert inconclusive_critical.outcome is ReproductionOutcome.INCONCLUSIVE


def test_outcome_critical_inconclusive_caps_outcome_at_inconclusive() -> None:
    # Section 5 rule 4: any unresolved Critical Requirement INCONCLUSIVE ->
    # the project outcome cannot exceed INCONCLUSIVE.
    assessment = aggregate_project_outcome(
        [
            _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-2", Criticality.CRITICAL, RequirementOutcome.INCONCLUSIVE),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.INCONCLUSIVE
    assert assessment.matched_rule_id == "R-PRJ-4"


def test_outcome_non_critical_inconclusive_caps_outcome_at_inconclusive() -> None:
    # Normative reading (01-PRODUCT-REQUIREMENTS.md item 15 admits "validly
    # inconclusive" Requirements into Final Validation): an INCONCLUSIVE
    # Requirement of any criticality cannot support a positive conclusion.
    assessment = aggregate_project_outcome(
        [
            _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.INCONCLUSIVE),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.INCONCLUSIVE


def test_outcome_critical_not_reproduced_dominates_critical_inconclusive() -> None:
    # Normative reading (rule 3 before rule 4): a determinate negative
    # Critical closure under a satisfied Closure Contract does not "exceed"
    # INCONCLUSIVE -- it is the least favorable conclusion.
    assessment = aggregate_project_outcome(
        [
            _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.NOT_REPRODUCED),
            _record("REQ-2", Criticality.CRITICAL, RequirementOutcome.INCONCLUSIVE),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    assert assessment.matched_rule_id == "R-PRJ-3"


def test_outcome_non_critical_not_reproduced_requires_satisfied_closure_too() -> None:
    # Normative reading (closure gate for every NOT_REPRODUCED closure): a
    # non-critical NOT_REPRODUCED Requirement without a satisfied Closure
    # Contract blocks determination instead of feeding a partial conclusion.
    for closure_allowed, expected_rule in ((False, "R-PRJ-UND-2"), (None, "R-PRJ-UND-3")):
        assessment = aggregate_project_outcome(
            [
                _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
                _record("REQ-SUP", Criticality.SUPPORTING, RequirementOutcome.NOT_REPRODUCED),
            ],
            closure_allowed=closure_allowed,
        )
        assert assessment.outcome is ReproductionOutcome.UNDETERMINED
        assert assessment.matched_rule_id == expected_rule
    # With the Closure Contract satisfied the same input determines.
    allowed = aggregate_project_outcome(
        [
            _record("REQ-CRIT", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-SUP", Criticality.SUPPORTING, RequirementOutcome.NOT_REPRODUCED),
        ],
        closure_allowed=True,
    )
    assert allowed.outcome is ReproductionOutcome.PARTIALLY_REPRODUCED


def test_outcome_closure_flag_is_irrelevant_without_negative_closures() -> None:
    # The Closure Contract only governs negative closures: a fully reproduced
    # project determines regardless of the closure flag.
    requirements = [
        _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
        _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.REPRODUCED_WITH_RECOVERY),
    ]
    for closure_allowed in CLOSURE_VALUES:
        assessment = aggregate_project_outcome(requirements, closure_allowed=closure_allowed)
        assert assessment.outcome is ReproductionOutcome.FULLY_REPRODUCED
        assert assessment.matched_rule_id == "R-PRJ-1"


def test_outcome_criticality_grid_biimplication() -> None:
    # The full (criticality x outcome) single-requirement grid x closure:
    # every outcome is exactly the independent expectation, proving critical
    # and non-critical Requirements are treated differently per section 5.
    for requirements in _all_single_requirement_sets():
        for closure_allowed in CLOSURE_VALUES:
            assessment = aggregate_project_outcome(
                requirements, closure_allowed=closure_allowed
            )
            expected = _expected_project_outcome(requirements, closure_allowed)
            assert assessment.outcome is expected, (requirements, closure_allowed)


# ---------------------------------------------------------------------------
# Composition with the closure rules (DEV-M2-G05) -- consumed, not re-derived
# ---------------------------------------------------------------------------


def test_outcome_composes_with_closure_rules_without_duplicating_them() -> None:
    # The Closure Contract decision comes from rules.closure; the outcome
    # aggregator consumes ``closure_allowed`` and never re-derives it. The
    # S6 scenario (examples/fdm-201/simulated-scenarios.md): recovery
    # exhausted, saturation reached, execution valid, statistics sufficient
    # -> closure allowed -> a Critical NOT_REPRODUCED Requirement yields
    # NOT_REPRODUCED_WITHIN_DEFINED_SCOPE.
    closure = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
        )
    )
    assert closure.closure_allowed is True
    assessment = aggregate_project_outcome(
        [
            _record(
                "REQ-ADS-001",
                Criticality.CRITICAL,
                RequirementOutcome.NOT_REPRODUCED,
            )
        ],
        closure_allowed=closure.closure_allowed,
    )
    assert assessment.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    # The same requirement with a blocked closure stays UNDETERMINED: the
    # aggregator does not second-guess or duplicate the closure axes.
    blocked = evaluate_closure(
        ClosureRecord(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=1,
        )
    )
    assert blocked.closure_allowed is False
    blocked_assessment = aggregate_project_outcome(
        [_record("REQ-ADS-001", Criticality.CRITICAL, RequirementOutcome.NOT_REPRODUCED)],
        closure_allowed=blocked.closure_allowed,
    )
    assert blocked_assessment.outcome is ReproductionOutcome.UNDETERMINED
    assert blocked_assessment.matched_rule_id == "R-PRJ-UND-2"


# ---------------------------------------------------------------------------
# Spec scenarios (04-PROJECT-LIFECYCLE.md section 5; 18-TEST-AND-ACCEPTANCE-PLAN)
# ---------------------------------------------------------------------------


def test_lifecycle_spec_rule1_all_reproduced_is_fully_reproduced() -> None:
    assessment = aggregate_project_outcome(
        [
            _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.REPRODUCED),
            _record(
                "REQ-3",
                Criticality.SUPPORTING,
                RequirementOutcome.REPRODUCED_WITH_RECOVERY,
            ),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert assessment.matched_rule_id == "R-PRJ-1"


def test_acceptance_plan_scenario_a_strict_success() -> None:
    # 18-TEST-AND-ACCEPTANCE-PLAN.md Scenario A: valid independent Runs,
    # analysis supports frozen acceptance -> every Requirement reproduced ->
    # FULLY_REPRODUCED, no recovery in the audit chain.
    assessment = aggregate_project_outcome(
        [
            _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
            _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.REPRODUCED),
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.FULLY_REPRODUCED
    assert all(
        a.matched_rule_id in ("R-REQOUT-1",)
        for a in assessment.requirement_assessments
    )


def test_acceptance_plan_scenario_b_recovery_success() -> None:
    # 18-TEST-AND-ACCEPTANCE-PLAN.md Scenario B: strict failure -> diagnosis
    # -> research -> recovery -> success. Scientific Requirement
    # REPRODUCED_WITH_RECOVERY; the project is FULLY_REPRODUCED and the
    # method reproducibility is lower than direct reproducibility.
    requirements = (
        _record(
            "REQ-ADS-001",
            Criticality.CRITICAL,
            RequirementOutcome.REPRODUCED_WITH_RECOVERY,
        ),
        _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.REPRODUCED),
    )
    project = aggregate_project_outcome(requirements, closure_allowed=True)
    assert project.outcome is ReproductionOutcome.FULLY_REPRODUCED
    ratings = aggregate_method_reproducibility(
        (
            _mr_record(
                "REQ-ADS-001",
                MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY,
            ),
            _mr_record("REQ-2", MethodReproducibility.DIRECTLY_REPRODUCIBLE),
        )
    )
    assert ratings.reproducibility is MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY


def test_acceptance_plan_scenario_c_non_reproduced_closure() -> None:
    # 18-TEST-AND-ACCEPTANCE-PLAN.md Scenario C: strict failure statistically
    # sufficient, QC valid, hypotheses exhausted, saturation met -> Closure
    # Contract satisfied -> NOT_REPRODUCED Requirement and project
    # aggregation behavior.
    assessment = aggregate_project_outcome(
        [
            _record(
                "REQ-ADS-001",
                Criticality.CRITICAL,
                RequirementOutcome.NOT_REPRODUCED,
            )
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.NOT_REPRODUCED_WITHIN_DEFINED_SCOPE
    # Without a satisfied Closure Contract the same inputs stay UNDETERMINED.
    blocked = aggregate_project_outcome(
        [
            _record(
                "REQ-ADS-001",
                Criticality.CRITICAL,
                RequirementOutcome.NOT_REPRODUCED,
            )
        ],
        closure_allowed=False,
    )
    assert blocked.outcome is ReproductionOutcome.UNDETERMINED


def test_acceptance_plan_scenario_d_inconclusive_statistics() -> None:
    # 18-TEST-AND-ACCEPTANCE-PLAN.md Scenario D: CI too wide / sample size
    # insufficient -> INCONCLUSIVE Requirement -> project outcome cannot
    # exceed INCONCLUSIVE; no PASS/FAIL shortcut.
    assessment = aggregate_project_outcome(
        [
            _record(
                "REQ-ADS-001",
                Criticality.CRITICAL,
                RequirementOutcome.INCONCLUSIVE,
            )
        ],
        closure_allowed=True,
    )
    assert assessment.outcome is ReproductionOutcome.INCONCLUSIVE
    assert assessment.determined is True


def test_fdm201_project_example_begins_undetermined() -> None:
    # examples/fdm-201/project.example.yaml: the project starts at PLANNING
    # with reproduction_outcome UNDETERMINED -- the phase and the outcome are
    # separate fields and the outcome is not derived from the phase.
    example = (
        "project_id: RP-FDM201-001",
        "project_phase: PLANNING",
        "reproduction_outcome: UNDETERMINED",
    )
    assert any("project_phase: PLANNING" in line for line in example)
    assert any("reproduction_outcome: UNDETERMINED" in line for line in example)
    # A single OPEN Requirement (the pre-execution state) aggregates to
    # UNDETERMINED regardless of any imagined phase.
    assessment = aggregate_project_outcome(
        [_record("REQ-ADS-001", Criticality.CRITICAL, RequirementOutcome.OPEN)],
        closure_allowed=None,
    )
    assert assessment.outcome is ReproductionOutcome.UNDETERMINED


# ---------------------------------------------------------------------------
# Method reproducibility aggregation hook (deliverable 3)
# ---------------------------------------------------------------------------


def test_method_reproducibility_any_undetermined_is_undetermined() -> None:
    # 04-PROJECT-LIFECYCLE.md section 6 + determination-gate reading: an
    # unrated Requirement means the project-level rating cannot be
    # determined.
    assessment = aggregate_method_reproducibility(
        [
            _mr_record("REQ-1", MethodReproducibility.DIRECTLY_REPRODUCIBLE),
            _mr_record("REQ-2", MethodReproducibility.UNDETERMINED),
        ]
    )
    assert assessment.reproducibility is MethodReproducibility.UNDETERMINED
    assert assessment.matched_rule_id == "R-MR-1"


def test_method_reproducibility_any_inconclusive_is_inconclusive() -> None:
    assessment = aggregate_method_reproducibility(
        [
            _mr_record("REQ-1", MethodReproducibility.NOT_REPRODUCIBLE),
            _mr_record("REQ-2", MethodReproducibility.INCONCLUSIVE),
        ]
    )
    assert assessment.reproducibility is MethodReproducibility.INCONCLUSIVE
    assert assessment.matched_rule_id == "R-MR-2"


def test_method_reproducibility_worst_of_over_exhaustive_grid() -> None:
    # Normative reading: the project-level rating is the worst (least
    # reproducible) terminal per-Requirement rating, over the exhaustive
    # single- and pair-ratings grid (bi-implication).
    for records in _all_mr_sets():
        expected = _expected_method_reproducibility(records)
        assessment = aggregate_method_reproducibility(records)
        assert assessment.reproducibility is expected, records
        assert (assessment.matched_rule_id == "R-MR-3") == (
            expected not in (
                MethodReproducibility.UNDETERMINED,
                MethodReproducibility.INCONCLUSIVE,
            )
        ), records


def test_method_reproducibility_empty_set_is_undetermined() -> None:
    assessment = aggregate_method_reproducibility([])
    assert assessment.reproducibility is MethodReproducibility.UNDETERMINED
    assert assessment.matched_rule_id == "R-MR-3"


def test_method_reproducibility_is_separate_from_scientific_outcome() -> None:
    # Section 6: method reproducibility is a separate axis from the
    # scientific outcome -- the records carry no outcome field, and the same
    # project outcome vocabulary works with different reproducibility
    # ratings.
    assert {field.name for field in dataclasses.fields(MethodReproducibilityRecord)} == {
        "requirement_id",
        "reproducibility",
    }
    direct = aggregate_method_reproducibility(
        [_mr_record("REQ-1", MethodReproducibility.DIRECTLY_REPRODUCIBLE)]
    )
    redesigned = aggregate_method_reproducibility(
        [_mr_record("REQ-1", MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN)]
    )
    assert direct.reproducibility is MethodReproducibility.DIRECTLY_REPRODUCIBLE
    assert redesigned.reproducibility is MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN
    # The same project outcome can accompany either rating.
    outcome = aggregate_project_outcome(
        [_record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED)],
        closure_allowed=True,
    )
    assert outcome.outcome is ReproductionOutcome.FULLY_REPRODUCED


def test_method_reproducibility_is_deterministic_and_auditable() -> None:
    for records in _all_mr_sets()[::3]:
        reference = aggregate_method_reproducibility(records)
        for _ in range(25):
            assert aggregate_method_reproducibility(records) == reference
        assert reference.records == records
        assert reference.ruleset_version == RULESET_VERSION
        assert [d.rule_id for d in reference.decisions] == [
            rule.rule_id for rule in METHOD_REPRODUCIBILITY_RULES
        ]
        matched = next(d for d in reference.decisions if d.matched)
        assert matched.rule_id == reference.matched_rule_id
        assert matched.reproducibility is reference.reproducibility


def test_method_reproducibility_rejects_non_sequence_and_non_records() -> None:
    with pytest.raises(TypeError):
        aggregate_method_reproducibility("REQ-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        aggregate_method_reproducibility([{"requirement_id": "REQ-1"}])  # type: ignore[list-item]


# ---------------------------------------------------------------------------
# Audit trails, determinism, TypeError paths
# ---------------------------------------------------------------------------


def test_outcome_assessment_records_exact_inputs() -> None:
    requirements = (
        _record("REQ-1", Criticality.CRITICAL, RequirementOutcome.REPRODUCED),
        _record("REQ-2", Criticality.REQUIRED, RequirementOutcome.NOT_REPRODUCED),
    )
    assessment = aggregate_project_outcome(requirements, closure_allowed=True)
    assert assessment.ruleset_version == RULESET_VERSION
    assert assessment.requirements == requirements
    assert assessment.closure_allowed is True
    assert assessment.outcome is ReproductionOutcome.PARTIALLY_REPRODUCED


def test_outcome_assessment_records_every_rule_decision() -> None:
    assessment = aggregate_project_outcome(
        [_record("REQ-1", Criticality.CRITICAL, RequirementOutcome.OPEN)],
        closure_allowed=True,
    )
    assert [d.rule_id for d in assessment.rule_decisions] == [
        rule.rule_id for rule in PROJECT_OUTCOME_RULES
    ]
    for decision, rule in zip(assessment.rule_decisions, PROJECT_OUTCOME_RULES, strict=True):
        assert decision.rule_id == rule.rule_id
        assert decision.description == rule.description
        assert decision.outcome == rule.outcome


def test_outcome_matched_rule_is_the_first_true_predicate() -> None:
    for requirements in (
        _all_single_requirement_sets()
        + _all_two_requirement_sets()[::13]
        + _all_three_requirement_criticality_sets()[::11]
    ):
        for closure_allowed in CLOSURE_VALUES:
            assessment = aggregate_project_outcome(
                requirements, closure_allowed=closure_allowed
            )
            matched = next(
                d for d in assessment.rule_decisions if d.matched
            )
            assert matched.rule_id == assessment.matched_rule_id
            assert matched.outcome is assessment.outcome
            for earlier in assessment.rule_decisions[: assessment.rule_decisions.index(matched)]:
                assert earlier.matched is False
            rule = next(
                r for r in PROJECT_OUTCOME_RULES if r.rule_id == matched.rule_id
            )
            assert rule.predicate(assessment.requirement_assessments, closure_allowed) is True


def test_outcome_evaluation_is_deterministic() -> None:
    for requirements in _all_two_requirement_sets()[::7]:
        for closure_allowed in CLOSURE_VALUES:
            reference = aggregate_project_outcome(
                requirements, closure_allowed=closure_allowed
            )
            for _ in range(25):
                assert (
                    aggregate_project_outcome(requirements, closure_allowed=closure_allowed)
                    == reference
                )


def test_outcome_closure_flag_wrong_type_rejected() -> None:
    with pytest.raises(TypeError):
        aggregate_project_outcome([_record()], closure_allowed=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        aggregate_project_outcome([_record()], closure_allowed="yes")  # type: ignore[arg-type]


def test_outcome_project_aggregate_rejects_non_sequence_and_non_records() -> None:
    with pytest.raises(TypeError):
        aggregate_project_outcome(_record())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        aggregate_project_outcome("REQ-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        aggregate_project_outcome([{"requirement_id": "REQ-1"}])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        aggregate_project_outcome([None])  # type: ignore[list-item]


def test_errors_are_value_error_subclasses() -> None:
    # Stable error hierarchy: rule-engine errors derive from ValueError.
    assert issubclass(OutcomeRecordError, OutcomeRulesError)
    assert issubclass(OutcomeRulesError, ValueError)
