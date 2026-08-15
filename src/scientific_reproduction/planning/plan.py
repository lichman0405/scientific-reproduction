"""Plan v1 construction and the plan / goal-contract registries (DEV-M4-G04).

Implements the **Plan v1 builder** deliverable of DEV-M4-G04 over the
frozen models and the registered M4-G02/G03 state, grounded in:

* ``01-PRODUCT-REQUIREMENTS.md`` SS5 steps 7-8: the Supervisor "creates
  Work Packages, Requirements, Unit Process ``/goals``, dependencies,
  resources, acceptance criteria, replication plans, primary analysis
  protocols, assumption registry and closure contracts", then "Plan v1 is
  audited and frozen";
* ``14-STATE-GIT-ARTIFACTS.md`` SS3 (per-object state files) and SS5
  ("Plan v1 frozen" git checkpoint) -- the ``plans/`` directory of
  ``templates/PROJECT-TREE.template.txt`` is the plan state dir;
* ``core/models.py``: ``Plan`` (plan_id, version, status, inventory_audit,
  goal_ids, requirement_ids, parent_plan_version, work_packages,
  resource_ids, frozen_at, frozen_commit), ``PlanStatus``
  (DRAFT/UNDER_AUDIT/FROZEN/SUPERSEDED), ``PlanInventoryAudit``, and the
  goal-contract family ``GoalContract`` / ``AcceptanceCriteria`` /
  ``AnalysisProtocolOrResult`` / ``ClosureContract`` / ``GoalAcceptance``;
* ``schemas/plan.schema.yaml``: the plan object shape (``inventory_audit``
  sub-object, ``status`` enum DRAFT/UNDER_AUDIT/FROZEN/SUPERSEDED,
  ``parent_plan_version`` / ``frozen_at`` / ``frozen_commit`` nullable);
* ``planning/audit.py`` (DEV-M4-G03): ``audit_inventory_registry`` /
  ``evaluate_completeness_audit`` and the ``plan_inventory_audit()`` view
  -- the plan v1 record embeds that view as its ``inventory_audit``, and
  the audit is always recomputed from the registered state (never trusted
  from a stored snapshot);
* ``planning/inventory.py`` (DEV-M4-G02): ``load_inventory_registry`` --
  the typed registered state (items + requirements) the plan is a pure
  function of;
* ``planning/init.py``: ``INITIAL_PLAN_VERSION`` (``"v1-draft"`` -- the
  pre-freeze working version of ``examples/fdm-201/project.example.yaml``),
  ``ProjectNotInitializedError`` and the project state record;
* ``05-GOAL-RUN-SCHEMA.md`` SS2 (a Requirement "may map to one or more
  Goals") and SS4 (Goal Contract "version/freeze metadata"): the plan's
  ``goal_ids`` are the distinct goal ids the registered requirements map
  to, and the goal-contract family carries version/freeze fields.

Plan v1 builder (determinism)
-----------------------------
``build_plan_v1`` constructs the DRAFT plan (``PlanStatus.DRAFT``, version
``INITIAL_PLAN_VERSION``) as a **pure function of the registered state**:
same state -> same plan content. ``plan_id`` is derived deterministically
from the project id (``core.ids.generate_id``), ``goal_ids`` is the sorted
distinct goal-id set of the registered requirements, ``requirement_ids``
the sorted registered requirement ids, and ``inventory_audit`` the
recomputed ``PlanInventoryAudit`` view of the completeness audit. Work
packages and resource ids are empty lists in v1 drafts: the work-package
DAG arrives with DEV-M4-G05 (resource blocker + planning DAG export). No
wall clock, no randomness, no LLM; the draft record is written by the
freeze flow (``planning/freeze.py``), not by the builder.

Version semantics (normative)
-----------------------------
Versions are ``v<N>`` (formal) or ``v<N>-draft`` (draft); the initial
draft is ``"v1-draft"`` (``INITIAL_PLAN_VERSION``) and freezing a draft
produces the formal version of the same number (``"v1-draft"`` ->
``"v1"``). ``next_version`` increments the number of a formal version
(``"v1"`` -> ``"v2"``); the frozen plan records carry
``parent_plan_version`` pointing at the version they were revised from
(``core/models.py`` ``Plan.parent_plan_version``).

The plan registry (normative)
-----------------------------
The registry follows the M4-G02 inventory pattern: canonical JSON records
via ``core.atomic.atomic_write`` (atomic, parent dirs created), ids
validated as single path segments, schema-validated before persistence
(``validate_and_reject`` ``"plan"``), and **immutable-functional**: a
version is written exactly once, and re-registration of a version raises
``DuplicatePlanVersionError``. The registry keys on **version**
(``plans/<version>.json``), one file per plan version: there is exactly
one plan lineage per project (``core/models.py`` ``Project`` carries a
single ``current_plan_version``), and version-keyed files preserve every
historical record -- the storage that makes revision semantics (AC-03)
immutable. ``plan_lineage`` returns the versioned views with the
**effective** status recomputed by the versioned ``SUPERSEDED_RULES``
rule table (first match wins, trailing total default): a stored FROZEN
record is *superseded* iff a newer version of the lineage is registered.
The stored record bytes are never rewritten -- ``SUPERSEDED`` is a
computed lineage status, never a stored mutation (the M4-G02 convention
that statuses are recomputed from state, never trusted from stored
snapshots).

Goal-contract registries (normative)
------------------------------------
The goal-contract family (``GoalContract``, ``AcceptanceCriteria``,
``StatisticalDesign``, ``AnalysisProtocolOrResult``,
``ClosureContract``) is authored through draft registrations before the
plan freeze (``01-PRODUCT-REQUIREMENTS.md`` SS5 step 7 precedes step 8):
``register_goal`` / ``register_acceptance`` /
``register_statistical_design`` / ``register_analysis_protocol`` /
``register_closure_contract`` persist one schema-validated record per id
into ``goals/``, ``acceptance/``, ``designs/``, ``protocols/`` and
``closure/``. The first and third directories are declared in
``templates/PROJECT-TREE.template.txt``; ``acceptance/``, ``designs/``
and ``closure/`` are created on demand by ``atomic_write`` -- normative
reading: the tree template enumerates the init-time layout, and the
per-object state-file convention (``14-STATE-GIT-ARTIFACTS.md`` SS3)
applies to every record kind that has a schema
(``schemas/acceptance-criteria.schema.yaml``,
``schemas/statistical-design.schema.yaml``,
``schemas/closure-contract.schema.yaml``; the FDM-201 example ships
``examples/fdm-201/acceptance.example.yaml`` and
``examples/fdm-201/statistical-design.example.yaml``). The registries
are immutable-functional like the M4-G02 registry: a record id can be
registered exactly once (``DuplicateGoalError`` and siblings), drafts
carry the draft version (``"v1-draft"``) with ``frozen`` False, and the
freeze flow (``planning/freeze.py``) consumes them to produce the frozen
Goal/Acceptance/StatisticalDesign/Analysis/Closure contracts.

Pure deterministic functions, no randomness, no wall-clock, no LLM;
``TypeError`` at the public boundaries; errors of the registry path
(``ProjectNotInitializedError``, corrupt-record ``ValueError``) follow
the ``planning/inventory.py`` conventions with stable messages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias, TypeVar, cast

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisProtocolOrResult,
    ClosureContract,
    CoreModel,
    GoalContract,
    Plan,
    PlanStatus,
    StatisticalDesign,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.audit import evaluate_completeness_audit
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    PROJECT_STATE_FILENAME,
    PlanningError,
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.inventory import load_inventory_registry

__all__ = [
    "ACCEPTANCE_STATE_DIR",
    "CLOSURE_STATE_DIR",
    "DESIGNS_STATE_DIR",
    "GOALS_STATE_DIR",
    "PLANS_STATE_DIR",
    "PROTOCOLS_STATE_DIR",
    "SUPERSEDED_RULES",
    "SUPERSEDED_RULESET_VERSION",
    "AcceptanceInput",
    "AcceptanceNotFoundError",
    "AnalysisInput",
    "AnalysisProtocolNotFoundError",
    "ClosureContractNotFoundError",
    "ClosureInput",
    "DuplicateAcceptanceError",
    "DuplicateAnalysisProtocolError",
    "DuplicateClosureContractError",
    "DuplicateGoalError",
    "DuplicatePlanVersionError",
    "DuplicateStatisticalDesignError",
    "GoalFamilyError",
    "GoalInput",
    "GoalNotFoundError",
    "InvalidPlanIdError",
    "InvalidPlanVersionError",
    "InvalidRecordIdError",
    "PlanError",
    "PlanInput",
    "PlanLineageEntry",
    "PlanNotFoundError",
    "PlanStatusAssessment",
    "PlanStatusDecision",
    "PlanStatusInput",
    "PlanStatusRule",
    "StatisticalDesignInput",
    "StatisticalDesignNotFoundError",
    "build_plan_v1",
    "formal_version",
    "is_draft_version",
    "is_formal_version",
    "list_acceptance",
    "list_analysis_protocols",
    "list_closure_contracts",
    "list_goals",
    "list_plans",
    "list_statistical_designs",
    "next_version",
    "plan_lineage",
    "read_acceptance",
    "read_analysis_protocol",
    "read_closure_contract",
    "read_goal",
    "read_plan",
    "read_statistical_design",
    "register_acceptance",
    "register_analysis_protocol",
    "register_closure_contract",
    "register_goal",
    "register_plan",
    "register_statistical_design",
]

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanError(PlanningError):
    """Base class for all plan registry errors."""


class DuplicatePlanVersionError(PlanError, ValueError):
    """Raised when a plan version is registered a second time (no clobbering)."""


class PlanNotFoundError(PlanError, ValueError):
    """Raised when reading a plan version that is not registered."""


class InvalidPlanVersionError(PlanError, ValueError):
    """Raised when a version is not ``v<N>`` or ``v<N>-draft``."""


class InvalidPlanIdError(PlanError, ValueError):
    """Raised when a plan id is not a safe single registry path segment."""


class GoalFamilyError(PlanningError):
    """Base class for goal-contract family registry errors."""


class DuplicateGoalError(GoalFamilyError, ValueError):
    """Raised when a ``goal_id`` is registered a second time."""


class GoalNotFoundError(GoalFamilyError, ValueError):
    """Raised when reading a goal id that is not registered."""


class DuplicateAcceptanceError(GoalFamilyError, ValueError):
    """Raised when an ``acceptance_id`` is registered a second time."""


class AcceptanceNotFoundError(GoalFamilyError, ValueError):
    """Raised when reading an acceptance id that is not registered."""


class DuplicateAnalysisProtocolError(GoalFamilyError, ValueError):
    """Raised when an ``analysis_id`` is registered a second time."""


class AnalysisProtocolNotFoundError(GoalFamilyError, ValueError):
    """Raised when reading an analysis protocol id that is not registered."""


class DuplicateClosureContractError(GoalFamilyError, ValueError):
    """Raised when a ``closure_id`` is registered a second time."""


class ClosureContractNotFoundError(GoalFamilyError, ValueError):
    """Raised when reading a closure contract id that is not registered."""


class DuplicateStatisticalDesignError(GoalFamilyError, ValueError):
    """Raised when a ``design_id`` is registered a second time."""


class StatisticalDesignNotFoundError(GoalFamilyError, ValueError):
    """Raised when reading a design id that is not registered."""


class InvalidRecordIdError(GoalFamilyError, ValueError):
    """Raised when a goal-family id is not a safe registry path segment."""


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Workspace directory holding the versioned plan records
#: (``templates/PROJECT-TREE.template.txt``).
PLANS_STATE_DIR: str = "plans"

#: Workspace directory holding the goal contract records
#: (``templates/PROJECT-TREE.template.txt``).
GOALS_STATE_DIR: str = "goals"

#: Workspace directory holding the acceptance criteria records
#: (created on demand; see the module docstring for the normative reading).
ACCEPTANCE_STATE_DIR: str = "acceptance"

#: Workspace directory holding the analysis protocol records
#: (``templates/PROJECT-TREE.template.txt``).
PROTOCOLS_STATE_DIR: str = "protocols"

#: Workspace directory holding the closure contract records
#: (created on demand; see the module docstring for the normative reading).
CLOSURE_STATE_DIR: str = "closure"

#: Workspace directory holding the statistical design records
#: (created on demand; see the module docstring for the normative reading).
DESIGNS_STATE_DIR: str = "designs"

#: Version of the plan-status (supersession) rule table. Bumped whenever a
#: rule changes; recorded in every assessment so old decisions stay
#: interpretable.
SUPERSEDED_RULESET_VERSION: str = "1.0"

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: Version syntax: ``v<N>`` (formal) or ``v<N>-draft`` (draft).
_VERSION_RE = re.compile(r"^v(?P<number>\d+)(?P<suffix>-draft)?$")

#: A user-supplied plan: the typed model or a schema-shaped dict.
PlanInput: TypeAlias = Plan | Mapping[str, Any]

#: A user-supplied goal contract: the typed model or a schema-shaped dict.
GoalInput: TypeAlias = GoalContract | Mapping[str, Any]

#: A user-supplied acceptance criteria record: the typed model or a dict.
AcceptanceInput: TypeAlias = AcceptanceCriteria | Mapping[str, Any]

#: A user-supplied analysis protocol record: the typed model or a dict.
AnalysisInput: TypeAlias = AnalysisProtocolOrResult | Mapping[str, Any]

#: A user-supplied closure contract record: the typed model or a dict.
ClosureInput: TypeAlias = ClosureContract | Mapping[str, Any]

#: A user-supplied statistical design record: the typed model or a dict.
StatisticalDesignInput: TypeAlias = StatisticalDesign | Mapping[str, Any]

_R = TypeVar("_R", bound=CoreModel)

# ---------------------------------------------------------------------------
# Plan version semantics (v<N> / v<N>-draft)
# ---------------------------------------------------------------------------


def is_draft_version(version: str) -> bool:
    """Return True iff ``version`` is a draft version (``v<N>-draft``).

    Raises:
        TypeError: ``version`` is not a str.
        InvalidPlanVersionError: ``version`` is not ``v<N>`` or
            ``v<N>-draft``.
    """
    _validate_version(version)
    return _VERSION_RE.fullmatch(version).group("suffix") is not None  # type: ignore[union-attr]


def is_formal_version(version: str) -> bool:
    """Return True iff ``version`` is a formal version (``v<N>``).

    Raises:
        TypeError: ``version`` is not a str.
        InvalidPlanVersionError: ``version`` is not ``v<N>`` or
            ``v<N>-draft``.
    """
    _validate_version(version)
    return not is_draft_version(version)


def formal_version(version: str) -> str:
    """Return the formal version of ``version`` (draft suffix stripped).

    ``"v1-draft"`` -> ``"v1"``; a formal version maps to itself. This is
    the version transition the freeze applies: freezing draft ``v<N>-draft``
    produces the frozen record at ``v<N>``.

    Raises:
        TypeError: ``version`` is not a str.
        InvalidPlanVersionError: ``version`` is not ``v<N>`` or
            ``v<N>-draft``.
    """
    _validate_version(version)
    return version.removesuffix("-draft")


def next_version(version: str) -> str:
    """Return the next formal version (``"v1"`` -> ``"v2"``).

    Revision semantics: a formal revision of a FROZEN plan creates the
    record at the next version number (AC-03). Draft versions are
    rejected: revision operates on frozen formal versions only.

    Raises:
        TypeError: ``version`` is not a str.
        InvalidPlanVersionError: ``version`` is not a formal ``v<N>``.
    """
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    match = _VERSION_RE.fullmatch(version)
    if match is None or match.group("suffix") is not None:
        raise InvalidPlanVersionError(
            f"invalid plan version {version!r}: next_version expects a formal"
            " version 'v<N>' (revision operates on frozen formal versions)"
        )
    return f"v{int(match.group('number')) + 1}"


def _validate_version(version: str) -> None:
    """Reject non-str and malformed version values with stable messages."""
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    if _VERSION_RE.fullmatch(version) is None:
        raise InvalidPlanVersionError(
            f"invalid plan version {version!r}: expected 'v<N>' or 'v<N>-draft'"
        )


def _version_sort_key(version: str) -> tuple[int, int]:
    """Sort key: version number first, draft before formal of the same number."""
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise InvalidPlanVersionError(
            f"invalid plan version {version!r}: expected 'v<N>' or 'v<N>-draft'"
        )
    return (int(match.group("number")), 0 if match.group("suffix") else 1)


# ---------------------------------------------------------------------------
# Effective plan status: the supersession rule table (AC-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStatusInput:
    """The state an effective plan status is a pure function of.

    Frozen and hashable so "same state -> same status" is directly
    testable. ``has_newer_version`` is True iff the plan lineage registry
    holds a later version of the plan (a higher number, or the formal
    record of the same number when the evaluated record is a draft).
    """

    stored_status: PlanStatus
    has_newer_version: bool


@dataclass(frozen=True)
class PlanStatusRule:
    """One entry of the ordered plan-status (supersession) rule table."""

    rule_id: str
    description: str
    status: PlanStatus
    predicate: Callable[[PlanStatusInput], bool]


@dataclass(frozen=True)
class PlanStatusDecision:
    """Record of one rule evaluation for a given state (auditability)."""

    rule_id: str
    description: str
    status: PlanStatus
    matched: bool


#: The ordered plan-status rule table. First match wins; order is
#: normative. Predicates are pure functions of the :class:`PlanStatusInput`
#: only. The stored record bytes are never rewritten: ``SUPERSEDED`` is a
#: computed lineage status, never a stored mutation (AC-03 -- revision
#: must never mutate the old record in place).
SUPERSEDED_RULES: tuple[PlanStatusRule, ...] = (
    PlanStatusRule(
        rule_id="R-SUP-D1",
        description=(
            "the stored status is DRAFT: working drafts stay DRAFT -- they"
            " are pre-freeze records and are never superseded"
        ),
        status=PlanStatus.DRAFT,
        predicate=lambda i: i.stored_status is PlanStatus.DRAFT,
    ),
    PlanStatusRule(
        rule_id="R-SUP-S1",
        description=(
            "the stored status is SUPERSEDED: already superseded"
            " (defensive -- this module never stores SUPERSEDED)"
        ),
        status=PlanStatus.SUPERSEDED,
        predicate=lambda i: i.stored_status is PlanStatus.SUPERSEDED,
    ),
    PlanStatusRule(
        rule_id="R-SUP-P1",
        description=(
            "the stored status is FROZEN and a newer version of the plan"
            " lineage is registered: the formal revision supersedes the old"
            " frozen record (AC-03)"
        ),
        status=PlanStatus.SUPERSEDED,
        predicate=lambda i: (
            i.stored_status is PlanStatus.FROZEN and i.has_newer_version
        ),
    ),
    PlanStatusRule(
        rule_id="R-SUP-F1",
        description=(
            "the stored status is FROZEN and no newer version is registered"
            " (default, total)"
        ),
        status=PlanStatus.FROZEN,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class PlanStatusAssessment:
    """Full, auditable result of an effective-status decision.

    ``input`` is the exact state the status was computed from;
    ``decisions`` records the outcome of every rule in the table (in
    evaluation order); ``matched_rule_id`` names the deciding rule (``None``
    is impossible: the final default rule always matches);
    ``ruleset_version`` records the rule table version (``SUPERSEDED_RULESET_VERSION``).
    """

    input: PlanStatusInput
    status: PlanStatus
    decisions: tuple[PlanStatusDecision, ...]
    matched_rule_id: str
    ruleset_version: str = SUPERSEDED_RULESET_VERSION


def evaluate_plan_status(
    stored_status: PlanStatus, has_newer_version: bool
) -> PlanStatusAssessment:
    """Decide the effective plan status with the ordered rule table.

    Pure and deterministic: the effective status is a pure function of the
    stored status and whether a newer version of the lineage is registered.
    ``DRAFT`` is never superseded (R-SUP-D1); a ``FROZEN`` record with a
    newer version registered is ``SUPERSEDED`` (R-SUP-P1); a ``FROZEN``
    record without one stays ``FROZEN`` (R-SUP-F1, the total default).

    Raises:
        TypeError: ``stored_status`` is not a ``PlanStatus``, or
            ``has_newer_version`` is not a bool.
    """
    if not isinstance(stored_status, PlanStatus):
        raise TypeError(
            f"stored_status must be a PlanStatus, got {type(stored_status).__name__}"
        )
    if not isinstance(has_newer_version, bool):
        raise TypeError(
            "has_newer_version must be a bool, got"
            f" {type(has_newer_version).__name__}"
        )
    audit_input = PlanStatusInput(
        stored_status=stored_status, has_newer_version=has_newer_version
    )
    decisions: list[PlanStatusDecision] = []
    matched_rule_id: str | None = None
    matched_status = PlanStatus.FROZEN  # unreachable default
    for rule in SUPERSEDED_RULES:
        matched = rule.predicate(audit_input)
        decisions.append(
            PlanStatusDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                status=rule.status,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_status = rule.status
    # R-SUP-F1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return PlanStatusAssessment(
        input=audit_input,
        status=matched_status,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# Plan v1 builder (determinism: same state -> same plan content)
# ---------------------------------------------------------------------------


def build_plan_v1(root: str | Path) -> Plan:
    """Construct the Plan v1 DRAFT record from the registered state.

    Pure and deterministic: the draft plan is a pure function of the
    registered state at ``root`` (project id + inventory items +
    requirements, ``planning/inventory.py``). The record carries:

    * ``plan_id`` -- derived deterministically from the project id
      (``core.ids.generate_id``); the plan lineage id is stable across
      versions;
    * ``version`` -- ``INITIAL_PLAN_VERSION`` (``"v1-draft"``,
      ``planning/init.py``);
    * ``status`` -- ``PlanStatus.DRAFT``;
    * ``inventory_audit`` -- the ``PlanInventoryAudit`` view of the
      completeness audit recomputed from the registered state
      (``planning/audit.py``, ``plan_inventory_audit()``): stored
      snapshots are never trusted;
    * ``goal_ids`` -- the sorted distinct goal ids the registered
      requirements map to (``05-GOAL-RUN-SCHEMA.md`` SS2);
    * ``requirement_ids`` -- the sorted registered requirement ids;
    * empty ``work_packages`` / ``resource_ids`` (the work-package DAG
      arrives with DEV-M4-G05).

    Nothing is persisted here: registration and the freeze flow
    (``planning/freeze.py``) own the writes. Same registered state ->
    identical plan content, on every call and in any workspace.

    Args:
        root: the initialized workspace root.

    Returns:
        The frozen :class:`Plan` DRAFT record (version ``"v1-draft"``).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored inventory or requirement record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    project = read_project_state(project_root)
    registry = load_inventory_registry(project_root)
    audit = evaluate_completeness_audit(registry.items, registry.requirements)
    goal_ids = sorted({goal_id for r in registry.requirements for goal_id in r.goal_ids})
    requirement_ids = sorted(r.requirement_id for r in registry.requirements)
    return Plan(
        plan_id=generate_id("plan", project.project_id),
        version=INITIAL_PLAN_VERSION,
        status=PlanStatus.DRAFT,
        inventory_audit=audit.plan_inventory_audit(),
        goal_ids=goal_ids,
        requirement_ids=requirement_ids,
        work_packages=[],
        resource_ids=[],
    )


