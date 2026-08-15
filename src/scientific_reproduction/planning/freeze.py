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
* ``core/models.py``: ``PlanStatus`` (DRAFT/UNDER_AUDIT/FROZEN/SUPERSEDED)
  and the frozen goal-contract family (``GoalContract`` /
  ``AcceptanceCriteria`` / ``AnalysisProtocolOrResult`` /
  ``ClosureContract``).

AC-01 -- the audit gate
-----------------------
``freeze_plan`` is **prohibited** unless the completeness audit passes
(freeze eligibility, ``planning/audit.py``). The audit is always
recomputed from the **registered state at freeze time**
(``audit_inventory_registry``); the embedded ``inventory_audit`` snapshot
of the draft is never trusted. A failed gate raises
``FreezeProhibitedError`` naming the offending item ids (unmapped or
ambiguous formal items), with no record written.

AC-02 -- frozen contracts
-------------------------
On success, ``freeze_plan`` produces the frozen ``Plan``
(``PlanStatus.FROZEN``, ``frozen_at``, ``frozen_commit`` = the pre-freeze
``git HEAD`` at ``root`` -- ``None`` when ``root`` is not a Git
repository, which is documented in the record) **and** the frozen
Goal/Acceptance/Analysis/Closure contracts (``PlanFreezeResult``):
direct mutation of any frozen object is rejected with
``FrozenInstanceError``. Both are **persisted**: the frozen Plan record
at ``plans/<version>.json`` and the frozen goal-contract family in
place at its registry paths (``goals/<id>.json``, ``acceptance/``,
``protocols/``, ``closure/`` -- ``frozen`` True, the formal plan
version, freeze metadata where the model declares it), so any state
reader (``read_goal`` / ``read_acceptance`` /
``read_analysis_protocol`` / ``read_closure_contract``) sees the same
frozen contract the freeze returned. The public ``register_*`` API
keeps its exactly-once contract; the freeze -- and the revision that
re-opens the family as drafts of the next version (AC-03) -- are the
documented transitions that rewrite the records. No plan record is ever
clobbered: the draft is written when absent, tolerated when byte-equal,
and a differing record at the same version is rejected.

AC-03 -- versioned revision
---------------------------
``revise_plan`` creates the next plan version from a **registered,
frozen** plan: the new draft carries the incremented version
(``v1`` -> ``v2-draft``), ``parent_plan_version`` = the frozen version,
the frozen plan's content (goal_ids, requirement_ids, work_packages,
resource_ids) and a freshly recomputed ``inventory_audit``. The old
record is **never touched**: the stored file stays byte-identical and
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
)
from scientific_reproduction.planning.audit import audit_inventory_registry
from scientific_reproduction.planning.init import PlanningError
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
    """Raised when the completeness audit blocks the freeze (AC-01).

    The message names the offending inventory item ids, and
    ``offending_item_ids`` carries them structurally (deterministic,
    sorted by inventory id).
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
    registered state when no draft is registered yet).
    """


class PlanAlreadyFrozenError(FreezeError, ValueError):
    """Raised when the formal version of the draft is already frozen."""


class PlanNotFrozenError(FreezeError, ValueError):
    """Raised when revising a plan that is not registered and FROZEN."""


class UnresolvedContractReferenceError(FreezeError, ValueError):
    """Raised when a goal-family reference cannot be resolved.

    Freezing requires every goal referenced by the plan to be registered
    and every registered goal's acceptance/analysis/closure references to
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
    ``analysis_protocols`` / ``closure_contracts`` are the frozen
    goal-contract family variants produced from the registered drafts
    (version set to the frozen plan version, ``frozen`` True, freeze
    metadata attached where the model declares it) **and persisted in
    place** at their registry paths -- any state reader sees the same
    frozen contracts. Every returned object is a frozen dataclass
    rejecting direct mutation. ``frozen_at`` / ``frozen_commit`` are the
    freeze stamp shared by all of them.
    """

    frozen_plan: Plan
    goals: tuple[GoalContract, ...]
    acceptance: tuple[AcceptanceCriteria, ...]
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
    """Freeze the draft plan, gated by the completeness audit (AC-01).

    The freeze is **prohibited** unless the completeness audit evaluated
    from the registered state at freeze time passes
    (``FreezeProhibitedError`` naming the offending item ids, no record
    written). The plan must be the DRAFT plan of the registered state
    (``PlanStateMismatchError`` otherwise): the registered draft at its
    version, or -- when no draft is registered yet -- the deterministic
    ``build_plan_v1`` of the current registered state (the draft is then
    written by the freeze). The formal version must not be frozen yet
    (``PlanAlreadyFrozenError``); every goal referenced by the plan must
    be registered and every registered goal's acceptance/analysis/closure
    references must resolve (``UnresolvedContractReferenceError``).

    On success, the frozen ``Plan`` (``PlanStatus.FROZEN``, ``frozen_at``,
    ``frozen_commit`` = pre-freeze ``git HEAD`` or ``None`` outside a Git
    repository) is persisted at ``plans/<formal-version>.json`` and the
    frozen goal-contract family is persisted in place at its registry
    paths and returned (:class:`PlanFreezeResult`); the draft is written
    when absent and never clobbered. No Git commit is created here (the
    ``plan.freeze`` checkpoint is owned by the Supervisor flow,
    ``14-STATE-GIT-ARTIFACTS.md`` SS5).

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
            draft plan.
        PlanAlreadyFrozenError: the formal version is already frozen.
        FreezeProhibitedError: the completeness audit fails (AC-01);
            message names the offending item ids.
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
    elif plan != build_plan_v1(project_root):
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

    # AC-01: the audit gate, recomputed from the registered state at
    # freeze time (stored inventory_audit snapshots are never trusted).
    audit = audit_inventory_registry(project_root)
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
    * copies the frozen plan's content (goal_ids, requirement_ids,
      work_packages, resource_ids) as the revision baseline;
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
    new_draft = Plan(
        plan_id=plan.plan_id,
        version=next_draft,
        status=PlanStatus.DRAFT,
        inventory_audit=audit.plan_inventory_audit(),
        goal_ids=list(plan.goal_ids),
        requirement_ids=list(plan.requirement_ids),
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
    family (``01-PRODUCT-REQUIREMENTS.md`` SS5 steps 7-8). A registered
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
    analysis = list_analysis_protocols(project_root)
    closure = list_closure_contracts(project_root)
    acceptance_ids = {a.acceptance_id for a in acceptance}
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

    for record in (
        *goals,
        *acceptance,
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
    return "closure contract", record.closure_id


def _frozen_goal_family(
    project_root: Path, frozen_plan: Plan
) -> PlanFreezeResult:
    """Build and persist the frozen goal-contract family (AC-02).

    Every registered draft is replaced **in place** by its frozen
    variant: the plan's formal version (``protocol_version`` for
    analysis protocols -- the model's version field), ``frozen`` True,
    and the freeze stamp where the model declares those fields
    (``GoalContract.frozen_at`` / ``frozen_commit``; acceptance and
    analysis models carry no ``frozen_at``/``frozen_commit``,
    ``ClosureContract`` carries no version fields at all -- see
    ``core/models.py``). After the freeze, any state reader
    (``read_goal`` / ``read_acceptance`` / ``read_analysis_protocol`` /
    ``read_closure_contract``) sees the frozen contract; the public
    ``register_*`` API keeps its exactly-once contract (the freeze and
    the revision that re-opens the family are the documented
    transitions that rewrite the records).
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
