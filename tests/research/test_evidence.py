"""Tests for the claim-specific evidence registry (DEV-M5-G03).

Acceptance coverage (exact AC test names below):

  * AC-01 -- ``test_ac01_*``: a source cannot have a single global
    reliability field standing in for all claims. Proven structurally
    (the frozen ``ResearchSource`` model carries no reliability/directness
    field, the registry has no source-alone assessment API, and every
    assessment lookup requires a claim argument) and behaviorally (only
    claim-scoped lookups answer; lookups without a claim fail loudly).
  * AC-02 -- ``test_ac02_*``: the same source carries different
    Directness/Reliability assessments for different claims. Proven
    behaviorally (two claims over one source hold independent, both
    retrievable) and structurally (the registry is immutable and purely
    additive, so no registration can clobber an earlier assessment).
  * AC-03 -- ``test_ac03_*``: used-by links are stored. The record's
    ``used_by`` refs (Goals/decisions using the evidence, 06-EVIDENCE-
    SYSTEM.md SS6) are stored, retrievable per claim, preserved through
    serialization round-trips, and never dropped.

The registry is also proven to be the data layer the evidence rules of
``core/rules/evidence.py`` consume (``count_independent_qualifying_sources``
and the hard-gate predicates accept registry records/assessments directly),
and unassessed (source, claim) pairs resolve to absence (``None``) -- the
frozen vocabulary defines no UNDETERMINED evidence state, so the registry
invents none.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import pytest

from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    EvidenceAssessment,
    ResearchSource,
    SourceType,
)
from scientific_reproduction.core.rules.evidence import (
    count_independent_qualifying_sources,
    directness_gate_passes,
    reliability_gate_passes,
)
from scientific_reproduction.research.evidence import (
    EvidenceDuplicateError,
    EvidenceRegistrationError,
    EvidenceRegistry,
    EvidenceRegistryError,
)

CHECKLIST_REF = "RCHK-DEV-M5-G03-001"


def _source(source_id: str = "SRC-1") -> ResearchSource:
    """Build a frozen ResearchSource with compact defaults."""
    return ResearchSource(
        source_id=source_id,
        source_type=SourceType.PEER_REVIEWED_PAPER,
        title="A test source",
        provenance="test fixture",
    )


def _evidence(
    evidence_id: str = "EVID-1",
    source_id: str = "SRC-1",
    claim_id: str = "CLAIM-1",
    finding: str = "The source reports the claimed parameter value.",
    authority: int = 3,
    reliability: int = 3,
    directness: int = 2,
    ranking_score: float | None = 50.0,
    used_by: list[str] | None = None,
    **kwargs: Any,
) -> ClaimSpecificEvidence:
    """Build a frozen ClaimSpecificEvidence with compact defaults."""
    return ClaimSpecificEvidence(
        evidence_id=evidence_id,
        source_id=source_id,
        claim_id=claim_id,
        finding=finding,
        assessment=EvidenceAssessment(
            authority=authority,
            reliability=reliability,
            directness=directness,
            reliability_checklist_ref=CHECKLIST_REF,
            ranking_score=ranking_score,
        ),
        used_by=list(used_by) if used_by is not None else [],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Registry shape and determinism
# ---------------------------------------------------------------------------


def test_registry_is_an_immutable_frozen_dataclass() -> None:
    registry = EvidenceRegistry.from_records([_evidence()])
    with pytest.raises(dataclasses.FrozenInstanceError):
        registry.records = ()  # type: ignore[misc]


def test_registry_register_returns_a_new_registry_without_mutating_the_old() -> None:
    first = EvidenceRegistry()
    second = first.register(_evidence(evidence_id="EVID-1"))
    third = second.register(_evidence(evidence_id="EVID-2", claim_id="CLAIM-2"))
    assert len(first) == 0
    assert len(second) == 1
    assert len(third) == 2
    # The earlier registries keep answering exactly what they held: no
    # later registration ever changes an earlier registry (AC-02).
    assert first.get_assessment("SRC-1", "CLAIM-1") is None
    assert second.get_assessment("SRC-1", "CLAIM-1") is not None
    assert second.get_assessment("SRC-1", "CLAIM-2") is None


def test_registry_from_records_preserves_registration_order() -> None:
    records = (
        _evidence(evidence_id="EVID-1", claim_id="CLAIM-1"),
        _evidence(evidence_id="EVID-2", claim_id="CLAIM-2"),
        _evidence(evidence_id="EVID-3", claim_id="CLAIM-3"),
    )
    registry = EvidenceRegistry.from_records(records)
    assert [r.evidence_id for r in registry] == [
        "EVID-1",
        "EVID-2",
        "EVID-3",
    ]
    assert [r.claim_id for r in registry] == ["CLAIM-1", "CLAIM-2", "CLAIM-3"]
    assert len(registry) == 3


def test_registry_lookups_are_deterministic_pure_functions() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(evidence_id="EVID-1", claim_id="CLAIM-1"),
            _evidence(evidence_id="EVID-2", claim_id="CLAIM-2"),
        ]
    )
    first = registry.get_assessment("SRC-1", "CLAIM-1")
    second = registry.get_assessment("SRC-1", "CLAIM-1")
    assert first is not None and second is not None
    assert first == second
    assert registry.sources() == ("SRC-1",)
    assert registry.claims() == ("CLAIM-1", "CLAIM-2")
    # Repeated queries never mutate the registry.
    assert len(registry) == 2


# ---------------------------------------------------------------------------
# AC-01: no global reliability field for a source (claim-scoped by
# construction)
# ---------------------------------------------------------------------------


def test_ac01_frozen_source_model_has_no_global_reliability_field() -> None:
    # The prohibition is structural at the frozen-model level: the source
    # record that the registry consumes carries no reliability/directness
    # field at all, so no assessment can be attached to a source alone.
    field_names = {field.name for field in dataclasses.fields(ResearchSource)}
    for global_assessment_field in ("reliability", "directness", "authority"):
        assert global_assessment_field not in field_names


def test_ac01_every_assessment_lookup_requires_a_claim_argument() -> None:
    # Structural proof at the API level: the claim argument is mandatory
    # (no default), so "reliability of source S" is unanswerable.
    signature = inspect.signature(EvidenceRegistry.get_assessment)
    assert "claim_id" in signature.parameters
    assert (
        signature.parameters["claim_id"].default is inspect.Parameter.empty
    )
    assert (
        signature.parameters["source"].default is inspect.Parameter.empty
    )
    # Behaviorally: calling without a claim fails loudly, never returning
    # some source-global value.
    registry = EvidenceRegistry.from_records([_evidence()])
    with pytest.raises(TypeError):
        registry.get_assessment("SRC-1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        registry.get_assessment("SRC-1", "CLAIM-1", "extra")  # type: ignore[call-arg]


def test_ac01_registry_offers_no_source_alone_assessment_api() -> None:
    # The registry cannot answer "reliability of source S": no method name
    # that reads as a source-global assessment exists on the class.
    registry = EvidenceRegistry()
    for forbidden in (
        "reliability",
        "source_reliability",
        "reliability_of",
        "directness_of",
        "assessment_of_source",
    ):
        assert not hasattr(registry, forbidden)
    # And the records themselves never carry a source-global assessment:
    # every stored record is a ClaimSpecificEvidence bound to one claim.
    assert all(
        isinstance(record, ClaimSpecificEvidence)
        for record in registry.records
    )


def test_ac01_reliability_is_answered_only_for_a_claim() -> None:
    # Behavioral AC-01: the same source yields different reliability values
    # for different claims -- there is no single value the registry could
    # return for the source alone.
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                claim_id="CLAIM-HIGH",
                reliability=3,
                directness=2,
            ),
            _evidence(
                evidence_id="EVID-2",
                claim_id="CLAIM-LOW",
                reliability=1,
                directness=4,
            ),
        ]
    )
    assert registry.get_assessment("SRC-1", "CLAIM-HIGH") is not None
    assert registry.get_assessment("SRC-1", "CLAIM-LOW") is not None
    # No callable path returns a reliability without a claim: the two
    # lookups above are the ONLY assessment-returning calls, both
    # claim-scoped (signature test in test_ac01_every_assessment_lookup_requires_a_claim_argument).
    with pytest.raises(TypeError):
        registry.get_assessment("SRC-1")


# ---------------------------------------------------------------------------
# AC-02: same source, different claims -> independent assessments
# ---------------------------------------------------------------------------


def test_ac02_same_source_different_claims_carry_independent_assessments() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                claim_id="CLAIM-ADS-001",
                reliability=3,
                directness=2,
            ),
            _evidence(
                evidence_id="EVID-2",
                claim_id="CLAIM-ADS-002",
                reliability=1,
                directness=4,
            ),
        ]
    )
    first = registry.get_assessment("SRC-1", "CLAIM-ADS-001")
    second = registry.get_assessment("SRC-1", "CLAIM-ADS-002")
    assert first is not None and second is not None
    # The same source carries different Reliability AND different Directness
    # for the two claims.
    assert first.reliability == 3
    assert second.reliability == 1
    assert first.directness == 2
    assert second.directness == 4
    # Neither clobbers the other: both are retrievable after both registrations.
    assert first.reliability != second.reliability
    assert registry.get_assessment("SRC-1", "CLAIM-ADS-001") == first
    assert registry.get_assessment("SRC-1", "CLAIM-ADS-002") == second
    assert registry.sources() == ("SRC-1",)
    assert registry.claims() == ("CLAIM-ADS-001", "CLAIM-ADS-002")


def test_ac02_registering_never_clobbers_an_earlier_assessment() -> None:
    # Functional register: the registry that held claim A is unchanged after
    # claim B is registered -- the AC-02 "no clobbering" property holds by
    # construction and is observable through every returned instance.
    registry_a = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                claim_id="CLAIM-A",
                reliability=3,
            )
        ]
    )
    registry_ab = registry_a.register(
        _evidence(
            evidence_id="EVID-2",
            claim_id="CLAIM-B",
            reliability=2,
            directness=1,
        )
    )
    assert registry_a.get_assessment("SRC-1", "CLAIM-A").reliability == 3  # type: ignore[union-attr]
    assert registry_a.get_assessment("SRC-1", "CLAIM-B") is None
    assert registry_ab.get_assessment("SRC-1", "CLAIM-A").reliability == 3  # type: ignore[union-attr]
    assert registry_ab.get_assessment("SRC-1", "CLAIM-B").reliability == 2  # type: ignore[union-attr]


def test_ac02_same_claim_different_sources_carry_independent_assessments() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                source_id="SRC-1",
                claim_id="CLAIM-X",
                reliability=4,
                directness=4,
            ),
            _evidence(
                evidence_id="EVID-2",
                source_id="SRC-2",
                claim_id="CLAIM-X",
                reliability=2,
                directness=1,
            ),
        ]
    )
    one = registry.get_assessment("SRC-1", "CLAIM-X")
    two = registry.get_assessment("SRC-2", "CLAIM-X")
    assert one is not None and two is not None
    assert (one.reliability, one.directness) == (4, 4)
    assert (two.reliability, two.directness) == (2, 1)
    assert registry.sources() == ("SRC-1", "SRC-2")
    assert registry.claims() == ("CLAIM-X",)


def test_ac02_multiple_records_for_one_pair_are_additive() -> None:
    # A (source, claim) pair may carry several records (different
    # extractions/source locations); registration is purely additive, the
    # first-registered record determines the deterministic lookup, and all
    # records stay retrievable.
    registry = EvidenceRegistry.from_records(
        [
            _evidence(evidence_id="EVID-1", claim_id="CLAIM-1", directness=2),
            _evidence(evidence_id="EVID-2", claim_id="CLAIM-1", directness=3),
        ]
    )
    assert registry.get("SRC-1", "CLAIM-1") is not None
    assert registry.get("SRC-1", "CLAIM-1").evidence_id == "EVID-1"  # type: ignore[union-attr]
    assert registry.get_assessment("SRC-1", "CLAIM-1").directness == 2  # type: ignore[union-attr]
    assert [r.evidence_id for r in registry.get_all("SRC-1", "CLAIM-1")] == [
        "EVID-1",
        "EVID-2",
    ]
    assert registry.get("SRC-1", "CLAIM-1").evidence_id == "EVID-1"  # type: ignore[union-attr]
    # A different pair with no records is untouched.
    assert registry.get_all("SRC-1", "CLAIM-OTHER") == ()


def test_ac02_assessments_are_typed_against_the_frozen_vocabulary() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(evidence_id="EVID-1", claim_id="CLAIM-1"),
            _evidence(evidence_id="EVID-2", claim_id="CLAIM-2"),
        ]
    )
    for claim_id in ("CLAIM-1", "CLAIM-2"):
        assessment = registry.get_assessment("SRC-1", claim_id)
        assert isinstance(assessment, EvidenceAssessment)
        for axis in ("authority", "reliability", "directness"):
            value = getattr(assessment, axis)
            assert isinstance(value, int)
            assert 0 <= value <= 4
        assert assessment.reliability_checklist_ref == CHECKLIST_REF


# ---------------------------------------------------------------------------
# AC-03: used-by links are stored and retrievable
# ---------------------------------------------------------------------------


def test_ac03_used_by_links_are_stored_and_retrievable() -> None:
    used_by = ["goal:G-01", "req:REQ-1", "decision:D-7"]
    registry = EvidenceRegistry.from_records(
        [_evidence(evidence_id="EVID-1", used_by=used_by)]
    )
    record = registry.get("SRC-1", "CLAIM-1")
    assert record is not None
    assert list(record.used_by) == used_by
    assert registry.used_by("SRC-1", "CLAIM-1") == tuple(used_by)
    assert registry.used_by(_source("SRC-1"), "CLAIM-1") == tuple(used_by)


def test_ac03_used_by_links_survive_serialization_round_trip() -> None:
    # "Stored, retrievable, never dropped": the frozen model's to_dict /
    # from_dict round-trip carries used_by verbatim, and a registry rebuilt
    # from the serialized records still answers the linkage.
    used_by = ["goal:G-01", "req:REQ-1"]
    original = _evidence(evidence_id="EVID-1", used_by=used_by)
    rebuilt = ClaimSpecificEvidence.from_dict(original.to_dict())
    registry = EvidenceRegistry.from_records([rebuilt])
    assert list(registry.get("SRC-1", "CLAIM-1").used_by) == used_by  # type: ignore[union-attr]
    assert registry.used_by("SRC-1", "CLAIM-1") == tuple(used_by)


def test_ac03_used_by_links_are_per_claim_and_independent() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                claim_id="CLAIM-1",
                used_by=["goal:G-01", "req:REQ-1"],
            ),
            _evidence(
                evidence_id="EVID-2",
                claim_id="CLAIM-2",
                used_by=["goal:G-02"],
            ),
        ]
    )
    assert registry.used_by("SRC-1", "CLAIM-1") == ("goal:G-01", "req:REQ-1")
    assert registry.used_by("SRC-1", "CLAIM-2") == ("goal:G-02",)
    # Each claim's link set is independent of the other's.
    assert registry.all_used_by() == ("goal:G-01", "req:REQ-1", "goal:G-02")


def test_ac03_all_used_by_aggregates_every_link_in_first_seen_order() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                claim_id="CLAIM-1",
                used_by=["goal:G-01", "req:REQ-1"],
            ),
            _evidence(
                evidence_id="EVID-2",
                claim_id="CLAIM-2",
                used_by=["req:REQ-1", "goal:G-02"],
            ),
        ]
    )
    # Distinct refs, first-seen order; a ref shared by two claims appears once.
    assert registry.all_used_by() == ("goal:G-01", "req:REQ-1", "goal:G-02")


def test_ac03_empty_used_by_defaults_to_empty_tuple() -> None:
    registry = EvidenceRegistry.from_records([_evidence(evidence_id="EVID-1")])
    assert registry.used_by("SRC-1", "CLAIM-1") == ()
    assert registry.all_used_by() == ()


# ---------------------------------------------------------------------------
# Registration validation (stable errors)
# ---------------------------------------------------------------------------


def test_registry_rejects_non_evidence_records() -> None:
    registry = EvidenceRegistry()
    with pytest.raises(TypeError):
        registry.register({})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EvidenceRegistry.from_records([_evidence().to_dict()])  # type: ignore[list-item]


def test_registry_rejects_non_sequence_input() -> None:
    with pytest.raises(TypeError):
        EvidenceRegistry.from_records("SRC-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        EvidenceRegistry.from_records(42)  # type: ignore[arg-type]


def test_registry_rejects_out_of_range_axes() -> None:
    for reliability in (-1, 5):
        with pytest.raises(EvidenceRegistrationError) as exc_info:
            EvidenceRegistry.from_records(
                [_evidence(evidence_id="EVID-1", reliability=reliability)]
            )
        assert "0-4 rubric range" in str(exc_info.value)
    for directness in (-1, 5):
        with pytest.raises(EvidenceRegistrationError):
            EvidenceRegistry.from_records(
                [_evidence(evidence_id="EVID-1", directness=directness)]
            )
    with pytest.raises(EvidenceRegistrationError):
        EvidenceRegistry.from_records(
            [_evidence(evidence_id="EVID-1", authority=99)]
        )


def test_registry_accepts_rubric_boundary_axes() -> None:
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                claim_id="CLAIM-LOW",
                authority=0,
                reliability=0,
                directness=0,
            ),
            _evidence(
                evidence_id="EVID-2",
                claim_id="CLAIM-HIGH",
                authority=4,
                reliability=4,
                directness=4,
            ),
        ]
    )
    low = registry.get_assessment("SRC-1", "CLAIM-LOW")
    high = registry.get_assessment("SRC-1", "CLAIM-HIGH")
    assert low is not None and high is not None
    assert (low.authority, low.reliability, low.directness) == (0, 0, 0)
    assert (high.authority, high.reliability, high.directness) == (4, 4, 4)


def test_registry_rejects_blank_ids_and_finding() -> None:
    for field_name, value in (
        ("evidence_id", ""),
        ("source_id", ""),
        ("claim_id", ""),
        ("finding", ""),
    ):
        kwargs: dict[str, Any] = {field_name: value}
        if field_name != "evidence_id":
            kwargs["evidence_id"] = f"EVID-{field_name}"
        with pytest.raises(EvidenceRegistrationError) as exc_info:
            EvidenceRegistry.from_records([_evidence(**kwargs)])
        assert field_name in str(exc_info.value)


def test_registry_rejects_assessment_without_checklist_reference() -> None:
    # CLAUDE-CODE-HANDOFF.md M5 acceptance: Reliability cannot be written
    # without a checklist result reference.
    blank_ref = _evidence(evidence_id="EVID-1")
    blank_ref = dataclasses.replace(
        blank_ref,
        assessment=dataclasses.replace(blank_ref.assessment, reliability_checklist_ref=""),
    )
    with pytest.raises(EvidenceRegistrationError) as exc_info:
        EvidenceRegistry.from_records([blank_ref])
    assert "reliability_checklist_ref" in str(exc_info.value)


def test_registry_rejects_duplicate_evidence_id_without_clobbering() -> None:
    first = _evidence(evidence_id="EVID-1", claim_id="CLAIM-1", reliability=3)
    second = _evidence(evidence_id="EVID-1", claim_id="CLAIM-2", reliability=1)
    with pytest.raises(EvidenceDuplicateError) as exc_info:
        EvidenceRegistry.from_records([first, second])
    assert "EVID-1" in str(exc_info.value)
    assert issubclass(EvidenceDuplicateError, EvidenceRegistryError)
    assert issubclass(EvidenceRegistryError, ValueError)
    # The already-registered record is untouched by the failed registration.
    registry = EvidenceRegistry.from_records([first])
    with pytest.raises(EvidenceDuplicateError):
        registry.register(second)
    assert registry.get_assessment("SRC-1", "CLAIM-1").reliability == 3  # type: ignore[union-attr]


def test_registry_rejects_non_string_used_by_entries() -> None:
    with pytest.raises(EvidenceRegistrationError) as exc_info:
        EvidenceRegistry.from_records(
            [_evidence(evidence_id="EVID-1", used_by=["goal:G-01", 7])]  # type: ignore[list-item]
        )
    assert "used_by" in str(exc_info.value)


# ---------------------------------------------------------------------------
# The registry as the data layer for the evidence rules
# ---------------------------------------------------------------------------


def test_registry_is_the_data_layer_for_the_evidence_rules() -> None:
    # The rules consume ClaimSpecificEvidence records and EvidenceAssessment
    # objects; the registry provides both, so
    # count_independent_qualifying_sources(registry) works directly and
    # hard-gate predicates evaluate registry-stored assessments.
    registry = EvidenceRegistry.from_records(
        [
            _evidence(
                evidence_id="EVID-1",
                source_id="SRC-1",
                claim_id="CLAIM-1",
                reliability=3,
                directness=2,
            ),
            _evidence(
                evidence_id="EVID-2",
                source_id="SRC-1",  # mirror of SRC-1 must not double-count
                claim_id="CLAIM-2",
                reliability=4,
                directness=1,
            ),
            _evidence(
                evidence_id="EVID-3",
                source_id="SRC-2",
                claim_id="CLAIM-1",
                reliability=1,
                directness=3,
            ),
        ]
    )
    # One source per qualifying reliability >= 3: SRC-1 counts once.
    assert count_independent_qualifying_sources(registry) == 1
    # Hard gates over the registry's claim-scoped lookups.
    assessment_1 = registry.get_assessment("SRC-1", "CLAIM-1")
    assessment_2 = registry.get_assessment("SRC-1", "CLAIM-2")
    assert assessment_1 is not None and assessment_2 is not None
    assert reliability_gate_passes(assessment_1, minimum=3) is True
    assert directness_gate_passes(assessment_1, minimum=2) is True
    assert directness_gate_passes(assessment_2, minimum=2) is False
    assessment_3 = registry.get_assessment("SRC-2", "CLAIM-1")
    assert assessment_3 is not None
    assert reliability_gate_passes(assessment_3, minimum=3) is False


def test_registry_lookup_accepts_a_research_source_or_a_source_id() -> None:
    registry = EvidenceRegistry.from_records([_evidence(evidence_id="EVID-1")])
    by_id = registry.get_assessment("SRC-1", "CLAIM-1")
    by_object = registry.get_assessment(_source("SRC-1"), "CLAIM-1")
    by_other_object = registry.get_assessment(
        _source("SRC-2"), "CLAIM-1"
    )
    assert by_id == by_object
    assert by_id is not None
    assert by_other_object is None
    with pytest.raises(TypeError):
        registry.get_assessment(42, "CLAIM-1")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.records_for_source(42)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        registry.records_for_claim(42)  # type: ignore[arg-type]


def test_registry_unassessed_state_is_absence_not_an_invented_state() -> None:
    # The frozen vocabulary defines no UNDETERMINED evidence state; the
    # registry's unassessed state is absence -- None / empty tuple -- and
    # no invented state value ever appears.
    registry = EvidenceRegistry()
    assert registry.get("SRC-1", "CLAIM-1") is None
    assert registry.get_assessment("SRC-1", "CLAIM-1") is None
    assert registry.is_assessed("SRC-1", "CLAIM-1") is False
    assert registry.used_by("SRC-1", "CLAIM-1") == ()
    assert registry.get_all("SRC-1", "CLAIM-1") == ()
    assert registry.sources() == ()
    assert registry.claims() == ()
    assert registry.all_used_by() == ()
    assert len(registry) == 0