# ---------------------------------------------------------------------------
# Plan registry (version-keyed, immutable-functional, no clobbering)
# ---------------------------------------------------------------------------


def register_plan(root: str | Path, plan: PlanInput) -> Plan:
    """Register one plan record at ``plans/<version>.json``.

    The record is schema-validated (``validate_and_reject`` ``"plan"``)
    and persisted as canonical JSON (``core.atomic.atomic_write``). The
    registry is immutable-functional: a version is written exactly once
    and a second registration is rejected with a stable
    ``DuplicatePlanVersionError`` (no clobbering) -- the storage that
    preserves old records across revision (AC-03). The version must be
    ``v<N>`` or ``v<N>-draft`` and the plan id a safe single path segment.

    Args:
        root: the initialized workspace root.
        plan: the plan as a typed ``Plan`` or a schema-shaped mapping
            (missing ``version`` defaults to ``INITIAL_PLAN_VERSION``,
            missing ``status`` to ``DRAFT``, missing lists to empty).

    Returns:
        The registered plan record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``plan`` is neither a
            ``Plan`` nor a mapping.
        ValueError: the plan is schema-invalid (subclass
            ``SchemaValidationError``) or a required field is missing.
        InvalidPlanVersionError: the version is not ``v<N>`` /
            ``v<N>-draft``.
        InvalidPlanIdError: the plan id is not a safe single path segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicatePlanVersionError: the version is already registered
            (stable message).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    plan_model = _coerce_plan(plan)
    _validate_plan_id(plan_model.plan_id)
    _validate_version(plan_model.version)
    state_path = _plan_path(project_root, plan_model.version)
    if state_path.is_file():
        raise DuplicatePlanVersionError(
            f"plan version {plan_model.version!r} is already registered; plan"
            " records are immutable and each version is written exactly once"
        )
    validate_and_reject("plan", plan_model.to_dict())
    atomic_write(state_path, _canonical_json(plan_model.to_dict()))
    return plan_model


def read_plan(root: str | Path, version: str) -> Plan:
    """Read one registered plan record as a typed model.

    The returned record is the exact stored record (bytes -> model): the
    stored file is never rewritten by revision, so this read is stable
    across ``revise_plan`` calls (AC-03: the old record is preserved
    untouched).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``version`` is not a str.
        InvalidPlanVersionError: ``version`` is not ``v<N>`` /
            ``v<N>-draft``.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        PlanNotFoundError: no record with that version is registered.
        ValueError: the stored record is corrupt (unparseable or not an
            object).
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_version(version)
    state_path = _plan_path(project_root, version)
    if not state_path.is_file():
        raise PlanNotFoundError(
            f"no plan with version {version!r} is registered at {project_root}"
        )
    return _read_record(state_path, Plan, "plan")


