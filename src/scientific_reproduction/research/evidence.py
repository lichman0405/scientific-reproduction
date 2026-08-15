"""Claim-specific evidence registry: Source x Claim evidence records (DEV-M5-G03).

Implements the **claim-specific evidence registry** deliverable: a
deterministic, immutable data layer holding frozen
``core.models.ClaimSpecificEvidence`` records (the model mirroring
``schemas/evidence.schema.yaml``) and answering claim-scoped lookups for the
evidence rule engine (``core/rules/evidence.py``, DEV-M2-G03). The frozen
spec grounds this module:

* ``06-EVIDENCE-SYSTEM.md`` SS1 ("Core rule"): *Evidence is assessed as
  Source x Claim, never as one global score for an entire paper.* The
  registry enforces this structurally: assessments exist only as
  (source_id, claim_id) pairs, and every assessment-returning API requires
  both a source and a claim argument (AC-01).
* ``06-EVIDENCE-SYSTEM.md`` SS2: the A/R/D axes are 0-4 rubric levels.
* ``06-EVIDENCE-SYSTEM.md`` SS6 ("Evidence record requirements"): each
  record stores an evidence ID, source ID, claim ID/text, the A/R/D
  scores and the *Goals/decisions using the evidence* -- the record's
  ``used_by`` ref list (AC-03).
* ``09-RESEARCH-SUBSYSTEM.md`` SS5 ("Evidence extraction"): research must
  produce *structured source and evidence records, including exact claims,
  locations, limitations and A/R/D assessments*.
* Frozen acceptance: *evidence is claim-specific*, and *Reliability cannot
  be written without checklist result reference*.
* ``agent-contracts/RESEARCH.md``: the research role builds *the project
  evidence base using traceable sources and claim-specific evidence
  assessments*.

Normative readings
------------------
* **Claim vocabulary**: the frozen model has no ``Claim`` object; a claim
  is the opaque string ``claim_id`` on ``ClaimSpecificEvidence``. The
  registry never interprets claim ids.
* **Directness/Reliability vocabulary**: the frozen axes are the 0-4
  integers of ``EvidenceAssessment`` (``schemas/evidence.schema.yaml``
  declares ``authority``/``reliability``/``directness`` as integers in
  [0, 4]). There is no Directness/Reliability StrEnum in the frozen model;
  the registry types the axes as ``int`` and rejects out-of-range values at
  registration with a stable error -- the same range the rules enforce via
  ``core.rules.evidence._check_axis``.
* **Unassessed state**: the frozen vocabulary defines no UNDETERMINED
  evidence-assessment state (the ``UNDETERMINED`` members of
  ``ReproductionOutcome``/``MethodReproducibility`` are outcome
  vocabularies, not evidence states). The registry's unassessed state is
  therefore **absence**: ``get``/``get_assessment`` return ``None`` for a
  (source, claim) pair with no registered record. No state is invented.
* **Used-by links**: ``ClaimSpecificEvidence.used_by`` is the record's
  generic ref list for the *Goals/decisions using the evidence*
  (06-EVIDENCE-SYSTEM.md SS6) -- refs to Goals, Requirements and decisions
  are opaque strings, stored and returned verbatim, never interpreted and
  never dropped (AC-03). The frozen model has no separate
  ``used_by_goal_ids``/``used_by_requirement_ids`` fields, so the registry
  does not invent them.
* **Referential integrity**: at this milestone the registry is
  intentionally unvalidated against source/claim/goal registries (none
  exist in scope): ids are opaque strings and no cross-registry checks are
  performed. Registration validates the record shape against the frozen
  schema's constraints (types, required fields, 0-4 axis range, checklist
  reference, string used-by entries) with stable errors.

Design
------
``EvidenceRegistry`` is an immutable frozen dataclass over an ordered tuple
of ``ClaimSpecificEvidence`` records. ``register`` is functional -- it
returns a **new** registry with the record appended and never mutates the
caller's registry, so an earlier assessment can never be clobbered by a
later registration (AC-02, by construction). All lookups are pure and
deterministic: first-seen registration order decides first-match semantics,
and repeated queries over the same registry return identical answers --
there is no wall-clock, randomness or counter state anywhere. The registry
is directly consumable by the evidence rules of ``core/rules/evidence.py``:
it is iterable and yields ``ClaimSpecificEvidence`` records, so e.g.
``count_independent_qualifying_sources(registry)`` works unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

from scientific_reproduction.core.models import (
    ClaimSpecificEvidence,
    EvidenceAssessment,
    ResearchSource,
)

__all__ = [
    "EvidenceRegistryError",
    "EvidenceRegistrationError",
    "EvidenceDuplicateError",
    "EvidenceRegistry",
    "validate_evidence_record",
]

#: The 0-4 rubric axis range (06-EVIDENCE-SYSTEM.md SS2; the frozen
#: schema's authority/reliability/directness bounds).
_AXIS_MIN = 0
_AXIS_MAX = 4


class EvidenceRegistryError(ValueError):
    """Base error for the claim-specific evidence registry.

    Raised when a registration violates the frozen evidence-record shape
    (``ClaimSpecificEvidence`` / ``schemas/evidence.schema.yaml``) or the
    registry's identity rules. Stable messages: every message names the
    offending value and the reason.
    """


class EvidenceRegistrationError(EvidenceRegistryError):
    """Raised when a record cannot be registered (malformed record)."""


class EvidenceDuplicateError(EvidenceRegistryError):
    """Raised when an ``evidence_id`` is registered twice.

    An evidence id is the record's identity (06-EVIDENCE-SYSTEM.md SS6,
    "evidence ID"): a registry holds at most one record per id, so a
    duplicate registration is a data-integrity error -- never a silent
    overwrite.
    """


@dataclass(frozen=True)
class EvidenceRegistry:
    """Immutable, claim-scoped store of ``ClaimSpecificEvidence`` records.

    ``records`` holds every registered record in registration order
    (first-seen order is the registry's deterministic ordering). An
    assessment exists only as a (source, claim) pair: every
    assessment-returning method requires both a source and a claim
    argument, so the registry cannot answer "reliability of source S"
    without a claim (AC-01).
    """

    records: tuple[ClaimSpecificEvidence, ...] = ()

    @classmethod
    def from_records(
        cls, records: Sequence[ClaimSpecificEvidence]
    ) -> EvidenceRegistry:
        """Build a registry from a sequence of evidence records.

        Every record is validated with the same shape rules as
        ``register``; duplicate ``evidence_id`` values are rejected, so
        batch construction can never silently drop or overwrite a record.

        Raises:
            TypeError: ``records`` is not a sequence (a ``str``/``bytes``
                is rejected explicitly), or an element is not a
                ``ClaimSpecificEvidence``.
            EvidenceRegistrationError: a record violates the frozen
                evidence shape.
            EvidenceDuplicateError: two records share an ``evidence_id``.
        """
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
            raise TypeError(
                "EvidenceRegistry.from_records expects a sequence of"
                f" ClaimSpecificEvidence, got {type(records).__name__}"
            )
        items = tuple(records)
        seen: set[str] = set()
        for item in items:
            validate_evidence_record(item)
            if item.evidence_id in seen:
                raise EvidenceDuplicateError(
                    f"evidence {item.evidence_id!r} is already registered"
                )
            seen.add(item.evidence_id)
        return cls(items)

    def register(self, evidence: ClaimSpecificEvidence) -> EvidenceRegistry:
        """Return a new registry with ``evidence`` appended.

        Functional by design: the caller's registry is never mutated, so
        assessments already registered remain retrievable after any later
        registration (AC-02: registrations never clobber one another).

        Raises:
            TypeError: ``evidence`` is not a ``ClaimSpecificEvidence``.
            EvidenceRegistrationError: the record violates the frozen
                evidence shape.
            EvidenceDuplicateError: the ``evidence_id`` is already
                registered.
        """
        return self.from_records((*self.records, evidence))

    def get(
        self, source: ResearchSource | str, claim_id: str
    ) -> ClaimSpecificEvidence | None:
        """Return the first-registered record for the (source, claim) pair.

        Claim-scoped by construction (AC-01): the pair is mandatory -- there
        is no way to ask the registry for a source's evidence alone.
        Deterministic: the first record registered for the pair wins; every
        record for the pair is available via ``get_all``.

        Raises:
            TypeError: ``source`` is not a ``ResearchSource`` or a ``str``,
                or ``claim_id`` is not a ``str``.
        """
        records = self.get_all(source, claim_id)
        return records[0] if records else None

    def get_assessment(
        self, source: ResearchSource | str, claim_id: str
    ) -> EvidenceAssessment | None:
        """Return the assessment of the (source, claim) record, if registered.

        The unassessed state is absence (see the module docstring):
        ``None`` when no record is registered for the pair -- the frozen
        vocabulary defines no UNDETERMINED evidence state, so none is
        invented. This is the lookup the evidence rules consume: the
        returned ``EvidenceAssessment`` is accepted by the hard-gate
        predicates of ``core/rules/evidence.py``.

        Raises:
            TypeError: ``source`` is not a ``ResearchSource`` or a ``str``,
                or ``claim_id`` is not a ``str``.
        """
        record = self.get(source, claim_id)
        return None if record is None else record.assessment

    def get_all(
        self, source: ResearchSource | str, claim_id: str
    ) -> tuple[ClaimSpecificEvidence, ...]:
        """Return every registered record for the (source, claim) pair.

        Registration order is preserved. A pair may legitimately carry
        several records (different extractions or source locations for the
        same claim), and none shadows another -- the registry is purely
        additive (AC-02).

        Raises:
            TypeError: ``source`` is not a ``ResearchSource`` or a ``str``,
                or ``claim_id`` is not a ``str``.
        """
        source_id = _coerce_source_id(source, "EvidenceRegistry")
        if not isinstance(claim_id, str):
            raise TypeError(
                f"claim_id must be a str, got {type(claim_id).__name__}"
            )
        return tuple(
            record
            for record in self.records
            if record.source_id == source_id and record.claim_id == claim_id
        )

    def used_by(
        self, source: ResearchSource | str, claim_id: str
    ) -> tuple[str, ...]:
        """Return the used-by refs of the (source, claim) record.

        The record's ``used_by`` list holds the Goals/decisions using the
        evidence (06-EVIDENCE-SYSTEM.md SS6) as opaque refs (AC-03); refs
        are returned verbatim in stored order and never dropped. An empty
        tuple when the pair is unassessed or the record carries no refs.

        Raises:
            TypeError: ``source`` is not a ``ResearchSource`` or a ``str``,
                or ``claim_id`` is not a ``str``.
        """
        record = self.get(source, claim_id)
        return () if record is None else tuple(record.used_by)

    def is_assessed(self, source: ResearchSource | str, claim_id: str) -> bool:
        """True when at least one record is registered for the pair.

        Raises:
            TypeError: ``source`` is not a ``ResearchSource`` or a ``str``,
                or ``claim_id`` is not a ``str``.
        """
        return self.get(source, claim_id) is not None

    def records_for_source(
        self, source: ResearchSource | str
    ) -> tuple[ClaimSpecificEvidence, ...]:
        """Every record of one source, in registration order.

        Raises:
            TypeError: ``source`` is not a ``ResearchSource`` or a ``str``.
        """
        source_id = _coerce_source_id(source, "EvidenceRegistry")
        return tuple(
            record for record in self.records if record.source_id == source_id
        )

    def records_for_claim(
        self, claim_id: str
    ) -> tuple[ClaimSpecificEvidence, ...]:
        """Every record of one claim, in registration order.

        Raises:
            TypeError: ``claim_id`` is not a ``str``.
        """
        if not isinstance(claim_id, str):
            raise TypeError(
                f"claim_id must be a str, got {type(claim_id).__name__}"
            )
        return tuple(
            record for record in self.records if record.claim_id == claim_id
        )

    def sources(self) -> tuple[str, ...]:
        """Distinct source ids with at least one record, first-seen order."""
        return _distinct_in_order(record.source_id for record in self.records)

    def claims(self) -> tuple[str, ...]:
        """Distinct claim ids with at least one record, first-seen order."""
        return _distinct_in_order(record.claim_id for record in self.records)

    def all_used_by(self) -> tuple[str, ...]:
        """Distinct used-by refs across all records, first-seen order.

        The aggregate linkage view (AC-03): every Goal/Requirement/decision
        ref the registry's evidence base is linked to.
        """
        refs: list[str] = []
        for record in self.records:
            for ref in record.used_by:
                if ref not in refs:
                    refs.append(ref)
        return tuple(refs)

    def __iter__(self) -> Iterator[ClaimSpecificEvidence]:
        """Iterate the registered records in registration order.

        Makes the registry directly consumable by the evidence rules of
        ``core/rules/evidence.py`` (e.g.
        ``count_independent_qualifying_sources(registry)``).
        """
        return iter(self.records)

    def __len__(self) -> int:
        """Number of registered records."""
        return len(self.records)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_source_id(source: ResearchSource | str, function: str) -> str:
    """Resolve a ``ResearchSource`` or a source id to a source id string.

    Raises:
        TypeError: ``source`` is neither a ``ResearchSource`` nor a ``str``.
    """
    if isinstance(source, ResearchSource):
        return source.source_id
    if isinstance(source, str):
        return source
    raise TypeError(
        f"{function} expects a ResearchSource or a str source id, got"
        f" {type(source).__name__}"
    )


def validate_evidence_record(evidence: ClaimSpecificEvidence) -> None:
    """Validate one record against the frozen evidence shape.

    Checks (stable messages, all raising ``EvidenceRegistrationError``):

    * ``evidence_id`` / ``source_id`` / ``claim_id`` / ``finding`` are
      non-empty strings (06-EVIDENCE-SYSTEM.md SS6: an evidence record
      must store an evidence ID, source ID and claim ID/text);
    * the A/R/D axes are integers within the 0-4 rubric range
      (06-EVIDENCE-SYSTEM.md SS2; the frozen schema bounds);
    * ``reliability_checklist_ref`` is a non-empty string (frozen
      acceptance: Reliability cannot be written without a checklist
      result reference);
    * every ``used_by`` entry is a non-empty string (AC-03 refs are
      opaque strings).

    Raises:
        TypeError: ``evidence`` is not a ``ClaimSpecificEvidence``.
        EvidenceRegistrationError: the record violates the shape above.
    """
    if not isinstance(evidence, ClaimSpecificEvidence):
        raise TypeError(
            "the evidence registry stores ClaimSpecificEvidence records,"
            f" got {type(evidence).__name__}"
        )
    for field_name in ("evidence_id", "source_id", "claim_id"):
        value = getattr(evidence, field_name)
        if not isinstance(value, str) or not value:
            raise EvidenceRegistrationError(
                f"evidence {field_name} must be a non-empty string, got"
                f" {value!r}"
            )
    if not isinstance(evidence.finding, str) or not evidence.finding:
        raise EvidenceRegistrationError(
            "evidence finding must be a non-empty string, got"
            f" {evidence.finding!r}"
        )
    for axis in ("authority", "reliability", "directness"):
        value = getattr(evidence.assessment, axis)
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvidenceRegistrationError(
                f"assessment {axis} must be an int, got {type(value).__name__}"
            )
        if not _AXIS_MIN <= value <= _AXIS_MAX:
            raise EvidenceRegistrationError(
                f"assessment {axis} must be within the 0-4 rubric range,"
                f" got {value}"
            )
    ref = evidence.assessment.reliability_checklist_ref
    if not isinstance(ref, str) or not ref:
        raise EvidenceRegistrationError(
            "assessment reliability_checklist_ref must be a non-empty string:"
            " reliability cannot be written without a checklist result"
            " reference"
        )
    for entry in evidence.used_by:
        if not isinstance(entry, str) or not entry:
            raise EvidenceRegistrationError(
                "used_by entries must be non-empty strings (Goals/decisions"
                f" using the evidence), got {entry!r}"
            )


def _distinct_in_order(values: Iterator[str]) -> tuple[str, ...]:
    """Distinct values in first-seen order."""
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)
