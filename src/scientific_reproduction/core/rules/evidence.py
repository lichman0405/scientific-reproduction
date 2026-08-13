"""Deterministic Authority/Reliability/Directness evidence rules (DEV-M2-G03).

Pure logic implementing the frozen evidence rubric of ``06-EVIDENCE-SYSTEM.md``
against the frozen assessment structure of ``schemas/evidence.schema.yaml``
(model: ``core.models.EvidenceAssessment``). No LLM, no randomness, no
wall-clock dependence: every public function returns the same answer for the
same inputs on every platform and Python version.

Normative principles implemented here
-------------------------------------
* Source x Claim (06-EVIDENCE-SYSTEM.md SS1): an assessment is always a
  (source, claim) pair -- never a global score for a whole paper. The
  assessment hook ``assess`` takes the source and the claim together and
  produces the Authority/Reliability/Directness triple.
* Reliability is checklist/rule-derived (SS2, architecture decision 18):
  ``reliability_score`` computes 0-4 from a structured checklist of the nine
  dimensions the spec requires ("original/raw data available?", "method
  sufficiently complete?", ...), mapped by a **versioned rule**
  (``RELIABILITY_RULE_VERSION``). There is no API path that accepts a
  directly-assigned reliability score: ``reliability_score`` has no score
  parameter, ``assess`` has no reliability parameter, and the checklist
  reference that the frozen schema requires
  (``assessment.reliability_checklist_ref``) is always derived from a
  ``ReliabilityChecklist`` object or supplied explicitly for a raw dict of
  answers -- never invented.
* Composite score (SS3): ``ranking_score`` is a weighted, versioned,
  display/search-ranking-only number. Hard-gate predicates (SS4) never read
  it; they evaluate the raw axes, so they can run on assessments that carry
  no ranking score at all (AC-03).
* Hard gates (SS4): reliability gate, directness gate, recovery-hypothesis
  eligibility (v0.1 default ``R >= 3 and D >= 2 and scientifically_actionable``)
  and acceptance-criterion support (``R >= 3`` with the documented
  authoritative-standard / target-paper exceptions) are standalone functions,
  independent of any weighted display score.

The rule versions are frozen names, not code patches: bumping the mapping
means adding a new versioned function/constant, keeping old versions
reproducible for audit (M2 milestone acceptance: "Evidence ... rules are
auditable").
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping

from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    EvidenceAssessment,
    ResearchSource,
    SourceType,
)

__all__ = [
    # checklist model
    "ReliabilityChecklist",
    "RELIABILITY_CHECKLIST_DIMENSIONS",
    "RELIABILITY_RULE_VERSION",
    # errors
    "EvidenceRulesError",
    "ReliabilityChecklistError",
    # scoring
    "reliability_score",
    "authority_grade",
    "AUTHORITY_BY_SOURCE_TYPE",
    "RankingWeights",
    "RANKING_RULE_VERSION",
    "DEFAULT_RANKING_WEIGHTS",
    "ranking_score",
    # assessment hook
    "assess",
    "validate_assessment_against_checklist",
    # hard-gate predicates (AC-03)
    "reliability_gate_passes",
    "directness_gate_passes",
    "recovery_hypothesis_eligible",
    "acceptance_support_qualifies",
    "count_independent_qualifying_sources",
]

#: Version of the checklist-answer -> Reliability mapping rule (SS2:
#: "A versioned rule maps checklist answers to 0-4 Reliability").
RELIABILITY_RULE_VERSION = "reliability-rule-v1"

#: Version of the weighted composite rule (SS3: weights are configurable and
#: versioned).
RANKING_RULE_VERSION = "ranking-rule-v1"

#: The nine reliability checklist dimensions the frozen spec requires
#: ("Required checklist dimensions should include at least:"). Each entry is
#: ``(key, question)``; the final dimension is the negative signal
#: (retraction/correction/methodological defect).
RELIABILITY_CHECKLIST_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("raw_data_available", "original/raw data available?"),
    ("method_complete", "method sufficiently complete?"),
    ("independent_replication_performed", "independent replication performed?"),
    ("uncertainty_reported", "uncertainty/variation reported?"),
    ("independent_external_validation", "independent external validation?"),
    ("data_internally_consistent", "data internally consistent?"),
    ("conclusion_supported_by_data", "conclusion supported by data?"),
    ("material_identity_controlled", "material/sample identity controlled?"),
    (
        "known_retraction_correction_defect",
        "known retraction/correction/methodological defect?",
    ),
)

_CHECKLIST_DIMENSION_KEYS: tuple[str, ...] = tuple(
    key for key, _ in RELIABILITY_CHECKLIST_DIMENSIONS
)
_POSITIVE_DIMENSION_KEYS: tuple[str, ...] = _CHECKLIST_DIMENSION_KEYS[:-1]
#: The negative checklist dimension: its presence is a disqualifying signal.
NEGATIVE_DIMENSION_KEY = _CHECKLIST_DIMENSION_KEYS[-1]


class EvidenceRulesError(ValueError):
    """Base error for the evidence rule engine.

    Raised when rule inputs violate the frozen rubric: out-of-range axis
    grades, malformed checklists, missing checklist references, or invalid
    ranking weights.
    """


class ReliabilityChecklistError(EvidenceRulesError):
    """Raised when a reliability score cannot be computed as specified.

    Covers checklists with unknown/missing dimensions and assessments whose
    reliability is requested without a checklist reference (AC-01): the
    rule engine never produces a reliability score from anything other than
    checklist inputs plus a reference to the checklist record.
    """


@dataclasses.dataclass(frozen=True)
class ReliabilityChecklist:
    """Structured answers to the nine frozen reliability checklist dimensions.

    Attributes:
        checklist_ref: reference to the persisted checklist record (the
            value stored as ``assessment.reliability_checklist_ref``). Must
            be a non-empty string: a checklist without a record reference
            cannot back an assessment (AC-01).
        raw_data_available: original/raw data available?
        method_complete: method sufficiently complete?
        independent_replication_performed: independent replication performed?
        uncertainty_reported: uncertainty/variation reported?
        independent_external_validation: independent external validation?
        data_internally_consistent: data internally consistent?
        conclusion_supported_by_data: conclusion supported by data?
        material_identity_controlled: material/sample identity controlled?
        known_retraction_correction_defect: known retraction/correction/
            methodological defect? (negative signal)
    """

    checklist_ref: str
    raw_data_available: bool
    method_complete: bool
    independent_replication_performed: bool
    uncertainty_reported: bool
    independent_external_validation: bool
    data_internally_consistent: bool
    conclusion_supported_by_data: bool
    material_identity_controlled: bool
    known_retraction_correction_defect: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.checklist_ref, str)
            or not self.checklist_ref.strip()
        ):
            raise ReliabilityChecklistError(
                "ReliabilityChecklist.checklist_ref must be a non-empty"
                " string: reliability cannot be produced without a checklist"
                " reference (AC-01)"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReliabilityChecklist":
        """Build a checklist from a plain mapping with the canonical keys.

        Strict by design (deterministic and auditable): every one of the
        nine frozen dimension keys must be present and boolean, the optional
        ``checklist_ref`` key supplies the reference, and unknown keys are
        rejected so a typo cannot silently change the score.

        Raises:
            ReliabilityChecklistError: missing dimension keys, non-boolean
                answers, unknown keys, or a missing/empty reference.
        """
        unknown = set(data) - {*_CHECKLIST_DIMENSION_KEYS, "checklist_ref"}
        if unknown:
            raise ReliabilityChecklistError(
                "unknown reliability checklist key(s):"
                f" {', '.join(sorted(unknown))}; expected the frozen"
                f" dimensions {', '.join(_CHECKLIST_DIMENSION_KEYS)}"
            )
        missing = [
            key for key in _CHECKLIST_DIMENSION_KEYS if key not in data
        ]
        if missing:
            raise ReliabilityChecklistError(
                "reliability checklist missing dimension(s):"
                f" {', '.join(sorted(missing))}"
            )
        for key in _CHECKLIST_DIMENSION_KEYS:
            if not isinstance(data[key], bool):
                raise ReliabilityChecklistError(
                    f"reliability checklist dimension {key!r} must be a"
                    f" bool, got {type(data[key]).__name__}"
                )
        ref = data.get("checklist_ref")
        if not isinstance(ref, str) or not ref:
            raise ReliabilityChecklistError(
                "reliability checklist has no non-empty 'checklist_ref':"
                " reliability cannot be produced without a checklist"
                " reference (AC-01)"
            )
        return cls(checklist_ref=ref, **{key: data[key] for key in _CHECKLIST_DIMENSION_KEYS})  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical mapping (dimension keys plus checklist_ref)."""
        return {
            **{key: getattr(self, key) for key in _CHECKLIST_DIMENSION_KEYS},
            "checklist_ref": self.checklist_ref,
        }

    def as_mapping(self) -> dict[str, bool]:
        """Return only the dimension answers (no reference key)."""
        return {key: getattr(self, key) for key in _CHECKLIST_DIMENSION_KEYS}


