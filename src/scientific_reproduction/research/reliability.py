"""Reliability checklist workflow: structured checklist persistence and
rule-derived score reference (DEV-M5-G04).

Implements the **reliability checklist model** and **checklist-to-score
integration** deliverables: a persisted, frozen checklist record over the
frozen reliability dimensions, and a versioned, ordered rule table that maps
a stored checklist record to a 0-4 Reliability score with a full auditable
rule trace. The frozen spec grounds this module:

* ``06-EVIDENCE-SYSTEM.md`` SS2 ("Reliability (R)"): *Reliability must be
  produced from a checklist and rule engine, not an LLM gut score*, the
  required checklist dimensions (the nine listed there, "at least"), and *a
  versioned rule maps checklist answers to 0-4 Reliability*.
* ``schemas/evidence.schema.yaml``: the assessment requires
  ``reliability`` (integer 0-4) and ``reliability_checklist_ref`` (a
  non-empty string -- the checklist record reference).
* ``schemas/research-request.schema.yaml``: ``minimum_reliability`` is an
  integer 0-4 or null, so the derived score is schema-compatible by
  construction.
* ``09-RESEARCH-SUBSYSTEM.md`` SS5 ("Evidence extraction"): research must
  produce *structured source and evidence records ... and A/R/D
  assessments*; reliability is written only after the checklist result.
* ``agent-contracts/RESEARCH.md`` ("Must not"): the research role must not
  *assign Reliability from intuition instead of checklist/rule mapping*.
* ``CLAUDE-CODE-HANDOFF.md`` M5 acceptance: *Reliability cannot be written
  without checklist result reference* (AC-01).
* ``research/evidence.py`` (DEV-M5-G03, the sibling data layer): the
  ``EvidenceAssessment.reliability_checklist_ref`` non-empty-string rule is
  the handoff this module's AC-01 rule layer builds on -- the score and its
  checklist reference produced here are exactly what that data layer stores.

The six AC-02 factors vs the nine spec dimensions (normative reading)
----------------------------------------------------------------------
AC-02 names six factors the checklist must record -- raw-data, replication,
uncertainty, method completeness, validation and consistency -- while
06-EVIDENCE-SYSTEM.md SS2 lists nine dimensions ("at least"). Reading: the
six AC-02 factors are the model's **core** and are recorded as named frozen
fields (``raw_data_available``, ``independent_replication_performed``,
``uncertainty_reported``, ``method_complete``,
``independent_external_validation``, ``data_internally_consistent``); the
three remaining spec dimensions (``conclusion_supported_by_data``,
``material_identity_controlled`` and the negative signal
``known_retraction_correction_defect``) are **also recorded** as frozen
checklist fields, because the spec lists them as required dimensions
("at least") and the M2 evidence rules already score all nine. Nothing from
the frozen spec is dropped: the checklist record covers all nine
dimensions, the six AC-02 factors are first-class, and ``CORE_FACTOR_KEYS``
/ ``ADDITIONAL_DIMENSION_KEYS`` expose the split for tests and callers.

The answer vocabulary (normative reading)
-----------------------------------------
Checklist answers are deterministic enumerated values
(:class:`ChecklistAnswer`: ``YES`` / ``NO`` / ``UNKNOWN``) -- no free-form
text, because free text would break AC-03 (a rule result reproducible from
the stored checklist). ``UNKNOWN`` expresses "the researcher could not
establish this factor"; it is distinct from ``NO`` ("the factor is absent")
in the record, and the score rule never credits an ``UNKNOWN`` as
satisfied (conservative: the rule cannot reward a factor that was not
established). The vocabulary is versioned (``CHECKLIST_VOCABULARY_VERSION``
and the record's ``vocabulary_version`` ClassVar).

The versioned score rule (AC-03)
--------------------------------
:func:`evaluate_reliability` maps one stored :class:`ReliabilityChecklistRecord`
to a 0-4 score through the versioned, ordered rule table
:data:`RELIABILITY_RULES` (``RELIABILITY_RULESET_VERSION``), in the frozen
rule-engine paradigm of ``core/rules/``, ``research/dedupe.py`` and
``research/requests.py``: first match wins, a total default rule closes the
table, every rule evaluation is recorded in an auditable assessment, and
the rules are pure deterministic predicates (no LLM, no randomness, no
wall-clock; timestamps are not part of this module). The table:

1. ``R-REL-D0``  a known retraction/correction/methodological defect is
   recorded (negative dimension answered YES)            -> score 0
2. ``R-REL-H1``  all eight positive dimensions answered YES -> score 4
3. ``R-REL-H2``  six or seven positive dimensions YES     -> score 3
4. ``R-REL-H3``  four or five positive dimensions YES     -> score 2
5. ``R-REL-H4``  two or three positive dimensions YES     -> score 1
6. ``R-REL-H5``  fewer than two satisfied (total default) -> score 0

The banding reproduces the M2 core rubric of
``core/rules/evidence.py``'s ``reliability_score`` (same eight positive
dimensions, same bands, same disqualifying negative signal), so a checklist
record whose answers are all ``YES``/``NO`` scores identically at both
layers -- the research-subsystem workflow is the auditable,
reference-carrying realization of the core rule hook, and old decisions stay
interpretable because every assessment records ``ruleset_version``.

AC-01: the checklist-reference enforcement point
------------------------------------------------
A reliability score cannot be produced or accepted without a checklist
record and its reference, by construction:

* :func:`evaluate_reliability` **requires** a ``ReliabilityChecklistRecord``
  (``TypeError`` otherwise) and has no score parameter -- there is no API
  path that accepts a directly-assigned reliability value;
* a ``ReliabilityChecklistRecord`` cannot exist without a non-empty
  ``checklist_ref`` (enforced in ``__post_init__``);
* :meth:`ReliabilityChecklistRegistry.evaluate` accepts a *reference* and
  derives the score from the **stored** record only -- a reference with no
  stored record raises, so no reliability value can be accepted against an
  unverifiable reference;
* every produced :class:`ReliabilityAssessment` returns the score and the
  checklist reference together (``assessment.score`` /
  ``assessment.checklist_ref``) -- the exact pair the sibling
  ``research/evidence.py`` data layer stores as
  ``EvidenceAssessment(reliability=..., reliability_checklist_ref=...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, ClassVar, Iterator, Mapping, Sequence

__all__ = [
    # versions
    "CHECKLIST_VOCABULARY_VERSION",
    "RELIABILITY_RULESET_VERSION",
    # answer vocabulary and dimensions
    "ChecklistAnswer",
    "CHECKLIST_DIMENSIONS",
    "CORE_FACTOR_KEYS",
    "ADDITIONAL_DIMENSION_KEYS",
    "POSITIVE_DIMENSION_KEYS",
    "NEGATIVE_DIMENSION_KEY",
    # errors
    "ReliabilityChecklistError",
    "ReliabilityChecklistRecordError",
    "ReliabilityChecklistDuplicateError",
    # checklist record
    "ReliabilityChecklistRecord",
    # score rule table
    "ReliabilityRule",
    "ReliabilityRuleDecision",
    "RELIABILITY_RULES",
    "ReliabilityAssessment",
    "evaluate_reliability",
    # checklist registry
    "ReliabilityChecklistRegistry",
]

#: Version of the checklist answer vocabulary (``YES``/``NO``/``UNKNOWN``).
#: The record class exposes it as ``vocabulary_version`` so stored records
#: stay interpretable; bumped whenever the answer vocabulary changes.
CHECKLIST_VOCABULARY_VERSION: str = "1.0"

#: Version of the checklist-to-score rule table. Bumped whenever a rule
#: changes; recorded in every assessment so old decisions stay
#: interpretable (06-EVIDENCE-SYSTEM.md SS2: "a versioned rule maps
#: checklist answers to 0-4 Reliability").
RELIABILITY_RULESET_VERSION: str = "1.0"


class ChecklistAnswer(StrEnum):
    """Deterministic answer vocabulary for the checklist dimensions.

    ``YES`` the factor holds, ``NO`` the factor is absent, ``UNKNOWN`` the
    researcher could not establish the factor. Members only -- plain
    strings are never accepted as answers, so a stored checklist can never
    carry free text (AC-03 reproducibility).
    """

    YES = "YES"
    NO = "NO"
    UNKNOWN = "UNKNOWN"


#: The nine frozen reliability checklist dimensions as ``(key, question)``
#: pairs (06-EVIDENCE-SYSTEM.md SS2: "Required checklist dimensions should
#: include at least:"). The six AC-02 core factors come first (AC-02's
#: order: raw-data, replication, uncertainty, method completeness,
#: validation, consistency), then the three additional spec dimensions
#: (conclusion supported by data, material/sample identity controlled,
#: known retraction/correction/methodological defect). Keys match the M2
#: core rules' ``RELIABILITY_CHECKLIST_DIMENSIONS`` exactly, so a fully
#: answered YES/NO record scores identically at both layers.
CHECKLIST_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("raw_data_available", "original/raw data available?"),
    (
        "independent_replication_performed",
        "independent replication performed?",
    ),
    ("uncertainty_reported", "uncertainty/variation reported?"),
    ("method_complete", "method sufficiently complete?"),
    (
        "independent_external_validation",
        "independent external validation?",
    ),
    ("data_internally_consistent", "data internally consistent?"),
    ("conclusion_supported_by_data", "conclusion supported by data?"),
    ("material_identity_controlled", "material/sample identity controlled?"),
    (
        "known_retraction_correction_defect",
        "known retraction/correction/methodological defect?",
    ),
)

_DIMENSION_KEYS: tuple[str, ...] = tuple(
    key for key, _ in CHECKLIST_DIMENSIONS
)

#: The six AC-02 core factors (raw-data, replication, uncertainty, method
#: completeness, validation, consistency) -- the acceptance-criterion core
#: of the checklist model, recorded as named frozen fields.
CORE_FACTOR_KEYS: tuple[str, ...] = _DIMENSION_KEYS[:6]

#: The three remaining spec dimensions (06-EVIDENCE-SYSTEM.md SS2):
#: conclusion supported by data, material/sample identity controlled, and
#: the negative signal (known retraction/correction/methodological defect).
ADDITIONAL_DIMENSION_KEYS: tuple[str, ...] = _DIMENSION_KEYS[6:]

#: The positive (non-disqualifying) dimensions; the count of YES answers
#: here drives the score bands.
POSITIVE_DIMENSION_KEYS: tuple[str, ...] = _DIMENSION_KEYS[:-1]

#: The negative dimension: a recorded defect is a disqualifying signal.
NEGATIVE_DIMENSION_KEY: str = _DIMENSION_KEYS[-1]


class ReliabilityChecklistError(ValueError):
    """Base error for the reliability checklist workflow.

    Raised when a checklist record violates the frozen checklist shape or a
    score cannot be produced as specified. Stable messages: every message
    names the offending value and the reason.
    """


class ReliabilityChecklistRecordError(ReliabilityChecklistError):
    """Raised when a checklist record is malformed or missing.

    Covers records without a non-empty reference (AC-01), answers outside
    the ``YES``/``NO``/``UNKNOWN`` vocabulary, and registry evaluations of
    references with no stored record.
    """


class ReliabilityChecklistDuplicateError(ReliabilityChecklistError):
    """Raised when a ``checklist_ref`` is registered twice.

    A checklist reference is the record's identity: a registry holds at most
    one record per reference, so a duplicate registration is a
    data-integrity error -- never a silent overwrite.
    """


@dataclass(frozen=True)
class ReliabilityChecklistRecord:
    """Structured answers to the frozen reliability checklist dimensions.

    The persisted checklist record. ``checklist_ref`` is the record's
    reference -- the value stored as
    ``assessment.reliability_checklist_ref`` by the sibling evidence data
    layer -- and must be a non-empty string: a checklist record without a
    reference cannot back a reliability score (AC-01). The six AC-02 core
    factor answers come first (raw-data, replication, uncertainty, method
    completeness, validation, consistency), then the three additional spec
    dimensions (conclusion supported by data, material/sample identity
    controlled, known retraction/correction/methodological defect).

    All answers are :class:`ChecklistAnswer` members -- deterministic
    enumerated values, never free text (AC-03). Frozen and hashable, so
    "same stored record -> same score" is directly testable and the exact
    record is preserved in every assessment.
    """

    #: Version of the answer vocabulary this record's answers use; recorded
    #: on the class so stored records stay interpretable across versions.
    vocabulary_version: ClassVar[str] = CHECKLIST_VOCABULARY_VERSION

    checklist_ref: str
    # -- the six AC-02 core factors (AC-02) --
    raw_data_available: ChecklistAnswer
    independent_replication_performed: ChecklistAnswer
    uncertainty_reported: ChecklistAnswer
    method_complete: ChecklistAnswer
    independent_external_validation: ChecklistAnswer
    data_internally_consistent: ChecklistAnswer
    # -- the additional spec dimensions (06-EVIDENCE-SYSTEM.md SS2) --
    conclusion_supported_by_data: ChecklistAnswer
    material_identity_controlled: ChecklistAnswer
    known_retraction_correction_defect: ChecklistAnswer

    def __post_init__(self) -> None:
        if (
            not isinstance(self.checklist_ref, str)
            or not self.checklist_ref.strip()
        ):
            raise ReliabilityChecklistRecordError(
                "ReliabilityChecklistRecord.checklist_ref must be a"
                " non-empty string: reliability cannot be produced without a"
                " checklist record reference (AC-01)"
            )
        for key in _DIMENSION_KEYS:
            value = getattr(self, key)
            if not isinstance(value, ChecklistAnswer):
                raise ReliabilityChecklistRecordError(
                    f"reliability checklist dimension {key!r} must be a"
                    " ChecklistAnswer (YES/NO/UNKNOWN), got"
                    f" {value!r}"
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReliabilityChecklistRecord:
        """Build a checklist record from a plain mapping.

        Strict by design (deterministic and auditable): every one of the
        nine frozen dimension keys must be present, answers must be
        :class:`ChecklistAnswer` members or their canonical string values
        (``"YES"``/``"NO"``/``"UNKNOWN"``), ``checklist_ref`` must be a
        non-empty string, and unknown keys are rejected so a typo cannot
        silently change a stored record.

        Raises:
            TypeError: ``data`` is not a ``Mapping``.
            ReliabilityChecklistRecordError: missing dimension keys, unknown
                keys, non-vocabulary answers, or a missing/empty reference.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                "ReliabilityChecklistRecord.from_dict expects a Mapping,"
                f" got {type(data).__name__}"
            )
        unknown = set(data) - {*_DIMENSION_KEYS, "checklist_ref"}
        if unknown:
            raise ReliabilityChecklistRecordError(
                "unknown reliability checklist key(s):"
                f" {', '.join(sorted(unknown))}; expected the frozen"
                f" dimensions {', '.join(_DIMENSION_KEYS)} plus"
                " 'checklist_ref'"
            )
        missing = [key for key in _DIMENSION_KEYS if key not in data]
        if missing:
            raise ReliabilityChecklistRecordError(
                "reliability checklist missing dimension(s):"
                f" {', '.join(sorted(missing))}"
            )
        ref = data.get("checklist_ref")
        if not isinstance(ref, str) or not ref.strip():
            raise ReliabilityChecklistRecordError(
                "reliability checklist record has no non-empty"
                " 'checklist_ref': a checklist record without a reference"
                " cannot back a reliability score (AC-01)"
            )
        answers = {
            key: _coerce_answer(data[key], key) for key in _DIMENSION_KEYS
        }
        return cls(checklist_ref=ref, **answers)  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical mapping (dimension answers plus reference).

        The serialization form a store persists; answers serialize to their
        canonical ``"YES"``/``"NO"``/``"UNKNOWN"`` strings, and
        ``from_dict(to_dict(record))`` round-trips exactly (AC-03).
        """
        return {
            **{key: getattr(self, key).value for key in _DIMENSION_KEYS},
            "checklist_ref": self.checklist_ref,
        }

    def as_mapping(self) -> dict[str, ChecklistAnswer]:
        """Return only the dimension answers (no reference key)."""
        return {key: getattr(self, key) for key in _DIMENSION_KEYS}


def _coerce_answer(value: Any, key: str) -> ChecklistAnswer:
    """Resolve one answer value to a ``ChecklistAnswer`` member.

    Accepts a member or its canonical string value (``"YES"``/``"NO"``/
    ``"UNKNOWN"``); anything else -- including ``bool``, lower-case strings
    and invented vocabulary -- raises with a stable message.
    """
    if isinstance(value, ChecklistAnswer):
        return value
    if isinstance(value, str):
        try:
            return ChecklistAnswer(value)
        except ValueError:
            raise ReliabilityChecklistRecordError(
                f"reliability checklist dimension {key!r} must be one of"
                f" YES/NO/UNKNOWN, got {value!r}"
            ) from None
    raise ReliabilityChecklistRecordError(
        f"reliability checklist dimension {key!r} must be a ChecklistAnswer"
        f" (YES/NO/UNKNOWN), got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# The versioned checklist-to-score rule table (AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReliabilityRule:
    """One entry of the ordered checklist-to-score rule table."""

    rule_id: str
    description: str
    score: int
    predicate: Callable[[ReliabilityChecklistRecord], bool]


@dataclass(frozen=True)
class ReliabilityRuleDecision:
    """Record of one rule evaluation for a given checklist (auditability)."""

    rule_id: str
    description: str
    score: int
    matched: bool


def _satisfied_positive_count(checklist: ReliabilityChecklistRecord) -> int:
    """Number of positive dimensions answered YES.

    ``NO`` and ``UNKNOWN`` never count as satisfied (normative reading: the
    rule cannot reward a factor the researcher could not establish).
    """
    return sum(
        1
        for key in POSITIVE_DIMENSION_KEYS
        if getattr(checklist, key) is ChecklistAnswer.YES
    )


#: The ordered checklist-to-score rule table. First match wins; order is
#: normative (see the module docstring). Predicates are pure functions of
#: the checklist record only; ``R-REL-H5`` is the total default rule.
RELIABILITY_RULES: tuple[ReliabilityRule, ...] = (
    ReliabilityRule(
        rule_id="R-REL-D0",
        description=(
            "known retraction/correction/methodological defect is recorded:"
            " the negative spec dimension disqualifies the source"
        ),
        score=0,
        predicate=lambda c: (
            c.known_retraction_correction_defect is ChecklistAnswer.YES
        ),
    ),
    ReliabilityRule(
        rule_id="R-REL-H1",
        description="all eight positive dimensions are satisfied",
        score=4,
        predicate=lambda c: _satisfied_positive_count(c) == 8,
    ),
    ReliabilityRule(
        rule_id="R-REL-H2",
        description="six or seven positive dimensions are satisfied",
        score=3,
        predicate=lambda c: _satisfied_positive_count(c) >= 6,
    ),
    ReliabilityRule(
        rule_id="R-REL-H3",
        description="four or five positive dimensions are satisfied",
        score=2,
        predicate=lambda c: _satisfied_positive_count(c) >= 4,
    ),
    ReliabilityRule(
        rule_id="R-REL-H4",
        description="two or three positive dimensions are satisfied",
        score=1,
        predicate=lambda c: _satisfied_positive_count(c) >= 2,
    ),
    ReliabilityRule(
        rule_id="R-REL-H5",
        description=(
            "fewer than two positive dimensions are satisfied (total"
            " default: the checklist does not establish reliability)"
        ),
        score=0,
        predicate=lambda c: True,
    ),
)


@dataclass(frozen=True)
class ReliabilityAssessment:
    """Full, auditable score derivation for one checklist record (AC-03).

    ``checklist`` is the exact stored record the score derives from;
    ``score`` the 0-4 Reliability; ``decisions`` the trace of every rule
    evaluation (all rules, matched flags, scores); ``matched_rule_id`` the
    first matching rule; ``ruleset_version`` the rule table version the
    score was produced with, so old decisions stay interpretable.
    """

    checklist: ReliabilityChecklistRecord
    score: int
    decisions: tuple[ReliabilityRuleDecision, ...]
    matched_rule_id: str
    ruleset_version: str

    @property
    def checklist_ref(self) -> str:
        """The checklist record reference (the AC-01 score companion)."""
        return self.checklist.checklist_ref


def evaluate_reliability(
    checklist: ReliabilityChecklistRecord,
) -> ReliabilityAssessment:
    """Compute the 0-4 Reliability score from a checklist record.

    The AC-01 enforcement point: the only way to obtain a reliability score
    is to evaluate a :class:`ReliabilityChecklistRecord` -- there is no
    score parameter and no other derivation path. Pure and deterministic:
    the score is a pure function of the stored record (equal records ->
    equal assessments), and the returned :class:`ReliabilityAssessment`
    carries the score and the checklist reference together.

    Raises:
        TypeError: ``checklist`` is not a ``ReliabilityChecklistRecord``.
    """
    if not isinstance(checklist, ReliabilityChecklistRecord):
        raise TypeError(
            "evaluate_reliability expects a ReliabilityChecklistRecord, got"
            f" {type(checklist).__name__}"
        )
    decisions: list[ReliabilityRuleDecision] = []
    matched_rule_id: str | None = None
    matched_score = 0  # unreachable default; R-REL-H5 always matches
    for rule in RELIABILITY_RULES:
        matched = rule.predicate(checklist)
        decisions.append(
            ReliabilityRuleDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                score=rule.score,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_score = rule.score
    # R-REL-H5 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return ReliabilityAssessment(
        checklist=checklist,
        score=matched_score,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
        ruleset_version=RELIABILITY_RULESET_VERSION,
    )


# ---------------------------------------------------------------------------
# Checklist persistence registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReliabilityChecklistRegistry:
    """Immutable store of persisted :class:`ReliabilityChecklistRecord` s.

    The persistence home of checklist records (AC-03: the rule result is
    reproducible *from the stored checklist*): records are stored under
    their ``checklist_ref``, and :meth:`evaluate` derives scores from the
    stored record only. ``register`` is functional -- it returns a **new**
    registry and never mutates the caller's, so a stored record can never
    be clobbered.
    """

    records: tuple[ReliabilityChecklistRecord, ...] = ()

    @classmethod
    def from_records(
        cls, records: Sequence[ReliabilityChecklistRecord]
    ) -> ReliabilityChecklistRegistry:
        """Build a registry from a sequence of checklist records.

        Every record is validated with the same rules as ``register``;
        duplicate ``checklist_ref`` values are rejected, so batch
        construction can never silently drop or overwrite a record.

        Raises:
            TypeError: ``records`` is not a sequence (a ``str``/``bytes``
                is rejected explicitly), or an element is not a
                ``ReliabilityChecklistRecord``.
            ReliabilityChecklistDuplicateError: two records share a
                ``checklist_ref``.
        """
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise TypeError(
                "ReliabilityChecklistRegistry.from_records expects a"
                " sequence of ReliabilityChecklistRecord, got"
                f" {type(records).__name__}"
            )
        items = tuple(records)
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, ReliabilityChecklistRecord):
                raise TypeError(
                    "ReliabilityChecklistRegistry.from_records expects"
                    " ReliabilityChecklistRecord elements, got"
                    f" {type(item).__name__}"
                )
            if item.checklist_ref in seen:
                raise ReliabilityChecklistDuplicateError(
                    "reliability checklist reference"
                    f" {item.checklist_ref!r} is already registered"
                )
            seen.add(item.checklist_ref)
        return cls(items)

    def register(
        self, record: ReliabilityChecklistRecord
    ) -> ReliabilityChecklistRegistry:
        """Return a new registry with ``record`` appended.

        Functional by design: the caller's registry is never mutated, so
        records already stored remain retrievable after any later
        registration.

        Raises:
            TypeError: ``record`` is not a ``ReliabilityChecklistRecord``.
            ReliabilityChecklistDuplicateError: the ``checklist_ref`` is
                already registered.
        """
        return self.from_records((*self.records, record))

    def get(self, checklist_ref: str) -> ReliabilityChecklistRecord | None:
        """Return the stored record for ``checklist_ref``, or None.

        Raises:
            TypeError: ``checklist_ref`` is not a ``str``.
        """
        if not isinstance(checklist_ref, str):
            raise TypeError(
                f"checklist_ref must be a str, got {type(checklist_ref).__name__}"
            )
        for record in self.records:
            if record.checklist_ref == checklist_ref:
                return record
        return None

    def evaluate(self, checklist_ref: str) -> ReliabilityAssessment:
        """Derive the reliability score from the stored checklist record.

        The reference-acceptance path of AC-01: a reliability score is
        accepted only for a reference with a **stored** checklist record --
        a reference pointing at nothing raises, because no score can be
        derived from a record that is not there.

        Raises:
            TypeError: ``checklist_ref`` is not a ``str``.
            ReliabilityChecklistRecordError: no record is stored under
                ``checklist_ref``.
        """
        if not isinstance(checklist_ref, str):
            raise TypeError(
                f"checklist_ref must be a str, got {type(checklist_ref).__name__}"
            )
        record = self.get(checklist_ref)
        if record is None:
            raise ReliabilityChecklistRecordError(
                "no reliability checklist record is registered with"
                f" reference {checklist_ref!r}: a reliability score cannot"
                " be accepted without the stored checklist record (AC-01)"
            )
        return evaluate_reliability(record)

    def __iter__(self) -> Iterator[ReliabilityChecklistRecord]:
        """Iterate the stored records in registration order."""
        return iter(self.records)

    def __len__(self) -> int:
        """Number of stored checklist records."""
        return len(self.records)
