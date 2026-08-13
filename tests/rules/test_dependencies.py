"""Tests for dependency / execution-gate / acceptance-gate rules (DEV-M2-G02).

Acceptance coverage (goal contract DEV-M2-G02):
  * AC-01 -- hard gate blocks execution when unresolved: an unresolved
    ``hard_gate`` dependency with ``execution_gate`` set makes the execution
    gate BLOCKED and is listed in ``blocking_goal_ids``; resolved, it is
    ALLOWED. Proved by the named tests plus the exhaustive-grid
    bi-implication: execution BLOCKED holds exactly when at least one
    dependency is in a ``BLOCKS_EXECUTION*`` state.
  * AC-02 -- soft/informational dependencies do not incorrectly serialize
    the DAG: unresolved soft and informational dependencies never block
    execution or acceptance (the gate outcomes stay ALLOWED and the
    blocking lists stay empty); an unresolved soft dependency is recorded
    only as a non-blocking ordering hint (``pending_non_blocking_goal_ids``)
    and an informational dependency as ``INFORMATIONAL``. A hard dependency
    without ``execution_gate`` likewise never serializes execution. Proved
    by the named tests plus the exhaustive-grid bi-implication over every
    ``DependencyType``.
  * AC-03 -- acceptance can remain blocked after execution is allowed: an
    execution-eligible dependency set (no execution-blocking state) can still
    block acceptance -- whether via an acceptance-only hard gate (the
    FDM-201 pattern of ``17-FDM201-REFERENCE-CASE.md``) or via an upstream
    whose execution state is reached while its evidence is not yet valid.
    Proved by the named tests and the grid invariant that both gates are
    independent axes.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from scientific_reproduction.core.models import DependencyType, GoalDependency
from scientific_reproduction.core.rules.dependencies import (
    ACCEPTANCE_GATE_RULES,
    ACCEPTANCE_OUTCOME_RULES,
    DEPENDENCY_STATE_RULES,
    EXECUTION_GATE_RULES,
    EXECUTION_OUTCOME_RULES,
    RULESET_VERSION,
    AcceptanceGateOutcome,
    DependencyRecord,
    DependencyRecordError,
    DependencyRulesError,
    DependencyState,
    ExecutionGateOutcome,
    evaluate_acceptance_gate,
    evaluate_dependency,
    evaluate_execution_gate,
)

DEPENDENCY_TYPES: tuple[DependencyType, ...] = (
    DependencyType.HARD_GATE,
    DependencyType.SOFT_DEPENDENCY,
    DependencyType.INFORMATIONAL,
)

_EXEC_BLOCKING_STATES = frozenset(
    {
        DependencyState.BLOCKS_EXECUTION,
        DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE,
    }
)
_ACC_BLOCKING_STATES = frozenset(
    {
        DependencyState.BLOCKS_ACCEPTANCE,
        DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE,
    }
)


def _record(
    goal_id: str = "UP-1",
    dependency_type: DependencyType = DependencyType.HARD_GATE,
    execution_gate: bool = False,
    acceptance_gate: bool = False,
    execution_resolved: bool = False,
    acceptance_resolved: bool = False,
) -> DependencyRecord:
    """Build a dependency record with explicit, typed keyword arguments."""
    return DependencyRecord(
        goal_id=goal_id,
        dependency_type=dependency_type,
        execution_gate=execution_gate,
        acceptance_gate=acceptance_gate,
        execution_resolved=execution_resolved,
        acceptance_resolved=acceptance_resolved,
    )


def _all_records() -> list[DependencyRecord]:
    """Exhaustive battery: every dependency record (3 x 2^4 = 48)."""
    return [
        _record(
            goal_id=f"UP-{index}",
            dependency_type=kind,
            execution_gate=execution_gate,
            acceptance_gate=acceptance_gate,
            execution_resolved=execution_resolved,
            acceptance_resolved=acceptance_resolved,
        )
        for index, (
            kind,
            execution_gate,
            acceptance_gate,
            execution_resolved,
            acceptance_resolved,
        ) in enumerate(
            itertools.product(
                DEPENDENCY_TYPES,
                (False, True),
                (False, True),
                (False, True),
                (False, True),
            )
        )
    ]


def _expected_state(record: DependencyRecord) -> DependencyState:
    """The spec-expected dependency state (independent re-implementation).

    Encodes the normative reading of ``05-GOAL-RUN-SCHEMA.md`` section 5:
    only a hard dependency converts an unresolved gated axis into a block;
    soft dependencies are ordering hints at most; informational dependencies
    are inert.
    """
    if record.dependency_type is DependencyType.HARD_GATE:
        blocks_execution = record.execution_gate and not record.execution_resolved
        blocks_acceptance = record.acceptance_gate and not record.acceptance_resolved
        if blocks_execution and blocks_acceptance:
            return DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE
        if blocks_execution:
            return DependencyState.BLOCKS_EXECUTION
        if blocks_acceptance:
            return DependencyState.BLOCKS_ACCEPTANCE
        return DependencyState.SATISFIED
    if record.dependency_type is DependencyType.SOFT_DEPENDENCY:
        gated_axis_unresolved = (
            record.execution_gate and not record.execution_resolved
        ) or (record.acceptance_gate and not record.acceptance_resolved)
        return (
            DependencyState.ORDERING_ONLY
            if gated_axis_unresolved
            else DependencyState.SATISFIED
        )
    return DependencyState.INFORMATIONAL


def _expected_blocking(
    record: DependencyRecord, state: DependencyState
) -> tuple[bool, bool]:
    """Expected (blocks_execution, blocks_acceptance) for one dependency."""
    return state in _EXEC_BLOCKING_STATES, state in _ACC_BLOCKING_STATES


# ---------------------------------------------------------------------------
# Rule table shape (deliverable: three ordered rule tables)
# ---------------------------------------------------------------------------


def test_ruleset_is_versioned_and_total() -> None:
    assert isinstance(RULESET_VERSION, str)
    assert RULESET_VERSION == "1.0"

    state_rule_ids = [rule.rule_id for rule in DEPENDENCY_STATE_RULES]
    assert len(state_rule_ids) == len(set(state_rule_ids)), "rule ids unique"
    assert len(DEPENDENCY_STATE_RULES) == 6
    for rule in DEPENDENCY_STATE_RULES:
        assert isinstance(rule.state, DependencyState)
        assert rule.description
    # The trailing default rule matches every record, so classification is
    # total: every record yields exactly one of the six enum values.
    assert DEPENDENCY_STATE_RULES[-1].predicate(_record()) is True

    exec_ids = [rule.rule_id for rule in EXECUTION_GATE_RULES]
    assert len(exec_ids) == len(set(exec_ids))
    assert len(EXECUTION_GATE_RULES) == 2
    assert EXECUTION_GATE_RULES[-1].predicate(DependencyState.SATISFIED) is True

    outcome_ids = [rule.rule_id for rule in EXECUTION_OUTCOME_RULES]
    assert len(outcome_ids) == len(set(outcome_ids))
    assert len(EXECUTION_OUTCOME_RULES) == 2
    assert EXECUTION_OUTCOME_RULES[-1].predicate(()) is True

    acc_ids = [rule.rule_id for rule in ACCEPTANCE_GATE_RULES]
    assert len(acc_ids) == len(set(acc_ids))
    assert len(ACCEPTANCE_GATE_RULES) == 2
    assert ACCEPTANCE_GATE_RULES[-1].predicate(DependencyState.SATISFIED) is True

    acc_outcome_ids = [rule.rule_id for rule in ACCEPTANCE_OUTCOME_RULES]
    assert len(acc_outcome_ids) == len(set(acc_outcome_ids))
    assert len(ACCEPTANCE_OUTCOME_RULES) == 2
    assert ACCEPTANCE_OUTCOME_RULES[-1].predicate(()) is True


# ---------------------------------------------------------------------------
# Input model (frozen, validated, round-trippable)
# ---------------------------------------------------------------------------


def test_dependency_record_is_frozen() -> None:
    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.goal_id = "OTHER"  # type: ignore[misc]


def test_dependency_record_to_dict_round_trips() -> None:
    for record in _all_records():
        plain = record.to_dict()
        assert set(plain) == {
            "goal_id",
            "dependency_type",
            "execution_gate",
            "acceptance_gate",
            "execution_resolved",
            "acceptance_resolved",
        }
        assert plain["dependency_type"] == record.dependency_type.value
        rebuilt = DependencyRecord(
            goal_id=plain["goal_id"],
            dependency_type=DependencyType(plain["dependency_type"]),
            execution_gate=plain["execution_gate"],
            acceptance_gate=plain["acceptance_gate"],
            execution_resolved=plain["execution_resolved"],
            acceptance_resolved=plain["acceptance_resolved"],
        )
        assert rebuilt == record


def test_dependency_record_rejects_empty_goal_id() -> None:
    with pytest.raises(DependencyRecordError):
        DependencyRecord(goal_id="", dependency_type=DependencyType.HARD_GATE)
    with pytest.raises(DependencyRecordError):
        DependencyRecord(goal_id="   ", dependency_type=DependencyType.HARD_GATE)


def test_dependency_record_rejects_non_string_goal_id() -> None:
    with pytest.raises(DependencyRecordError):
        DependencyRecord(goal_id=42, dependency_type=DependencyType.HARD_GATE)  # type: ignore[arg-type]


def test_dependency_record_rejects_non_dependency_type() -> None:
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[arg-type]
            goal_id="UP-1", dependency_type="hard_gate"
        )
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[arg-type]
            goal_id="UP-1", dependency_type=42
        )


def test_dependency_record_rejects_non_bool_flags() -> None:
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[arg-type]
            goal_id="UP-1",
            dependency_type=DependencyType.HARD_GATE,
            execution_gate=1,
        )
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[arg-type]
            goal_id="UP-1",
            dependency_type=DependencyType.HARD_GATE,
            acceptance_gate="yes",
        )
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[arg-type]
            goal_id="UP-1",
            dependency_type=DependencyType.HARD_GATE,
            execution_resolved=None,
        )
    with pytest.raises(DependencyRecordError):
        DependencyRecord(  # type: ignore[arg-type]
            goal_id="UP-1",
            dependency_type=DependencyType.HARD_GATE,
            acceptance_resolved="yes",
        )


def test_from_goal_dependency_maps_the_frozen_model() -> None:
    model = GoalDependency(
        goal_id="GOAL-SYN-FDM201-001",
        type=DependencyType.HARD_GATE,
        execution_gate=False,
        acceptance_gate=True,
    )
    record = DependencyRecord.from_goal_dependency(
        model, execution_resolved=True, acceptance_resolved=False
    )
    assert record == _record(
        goal_id="GOAL-SYN-FDM201-001",
        dependency_type=DependencyType.HARD_GATE,
        acceptance_gate=True,
        execution_resolved=True,
    )
    # Defaults: resolution is never invented by the mapping.
    assert DependencyRecord.from_goal_dependency(model) == _record(
        goal_id="GOAL-SYN-FDM201-001",
        dependency_type=DependencyType.HARD_GATE,
        acceptance_gate=True,
    )


def test_from_goal_dependency_rejects_non_model() -> None:
    with pytest.raises(TypeError):
        DependencyRecord.from_goal_dependency({"goal_id": "x"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Dependency evaluator: total state over the exhaustive grid
# ---------------------------------------------------------------------------


def test_state_matches_spec_biimplication_over_exhaustive_grid() -> None:
    # The dependency evaluator is exactly the normative reading for every
    # one of the 48 records: BLOCKS_EXECUTION* exactly when a hard
    # execution-gated axis is unresolved; BLOCKS_ACCEPTANCE* exactly when a
    # hard acceptance-gated axis is unresolved; ORDERING_ONLY exactly for
    # soft dependencies with an unresolved gated axis; INFORMATIONAL exactly
    # for informational dependencies; SATISFIED otherwise.
    for record in _all_records():
        expected = _expected_state(record)
        assessment = evaluate_dependency(record)
        assert assessment.state == expected, record
        if record.dependency_type is DependencyType.HARD_GATE:
            assert (
                assessment.state
                in (DependencyState.BLOCKS_EXECUTION, DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE)
            ) == (
                record.execution_gate and not record.execution_resolved
            )
            assert (
                assessment.state
                in (DependencyState.BLOCKS_ACCEPTANCE, DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE)
            ) == (
                record.acceptance_gate and not record.acceptance_resolved
            )
        if record.dependency_type is DependencyType.SOFT_DEPENDENCY:
            assert (
                assessment.state is DependencyState.ORDERING_ONLY
            ) == (
                (record.execution_gate and not record.execution_resolved)
                or (record.acceptance_gate and not record.acceptance_resolved)
            )
        if record.dependency_type is DependencyType.INFORMATIONAL:
            assert assessment.state is DependencyState.INFORMATIONAL


def test_state_full_enum_coverage_over_grid() -> None:
    states = {
        evaluate_dependency(record).state for record in _all_records()
    }
    assert states == set(DependencyState)


def test_first_matching_state_rule_wins() -> None:
    # A record that satisfies every hard rule in the table: the FIRST
    # matching rule (R-DEP-1, both axes unresolved) decides, deterministically.
    record = _record(
        goal_id="UP-1",
        dependency_type=DependencyType.HARD_GATE,
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=False,
        acceptance_resolved=False,
    )
    assessment = evaluate_dependency(record)
    assert assessment.state == DependencyState.BLOCKS_EXECUTION_AND_ACCEPTANCE
    assert assessment.matched_rule_id == "R-DEP-1"


def test_dependency_evaluation_is_deterministic() -> None:
    for record in _all_records():
        reference = evaluate_dependency(record)
        for _ in range(25):
            assert evaluate_dependency(record) == reference


def test_dependency_assessment_records_exact_input() -> None:
    for record in _all_records():
        assessment = evaluate_dependency(record)
        assert assessment.ruleset_version == RULESET_VERSION
        assert assessment.dependency == record


def test_dependency_assessment_records_every_rule_decision() -> None:
    record = _record(execution_gate=True, execution_resolved=False)
    assessment = evaluate_dependency(record)
    recorded_ids = [decision.rule_id for decision in assessment.decisions]
    assert recorded_ids == [rule.rule_id for rule in DEPENDENCY_STATE_RULES]
    for decision, rule in zip(
        assessment.decisions, DEPENDENCY_STATE_RULES, strict=True
    ):
        assert decision.rule_id == rule.rule_id
        assert decision.description == rule.description
        assert decision.state == rule.state


def test_dependency_matched_rule_is_the_first_true_predicate() -> None:
    for record in _all_records():
        assessment = evaluate_dependency(record)
        matched = next(
            decision for decision in assessment.decisions if decision.matched
        )
        assert matched.rule_id == assessment.matched_rule_id
        for earlier in assessment.decisions[: assessment.decisions.index(matched)]:
            assert earlier.matched is False
        rule = next(
            r for r in DEPENDENCY_STATE_RULES if r.rule_id == matched.rule_id
        )
        assert rule.predicate(record) is True
        assert rule.state == assessment.state


# ---------------------------------------------------------------------------
# AC-01: hard gate blocks execution when unresolved
# ---------------------------------------------------------------------------


def test_hard_execution_gate_unresolved_blocks_execution() -> None:
    record = _record(execution_gate=True, execution_resolved=False)
    assessment = evaluate_execution_gate([record])
    assert assessment.execution_allowed is False
    assert assessment.outcome is ExecutionGateOutcome.BLOCKED
    assert assessment.blocking_goal_ids == ("UP-1",)
    # The dependency state and per-dependency decision agree.
    decision = assessment.decisions[0]
    assert decision.state is DependencyState.BLOCKS_EXECUTION
    assert decision.blocks_execution is True
    assert decision.rule_id == "R-EXEC-1"


def test_hard_execution_gate_unresolved_blocks_combined_both_axes() -> None:
    record = _record(
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=False,
        acceptance_resolved=False,
    )
    assessment = evaluate_execution_gate([record])
    assert assessment.outcome is ExecutionGateOutcome.BLOCKED
    assert assessment.blocking_goal_ids == ("UP-1",)
    assert assessment.decisions[0].rule_id == "R-EXEC-1"


def test_hard_execution_gate_resolved_allows_execution() -> None:
    record = _record(execution_gate=True, execution_resolved=True)
    assessment = evaluate_execution_gate([record])
    assert assessment.execution_allowed is True
    assert assessment.outcome is ExecutionGateOutcome.ALLOWED
    assert assessment.blocking_goal_ids == ()
    assert assessment.decisions[0].rule_id == "R-EXEC-2"


def test_hard_execution_gate_multiple_blocking_dependencies_all_listed() -> None:
    first = _record(goal_id="UP-A", execution_gate=True)
    second = _record(goal_id="UP-B", execution_gate=True)
    soft = _record(
        goal_id="UP-SOFT",
        dependency_type=DependencyType.SOFT_DEPENDENCY,
        execution_gate=True,
    )
    assessment = evaluate_execution_gate([first, second, soft])
    assert assessment.outcome is ExecutionGateOutcome.BLOCKED
    assert assessment.blocking_goal_ids == ("UP-A", "UP-B")


def test_ungated_hard_dependency_never_blocks_execution() -> None:
    # A hard_gate dependency with no execution gate flag gates nothing
    # (schema-legal shape): it must not serialize execution even unresolved.
    record = _record()  # hard, both gates False, both axes unresolved
    assessment = evaluate_execution_gate([record])
    assert assessment.execution_allowed is True
    assert assessment.blocking_goal_ids == ()
    assert evaluate_dependency(record).state is DependencyState.SATISFIED


# ---------------------------------------------------------------------------
# AC-02: soft/informational dependencies do not incorrectly serialize the DAG
# ---------------------------------------------------------------------------


def test_soft_unresolved_dependency_does_not_block_execution() -> None:
    record = _record(
        dependency_type=DependencyType.SOFT_DEPENDENCY,
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=False,
        acceptance_resolved=False,
    )
    assessment = evaluate_execution_gate([record])
    assert assessment.execution_allowed is True
    assert assessment.outcome is ExecutionGateOutcome.ALLOWED
    assert assessment.blocking_goal_ids == ()
    # The soft dependency is recorded as a non-blocking ordering hint.
    assert assessment.pending_non_blocking_goal_ids == ("UP-1",)
    assert evaluate_dependency(record).state is DependencyState.ORDERING_ONLY
    assert assessment.decisions[0].blocks_execution is False
    assert assessment.decisions[0].rule_id == "R-EXEC-2"


def test_informational_unresolved_dependency_does_not_block_execution() -> None:
    record = _record(
        dependency_type=DependencyType.INFORMATIONAL,
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=False,
        acceptance_resolved=False,
    )
    assessment = evaluate_execution_gate([record])
    assert assessment.execution_allowed is True
    assert assessment.blocking_goal_ids == ()
    assert assessment.pending_non_blocking_goal_ids == ()
    assert evaluate_dependency(record).state is DependencyState.INFORMATIONAL


def test_soft_and_informational_do_not_serialize_the_dag() -> None:
    # A dependent goal whose upstreams are an unresolved soft dependency, an
    # unresolved informational dependency and a satisfied hard dependency
    # may still execute: nothing forces sequentialization (AC-02).
    soft = _record(
        goal_id="UP-PXRD",
        dependency_type=DependencyType.SOFT_DEPENDENCY,
        execution_gate=True,
        execution_resolved=False,
    )
    informational = _record(
        goal_id="UP-CIF",
        dependency_type=DependencyType.INFORMATIONAL,
    )
    satisfied_hard = _record(
        goal_id="UP-SYN", execution_gate=True, execution_resolved=True
    )
    assessment = evaluate_execution_gate([soft, informational, satisfied_hard])
    assert assessment.execution_allowed is True
    assert assessment.outcome is ExecutionGateOutcome.ALLOWED
    assert assessment.blocking_goal_ids == ()
    assert assessment.pending_non_blocking_goal_ids == ("UP-PXRD",)
    assert assessment.matched_rule_id == "R-EXEC-G-2"


def test_hard_dependency_without_execution_gate_allows_parallel_execution() -> None:
    # "This allows safe parallelism without invalidating final evidence"
    # (05-GOAL-RUN-SCHEMA.md section 5): a hard acceptance-only gate must
    # not serialize execution -- the FDM-201 isotherm pattern
    # (examples/fdm-201/goal.example.yaml: execution_gate false,
    # acceptance_gate true).
    record = _record(
        goal_id="GOAL-SYN-FDM201-001",
        execution_gate=False,
        acceptance_gate=True,
        execution_resolved=False,
        acceptance_resolved=False,
    )
    assessment = evaluate_execution_gate([record])
    assert assessment.execution_allowed is True
    assert assessment.blocking_goal_ids == ()
    assert evaluate_dependency(record).state is DependencyState.BLOCKS_ACCEPTANCE


def test_soft_dependency_never_blocks_acceptance() -> None:
    record = _record(
        dependency_type=DependencyType.SOFT_DEPENDENCY,
        acceptance_gate=True,
        acceptance_resolved=False,
    )
    acceptance = evaluate_acceptance_gate([record])
    assert acceptance.acceptance_allowed is True
    assert acceptance.blocking_goal_ids == ()
    assert acceptance.pending_non_blocking_goal_ids == ("UP-1",)
    execution = evaluate_execution_gate([record])
    assert execution.execution_allowed is True


def test_informational_dependency_gate_flags_are_inert() -> None:
    # Informational dependencies carry no gating and no ordering weight: the
    # flags are inert and the dependency is recorded as INFORMATIONAL even
    # when its upstream is unresolved.
    record = _record(
        dependency_type=DependencyType.INFORMATIONAL,
        execution_gate=True,
        acceptance_gate=True,
    )
    assert evaluate_dependency(record).state is DependencyState.INFORMATIONAL
    execution = evaluate_execution_gate([record])
    acceptance = evaluate_acceptance_gate([record])
    assert execution.execution_allowed is True
    assert execution.pending_non_blocking_goal_ids == ()
    assert acceptance.acceptance_allowed is True
    assert acceptance.pending_non_blocking_goal_ids == ()


def test_empty_dependency_set_allows_both_gates() -> None:
    execution = evaluate_execution_gate([])
    acceptance = evaluate_acceptance_gate([])
    assert execution.execution_allowed is True
    assert execution.matched_rule_id == "R-EXEC-G-2"
    assert acceptance.acceptance_allowed is True
    assert acceptance.matched_rule_id == "R-ACC-G-2"


# ---------------------------------------------------------------------------
# AC-03: acceptance can remain blocked after execution is allowed
# ---------------------------------------------------------------------------


def test_acceptance_blocked_while_execution_allowed_acceptance_only_gate() -> None:
    # The FDM-201 isotherm pattern (17-FDM201-REFERENCE-CASE.md: "BET
    # acceptance may require a hard acceptance gate on sample identity even
    # if measurement execution was started earlier"): the dependency gates
    # acceptance only, so execution is allowed while acceptance stays
    # blocked.
    record = _record(
        goal_id="GOAL-PXRD-IDENTITY-001",
        execution_gate=False,
        acceptance_gate=True,
        execution_resolved=False,
        acceptance_resolved=False,
    )
    execution = evaluate_execution_gate([record])
    acceptance = evaluate_acceptance_gate([record])
    assert execution.execution_allowed is True
    assert execution.outcome is ExecutionGateOutcome.ALLOWED
    assert acceptance.acceptance_allowed is False
    assert acceptance.outcome is AcceptanceGateOutcome.BLOCKED
    assert acceptance.blocking_goal_ids == ("GOAL-PXRD-IDENTITY-001",)
    assert acceptance.decisions[0].blocks_acceptance is True
    assert acceptance.decisions[0].rule_id == "R-ACC-1"
    assert acceptance.matched_rule_id == "R-ACC-G-1"


def test_acceptance_blocked_while_execution_allowed_upstream_execution_reached() -> None:
    # Even a dependency that gates BOTH axes can show the split: the upstream
    # execution state is reached (measurement may run) while its evidence is
    # not yet valid (acceptance stays blocked) -- the BET/sample-identity
    # case of 17-FDM201-REFERENCE-CASE.md.
    record = _record(
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=True,
        acceptance_resolved=False,
    )
    execution = evaluate_execution_gate([record])
    acceptance = evaluate_acceptance_gate([record])
    assert execution.execution_allowed is True
    assert execution.decisions[0].rule_id == "R-EXEC-2"
    assert acceptance.acceptance_allowed is False
    assert acceptance.blocking_goal_ids == ("UP-1",)
    assert evaluate_dependency(record).state is DependencyState.BLOCKS_ACCEPTANCE


def test_acceptance_allowed_when_all_gated_axes_resolved() -> None:
    record = _record(
        execution_gate=True,
        acceptance_gate=True,
        execution_resolved=True,
        acceptance_resolved=True,
    )
    acceptance = evaluate_acceptance_gate([record])
    assert acceptance.acceptance_allowed is True
    assert acceptance.outcome is AcceptanceGateOutcome.ALLOWED
    assert acceptance.blocking_goal_ids == ()
    assert acceptance.matched_rule_id == "R-ACC-G-2"
    assert evaluate_dependency(record).state is DependencyState.SATISFIED


def test_acceptance_blocked_reports_all_blocking_goal_ids() -> None:
    first = _record(goal_id="UP-A", acceptance_gate=True)
    second = _record(goal_id="UP-B", execution_gate=True, acceptance_gate=True)
    hard_exec_only = _record(goal_id="UP-C", execution_gate=True)
    assessment = evaluate_acceptance_gate([first, second, hard_exec_only])
    assert assessment.acceptance_allowed is False
    # UP-C blocks execution only, so it never appears in the acceptance list.
    assert assessment.blocking_goal_ids == ("UP-A", "UP-B")


def test_gates_are_independent_axes_over_the_exhaustive_grid() -> None:
    # AC-03 grid invariant: execution and acceptance are independent axes --
    # every combination (exec allowed / blocked) x (acceptance allowed /
    # blocked) is produced by some dependency record.
    combos = {
        (
            evaluate_execution_gate([record]).execution_allowed,
            evaluate_acceptance_gate([record]).acceptance_allowed,
        )
        for record in _all_records()
    }
    assert combos == {
        (True, True),
        (True, False),  # AC-03: execution allowed, acceptance blocked
        (False, True),
        (False, False),
    }


# ---------------------------------------------------------------------------
# Gate evaluators: bi-implications, determinism, audit trails
# ---------------------------------------------------------------------------


def test_execution_gate_matches_state_biimplication_over_grid() -> None:
    for record in _all_records():
        state = _expected_state(record)
        expected_exec_blocked, expected_acc_blocked = _expected_blocking(
            record, state
        )
        execution = evaluate_execution_gate([record])
        assert execution.decisions[0].state == state
        assert (execution.decisions[0].blocks_execution) == expected_exec_blocked
        assert (execution.outcome is ExecutionGateOutcome.BLOCKED) == (
            state in _EXEC_BLOCKING_STATES
        )
        assert execution.execution_allowed == (state not in _EXEC_BLOCKING_STATES)


def test_acceptance_gate_matches_state_biimplication_over_grid() -> None:
    for record in _all_records():
        state = _expected_state(record)
        expected_exec_blocked, expected_acc_blocked = _expected_blocking(
            record, state
        )
        acceptance = evaluate_acceptance_gate([record])
        assert acceptance.decisions[0].state == state
        assert (
            acceptance.decisions[0].blocks_acceptance
        ) == expected_acc_blocked
        assert (acceptance.outcome is AcceptanceGateOutcome.BLOCKED) == (
            state in _ACC_BLOCKING_STATES
        )
        assert acceptance.acceptance_allowed == (
            state not in _ACC_BLOCKING_STATES
        )


def test_multi_dependency_gate_outcomes_are_compositional() -> None:
    # With several dependencies the gates are the conjunction of the
    # per-dependency decisions: blocked exactly when at least one dependency
    # blocks that axis.
    for records in (
        [
            _record(goal_id="UP-A", execution_gate=True, execution_resolved=True),
            _record(
                goal_id="UP-B",
                dependency_type=DependencyType.SOFT_DEPENDENCY,
                execution_gate=True,
            ),
            _record(goal_id="UP-C", execution_gate=True),
        ],
        [
            _record(
                goal_id="UP-A",
                dependency_type=DependencyType.INFORMATIONAL,
                execution_gate=True,
            ),
            _record(
                goal_id="UP-B", acceptance_gate=True, acceptance_resolved=True
            ),
        ],
    ):
        execution = evaluate_execution_gate(records)
        acceptance = evaluate_acceptance_gate(records)
        assert execution.outcome is ExecutionGateOutcome.BLOCKED or execution.outcome is ExecutionGateOutcome.ALLOWED
        assert execution.execution_allowed == all(
            decision.blocks_execution is False for decision in execution.decisions
        )
        assert acceptance.acceptance_allowed == all(
            decision.blocks_acceptance is False for decision in acceptance.decisions
        )


def test_gate_evaluation_is_deterministic_across_repeated_evaluations() -> None:
    sample = _all_records()[::3]
    for record in sample:
        execution_reference = evaluate_execution_gate([record])
        acceptance_reference = evaluate_acceptance_gate([record])
        for _ in range(50):
            assert evaluate_execution_gate([record]) == execution_reference
            assert evaluate_acceptance_gate([record]) == acceptance_reference


def test_gate_outcome_is_order_independent() -> None:
    records = [
        _record(
            goal_id="UP-A",
            dependency_type=DependencyType.SOFT_DEPENDENCY,
            execution_gate=True,
        ),
        _record(goal_id="UP-B", execution_gate=True),
        _record(
            goal_id="UP-C",
            dependency_type=DependencyType.INFORMATIONAL,
        ),
    ]
    for permutation in itertools.permutations(records):
        forward = evaluate_execution_gate(permutation)
        backward = evaluate_execution_gate(tuple(reversed(permutation)))
        # The outcome (and the set of blockers) must not depend on order;
        # only the id-list order follows the declared input order.
        assert forward.outcome is backward.outcome
        assert set(forward.blocking_goal_ids) == set(backward.blocking_goal_ids)
        assert set(forward.pending_non_blocking_goal_ids) == set(
            backward.pending_non_blocking_goal_ids
        )


def test_execution_gate_assessment_records_exact_inputs() -> None:
    records = (
        _record(goal_id="UP-A", execution_gate=True),
        _record(
            goal_id="UP-B",
            dependency_type=DependencyType.INFORMATIONAL,
        ),
    )
    assessment = evaluate_execution_gate(records)
    assert assessment.ruleset_version == RULESET_VERSION
    assert assessment.dependencies == records


def test_execution_gate_assessment_records_every_decision() -> None:
    records = (
        _record(goal_id="UP-A", execution_gate=True),
        _record(
            goal_id="UP-B",
            dependency_type=DependencyType.SOFT_DEPENDENCY,
            acceptance_gate=True,
        ),
    )
    assessment = evaluate_execution_gate(records)
    assert [d.rule_id for d in assessment.decisions] == ["R-EXEC-1", "R-EXEC-2"]
    assert [d.blocks_execution for d in assessment.decisions] == [True, False]
    outcome_ids = [d.rule_id for d in assessment.outcome_decisions]
    assert outcome_ids == [
        rule.rule_id for rule in EXECUTION_OUTCOME_RULES
    ]
    # The aggregate decision trace matches the per-dependency trace.
    matched = next(
        d for d in assessment.outcome_decisions if d.matched
    )
    assert matched.rule_id == assessment.matched_rule_id
    assert matched.outcome is assessment.outcome


def test_acceptance_gate_assessment_records_every_decision() -> None:
    records = (
        _record(goal_id="UP-A", acceptance_gate=True),
        _record(goal_id="UP-B", acceptance_gate=True, acceptance_resolved=True),
    )
    assessment = evaluate_acceptance_gate(records)
    assert [d.rule_id for d in assessment.decisions] == ["R-ACC-1", "R-ACC-2"]
    assert [d.blocks_acceptance for d in assessment.decisions] == [True, False]
    outcome_ids = [d.rule_id for d in assessment.outcome_decisions]
    assert outcome_ids == [
        rule.rule_id for rule in ACCEPTANCE_OUTCOME_RULES
    ]
    matched = next(
        d for d in assessment.outcome_decisions if d.matched
    )
    assert matched.rule_id == assessment.matched_rule_id
    assert matched.outcome is assessment.outcome
    assert assessment.blocking_goal_ids == ("UP-A",)


def test_gate_matched_rule_is_the_first_true_predicate() -> None:
    records = [
        _record(goal_id="UP-A", execution_gate=True),
        _record(
            goal_id="UP-B",
            dependency_type=DependencyType.SOFT_DEPENDENCY,
            execution_gate=True,
        ),
    ]
    execution = evaluate_execution_gate(records)
    matched = next(
        d for d in execution.outcome_decisions if d.matched
    )
    for earlier in execution.outcome_decisions[: execution.outcome_decisions.index(matched)]:
        assert earlier.matched is False

    blocked = evaluate_execution_gate([_record(execution_gate=True)])
    blocked_matched = next(
        d for d in blocked.outcome_decisions if d.matched
    )
    assert blocked_matched.rule_id == "R-EXEC-G-1"

    allowed = evaluate_execution_gate([])
    allowed_matched = next(
        d for d in allowed.outcome_decisions if d.matched
    )
    assert allowed_matched.rule_id == "R-EXEC-G-2"

    acceptance = evaluate_acceptance_gate([_record(acceptance_gate=True)])
    acc_matched = next(
        d for d in acceptance.outcome_decisions if d.matched
    )
    assert acc_matched.rule_id == "R-ACC-G-1"


# ---------------------------------------------------------------------------
# TypeError paths (wrong-typed public inputs)
# ---------------------------------------------------------------------------


def test_evaluate_dependency_rejects_non_record() -> None:
    with pytest.raises(TypeError):
        evaluate_dependency({"goal_id": "UP-1"})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_dependency(None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_input",
    [
        42,
        "UP-1",
        b"UP-1",
        None,
        {"goal_id": "UP-1"},
    ],
    ids=["int", "str", "bytes", "none", "dict"],
)
def test_evaluate_execution_gate_rejects_non_sequence(bad_input: object) -> None:
    with pytest.raises(TypeError):
        evaluate_execution_gate(bad_input)  # type: ignore[arg-type]


def test_evaluate_execution_gate_rejects_non_record_elements() -> None:
    with pytest.raises(TypeError):
        evaluate_execution_gate(["UP-1"])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        evaluate_execution_gate([_record(), 42])  # type: ignore[list-item]


@pytest.mark.parametrize(
    "bad_input",
    [
        42,
        "UP-1",
        b"UP-1",
        None,
        {"goal_id": "UP-1"},
    ],
    ids=["int", "str", "bytes", "none", "dict"],
)
def test_evaluate_acceptance_gate_rejects_non_sequence(bad_input: object) -> None:
    with pytest.raises(TypeError):
        evaluate_acceptance_gate(bad_input)  # type: ignore[arg-type]


def test_evaluate_acceptance_gate_rejects_non_record_elements() -> None:
    with pytest.raises(TypeError):
        evaluate_acceptance_gate(["UP-1"])  # type: ignore[list-item]
    with pytest.raises(TypeError):
        evaluate_acceptance_gate([_record(), 42])  # type: ignore[list-item]


def test_errors_are_value_error_subclasses() -> None:
    # Stable error hierarchy: rule-engine errors derive from ValueError.
    assert issubclass(DependencyRecordError, DependencyRulesError)
    assert issubclass(DependencyRulesError, ValueError)
