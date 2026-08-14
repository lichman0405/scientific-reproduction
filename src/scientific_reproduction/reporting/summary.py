"""Final outcome and method-reproducibility summaries (DEV-M13-G03).

Implements the **final summary rendering/serialization** deliverable of
DEV-M13-G03: the final outcome summary and the method reproducibility
summary, rendered from **already-evaluated** Requirements without
introducing new scientific decision logic in reporting. The frozen
objective forbids the reporting layer from re-deriving or re-deciding
scientific outcomes; this module consumes, and renders verbatim, the
recorded state and the Core aggregation results:

* the persisted Requirement records -- ``ReproductionRequirement.outcome``
  / ``method_reproducibility`` through the real registration API
  ``planning.inventory.list_requirements`` (the frozen
  ``RequirementOutcome`` vocabulary of ``schemas/requirement.schema.yaml``,
  ``05-GOAL-RUN-SCHEMA.md`` SS2: ``OPEN`` / ``REPRODUCED`` /
  ``REPRODUCED_WITH_RECOVERY`` / ``NOT_REPRODUCED`` / ``INCONCLUSIVE``),
  rendered **verbatim** -- an outcome that is not recorded is rendered as
  recorded (``OPEN`` stays ``OPEN``, an unrated ``method_reproducibility``
  renders the canonical ``UNDETERMINED`` of the Core input model), never
  computed;
* the **Core aggregation results** -- the versioned rule module
  ``core.rules.outcome`` (DEV-M2-G06), the exact aggregator mandated by the
  aggregation rules spec ``04-PROJECT-LIFECYCLE.md`` sections 4-6 ("The
  exact aggregator should be implemented as a versioned rule module, not
  hardcoded across agents"): ``aggregate_project_outcome`` and
  ``aggregate_method_reproducibility`` are **called** with the requirement
  records, and their assessments are rendered exactly -- the
  ``ReproductionOutcome`` value (``FULLY_REPRODUCED`` /
  ``PARTIALLY_REPRODUCED`` / ``NOT_REPRODUCED_WITHIN_DEFINED_SCOPE`` /
  ``INCONCLUSIVE`` / ``UNDETERMINED``, ``04-PROJECT-LIFECYCLE.md`` section
  3), the ``MethodReproducibility`` rating, the ruleset version, the
  matched rule id and the blocking reasons. Nothing is re-implemented
  here: the summary reflects the Core aggregation exactly (AC-01) because
  the rendered values are the aggregation's own values over the same
  records;
* the recorded closure decision -- ``ClosureContract.closure_allowed`` of
  the registered closure contracts (``planning.plan.list_closure_contracts``),
  consumed (never re-derived) as the ``closure_allowed`` input of
  ``aggregate_project_outcome``, mirroring the composition contract of
  ``core.rules.outcome`` with ``rules.closure``.

Phase/outcome separation (AC-02)
--------------------------------
``project_phase`` and the outcome vocabularies are strictly separate
(``04-PROJECT-LIFECYCLE.md`` section 1: the phase answers "where is the
workflow now?", the outcome answers "what is the final scientific
reproduction conclusion?"). The summary renders ``project_phase`` in its
own section with its own ``ProjectPhase`` vocabulary
(``schemas/project.schema.yaml``) and renders every outcome value
(``RequirementOutcome``, ``ReproductionOutcome``, ``MethodReproducibility``)
verbatim from its own vocabulary; no symbol, section or rendering path ever
maps a phase value onto an outcome value or vice versa.

Recovery summary (AC-03)
------------------------
v0.1 represents "recovery" through the frozen records of
``08-STRICT-RECOVERY-CLOSURE.md`` (section 1 tracks, section 2 recovery
levels L1-L4, section 5 closure outcomes) and ``18-TEST-AND-ACCEPTANCE-PLAN.md``
Scenario B ("scientific Requirement ``REPRODUCED_WITH_RECOVERY``; method
reproducibility lower than direct reproducibility"):

* a Requirement whose **recorded** outcome is ``REPRODUCED_WITH_RECOVERY``
  (a recovery closure);
* a Goal whose **recorded** track is ``RECOVERY`` or ``METHOD_REDESIGN``
  (``GoalContract.track``, ``05-GOAL-RUN-SCHEMA.md`` SS4);
* the **recorded** per-Requirement ``MethodReproducibility`` ratings, the
  vocabulary that encodes the recovery levels (``REPRODUCIBLE_WITH_MINOR_RECOVERY``
  ~ L1/L2, ``REPRODUCIBLE_WITH_METHOD_ADJUSTMENT`` ~ L3,
  ``ONLY_REPRODUCIBLE_AFTER_REDESIGN`` ~ L4 method redesign);
* the **recorded** ``ClosureContract.closure_allowed`` decisions of the
  registered closure contracts (the v0.1 closure-contract records of the
  Closure Contract governance, ``08-STRICT-RECOVERY-CLOSURE.md`` section 4).

The :class:`RecoverySummary` section counts and lists exactly those
recorded values -- the summary **never** decides which recovery level a
recorded recovery was, never infers a recovery from any other data, and
never invents a closure decision: when no closure contract is registered
(or the registered contracts record conflicting decisions) the
aggregation input is ``None`` (unassessed), mirroring the unresolved-gate
philosophy of ``rules.closure``.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state: no wall clock, no
randomness, no network. All collections are sorted by stable keys
(requirements by ``requirement_id``, goals by ``goal_id``, closure
contracts by ``closure_id``), so identical state always yields
byte-identical canonical JSON. ``TypeError`` at the public boundaries;
errors follow the ``ValueError``-subclass convention with stable messages;
``from __future__ import annotations``; ``__all__``. The summary only
reads: it creates no durable records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scientific_reproduction.core.models import (
    ClosureContract,
    Criticality,
    GoalContract,
    GoalTrack,
    MethodReproducibility,
    ProjectPhase,
    ReproductionOutcome,
    ReproductionRequirement,
    RequirementOutcome,
)
from scientific_reproduction.core.rules.outcome import (
    MethodReproducibilityRecord,
    RequirementOutcomeRecord,
    aggregate_method_reproducibility,
    aggregate_project_outcome,
)
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    read_project_state,
)
from scientific_reproduction.planning.inventory import list_requirements
from scientific_reproduction.planning.plan import (
    list_closure_contracts,
    list_goals,
)

__all__ = [
    "SUMMARY_VERSION",
    "OutcomeSummary",
    "RecoverySummary",
    "RequirementOutcomeEntry",
    "SummaryCorruptError",
    "SummaryError",
    "SummaryNotInitializedError",
    "build_summary",
]

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2

#: Version of the summary serialization (``version`` key of
#: :class:`OutcomeSummary`).
SUMMARY_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class SummaryError(ValueError):
    """Base class for all summary rendering errors."""


class SummaryNotInitializedError(SummaryError):
    """Raised when summarizing is attempted on a workspace without a project
    state record (no ``project.yaml`` at the root)."""


class SummaryCorruptError(SummaryError):
    """Raised when a stored record the summary reads is corrupt.

    The registered state is read through the real registry read APIs;
    those APIs surface corruption as ``ValueError``, which this module
    re-raises as ``SummaryCorruptError`` with the same message so the
    summary's error surface stays stable.
    """


# ---------------------------------------------------------------------------
# The rendered sections (all values from recorded state, verbatim)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequirementOutcomeEntry:
    """One Requirement of the outcome summary, rendered verbatim (AC-01).

    Every field is the recorded value of the persisted
    ``ReproductionRequirement`` record read through the real inventory
    API -- nothing is transformed, derived or decided: ``outcome`` is the
    exact ``RequirementOutcome`` value (``OPEN`` stays ``OPEN``, AC-02),
    ``method_reproducibility`` is the recorded rating or, when the record
    carries none, the canonical ``UNDETERMINED`` of the Core input model
    (``core.rules.outcome.MethodReproducibilityRecord``: "an unrated
    Requirement is an undetermined one, never an invented rating").
    """

    requirement_id: str
    statement: str
    criticality: Criticality
    outcome: RequirementOutcome
    method_reproducibility: MethodReproducibility

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the entry in canonical field order."""
        return {
            "requirement_id": self.requirement_id,
            "statement": self.statement,
            "criticality": self.criticality.value,
            "outcome": self.outcome.value,
            "method_reproducibility": self.method_reproducibility.value,
        }


@dataclass(frozen=True)
class RecoverySummary:
    """The recovery state of the project, summarized from the recorded
    records (AC-03, ``08-STRICT-RECOVERY-CLOSURE.md`` sections 1-2).

    ``recovered_requirements`` are the ids of the Requirements whose
    **recorded** outcome is ``REPRODUCED_WITH_RECOVERY``;
    ``recovery_goals`` / ``method_redesign_goals`` the ids of the Goals
    whose **recorded** track is ``RECOVERY`` / ``METHOD_REDESIGN``;
    ``recorded_closure_decisions`` the **recorded**
    ``ClosureContract.closure_allowed`` of every registered closure
    contract (``(closure_id, closure_allowed)``). All collections are
    sorted by stable keys and contain only values found verbatim in the
    records -- the summary counts recorded recovery, it never decides
    which recovery level a recovery was.
    """

    recovered_requirements: tuple[str, ...]
    recovery_goals: tuple[str, ...]
    method_redesign_goals: tuple[str, ...]
    recorded_closure_decisions: tuple[tuple[str, bool], ...]

    @property
    def recovered_count(self) -> int:
        """The number of Requirements recorded with a recovery closure."""
        return len(self.recovered_requirements)

    @property
    def recovery_goal_count(self) -> int:
        """The number of Goals recorded on the RECOVERY track."""
        return len(self.recovery_goals)

    @property
    def method_redesign_goal_count(self) -> int:
        """The number of Goals recorded on the METHOD_REDESIGN track."""
        return len(self.method_redesign_goals)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the recovery summary in canonical field order."""
        return {
            "recovered_count": self.recovered_count,
            "recovered_requirement_ids": list(self.recovered_requirements),
            "recovery_track_goal_count": self.recovery_goal_count,
            "recovery_track_goal_ids": list(self.recovery_goals),
            "method_redesign_track_goal_count": self.method_redesign_goal_count,
            "method_redesign_track_goal_ids": list(self.method_redesign_goals),
            "recorded_closure_decisions": [
                {"closure_id": closure_id, "closure_allowed": allowed}
                for closure_id, allowed in self.recorded_closure_decisions
            ],
        }


@dataclass(frozen=True)
class OutcomeSummary:
    """The final summary of the project, rendered from recorded state.

    ``project_phase`` is the recorded ``ProjectPhase`` value of the
    ``project.yaml`` record, rendered in its own section with its own
    vocabulary (AC-02); ``reproduction_outcome`` is the exact
    ``ReproductionOutcome`` value of the **Core aggregation**
    (``core.rules.outcome.aggregate_project_outcome`` over the recorded
    Requirement outcomes, consuming the recorded closure decisions),
    with the aggregation's ``outcome_ruleset_version``,
    ``outcome_matched_rule_id`` and ``outcome_blocking_reasons``;
    ``requirements`` are the per-Requirement entries (verbatim, sorted by
    ``requirement_id``); ``method_reproducibility`` is the exact
    ``MethodReproducibility`` rating of the Core aggregation
    (``aggregate_method_reproducibility``), with its ruleset version and
    matched rule id; ``recovery`` is the :class:`RecoverySummary` (AC-03).
    """

    project_id: str
    project_phase: ProjectPhase
    reproduction_outcome: ReproductionOutcome
    outcome_ruleset_version: str
    outcome_matched_rule_id: str
    outcome_blocking_reasons: tuple[str, ...]
    requirements: tuple[RequirementOutcomeEntry, ...]
    method_reproducibility: MethodReproducibility
    method_ruleset_version: str
    method_matched_rule_id: str
    recovery: RecoverySummary

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the summary in canonical field order."""
        return {
            "version": SUMMARY_VERSION,
            "project_id": self.project_id,
            "project_phase": self.project_phase.value,
            "reproduction_outcome": self.reproduction_outcome.value,
            "outcome_ruleset_version": self.outcome_ruleset_version,
            "outcome_matched_rule_id": self.outcome_matched_rule_id,
            "outcome_blocking_reasons": list(self.outcome_blocking_reasons),
            "requirements": [entry.to_dict() for entry in self.requirements],
            "method_reproducibility": self.method_reproducibility.value,
            "method_ruleset_version": self.method_ruleset_version,
            "method_matched_rule_id": self.method_matched_rule_id,
            "recovery": self.recovery.to_dict(),
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Summary rendering (pure, deterministic; consumes the Core aggregation)
# ---------------------------------------------------------------------------


def build_summary(root: str | Path) -> OutcomeSummary:
    """Build the final outcome and method-reproducibility summary (AC-01..03).

    Reads the recorded state through the real registration APIs -- the
    ``project.yaml`` record (``planning.init.read_project_state``), the
    persisted Requirement records (``planning.inventory.list_requirements``),
    the Goal contracts (``planning.plan.list_goals``) and the closure
    contracts (``planning.plan.list_closure_contracts``) -- and **consumes**
    the Core aggregation results: ``core.rules.outcome.aggregate_project_outcome``
    over the recorded Requirement outcomes (with the recorded closure
    decision of the registered closure contracts, or ``None`` -- unassessed
    -- when nothing is recorded or the recorded decisions conflict) and
    ``aggregate_method_reproducibility`` over the recorded per-Requirement
    ratings. The rendered outcome values are the recorded values and the
    aggregation's own values, verbatim: no scientific outcome is derived or
    decided in this module (``OPEN`` stays ``OPEN``; an unrated Requirement
    aggregates as ``UNDETERMINED`` through the Core input model, never
    through a reporting-side mapping).

    Args:
        root: the initialized workspace root.

    Returns:
        The deterministic :class:`OutcomeSummary`.

    Raises:
        TypeError: ``root`` is not a str/Path.
        SummaryNotInitializedError: no ``project.yaml`` exists at ``root``.
        SummaryCorruptError: a stored record the summary reads is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)

    try:
        project = read_project_state(project_root)
        requirements = list_requirements(project_root)
        goals = list_goals(project_root)
        closures = list_closure_contracts(project_root)
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc

    try:
        requirement_records = tuple(
            RequirementOutcomeRecord.from_reproduction_requirement(requirement)
            for requirement in requirements
        )
        closure_allowed = _recorded_closure_allowed(closures)
        outcome_assessment = aggregate_project_outcome(
            requirement_records, closure_allowed=closure_allowed
        )
        method_records = tuple(
            MethodReproducibilityRecord.from_reproduction_requirement(requirement)
            for requirement in requirements
        )
        method_assessment = aggregate_method_reproducibility(method_records)
    except ValueError as exc:
        # The Core input models reject malformed records (OutcomeRecordError,
        # a ValueError subclass); a schema-valid registry cannot produce one,
        # so reaching this path means the registered state is corrupt.
        raise _wrap_corrupt(exc) from exc

    entries = tuple(
        RequirementOutcomeEntry(
            requirement_id=requirement.requirement_id,
            statement=requirement.statement,
            criticality=requirement.criticality,
            outcome=requirement.outcome,
            method_reproducibility=_recorded_reproducibility(requirement),
        )
        for requirement in sorted(requirements, key=lambda r: r.requirement_id)
    )

    return OutcomeSummary(
        project_id=project.project_id,
        project_phase=project.project_phase,
        reproduction_outcome=outcome_assessment.outcome,
        outcome_ruleset_version=outcome_assessment.ruleset_version,
        outcome_matched_rule_id=outcome_assessment.matched_rule_id,
        outcome_blocking_reasons=outcome_assessment.blocking_reasons,
        requirements=entries,
        method_reproducibility=method_assessment.reproducibility,
        method_ruleset_version=method_assessment.ruleset_version,
        method_matched_rule_id=method_assessment.matched_rule_id,
        recovery=_build_recovery_summary(requirements, goals, closures),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject summarizing on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise SummaryNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _wrap_corrupt(exc: ValueError) -> SummaryCorruptError:
    """Re-raise a stored-record corruption as ``SummaryCorruptError``."""
    return SummaryCorruptError(f"corrupt registered state: {exc}")


def _recorded_closure_allowed(
    closures: tuple[ClosureContract, ...],
) -> bool | None:
    """The recorded closure decision consumed by the project aggregation.

    The recorded ``closure_allowed`` of the registered closure contracts:
    when no contract is registered, nothing is recorded and the decision is
    ``None`` (unassessed); when the registered contracts record one uniform
    decision, that value is consumed; when they record conflicting
    decisions, the recorded state is ambiguous and the aggregation input is
    ``None`` (unassessed) -- every contract's recorded value stays visible
    in :class:`RecoverySummary.recorded_closure_decisions` either way.
    Nothing is derived: the consumed value is a recorded value or the
    unassessed state.
    """
    decisions = {contract.closure_allowed for contract in closures}
    if len(decisions) == 1:
        return next(iter(decisions))
    return None


def _recorded_reproducibility(
    requirement: ReproductionRequirement,
) -> MethodReproducibility:
    """The recorded rating, or the Core input model's canonical UNDETERMINED.

    Consumes ``core.rules.outcome.MethodReproducibilityRecord`` -- the exact
    input model of the Core method-reproducibility aggregation -- so the
    rendered per-Requirement rating always equals the rating the aggregation
    consumed for that Requirement (AC-01): a record carrying no rating is an
    undetermined one, never an invented rating.
    """
    return MethodReproducibilityRecord.from_reproduction_requirement(
        requirement
    ).reproducibility


def _build_recovery_summary(
    requirements: tuple[ReproductionRequirement, ...],
    goals: tuple[GoalContract, ...],
    closures: tuple[ClosureContract, ...],
) -> RecoverySummary:
    """Summarize the recorded recovery state (AC-03), sorted deterministically.

    Only recorded values participate: requirements whose recorded outcome is
    ``REPRODUCED_WITH_RECOVERY``, goals whose recorded track is ``RECOVERY``
    / ``METHOD_REDESIGN``, and the recorded ``closure_allowed`` of every
    registered closure contract. The summary counts and lists; it never
    decides which recovery level a recorded recovery was.
    """
    sorted_requirements = sorted(
        requirements, key=lambda requirement: requirement.requirement_id
    )
    sorted_goals = sorted(goals, key=lambda goal: goal.goal_id)
    recovered = tuple(
        requirement.requirement_id
        for requirement in sorted_requirements
        if requirement.outcome is RequirementOutcome.REPRODUCED_WITH_RECOVERY
    )
    recovery_goals = tuple(
        goal.goal_id
        for goal in sorted_goals
        if goal.track is GoalTrack.RECOVERY
    )
    redesign_goals = tuple(
        goal.goal_id
        for goal in sorted_goals
        if goal.track is GoalTrack.METHOD_REDESIGN
    )
    closure_decisions = tuple(
        (contract.closure_id, contract.closure_allowed)
        for contract in sorted(closures, key=lambda contract: contract.closure_id)
    )
    return RecoverySummary(
        recovered_requirements=recovered,
        recovery_goals=recovery_goals,
        method_redesign_goals=redesign_goals,
        recorded_closure_decisions=closure_decisions,
    )