# ---------------------------------------------------------------------------
# Reliability rule (AC-01)
# ---------------------------------------------------------------------------


def reliability_score(
    checklist: ReliabilityChecklist | Mapping[str, Any],
) -> int:
    """Compute the 0-4 Reliability score from checklist inputs only.

    The reliability rubric of 06-EVIDENCE-SYSTEM.md SS2 is mapped by the
    versioned rule ``RELIABILITY_RULE_VERSION``:

    * ``known_retraction_correction_defect`` is a disqualifying signal: a
      source with a known retraction/correction/methodological defect scores
      0 regardless of the other answers.
    * Otherwise the score is banded on the count of satisfied positive
      dimensions (there are eight): 8 -> 4, 6-7 -> 3, 4-5 -> 2, 2-3 -> 1,
      0-1 -> 0.

    The signature accepts *only* checklist inputs -- there is no parameter
    for a directly-assigned reliability value (AC-01). A ``ReliabilityChecklist``
    object carries its own ``checklist_ref``; a raw mapping must be a
    canonical answer dict (see ``ReliabilityChecklist.from_dict``) -- the
    optional ``checklist_ref`` key is tolerated so a
    ``ReliabilityChecklist.to_dict()`` output can be passed directly.

    Raises:
        ReliabilityChecklistError: the mapping is not a canonical checklist
            (missing/unknown/typo'd dimensions, non-boolean answers).
    """
    if isinstance(checklist, ReliabilityChecklist):
        answers = checklist.as_mapping()
    else:
        # Strict canonical-dict validation: the optional non-dimension key
        # "checklist_ref" is tolerated (so a ReliabilityChecklist.to_dict()
        # round-trips through this function), everything else must be one of
        # the nine frozen dimension keys with a boolean value.
        allowed = {*_CHECKLIST_DIMENSION_KEYS, "checklist_ref"}
        unknown = set(checklist) - allowed
        if unknown:
            raise ReliabilityChecklistError(
                "unknown reliability checklist key(s):"
                f" {', '.join(sorted(unknown))}; expected the frozen"
                f" dimensions {', '.join(_CHECKLIST_DIMENSION_KEYS)}"
            )
        missing = [
            key for key in _CHECKLIST_DIMENSION_KEYS if key not in checklist
        ]
        if missing:
            raise ReliabilityChecklistError(
                "reliability checklist missing dimension(s):"
                f" {', '.join(sorted(missing))}"
            )
        for key in _CHECKLIST_DIMENSION_KEYS:
            if not isinstance(checklist[key], bool):
                raise ReliabilityChecklistError(
                    f"reliability checklist dimension {key!r} must be a"
                    f" bool, got {type(checklist[key]).__name__}"
                )
        answers = {key: checklist[key] for key in _CHECKLIST_DIMENSION_KEYS}
    if answers[NEGATIVE_DIMENSION_KEY]:
        return 0
    satisfied = sum(1 for key in _POSITIVE_DIMENSION_KEYS if answers[key])
    if satisfied >= 8:
        return 4
    if satisfied >= 6:
        return 3
    if satisfied >= 4:
        return 2
    if satisfied >= 2:
        return 1
    return 0


