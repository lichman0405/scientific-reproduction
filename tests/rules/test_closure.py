"""Tests for the closure hard-gate rules (DEV-M2-G05).

Acceptance coverage (goal contract DEV-M2-G05):
  * AC-01 -- closure fails when ANY mandatory gate is unresolved: a gate
    that is not satisfied -- whether evaluated-and-failing (NOT_SATISFIED)
    or unknown (UNRESOLVED) -- blocks closure, the deciding aggregate rule
    is R-CLOSE-1, and the assessment reports every blocking gate in
    ``closure_reasons`` / ``blocked_gate_ids``. Proved by the named tests
    plus the exhaustive-grid bi-implication: CLOSURE_ALLOWED holds exactly
    when all four mandatory gates are SATISFIED.
  * AC-02 -- no fixed failure-count shortcut can close a required goal: the
    input record has no failure/attempt counter, the rule table is exactly
    the two-rule conjunction table (R-CLOSE-1 / R-CLOSE-2) with no
    failure-count predicate, and the exhaustive grid proves the closure
    decision is a pure function of the four mandatory axes -- nothing else
    can shortcut a required goal into closure.
  * AC-03 -- eligible recovery hypotheses remaining > 0 prevent
    non-reproduced closure: remaining > 0 blocks closure and the reason
    reports the exact count; the pool exhausted (remaining == 0) and all
    other axes satisfied allows closure. Proved by the named tests plus the
    recovery-axis bi-implication over the exhaustive remaining-count grid.
"""

from __future__ import annotations

import dataclasses
import itertools
import re

import pytest

from scientific_reproduction.core.models import (
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
)
from scientific_reproduction.core.rules.closure import (
    CLOSURE_RULES,
    EXECUTION_VALIDITY_RULES,
    GATE_RULE_TABLES,
    MANDATORY_GATES,
    RECOVERY_EXHAUSTION_RULES,
    RESEARCH_SATURATION_RULES,
    RULESET_VERSION,
    STATISTICAL_SUFFICIENCY_RULES,
    ClosureAxisState,
    ClosureGateId,
    ClosureOutcome,
    ClosureRecord,
    ClosureRecordError,
    ClosureRulesError,
    evaluate_closure,
)


def _record(
    statistics_sufficient: bool | None = True,
    execution_valid: bool | None = True,
    recovery_hypotheses_remaining: int | None = 0,
    eligible_hypotheses_total: int | None = 0,
    tested_or_ruled_out: int | None = 0,
    required_search_families_completed: bool | None = True,
    consecutive_zero_novelty_cycles: int | None = 2,
    required_zero_novelty_cycles: int = 2,
) -> ClosureRecord:
    """Build a closure record with explicit, typed keyword arguments.

    The defaults form a fully-satisfied record: every mandatory gate is
    SATISFIED, so ``evaluate_closure(_record())`` is CLOSURE_ALLOWED.
    """
    return ClosureRecord(
        statistics_sufficient=statistics_sufficient,
        execution_valid=execution_valid,
        recovery_hypotheses_remaining=recovery_hypotheses_remaining,
        eligible_hypotheses_total=eligible_hypotheses_total,
        tested_or_ruled_out=tested_or_ruled_out,
        required_search_families_completed=required_search_families_completed,
        consecutive_zero_novelty_cycles=consecutive_zero_novelty_cycles,
        required_zero_novelty_cycles=required_zero_novelty_cycles,
    )


def _all_records() -> list[ClosureRecord]:
    """Exhaustive battery over the four mandatory axes (3 x 3 x 4 x 3 x 5 x 2).

    Tri-states for the boolean axes, remaining-count values covering
    exhausted / remaining / unassessed, cycle counts covering
    not-counted / zero / below-rule / at-rule / above-rule, and two
    configured saturation rules (1 and the schema default 2).
    """
    return [
        ClosureRecord(
            statistics_sufficient=statistics_sufficient,
            execution_valid=execution_valid,
            recovery_hypotheses_remaining=remaining,
            required_search_families_completed=families,
            consecutive_zero_novelty_cycles=cycles,
            required_zero_novelty_cycles=required,
        )
        for (
            statistics_sufficient,
            execution_valid,
            remaining,
            families,
            cycles,
            required,
        ) in itertools.product(
            (True, False, None),
            (True, False, None),
            (None, 0, 1, 2),
            (True, False, None),
            (None, 0, 1, 2, 5),
            (1, 2),
        )
    ]


