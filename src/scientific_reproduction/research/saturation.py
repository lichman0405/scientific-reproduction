"""Search cycles and saturation records: the frozen two-zero-novelty-cycle
operational rule (DEV-M5-G05).

Implements the **search cycle model**, the **eligible-hypothesis novelty
tracker** and the **saturation evaluator** deliverables: a frozen record of
each search cycle with the number of *new* eligible Recovery hypotheses it
produced, the threshold-based tracker that derives that novelty count, and a
versioned, ordered rule table deciding the frozen v0.1 operational saturation
rule. The frozen spec grounds this module:

* ``09-RESEARCH-SUBSYSTEM.md`` SS7 ("Search saturation"): *Store each search
  cycle and the number of new eligible Recovery hypotheses. Default closure
  support requires two consecutive zero-novelty cycles after all required
  search families have been covered.*
* ``08-STRICT-RECOVERY-CLOSURE.md`` SS4: default v0.1 Recovery eligibility =
  Reliability >= 3, Directness >= 2, actionable = true; default v0.1
  operational saturation rule = *two consecutive expansion search cycles
  produce zero new eligible Recovery hypotheses*; *This is a governance
  rule, not a universal scientific constant; it must be configurable and
  frozen.*
* ``03-ROLE-AND-PERMISSION-SPEC.md``: Research may *record search
  saturation cycles*.
* ``core/models.py`` (frozen): ``ClosureLiterature``
  (``required_search_families_completed`` | None,
  ``consecutive_zero_novelty_cycles`` | None,
  ``required_zero_novelty_cycles: int = 2``) -- the frozen saturation-record
  fields this module fills; ``ClosureRecovery``
  (``eligible_hypotheses_total``, ``tested_or_ruled_out``, ``remaining``) --
  the eligible-pool context of the novelty tracker; ``EvidenceAssessment``
  (``authority``, ``reliability``, ``directness``,
  ``reliability_checklist_ref``) -- the per-hypothesis assessment the
  eligibility threshold evaluates.
* ``core/rules/closure.py`` (DEV-M2-G05, frozen): the closure layer's
  ``RESEARCH_SATURATION_RULES`` compose the family-completion requirement
  with the zero-novelty-cycle count -- the composition point this module's
  assessment feeds via :meth:`SaturationAssessment.to_closure_literature`.
* ``core/rules/evidence.py`` (DEV-M2-G03, frozen): the core
  Reliability/Directness hard-gate rules (``reliability_gate_passes``,
  ``directness_gate_passes``, ``recovery_hypothesis_eligible``) -- the
  rules the eligibility threshold references (AC-03).
* ``research/reliability.py`` (DEV-M5-G04, merged): ``evaluate_reliability``
  and ``ReliabilityChecklistRegistry`` -- the research-layer reliability
  hook an eligibility threshold may reference (AC-03).
* ``research/evidence.py`` (DEV-M5-G03, merged): the claim-specific
  evidence data layer (``EvidenceRegistry``,
  ``core.models.EvidenceAssessment`` handoff) whose assessments the tracker
  consumes unchanged.

Normative readings (locked here, proven in the tests)
-----------------------------------------------------
* **Cycle record**: one :class:`SearchCycle` records the cycle index, the
  search family covered, whether the cycle completed, whether it was an
  *expansion* cycle, and the number of *new* eligible Recovery hypotheses
  it produced (the novelty count, ``new_eligible_hypotheses``). A cycle is
  an expansion cycle unless it is explicitly recorded otherwise
  (``expansion=False``): expansion cycles are the search activity that
  grows coverage, and the spec's operational rule is stated over them;
  non-expansion cycles (e.g. re-verification passes) exist and are treated
  as breaks by the run semantics. Hypotheses are opaque string refs -- the
  frozen schemas define no ``Hypothesis`` object, so none is invented.
* **Consecutive-run semantics**: the consecutive zero-novelty run is
  counted over the recorded history in cycle order. A *completed expansion*
  cycle with zero new eligible hypotheses extends the run; a completed
  expansion cycle with novel hypotheses *resets* the run to zero; any cycle
  that is not a completed expansion cycle -- an incomplete cycle or a
  non-expansion cycle -- *breaks* the run. The run is a well-defined count
  exactly when the most recent recorded cycle is a completed expansion
  cycle; otherwise it is ``None`` (the count has not been established yet).
  This mirrors the frozen ``ClosureLiterature.consecutive_zero_novelty_cycles``
  field (``int | None``) and the closure layer's treatment of ``None`` as
  unresolved.
* **Family-completion interaction**: the evaluator takes the recorded
  family-coverage state (``required_search_families_completed``) as an
  *input* and records it in every assessment, but the two-consecutive-zero
  rule is the v0.1 **operational** rule and is exactly what the evaluator's
  verdict decides (AC-01: one zero-novelty cycle is insufficient regardless
  of family coverage). Research saturation (08-STRICT-RECOVERY-CLOSURE.md
  SS4: *All required search families have been completed and the configured
  saturation rule is satisfied*) is the conjunction, composed by the
  closure layer (``core/rules/closure.py`` ``R-SAT-1``) from the frozen
  ``ClosureLiterature`` fields this assessment fills.
* **Eligibility configurability**: "configurable and frozen" is realized as
  a named, versioned, frozen :class:`EligibilityRule` object -- the default
  is the frozen v0.1 predicate (Reliability >= 3, Directness >= 2,
  actionable = true) evaluated through the core hard-gate rules of
  ``core/rules/evidence.py`` (AC-03), and every alternative threshold is a
  versioned rule object recorded in every eligibility decision -- never an
  arbitrary runtime knob at evaluation time.

The module follows the frozen rule-engine paradigm of ``core/rules/`` and
the M4/M5 research-subsystem precedents (``research/dedupe.py``,
``research/requests.py``, ``research/reliability.py``): frozen input
dataclasses, ordered rule tables with first-match-wins plus a trailing total
default, ``RULESET_VERSION`` constants recorded in every assessment, pure
deterministic functions (no LLM, no randomness, no wall-clock), ``TypeError``
at public boundaries, a ``ValueError``-subclass error hierarchy with stable
path-free messages, ``from __future__ import annotations`` and ``__all__``
exports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence

from scientific_reproduction.core.models import (
    ClosureLiterature,
    EvidenceAssessment,
)
from scientific_reproduction.core.rules.evidence import (
    directness_gate_passes,
    reliability_gate_passes,
)
from scientific_reproduction.research.reliability import (
    ReliabilityChecklistRegistry,
    evaluate_reliability,
)

__all__ = [
    # versions and frozen v0.1 thresholds
    "SATURATION_RULESET_VERSION",
    "ELIGIBILITY_RULESET_VERSION",
    "V01_ELIGIBILITY_MIN_RELIABILITY",
    "V01_ELIGIBILITY_MIN_DIRECTNESS",
    "V01_ELIGIBILITY_ACTIONABLE",
    # errors
    "SaturationError",
    "SaturationRecordError",
    "SaturationEligibilityError",
    # search cycle model
    "SearchCycle",
    # eligibility threshold and novelty tracker
    "HypothesisCandidate",
    "EligibilityRule",
    "V01_ELIGIBILITY_RULE",
    "EligibilityDecision",
    "NoveltyCount",
    "track_new_eligible_hypotheses",
    # saturation evaluator
    "SaturationVerdict",
    "SaturationRecord",
    "SaturationRule",
    "SaturationRuleDecision",
    "SATURATION_RULES",
    "consecutive_zero_novelty_run",
    "SaturationAssessment",
    "evaluate_saturation",
]

#: Version of the saturation rule table. Bumped whenever a rule changes;
#: recorded in every assessment so old saturation decisions stay
#: interpretable (auditability).
SATURATION_RULESET_VERSION: str = "1.0"

#: Version of the eligibility threshold vocabulary (the version field of
#: every :class:`EligibilityRule`; recorded in every eligibility decision).
ELIGIBILITY_RULESET_VERSION: str = "1.0"

#: Frozen v0.1 Recovery-eligibility threshold: minimum Reliability axis
#: (08-STRICT-RECOVERY-CLOSURE.md SS4: Reliability >= 3).
V01_ELIGIBILITY_MIN_RELIABILITY: int = 3

#: Frozen v0.1 Recovery-eligibility threshold: minimum Directness axis
#: (08-STRICT-RECOVERY-CLOSURE.md SS4: Directness >= 2).
V01_ELIGIBILITY_MIN_DIRECTNESS: int = 2

#: Frozen v0.1 Recovery-eligibility constant: actionable = true
#: (08-STRICT-RECOVERY-CLOSURE.md SS4).
V01_ELIGIBILITY_ACTIONABLE: bool = True

#: The 0-4 rubric axis range (06-EVIDENCE-SYSTEM.md SS2), used to validate
#: threshold parameters of eligibility rule factories.
_AXIS_MIN = 0
_AXIS_MAX = 4

#: Canonical field names of a :class:`SearchCycle` (its to_dict keys).
_CYCLE_FIELDS: tuple[str, ...] = (
    "cycle_index",
    "search_family",
    "completed",
    "new_eligible_hypotheses",
    "expansion",
)

#: Canonical field names of a :class:`SaturationRecord` (its to_dict keys).
_RECORD_FIELDS: tuple[str, ...] = (
    "cycles",
    "required_search_families_completed",
    "required_zero_novelty_cycles",
)


# ---------------------------------------------------------------------------
# Errors -- stable messages, ValueError subclasses (rule-engine paradigm)
# ---------------------------------------------------------------------------


class SaturationError(ValueError):
    """Base error for the search-cycle and saturation workflow.

    Raised when a cycle record, a saturation record or a tracker input
    violates the frozen vocabulary or shape. Stable messages: every message
    names the offending value and the reason, so callers and tests can rely
    on them.
    """


class SaturationRecordError(SaturationError):
    """Raised when a :class:`SearchCycle` or :class:`SaturationRecord` is
    malformed.

    Covers empty search families, negative/out-of-order cycle indexes,
    negative novelty counts, non-sequence/non-record histories, an
    out-of-range ``required_zero_novelty_cycles`` (the closure-contract
    schema minimum is 1), and non-tri-state family-coverage values.
    """


class SaturationEligibilityError(SaturationError):
    """Raised when the novelty tracker's inputs violate the eligible-hypothesis
    vocabulary.

    Covers candidates without a non-empty hypothesis reference and empty
    members of the known-eligible set (hypotheses are opaque non-empty
    string refs).
    """


# ---------------------------------------------------------------------------
# Search cycle model (09-RESEARCH-SUBSYSTEM.md SS7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchCycle:
    """One recorded search cycle (09-RESEARCH-SUBSYSTEM.md SS7).

    ``cycle_index`` is the cycle's position in the history (a non-negative
    int; a :class:`SaturationRecord` requires strictly increasing indexes).
    ``search_family`` names the search family covered (09 SS6 lists the
    required families; the recorded value is an opaque non-empty string,
    like every ref in the research subsystem). ``completed`` marks whether
    the cycle completed. ``new_eligible_hypotheses`` is the number of *new*
    eligible Recovery hypotheses the cycle produced -- the novelty count
    (09 SS7: *the number of new eligible Recovery hypotheses*), produced by
    :func:`track_new_eligible_hypotheses` at recording time and stored on
    the record; the evaluator reads the recorded count and never re-derives
    it, so a stored history reproduces a decision exactly (auditability).
    ``expansion`` marks an *expansion* search cycle (the spec's operational
    rule is stated over expansion cycles); a cycle is an expansion cycle
    unless explicitly recorded otherwise.

    Frozen and hashable: "same stored cycle -> same run contribution" is
    directly testable and the exact record is preserved in every
    assessment.

    Raises:
        SaturationRecordError: ``cycle_index`` is not a non-negative int,
            ``search_family`` is not a non-empty string, ``completed`` /
            ``expansion`` are not bools, or ``new_eligible_hypotheses`` is
            not a non-negative int.
    """

    cycle_index: int
    search_family: str
    completed: bool
    new_eligible_hypotheses: int
    expansion: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.cycle_index, bool) or not isinstance(
            self.cycle_index, int
        ):
            raise SaturationRecordError(
                "SearchCycle.cycle_index must be an int, got"
                f" {type(self.cycle_index).__name__}"
            )
        if self.cycle_index < 0:
            raise SaturationRecordError(
                "SearchCycle.cycle_index must be >= 0, got"
                f" {self.cycle_index}"
            )
        if not isinstance(self.search_family, str) or not self.search_family:
            raise SaturationRecordError(
                "SearchCycle.search_family must be a non-empty string, got"
                f" {self.search_family!r}"
            )
        if not isinstance(self.completed, bool):
            raise SaturationRecordError(
                "SearchCycle.completed must be a bool, got"
                f" {type(self.completed).__name__}"
            )
        if isinstance(self.new_eligible_hypotheses, bool) or not isinstance(
            self.new_eligible_hypotheses, int
        ):
            raise SaturationRecordError(
                "SearchCycle.new_eligible_hypotheses must be an int, got"
                f" {type(self.new_eligible_hypotheses).__name__}"
            )
        if self.new_eligible_hypotheses < 0:
            raise SaturationRecordError(
                "SearchCycle.new_eligible_hypotheses must be >= 0, got"
                f" {self.new_eligible_hypotheses}"
            )
        if not isinstance(self.expansion, bool):
            raise SaturationRecordError(
                "SearchCycle.expansion must be a bool, got"
                f" {type(self.expansion).__name__}"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SearchCycle:
        """Build a cycle record from a plain mapping (canonical keys).

        Strict by design (deterministic and auditable): every canonical key
        must be present, unknown keys are rejected so a typo cannot
        silently change a stored cycle, and values are validated exactly as
        the constructor validates them. ``from_dict(to_dict(cycle))``
        round-trips exactly, so a stored cycle survives serialization.

        Raises:
            TypeError: ``data`` is not a ``Mapping``.
            SaturationRecordError: missing/unknown keys or an invalid value.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "SearchCycle.from_dict expects a Mapping, got"
                f" {type(data).__name__}"
            )
        unknown = set(data) - set(_CYCLE_FIELDS)
        if unknown:
            raise SaturationRecordError(
                "unknown search cycle key(s):"
                f" {', '.join(sorted(unknown))}; expected"
                f" {', '.join(_CYCLE_FIELDS)}"
            )
        missing = [key for key in _CYCLE_FIELDS if key not in data]
        if missing:
            raise SaturationRecordError(
                "search cycle missing field(s):"
                f" {', '.join(sorted(missing))}"
            )
        return cls(
            cycle_index=data["cycle_index"],
            search_family=data["search_family"],
            completed=data["completed"],
            new_eligible_hypotheses=data["new_eligible_hypotheses"],
            expansion=data["expansion"],
        )  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical mapping (the serialization form a store
        persists). ``from_dict(to_dict(cycle))`` round-trips exactly."""
        return {
            "cycle_index": self.cycle_index,
            "search_family": self.search_family,
            "completed": self.completed,
            "new_eligible_hypotheses": self.new_eligible_hypotheses,
            "expansion": self.expansion,
        }


# ---------------------------------------------------------------------------
# Eligible-hypothesis novelty tracker (AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HypothesisCandidate:
    """One candidate Recovery hypothesis produced by a search cycle.

    ``hypothesis_ref`` is the opaque reference of the candidate (the frozen
    schemas define no ``Hypothesis`` object; hypotheses are referenced by
    opaque non-empty strings, like every ref in the research subsystem).
    ``assessment`` is the (source, claim) evidence assessment of the
    candidate -- the frozen ``core.models.EvidenceAssessment`` of
    ``schemas/evidence.schema.yaml``, exactly what the sibling evidence
    data layer (``research/evidence.py``) stores. The eligibility threshold
    evaluates the candidate's assessment: the Reliability/Directness rules
    read precisely its ``reliability`` and ``directness`` axes (AC-03).

    Raises:
        SaturationEligibilityError: ``hypothesis_ref`` is not a non-empty
            string, or ``assessment`` is not an ``EvidenceAssessment``.
    """

    hypothesis_ref: str
    assessment: EvidenceAssessment

    def __post_init__(self) -> None:
        if not isinstance(self.hypothesis_ref, str) or not self.hypothesis_ref:
            raise SaturationEligibilityError(
                "HypothesisCandidate.hypothesis_ref must be a non-empty"
                f" string, got {self.hypothesis_ref!r}"
            )
        if not isinstance(self.assessment, EvidenceAssessment):
            raise SaturationEligibilityError(
                "HypothesisCandidate.assessment must be an"
                f" EvidenceAssessment, got {type(self.assessment).__name__}"
            )


@dataclass(frozen=True)
class EligibilityRule:
    """A versioned Recovery-eligibility threshold (AC-03).

    ``predicate`` decides, for one :class:`HypothesisCandidate`, whether it
    is eligible. The frozen default is :data:`V01_ELIGIBILITY_RULE` -- the
    v0.1 predicate (Reliability >= 3, Directness >= 2, actionable = true,
    08-STRICT-RECOVERY-CLOSURE.md SS4) composed from the core hard-gate
    rules of ``core/rules/evidence.py`` (``reliability_gate_passes`` /
    ``directness_gate_passes``), which is exactly the gate
    ``recovery_hypothesis_eligible`` implements (06-EVIDENCE-SYSTEM.md
    SS4). *Configurable and frozen* (08 SS4) is realized as a named,
    versioned, frozen rule object recorded in every eligibility decision --
    never an arbitrary runtime knob at evaluation time.
    """

    rule_id: str
    version: str
    description: str
    predicate: Callable[[HypothesisCandidate], bool]

    @classmethod
    def from_reliability_registry(
        cls,
        registry: ReliabilityChecklistRegistry,
        *,
        minimum_reliability: int = 3,
    ) -> EligibilityRule:
        """Build an eligibility rule whose Reliability axis is re-derived from
        the stored checklist record -- the research-reliability-layer hook
        of AC-03 (``research/reliability.py``, DEV-M5-G04).

        Instead of trusting the assessment's stored ``reliability`` axis,
        the rule re-derives the Reliability score from the checklist record
        stored under ``assessment.reliability_checklist_ref`` via
        ``research.reliability.evaluate_reliability`` (the versioned R-REL
        table) and requires ``minimum_reliability`` (v0.1 default 3); the
        Directness gate remains the core rule at the frozen v0.1 threshold
        (>= 2, ``V01_ELIGIBILITY_MIN_DIRECTNESS``) and actionable stays the
        frozen v0.1 constant (``V01_ELIGIBILITY_ACTIONABLE``). A candidate
        whose checklist reference has no stored record is never eligible:
        reliability cannot be established without the stored checklist
        record (research/reliability.py AC-01).

        Raises:
            TypeError: ``registry`` is not a ``ReliabilityChecklistRegistry``,
                or ``minimum_reliability`` is not an int.
            SaturationEligibilityError: ``minimum_reliability`` is outside
                the 0-4 rubric range.
        """
        if not isinstance(registry, ReliabilityChecklistRegistry):
            raise TypeError(
                "EligibilityRule.from_reliability_registry expects a"
                " ReliabilityChecklistRegistry, got"
                f" {type(registry).__name__}"
            )
        if isinstance(minimum_reliability, bool) or not isinstance(
            minimum_reliability, int
        ):
            raise TypeError(
                "EligibilityRule.from_reliability_registry expects an int"
                f" minimum_reliability, got {type(minimum_reliability).__name__}"
            )
        if not _AXIS_MIN <= minimum_reliability <= _AXIS_MAX:
            raise SaturationEligibilityError(
                "minimum_reliability must be within the 0-4 rubric range,"
                f" got {minimum_reliability}"
            )

        def predicate(candidate: HypothesisCandidate) -> bool:
            stored = registry.get(
                candidate.assessment.reliability_checklist_ref
            )
            if stored is None:
                return False
            return (
                evaluate_reliability(stored).score >= minimum_reliability
                and directness_gate_passes(
                    candidate.assessment,
                    minimum=V01_ELIGIBILITY_MIN_DIRECTNESS,
                )
                and V01_ELIGIBILITY_ACTIONABLE
            )

        return cls(
            rule_id="R-ELIG-C1",
            version=ELIGIBILITY_RULESET_VERSION,
            description=(
                "Recovery eligibility with Reliability re-derived from the"
                " stored checklist record (research/reliability.py,"
                f" R-REL table, minimum {minimum_reliability}) and the core"
                " Directness gate at the frozen v0.1 threshold (>= 2),"
                " actionable = true"
            ),
            predicate=predicate,
        )


#: The frozen default eligibility threshold (08-STRICT-RECOVERY-CLOSURE.md
#: SS4): Reliability >= 3, Directness >= 2, actionable = true, composed from
#: the core hard-gate rules of ``core/rules/evidence.py`` at the frozen v0.1
#: thresholds (AC-03). Behaviorally identical to the canonical v0.1 gate
#: ``recovery_hypothesis_eligible`` of ``core/rules/evidence.py``; the test
#: suite proves the equivalence on the full Reliability x Directness x
#: actionable grid.
V01_ELIGIBILITY_RULE: EligibilityRule = EligibilityRule(
    rule_id="R-ELIG-V01",
    version=ELIGIBILITY_RULESET_VERSION,
    description=(
        "frozen v0.1 Recovery eligibility (08-STRICT-RECOVERY-CLOSURE.md"
        " SS4): Reliability >= 3 and Directness >= 2 and actionable = true,"
        " evaluated through the core hard-gate rules of"
        " core/rules/evidence.py"
    ),
    predicate=lambda c: (
        reliability_gate_passes(
            c.assessment, minimum=V01_ELIGIBILITY_MIN_RELIABILITY
        )
        and directness_gate_passes(
            c.assessment, minimum=V01_ELIGIBILITY_MIN_DIRECTNESS
        )
        and V01_ELIGIBILITY_ACTIONABLE
    ),
)


@dataclass(frozen=True)
class EligibilityDecision:
    """One candidate's eligibility evaluation (AC-03 auditability).

    Records the candidate reference, whether the candidate passed the
    threshold (``eligible``), whether it is new relative to the
    already-known eligible set (``novel``), and the eligibility rule that
    decided it (``rule_id`` / ``rule_version``) -- the exact rule object is
    preserved in the decision, so the count is auditable and
    reproducible.
    """

    hypothesis_ref: str
    eligible: bool
    novel: bool
    rule_id: str
    rule_version: str


@dataclass(frozen=True)
class NoveltyCount:
    """Result of tracking one search cycle's candidate hypotheses.

    ``new_eligible_hypotheses`` lists, in candidate order, the candidates
    that are both eligible and not already in the known eligible set -- the
    cycle's novelty (09-RESEARCH-SUBSYSTEM.md SS7: *the number of new
    eligible Recovery hypotheses*), recorded by the researcher as
    ``SearchCycle.new_eligible_hypotheses`` (:attr:`count`). Every eligible
    candidate (novel or already known) is listed in ``eligible_hypotheses``,
    and ``decisions`` records one :class:`EligibilityDecision` per candidate
    in candidate order.
    """

    new_eligible_hypotheses: tuple[str, ...]
    eligible_hypotheses: tuple[str, ...]
    decisions: tuple[EligibilityDecision, ...]

    @property
    def count(self) -> int:
        """The number of new eligible hypotheses (the cycle's novelty
        count)."""
        return len(self.new_eligible_hypotheses)


def track_new_eligible_hypotheses(
    candidates: Sequence[HypothesisCandidate],
    known_eligible_hypotheses: Sequence[str],
    eligibility: EligibilityRule | None = None,
) -> NoveltyCount:
    """Count, for one search cycle, the candidate hypotheses that are NEW
    (novel) and eligible.

    Eligibility is decided by ``eligibility`` -- the threshold object,
    defaulting to the frozen v0.1 :data:`V01_ELIGIBILITY_RULE` (AC-03: the
    threshold references the core Reliability/Directness rules; any other
    threshold is a versioned :class:`EligibilityRule` object recorded in
    every decision, never an evaluation-time knob). ``novel`` means the
    candidate reference is not in the already-known eligible set
    ``known_eligible_hypotheses`` (the eligible hypotheses established by
    earlier cycles). Pure and deterministic: identical inputs yield
    identical results, candidate order is preserved in every output tuple,
    and no state is mutated.

    Raises:
        TypeError: ``candidates`` / ``known_eligible_hypotheses`` is not a
            sequence (a ``str``/``bytes`` is rejected explicitly), an
            element of ``candidates`` is not a ``HypothesisCandidate``, a
            member of ``known_eligible_hypotheses`` is not a ``str``, or
            ``eligibility`` is neither ``None`` nor an ``EligibilityRule``.
        SaturationEligibilityError: a member of
            ``known_eligible_hypotheses`` is an empty string.
    """
    if isinstance(candidates, (str, bytes)) or not isinstance(
        candidates, Sequence
    ):
        raise TypeError(
            "track_new_eligible_hypotheses expects a sequence of"
            f" HypothesisCandidate, got {type(candidates).__name__}"
        )
    if isinstance(known_eligible_hypotheses, (str, bytes)) or not isinstance(
        known_eligible_hypotheses, Sequence
    ):
        raise TypeError(
            "track_new_eligible_hypotheses expects a sequence of str"
            " known_eligible_hypotheses, got"
            f" {type(known_eligible_hypotheses).__name__}"
        )
    for candidate in candidates:
        if not isinstance(candidate, HypothesisCandidate):
            raise TypeError(
                "track_new_eligible_hypotheses expects HypothesisCandidate"
                f" elements, got {type(candidate).__name__}"
            )
    known: list[str] = []
    for ref in known_eligible_hypotheses:
        if not isinstance(ref, str):
            raise TypeError(
                "track_new_eligible_hypotheses expects str"
                f" known_eligible_hypotheses members, got {type(ref).__name__}"
            )
        if not ref:
            raise SaturationEligibilityError(
                "track_new_eligible_hypotheses: known_eligible_hypotheses"
                " members must be non-empty strings (hypothesis references)"
            )
        known.append(ref)
    if eligibility is not None and not isinstance(eligibility, EligibilityRule):
        raise TypeError(
            "track_new_eligible_hypotheses expects an EligibilityRule or"
            f" None eligibility, got {type(eligibility).__name__}"
        )
    used = eligibility if eligibility is not None else V01_ELIGIBILITY_RULE
    known_set = frozenset(known)
    new: list[str] = []
    eligible: list[str] = []
    decisions: list[EligibilityDecision] = []
    for candidate in candidates:
        is_eligible = used.predicate(candidate)
        is_novel = candidate.hypothesis_ref not in known_set
        decisions.append(
            EligibilityDecision(
                hypothesis_ref=candidate.hypothesis_ref,
                eligible=is_eligible,
                novel=is_novel,
                rule_id=used.rule_id,
                rule_version=used.version,
            )
        )
        if is_eligible:
            eligible.append(candidate.hypothesis_ref)
            if is_novel:
                new.append(candidate.hypothesis_ref)
    return NoveltyCount(
        new_eligible_hypotheses=tuple(new),
        eligible_hypotheses=tuple(eligible),
        decisions=tuple(decisions),
    )


# ---------------------------------------------------------------------------
# Saturation evaluator (AC-01/AC-02): the versioned operational rule table
# ---------------------------------------------------------------------------


class SaturationVerdict(StrEnum):
    """Verdict of the v0.1 operational saturation rule table.

    ``SATURATED`` -- the consecutive zero-novelty cycle count meets the
    required rule (v0.1 default: two). ``NOT_SATURATED`` -- the count is
    evaluated and below the rule: one zero-novelty cycle is insufficient
    (AC-01). ``UNRESOLVED`` -- the count has not been established (the most
    recent recorded cycle is not a completed expansion cycle). The verdict
    is about the *operational* rule; the family-completion requirement is a
    separate input composed by the closure layer (see the module
    docstring).
    """

    SATURATED = "SATURATED"
    NOT_SATURATED = "NOT_SATURATED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class SaturationRecord:
    """One saturation evaluation input: the recorded cycle history plus the
    family-coverage state.

    ``cycles`` holds the recorded :class:`SearchCycle` records in recording
    (evaluation) order, with strictly increasing ``cycle_index`` values --
    a cycle history is a sequence, and out-of-order indexes would make the
    run computation order-dependent, so they are rejected up front.
    ``required_search_families_completed`` is the recorded family-coverage
    state, tri-state like every axis of the closure layer (True covered /
    False confirmed incomplete / None unknown): an *input* recorded in
    every assessment; per the normative reading the closure layer composes
    it with the operational-rule verdict (see the module docstring).
    ``required_zero_novelty_cycles`` is the frozen, configurable saturation
    rule (``schemas/closure-contract.schema.yaml``: integer, minimum 1,
    default 2 -- mirrored by ``core.models.ClosureLiterature``); *frozen*
    here means it is a recorded field of the record and of every
    assessment, not a runtime-adjustable knob.

    Raises:
        SaturationRecordError: ``cycles`` is not a sequence of
            ``SearchCycle`` records, their ``cycle_index`` values are not
            strictly increasing, ``required_search_families_completed`` is
            not a bool or None, or ``required_zero_novelty_cycles`` is not
            an int >= 1.
    """

    cycles: tuple[SearchCycle, ...] = ()
    required_search_families_completed: bool | None = None
    required_zero_novelty_cycles: int = 2

    def __post_init__(self) -> None:
        raw_cycles = self.cycles
        if isinstance(raw_cycles, (str, bytes)) or not isinstance(
            raw_cycles, Sequence
        ):
            raise SaturationRecordError(
                "SaturationRecord.cycles must be a sequence of SearchCycle"
                f" records, got {type(raw_cycles).__name__}"
            )
        cycles = list(raw_cycles)
        for cycle in cycles:
            if not isinstance(cycle, SearchCycle):
                raise SaturationRecordError(
                    "SaturationRecord.cycles must contain SearchCycle"
                    f" records, got {type(cycle).__name__}"
                )
        previous: int | None = None
        for cycle in cycles:
            if previous is not None and cycle.cycle_index <= previous:
                raise SaturationRecordError(
                    "SaturationRecord.cycles must be in strictly increasing"
                    " cycle_index order: cycle"
                    f" {cycle.cycle_index!r} follows {previous!r}"
                )
            previous = cycle.cycle_index
        families = self.required_search_families_completed
        if families is not None and not isinstance(families, bool):
            raise SaturationRecordError(
                "SaturationRecord.required_search_families_completed must be"
                f" a bool or None, got {type(families).__name__}"
            )
        required = self.required_zero_novelty_cycles
        if isinstance(required, bool) or not isinstance(required, int):
            raise SaturationRecordError(
                "SaturationRecord.required_zero_novelty_cycles must be an"
                f" int, got {type(required).__name__}"
            )
        if required < 1:
            raise SaturationRecordError(
                "SaturationRecord.required_zero_novelty_cycles must be >= 1"
                f" per the closure-contract schema, got {required}"
            )
        object.__setattr__(self, "cycles", tuple(cycles))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SaturationRecord:
        """Build a saturation record from a plain mapping (canonical keys).

        Strict by design: every canonical key must be present, unknown keys
        are rejected, and values are validated exactly as the constructor
        validates them. ``from_dict(to_dict(record))`` round-trips exactly,
        so a stored cycle history survives serialization and reproduces the
        same decision.

        Raises:
            TypeError: ``data`` is not a ``Mapping``.
            SaturationRecordError: missing/unknown keys or an invalid value.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "SaturationRecord.from_dict expects a Mapping, got"
                f" {type(data).__name__}"
            )
        unknown = set(data) - set(_RECORD_FIELDS)
        if unknown:
            raise SaturationRecordError(
                "unknown saturation record key(s):"
                f" {', '.join(sorted(unknown))}; expected"
                f" {', '.join(_RECORD_FIELDS)}"
            )
        missing = [key for key in _RECORD_FIELDS if key not in data]
        if missing:
            raise SaturationRecordError(
                "saturation record missing field(s):"
                f" {', '.join(sorted(missing))}"
            )
        cycles_data = data["cycles"]
        if isinstance(cycles_data, (str, bytes)) or not isinstance(
            cycles_data, Sequence
        ):
            raise SaturationRecordError(
                "saturation record 'cycles' must be a sequence of search"
                f" cycle mappings, got {type(cycles_data).__name__}"
            )
        cycles = tuple(SearchCycle.from_dict(cycle) for cycle in cycles_data)
        return cls(
            cycles=cycles,
            required_search_families_completed=data[
                "required_search_families_completed"
            ],
            required_zero_novelty_cycles=data["required_zero_novelty_cycles"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical mapping (the serialization form a store
        persists). ``from_dict(to_dict(record))`` round-trips exactly."""
        return {
            "cycles": [cycle.to_dict() for cycle in self.cycles],
            "required_search_families_completed": (
                self.required_search_families_completed
            ),
            "required_zero_novelty_cycles": self.required_zero_novelty_cycles,
        }


def consecutive_zero_novelty_run(
    cycles: Sequence[SearchCycle],
) -> int | None:
    """The consecutive completed-expansion cycles with zero new eligible
    hypotheses at the end of the recorded history.

    Normative reading (see the module docstring): a completed expansion
    cycle with zero new eligible hypotheses extends the run; a completed
    expansion cycle with novel hypotheses resets it to zero; any other
    cycle (incomplete, or a non-expansion cycle) breaks it. The run is a
    well-defined count exactly when the most recent recorded cycle is a
    completed expansion cycle; otherwise it is ``None`` (the count has not
    been established yet). This is the value recorded in the frozen
    ``ClosureLiterature.consecutive_zero_novelty_cycles`` field.

    Raises:
        TypeError: ``cycles`` is not a sequence (a ``str``/``bytes`` is
            rejected explicitly), or an element is not a ``SearchCycle``.
    """
    if isinstance(cycles, (str, bytes)) or not isinstance(cycles, Sequence):
        raise TypeError(
            "consecutive_zero_novelty_run expects a sequence of SearchCycle,"
            f" got {type(cycles).__name__}"
        )
    run: int | None = None
    for cycle in cycles:
        if not isinstance(cycle, SearchCycle):
            raise TypeError(
                "consecutive_zero_novelty_run expects SearchCycle elements,"
                f" got {type(cycle).__name__}"
            )
        if cycle.expansion and cycle.completed:
            if cycle.new_eligible_hypotheses == 0:
                run = (run if run is not None else 0) + 1
            else:
                run = 0
        else:
            run = None
    return run


@dataclass(frozen=True)
class SaturationRule:
    """One entry of the ordered operational-saturation rule table."""

    rule_id: str
    description: str
    verdict: SaturationVerdict
    predicate: Callable[[SaturationRecord], bool]


@dataclass(frozen=True)
class SaturationRuleDecision:
    """Record of one rule evaluation for a given history (auditability)."""

    rule_id: str
    description: str
    verdict: SaturationVerdict
    matched: bool


def _run_meets_rule(record: SaturationRecord) -> bool:
    """True when the consecutive zero-novelty run meets the required rule."""
    run = consecutive_zero_novelty_run(record.cycles)
    return run is not None and run >= record.required_zero_novelty_cycles


def _run_below_rule(record: SaturationRecord) -> bool:
    """True when the consecutive zero-novelty run is evaluated and below
    the required rule."""
    run = consecutive_zero_novelty_run(record.cycles)
    return run is not None and run < record.required_zero_novelty_cycles


#: The ordered operational-saturation rule table. First match wins; order is
#: normative (see the module docstring). ``R-SAT-D1`` is the total default
#: rule, so the table is total: every recorded history gets exactly one
#: verdict. The v0.1 default operational rule is exactly two consecutive
#: completed expansion cycles with zero new eligible hypotheses
#: (08-STRICT-RECOVERY-CLOSURE.md SS4); the required count is the frozen,
#: configurable field of the record (schema default 2).
SATURATION_RULES: tuple[SaturationRule, ...] = (
    SaturationRule(
        rule_id="R-SAT-S1",
        description=(
            "the consecutive completed-expansion cycles with zero new"
            " eligible hypotheses meet the required zero-novelty-cycle rule"
            " (v0.1 default: two consecutive zero-novelty cycles)"
        ),
        verdict=SaturationVerdict.SATURATED,
        predicate=_run_meets_rule,
    ),
    SaturationRule(
        rule_id="R-SAT-S2",
        description=(
            "the consecutive zero-novelty-cycle count is evaluated and"
            " below the required rule: saturation is not yet satisfied"
            " (one zero-novelty cycle is insufficient)"
        ),
        verdict=SaturationVerdict.NOT_SATURATED,
        predicate=_run_below_rule,
    ),
    SaturationRule(
        rule_id="R-SAT-D1",
        description=(
            "the most recent recorded cycle is not a completed expansion"
            " cycle: the consecutive zero-novelty-cycle count has not been"
            " established (total default)"
        ),
        verdict=SaturationVerdict.UNRESOLVED,
        predicate=lambda record: True,
    ),
)


@dataclass(frozen=True)
class SaturationAssessment:
    """Full, auditable result of one saturation evaluation.

    ``record`` is the exact input history; ``verdict`` is the
    operational-rule verdict (:data:`SaturationVerdict`);
    ``consecutive_zero_novelty_cycles`` is the derived run -- the frozen
    ``ClosureLiterature.consecutive_zero_novelty_cycles`` field value
    (``None`` when the count has not been established);
    ``required_search_families_completed`` and ``required_zero_novelty_cycles``
    mirror the record's frozen fields; ``decisions`` records every rule
    evaluation of the table; ``matched_rule_id`` names the deciding rule
    (never None: the trailing total default always matches);
    ``ruleset_version`` records the rule table version so old decisions
    stay interpretable.
    """

    ruleset_version: str
    record: SaturationRecord
    verdict: SaturationVerdict
    consecutive_zero_novelty_cycles: int | None
    required_search_families_completed: bool | None
    required_zero_novelty_cycles: int
    decisions: tuple[SaturationRuleDecision, ...]
    matched_rule_id: str

    @property
    def saturated(self) -> bool:
        """True exactly when the operational-rule verdict is SATURATED."""
        return self.verdict is SaturationVerdict.SATURATED

    def to_closure_literature(self) -> ClosureLiterature:
        """The frozen ``ClosureLiterature`` record this assessment fills.

        The closure layer composes the family-completion requirement with
        this count (``core/rules/closure.py`` ``RESEARCH_SATURATION_RULES``:
        research saturation requires the families completed *and* the
        consecutive count meeting the required rule); this method produces
        exactly the three fields that composition reads.
        """
        return ClosureLiterature(
            required_search_families_completed=(
                self.required_search_families_completed
            ),
            consecutive_zero_novelty_cycles=(
                self.consecutive_zero_novelty_cycles
            ),
            required_zero_novelty_cycles=self.required_zero_novelty_cycles,
        )


def evaluate_saturation(record: SaturationRecord) -> SaturationAssessment:
    """Evaluate the v0.1 operational saturation rule over the recorded cycle
    history.

    Pure and deterministic: the verdict is a pure function of the recorded
    history (AC-02 determinism) -- the novelty counts are read from the
    stored :class:`SearchCycle` records and never re-derived, so a stored
    history reproduces exactly the same assessment. The returned
    :class:`SaturationAssessment` carries the derived run
    (``consecutive_zero_novelty_cycles``), the family-coverage input, every
    rule evaluation and the deciding rule.

    Raises:
        TypeError: ``record`` is not a ``SaturationRecord``.
    """
    if not isinstance(record, SaturationRecord):
        raise TypeError(
            "evaluate_saturation expects a SaturationRecord, got"
            f" {type(record).__name__}"
        )
    decisions: list[SaturationRuleDecision] = []
    matched_rule_id: str | None = None
    matched_verdict = SaturationVerdict.UNRESOLVED  # unreachable default
    for rule in SATURATION_RULES:
        matched = rule.predicate(record)
        decisions.append(
            SaturationRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                verdict=rule.verdict,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_verdict = rule.verdict
    # R-SAT-D1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return SaturationAssessment(
        ruleset_version=SATURATION_RULESET_VERSION,
        record=record,
        verdict=matched_verdict,
        consecutive_zero_novelty_cycles=consecutive_zero_novelty_run(
            record.cycles
        ),
        required_search_families_completed=(
            record.required_search_families_completed
        ),
        required_zero_novelty_cycles=record.required_zero_novelty_cycles,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )
