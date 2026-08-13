"""Tests for the search-cycle and saturation records workflow (DEV-M5-G05).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_saturation_ac01_*``: one zero-novelty cycle is
    INSUFFICIENT. Proven behaviorally (a single completed expansion cycle
    with zero new eligible hypotheses yields the NOT_SATURATED verdict
    with a run of 1 below the required 2) and independently of the
    recorded family-coverage state (True / False / None all stay
    NOT_SATURATED).
  * AC-02 -- ``test_saturation_ac02_*``: two consecutive completed
    expansion cycles with zero new eligible hypotheses satisfy v0.1
    saturation. Also proven: the consecutive run breaks on a novel cycle
    (zero + novel = not saturated) and on an incomplete cycle (zero,
    incomplete, zero = not saturated); a novel cycle resets the counter;
    the required-cycle rule is the frozen configurable field of the
    record (schema minimum 1, default 2).
  * AC-03 -- ``test_saturation_ac03_*``: the eligibility threshold can
    reference the Reliability/Directness rules. Proven by construction
    (the default eligibility rule is composed from the core hard-gate
    rules ``reliability_gate_passes`` / ``directness_gate_passes`` at the
    frozen v0.1 thresholds, and it is behaviorally identical to the
    canonical ``recovery_hypothesis_eligible`` gate over the full
    Reliability x Directness grid) and through the research reliability
    layer (an eligibility rule that re-derives Reliability from the
    stored checklist record via ``research/reliability.py``); the tracker
    counts only NEW eligible hypotheses.

The module is also proven to follow the frozen rule-engine paradigm:
versioned constants, frozen immutable dataclasses, first-match-wins ordered
rule table with a total default, ``TypeError`` at public boundaries,
``ValueError``-subclass errors with stable messages, pure deterministic
functions, and the frozen ``ClosureLiterature`` connection the closure
layer composes.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from scientific_reproduction.core.models import (
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    EvidenceAssessment,
)
from scientific_reproduction.core.rules.closure import (
    ClosureRecord as CoreClosureRecord,
)
from scientific_reproduction.core.rules.closure import evaluate_closure
from scientific_reproduction.core.rules.evidence import (
    recovery_hypothesis_eligible as core_recovery_hypothesis_eligible,
)
from scientific_reproduction.research.reliability import (
    NEGATIVE_DIMENSION_KEY,
    POSITIVE_DIMENSION_KEYS,
    ChecklistAnswer,
    ReliabilityChecklistRecord,
    ReliabilityChecklistRegistry,
)
from scientific_reproduction.research.saturation import (
    ELIGIBILITY_RULESET_VERSION,
    SATURATION_RULES,
    SATURATION_RULESET_VERSION,
    V01_ELIGIBILITY_ACTIONABLE,
    V01_ELIGIBILITY_MIN_DIRECTNESS,
    V01_ELIGIBILITY_MIN_RELIABILITY,
    V01_ELIGIBILITY_RULE,
    EligibilityRule,
    HypothesisCandidate,
    NoveltyCount,
    SaturationAssessment,
    SaturationEligibilityError,
    SaturationError,
    SaturationRecord,
    SaturationRecordError,
    SaturationRule,
    SaturationVerdict,
    SearchCycle,
    consecutive_zero_novelty_run,
    evaluate_saturation,
    track_new_eligible_hypotheses,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _assessment(
    *,
    reliability: int,
    directness: int,
    checklist_ref: str | None = None,
    authority: int = 3,
) -> EvidenceAssessment:
    """Build a frozen assessment with the given Reliability/Directness axes."""
    return EvidenceAssessment(
        authority=authority,
        reliability=reliability,
        directness=directness,
        reliability_checklist_ref=(
            checklist_ref
            if checklist_ref is not None
            else f"RCHK-R{reliability}-D{directness}"
        ),
    )


def _candidate(ref: str, assessment: EvidenceAssessment) -> HypothesisCandidate:
    """Build one candidate hypothesis with its evidence assessment."""
    return HypothesisCandidate(hypothesis_ref=ref, assessment=assessment)


def _cycle(
    index: int,
    *,
    family: str = "citations",
    completed: bool = True,
    new_eligible: int = 0,
    expansion: bool = True,
) -> SearchCycle:
    """Build one recorded search cycle (defaults: a completed expansion
    cycle with zero new eligible hypotheses -- a zero-novelty cycle)."""
    return SearchCycle(
        cycle_index=index,
        search_family=family,
        completed=completed,
        new_eligible_hypotheses=new_eligible,
        expansion=expansion,
    )


def _record(
    *cycles: SearchCycle,
    families_completed: bool | None = None,
    required: int = 2,
) -> SaturationRecord:
    """Build a saturation record; cycle indexes are derived from position."""
    numbered = tuple(
        cycle if cycle.cycle_index == i else dataclasses.replace(
            cycle, cycle_index=i
        )
        for i, cycle in enumerate(cycles)
    )
    return SaturationRecord(
        cycles=numbered,
        required_search_families_completed=families_completed,
        required_zero_novelty_cycles=required,
    )


def _checklist_record(score: int, checklist_ref: str) -> ReliabilityChecklistRecord:
    """Build a checklist record that scores exactly ``score`` (0-4).

    ``score`` positive dimensions answered YES out of the eight positive
    dimensions (2*score - 1 or 2*score choices follow the R-REL bands: 8 ->
    4, 6-7 -> 3, 4-5 -> 2, 2-3 -> 1, 0-1 -> 0).
    """
    satisfied = {
        4: 8,
        3: 7,
        2: 5,
        1: 3,
        0: 0,
    }[score]
    answers: dict[str, ChecklistAnswer] = {
        key: ChecklistAnswer.YES for key in POSITIVE_DIMENSION_KEYS[:satisfied]
    }
    for key in POSITIVE_DIMENSION_KEYS[satisfied:]:
        answers[key] = ChecklistAnswer.NO
    answers[NEGATIVE_DIMENSION_KEY] = ChecklistAnswer.NO
    return ReliabilityChecklistRecord(
        checklist_ref=checklist_ref, **answers  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Model shape, versions and vocabulary
# ---------------------------------------------------------------------------


def test_saturation_ruleset_and_eligibility_versions_are_versioned_constants() -> None:
    # Rule-table and eligibility versions are non-empty string constants;
    # every assessment/decision records the version it was produced with.
    assert isinstance(SATURATION_RULESET_VERSION, str)
    assert SATURATION_RULESET_VERSION
    assert isinstance(ELIGIBILITY_RULESET_VERSION, str)
    assert ELIGIBILITY_RULESET_VERSION
    assert (
        evaluate_saturation(_record()).ruleset_version
        == SATURATION_RULESET_VERSION
    )
    assert V01_ELIGIBILITY_RULE.version == ELIGIBILITY_RULESET_VERSION


def test_saturation_v01_threshold_constants_match_the_frozen_eligibility() -> None:
    # 08-STRICT-RECOVERY-CLOSURE.md SS4: default v0.1 Recovery eligibility =
    # Reliability >= 3, Directness >= 2, actionable = true.
    assert V01_ELIGIBILITY_MIN_RELIABILITY == 3
    assert V01_ELIGIBILITY_MIN_DIRECTNESS == 2
    assert V01_ELIGIBILITY_ACTIONABLE is True


def test_saturation_verdict_vocabulary_is_exact() -> None:
    # The verdict vocabulary is exactly SATURATED/NOT_SATURATED/UNRESOLVED
    # -- nothing invented.
    assert list(SaturationVerdict) == [
        SaturationVerdict.SATURATED,
        SaturationVerdict.NOT_SATURATED,
        SaturationVerdict.UNRESOLVED,
    ]
    assert SaturationVerdict.SATURATED.value == "SATURATED"
    assert SaturationVerdict.NOT_SATURATED.value == "NOT_SATURATED"
    assert SaturationVerdict.UNRESOLVED.value == "UNRESOLVED"


def test_saturation_cycle_record_is_an_immutable_frozen_dataclass() -> None:
    cycle = _cycle(0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        cycle.new_eligible_hypotheses = 5  # type: ignore[misc]
    assert cycle.new_eligible_hypotheses == 0  # mutation attempt changed nothing


def test_saturation_cycle_record_roundtrips_through_serialization() -> None:
    # A stored cycle survives serialization exactly ("reproducible from
    # stored input"): to_dict/from_dict round-trips, and the restored record
    # contributes identically to the run.
    cycle = _cycle(2, family="related methods", completed=False, new_eligible=3)
    rebuilt = SearchCycle.from_dict(cycle.to_dict())
    assert rebuilt == cycle
    assert SearchCycle.from_dict(rebuilt.to_dict()) == rebuilt
    assert consecutive_zero_novelty_run((cycle,)) is None
    assert consecutive_zero_novelty_run((rebuilt,)) is None


def test_saturation_record_roundtrips_through_serialization() -> None:
    # A stored cycle history survives serialization exactly, and the
    # restored history reproduces the same verdict.
    record = _record(
        _cycle(0),
        _cycle(1),
        families_completed=True,
    )
    rebuilt = SaturationRecord.from_dict(record.to_dict())
    assert rebuilt == record
    assert rebuilt.to_dict() == record.to_dict()
    assert evaluate_saturation(rebuilt) == evaluate_saturation(record)


def test_saturation_search_family_is_recorded_per_cycle() -> None:
    # The cycle records which search family it covered (09-RESEARCH-SUBSYSTEM.md
    # SS7: each search cycle is stored); the family is opaque and preserved.
    cycle = _cycle(0, family="exact node/linker/precursor chemistry")
    assert cycle.search_family == "exact node/linker/precursor chemistry"
    assert SearchCycle.from_dict(cycle.to_dict()).search_family == (
        cycle.search_family
    )
    # The researcher may record the same family across cycles; families are
    # per-cycle facts, not a global state.
    first = _cycle(0, family="cited references", new_eligible=2)
    second = _cycle(1, family="cited references", new_eligible=1)
    assert first.search_family == second.search_family
    assert consecutive_zero_novelty_run((first, second)) == 0


# ---------------------------------------------------------------------------
# AC-01: one zero-novelty cycle is insufficient
# ---------------------------------------------------------------------------


def test_saturation_ac01_one_zero_novelty_cycle_is_insufficient() -> None:
    # One completed expansion cycle with zero new eligible hypotheses
    # produces a run of 1 -- below the v0.1 required 2 -- so the verdict is
    # NOT_SATURATED (AC-01), decided by R-SAT-S2.
    assessment = evaluate_saturation(_record(_cycle(0)))
    assert assessment.consecutive_zero_novelty_cycles == 1
    assert assessment.verdict is SaturationVerdict.NOT_SATURATED
    assert assessment.saturated is False
    assert assessment.matched_rule_id == "R-SAT-S2"
    assert assessment.required_zero_novelty_cycles == 2


def test_saturation_ac01_one_zero_novelty_cycle_is_insufficient_regardless_of_family_coverage() -> None:
    # AC-01 holds independently of the recorded family-coverage state: a
    # single zero-novelty cycle is NOT_SATURATED whether all required
    # search families are confirmed completed (True), confirmed incomplete
    # (False) or unknown (None) -- the operational rule needs two
    # consecutive zero-novelty cycles.
    for families in (True, False, None):
        assessment = evaluate_saturation(
            _record(_cycle(0), families_completed=families)
        )
        assert assessment.verdict is SaturationVerdict.NOT_SATURATED
        assert assessment.saturated is False
        assert assessment.consecutive_zero_novelty_cycles == 1
        assert assessment.required_search_families_completed is families


def test_saturation_ac01_one_zero_novelty_cycle_never_yields_a_run_of_two() -> None:
    # The run after one zero-novelty cycle is exactly 1 -- never 2 -- so no
    # single cycle can satisfy the v0.1 rule through any other path.
    assert consecutive_zero_novelty_run((_cycle(0),)) == 1
    assert consecutive_zero_novelty_run((_cycle(0, new_eligible=0),)) == 1
    assert evaluate_saturation(_record(_cycle(0))).verdict is (
        SaturationVerdict.NOT_SATURATED
    )


# ---------------------------------------------------------------------------
# AC-02: two consecutive zero-novelty cycles satisfy v0.1 saturation
# ---------------------------------------------------------------------------


def test_saturation_ac02_two_consecutive_zero_novelty_cycles_satisfy_v01() -> None:
    # Two consecutive completed expansion cycles with zero new eligible
    # hypotheses: run 2 meets the required 2 -> SATURATED (AC-02), decided
    # by R-SAT-S1. The verdict is the operational rule: it is SATURATED
    # even before family coverage is recorded, and the assessment carries
    # the family-coverage input for the closure layer to compose.
    for families in (None, False, True):
        assessment = evaluate_saturation(
            _record(_cycle(0), _cycle(1), families_completed=families)
        )
        assert assessment.consecutive_zero_novelty_cycles == 2
        assert assessment.verdict is SaturationVerdict.SATURATED
        assert assessment.saturated is True
        assert assessment.matched_rule_id == "R-SAT-S1"


def test_saturation_ac02_zero_then_novel_breaks_the_consecutive_run() -> None:
    # One zero-novelty cycle followed by a novel cycle: the novel cycle
    # resets the counter, so the two cycles are not two consecutive
    # zero-novelty cycles -> NOT_SATURATED.
    assessment = evaluate_saturation(
        _record(_cycle(0), _cycle(1, new_eligible=2))
    )
    assert assessment.consecutive_zero_novelty_cycles == 0
    assert assessment.verdict is SaturationVerdict.NOT_SATURATED


def test_saturation_ac02_zero_incomplete_zero_breaks_the_consecutive_run() -> None:
    # zero, incomplete, zero: the incomplete cycle is not a completed
    # expansion cycle, so it breaks the run -- only the trailing zero
    # counts, run 1 < 2 -> NOT_SATURATED.
    assessment = evaluate_saturation(
        _record(_cycle(0), _cycle(1, completed=False), _cycle(2))
    )
    assert assessment.consecutive_zero_novelty_cycles == 1
    assert assessment.verdict is SaturationVerdict.NOT_SATURATED
    assert assessment.saturated is False


def test_saturation_ac02_novel_cycle_resets_the_consecutive_counter() -> None:
    # zero, zero, novel: the trailing novel cycle resets the counter to 0,
    # so the earlier pair no longer counts -> NOT_SATURATED. Novel cycles
    # reset; only a trailing run of zeros satisfies the rule.
    assessment = evaluate_saturation(
        _record(_cycle(0), _cycle(1), _cycle(2, new_eligible=1))
    )
    assert assessment.consecutive_zero_novelty_cycles == 0
    assert assessment.verdict is SaturationVerdict.NOT_SATURATED
    # A run that resumes after novelty does satisfy: novel, zero, zero.
    resumed = evaluate_saturation(
        _record(_cycle(0, new_eligible=1), _cycle(1), _cycle(2))
    )
    assert resumed.consecutive_zero_novelty_cycles == 2
    assert resumed.verdict is SaturationVerdict.SATURATED


def test_saturation_ac02_three_consecutive_zero_novelty_cycles_still_satisfy() -> None:
    # Three consecutive zero-novelty cycles satisfy the v0.1 rule as well:
    # the rule requires AT LEAST two; the run keeps counting.
    assessment = evaluate_saturation(_record(_cycle(0), _cycle(1), _cycle(2)))
    assert assessment.consecutive_zero_novelty_cycles == 3
    assert assessment.verdict is SaturationVerdict.SATURATED


def test_saturation_ac02_non_expansion_cycle_breaks_the_consecutive_run() -> None:
    # The operational rule is stated over *expansion* search cycles: a
    # non-expansion cycle (e.g. a re-verification pass) is not a completed
    # expansion cycle, so it breaks the run. zero, non-expansion, zero ->
    # run 1 -> NOT_SATURATED.
    assessment = evaluate_saturation(
        _record(
            _cycle(0),
            _cycle(1, new_eligible=0, expansion=False),
            _cycle(2),
        )
    )
    assert assessment.consecutive_zero_novelty_cycles == 1
    assert assessment.verdict is SaturationVerdict.NOT_SATURATED
    # A non-expansion cycle alone never establishes the run either.
    only_non_expansion = evaluate_saturation(
        _record(_cycle(0, new_eligible=0, expansion=False))
    )
    assert only_non_expansion.consecutive_zero_novelty_cycles is None
    assert only_non_expansion.verdict is SaturationVerdict.UNRESOLVED


def test_saturation_ac02_required_cycle_count_is_the_frozen_configurable_rule() -> None:
    # "This is a governance rule ... it must be configurable and frozen":
    # the required count is the frozen field of the record and of the
    # assessment (schemas/closure-contract.schema.yaml: minimum 1, default
    # 2) -- it is never an evaluation-time knob. With a frozen rule of 3,
    # two zero-novelty cycles are insufficient and three satisfy it.
    two = evaluate_saturation(_record(_cycle(0), _cycle(1), required=3))
    assert two.consecutive_zero_novelty_cycles == 2
    assert two.verdict is SaturationVerdict.NOT_SATURATED
    assert two.required_zero_novelty_cycles == 3
    three = evaluate_saturation(
        _record(_cycle(0), _cycle(1), _cycle(2), required=3)
    )
    assert three.verdict is SaturationVerdict.SATURATED
    # The rule is frozen in the record: a required count below the schema
    # minimum of 1 is rejected up front.
    with pytest.raises(SaturationRecordError) as exc_info:
        _record(_cycle(0), required=0)
    assert ">= 1" in str(exc_info.value)


def test_saturation_ac02_empty_history_is_unresolved() -> None:
    # No recorded cycle: the consecutive zero-novelty count has not been
    # established -> UNRESOLVED (the count field stays None, exactly the
    # frozen ClosureLiterature default).
    assessment = evaluate_saturation(_record())
    assert assessment.consecutive_zero_novelty_cycles is None
    assert assessment.verdict is SaturationVerdict.UNRESOLVED
    assert assessment.matched_rule_id == "R-SAT-D1"
    assert assessment.required_zero_novelty_cycles == 2


def test_saturation_ac02_history_ending_in_an_incomplete_cycle_is_unresolved() -> None:
    # When the most recent recorded cycle is not a completed expansion
    # cycle, the count has not been established -> UNRESOLVED, whether the
    # history is a single incomplete cycle or a zero-novelty pair followed
    # by an incomplete cycle.
    for history in (
        (_cycle(0, completed=False),),
        (_cycle(0), _cycle(1, completed=False)),
    ):
        assessment = evaluate_saturation(_record(*history))
        assert assessment.consecutive_zero_novelty_cycles is None
        assert assessment.verdict is SaturationVerdict.UNRESOLVED
        assert assessment.saturated is False


# ---------------------------------------------------------------------------
# AC-03: the eligibility threshold references the Reliability/Directness rules
# ---------------------------------------------------------------------------


def test_saturation_ac03_default_eligibility_references_the_core_rules() -> None:
    # The default threshold is composed from the core Reliability/Directness
    # hard-gate rules at the frozen v0.1 thresholds, and it is behaviorally
    # identical to the canonical v0.1 gate recovery_hypothesis_eligible
    # (core/rules/evidence.py) over the full Reliability x Directness grid.
    checked = 0
    for reliability in range(0, 5):
        for directness in range(0, 5):
            candidate = _candidate(
                f"H-R{reliability}-D{directness}",
                _assessment(reliability=reliability, directness=directness),
            )
            expected = core_recovery_hypothesis_eligible(candidate.assessment)
            assert V01_ELIGIBILITY_RULE.predicate(candidate) is expected
            assert expected == (
                reliability >= V01_ELIGIBILITY_MIN_RELIABILITY
                and directness >= V01_ELIGIBILITY_MIN_DIRECTNESS
            )
            checked += 1
    assert checked == 25
    # The frozen thresholds themselves are load-bearing: the boundary
    # assessments behave exactly at the v0.1 threshold.
    at_threshold = _candidate(
        "H-AT", _assessment(reliability=3, directness=2)
    )
    below_reliability = _candidate(
        "H-BELOW-R", _assessment(reliability=2, directness=2)
    )
    below_directness = _candidate(
        "H-BELOW-D", _assessment(reliability=3, directness=1)
    )
    assert V01_ELIGIBILITY_RULE.predicate(at_threshold) is True
    assert V01_ELIGIBILITY_RULE.predicate(below_reliability) is False
    assert V01_ELIGIBILITY_RULE.predicate(below_directness) is False


def test_saturation_ac03_eligibility_rule_built_from_core_threshold_gates() -> None:
    # A threshold built explicitly from the core Reliability/Directness
    # gates at the frozen v0.1 thresholds (with a named version) decides
    # identically to the frozen default -- the core rules are the
    # referenceable building blocks (AC-03).
    from scientific_reproduction.core.rules.evidence import (
        directness_gate_passes,
        reliability_gate_passes,
    )

    custom = EligibilityRule(
        rule_id="R-ELIG-EXPLICIT",
        version=ELIGIBILITY_RULESET_VERSION,
        description=(
            "eligibility composed from core Reliability/Directness gates at"
            " the frozen v0.1 thresholds"
        ),
        predicate=lambda c: (
            reliability_gate_passes(
                c.assessment, minimum=V01_ELIGIBILITY_MIN_RELIABILITY
            )
            and directness_gate_passes(
                c.assessment, minimum=V01_ELIGIBILITY_MIN_DIRECTNESS
            )
        ),
    )
    for reliability in range(0, 5):
        for directness in range(0, 5):
            candidate = _candidate(
                f"H-EXPL-R{reliability}-D{directness}",
                _assessment(reliability=reliability, directness=directness),
            )
            assert custom.predicate(candidate) is V01_ELIGIBILITY_RULE.predicate(
                candidate
            )


def test_saturation_ac03_eligibility_rule_references_the_research_reliability_layer() -> None:
    # The research-layer hook (AC-03): an eligibility rule that re-derives
    # the Reliability score from the STORED checklist record via
    # research/reliability.py (the R-REL rule table) and applies the core
    # Directness gate. The stored record is authoritative: a candidate whose
    # stored checklist scores below the threshold is not eligible even when
    # the assessment's stored reliability axis claims otherwise.
    registry = ReliabilityChecklistRegistry.from_records(
        [
            _checklist_record(score=4, checklist_ref="RCHK-STRONG"),
            _checklist_record(score=1, checklist_ref="RCHK-WEAK"),
        ]
    )
    rule = EligibilityRule.from_reliability_registry(registry)
    assert rule.rule_id == "R-ELIG-C1"
    assert rule.version == ELIGIBILITY_RULESET_VERSION
    strong = _candidate(
        "H-STRONG", _assessment(reliability=2, directness=2, checklist_ref="RCHK-STRONG")
    )
    weak = _candidate(
        "H-WEAK",
        _assessment(reliability=4, directness=2, checklist_ref="RCHK-WEAK"),
    )
    # The assessment's stored reliability axis (2) is overridden by the
    # re-derived stored score (4); the assessment's stored axis (4) is
    # overridden by the re-derived stored score (1).
    assert rule.predicate(strong) is True
    assert rule.predicate(weak) is False
    # A candidate whose checklist reference has no stored record is never
    # eligible (no reliability without the stored checklist record).
    unknown = _candidate(
        "H-UNKNOWN", _assessment(reliability=4, directness=2, checklist_ref="RCHK-NOPE")
    )
    assert rule.predicate(unknown) is False
    # A candidate below the Directness gate stays ineligible regardless of
    # the stored reliability score.
    not_direct = _candidate(
        "H-NOT-DIRECT",
        _assessment(reliability=4, directness=1, checklist_ref="RCHK-STRONG"),
    )
    assert rule.predicate(not_direct) is False


def test_saturation_ac03_tracker_counts_only_new_eligible_hypotheses() -> None:
    # The tracker counts how many candidates are NEW (novel) and eligible:
    # eligible-and-novel candidates count; eligible-but-already-known
    # candidates do not; ineligible candidates never count.
    candidates = (
        _candidate("H-NEW-1", _assessment(reliability=4, directness=3)),
        _candidate("H-KNOWN", _assessment(reliability=4, directness=3)),
        _candidate("H-NEW-INELIGIBLE", _assessment(reliability=2, directness=3)),
        _candidate("H-KNOWN-INELIGIBLE", _assessment(reliability=1, directness=1)),
        _candidate("H-NEW-2", _assessment(reliability=3, directness=2)),
    )
    novelty = track_new_eligible_hypotheses(
        candidates, known_eligible_hypotheses=("H-KNOWN", "H-KNOWN-INELIGIBLE")
    )
    assert isinstance(novelty, NoveltyCount)
    assert novelty.count == 2
    assert novelty.new_eligible_hypotheses == ("H-NEW-1", "H-NEW-2")
    # Every threshold-passing candidate is reported, novel or not.
    assert novelty.eligible_hypotheses == ("H-NEW-1", "H-KNOWN", "H-NEW-2")
    # One decision per candidate, in candidate order, with the deciding rule.
    assert [d.hypothesis_ref for d in novelty.decisions] == [
        c.hypothesis_ref for c in candidates
    ]
    assert all(
        d.rule_id == V01_ELIGIBILITY_RULE.rule_id
        and d.rule_version == V01_ELIGIBILITY_RULE.version
        for d in novelty.decisions
    )


def test_saturation_ac03_tracker_uses_the_threshold_rule_object() -> None:
    # The threshold is a versioned rule object, not an evaluation-time knob:
    # passing an explicit eligibility rule changes which candidates are
    # counted, and the decisions record that rule.
    registry = ReliabilityChecklistRegistry.from_records(
        [_checklist_record(score=4, checklist_ref="RCHK-STRONG")]
    )
    research_rule = EligibilityRule.from_reliability_registry(registry)
    candidates = (
        _candidate("H-A", _assessment(reliability=4, directness=3)),
        _candidate("H-B", _assessment(reliability=1, directness=3, checklist_ref="RCHK-STRONG")),
    )
    with_default = track_new_eligible_hypotheses(candidates, ())
    assert with_default.new_eligible_hypotheses == ("H-A",)
    with_research = track_new_eligible_hypotheses(
        candidates, (), eligibility=research_rule
    )
    assert with_research.new_eligible_hypotheses == ("H-B",)
    assert with_research.decisions[0].rule_id == "R-ELIG-C1"


def test_saturation_ac03_tracker_is_deterministic_and_order_preserving() -> None:
    # Identical inputs -> identical results; candidate order is preserved in
    # every output tuple; an empty candidate set tracks to zero.
    candidates = (
        _candidate("H-1", _assessment(reliability=4, directness=2)),
        _candidate("H-2", _assessment(reliability=1, directness=2)),
    )
    first = track_new_eligible_hypotheses(candidates, ())
    second = track_new_eligible_hypotheses(candidates, ())
    assert first == second
    assert first.new_eligible_hypotheses == ("H-1",)
    empty = track_new_eligible_hypotheses((), ())
    assert empty.count == 0
    assert empty.new_eligible_hypotheses == ()
    assert empty.eligible_hypotheses == ()
    assert empty.decisions == ()
    # Known-set membership is by reference: an empty known set makes every
    # eligible candidate novel; a full known set makes none novel.
    known = track_new_eligible_hypotheses(candidates, ("H-1",))
    assert known.count == 0
    assert known.eligible_hypotheses == ("H-1",)


def test_saturation_ac03_tracker_feeds_the_cycle_record() -> None:
    # The tracker produces the novelty count stored on the cycle record (09
    # SS7: "Store each search cycle and the number of new eligible Recovery
    # hypotheses"), and the recorded count drives the run.
    candidates = (
        _candidate("H-NEW", _assessment(reliability=4, directness=3)),
        _candidate("H-KNOWN", _assessment(reliability=4, directness=3)),
    )
    novelty = track_new_eligible_hypotheses(candidates, ("H-KNOWN",))
    assert novelty.count == 1
    cycle = SearchCycle(
        cycle_index=0,
        search_family="citations",
        completed=True,
        new_eligible_hypotheses=novelty.count,
    )
    assert cycle.new_eligible_hypotheses == 1
    # One novel cycle followed by an empty cycle: the empty cycle is a
    # zero-novelty cycle and contributes to the run.
    assert consecutive_zero_novelty_run((cycle, _cycle(1))) == 1
    assert evaluate_saturation(_record(cycle, _cycle(1))).saturated is False


# ---------------------------------------------------------------------------
# Rule-table shape: ordered, first-match-wins, total default
# ---------------------------------------------------------------------------


def test_saturation_rule_table_is_ordered_and_first_match_wins() -> None:
    # The table order is normative: R-SAT-S1 (met) before R-SAT-S2 (below)
    # before R-SAT-D1 (default). A run of 2 matches S1 first even though S2
    # also matches (2 is not < 2, so it does not -- but 3 matches S1 and not
    # S2); a run below the rule matches S2; an unknown run matches only D1.
    rule_ids = [rule.rule_id for rule in SATURATION_RULES]
    assert rule_ids == ["R-SAT-S1", "R-SAT-S2", "R-SAT-D1"]
    saturated = evaluate_saturation(_record(_cycle(0), _cycle(1)))
    assert saturated.matched_rule_id == "R-SAT-S1"
    winner_index = [d.rule_id for d in saturated.decisions].index("R-SAT-S1")
    assert saturated.decisions[winner_index].matched is True
    assert all(
        not d.matched for d in saturated.decisions[:winner_index]
    )
    below = evaluate_saturation(_record(_cycle(0)))
    assert below.matched_rule_id == "R-SAT-S2"
    unresolved = evaluate_saturation(_record())
    assert unresolved.matched_rule_id == "R-SAT-D1"


def test_saturation_rule_table_is_total_with_a_default() -> None:
    # The trailing total default closes the table: R-SAT-D1 matches every
    # record, so every recorded history gets exactly one verdict and
    # evaluation never fails to decide.
    assert SATURATION_RULES[-1].rule_id == "R-SAT-D1"
    assert SATURATION_RULES[-1].verdict is SaturationVerdict.UNRESOLVED
    for record in (
        _record(),
        _record(_cycle(0)),
        _record(_cycle(0), _cycle(1)),
        _record(_cycle(0, new_eligible=3)),
        _record(_cycle(0, completed=False)),
        _record(_cycle(0), _cycle(1, expansion=False)),
    ):
        assert SATURATION_RULES[-1].predicate(record) is True
        assessment = evaluate_saturation(record)
        assert assessment.matched_rule_id in {
            rule.rule_id for rule in SATURATION_RULES
        }
        assert assessment.decisions[-1].matched is True
        assert len(assessment.decisions) == len(SATURATION_RULES)
        assert [d.rule_id for d in assessment.decisions] == rule_ids_of(
            SATURATION_RULES
        )


def rule_ids_of(rules: tuple[SaturationRule, ...]) -> list[str]:
    """Helper: the rule ids of a rule table, in order."""
    return [rule.rule_id for rule in rules]


def test_saturation_evaluator_is_pure_and_deterministic() -> None:
    # Equal records -> equal assessments; repeated evaluation of the same
    # record yields identical assessments (AC-02 determinism); the verdict
    # depends only on the recorded history.
    a = evaluate_saturation(_record(_cycle(0), _cycle(1)))
    b = evaluate_saturation(_record(_cycle(0), _cycle(1)))
    assert isinstance(a, SaturationAssessment)
    assert a == b
    for _ in range(3):
        assert evaluate_saturation(_record(_cycle(0), _cycle(1))) == a
    # Records that differ only in family coverage differ in the recorded
    # field but keep the same operational verdict.
    covered = evaluate_saturation(
        _record(_cycle(0), _cycle(1), families_completed=True)
    )
    assert covered.verdict is SaturationVerdict.SATURATED
    assert covered.required_search_families_completed is True


def test_saturation_run_is_a_pure_function_of_the_recorded_cycles() -> None:
    # The run function is deterministic and monotonic in the trailing run,
    # and never reads anything but the recorded cycles.
    assert consecutive_zero_novelty_run(()) is None
    assert consecutive_zero_novelty_run((_cycle(0),)) == 1
    assert consecutive_zero_novelty_run((_cycle(0), _cycle(1))) == 2
    assert consecutive_zero_novelty_run((_cycle(0), _cycle(1), _cycle(2))) == 3
    assert consecutive_zero_novelty_run((_cycle(0, new_eligible=4),)) == 0
    assert consecutive_zero_novelty_run((_cycle(0, completed=False),)) is None
    assert consecutive_zero_novelty_run((_cycle(0), _cycle(1, completed=False))) is None
    assert consecutive_zero_novelty_run(
        (_cycle(0), _cycle(1, completed=False), _cycle(2))
    ) == 1
    assert consecutive_zero_novelty_run(
        (_cycle(0, new_eligible=2), _cycle(1, new_eligible=0), _cycle(2, new_eligible=0))
    ) == 2


# ---------------------------------------------------------------------------
# The frozen ClosureLiterature connection (composition by the closure layer)
# ---------------------------------------------------------------------------


def test_saturation_assessment_connects_to_the_frozen_closure_literature() -> None:
    # The assessment fills exactly the frozen ClosureLiterature fields
    # (core/models.py): required_search_families_completed,
    # consecutive_zero_novelty_cycles (None | int >= 0),
    # required_zero_novelty_cycles (default 2).
    assessment = evaluate_saturation(
        _record(_cycle(0), _cycle(1), families_completed=True)
    )
    literature = assessment.to_closure_literature()
    assert isinstance(literature, ClosureLiterature)
    assert literature.required_search_families_completed is True
    assert literature.consecutive_zero_novelty_cycles == 2
    assert literature.required_zero_novelty_cycles == 2
    unresolved = evaluate_saturation(_record()).to_closure_literature()
    assert unresolved.consecutive_zero_novelty_cycles is None
    assert unresolved.required_zero_novelty_cycles == 2


def test_saturation_assessment_composes_with_the_frozen_closure_gate() -> None:
    # The closure layer (core/rules/closure.py, DEV-M2-G05) composes the
    # family-completion requirement with the zero-novelty-cycle count:
    # research saturation is satisfied exactly when the families are
    # completed AND the count meets the required rule -- the normative
    # reading this module documents and feeds.
    def closure_saturation_state(
        families: bool | None, *cycles: SearchCycle
    ) -> str:
        assessment = evaluate_saturation(
            _record(*cycles, families_completed=families)
        )
        contract = ClosureContract(
            closure_id="CL-1",
            frozen=True,
            statistical_sufficiency={},
            execution_validity={},
            diagnosis={},
            recovery=ClosureRecovery(),
            literature=assessment.to_closure_literature(),
        )
        record = CoreClosureRecord.from_closure_contract(
            contract,
            statistics_sufficient=True,
            execution_valid=True,
        )
        closure = evaluate_closure(record)
        return next(
            decision.state.value
            for decision in closure.gate_decisions
            if decision.gate_id.value == "research_saturation"
        )

    # Two consecutive zero-novelty cycles + families completed: satisfied.
    assert closure_saturation_state(True, _cycle(0), _cycle(1)) == "SATISFIED"
    # Two consecutive zero-novelty cycles but families not (yet) completed:
    # the composed research-saturation gate is NOT satisfied.
    assert closure_saturation_state(False, _cycle(0), _cycle(1)) == "NOT_SATISFIED"
    assert closure_saturation_state(None, _cycle(0), _cycle(1)) == "UNRESOLVED"
    # One zero-novelty cycle, families completed: the count is below the
    # required rule, so the gate is NOT satisfied (AC-01 composed).
    assert closure_saturation_state(True, _cycle(0)) == "NOT_SATISFIED"


# ---------------------------------------------------------------------------
# Validation: boundaries, errors, record invariants
# ---------------------------------------------------------------------------


def test_saturation_type_errors_at_public_boundaries() -> None:
    # TypeError (not ValueError) at every public boundary: wrong evaluator
    # inputs, wrong tracker inputs, wrong run inputs, wrong factory inputs,
    # non-mapping from_dict inputs.
    with pytest.raises(TypeError):
        evaluate_saturation("R-SAT")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_saturation(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_saturation(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses("H-1", ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses(42, ())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses((), "H-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses((), 42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses(  # type: ignore[list-item]
            [{"hypothesis_ref": "H-1"}], ()
        )
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses((), (42,))  # type: ignore[list-item]
    with pytest.raises(TypeError):
        track_new_eligible_hypotheses((), (), eligibility="R-ELIG-V01")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        consecutive_zero_novelty_run("R-SAT")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        consecutive_zero_novelty_run((42,))  # type: ignore[list-item]
    with pytest.raises(TypeError):
        EligibilityRule.from_reliability_registry(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EligibilityRule.from_reliability_registry(  # type: ignore[arg-type]
            ReliabilityChecklistRegistry(), minimum_reliability=True
        )
    with pytest.raises(TypeError):
        SearchCycle.from_dict(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SaturationRecord.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_saturation_errors_are_value_error_subclasses_with_stable_messages() -> None:
    # Error hierarchy: record errors and eligibility errors are both
    # SaturationError subclasses, which is a ValueError subclass; every
    # message names the offending value and the reason.
    assert issubclass(SaturationRecordError, SaturationError)
    assert issubclass(SaturationEligibilityError, SaturationError)
    assert issubclass(SaturationError, ValueError)
    # Empty search family.
    with pytest.raises(SaturationRecordError) as exc_info:
        SearchCycle(cycle_index=0, search_family="", completed=True, new_eligible_hypotheses=0)
    assert "search_family" in str(exc_info.value)
    # Negative novelty count.
    with pytest.raises(SaturationRecordError) as exc_info:
        SearchCycle(cycle_index=0, search_family="f", completed=True, new_eligible_hypotheses=-1)
    assert ">= 0" in str(exc_info.value)
    # Negative cycle index.
    with pytest.raises(SaturationRecordError) as exc_info:
        SearchCycle(cycle_index=-2, search_family="f", completed=True, new_eligible_hypotheses=0)
    assert "cycle_index" in str(exc_info.value)
    # Unknown from_dict keys and missing keys are rejected.
    with pytest.raises(SaturationRecordError) as exc_info:
        SearchCycle.from_dict({**_cycle(0).to_dict(), "familiy": "citations"})
    assert "unknown" in str(exc_info.value)
    with pytest.raises(SaturationRecordError) as exc_info:
        SaturationRecord.from_dict(
            {**_record().to_dict(), "required_zero_novelties": 2}
        )
    assert "unknown" in str(exc_info.value)
    # A candidate without a reference cannot exist.
    with pytest.raises(SaturationEligibilityError) as exc_info:
        HypothesisCandidate(
            hypothesis_ref="",
            assessment=_assessment(reliability=3, directness=2),
        )
    assert "hypothesis_ref" in str(exc_info.value)
    # Empty known-eligible members are rejected.
    with pytest.raises(SaturationEligibilityError) as exc_info:
        track_new_eligible_hypotheses((), ("",))
    assert "non-empty" in str(exc_info.value)
    # Out-of-range checklist minimum is rejected.
    with pytest.raises(SaturationEligibilityError) as exc_info:
        EligibilityRule.from_reliability_registry(
            ReliabilityChecklistRegistry(), minimum_reliability=7
        )
    assert "0-4" in str(exc_info.value)


def test_saturation_record_requires_strictly_increasing_cycle_indexes() -> None:
    # A cycle history is a sequence: out-of-order or duplicated indexes are
    # rejected up front (stable message), so the run computation can never
    # silently depend on argument order. (Constructed directly: the _record
    # helper renumbers cycles by position.)
    with pytest.raises(SaturationRecordError) as exc_info:
        SaturationRecord(cycles=(_cycle(0), _cycle(0)))
    assert "strictly increasing" in str(exc_info.value)
    with pytest.raises(SaturationRecordError) as exc_info:
        SaturationRecord(cycles=(_cycle(1), _cycle(0)))
    assert "strictly increasing" in str(exc_info.value)
    # Reordering the same cycles changes the run -- the record rejects the
    # ambiguity instead of guessing.
    ordered = _record(_cycle(0), _cycle(1))
    assert evaluate_saturation(ordered).consecutive_zero_novelty_cycles == 2


def test_saturation_record_rejects_malformed_histories_and_states() -> None:
    # Non-sequence histories, non-cycle elements, non-tri-state family
    # coverage and non-int/bool required counts are all rejected with
    # stable messages.
    with pytest.raises(SaturationRecordError):
        SaturationRecord(cycles="R-SAT")  # type: ignore[arg-type]
    with pytest.raises(SaturationRecordError):
        SaturationRecord(cycles=("not-a-cycle",))  # type: ignore[arg-type]
    with pytest.raises(SaturationRecordError):
        SaturationRecord(required_search_families_completed="yes")  # type: ignore[arg-type]
    with pytest.raises(SaturationRecordError):
        SaturationRecord(required_zero_novelty_cycles=True)  # type: ignore[arg-type]
    with pytest.raises(SaturationRecordError):
        SaturationRecord(required_zero_novelty_cycles=0)
    # A non-expansion cycle can be recorded; it is a break, never a silent
    # expansion.
    non_expansion = _cycle(0, expansion=False)
    assert non_expansion.expansion is False
    assert consecutive_zero_novelty_run((non_expansion,)) is None


def test_saturation_cycle_parameter_has_no_default_for_required_fields() -> None:
    # The four essential cycle facts (index, family, completed, novelty
    # count) have no defaults: a recorded cycle always carries all of them
    # (the expansion flag is the only defaulted field).
    parameters = inspect.signature(SearchCycle).parameters
    for name in (
        "cycle_index",
        "search_family",
        "completed",
        "new_eligible_hypotheses",
    ):
        assert parameters[name].default is inspect.Parameter.empty
    assert parameters["expansion"].default is True