def validate_assessment_against_checklist(
    assessment: EvidenceAssessment | Mapping[str, Any],
    checklist: ReliabilityChecklist | Mapping[str, Any],
) -> bool:
    """Return True when ``assessment.reliability`` matches the checklist rule.

    The audit hook for AC-01: a stored assessment is trustworthy only if its
    reliability axis equals ``reliability_score(checklist)`` and its
    ``reliability_checklist_ref`` matches the checklist's reference. Raises
    ``ReliabilityChecklistError`` when the assessment carries no checklist
    reference or the checklist has no valid reference to compare against.

    Raises:
        ReliabilityChecklistError: assessment lacks a
            ``reliability_checklist_ref``, or the checklist reference is
            missing/empty.
    """
    assessment_ref = _mapping_get(assessment, "reliability_checklist_ref")
    if (
        not isinstance(assessment_ref, str)
        or not assessment_ref.strip()
    ):
        raise ReliabilityChecklistError(
            "assessment has no non-empty 'reliability_checklist_ref': a"
            " reliability score cannot exist without a checklist reference"
            " (AC-01)"
        )
    checklist_record_ref: str | None
    if isinstance(checklist, ReliabilityChecklist):
        checklist_record_ref = checklist.checklist_ref
        score = reliability_score(checklist)
    else:
        checklist_record_ref = checklist.get("checklist_ref")
        if (
            not isinstance(checklist_record_ref, str)
            or not checklist_record_ref.strip()
        ):
            raise ReliabilityChecklistError(
                "checklist has no non-empty 'checklist_ref': cannot verify"
                " the assessment against it (AC-01)"
            )
        score = reliability_score(checklist)
    return (
        assessment_ref == checklist_record_ref
        and _axis(assessment, "reliability") == score
    )