def _expected_gate_state(
    record: ClosureRecord, gate_id: ClosureGateId
) -> ClosureAxisState:
    """The spec-expected gate state (independent re-implementation).

    Encodes the normative readings of ``08-STRICT-RECOVERY-CLOSURE.md``
    section 4 and ``09-RESEARCH-SUBSYSTEM.md`` section 7 without consulting
    any rule table: recovery exhaustion holds exactly when the pool is
    exhausted; saturation requires family completion plus the configured
    zero-novelty rule; unresolved (None) inputs stay UNRESOLVED.
    """
    if gate_id is ClosureGateId.STATISTICAL_SUFFICIENCY:
        if record.statistics_sufficient is True:
            return ClosureAxisState.SATISFIED
        if record.statistics_sufficient is False:
            return ClosureAxisState.NOT_SATISFIED
        return ClosureAxisState.UNRESOLVED
    if gate_id is ClosureGateId.EXECUTION_VALIDITY:
        if record.execution_valid is True:
            return ClosureAxisState.SATISFIED
        if record.execution_valid is False:
            return ClosureAxisState.NOT_SATISFIED
        return ClosureAxisState.UNRESOLVED
    if gate_id is ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION:
        if record.recovery_hypotheses_remaining == 0:
            return ClosureAxisState.SATISFIED
        if record.recovery_hypotheses_remaining is not None:
            return ClosureAxisState.NOT_SATISFIED
        return ClosureAxisState.UNRESOLVED
    # RESEARCH_SATURATION
    if (
        record.required_search_families_completed is True
        and record.consecutive_zero_novelty_cycles is not None
        and record.consecutive_zero_novelty_cycles
        >= record.required_zero_novelty_cycles
    ):
        return ClosureAxisState.SATISFIED
    if record.required_search_families_completed is False or (
        record.consecutive_zero_novelty_cycles is not None
        and record.consecutive_zero_novelty_cycles
        < record.required_zero_novelty_cycles
    ):
        return ClosureAxisState.NOT_SATISFIED
    return ClosureAxisState.UNRESOLVED


def _expected_outcome(
    record: ClosureRecord,
) -> tuple[ClosureOutcome, tuple[ClosureGateId, ...]]:
    """Expected (outcome, blocked gate ids) for one record."""
    blocked = tuple(
        gate
        for gate in MANDATORY_GATES
        if _expected_gate_state(record, gate) is not ClosureAxisState.SATISFIED
    )
    return (
        ClosureOutcome.CLOSURE_BLOCKED if blocked else ClosureOutcome.CLOSURE_ALLOWED,
        blocked,
    )


# ---------------------------------------------------------------------------
# Rule table shape (deliverable: closure evaluator + reason reporting)
# ---------------------------------------------------------------------------


def test_closure_ruleset_is_versioned_and_total() -> None:
    assert isinstance(RULESET_VERSION, str)
    assert RULESET_VERSION == "1.0"

    all_rule_ids = [rule.rule_id for rule in STATISTICAL_SUFFICIENCY_RULES]
    all_rule_ids += [rule.rule_id for rule in EXECUTION_VALIDITY_RULES]
    all_rule_ids += [rule.rule_id for rule in RECOVERY_EXHAUSTION_RULES]
    all_rule_ids += [rule.rule_id for rule in RESEARCH_SATURATION_RULES]
    all_rule_ids += [rule.rule_id for rule in CLOSURE_RULES]
    assert len(all_rule_ids) == len(set(all_rule_ids)), "rule ids unique"

    # Four per-gate tables, one per mandatory gate, each with a trailing
    # default rule so every gate state is total.
    assert tuple(gate_id for gate_id, _ in GATE_RULE_TABLES) == MANDATORY_GATES
    assert tuple(MANDATORY_GATES) == (
        ClosureGateId.STATISTICAL_SUFFICIENCY,
        ClosureGateId.EXECUTION_VALIDITY,
        ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
        ClosureGateId.RESEARCH_SATURATION,
    )
    for gate_id, rules in GATE_RULE_TABLES:
        assert isinstance(gate_id, ClosureGateId)
        assert len(rules) == 3
        for rule in rules:
            assert isinstance(rule.state, ClosureAxisState)
            assert rule.description
        assert rules[-1].predicate(_record()) is True

    # The aggregate table: exactly the conjunction pair, total by default.
    assert len(CLOSURE_RULES) == 2
    assert CLOSURE_RULES[-1].predicate(()) is True


