"""Plan freeze and versioned revision (DEV-M4-G04, AC-01/02/03).

Implements the **freeze API** and **versioned revision API** deliverables
of DEV-M4-G04 over the ``planning/plan.py`` registry (DEV-M4-G04) and the
``planning/audit.py`` completeness audit (DEV-M4-G03), grounded in:

* ``01-PRODUCT-REQUIREMENTS.md`` SS5 step 8: "Plan v1 is audited and
  frozen" -- the freeze is the audit gate of the planning phase;
* ``14-STATE-GIT-ARTIFACTS.md`` SS5: the "plan.freeze" checkpoint marks
  the frozen plan (``CHECKPOINTS`` in ``audit/git.py``; the checkpoint
  commit itself is created by the Supervisor flow -- this module only
  records the pre-freeze ``git HEAD`` as ``frozen_commit``, it never
  writes Git state);
* ``core/models.py``: ``PlanStatus`` (DRAFT/FROZEN/SUPERSEDED)
  and the frozen goal-contract family (``GoalContract`` /
  ``AcceptanceCriteria`` / ``AnalysisProtocolOrResult`` /
  ``ClosureContract``).

AC-01 -- the audit gate
-----------------------
``freeze_plan`` is **prohibited** unless the freeze preconditions hold
and the completeness audit passes (freeze eligibility,
``planning/audit.py``). The preconditions (issue #137) are read from the
**registered state at freeze time**: the registered project phase has
reached ``REPRODUCTION_INVENTORY`` on the normative phase mainline
(``core/rules/lifecycle.py`` ``PROJECT_PHASE_MAINLINE`` ordering), and
the registered inventory holds at least one formally reported item.
The audit is always recomputed from the registered state at freeze time
(``audit_inventory_registry``); the embedded ``inventory_audit`` snapshot
of the draft is never trusted. A violated precondition or a failed gate
raises ``FreezeProhibitedError`` (naming the offending item ids for
audit failures -- unmapped or ambiguous formal items), with no record
written. The pure audit API keeps its vacuous-PASS acceptance on an
empty inventory: the non-empty precondition belongs to the freeze gate,
not to the audit rule table (``planning/audit.py`` is unchanged).

Since issue #140 the freeze also validates the submitted plan's
``goal_ids`` / ``requirement_ids`` against the registry in **both**
freshness branches: a registered draft's content is immutable
(``register_plan`` has no update API), so lists diverging from the
registry-derived content (the deterministic ``build_plan_v1``
derivation) are rejected with ``PlanStateMismatchError`` naming the
divergent fields -- a frozen plan can never persist lists that diverge
from the state its audit was recomputed from.

AC-02 -- frozen contracts
-------------------------
On success, ``freeze_plan`` produces the frozen ``Plan``
(``PlanStatus.FROZEN``, ``frozen_at``, ``frozen_commit`` = the pre-freeze
``git HEAD`` at ``root`` -- ``None`` when ``root`` is not a Git
repository, which is documented in the record) **and** the frozen
Goal/Acceptance/StatisticalDesign/Analysis/Closure contracts
(``PlanFreezeResult``): direct mutation of any frozen object is
rejected with ``FrozenInstanceError``. Both are **persisted**: the
frozen Plan record at ``plans/<version>.json`` and the frozen
goal-contract family in place at its registry paths (``goals/<id>.json``,
``acceptance/``, ``protocols/``, ``closure/`` -- ``frozen`` True, the
formal plan version, freeze metadata where the model declares it), so
any state reader (``read_goal`` / ``read_acceptance`` /
``read_analysis_protocol`` / ``read_closure_contract``) sees the same
frozen contract the freeze returned. The statistical designs are
already first-class registered records (frozen before data generation,
``07-STATISTICS-AND-ACCEPTANCE.md`` SS9) and are carried into the
result as-is. The public ``register_*`` API keeps its exactly-once
contract; the freeze -- and the revision that re-opens the family as
drafts of the next version (AC-03) -- are the documented transitions
that rewrite the records. No plan record is ever clobbered: the draft
is written when absent, tolerated when byte-equal, and a differing
record at the same version is rejected.

AC-03 -- versioned revision
---------------------------
``revise_plan`` creates the next plan version from a **registered,
frozen** plan: the new draft carries the incremented version
(``v1`` -> ``v2-draft``), ``parent_plan_version`` = the frozen version,
and a freshly recomputed ``inventory_audit``. Since issue #140 the plan
content is refreshed, not copied verbatim: ``goal_ids`` /
``requirement_ids`` are re-derived from the current registered state
(the deterministic ``build_plan_v1`` derivation -- the registry may have
grown since the frozen version was authored, and a v2 freeze must not
persist stale lists next to an audit recomputed from the grown state),
while the authored ``work_packages`` / ``resource_ids`` keep the frozen
plan's values as the revision baseline. The old record is **never
touched**: the stored file stays byte-identical and
``planning.plan.plan_lineage`` reports the old version as ``SUPERSEDED``
(via the versioned ``SUPERSEDED_RULES`` rule table) without any in-place
mutation -- supersession is a computed lineage status.

Determinism and boundaries
--------------------------
All checks and derived records are pure functions of the registered
state plus the injectable ``timestamp`` (naive datetimes rejected, like
``planning/init.py``). ``TypeError`` at the public boundaries; error
messages are stable. Errors follow the ``planning/plan.py`` convention
(``ValueError`` subclasses).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientific_reproduction.audit.git import NotARepositoryError, current_head
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisProtocolOrResult,
    ClosureContract,
    GoalContract,
    Plan,
    PlanStatus,
    ProjectPhase,
    StatisticalDesign,
)
from scientific_reproduction.core.rules.lifecycle import PROJECT_PHASE_MAINLINE
from scientific_reproduction.planning.audit import audit_inventory_registry
from scientific_reproduction.planning.init import PlanningError, read_project_state
from scientific_reproduction.planning.plan import (
    ACCEPTANCE_STATE_DIR,
    CLOSURE_STATE_DIR,
    GOALS_STATE_DIR,
    PLANS_STATE_DIR,
    PROTOCOLS_STATE_DIR,
    DuplicatePlanVersionError,
    InvalidPlanVersionError,
    _persist_goal_family_record,
    build_plan_v1,
    formal_version,
    is_draft_version,
    is_formal_version,
    list_acceptance,
    list_analysis_protocols,
    list_closure_contracts,
    list_goals,
    list_statistical_designs,
    next_version,
    read_plan,
    register_plan,
)

__all__ = [
    "FreezeError",
    "FreezeProhibitedError",
    "GoalFamilyNotDraftError",
    "PlanAlreadyFrozenError",
    "PlanFreezeResult",
    "PlanNotDraftError",
    "PlanNotFrozenError",
    "PlanStateMismatchError",
    "UnresolvedContractReferenceError",
    "freeze_plan",
    "revise_plan",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FreezeError(PlanningError):
    """Base class for all plan freeze and revision errors."""


class FreezeProhibitedError(FreezeError, ValueError):
    """Raised when a freeze precondition blocks the freeze (AC-01).

    The preconditions (issue #137) are: the registered project phase has
    reached ``REPRODUCTION_INVENTORY`` on the normative phase mainline,
    the registered inventory holds at least one formally reported item,
    and the completeness audit passes. For audit failures the message
    names the offending inventory item ids, and ``offending_item_ids``
    carries them structurally (deterministic, sorted by inventory id);
    phase/inventory precondition failures carry an empty tuple.
    """

    def __init__(
        self, message: str, offending_item_ids: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message)
        self.offending_item_ids: tuple[str, ...] = offending_item_ids


class PlanNotDraftError(FreezeError, ValueError):
    """Raised when the plan to freeze is not a DRAFT."""


class PlanStateMismatchError(FreezeError, ValueError):
    """Raised when the given plan is not the registered state's plan.

    Guards against stale plan objects: the plan must be the registered
    draft at its version (or the deterministic build of the current
    registered state when no draft is registered yet). Since issue #140
    the guard also validates the plan content in **both** branches:
    ``goal_ids`` / ``requirement_ids`` must equal the registry-derived
    content (the deterministic ``build_plan_v1`` derivation) -- a
    registered draft's content is immutable, so divergent lists are
    rejected with a message naming the divergent fields.
    """


class PlanAlreadyFrozenError(FreezeError, ValueError):
    """Raised when the formal version of the draft is already frozen."""


class PlanNotFrozenError(FreezeError, ValueError):
    """Raised when revising a plan that is not registered and FROZEN."""


class UnresolvedContractReferenceError(FreezeError, ValueError):
    """Raised when a goal-family reference cannot be resolved.

    Freezing requires every goal referenced by the plan to be registered
    and every registered goal's acceptance/analysis/closure references --
    and every acceptance's ``statistical_design_ref`` (the frozen
    statistical design, ``07-STATISTICS-AND-ACCEPTANCE.md`` SS9) -- to
    resolve to registered records (the goal-contract family is part of
    the frozen contract, ``01-PRODUCT-REQUIREMENTS.md`` SS5 step 7-8).
    """


class GoalFamilyNotDraftError(FreezeError, ValueError):
    """Raised when a goal-family record is already frozen at freeze time."""


# ---------------------------------------------------------------------------
# Freeze result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanFreezeResult:
    """The frozen contract of one freeze (AC-02).

    ``frozen_plan`` is the persisted frozen ``Plan`` record
    (``plans/<formal-version>.json``). ``goals`` / ``acceptance`` /
    ``statistical_designs`` / ``analysis_protocols`` /
    ``closure_contracts`` are the frozen goal-contract family variants
    produced from the registered drafts (version set to the frozen plan
    version, ``frozen`` True, freeze metadata attached where the model
    declares it) **and persisted in place** at their registry paths --
    any state reader sees the same frozen contracts. The statistical
    designs are already first-class registered records (frozen before
    data generation) and are carried into the result as-is. Every
    returned object is a frozen dataclass rejecting direct mutation.
    ``frozen_at`` / ``frozen_commit`` are the freeze stamp shared by all
    of them.
    """

    frozen_plan: Plan
    goals: tuple[GoalContract, ...]
    acceptance: tuple[AcceptanceCriteria, ...]
    statistical_designs: tuple[StatisticalDesign, ...]
    analysis_protocols: tuple[AnalysisProtocolOrResult, ...]
    closure_contracts: tuple[ClosureContract, ...]
    frozen_at: str
    frozen_commit: str | None


# ---------------------------------------------------------------------------
# Freeze (AC-01 gate + AC-02 frozen contracts)
# ---------------------------------------------------------------------------


def freeze_plan(
    root: str | Path,
    plan: Plan,
    *,
    timestamp: datetime | None = None,
) -> PlanFreezeResult:
    """Freeze the draft plan, gated by the freeze preconditions (AC-01).

    The freeze is **prohibited** unless the registered project phase has
    reached ``REPRODUCTION_INVENTORY`` on the normative phase mainline
    (``PROJECT_PHASE_MAINLINE`` ordering), the registered inventory holds
    at least one formally reported item, and the completeness audit
    evaluated from the registered state at freeze time passes
    (``FreezeProhibitedError`` naming the offending item ids for audit
    failures, no record written). The plan must be the DRAFT plan of the
    registered state
    (``PlanStateMismatchError`` otherwise): the registered draft at its
    version, or -- when no draft is registered yet -- the deterministic
    ``build_plan_v1`` of the current registered state (the draft is then
    written by the freeze). In both cases the submitted plan's
    ``goal_ids`` / ``requirement_ids`` must equal the registry-derived
    content (the deterministic ``build_plan_v1`` derivation -- issue
    #140): a registered draft's content is immutable (``register_plan``
    has no update API), so a draft whose lists diverged from the
    registered requirements is rejected, naming the divergent fields.
    The formal version must not be frozen yet
    (``PlanAlreadyFrozenError``); every goal referenced by the plan must
    be registered and every registered goal's acceptance/analysis/closure
    references -- and every acceptance's ``statistical_design_ref``
    (07-STATISTICS-AND-ACCEPTANCE.md SS9: the design is frozen before
    data generation) -- must resolve
    (``UnresolvedContractReferenceError``).

    On success, the frozen ``Plan`` (``PlanStatus.FROZEN``, ``frozen_at``,
    ``frozen_commit`` = pre-freeze ``git HEAD`` or ``None`` outside a Git
    repository) is persisted at ``plans/<formal-version>.json`` and the
    frozen goal-contract family is persisted in place at its registry
    paths and returned (:class:`PlanFreezeResult`); the draft is written
    when absent and never clobbered. The frozen analysis protocols also
    resolve through the analysis-subsystem versioned registry
    (``analysis.protocols.read_protocol_version`` -- and with it
    ``analysis.results.register_result``): its id-keyed fallback reads
    the goal-contract record at its stored ``protocol_version``, so a
    project that froze its protocols here needs no re-registration to
    register analysis results against the frozen versions. No Git commit
    is created here (the ``plan.freeze`` checkpoint is owned by the
    Supervisor flow, ``14-STATE-GIT-ARTIFACTS.md`` SS5).

    Args:
        root: the initialized workspace root.
        plan: the draft plan to freeze (a ``Plan`` built from the
            registered state; ``TypeError`` otherwise).
        timestamp: injectable freeze timestamp (defaults to now-UTC).
            Naive datetimes are rejected.

    Returns:
        The :class:`PlanFreezeResult` with the frozen plan and the frozen
        goal-contract family.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``plan`` is not a
            ``Plan``, or ``timestamp`` is not a datetime.
        ValueError: ``timestamp`` is naive.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        PlanNotDraftError: ``plan`` is not a DRAFT plan.
        InvalidPlanVersionError: ``plan.version`` is not a draft version
            (``v<N>-draft``).
        PlanStateMismatchError: ``plan`` is not the registered state's
            draft plan, or its ``goal_ids`` / ``requirement_ids`` diverge
            from the registry-derived content (the message names the
            divergent fields).
        PlanAlreadyFrozenError: the formal version is already frozen.
        FreezeProhibitedError: a freeze precondition fails (AC-01) --
            the registered project phase has not reached
            ``REPRODUCTION_INVENTORY`` on the normative phase mainline,
            the registered inventory holds no formally reported item, or
            the completeness audit fails (message names the offending
            item ids for audit failures).
        UnresolvedContractReferenceError: a goal-family reference is
            unresolvable.
        GoalFamilyNotDraftError: a goal-family record is already frozen.
        ValueError: a stored registry record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(plan, Plan):
        raise TypeError(f"plan must be a Plan, got {type(plan).__name__}")
    project_root = Path(root).resolve()
    resolved_timestamp = _resolve_timestamp(timestamp, name="timestamp")

    if plan.status is not PlanStatus.DRAFT:
        raise PlanNotDraftError(
            f"plan freeze requires a DRAFT plan, got status {plan.status.value!r}"
        )
    if not is_draft_version(plan.version):
        raise _freeze_expected_draft_version(plan.version)

    # Freshness (issue #140): the plan's goal_ids / requirement_ids must
    # match the registry in BOTH branches -- a registered draft's content
    # is immutable (register_plan has no update API), so a draft whose
    # lists diverged from the registered requirements must never freeze
    # next to an audit recomputed from the diverged state. The expected
    # content is the deterministic derivation of build_plan_v1.
    expected = build_plan_v1(project_root)
    divergent: list[str] = []
    if plan.goal_ids != expected.goal_ids:
        divergent.append("goal_ids")
    if plan.requirement_ids != expected.requirement_ids:
        divergent.append("requirement_ids")
    if divergent:
        raise PlanStateMismatchError(
            f"plan {plan.version!r} diverges from the registered state in"
            f" {', '.join(divergent)}; re-derive the plan content with"
            " build_plan_v1(root)"
        )

    # Freshness: the plan must be the registered draft at its version, or
    # (when none is registered yet) the deterministic build of the
    # current registered state. Either way it must match the state the
    # freeze reads -- stale plan objects are rejected.
    draft_path = project_root / PLANS_STATE_DIR / f"{plan.version}.json"
    if draft_path.is_file():
        if read_plan(project_root, plan.version) != plan:
            raise PlanStateMismatchError(
                f"plan {plan.version!r} is not the registered draft of the"
                " workspace; re-build it from the current registered state"
            )
    elif plan != expected:
        raise PlanStateMismatchError(
            f"plan {plan.version!r} does not match the deterministic build"
            " of the current registered state; re-build it with"
            " build_plan_v1(root)"
        )

    formal = formal_version(plan.version)
    if (project_root / PLANS_STATE_DIR / f"{formal}.json").is_file():
        raise PlanAlreadyFrozenError(
            f"plan version {formal!r} is already frozen; a formal plan"
            " version is written exactly once"
        )

    # Freeze preconditions (issue #137), read from the registered state
    # at freeze time -- stored snapshots are never trusted:
    # (a) the registered project phase has reached REPRODUCTION_INVENTORY
    # on the normative phase mainline (``PROJECT_PHASE_MAINLINE``
    # ordering -- the only phase ordering of the frozen state model);
    # (b) the registered inventory holds at least one formally reported
    # item (counts recomputed below by the audit from the registry).
    project = read_project_state(project_root)
    if not _phase_reaches_freeze_threshold(project.project_phase):
        raise FreezeProhibitedError(
            "plan freeze is prohibited: the registered project phase is"
            f" {project.project_phase.value!r}, which has not reached"
            f" {ProjectPhase.REPRODUCTION_INVENTORY.value!r} on the"
            " normative phase mainline; the reproduction inventory phase"
            " must be reached before the plan can be frozen"
        )

    # AC-01: the audit gate, recomputed from the registered state at
    # freeze time (stored inventory_audit snapshots are never trusted).
    audit = audit_inventory_registry(project_root)
    if audit.summary.formally_reported_items == 0:
        raise FreezeProhibitedError(
            "plan freeze is prohibited: the registered inventory contains"
            " no formally reported items; at least one formally reported"
            " inventory item is required before the plan can be frozen"
        )
    if not audit.freeze_eligible:
        raise FreezeProhibitedError(
            "plan freeze is prohibited until the completeness audit"
            " passes; offending inventory item ids:"
            f" {', '.join(audit.offending_item_ids)}",
            offending_item_ids=audit.offending_item_ids,
        )

    _verify_goal_family_closed(project_root, plan)

    # Persist the draft when absent (never clobber).
    if not draft_path.is_file():
        register_plan(project_root, plan)

    frozen_at = _format_iso(resolved_timestamp)
    frozen_commit = _resolve_frozen_commit(project_root)

    frozen_plan = replace(
        plan,
        version=formal,
        status=PlanStatus.FROZEN,
        inventory_audit=audit.plan_inventory_audit(),
        frozen_at=frozen_at,
        frozen_commit=frozen_commit,
    )
    register_plan(project_root, frozen_plan)

    return _frozen_goal_family(project_root, frozen_plan)