# ---------------------------------------------------------------------------
# Authority rule (SS2)
# ---------------------------------------------------------------------------

#: Deterministic Authority grade by source type, from the SS2 rubric.
#: ``None`` marks the types the rubric leaves to judgment (a review may be a
#: strong or a limited secondary compilation; a database record may be an
#: authoritative official database or a limited compilation; ``other`` is
#: traceability-dependent) -- for those, ``assess`` requires an explicit
#: authority grade.
AUTHORITY_BY_SOURCE_TYPE: dict[SourceType, int | None] = {
    SourceType.TARGET_PAPER: 4,  # target paper/SI
    SourceType.SUPPLEMENTARY_INFORMATION: 4,  # target paper/SI
    SourceType.DATASET: 4,  # primary deposited data
    SourceType.STRUCTURE_DEPOSITION: 4,  # primary deposited data
    SourceType.STANDARD: 4,  # authoritative official database/standard
    SourceType.OFFICIAL_DOCUMENTATION: 4,  # authoritative official source
    SourceType.PEER_REVIEWED_PAPER: 3,  # strong peer-reviewed scholarly source
    SourceType.THESIS: 3,  # detailed thesis
    SourceType.PREPRINT: 2,  # preprint
    SourceType.VENDOR_NOTE: 2,  # vendor application note
    SourceType.INFORMAL: 1,  # informal technical source
    SourceType.REVIEW: None,  # strong vs limited secondary compilation
    SourceType.DATABASE_RECORD: None,  # authoritative vs limited database
    SourceType.OTHER: None,  # traceability-dependent
}


def authority_grade(source_type: SourceType) -> int | None:
    """Return the rubric-determined Authority grade (0-4) for ``source_type``.

    Returns None for the source types the SS2 rubric leaves to judgment
    (``review``, ``database_record``, ``other``); ``assess`` then requires an
    explicit authority grade. Authority measures *what type of source this
    is* and does not imply reproducibility (06-EVIDENCE-SYSTEM.md SS2).
    """
    return AUTHORITY_BY_SOURCE_TYPE.get(source_type)


# ---------------------------------------------------------------------------
# Composite display score (SS3)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RankingWeights:
    """Versioned weights for the display-only composite score (SS3).

    Attributes:
        authority: weight of the Authority axis (default 0.25).
        reliability: weight of the Reliability axis (default 0.45).
        directness: weight of the Directness axis (default 0.30).
    """

    authority: float = 0.25
    reliability: float = 0.45
    directness: float = 0.30


#: v1 weights from 06-EVIDENCE-SYSTEM.md SS3 (``ranking_rule_v1``).
DEFAULT_RANKING_WEIGHTS = RankingWeights()