def test_closure_mandatory_gates_match_the_objective() -> None:
    # The four mandatory gates are exactly the axes named in the objective
    # (statistics sufficiency, valid execution, recovery hypothesis
    # exhaustion, research saturation) -- no more, no less.
    assert set(MANDATORY_GATES) == {
        ClosureGateId.STATISTICAL_SUFFICIENCY,
        ClosureGateId.EXECUTION_VALIDITY,
        ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
        ClosureGateId.RESEARCH_SATURATION,
    }


# ---------------------------------------------------------------------------
# Input model (frozen, validated, round-trippable)
# ---------------------------------------------------------------------------


def test_closure_record_is_frozen() -> None:
    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.execution_valid = False  # type: ignore[misc]


def test_closure_record_to_dict_round_trips() -> None:
    for record in _all_records()[::7]:
        plain = record.to_dict()
        assert set(plain) == {
            "statistics_sufficient",
            "execution_valid",
            "recovery_hypotheses_remaining",
            "eligible_hypotheses_total",
            "tested_or_ruled_out",
            "required_search_families_completed",
            "consecutive_zero_novelty_cycles",
            "required_zero_novelty_cycles",
        }
        assert ClosureRecord(**plain) == record


def test_closure_record_rejects_non_bool_tristate_axes() -> None:
    with pytest.raises(ClosureRecordError):
        ClosureRecord(statistics_sufficient=1)  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(execution_valid="yes")  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(required_search_families_completed=1)  # type: ignore[arg-type]


def test_closure_record_rejects_non_int_counts() -> None:
    with pytest.raises(ClosureRecordError):
        ClosureRecord(recovery_hypotheses_remaining="2")  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(recovery_hypotheses_remaining=True)  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(eligible_hypotheses_total="5")  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(tested_or_ruled_out="5")  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(consecutive_zero_novelty_cycles="2")  # type: ignore[arg-type]
    with pytest.raises(ClosureRecordError):
        ClosureRecord(required_zero_novelty_cycles="2")  # type: ignore[arg-type]


def test_closure_record_rejects_negative_counts() -> None:
    with pytest.raises(ClosureRecordError):
        ClosureRecord(recovery_hypotheses_remaining=-1)
    with pytest.raises(ClosureRecordError):
        ClosureRecord(eligible_hypotheses_total=-1)
    with pytest.raises(ClosureRecordError):
        ClosureRecord(tested_or_ruled_out=-1)
    with pytest.raises(ClosureRecordError):
        ClosureRecord(consecutive_zero_novelty_cycles=-1)


def test_closure_record_rejects_saturation_rule_below_schema_minimum() -> None:
    # schemas/closure-contract.schema.yaml: required_zero_novelty_cycles
    # minimum 1.
    with pytest.raises(ClosureRecordError):
        ClosureRecord(required_zero_novelty_cycles=0)
    with pytest.raises(ClosureRecordError):
        ClosureRecord(required_zero_novelty_cycles=-2)


def test_from_closure_contract_maps_the_frozen_model() -> None:
    contract = ClosureContract(
        closure_id="CLC-ADS-C3H6-298K-001",
        frozen=True,
        statistical_sufficiency={},
        execution_validity={},
        diagnosis={},
        recovery=ClosureRecovery(
            eligibility_rule={},
            eligible_hypotheses_total=5,
            tested_or_ruled_out=2,
            remaining=3,
        ),
        literature=ClosureLiterature(
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
        ),
    )
    record = ClosureRecord.from_closure_contract(
        contract, statistics_sufficient=True, execution_valid=True
    )
    assert record == _record(
        recovery_hypotheses_remaining=3,
        eligible_hypotheses_total=5,
        tested_or_ruled_out=2,
    )
    # The tri-state axes are never invented by the mapping: without explicit
    # values they stay unresolved, even though the model carries free-form
    # dicts for those categories (normative reading).
    defaulted = ClosureRecord.from_closure_contract(contract)
    assert defaulted.statistics_sufficient is None
    assert defaulted.execution_valid is None
    # The model's saturation default rule is preserved.
    assert defaulted.required_zero_novelty_cycles == 2