# ---------------------------------------------------------------------------
# Versioned revision (AC-03)
# ---------------------------------------------------------------------------


def revise_plan(root: str | Path, plan: Plan) -> Plan:
    """Revise a registered FROZEN plan into the next draft version (AC-03).

    The plan must be the **registered** frozen plan of the workspace
    (``PlanNotFoundError`` / ``PlanStateMismatchError`` /
    ``PlanNotFrozenError`` otherwise) and carry a formal version
    (``v<N>``; ``InvalidPlanVersionError`` otherwise). The revision

    * creates the next draft version (``v1`` -> ``v2-draft``) with
      ``parent_plan_version`` set to the frozen version;
    * re-derives ``goal_ids`` / ``requirement_ids`` from the current
      registered state (the deterministic ``build_plan_v1`` derivation,
      issue #140): the registry may have grown since the frozen version
      was authored, and a v2 freeze must not persist stale lists next to
      an audit recomputed from the grown state; the authored
      ``work_packages`` / ``resource_ids`` are copied from the frozen
      plan as the revision baseline;
    * recomputes ``inventory_audit`` from the registered state at revise
      time;
    * re-opens the registered goal-contract family as drafts of the next
      version (the frozen content as the authoring baseline, freeze
      metadata cleared) -- the next freeze re-freezes it (AC-02
      persistence keeps the on-disk family in step with the plan line);
    * writes the new draft record and leaves the old record **byte
      untouched** -- the old version is reported ``SUPERSEDED`` by
      ``planning.plan.plan_lineage`` (computed lineage status, never a
      stored mutation).

    No timestamp is taken: a revision produces a working DRAFT record
    (no freeze metadata); the subsequent freeze stamps it.

    Args:
        root: the initialized workspace root.
        plan: the registered FROZEN formal plan to revise (``TypeError``
            otherwise).

    Returns:
        The new draft ``Plan`` (version ``v<N+1>-draft``,
        ``PlanStatus.DRAFT``, ``parent_plan_version`` = the frozen
        version), persisted at ``plans/<new-version>.json``; the
        registered goal family is re-opened as drafts of the same
        version.

    Raises:
        TypeError: ``root`` is not a str/Path, or ``plan`` is not a
            ``Plan``.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        PlanNotFoundError: no record with the plan's version is
            registered.
        PlanStateMismatchError: ``plan`` is not the registered record of
            its version.
        PlanNotFrozenError: the registered plan is not FROZEN.
        InvalidPlanVersionError: ``plan.version`` is not a formal
            ``v<N>``.
        DuplicatePlanVersionError: the next version is already registered.
        ValueError: a stored registry record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(plan, Plan):
        raise TypeError(f"plan must be a Plan, got {type(plan).__name__}")
    project_root = Path(root).resolve()

    registered = read_plan(project_root, plan.version)
    if registered != plan:
        raise PlanStateMismatchError(
            f"plan {plan.version!r} is not the registered record of the"
            " workspace; re-read it with read_plan(root, version)"
        )
    if registered.status is not PlanStatus.FROZEN:
        raise PlanNotFrozenError(
            f"revision requires a FROZEN plan, got status"
            f" {registered.status.value!r} for version {plan.version!r}"
        )
    if not is_formal_version(plan.version):
        raise _revision_expected_formal_version(plan.version)

    next_draft = f"{next_version(plan.version)}-draft"
    if (project_root / PLANS_STATE_DIR / f"{next_draft}.json").is_file():
        raise DuplicatePlanVersionError(
            f"plan version {next_draft!r} is already registered; plan"
            " records are immutable and each version is written exactly once"
        )

    audit = audit_inventory_registry(project_root)
    # Issue #140: the plan content is re-derived from the current
    # registered state (the deterministic derivation of build_plan_v1),
    # not copied from the frozen plan -- the registry may have grown
    # since the frozen version was authored, and copying the frozen
    # lists would persist stale content that the next freeze rejects
    # (or worse, freezes next to an audit recomputed from the grown
    # state). The authored work_packages / resource_ids keep the frozen
    # plan's values as the revision baseline.
    content = build_plan_v1(project_root)
    new_draft = Plan(
        plan_id=plan.plan_id,
        version=next_draft,
        status=PlanStatus.DRAFT,
        inventory_audit=audit.plan_inventory_audit(),
        goal_ids=list(content.goal_ids),
        requirement_ids=list(content.requirement_ids),
        parent_plan_version=plan.version,
        work_packages=[dict(wp) for wp in plan.work_packages],
        resource_ids=list(plan.resource_ids),
    )
    registered = register_plan(project_root, new_draft)
    _reopen_goal_family_drafts(project_root, next_draft)
    return registered


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


#: Mainline rank of every normative project phase: the declaration order
#: of ``core/rules/lifecycle.py`` ``PROJECT_PHASE_MAINLINE`` -- the only
#: phase ordering of the frozen state model. The ``StrEnum`` lexicographic
#: order is NOT the phase order (e.g. ``PLANNING`` < ``REPRODUCTION_INVENTORY``
#: lexicographically, but follows it on the mainline).
_PHASE_MAINLINE_RANK: dict[ProjectPhase, int] = {
    phase: rank for rank, phase in enumerate(PROJECT_PHASE_MAINLINE)
}


def _phase_reaches_freeze_threshold(phase: ProjectPhase) -> bool:
    """True iff ``phase`` is at or beyond ``REPRODUCTION_INVENTORY``.

    Off-mainline phases (``PAUSED`` / ``WAITING_*`` / ``REPLANNING``)
    carry no mainline rank and never reach the threshold: a suspended or
    replanning workspace cannot freeze.
    """
    return _PHASE_MAINLINE_RANK.get(phase, -1) >= _PHASE_MAINLINE_RANK[
        ProjectPhase.REPRODUCTION_INVENTORY
    ]


def _resolve_timestamp(timestamp: datetime | None, *, name: str) -> datetime:
    """Return the injectable timestamp (default now-UTC); reject naive."""
    if timestamp is None:
        return datetime.now(timezone.utc)
    if not isinstance(timestamp, datetime):
        raise TypeError(f"{name} must be a datetime, got {type(timestamp).__name__}")
    if timestamp.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return timestamp


def _format_iso(value: datetime) -> str:
    """Format a timezone-aware datetime as git-style UTC ISO-8601 (``Z``)."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_frozen_commit(project_root: Path) -> str | None:
    """Return the pre-freeze ``git HEAD``, or None outside a Git repo.

    The ``plan.freeze`` checkpoint commit itself is created by the
    Supervisor flow (``audit/git.py`` CHECKPOINTS); this module only
    records the commit the freeze is based on. Outside a Git repository
    the field is ``None`` -- documented in the record (no fabrication).
    """
    try:
        return current_head(project_root)
    except NotARepositoryError:
        return None