def ranking_score(
    authority: int,
    reliability: int,
    directness: int,
    *,
    weights: RankingWeights | None = None,
) -> float:
    """Weighted composite 0-100 score, for search ranking/display only (SS3).

    ``ranking_score = (w_a*A + w_r*R + w_d*D) / 4 * 100`` with the versioned
    ``RANKING_RULE_VERSION`` weights (defaults from SS3). The score is a
    search-ranking aid and must not replace hard gates (SS3; AC-03): the
    gate predicates never read it.

    Raises:
        EvidenceRulesError: an axis is outside 0-4, or the weights are
            negative or do not sum to 1 (within floating-point tolerance).
    """
    for name, axis_value in (
        ("authority", authority),
        ("reliability", reliability),
        ("directness", directness),
    ):
        _check_axis(name, axis_value)
    used = weights if weights is not None else DEFAULT_RANKING_WEIGHTS
    for name, weight_value in (
        ("authority", used.authority),
        ("reliability", used.reliability),
        ("directness", used.directness),
    ):
        if not isinstance(weight_value, (int, float)) or isinstance(
            weight_value, bool
        ):
            raise EvidenceRulesError(
                f"ranking weight {name!r} must be a number, got"
                f" {type(weight_value).__name__}"
            )
        if weight_value < 0:
            raise EvidenceRulesError(
                f"ranking weight {name!r} must be non-negative, got"
                f" {weight_value}"
            )
    total = used.authority + used.reliability + used.directness
    if abs(total - 1.0) > 1e-9:
        raise EvidenceRulesError(
            f"ranking weights must sum to 1, got {total!r} (rule"
            f" {RANKING_RULE_VERSION})"
        )
    raw = (
        used.authority * authority
        + used.reliability * reliability
        + used.directness * directness
    ) / 4.0 * 100.0
    return round(raw, 2)


# ---------------------------------------------------------------------------
# Source x Claim assessment hook (AC-02)
# ---------------------------------------------------------------------------


def assess(
    *,
    source: ResearchSource | SourceType,
    claim_id: str,
    checklist: ReliabilityChecklist | Mapping[str, Any],
    directness: int,
    authority: int | None = None,
    checklist_ref: str | None = None,
    weights: RankingWeights | None = None,
) -> EvidenceAssessment:
    """Assess one (source, claim) pair into the structured A/R/D triple.

    Implements the Source x Claim rule (06-EVIDENCE-SYSTEM.md SS1): an
    assessment always binds a source to a claim, never a global score. The
    returned ``EvidenceAssessment`` is the frozen core model whose
    ``to_dict()`` satisfies ``schemas/evidence.schema.yaml``.

    Deterministic derivation:
    * reliability -- from ``checklist`` via ``reliability_score`` (AC-01);
      a ``ReliabilityChecklist`` contributes its own ``checklist_ref``; a
      raw answer mapping requires ``checklist_ref`` to be given, otherwise
      the assessment cannot be produced (AC-01).
    * authority -- from ``source.source_type`` via the SS2 rubric; an
      explicit ``authority`` overrides the rubric and is required for the
      types the rubric leaves to judgment (``review``, ``database_record``,
      ``other``).
    * directness -- supplied per (source, claim) pair: directness is
      question-specific (SS5), so no global default exists; it must be a
      0-4 rubric level.
    * ranking_score -- the versioned weighted composite (SS3), for display
      only; hard gates never read it (AC-03).

    Args:
        source: the source under assessment (``ResearchSource`` or bare
            ``SourceType``). Only ``source_type`` participates in the rule.
        claim_id: the claim being assessed (a claim-specific evidence item
            is always Source x Claim).
        checklist: reliability checklist answers (AC-01).
        directness: 0-4 Directness rubric level for this (source, claim)
            pair.
        authority: optional explicit 0-4 Authority grade; defaults to the
            rubric grade for the source type, and is required (ValueError)
            for types without a rubric-fixed grade.
        checklist_ref: optional checklist reference; when ``checklist`` is a
            raw mapping this is required (AC-01), otherwise it defaults to
            the ``ReliabilityChecklist.checklist_ref``.
        weights: optional ranking weights (defaults to the v1 SS3 weights).

    Raises:
        TypeError: ``source`` is not a ``ResearchSource``/``SourceType`` or
            ``claim_id`` is not a string.
        ValueError: unknown ``SourceType`` member, or a required explicit
            authority/directness is missing or out of 0-4 range.
        ReliabilityChecklistError: the checklist answers/ref are not
            canonical (AC-01).
        EvidenceRulesError: invalid ranking weights.
    """
    if isinstance(source, ResearchSource):
        source_type = source.source_type
    elif isinstance(source, SourceType):
        source_type = source
    else:
        raise TypeError(
            "assess expects a ResearchSource or SourceType, got"
            f" {type(source).__name__}"
        )
    if not isinstance(claim_id, str):
        raise TypeError(
            f"claim_id must be a str, got {type(claim_id).__name__}"
        )
    _check_axis("directness", directness)
    if authority is None:
        authority = authority_grade(source_type)
        if authority is None:
            raise ValueError(
                f"source_type {source_type.value!r} has no rubric-fixed"
                " authority grade; pass an explicit authority (0-4) per the"
                " SS2 rubric"
            )
    _check_axis("authority", authority)
    reliability = reliability_score(checklist)
    if isinstance(checklist, ReliabilityChecklist):
        ref: str | None = (
            checklist_ref if checklist_ref is not None else checklist.checklist_ref
        )
    else:
        ref = checklist_ref
        if ref is None:
            raise ReliabilityChecklistError(
                "assess requires checklist_ref when checklist answers are"
                " passed as a raw mapping: reliability cannot be produced"
                " without a checklist reference (AC-01)"
            )
    if not isinstance(ref, str) or not ref.strip():
        raise ReliabilityChecklistError(
            "reliability_checklist_ref must be a non-empty string (AC-01)"
        )
    return EvidenceAssessment(
        authority=authority,
        reliability=reliability,
        directness=directness,
        reliability_checklist_ref=ref,
        ranking_score=ranking_score(
            authority, reliability, directness, weights=weights
        ),
    )