def test_from_closure_contract_rejects_non_model() -> None:
    with pytest.raises(TypeError):
        ClosureRecord.from_closure_contract({"closure_id": "x"})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC-01: closure fails when any mandatory gate is unresolved
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gate_id,make_record",
    [
        (
            ClosureGateId.STATISTICAL_SUFFICIENCY,
            lambda: _record(statistics_sufficient=None),
        ),
        (
            ClosureGateId.EXECUTION_VALIDITY,
            lambda: _record(execution_valid=None),
        ),
        (
            ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
            lambda: _record(recovery_hypotheses_remaining=None),
        ),
        (
            ClosureGateId.RESEARCH_SATURATION,
            lambda: _record(consecutive_zero_novelty_cycles=None),
        ),
    ],
    ids=[
        "statistical-sufficiency",
        "execution-validity",
        "recovery-exhaustion",
        "research-saturation",
    ],
)
def test_closure_fails_when_any_mandatory_gate_is_unresolved(
    gate_id: ClosureGateId, make_record: object
) -> None:
    # One gate unknown, all other axes satisfied: closure is blocked and the
    # reason reports exactly that gate (AC-01).
    record = make_record()  # type: ignore[misc]
    assessment = evaluate_closure(record)
    assert assessment.closure_allowed is False
    assert assessment.outcome is ClosureOutcome.CLOSURE_BLOCKED
    assert assessment.matched_rule_id == "R-CLOSE-1"
    assert assessment.blocked_gate_ids == (gate_id,)
    assert [reason.gate_id for reason in assessment.closure_reasons] == [gate_id]
    (reason,) = assessment.closure_reasons
    assert reason.state is ClosureAxisState.UNRESOLVED
    assert reason.satisfied is False


@pytest.mark.parametrize(
    "gate_id,make_record",
    [
        (
            ClosureGateId.STATISTICAL_SUFFICIENCY,
            lambda: _record(statistics_sufficient=False),
        ),
        (
            ClosureGateId.EXECUTION_VALIDITY,
            lambda: _record(execution_valid=False),
        ),
        (
            ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
            lambda: _record(recovery_hypotheses_remaining=2),
        ),
        (
            ClosureGateId.RESEARCH_SATURATION,
            lambda: _record(required_search_families_completed=False),
        ),
    ],
    ids=[
        "statistical-sufficiency",
        "execution-validity",
        "recovery-exhaustion",
        "research-saturation",
    ],
)
def test_closure_fails_when_any_mandatory_gate_is_evaluated_failing(
    gate_id: ClosureGateId, make_record: object
) -> None:
    # One gate evaluated and found wanting, all other axes satisfied:
    # closure is blocked and the reason distinguishes NOT_SATISFIED from
    # UNRESOLVED (AC-01).
    record = make_record()  # type: ignore[misc]
    assessment = evaluate_closure(record)
    assert assessment.closure_allowed is False
    assert assessment.outcome is ClosureOutcome.CLOSURE_BLOCKED
    assert assessment.blocked_gate_ids == (gate_id,)
    (reason,) = assessment.closure_reasons
    assert reason.state is ClosureAxisState.NOT_SATISFIED


def test_closure_allowed_only_when_all_four_gates_satisfied() -> None:
    assessment = evaluate_closure(_record())
    assert assessment.closure_allowed is True
    assert assessment.outcome is ClosureOutcome.CLOSURE_ALLOWED
    assert assessment.matched_rule_id == "R-CLOSE-2"
    assert assessment.closure_reasons == ()
    assert assessment.blocked_gate_ids == ()
    assert all(decision.satisfied for decision in assessment.gate_decisions)


def test_closure_blocked_reports_every_blocking_gate() -> None:
    record = _record(
        statistics_sufficient=None,
        execution_valid=False,
        recovery_hypotheses_remaining=3,
        required_search_families_completed=None,
        consecutive_zero_novelty_cycles=None,
    )
    assessment = evaluate_closure(record)
    assert assessment.closure_allowed is False
    assert assessment.blocked_gate_ids == tuple(MANDATORY_GATES)
    assert [reason.gate_id for reason in assessment.closure_reasons] == list(
        MANDATORY_GATES
    )
    # Reasons are machine-readable: gate id, state, deciding rule and a
    # stable detail string, in normative gate order.
    states = {reason.gate_id: reason.state for reason in assessment.closure_reasons}
    assert states[ClosureGateId.STATISTICAL_SUFFICIENCY] is ClosureAxisState.UNRESOLVED
    assert states[ClosureGateId.EXECUTION_VALIDITY] is ClosureAxisState.NOT_SATISFIED
    assert states[ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION] is ClosureAxisState.NOT_SATISFIED
    assert states[ClosureGateId.RESEARCH_SATURATION] is ClosureAxisState.UNRESOLVED
    rule_ids = [reason.rule_id for reason in assessment.closure_reasons]
    assert rule_ids == ["R-STAT-3", "R-VALID-2", "R-RECOV-2", "R-SAT-3"]
    for reason in assessment.closure_reasons:
        assert reason.detail