def _verify_goal_family_closed(project_root: Path, plan: Plan) -> None:
    """Verify the goal-contract family is closed before freezing.

    Every goal id the plan references must have a registered goal
    contract, and every registered goal's acceptance criteria,
    analysis protocol and (optional) closure contract references must
    resolve to registered records -- the frozen contract is the whole
    family (``01-PRODUCT-REQUIREMENTS.md`` SS5 steps 7-8). Every
    acceptance's ``statistical_design_ref`` must resolve to a registered
    statistical design record: the design is frozen BEFORE data
    generation (``07-STATISTICS-AND-ACCEPTANCE.md`` SS9). A registered
    goal-family record that is already frozen blocks the freeze
    (``GoalFamilyNotDraftError``): the family must be frozen *by* the
    plan freeze, not before it.
    """
    goals = list_goals(project_root)
    registered_goal_ids = {g.goal_id for g in goals}
    missing_plan_goals = [gid for gid in plan.goal_ids if gid not in registered_goal_ids]
    if missing_plan_goals:
        raise UnresolvedContractReferenceError(
            "plan references goal contract(s) that are not registered:"
            f" {', '.join(sorted(missing_plan_goals))}"
        )

    acceptance = list_acceptance(project_root)
    designs = list_statistical_designs(project_root)
    analysis = list_analysis_protocols(project_root)
    closure = list_closure_contracts(project_root)
    acceptance_ids = {a.acceptance_id for a in acceptance}
    design_ids = {d.design_id for d in designs}
    analysis_ids = {a.analysis_id for a in analysis}
    closure_ids = {c.closure_id for c in closure}

    for goal in goals:
        refs = (
            (f"acceptance criteria {goal.acceptance.criteria_ref!r}", goal.acceptance.criteria_ref, acceptance_ids),
            (f"analysis protocol {goal.analysis_protocol_ref!r}", goal.analysis_protocol_ref, analysis_ids),
        )
        for label, ref_id, registered_ids in refs:
            if ref_id not in registered_ids:
                raise UnresolvedContractReferenceError(
                    f"goal contract {goal.goal_id!r} references {label} which is"
                    " not registered"
                )
        if (
            goal.closure_contract_ref is not None
            and goal.closure_contract_ref not in closure_ids
        ):
            raise UnresolvedContractReferenceError(
                f"goal contract {goal.goal_id!r} references closure contract"
                f" {goal.closure_contract_ref!r} which is not registered"
            )

    # The statistical design is frozen BEFORE data generation
    # (07-STATISTICS-AND-ACCEPTANCE.md SS9): every acceptance's
    # statistical_design_ref must resolve to a registered design record.
    for acceptance_record in acceptance:
        design_ref = acceptance_record.statistical_design_ref
        if design_ref is not None and design_ref not in design_ids:
            raise UnresolvedContractReferenceError(
                f"acceptance criteria {acceptance_record.acceptance_id!r} references"
                f" statistical design {design_ref!r} which is not registered"
            )

    for record in (
        *goals,
        *acceptance,
        *designs,
        *analysis,
        *closure,
    ):
        if getattr(record, "frozen", False):
            kind, record_id = _goal_family_kind_and_id(record)
            raise GoalFamilyNotDraftError(
                f"{kind} {record_id!r} is already frozen; the goal-contract"
                " family must be frozen by the plan freeze"
            )


