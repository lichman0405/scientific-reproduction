"""Shared test helpers for the workers/context tests (DEV-M6-G01).

``IDENTITY`` / ``TIMESTAMP`` pin every deterministic input the backing
``initialize_project`` call takes, and ``FROZEN_AT`` pins the plan freeze
stamp, so the tests exercise the deterministic path: same inputs in, same
frozen contracts and same context package out -- no wall clock anywhere.
The frozen Goal Contract is produced by the real plan freeze flow
(``planning.freeze.freeze_plan``), i.e. the exact M4-G04/M4-G05 semantics
the context generator requires.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    AutomaticRetryPolicy,
    AvailabilityState,
    ClaimSpecificEvidence,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    Confidence,
    Criticality,
    DecisionMode,
    DependencyType,
    EvidenceAssessment,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    InventoryItemType,
    MappingStatus,
    PrimaryOrExploratory,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    ResearchSource,
    Resource,
    ResourceType,
    SourceType,
    WorkerRole,
)
from scientific_reproduction.planning.freeze import PlanFreezeResult, freeze_plan
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    build_plan_v1,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
)
from scientific_reproduction.planning.resources import register_resource

#: Deterministic author/committer identity used by every context test.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed plan freeze timestamp: the frozen contracts are deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects
#: (``17-FDM201-REFERENCE-CASE.md``).
DOI = "10.1039/D5TA00771B"

#: The context test's worker role.
ROLE = WorkerRole.EXPERIMENT_WORKER


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


def make_goal(
    goal_id: str,
    *,
    dependencies: tuple[GoalDependency, ...] = (),
    outputs: tuple[object, ...] = (),
    resource_ids: tuple[str, ...] = (),
    requirement_ids: tuple[str, ...] = ("REQ-1",),
    retry_policy_ref: str | None = "RETRY-ENGINEERING-DEFAULT",
    analysis_id: str = "ANP-1",
) -> GoalContract:
    """Build a schema-valid draft goal contract (version ``v1-draft``)."""
    return GoalContract(
        goal_id=goal_id,
        title=f"Reproduce the reported isotherm ({goal_id}).",
        unit_process_type="gas_adsorption_isotherm",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective="Reproduce the formally reported isotherm dataset.",
        requirement_ids=list(requirement_ids),
        dependencies=list(dependencies),
        outputs=list(outputs),
        resource_ids=list(resource_ids),
        acceptance=GoalAcceptance(criteria_ref="ACC-1", frozen=False),
        analysis_protocol_ref=analysis_id,
        replication=GoalReplication(
            independent_required=False, planned_n_policy="single"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref="CLC-1",
        automatic_retry_policy_ref=retry_policy_ref,
    )


def make_acceptance() -> AcceptanceCriteria:
    """Build a schema-valid draft acceptance record (version ``v1-draft``)."""
    return AcceptanceCriteria(
        acceptance_id="ACC-1",
        goal_id="GOAL-1",
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "batch_level_uptake", "rule": "equivalence_interval"}],
        target={
            "metric": "uptake_at_defined_pressure",
            "published_seed_value_cm3_g": 180.5,
        },
        confidence=Confidence.LOW,
    )


def make_analysis_protocol(analysis_id: str = "ANP-1") -> AnalysisProtocolOrResult:
    """Build a schema-valid draft analysis protocol (version ``v1-draft``)."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=AnalysisKind.PROTOCOL,
        protocol_version=INITIAL_PLAN_VERSION,
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=False,
        methods=[{"name": "isotherm_fit"}],
    )


def make_closure() -> ClosureContract:
    """Build a schema-valid draft closure contract."""
    return ClosureContract(
        closure_id="CLC-1",
        frozen=False,
        statistical_sufficiency={"min_valid_n": 3},
        execution_validity={"verified": True},
        diagnosis={"tolerances": {}},
        recovery=ClosureRecovery(),
        literature=ClosureLiterature(),
    )


def make_resource(resource_id: str = "RES-1") -> Resource:
    """Build a frozen Resource with compact defaults."""
    return Resource(
        resource_id=resource_id,
        name=f"resource {resource_id}",
        resource_type=ResourceType.REAGENT,
        availability_state=AvailabilityState.AVAILABLE,
    )


def make_source(
    source_id: str,
    *,
    source_type: SourceType = SourceType.TARGET_PAPER,
) -> ResearchSource:
    """Build a frozen ResearchSource with compact defaults."""
    return ResearchSource(
        source_id=source_id,
        source_type=source_type,
        title=f"Reported source {source_id}",
        provenance="registered fixture",
        doi="10.1039/D5TA00771B",
    )


def make_evidence(
    evidence_id: str,
    source_id: str,
    *,
    used_by: tuple[str, ...] = (),
    claim_id: str = "CLAIM-1",
) -> ClaimSpecificEvidence:
    """Build a frozen ClaimSpecificEvidence record with compact defaults."""
    return ClaimSpecificEvidence(
        evidence_id=evidence_id,
        source_id=source_id,
        claim_id=claim_id,
        finding=f"Evidence finding of {evidence_id}.",
        assessment=EvidenceAssessment(
            authority=3,
            reliability=3,
            directness=2,
            reliability_checklist_ref="RELIABILITY-CHECKLIST-1",
            ranking_score=0.8,
        ),
        used_by=list(used_by),
    )