def test_closure_reason_detail_strings_are_stable() -> None:
    # The reason detail text is frozen by tests so downstream actors can rely
    # on the strings (and the exact recovery count is embedded).
    assessment = evaluate_closure(
        _record(statistics_sufficient=False, recovery_hypotheses_remaining=2)
    )
    details = {reason.gate_id: reason.detail for reason in assessment.closure_reasons}
    assert (
        details[ClosureGateId.STATISTICAL_SUFFICIENCY]
        == "statistical evidence is not adequate: failure/non-equivalence"
        " cannot be distinguished from insufficient precision"
    )
    assert details[ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION] == (
        "eligible recovery hypotheses remaining: 2"
    )
    recovered = evaluate_closure(_record(recovery_hypotheses_remaining=0))
    assert recovered.closure_allowed is True


# ---------------------------------------------------------------------------
# AC-02: no fixed failure-count shortcut can close a required goal
# ---------------------------------------------------------------------------


def test_closure_record_has_no_failure_count_input() -> None:
    # There is no input field through which a count of failed runs/attempts
    # could reach the rules -- so no rule of the form "after N failures,
    # close anyway" can even be expressed against the input model.
    field_names = {field.name for field in dataclasses.fields(ClosureRecord)}
    assert not any("fail" in name or "attempt" in name for name in field_names)
    # Unknown inputs are rejected up front (TypeError), including any
    # imagined failure counter.
    with pytest.raises(TypeError):
        ClosureRecord(failed_runs=3)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ClosureRecord(attempt_count=5)  # type: ignore[call-arg]


def test_closure_rule_table_has_no_failure_count_shortcut_rule() -> None:
    # The aggregate table is exactly the conjunction pair: no third rule can
    # close a required goal through a failure count or any other shortcut
    # (AC-02; 20-ARCHITECTURE-DECISIONS.md decision 20).
    assert [rule.rule_id for rule in CLOSURE_RULES] == ["R-CLOSE-1", "R-CLOSE-2"]
    shortcut = re.compile(r"(?i)\b(after|once|when)\b.*\b\d+\b.*\b(fail|attempt)")
    for table in (
        STATISTICAL_SUFFICIENCY_RULES,
        EXECUTION_VALIDITY_RULES,
        RECOVERY_EXHAUSTION_RULES,
        RESEARCH_SATURATION_RULES,
        CLOSURE_RULES,
    ):
        for rule in table:
            assert not shortcut.search(rule.description), rule.description
            assert "attempt" not in rule.description.lower(), rule.description
            assert "failure count" not in rule.description.lower(), rule.description


def test_closure_outcome_depends_only_on_the_four_mandatory_axes() -> None:
    # Bi-implication over the exhaustive grid (AC-02): closure is allowed
    # exactly when every mandatory gate is satisfied; nothing else in the
    # input can move the decision.
    for record in _all_records():
        expected_outcome, expected_blocked = _expected_outcome(record)
        assessment = evaluate_closure(record)
        assert assessment.outcome is expected_outcome, record
        assert assessment.blocked_gate_ids == expected_blocked, record
        assert assessment.closure_allowed == (expected_blocked == ()), record
        assert (assessment.matched_rule_id == "R-CLOSE-2") == (
            expected_blocked == ()
        ), record


def test_closure_auditable_counts_are_inert() -> None:
    # eligible_hypotheses_total / tested_or_ruled_out are recorded for the
    # audit trail but never read by the rules: records differing only in
    # these counts produce identical decisions and reasons.
    base = _record(recovery_hypotheses_remaining=1)
    reference = evaluate_closure(base)
    assert reference.closure_allowed is False
    for total, tested in ((None, None), (0, 0), (5, 3), (10, 10)):
        variant = _record(
            recovery_hypotheses_remaining=1,
            eligible_hypotheses_total=total,
            tested_or_ruled_out=tested,
        )
        assessment = evaluate_closure(variant)
        assert assessment.outcome is reference.outcome
        assert assessment.blocked_gate_ids == reference.blocked_gate_ids
        assert [r.detail for r in assessment.closure_reasons] == [
            r.detail for r in reference.closure_reasons
        ]


def test_closure_no_fixed_failure_count_shortcut_behavioral() -> None:
    # A required goal closes only through the legitimate closure axes: for
    # every record in the grid, the decision is a pure function of the four
    # axes, and a record with any gate failing or unknown -- no matter how
    # many failures a caller might imagine -- stays blocked. There is no
    # input that could express "N failures", so no N can force closure.
    for record in _all_records():
        states = {
            decision.gate_id: decision.state for decision in evaluate_closure(record).gate_decisions
        }
        expected = all(
            states[gate] is ClosureAxisState.SATISFIED for gate in MANDATORY_GATES
        )
        assert evaluate_closure(record).closure_allowed == expected, record