def _goal_family_kind_and_id(record: Any) -> tuple[str, str]:
    """Human label and registry id of a goal-family record."""
    if isinstance(record, GoalContract):
        return "goal contract", record.goal_id
    if isinstance(record, AcceptanceCriteria):
        return "acceptance criteria", record.acceptance_id
    if isinstance(record, AnalysisProtocolOrResult):
        return "analysis protocol", record.analysis_id
    if isinstance(record, StatisticalDesign):
        return "statistical design", record.design_id
    return "closure contract", record.closure_id


def _frozen_goal_family(
    project_root: Path, frozen_plan: Plan
) -> PlanFreezeResult:
    """Build and persist the frozen goal-contract family (AC-02).

    Every registered draft is replaced **in place** by its frozen
    variant: the plan's formal version (``protocol_version`` for
    analysis protocols -- the model's version field), ``frozen`` True,
    and the freeze stamp where the model declares those fields
    (``GoalContract.frozen_at`` / ``frozen_commit``; acceptance,
    statistical-design and analysis models carry no
    ``frozen_at``/``frozen_commit``, ``ClosureContract`` carries no
    version fields at all -- see ``core/models.py``). After the freeze,
    any state reader (``read_goal`` / ``read_acceptance`` /
    ``read_analysis_protocol`` / ``read_closure_contract``) sees the
    frozen contract; the public ``register_*`` API keeps its
    exactly-once contract (the freeze and the revision that re-opens the
    family are the documented transitions that rewrite the records). The
    statistical designs are already first-class registered records
    (frozen before data generation) and are carried into the result
    as-is.
    """
    version = frozen_plan.version
    frozen_at = frozen_plan.frozen_at or ""
    frozen_commit = frozen_plan.frozen_commit

    goals = tuple(
        replace(
            g,
            version=version,
            frozen=True,
            frozen_at=frozen_at,
            frozen_commit=frozen_commit,
            acceptance=replace(g.acceptance, frozen=True),
        )
        for g in list_goals(project_root)
    )
    acceptance = tuple(
        replace(a, version=version, frozen=True)
        for a in list_acceptance(project_root)
    )
    designs = tuple(
        replace(d, version=version, frozen=True)
        for d in list_statistical_designs(project_root)
    )
    analysis = tuple(
        replace(a, protocol_version=version, frozen=True)
        for a in list_analysis_protocols(project_root)
    )
    closure = tuple(
        replace(c, frozen=True) for c in list_closure_contracts(project_root)
    )
    _persist_goal_family(project_root, goals, acceptance, analysis, closure)
    return PlanFreezeResult(
        frozen_plan=frozen_plan,
        goals=goals,
        acceptance=acceptance,
        statistical_designs=designs,
        analysis_protocols=analysis,
        closure_contracts=closure,
        frozen_at=frozen_at,
        frozen_commit=frozen_commit,
    )