# ---------------------------------------------------------------------------
# Hard-gate predicates (AC-03)
# ---------------------------------------------------------------------------


def reliability_gate_passes(
    assessment: EvidenceAssessment | Mapping[str, Any],
    *,
    minimum: int = 3,
) -> bool:
    """True when the assessment's Reliability axis meets ``minimum``.

    A hard gate (06-EVIDENCE-SYSTEM.md SS4): evaluates the raw Reliability
    axis only -- the weighted display score is never consulted (AC-03).
    Accepts a frozen ``EvidenceAssessment`` or a plain mapping (e.g. a
    stored record's ``assessment`` dict).
    """
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise TypeError(
            f"minimum must be an int, got {type(minimum).__name__}"
        )
    return _axis(assessment, "reliability") >= minimum


def directness_gate_passes(
    assessment: EvidenceAssessment | Mapping[str, Any],
    *,
    minimum: int = 2,
) -> bool:
    """True when the assessment's Directness axis meets ``minimum``.

    A hard gate (SS4) that reads only the Directness axis, never the
    weighted display score (AC-03).
    """
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise TypeError(
            f"minimum must be an int, got {type(minimum).__name__}"
        )
    return _axis(assessment, "directness") >= minimum


def recovery_hypothesis_eligible(
    assessment: EvidenceAssessment | Mapping[str, Any],
    *,
    scientifically_actionable: bool = True,
) -> bool:
    """v0.1 recovery-hypothesis eligibility gate (06-EVIDENCE-SYSTEM.md SS4).

    ``R >= 3`` and ``D >= 2`` and ``scientifically_actionable``. Lower-quality
    evidence may be stored as hypothesis context but cannot independently
    trigger a formal Recovery modification. Reads only the raw axes and the
    explicit actionability flag -- never the weighted display score (AC-03).

    Args:
        assessment: the (source, claim) assessment to gate.
        scientifically_actionable: whether the hypothesis is scientifically
            actionable; the v0.1 default rule evaluates this flag as True.
    """
    if not isinstance(scientifically_actionable, bool):
        raise TypeError(
            "scientifically_actionable must be a bool, got"
            f" {type(scientifically_actionable).__name__}"
        )
    return (
        reliability_gate_passes(assessment, minimum=3)
        and directness_gate_passes(assessment, minimum=2)
        and scientifically_actionable
    )


def acceptance_support_qualifies(
    assessment: EvidenceAssessment | Mapping[str, Any],
    *,
    authoritative_standard: bool = False,
    target_paper_defines: bool = False,
) -> bool:
    """Acceptance-criterion-change gate (06-EVIDENCE-SYSTEM.md SS4).

    Requires ``R >= 3``. The spec's additional *preference* for at least two
    independent qualifying sources is an aggregate concern -- evaluated with
    ``count_independent_qualifying_sources`` over the candidate evidence
    set -- and is lifted when the source is an authoritative standard or the
    target paper itself defines the claimed parameter (those two flags relax
    the gate entirely, per the SS4 exception clause). Never reads the
    weighted display score (AC-03).
    """
    for name, flag in (
        ("authoritative_standard", authoritative_standard),
        ("target_paper_defines", target_paper_defines),
    ):
        if not isinstance(flag, bool):
            raise TypeError(
                f"{name} must be a bool, got {type(flag).__name__}"
            )
    if authoritative_standard or target_paper_defines:
        return True
    return reliability_gate_passes(assessment, minimum=3)