# ---------------------------------------------------------------------------
# AC-03: eligible recovery hypotheses remaining > 0 prevent closure
# ---------------------------------------------------------------------------


def test_recovery_remaining_hypotheses_prevent_closure() -> None:
    record = _record(recovery_hypotheses_remaining=1)
    assessment = evaluate_closure(record)
    assert assessment.closure_allowed is False
    assert assessment.outcome is ClosureOutcome.CLOSURE_BLOCKED
    assert assessment.matched_rule_id == "R-CLOSE-1"
    assert assessment.blocked_gate_ids == (
        ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION,
    )
    (reason,) = assessment.closure_reasons
    assert reason.state is ClosureAxisState.NOT_SATISFIED
    assert reason.rule_id == "R-RECOV-2"
    assert reason.detail == "eligible recovery hypotheses remaining: 1"


@pytest.mark.parametrize("remaining", [1, 3, 100])
def test_recovery_any_remaining_count_blocks_closure_with_reported_count(
    remaining: int,
) -> None:
    record = _record(recovery_hypotheses_remaining=remaining)
    assessment = evaluate_closure(record)
    assert assessment.closure_allowed is False
    (reason,) = assessment.closure_reasons
    assert reason.detail == f"eligible recovery hypotheses remaining: {remaining}"


def test_recovery_pool_unassessed_blocks_closure_as_unresolved() -> None:
    # remaining = None (pool not yet assessed) is an unresolved mandatory
    # gate: it blocks closure just like a positive count (AC-01).
    record = _record(recovery_hypotheses_remaining=None)
    assessment = evaluate_closure(record)
    assert assessment.closure_allowed is False
    (reason,) = assessment.closure_reasons
    assert reason.state is ClosureAxisState.UNRESOLVED
    assert reason.rule_id == "R-RECOV-3"


def test_non_reproduced_closure_requires_exhausted_recovery_pool_and_other_axes() -> None:
    # AC-03: a non-reproduced reproduction closes only when the pool is
    # exhausted (0 remaining) AND every other mandatory axis passes.
    exhausted = _record(recovery_hypotheses_remaining=0)
    assert evaluate_closure(exhausted).closure_allowed is True
    # One eligible hypothesis remains -> closure fails.
    assert evaluate_closure(
        _record(recovery_hypotheses_remaining=1)
    ).closure_allowed is False
    # Pool exhausted but another axis unresolved -> closure still fails.
    assert evaluate_closure(
        _record(recovery_hypotheses_remaining=0, statistics_sufficient=None)
    ).closure_allowed is False
    assert evaluate_closure(
        _record(recovery_hypotheses_remaining=0, execution_valid=False)
    ).closure_allowed is False


def test_recovery_axis_biimplication_over_remaining_grid() -> None:
    # The recovery gate is SATISFIED exactly when the pool is exhausted,
    # NOT_SATISFIED exactly when eligible hypotheses remain, UNRESOLVED
    # exactly when the pool is unassessed -- independent of the auditable
    # companion counts.
    for remaining in (None, 0, 1, 2, 7):
        record = _record(
            recovery_hypotheses_remaining=remaining,
            eligible_hypotheses_total=7,
            tested_or_ruled_out=7 - remaining if remaining is not None else None,
        )
        decision = next(
            decision
            for decision in evaluate_closure(record).gate_decisions
            if decision.gate_id is ClosureGateId.RECOVERY_HYPOTHESIS_EXHAUSTION
        )
        if remaining == 0:
            assert decision.state is ClosureAxisState.SATISFIED
            assert decision.rule_id == "R-RECOV-1"
        elif remaining is not None:
            assert decision.state is ClosureAxisState.NOT_SATISFIED
            assert decision.rule_id == "R-RECOV-2"
        else:
            assert decision.state is ClosureAxisState.UNRESOLVED
            assert decision.rule_id == "R-RECOV-3"


# ---------------------------------------------------------------------------
# Research saturation axis (09-RESEARCH-SUBSYSTEM.md section 7)
# ---------------------------------------------------------------------------