def _reopen_goal_family_drafts(project_root: Path, version: str) -> None:
    """Re-open the registered goal-contract family as drafts (AC-03).

    Revision returns the family to the authoring state of the next
    version: every registered record is replaced **in place** by its
    draft variant -- the frozen content as the revision baseline,
    ``version`` / ``protocol_version`` set to the next draft version,
    ``frozen`` False, freeze metadata cleared -- mirroring the plan
    revision, which copies the frozen plan's content into the next
    draft. The family must be frozen again by the next freeze (AC-01
    keeps requiring drafts at freeze time, ``GoalFamilyNotDraftError``).
    """
    goals = tuple(
        replace(
            g,
            version=version,
            frozen=False,
            frozen_at=None,
            frozen_commit=None,
            acceptance=replace(g.acceptance, frozen=False),
        )
        for g in list_goals(project_root)
    )
    acceptance = tuple(
        replace(a, version=version, frozen=False)
        for a in list_acceptance(project_root)
    )
    analysis = tuple(
        replace(a, protocol_version=version, frozen=False)
        for a in list_analysis_protocols(project_root)
    )
    closure = tuple(
        replace(c, frozen=False) for c in list_closure_contracts(project_root)
    )
    _persist_goal_family(project_root, goals, acceptance, analysis, closure)