def count_independent_qualifying_sources(
    evidences: Iterable[ClaimSpecificEvidence | Mapping[str, Any]],
    *,
    minimum_reliability: int = 3,
) -> int:
    """Count distinct sources whose assessment meets the reliability gate.

    Aggregate counterpart of the acceptance gate (SS4: "preferably at least
    two independent qualifying sources"): each evidence item contributes
    its ``source_id`` when its assessment's Reliability axis meets
    ``minimum_reliability``, and the count is over distinct source ids, so
    multiple mirrors/items of one paper count once. Mirrors of the same
    source therefore never inflate independence (SS7 deduplication
    principle). Reads only ``source_id`` and the Reliability axis -- never
    the weighted display score (AC-03).

    Args:
        evidences: evidence records (frozen ``ClaimSpecificEvidence`` or
            schema-shaped mappings with ``source_id`` and an ``assessment``
            dict).

    Raises:
        EvidenceRulesError: an evidence mapping lacks ``source_id`` or a
            nested ``assessment``.
        TypeError: an element is neither a ``ClaimSpecificEvidence`` nor a
            mapping.
    """
    qualifying: set[str] = set()
    for item in evidences:
        source_id, assessment = _split_evidence(item)
        if _axis(assessment, "reliability") >= minimum_reliability:
            qualifying.add(source_id)
    return len(qualifying)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_evidence(
    item: ClaimSpecificEvidence | Mapping[str, Any],
) -> tuple[str, EvidenceAssessment | Mapping[str, Any]]:
    """Extract ``(source_id, assessment)`` from an evidence record.

    Accepts a frozen ``ClaimSpecificEvidence`` or a schema-shaped mapping
    (``source_id`` plus a nested ``assessment`` dict).

    Raises:
        EvidenceRulesError: a mapping lacks a string ``source_id`` or a
            mapping ``assessment``.
        TypeError: ``item`` is neither a ``ClaimSpecificEvidence`` nor a
            mapping.
    """
    if isinstance(item, ClaimSpecificEvidence):
        return item.source_id, item.assessment
    if isinstance(item, Mapping):
        source_id = item.get("source_id")
        assessment = item.get("assessment")
        if not isinstance(source_id, str):
            raise EvidenceRulesError(
                "evidence mapping lacks a string 'source_id'"
            )
        if not isinstance(assessment, Mapping):
            raise EvidenceRulesError(
                "evidence mapping lacks an 'assessment' object"
            )
        return source_id, assessment
    raise TypeError(
        "expected ClaimSpecificEvidence or mapping evidence records, got"
        f" {type(item).__name__}"
    )


def _axis(
    assessment: EvidenceAssessment | Mapping[str, Any], name: str
) -> int:
    """Read axis ``name`` (authority/reliability/directness) from an assessment.

    Accepts a frozen ``EvidenceAssessment`` or a mapping with integer values
    (the schema's assessment dict, e.g. produced by ``to_dict()`` or loaded
    from a stored record).

    Raises:
        EvidenceRulesError: the mapping lacks the axis or its value is not
            an int.
    """
    if isinstance(assessment, EvidenceAssessment):
        return getattr(assessment, name)
    if isinstance(assessment, Mapping):
        value = assessment.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvidenceRulesError(
                f"assessment mapping lacks an integer {name!r} axis"
            )
        return value
    raise TypeError(
        f"assessment must be an EvidenceAssessment or a mapping, got"
        f" {type(assessment).__name__}"
    )


def _mapping_get(
    assessment: EvidenceAssessment | Mapping[str, Any], key: str
) -> Any:
    """Read ``key`` from an ``EvidenceAssessment`` or a plain mapping."""
    if isinstance(assessment, EvidenceAssessment):
        return getattr(assessment, key)
    return assessment.get(key)


def _check_axis(name: str, value: int) -> None:
    """Validate a 0-4 rubric axis grade (SS2: all axes are 0-4 integers)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvidenceRulesError(
            f"{name} must be an int, got {type(value).__name__}"
        )
    if not 0 <= value <= 4:
        raise EvidenceRulesError(
            f"{name} must be within the 0-4 rubric range, got {value}"
        )
