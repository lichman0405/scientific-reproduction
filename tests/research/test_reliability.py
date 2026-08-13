"""Tests for the reliability checklist workflow (DEV-M5-G04).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_ac01_*``: Evidence Reliability cannot be accepted
    without a checklist record/reference. Proven structurally (the record
    cannot exist without a non-empty ``checklist_ref``; the score rule has
    no score parameter and no score-accepting API exists) and behaviorally
    (every produced score carries its checklist reference; the registry
    accepts a score only for a reference with a stored record; the derived
    score and reference feed the sibling evidence data layer and research
    requests unchanged).
  * AC-02 -- ``test_ac02_*``: the checklist records the raw-data,
    replication, uncertainty, method completeness, validation and
    consistency factors. Proven by name (the six core factors are frozen
    fields), by independence (each factor's answer is stored separately),
    by spec grounding (the six keys match the frozen nine-dimension
    checklist of ``06-EVIDENCE-SYSTEM.md`` SS2 as implemented in
    ``core/rules/evidence.py``), and by vocabulary (deterministic
    ``YES``/``NO``/``UNKNOWN`` enum answers -- no free text).
  * AC-03 -- ``test_ac03_*``: the rule result is reproducible from the
    stored checklist. Proven by determinism (same record -> same score),
    by persistence (``to_dict``/``from_dict`` round-trip and
    registry-stored records score identically), by the full rule trace
    recorded in every assessment, by an exhaustive 3**9 answer grid (every
    score is a schema-compatible 0-4 int and the total default rule closes
    the table), and by cross-layer consistency with the M2 core rubric
    (``core/rules/evidence.py``).

The module is also proven to follow the frozen rule-engine paradigm:
versioned constants, frozen immutable dataclasses, first-match-wins ordered
rule table with a total default, ``TypeError`` at public boundaries,
``ValueError``-subclass errors with stable messages, and pure deterministic
functions.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
from typing import Any

import pytest

from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    EvidenceAssessment,
)
from scientific_reproduction.core.rules.evidence import (
    RELIABILITY_CHECKLIST_DIMENSIONS,
)
from scientific_reproduction.core.rules.evidence import (
    ReliabilityChecklist as CoreReliabilityChecklist,
)
from scientific_reproduction.core.rules.evidence import (
    reliability_score as core_reliability_score,
)
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.research.reliability import (
    ADDITIONAL_DIMENSION_KEYS,
    CHECKLIST_DIMENSIONS,
    CHECKLIST_VOCABULARY_VERSION,
    CORE_FACTOR_KEYS,
    NEGATIVE_DIMENSION_KEY,
    POSITIVE_DIMENSION_KEYS,
    RELIABILITY_RULES,
    RELIABILITY_RULESET_VERSION,
    ChecklistAnswer,
    ReliabilityAssessment,
    ReliabilityChecklistDuplicateError,
    ReliabilityChecklistError,
    ReliabilityChecklistRecord,
    ReliabilityChecklistRecordError,
    ReliabilityChecklistRegistry,
    evaluate_reliability,
)
from scientific_reproduction.research.requests import issue_research_request

#: The six AC-02 core factors, by name (AC-02: raw-data, replication,
#: uncertainty, method completeness, validation and consistency).
SIX_CORE_FACTOR_NAMES: tuple[str, ...] = (
    "raw_data_available",
    "independent_replication_performed",
    "uncertainty_reported",
    "method_complete",
    "independent_external_validation",
    "data_internally_consistent",
)

#: The three additional spec dimensions (06-EVIDENCE-SYSTEM.md SS2).
ADDITIONAL_DIMENSION_NAMES: tuple[str, ...] = (
    "conclusion_supported_by_data",
    "material_identity_controlled",
    "known_retraction_correction_defect",
)


def _record(
    checklist_ref: str = "RCHK-001",
    **answers: ChecklistAnswer,
) -> ReliabilityChecklistRecord:
    """Build a frozen checklist record; all dimensions default to YES.

    The negative dimension defaults to NO (no known defect), so the default
    record scores 4.
    """
    base = {key: ChecklistAnswer.YES for key in POSITIVE_DIMENSION_KEYS}
    base[NEGATIVE_DIMENSION_KEY] = ChecklistAnswer.NO
    base.update(answers)
    return ReliabilityChecklistRecord(
        checklist_ref=checklist_ref, **base  # type: ignore[arg-type]
    )


def _score(record: ReliabilityChecklistRecord) -> int:
    """Convenience: the rule-derived score of one record."""
    return evaluate_reliability(record).score


# ---------------------------------------------------------------------------
# Model shape, versions and vocabulary
# ---------------------------------------------------------------------------


def test_ruleset_and_vocabulary_are_versioned_constants() -> None:
    assert isinstance(RELIABILITY_RULESET_VERSION, str)
    assert RELIABILITY_RULESET_VERSION
    assert isinstance(CHECKLIST_VOCABULARY_VERSION, str)
    assert CHECKLIST_VOCABULARY_VERSION
    # Stored records expose their vocabulary version; assessments record
    # the rule table version they were produced with (auditability).
    assert (
        ReliabilityChecklistRecord.vocabulary_version
        == CHECKLIST_VOCABULARY_VERSION
    )
    assert (
        evaluate_reliability(_record()).ruleset_version
        == RELIABILITY_RULESET_VERSION
    )


def test_checklist_answers_are_an_exact_enum_vocabulary() -> None:
    # The answer vocabulary is exactly YES/NO/UNKNOWN -- nothing invented.
    assert list(ChecklistAnswer) == [
        ChecklistAnswer.YES,
        ChecklistAnswer.NO,
        ChecklistAnswer.UNKNOWN,
    ]
    assert ChecklistAnswer.YES.value == "YES"
    assert ChecklistAnswer.NO.value == "NO"
    assert ChecklistAnswer.UNKNOWN.value == "UNKNOWN"


def test_checklist_record_is_an_immutable_frozen_dataclass() -> None:
    record = _record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.raw_data_available = ChecklistAnswer.NO  # type: ignore[misc]
    assert _score(record) == 4  # mutation attempt changed nothing


def test_checklist_dimensions_cover_the_nine_spec_dimensions() -> None:
    # The frozen nine-dimension checklist ("at least" per
    # 06-EVIDENCE-SYSTEM.md SS2): six AC-02 core factors plus the three
    # additional spec dimensions -- nothing from the spec is dropped.
    assert [key for key, _ in CHECKLIST_DIMENSIONS] == [
        *SIX_CORE_FACTOR_NAMES,
        *ADDITIONAL_DIMENSION_NAMES,
    ]
    assert CORE_FACTOR_KEYS == SIX_CORE_FACTOR_NAMES
    assert ADDITIONAL_DIMENSION_KEYS == ADDITIONAL_DIMENSION_NAMES
    assert POSITIVE_DIMENSION_KEYS == tuple(SIX_CORE_FACTOR_NAMES) + (
        "conclusion_supported_by_data",
        "material_identity_controlled",
    )
    assert NEGATIVE_DIMENSION_KEY == "known_retraction_correction_defect"


# ---------------------------------------------------------------------------
# AC-01: no reliability score without a checklist record/reference
# ---------------------------------------------------------------------------


def test_ac01_score_derivation_requires_a_checklist_record() -> None:
    # The score rule accepts only ReliabilityChecklistRecord inputs: no
    # raw dict, no string, no int can be turned into a score.
    for bad in (42, None, "RCHK-001", {}, {"raw_data_available": True}):
        with pytest.raises(TypeError) as exc_info:
            evaluate_reliability(bad)  # type: ignore[arg-type]
        assert "ReliabilityChecklistRecord" in str(exc_info.value)


def test_ac01_no_api_path_accepts_a_direct_score() -> None:
    # Structural proof at the API level: the only score-producing callable
    # has a single `checklist` parameter -- there is no score parameter, so
    # a reliability value can never be passed in directly.
    parameters = inspect.signature(evaluate_reliability).parameters
    assert list(parameters) == ["checklist"]
    assert parameters["checklist"].default is inspect.Parameter.empty
    # The checklist record itself carries no reliability score field.
    record_field_names = {
        field.name for field in dataclasses.fields(ReliabilityChecklistRecord)
    }
    assert "score" not in record_field_names
    assert "reliability" not in record_field_names
    # The registry has no method that accepts or stores a score.
    for forbidden in (
        "score",
        "register_score",
        "accept_score",
        "set_score",
        "assess",
        "add_score",
    ):
        assert not hasattr(ReliabilityChecklistRegistry, forbidden)


def test_ac01_every_produced_score_carries_its_checklist_reference() -> None:
    # "Producing a score returns both score and checklist ref": every
    # assessment pairs a 0-4 int with the non-empty reference of the
    # checklist record it was derived from -- the exact pair the frozen
    # assessment schema requires (reliability + reliability_checklist_ref).
    for ref in ("RCHK-001", "RCHK-002", "RCHK-003"):
        assessment = evaluate_reliability(_record(checklist_ref=ref))
        assert assessment.score == 4
        assert assessment.checklist_ref == ref
        assert isinstance(assessment.checklist_ref, str)
        assert assessment.checklist_ref
    # The reference is read from the stored record, never invented.
    assert (
        evaluate_reliability(_record(checklist_ref="RCHK-9")).checklist_ref
        == "RCHK-9"
    )


def test_ac01_record_cannot_be_created_without_a_reference() -> None:
    # A checklist record without a reference cannot exist: the constructor
    # rejects blank references (direct construction and from_dict alike),
    # and the parameter has no default so omitting it fails loudly.
    parameters = inspect.signature(ReliabilityChecklistRecord).parameters
    assert parameters["checklist_ref"].default is inspect.Parameter.empty
    for blank in ("", "   "):
        with pytest.raises(ReliabilityChecklistRecordError) as exc_info:
            _record(checklist_ref=blank)
        assert "checklist_ref" in str(exc_info.value)
    # from_dict rejects a blank or missing reference the same way.
    with pytest.raises(ReliabilityChecklistRecordError):
        ReliabilityChecklistRecord.from_dict(
            {**_record().to_dict(), "checklist_ref": ""}
        )
    without_ref = _record().to_dict()
    del without_ref["checklist_ref"]
    with pytest.raises(ReliabilityChecklistRecordError):
        ReliabilityChecklistRecord.from_dict(without_ref)


def test_ac01_registry_accepts_scores_only_for_stored_references() -> None:
    # The reference-acceptance path: a score can be accepted only for a
    # reference with a stored checklist record. An unknown reference raises
    # (no score against an unverifiable reference) and a non-str reference
    # raises TypeError.
    registry = ReliabilityChecklistRegistry.from_records(
        [_record(checklist_ref="RCHK-001")]
    )
    assert registry.evaluate("RCHK-001").score == 4
    with pytest.raises(ReliabilityChecklistRecordError) as exc_info:
        registry.evaluate("RCHK-NOPE")
    assert "RCHK-NOPE" in str(exc_info.value)
    assert "stored checklist record" in str(exc_info.value)
    with pytest.raises(TypeError):
        registry.evaluate(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.get(42)  # type: ignore[arg-type]
    # The stored record is what the score derives from: the registry score
    # equals the direct evaluation of the same stored record.
    stored = registry.get("RCHK-001")
    assert stored is not None
    assert registry.evaluate("RCHK-001") == evaluate_reliability(stored)


def test_ac01_score_and_reference_feed_the_evidence_handoff() -> None:
    # End-to-end AC-01: the derived (score, reference) pair is exactly what
    # the frozen assessment schema and the sibling layers consume -- the
    # evidence registry (DEV-M5-G03) accepts the assessment built from it,
    # and the research request rule (DEV-M5-G02) accepts the score as
    # minimum_reliability (0-4 per schemas/research-request.schema.yaml).
    assessment = evaluate_reliability(
        _record(checklist_ref="RCHK-AC01-HANDOFF")
    )
    evidence_assessment = EvidenceAssessment(
        authority=3,
        reliability=assessment.score,
        directness=2,
        reliability_checklist_ref=assessment.checklist_ref,
    )
    assert 0 <= evidence_assessment.reliability <= 4
    assert evidence_assessment.reliability_checklist_ref == "RCHK-AC01-HANDOFF"
    registry = EvidenceRegistry.from_records(
        [
            ClaimSpecificEvidence(
                evidence_id="EVID-1",
                source_id="SRC-1",
                claim_id="CLAIM-1",
                finding="The source reports the claimed parameter value.",
                assessment=evidence_assessment,
            )
        ]
    )
    stored = registry.get_assessment("SRC-1", "CLAIM-1")
    assert stored is not None
    assert stored.reliability == assessment.score
    assert stored.reliability_checklist_ref == assessment.checklist_ref
    for score in range(0, 5):
        request = issue_research_request(
            request_id=f"REQ-{score}",
            question="Is the reported parameter reproducible?",
            origin_refs=["goal:DEV-M5-G04"],
            minimum_reliability=score,
            issued_at="2026-01-01T00:00:00Z",
        )
        assert request.request.minimum_reliability == score


# ---------------------------------------------------------------------------
# AC-02: the six core factors are recorded
# ---------------------------------------------------------------------------


def test_ac02_record_exposes_the_six_core_factors_by_name() -> None:
    # The six AC-02 factors are first-class named fields of the checklist
    # model (raw-data, replication, uncertainty, method completeness,
    # validation and consistency).
    field_names = {
        field.name for field in dataclasses.fields(ReliabilityChecklistRecord)
    }
    for factor_name in SIX_CORE_FACTOR_NAMES:
        assert factor_name in field_names
    assert CORE_FACTOR_KEYS == SIX_CORE_FACTOR_NAMES
    # The field order follows AC-02's factor listing.
    assert tuple(field.name for field in dataclasses.fields(
        ReliabilityChecklistRecord
    ))[1:7] == SIX_CORE_FACTOR_NAMES


def test_ac02_six_core_factors_match_the_frozen_spec_dimensions() -> None:
    # Each of the six core factors is one of the frozen spec dimensions of
    # 06-EVIDENCE-SYSTEM.md SS2, with the same key and question text the M2
    # core rules use -- the two layers share one vocabulary.
    core_keys = [key for key, _ in RELIABILITY_CHECKLIST_DIMENSIONS]
    for factor_name in SIX_CORE_FACTOR_NAMES:
        assert factor_name in core_keys
    ours = {key: question for key, question in CHECKLIST_DIMENSIONS}
    theirs = dict(RELIABILITY_CHECKLIST_DIMENSIONS)
    for factor_name in SIX_CORE_FACTOR_NAMES:
        assert ours[factor_name] == theirs[factor_name]


def test_ac02_core_factor_answers_are_recorded_independently() -> None:
    # Each factor's answer is stored separately: a record that answers only
    # one factor YES stores YES for it and NO for every other core factor --
    # no factor is derived from or clobbered by another.
    for factor_name in SIX_CORE_FACTOR_NAMES:
        answers: dict[str, ChecklistAnswer] = {
            key: ChecklistAnswer.NO for key in SIX_CORE_FACTOR_NAMES
        }
        answers[factor_name] = ChecklistAnswer.YES
        record = _record(checklist_ref=f"RCHK-{factor_name}", **answers)
        for other_name in SIX_CORE_FACTOR_NAMES:
            if other_name == factor_name:
                assert getattr(record, other_name) == ChecklistAnswer.YES
            else:
                assert getattr(record, other_name) == ChecklistAnswer.NO
        # The answer is retrievable from the record itself, not inferred.
        assert record.as_mapping()[factor_name] == ChecklistAnswer.YES


def test_ac02_additional_spec_dimensions_are_also_recorded() -> None:
    # The three remaining spec dimensions (conclusion supported by data,
    # material/sample identity controlled, known retraction/correction/
    # methodological defect) are recorded as frozen checklist fields too,
    # so no required spec dimension is dropped from the model.
    field_names = {
        field.name for field in dataclasses.fields(ReliabilityChecklistRecord)
    }
    for dimension_name in ADDITIONAL_DIMENSION_NAMES:
        assert dimension_name in field_names
    record = _record(
        checklist_ref="RCHK-ADDITIONAL",
        conclusion_supported_by_data=ChecklistAnswer.NO,
        material_identity_controlled=ChecklistAnswer.UNKNOWN,
        known_retraction_correction_defect=ChecklistAnswer.NO,
    )
    assert record.conclusion_supported_by_data == ChecklistAnswer.NO
    assert record.material_identity_controlled == ChecklistAnswer.UNKNOWN
    assert record.known_retraction_correction_defect == ChecklistAnswer.NO
    assert ADDITIONAL_DIMENSION_KEYS == ADDITIONAL_DIMENSION_NAMES


def test_ac02_answers_are_deterministic_enum_values_not_free_text() -> None:
    # Answers are the enumerated YES/NO/UNKNOWN vocabulary -- free-form
    # text (which would break AC-03 reproducibility) is rejected at
    # construction and at from_dict.
    for bad in ("maybe", "yes", "Yes", "PARTIALLY", 1, True, None, 0.5):
        with pytest.raises(ReliabilityChecklistRecordError) as exc_info:
            _record(
                checklist_ref="RCHK-BAD",
                raw_data_available=bad,  # type: ignore[arg-type]
            )
        assert "raw_data_available" in str(exc_info.value)
    with pytest.raises(ReliabilityChecklistRecordError):
        ReliabilityChecklistRecord.from_dict(
            {**_record().to_dict(), "raw_data_available": "PARTIALLY"}
        )


# ---------------------------------------------------------------------------
# AC-03: the rule result is reproducible from the stored checklist
# ---------------------------------------------------------------------------


def test_ac03_rule_result_is_reproducible_from_the_stored_checklist() -> None:
    # The score is a pure function of the stored record: repeated
    # evaluation yields identical assessments, and the registry's stored
    # record reproduces exactly the same result as direct evaluation.
    record = _record(
        checklist_ref="RCHK-REPRO",
        raw_data_available=ChecklistAnswer.YES,
        independent_replication_performed=ChecklistAnswer.NO,
        uncertainty_reported=ChecklistAnswer.YES,
        method_complete=ChecklistAnswer.UNKNOWN,
        independent_external_validation=ChecklistAnswer.NO,
        data_internally_consistent=ChecklistAnswer.YES,
        conclusion_supported_by_data=ChecklistAnswer.YES,
        material_identity_controlled=ChecklistAnswer.UNKNOWN,
        known_retraction_correction_defect=ChecklistAnswer.NO,
    )
    first = evaluate_reliability(record)
    second = evaluate_reliability(record)
    assert isinstance(first, ReliabilityAssessment)
    assert first == second
    assert first.score == 2
    registry = ReliabilityChecklistRegistry.from_records([record])
    assert registry.evaluate("RCHK-REPRO") == first


def test_ac03_assessment_records_the_full_rule_trace() -> None:
    # Every rule evaluation is recorded in the assessment: one decision per
    # table rule, in table order, with exactly one match -- the matched
    # rule's score is the assessment's score and its id is recorded.
    assessment = evaluate_reliability(_record())
    assert len(assessment.decisions) == len(RELIABILITY_RULES)
    assert [d.rule_id for d in assessment.decisions] == [
        rule.rule_id for rule in RELIABILITY_RULES
    ]
    # The trace records every predicate result; the FIRST match wins: the
    # winning decision is the first one whose predicate matched (nothing
    # before it matched), its score is the assessment's score, and every
    # decision carries a description.
    winner_index = [d.rule_id for d in assessment.decisions].index(
        assessment.matched_rule_id
    )
    assert assessment.decisions[winner_index].matched is True
    assert all(
        not d.matched for d in assessment.decisions[:winner_index]
    )
    assert assessment.decisions[winner_index].score == assessment.score
    assert all(d.description for d in assessment.decisions)
    # A non-default outcome also records the trace.
    low = evaluate_reliability(
        _record(
            checklist_ref="RCHK-LOW",
            **{
                key: ChecklistAnswer.NO
                for key in POSITIVE_DIMENSION_KEYS
            },
        )
    )
    assert low.score == 0
    assert low.matched_rule_id == "R-REL-H5"
    assert [d.matched for d in low.decisions].count(True) == 1


def test_ac03_stored_checklist_survives_serialization_and_scores_identically() -> None:
    # The persisted form (to_dict/from_dict) round-trips exactly, and the
    # rule result is reproducible from the restored record (AC-03).
    record = _record(
        checklist_ref="RCHK-ROUNDTRIP",
        independent_replication_performed=ChecklistAnswer.UNKNOWN,
        uncertainty_reported=ChecklistAnswer.NO,
    )
    rebuilt = ReliabilityChecklistRecord.from_dict(record.to_dict())
    assert rebuilt == record
    assert rebuilt.to_dict() == record.to_dict()
    assert evaluate_reliability(rebuilt) == evaluate_reliability(record)
    # Restored records stored in the registry also evaluate by reference.
    registry = ReliabilityChecklistRegistry.from_records([rebuilt])
    assert registry.evaluate("RCHK-ROUNDTRIP").score == _score(record)


def test_ac03_score_is_a_deterministic_pure_function_of_the_record() -> None:
    # Two records with identical answers but different references score
    # identically; the same record always scores identically -- the score
    # depends only on the stored checklist, never on anything else.
    answers: dict[str, ChecklistAnswer] = {
        "raw_data_available": ChecklistAnswer.YES,
        "independent_replication_performed": ChecklistAnswer.NO,
        "uncertainty_reported": ChecklistAnswer.UNKNOWN,
        "method_complete": ChecklistAnswer.YES,
        "independent_external_validation": ChecklistAnswer.NO,
        "data_internally_consistent": ChecklistAnswer.YES,
        "conclusion_supported_by_data": ChecklistAnswer.YES,
        "material_identity_controlled": ChecklistAnswer.NO,
        "known_retraction_correction_defect": ChecklistAnswer.NO,
    }
    a = _record(checklist_ref="RCHK-A", **answers)
    b = _record(checklist_ref="RCHK-B", **answers)
    assert a != b  # different records (different references)...
    # ...but the same stored answers: four dimensions satisfied -> score 2.
    assert _score(a) == _score(b) == 2
    for _ in range(3):
        assert evaluate_reliability(a).score == 2
        assert evaluate_reliability(a) == evaluate_reliability(a)


def test_ac03_scores_cover_the_schema_range_exhaustively() -> None:
    # Exhaustive grid over all 3**9 answer combinations: every stored
    # checklist yields an int score in 0..4 (the frozen schema bounds for
    # assessment.reliability and request.minimum_reliability), the total
    # default rule closes the table (evaluation never fails), and the
    # score equals the first matching rule's score.
    answers_values = (
        ChecklistAnswer.YES,
        ChecklistAnswer.NO,
        ChecklistAnswer.UNKNOWN,
    )
    keys = (*POSITIVE_DIMENSION_KEYS, NEGATIVE_DIMENSION_KEY)
    for combo in itertools.product(answers_values, repeat=len(keys)):
        answers = dict(zip(keys, combo))
        record = _record(checklist_ref="RCHK-EXHAUSTIVE", **answers)
        assessment = evaluate_reliability(record)
        assert isinstance(assessment.score, int)
        assert 0 <= assessment.score <= 4
        expected_score = next(
            rule.score
            for rule in RELIABILITY_RULES
            if rule.predicate(record)
        )
        assert assessment.score == expected_score
        # The matched decision is the first matching rule, recorded as such.
        matched_decision = next(
            d for d in assessment.decisions
            if d.rule_id == assessment.matched_rule_id
        )
        assert matched_decision.matched is True
        assert matched_decision.score == assessment.score


def test_ac03_total_default_rule_closes_the_table() -> None:
    # The table ends in a total default rule: every valid record is matched
    # by R-REL-H5 at the latest, so evaluation can never fail to decide.
    assert RELIABILITY_RULES[-1].rule_id == "R-REL-H5"
    assert RELIABILITY_RULES[-1].predicate(_record()) is True
    assert RELIABILITY_RULES[-1].score == 0
    for record in (
        _record(),
        _record(checklist_ref="RCHK-2", **{key: ChecklistAnswer.NO for key in POSITIVE_DIMENSION_KEYS}),
        _record(checklist_ref="RCHK-3", **{key: ChecklistAnswer.UNKNOWN for key in POSITIVE_DIMENSION_KEYS}),
    ):
        assessment = evaluate_reliability(record)
        assert assessment.matched_rule_id in {
            rule.rule_id for rule in RELIABILITY_RULES
        }
        assert assessment.decisions[-1].matched is True


# ---------------------------------------------------------------------------
# Rule semantics: first-match-wins ordering and cross-layer consistency
# ---------------------------------------------------------------------------


def test_rule_table_is_ordered_and_first_match_wins() -> None:
    # First match wins: an all-satisfied record triggers R-REL-H1 (score 4)
    # even though H2 also matches; six satisfied triggers R-REL-H2 (score
    # 3); and the negative dimension disqualifies regardless of how many
    # positives are satisfied (R-REL-D0 fires before every H rule).
    assert _score(_record()) == 4
    assert _score(_record(checklist_ref="RCHK-6", independent_replication_performed=ChecklistAnswer.NO, independent_external_validation=ChecklistAnswer.NO)) == 3
    assert (
        evaluate_reliability(_record()).matched_rule_id == "R-REL-H1"
    )
    six = _record(
        checklist_ref="RCHK-6",
        independent_replication_performed=ChecklistAnswer.NO,
        independent_external_validation=ChecklistAnswer.NO,
    )
    assert _score(six) == 3
    assert evaluate_reliability(six).matched_rule_id == "R-REL-H2"
    # All positives satisfied but a known defect: D0 fires first -> 0.
    defective = _record(
        checklist_ref="RCHK-DEFECT",
        known_retraction_correction_defect=ChecklistAnswer.YES,
    )
    assert _score(defective) == 0
    assert evaluate_reliability(defective).matched_rule_id == "R-REL-D0"
    # An UNKNOWN never counts as satisfied: six YES out of eight scores 3
    # regardless of how many of the rest are UNKNOWN rather than NO.
    with_unknowns = _record(
        checklist_ref="RCHK-UNK",
        independent_replication_performed=ChecklistAnswer.UNKNOWN,
        independent_external_validation=ChecklistAnswer.UNKNOWN,
    )
    assert _score(with_unknowns) == 3
    assert evaluate_reliability(with_unknowns).matched_rule_id == "R-REL-H2"


def test_cross_layer_consistency_with_the_core_evidence_rules() -> None:
    # Normative reading: the research-layer rule table reproduces the M2
    # core rubric (core.rules.evidence.reliability_score) for every record
    # without UNKNOWN answers -- the two layers share the eight positive
    # dimensions, the same bands and the same disqualifying negative
    # signal, so the same underlying answers score the same at both layers.
    # Same positive-dimension set as the core rules (the six core factors
    # are ordered per AC-02, so the comparison is set-based), and the same
    # negative dimension.
    assert set(POSITIVE_DIMENSION_KEYS) == {
        key for key, _ in RELIABILITY_CHECKLIST_DIMENSIONS[:-1]
    }
    assert NEGATIVE_DIMENSION_KEY == RELIABILITY_CHECKLIST_DIMENSIONS[-1][0]
    keys = (*POSITIVE_DIMENSION_KEYS, NEGATIVE_DIMENSION_KEY)
    boolean_combos = (
        (ChecklistAnswer.YES, ChecklistAnswer.NO),
    ) * len(keys)
    checked = 0
    for combo in itertools.product(*boolean_combos):
        answers = dict(zip(keys, combo))
        record = _record(checklist_ref=f"RCHK-CROSS-{checked}", **answers)
        core_mapping: dict[str, Any] = {
            key: answers[key] is ChecklistAnswer.YES for key in keys
        }
        core_mapping["checklist_ref"] = record.checklist_ref
        assert _score(record) == core_reliability_score(core_mapping)
        checked += 1
    assert checked == 2**9
    # A CoreReliabilityChecklist built from the same answers (all YES/NO)
    # scores identically too, through the core object API.
    all_yes = _record(checklist_ref="RCHK-CORE-ALL-YES")
    core_checklist = CoreReliabilityChecklist(
        checklist_ref=all_yes.checklist_ref,
        **{
            key: getattr(all_yes, key) is ChecklistAnswer.YES
            for key in keys
        },
    )
    assert _score(all_yes) == core_reliability_score(core_checklist)


# ---------------------------------------------------------------------------
# Validation: vocabulary bounds, errors, registry semantics
# ---------------------------------------------------------------------------


def test_vocabulary_bounds_reject_invented_answers() -> None:
    # Direct construction: every dimension must be a ChecklistAnswer.
    for bad in (True, 42, "MAYBE", "yes"):
        with pytest.raises(ReliabilityChecklistRecordError):
            _record(
                checklist_ref="RCHK-BOUND",
                method_complete=bad,  # type: ignore[arg-type]
            )
    # from_dict: unknown keys, missing keys, non-string ref and
    # non-vocabulary answer strings are all rejected with stable messages.
    with pytest.raises(ReliabilityChecklistRecordError) as exc_info:
        ReliabilityChecklistRecord.from_dict(
            {**_record().to_dict(), "data_internally_consistency": "YES"}
        )
    assert "unknown" in str(exc_info.value)
    with pytest.raises(ReliabilityChecklistRecordError) as exc_info:
        data = _record().to_dict()
        del data["method_complete"]
        ReliabilityChecklistRecord.from_dict(data)
    assert "missing" in str(exc_info.value)
    with pytest.raises(ReliabilityChecklistRecordError):
        ReliabilityChecklistRecord.from_dict(
            {**_record().to_dict(), "checklist_ref": 7}  # type: ignore[dict-item]
        )
    with pytest.raises(ReliabilityChecklistRecordError):
        ReliabilityChecklistRecord.from_dict(
            {**_record().to_dict(), "uncertainty_reported": "maybe"}
        )


def test_errors_are_value_error_subclasses_with_stable_messages() -> None:
    # Error hierarchy: the record error and the duplicate error are both
    # ReliabilityChecklistError subclasses, which is a ValueError subclass;
    # every message names the offending value and the reason.
    assert issubclass(ReliabilityChecklistRecordError, ReliabilityChecklistError)
    assert issubclass(
        ReliabilityChecklistDuplicateError, ReliabilityChecklistError
    )
    assert issubclass(ReliabilityChecklistError, ValueError)
    with pytest.raises(ReliabilityChecklistError) as exc_info:
        _record(checklist_ref="")
    assert "checklist_ref" in str(exc_info.value)
    with pytest.raises(ReliabilityChecklistError) as exc_info:
        _record(
            checklist_ref="RCHK-ERR",
            uncertainty_reported="MAYBE",  # type: ignore[arg-type]
        )
    assert "uncertainty_reported" in str(exc_info.value)
    assert "YES/NO/UNKNOWN" in str(exc_info.value)
    with pytest.raises(ReliabilityChecklistError) as exc_info:
        ReliabilityChecklistRegistry.from_records(
            [
                _record(checklist_ref="RCHK-DUP"),
                _record(checklist_ref="RCHK-DUP"),
            ]
        )
    assert "RCHK-DUP" in str(exc_info.value)


def test_type_errors_at_public_boundaries() -> None:
    # TypeError (not ValueError) at every public boundary: non-record
    # evaluation inputs, non-sequence/non-record registry inputs, non-str
    # references, non-mapping from_dict input.
    with pytest.raises(TypeError):
        evaluate_reliability("RCHK-001")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        evaluate_reliability({"raw_data_available": True})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReliabilityChecklistRegistry.from_records("RCHK-001")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReliabilityChecklistRegistry.from_records(  # type: ignore[list-item]
            [{"checklist_ref": "RCHK-X"}]
        )
    with pytest.raises(TypeError):
        ReliabilityChecklistRegistry().register({})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ReliabilityChecklistRecord.from_dict(42)  # type: ignore[arg-type]


def test_registry_is_frozen_functional_and_duplicate_safe() -> None:
    registry = ReliabilityChecklistRegistry()
    with pytest.raises(dataclasses.FrozenInstanceError):
        registry.records = ()  # type: ignore[misc]
    first = registry.register(_record(checklist_ref="RCHK-1"))
    second = first.register(_record(checklist_ref="RCHK-2"))
    assert len(registry) == 0
    assert len(first) == 1
    assert len(second) == 2
    assert [r.checklist_ref for r in second] == ["RCHK-1", "RCHK-2"]
    # Duplicate references are rejected and never clobber the stored record.
    with pytest.raises(ReliabilityChecklistDuplicateError):
        first.register(_record(checklist_ref="RCHK-1"))
    assert first.get("RCHK-1") is not None
    # Registration order is preserved; unknown references resolve to None.
    assert registry.get("RCHK-1") is None
    assert first.get("RCHK-2") is None
    assert second.get("RCHK-1").checklist_ref == "RCHK-1"  # type: ignore[union-attr]