def _persist_goal_family(
    project_root: Path,
    goals: tuple[GoalContract, ...],
    acceptance: tuple[AcceptanceCriteria, ...],
    analysis: tuple[AnalysisProtocolOrResult, ...],
    closure: tuple[ClosureContract, ...],
) -> None:
    """Persist the goal-family records in place at their registry paths."""
    for goal in goals:
        _persist_goal_family_record(
            root=project_root,
            state_dir=GOALS_STATE_DIR,
            schema_name="goal",
            kind_label="goal",
            record=goal,
            record_type=GoalContract,
        )
    for criterion in acceptance:
        _persist_goal_family_record(
            root=project_root,
            state_dir=ACCEPTANCE_STATE_DIR,
            schema_name="acceptance-criteria",
            kind_label="acceptance",
            record=criterion,
            record_type=AcceptanceCriteria,
        )
    for protocol in analysis:
        _persist_goal_family_record(
            root=project_root,
            state_dir=PROTOCOLS_STATE_DIR,
            schema_name="analysis",
            kind_label="analysis protocol",
            record=protocol,
            record_type=AnalysisProtocolOrResult,
        )
    for contract in closure:
        _persist_goal_family_record(
            root=project_root,
            state_dir=CLOSURE_STATE_DIR,
            schema_name="closure-contract",
            kind_label="closure contract",
            record=contract,
            record_type=ClosureContract,
        )


def _freeze_expected_draft_version(version: str) -> InvalidPlanVersionError:
    return InvalidPlanVersionError(
        f"plan freeze expects a draft version 'v<N>-draft', got {version!r}"
    )


def _revision_expected_formal_version(version: str) -> InvalidPlanVersionError:
    return InvalidPlanVersionError(
        "revision expects a formal frozen version 'v<N>', got"
        f" {version!r}"
    )