def test_saturation_requires_families_completed_and_zero_novelty_rule() -> None:
    # SATISFIED exactly when families are completed AND the configured
    # zero-novelty rule is met, over the full families x cycles grid.
    for families in (True, False, None):
        for cycles in (None, 0, 1, 2, 5):
            record = _record(
                required_search_families_completed=families,
                consecutive_zero_novelty_cycles=cycles,
            )
            decision = next(
                decision
                for decision in evaluate_closure(record).gate_decisions
                if decision.gate_id is ClosureGateId.RESEARCH_SATURATION
            )
            expected = (
                ClosureAxisState.SATISFIED
                if families is True
                and cycles is not None
                and cycles >= record.required_zero_novelty_cycles
                else ClosureAxisState.NOT_SATISFIED
                if families is False
                or (cycles is not None and cycles < record.required_zero_novelty_cycles)
                else ClosureAxisState.UNRESOLVED
            )
            assert decision.state is expected, (families, cycles)


def test_saturation_default_rule_is_two_zero_novelty_cycles() -> None:
    # Default v0.1 operational saturation rule: two consecutive expansion
    # search cycles produce zero new eligible Recovery hypotheses
    # (08-STRICT-RECOVERY-CLOSURE.md section 4).
    assessment = evaluate_closure(
        _record(
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
        )
    )
    assert assessment.closure_allowed is True
    decision = next(
        decision
        for decision in assessment.gate_decisions
        if decision.gate_id is ClosureGateId.RESEARCH_SATURATION
    )
    assert decision.state is ClosureAxisState.SATISFIED
    assert decision.rule_id == "R-SAT-1"
    below = evaluate_closure(
        _record(
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=1,
        )
    )
    below_decision = next(
        decision
        for decision in below.gate_decisions
        if decision.gate_id is ClosureGateId.RESEARCH_SATURATION
    )
    assert below_decision.state is ClosureAxisState.NOT_SATISFIED
    assert below_decision.rule_id == "R-SAT-2"


def test_saturation_rule_is_configurable_and_frozen() -> None:
    # The saturation rule is a governance rule that must be configurable and
    # frozen (08-STRICT-RECOVERY-CLOSURE.md section 4): the configured count
    # lives on the input record and the ruleset is versioned.
    assert _record().required_zero_novelty_cycles == 2  # schema default
    configured = _record(
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=3,
        required_zero_novelty_cycles=3,
    )
    assert evaluate_closure(configured).closure_allowed is True
    not_yet = _record(
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=2,
        required_zero_novelty_cycles=3,
    )
    assert evaluate_closure(not_yet).closure_allowed is False
    strict_rule = _record(
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=2,
        required_zero_novelty_cycles=1,
    )
    assert evaluate_closure(strict_rule).closure_allowed is True


def test_saturation_partial_knowledge_is_unresolved() -> None:
    # Family completion is known but the cycle count is unknown, and vice
    # versa: the axis stays UNRESOLVED rather than inventing a decision.
    unknown_cycles = _record(
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=None,
    )
    decision = next(
        decision
        for decision in evaluate_closure(unknown_cycles).gate_decisions
        if decision.gate_id is ClosureGateId.RESEARCH_SATURATION
    )
    assert decision.state is ClosureAxisState.UNRESOLVED
    unknown_families = _record(
        required_search_families_completed=None,
        consecutive_zero_novelty_cycles=2,
    )
    decision = next(
        decision
        for decision in evaluate_closure(unknown_families).gate_decisions
        if decision.gate_id is ClosureGateId.RESEARCH_SATURATION
    )
    assert decision.state is ClosureAxisState.UNRESOLVED
    # A counted shortfall is evaluated (failing), not unknown.
    shortfall = _record(
        required_search_families_completed=None,
        consecutive_zero_novelty_cycles=1,
    )
    decision = next(
        decision
        for decision in evaluate_closure(shortfall).gate_decisions
        if decision.gate_id is ClosureGateId.RESEARCH_SATURATION
    )
    assert decision.state is ClosureAxisState.NOT_SATISFIED


# ---------------------------------------------------------------------------
# Bi-implications, determinism, audit trails
# ---------------------------------------------------------------------------


def test_closure_evaluation_is_deterministic() -> None:
    for record in _all_records()[::3]:
        reference = evaluate_closure(record)
        for _ in range(25):
            assert evaluate_closure(record) == reference


def test_closure_assessment_records_exact_input() -> None:
    for record in _all_records()[::5]:
        assessment = evaluate_closure(record)
        assert assessment.ruleset_version == RULESET_VERSION
        assert assessment.record == record