def make_retry_policy(
    policy_id: str = "RETRY-ENGINEERING-DEFAULT",
) -> AutomaticRetryPolicy:
    """Build a frozen AutomaticRetryPolicy with compact defaults."""
    return AutomaticRetryPolicy(
        policy_id=policy_id,
        allowed_engineering_failures=["instrument_drift", "power_cycle"],
        supervisor_required_changes=["protocol_deviation"],
        max_identical_retries=2,
        invalidate_run_on=["sample_loss"],
    )


def make_item(
    inventory_id: str, *, requirement_ids: tuple[str, ...] = ()
) -> ReproductionInventoryItem:
    """Build a frozen ReproductionInventoryItem with compact defaults."""
    return ReproductionInventoryItem(
        inventory_id=inventory_id,
        source_id="SRC-TARGET-PAPER",
        item_type=InventoryItemType.EXPERIMENT,
        formal_report=True,
        description="Single-component C3H6 adsorption isotherm for FDM-201 at 298 K.",
        source_location="main adsorption figure, 'Adsorption isotherms' section",
        mapping_status=MappingStatus.UNMAPPED,
        requirement_ids=list(requirement_ids),
    )


def make_requirement(
    requirement_id: str,
    *,
    goal_ids: tuple[str, ...],
    inventory_items: tuple[str, ...] = ("ITEM-1",),
) -> ReproductionRequirement:
    """Build a frozen ReproductionRequirement with compact defaults."""
    return ReproductionRequirement(
        requirement_id=requirement_id,
        statement="Reproduce the reported single-component adsorption isotherm.",
        inventory_items=list(inventory_items),
        criticality=Criticality.REQUIRED,
        goal_ids=list(goal_ids),
        outcome=RequirementOutcome.OPEN,
    )


def build_complete_workspace(root: Path) -> Path:
    """Initialize a freeze-eligible workspace with a two-goal dependency chain.

    Registers inventory items and requirements mapping ``REQ-1`` to
    ``GOAL-1`` and ``REQ-2`` to ``GOAL-2``, the goal-contract family
    drafts (``GOAL-1`` depending on ``GOAL-2``, plus an unrelated
    ``GOAL-UNRELATED`` that is registered but is not a dependency), shared
    acceptance/analysis/closure records, and resource ``RES-1``. ``GOAL-1``
    declares output ``analysis_input_manifest``; ``GOAL-2`` declares
    ``raw_isotherm_data``; ``GOAL-UNRELATED`` declares
    ``unrelated_artifact``.
    """
    init_project(root)
    register_inventory_item(
        root, make_item("ITEM-1", requirement_ids=("REQ-1",))
    )
    register_inventory_item(
        root, make_item("ITEM-2", requirement_ids=("REQ-2",))
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            goal_ids=("GOAL-1",),
            inventory_items=("ITEM-1",),
        ),
    )
    register_requirement(
        root,
        make_requirement(
            "REQ-2",
            goal_ids=("GOAL-2",),
            inventory_items=("ITEM-2",),
        ),
    )
    register_goal(
        root,
        make_goal(
            "GOAL-1",
            dependencies=(
                GoalDependency(
                    goal_id="GOAL-2",
                    type=DependencyType.HARD_GATE,
                    execution_gate=True,
                    acceptance_gate=True,
                ),
            ),
            outputs=({"name": "analysis_input_manifest"},),
            resource_ids=("RES-1",),
            requirement_ids=("REQ-1",),
        ),
    )
    register_goal(
        root,
        make_goal(
            "GOAL-2",
            outputs=({"name": "raw_isotherm_data"},),
            requirement_ids=("REQ-2",),
        ),
    )
    register_goal(
        root,
        make_goal(
            "GOAL-UNRELATED",
            outputs=({"name": "unrelated_artifact"},),
            requirement_ids=("REQ-2",),
        ),
    )
    register_acceptance(root, make_acceptance())
    register_analysis_protocol(root, make_analysis_protocol("ANP-1"))
    register_closure_contract(root, make_closure())
    register_resource(root, make_resource("RES-1"))
    return root


def freeze_complete(root: Path) -> PlanFreezeResult:
    """Build and freeze the draft of a complete workspace deterministically."""
    return freeze_plan(root, build_plan_v1(root), timestamp=FROZEN_AT)


def frozen_goal(root: Path, goal_id: str = "GOAL-1") -> GoalContract:
    """The frozen Goal Contract of ``goal_id`` from the real freeze flow."""
    for goal in freeze_complete(root).goals:
        if goal.goal_id == goal_id:
            return goal
    raise AssertionError(f"no frozen goal {goal_id!r} in the freeze result")