def list_plans(root: str | Path) -> tuple[Plan, ...]:
    """List every registered plan record, sorted by version (deterministic).

    Order: version number ascending, draft before formal of the same
    number (``"v1-draft"``, ``"v1"``, ``"v2"``, ...).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
        InvalidPlanVersionError: a stored record carries a malformed
            version.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    directory = project_root / PLANS_STATE_DIR
    if not directory.is_dir():
        return ()
    records = [_read_record(path, Plan, "plan") for path in directory.glob("*.json")]
    return tuple(sorted(records, key=lambda r: _version_sort_key(r.version)))


@dataclass(frozen=True)
class PlanLineageEntry:
    """One version of the plan lineage with its effective status.

    ``plan`` is the exact stored record; ``status`` is the recomputed
    effective status (``SUPERSEDED`` when a newer version exists,
    decided by the ``SUPERSEDED_RULES`` table); ``assessment`` records the
    rule trace. The stored record bytes are never rewritten (AC-03).
    """

    plan: Plan
    status: PlanStatus
    assessment: PlanStatusAssessment


def plan_lineage(root: str | Path) -> tuple[PlanLineageEntry, ...]:
    """Return every plan version with its recomputed effective status.

    The supersession view of the versioned registry: a stored FROZEN
    record is reported ``SUPERSEDED`` iff a newer version of the lineage
    is registered (the formal revision supersedes the old, AC-03), by the
    ``SUPERSEDED_RULES`` table -- the stored record itself is never
    rewritten. Drafts always report ``DRAFT`` (R-SUP-D1).

    Raises:
        TypeError: ``root`` is not a str/Path.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ValueError: a stored record is corrupt.
        InvalidPlanVersionError: a stored record carries a malformed
            version.
    """
    records = list_plans(root)
    entries: list[PlanLineageEntry] = []
    for record in records:
        has_newer = any(
            _version_sort_key(other.version) > _version_sort_key(record.version)
            for other in records
            if other is not record
        )
        assessment = evaluate_plan_status(record.status, has_newer)
        entries.append(
            PlanLineageEntry(plan=record, status=assessment.status, assessment=assessment)
        )
    return tuple(entries)


# ---------------------------------------------------------------------------
# Goal-contract family registries (draft authoring, immutable-functional)
# ---------------------------------------------------------------------------


def register_goal(root: str | Path, goal: GoalInput) -> GoalContract:
    """Register one goal contract draft at ``goals/<goal_id>.json``.

    The record is schema-validated (``validate_and_reject`` ``"goal"``)
    and persisted as canonical JSON. Registration is immutable-functional:
    a ``goal_id`` is registered exactly once
    (``DuplicateGoalError``). Drafts default to version
    ``INITIAL_PLAN_VERSION`` and ``frozen`` False (the pre-freeze state
    of ``examples/fdm-201/goal.example.yaml``). Cross-record references
    (acceptance/analysis/closure) are resolved by the plan freeze flow
    (``planning/freeze.py``), not at registration.

    Args:
        root: the initialized workspace root.
        goal: the goal contract as a typed ``GoalContract`` or a
            schema-shaped mapping.

    Returns:
        The registered goal contract record (what is persisted).

    Raises:
        TypeError: ``root`` is not a str/Path, or ``goal`` is neither a
            ``GoalContract`` nor a mapping.
        ValueError: the record is schema-invalid or a required field is
            missing.
        InvalidRecordIdError: the ``goal_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateGoalError: a goal with the same ``goal_id`` is already
            registered (stable message).
    """
    return _register_goal_family_record(
        root=root,
        state_dir=GOALS_STATE_DIR,
        schema_name="goal",
        kind_label="goal",
        value=goal,
        record_type=GoalContract,
        duplicate_error=DuplicateGoalError,
        default_version=INITIAL_PLAN_VERSION,
    )


def register_acceptance(
    root: str | Path, acceptance: AcceptanceInput
) -> AcceptanceCriteria:
    """Register one acceptance criteria draft at ``acceptance/<id>.json``.

    Same registration contract as :func:`register_goal`
    (``schemas/acceptance-criteria.schema.yaml``; the FDM-201 example
    ``examples/fdm-201/acceptance.example.yaml``). Duplicate ids raise
    ``DuplicateAcceptanceError``.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``acceptance`` is neither
            an ``AcceptanceCriteria`` nor a mapping.
        ValueError: the record is schema-invalid or a required field is
            missing.
        InvalidRecordIdError: the ``acceptance_id`` is not a safe single
            path segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateAcceptanceError: an acceptance with the same id is already
            registered (stable message).
    """
    return _register_goal_family_record(
        root=root,
        state_dir=ACCEPTANCE_STATE_DIR,
        schema_name="acceptance-criteria",
        kind_label="acceptance",
        value=acceptance,
        record_type=AcceptanceCriteria,
        duplicate_error=DuplicateAcceptanceError,
        default_version=INITIAL_PLAN_VERSION,
    )


def register_analysis_protocol(
    root: str | Path, analysis: AnalysisInput
) -> AnalysisProtocolOrResult:
    """Register one analysis protocol draft at ``protocols/<id>.json``.

    Same registration contract as :func:`register_goal`
    (``schemas/analysis.schema.yaml``; the version field is
    ``protocol_version``, defaulting to ``INITIAL_PLAN_VERSION`` for
    drafts). Duplicate ids raise ``DuplicateAnalysisProtocolError``.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``analysis`` is neither an
            ``AnalysisProtocolOrResult`` nor a mapping.
        ValueError: the record is schema-invalid or a required field is
            missing.
        InvalidRecordIdError: the ``analysis_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateAnalysisProtocolError: an analysis protocol with the same
            id is already registered (stable message).
    """
    return _register_goal_family_record(
        root=root,
        state_dir=PROTOCOLS_STATE_DIR,
        schema_name="analysis",
        kind_label="analysis protocol",
        value=analysis,
        record_type=AnalysisProtocolOrResult,
        duplicate_error=DuplicateAnalysisProtocolError,
        default_version=INITIAL_PLAN_VERSION,
    )


def register_closure_contract(
    root: str | Path, closure: ClosureInput
) -> ClosureContract:
    """Register one closure contract draft at ``closure/<id>.json``.

    Same registration contract as :func:`register_goal`
    (``schemas/closure-contract.schema.yaml``; the model carries no
    version field, so drafts default ``frozen`` to False only). Duplicate
    ids raise ``DuplicateClosureContractError``.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``closure`` is neither a
            ``ClosureContract`` nor a mapping.
        ValueError: the record is schema-invalid or a required field is
            missing.
        InvalidRecordIdError: the ``closure_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateClosureContractError: a closure contract with the same id
            is already registered (stable message).
    """
    return _register_goal_family_record(
        root=root,
        state_dir=CLOSURE_STATE_DIR,
        schema_name="closure-contract",
        kind_label="closure contract",
        value=closure,
        record_type=ClosureContract,
        duplicate_error=DuplicateClosureContractError,
        default_version=None,
    )


def register_statistical_design(
    root: str | Path, design: StatisticalDesignInput
) -> StatisticalDesign:
    """Register one statistical design draft at ``designs/<id>.json``.

    Same registration contract as :func:`register_goal`
    (``schemas/statistical-design.schema.yaml``; the first-class record
    behind ``AcceptanceCriteria.statistical_design_ref`` --
    07-STATISTICS-AND-ACCEPTANCE.md SS9 freezes the design BEFORE data
    generation). Duplicate ids raise ``DuplicateStatisticalDesignError``.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``design`` is neither a
            ``StatisticalDesign`` nor a mapping.
        ValueError: the record is schema-invalid or a required field is
            missing.
        InvalidRecordIdError: the ``design_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        DuplicateStatisticalDesignError: a design with the same id is
            already registered (stable message).
    """
    return _register_goal_family_record(
        root=root,
        state_dir=DESIGNS_STATE_DIR,
        schema_name="statistical-design",
        kind_label="statistical design",
        value=design,
        record_type=StatisticalDesign,
        duplicate_error=DuplicateStatisticalDesignError,
        default_version=INITIAL_PLAN_VERSION,
    )


def read_goal(root: str | Path, goal_id: str) -> GoalContract:
    """Read one registered goal contract record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``goal_id`` is not a str.
        InvalidRecordIdError: ``goal_id`` is not a safe single path segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        GoalNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt.
    """
    return _read_goal_family_record(root, GOALS_STATE_DIR, "goal", goal_id, GoalContract, GoalNotFoundError)


def read_acceptance(root: str | Path, acceptance_id: str) -> AcceptanceCriteria:
    """Read one registered acceptance record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``acceptance_id`` is not
            a str.
        InvalidRecordIdError: ``acceptance_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        AcceptanceNotFoundError: no record with that id is registered.
        ValueError: the stored record is corrupt.
    """
    return _read_goal_family_record(root, ACCEPTANCE_STATE_DIR, "acceptance", acceptance_id, AcceptanceCriteria, AcceptanceNotFoundError)


def read_analysis_protocol(
    root: str | Path, analysis_id: str
) -> AnalysisProtocolOrResult:
    """Read one registered analysis protocol record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``analysis_id`` is not a
            str.
        InvalidRecordIdError: ``analysis_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        AnalysisProtocolNotFoundError: no record with that id is
            registered.
        ValueError: the stored record is corrupt.
    """
    return _read_goal_family_record(root, PROTOCOLS_STATE_DIR, "analysis protocol", analysis_id, AnalysisProtocolOrResult, AnalysisProtocolNotFoundError)


def read_closure_contract(root: str | Path, closure_id: str) -> ClosureContract:
    """Read one registered closure contract record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``closure_id`` is not a
            str.
        InvalidRecordIdError: ``closure_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        ClosureContractNotFoundError: no record with that id is
            registered.
        ValueError: the stored record is corrupt.
    """
    return _read_goal_family_record(root, CLOSURE_STATE_DIR, "closure contract", closure_id, ClosureContract, ClosureContractNotFoundError)


def read_statistical_design(root: str | Path, design_id: str) -> StatisticalDesign:
    """Read one registered statistical design record as a typed model.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``design_id`` is not a
            str.
        InvalidRecordIdError: ``design_id`` is not a safe single path
            segment.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        StatisticalDesignNotFoundError: no record with that id is
            registered.
        ValueError: the stored record is corrupt.
    """
    return _read_goal_family_record(root, DESIGNS_STATE_DIR, "statistical design", design_id, StatisticalDesign, StatisticalDesignNotFoundError)


def list_goals(root: str | Path) -> tuple[GoalContract, ...]:
    """List every registered goal contract, sorted by id (deterministic)."""
    return _list_goal_family_records(root, GOALS_STATE_DIR, "goal", GoalContract)


def list_acceptance(root: str | Path) -> tuple[AcceptanceCriteria, ...]:
    """List every registered acceptance record, sorted by id (deterministic)."""
    return _list_goal_family_records(root, ACCEPTANCE_STATE_DIR, "acceptance", AcceptanceCriteria)


def list_analysis_protocols(root: str | Path) -> tuple[AnalysisProtocolOrResult, ...]:
    """List every registered analysis protocol, sorted by id (deterministic)."""
    return _list_goal_family_records(root, PROTOCOLS_STATE_DIR, "analysis protocol", AnalysisProtocolOrResult)


def list_closure_contracts(root: str | Path) -> tuple[ClosureContract, ...]:
    """List every registered closure contract, sorted by id (deterministic)."""
    return _list_goal_family_records(root, CLOSURE_STATE_DIR, "closure contract", ClosureContract)


def list_statistical_designs(root: str | Path) -> tuple[StatisticalDesign, ...]:
    """List every registered statistical design, sorted by id (deterministic)."""
    return _list_goal_family_records(root, DESIGNS_STATE_DIR, "statistical design", StatisticalDesign)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )


def _is_safe_registry_id(value: str) -> bool:
    """True iff ``value`` is a safe single registry path segment."""
    return (
        value not in ("", ".", "..")
        and "/" not in value
        and "\\" not in value
    )


def _validate_registry_id(kind: str, value: str) -> None:
    """Reject ids that would escape the registry directory as file names."""
    if not _is_safe_registry_id(value):
        raise InvalidRecordIdError(
            f"invalid {kind} id {value!r}: ids must be non-empty single path"
            " segments (no '/', no '\\', not '.' or '..')"
        )


def _validate_plan_id(value: str) -> None:
    """Reject plan ids that would escape the plans registry directory."""
    if not _is_safe_registry_id(value):
        raise InvalidPlanIdError(
            f"invalid plan id {value!r}: plan ids must be non-empty single"
            " path segments (no '/', no '\\', not '.' or '..')"
        )


def _coerce_plan(plan: PlanInput) -> Plan:
    """Return a typed plan from either input form."""
    if isinstance(plan, Plan):
        return plan
    if isinstance(plan, Mapping):
        data = dict(plan)
        data.setdefault("version", INITIAL_PLAN_VERSION)
        data.setdefault("status", PlanStatus.DRAFT.value)
        data.setdefault("goal_ids", [])
        data.setdefault("requirement_ids", [])
        data.setdefault("work_packages", [])
        data.setdefault("resource_ids", [])
        return Plan.from_dict(data)
    raise TypeError(
        f"plan must be a Plan or a mapping, got {type(plan).__name__}"
    )


def _coerce_goal_family_record(
    record_type: type[_R],
    value: CoreModel | Mapping[str, Any],
    name: str,
    default_version: str | None,
) -> _R:
    """Return a typed record from either input form (drafts get defaults)."""
    if isinstance(value, record_type):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        if default_version is not None:
            data.setdefault("version", default_version)
        data.setdefault("frozen", False)
        return record_type.from_dict(data)
    raise TypeError(
        f"{name} must be a {record_type.__name__} or a mapping, got"
        f" {type(value).__name__}"
    )


def _register_goal_family_record(
    *,
    root: str | Path,
    state_dir: str,
    schema_name: str,
    kind_label: str,
    value: CoreModel | Mapping[str, Any],
    record_type: type[_R],
    duplicate_error: type[GoalFamilyError],
    default_version: str | None,
) -> _R:
    """Shared registration for the goal-contract family registries."""
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    model = _coerce_goal_family_record(
        record_type, value, kind_label, default_version
    )
    record_id = _record_id(record_type, model)
    _validate_registry_id(kind_label, record_id)
    state_path = project_root / state_dir / f"{record_id}.json"
    if state_path.is_file():
        raise duplicate_error(
            f"{kind_label} {record_id!r} is already registered; records are"
            " immutable and each id is written exactly once"
        )
    validate_and_reject(schema_name, model.to_dict())
    atomic_write(state_path, _canonical_json(model.to_dict()))
    return model


def _record_id(record_type: type[_R], record: CoreModel) -> str:
    """Return the registry id field of a goal-family record."""
    if issubclass(record_type, GoalContract):
        return cast(GoalContract, record).goal_id
    if issubclass(record_type, AcceptanceCriteria):
        return cast(AcceptanceCriteria, record).acceptance_id
    if issubclass(record_type, AnalysisProtocolOrResult):
        return cast(AnalysisProtocolOrResult, record).analysis_id
    if issubclass(record_type, StatisticalDesign):
        return cast(StatisticalDesign, record).design_id
    return cast(ClosureContract, record).closure_id


def _read_goal_family_record(
    root: str | Path,
    state_dir: str,
    kind_label: str,
    record_id: str,
    record_type: type[_R],
    not_found_error: type[GoalFamilyError],
) -> _R:
    """Shared single-record read for the goal-contract family registries."""
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(record_id, str):
        raise TypeError(
            f"record id must be a str, got {type(record_id).__name__}"
        )
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _validate_registry_id(kind_label, record_id)
    state_path = project_root / state_dir / f"{record_id}.json"
    if not state_path.is_file():
        raise not_found_error(
            f"no {kind_label} with id {record_id!r} is registered at"
            f" {project_root}"
        )
    return _read_record(state_path, record_type, kind_label)


def _list_goal_family_records(
    root: str | Path, state_dir: str, kind_label: str, record_type: type[_R]
) -> tuple[_R, ...]:
    """Shared listing for the goal-contract family registries."""
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    project_root = Path(root).resolve()
    _require_initialized(project_root)
    directory = project_root / state_dir
    if not directory.is_dir():
        return ()
    records = [_read_record(path, record_type, kind_label) for path in directory.glob("*.json")]
    return tuple(sorted(records, key=lambda r: str(_record_id(record_type, r))))


def _plan_path(root: Path, version: str) -> Path:
    return root / PLANS_STATE_DIR / f"{version}.json"


def _canonical_json(data: dict[str, object]) -> str:
    """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(data, indent=_JSON_INDENT, sort_keys=True) + "\n"


def _read_record(path: Path, model: type[_R], kind: str) -> _R:
    """Load and type a record, rejecting corrupt state with a stable error."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"corrupt {kind} record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"corrupt {kind} record at {path}: expected a JSON object"
        )
    return model.from_dict(raw)