def test_closure_assessment_records_every_rule_decision() -> None:
    assessment = evaluate_closure(
        _record(statistics_sufficient=False, recovery_hypotheses_remaining=1)
    )
    expected_ids: list[str] = []
    for gate_id, rules in GATE_RULE_TABLES:
        for rule in rules:
            expected_ids.append(rule.rule_id)
            decision = assessment.axis_decisions[len(expected_ids) - 1]
            assert decision.gate_id is gate_id
            assert decision.rule_id == rule.rule_id
            assert decision.description == rule.description
            assert decision.state == rule.state
    assert [decision.rule_id for decision in assessment.axis_decisions] == expected_ids
    assert [decision.rule_id for decision in assessment.rule_decisions] == [
        rule.rule_id for rule in CLOSURE_RULES
    ]


def test_closure_gate_decisions_cover_all_mandatory_gates_in_order() -> None:
    assessment = evaluate_closure(_record())
    assert [decision.gate_id for decision in assessment.gate_decisions] == list(
        MANDATORY_GATES
    )
    # Each gate decision carries the axis rule that decided it.
    assert [decision.rule_id for decision in assessment.gate_decisions] == [
        "R-STAT-1",
        "R-VALID-1",
        "R-RECOV-1",
        "R-SAT-1",
    ]


def test_closure_matched_rule_is_the_first_true_predicate() -> None:
    for record in _all_records()[::11]:
        assessment = evaluate_closure(record)
        matched = next(
            decision for decision in assessment.rule_decisions if decision.matched
        )
        assert matched.rule_id == assessment.matched_rule_id
        for earlier in assessment.rule_decisions[: assessment.rule_decisions.index(matched)]:
            assert earlier.matched is False
        for gate_id, rules in GATE_RULE_TABLES:
            gate_decision = next(
                decision
                for decision in assessment.gate_decisions
                if decision.gate_id is gate_id
            )
            axis_matched = next(
                decision
                for decision in assessment.axis_decisions
                if decision.gate_id is gate_id and decision.matched
            )
            assert axis_matched.rule_id == gate_decision.rule_id
            rule = next(r for r in rules if r.rule_id == axis_matched.rule_id)
            assert rule.predicate(record) is True


# ---------------------------------------------------------------------------
# Spec scenarios (18-TEST-AND-ACCEPTANCE-PLAN.md Scenario C / FDM-201 S6)
# ---------------------------------------------------------------------------


def test_fdm201_s6_recovery_exhausted_scenario_allows_closure() -> None:
    # examples/fdm-201/simulated-scenarios.md S6: "All eligible recovery
    # hypotheses processed, search saturation reached, execution valid and
    # statistics sufficient" -> Closure Contract allows the NOT_REPRODUCED
    # conclusion.
    assessment = evaluate_closure(
        _record(
            statistics_sufficient=True,
            execution_valid=True,
            recovery_hypotheses_remaining=0,
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
        )
    )
    assert assessment.closure_allowed is True
    assert assessment.outcome is ClosureOutcome.CLOSURE_ALLOWED
    assert assessment.matched_rule_id == "R-CLOSE-2"


def test_acceptance_plan_scenario_c_blocked_until_all_axes_pass() -> None:
    # 18-TEST-AND-ACCEPTANCE-PLAN.md Scenario C: strict failure
    # statistically sufficient, QC valid, all eligible hypotheses
    # tested/ruled out, research saturation met -> Closure Contract
    # satisfied. Each condition missing in isolation keeps closure blocked.
    base = dict(
        statistics_sufficient=True,
        execution_valid=True,
        recovery_hypotheses_remaining=0,
        required_search_families_completed=True,
        consecutive_zero_novelty_cycles=2,
    )
    assert evaluate_closure(_record(**base)).closure_allowed is True
    variants = [
        dict(base, statistics_sufficient=False),
        dict(base, execution_valid=False),
        dict(base, recovery_hypotheses_remaining=1),
        dict(base, consecutive_zero_novelty_cycles=1),
        dict(base, required_search_families_completed=False),
    ]
    for variant in variants:
        assert evaluate_closure(_record(**variant)).closure_allowed is False


# ---------------------------------------------------------------------------
# TypeError paths (wrong-typed public inputs)
# ---------------------------------------------------------------------------


def test_evaluate_closure_rejects_non_record() -> None:
    with pytest.raises(TypeError):
        evaluate_closure({"statistics_sufficient": True})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_closure(None)  # type: ignore[arg-type]


def test_errors_are_value_error_subclasses() -> None:
    # Stable error hierarchy: rule-engine errors derive from ValueError.
    assert issubclass(ClosureRecordError, ClosureRulesError)
    assert issubclass(ClosureRulesError, ValueError)
