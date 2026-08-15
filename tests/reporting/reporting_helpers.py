"""Shared test helpers for the reporting tests (DEV-M13-G01).

``IDENTITY`` / ``TIMESTAMP`` pin every deterministic input the backing
``initialize_project`` call takes, so each test exercises the
deterministic path; ``FROZEN_AT`` is the fixed freeze timestamp every
``freeze_primary_protocol`` call uses (no wall clock anywhere). The
workspace installers register a fully linked SS7 report-traceability
chain (``14-STATE-GIT-ARTIFACTS.md`` SS7) through the **real**
registration APIs -- project, PRIMARY analysis protocol (draft ->
frozen ``v1``), goal contract, acceptance criteria, inventory item and
requirement, Run record (``runs/<id>.json``), raw artifact manifest
and analysis result package -- plus the in-memory claim-specific
evidence registry. Nothing here is mocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientific_reproduction.analysis.protocols import (
    freeze_primary_protocol,
    register_analysis_record,
)
from scientific_reproduction.analysis.results import (
    ResultRecord,
    register_result,
)
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import AuditIdentity
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisKind,
    AnalysisProfile,
    AnalysisProtocolOrResult,
    ArtifactManifest,
    ClaimSpecificEvidence,
    Criticality,
    DecisionMode,
    EvidenceAssessment,
    GoalAcceptance,
    GoalContract,
    GoalReplication,
    GoalTrack,
    InventoryItemType,
    LifecycleState,
    MappingStatus,
    PrimaryOrExploratory,
    ReproductionInventoryItem,
    ReproductionRequirement,
    RequirementOutcome,
    Run,
    RunType,
    ScientificReview,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    register_acceptance,
    register_goal,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

#: Deterministic author/committer identity used by every init behind the
#: reporting tests.
IDENTITY = AuditIdentity(name="Audit Bot", email="audit@example.org")

#: Fixed timestamp for the initialized project state.
TIMESTAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)

#: Fixed freeze timestamp: every freeze in this suite is deterministic.
FROZEN_AT = datetime(2026, 6, 1, tzinfo=timezone.utc)

#: Primary target DOI used to initialize test projects
#: (``17-FDM201-REFERENCE-CASE.md``).
DOI = "10.1039/D5TA00771B"

#: A schema-valid sha256 for every fixture manifest (64 hex characters).
SHA256: str = "a" * 64

#: Deterministic record ids of the installed SS7 chain.
CLAIM_ID: str = "CLAIM-001"
EVIDENCE_ID: str = "EVID-001"
SOURCE_ID: str = "SRC-001"
GOAL_ID: str = "GOAL-001"
REQUIREMENT_ID: str = "REQ-001"
ACCEPTANCE_ID: str = "ACC-001"
ANALYSIS_ID: str = "ANAL-001"
PROTOCOL_VERSION: str = "v1"
RESULT_ID: str = "RES-001"
RUN_ID: str = "RUN-001"
FAILED_RUN_ID: str = "RUN-002"
ARTIFACT_ID: str = "ART-001"
INVENTORY_ID: str = "INV-001"


def init_project(root: Path) -> Path:
    """Initialize a deterministic one-paper project at ``root``; return it."""
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    return root


# ---------------------------------------------------------------------------
# Record makers (schema-valid fixtures with compact defaults)
# ---------------------------------------------------------------------------


def make_protocol(
    analysis_id: str = ANALYSIS_ID,
    *,
    primary_or_exploratory: PrimaryOrExploratory = PrimaryOrExploratory.PRIMARY,
    kind: AnalysisKind = AnalysisKind.PROTOCOL,
    frozen: bool = False,
    protocol_version: str = INITIAL_PLAN_VERSION,
    **kwargs: Any,
) -> AnalysisProtocolOrResult:
    """Build a schema-valid draft analysis protocol with compact defaults."""
    return AnalysisProtocolOrResult(
        analysis_id=analysis_id,
        kind=kind,
        protocol_version=protocol_version,
        primary_or_exploratory=primary_or_exploratory,
        profile=AnalysisProfile.ROUTINE_ANALYSIS,
        frozen=frozen,
        methods=[{"name": "isotherm_fit"}],
        **kwargs,
    )


def make_goal(goal_id: str = GOAL_ID, **kwargs: Any) -> GoalContract:
    """Build a schema-valid goal contract draft with compact defaults."""
    return GoalContract(
        goal_id=goal_id,
        title="Reproduce the FDM-201 batch-level uptake",
        unit_process_type="batch_adsorption",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective=(
            "Reproduce the batch-level uptake results of the FDM-201"
            " reference case"
        ),
        requirement_ids=[REQUIREMENT_ID],
        dependencies=[],
        acceptance=GoalAcceptance(criteria_ref=ACCEPTANCE_ID, frozen=True),
        analysis_protocol_ref=ANALYSIS_ID,
        replication=GoalReplication(
            independent_required=True, planned_n_policy="n=1 per condition"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        **kwargs,
    )


def make_acceptance(
    acceptance_id: str = ACCEPTANCE_ID,
    *,
    evidence_refs: list[str] | None = None,
    **kwargs: Any,
) -> AcceptanceCriteria:
    """Build a schema-valid acceptance criteria draft with compact defaults."""
    return AcceptanceCriteria(
        acceptance_id=acceptance_id,
        goal_id=GOAL_ID,
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "batch_level_uptake", "tolerance": 0.05}],
        evidence_refs=(
            [EVIDENCE_ID] if evidence_refs is None else list(evidence_refs)
        ),
        **kwargs,
    )


def make_inventory_item(
    inventory_id: str = INVENTORY_ID,
) -> ReproductionInventoryItem:
    """Build a schema-valid inventory item with compact defaults."""
    return ReproductionInventoryItem(
        inventory_id=inventory_id,
        source_id=SOURCE_ID,
        item_type=InventoryItemType.EXPERIMENT,
        formal_report=True,
        description="Batch adsorption experiment of the FDM-201 case",
        mapping_status=MappingStatus.MAPPED,
    )


def make_requirement(
    requirement_id: str = REQUIREMENT_ID,
    *,
    inventory_items: list[str] | None = None,
    goal_ids: list[str] | None = None,
    outcome: RequirementOutcome = RequirementOutcome.REPRODUCED,
    **kwargs: Any,
) -> ReproductionRequirement:
    """Build a schema-valid requirement with compact defaults."""
    return ReproductionRequirement(
        requirement_id=requirement_id,
        statement="Batch-level uptake must be reproduced within tolerance",
        inventory_items=(
            [INVENTORY_ID] if inventory_items is None else list(inventory_items)
        ),
        criticality=Criticality.CRITICAL,
        goal_ids=[GOAL_ID] if goal_ids is None else list(goal_ids),
        outcome=outcome,
        **kwargs,
    )


def make_evidence(
    evidence_id: str = EVIDENCE_ID,
    *,
    claim_id: str = CLAIM_ID,
    used_by: list[str] | None = None,
    **kwargs: Any,
) -> ClaimSpecificEvidence:
    """Build a schema-valid claim-specific evidence record."""
    return ClaimSpecificEvidence(
        evidence_id=evidence_id,
        source_id=SOURCE_ID,
        claim_id=claim_id,
        finding="The report claims batch-level uptake within tolerance",
        assessment=EvidenceAssessment(
            authority=3,
            reliability=4,
            directness=4,
            reliability_checklist_ref="CL-001",
        ),
        used_by=(
            [GOAL_ID, REQUIREMENT_ID] if used_by is None else list(used_by)
        ),
        **kwargs,
    )


def make_run(
    run_id: str = RUN_ID,
    *,
    goal_id: str = GOAL_ID,
    run_type: RunType = RunType.INDEPENDENT_REPLICATE,
    lifecycle_state: LifecycleState = LifecycleState.CLOSED,
    goal_version: str = INITIAL_PLAN_VERSION,
    scientific_review: ScientificReview = ScientificReview.PASS,
    artifacts: list[str] | None = None,
    **kwargs: Any,
) -> Run:
    """Build a schema-valid closed run record with compact defaults."""
    return Run(
        run_id=run_id,
        goal_id=goal_id,
        run_type=run_type,
        lifecycle_state=lifecycle_state,
        goal_version=goal_version,
        scientific_review=scientific_review,
        artifacts=(
            [ARTIFACT_ID] if artifacts is None else list(artifacts)
        ),
        created_at=TIMESTAMP.isoformat(),
        **kwargs,
    )


def make_manifest(
    artifact_id: str = ARTIFACT_ID,
    *,
    run_id: str = RUN_ID,
    analysis_id: str | None = RESULT_ID,
    **kwargs: Any,
) -> ArtifactManifest:
    """Build a schema-valid artifact manifest with compact defaults."""
    return ArtifactManifest(
        artifact_id=artifact_id,
        uri=f"file:///artifacts/{artifact_id}.raw",
        sha256=SHA256,
        size_bytes=1024,
        created_at=TIMESTAMP.isoformat(),
        run_id=run_id,
        analysis_id=analysis_id,
        **kwargs,
    )


def make_result_record(
    result_id: str = RESULT_ID,
    *,
    analysis_id: str = ANALYSIS_ID,
    protocol_version: str = PROTOCOL_VERSION,
    run_ref: str = RUN_ID,
    input_artifact_ids: list[str] | None = None,
    acceptance_ref: str | None = ACCEPTANCE_ID,
    requirement_refs: list[str] | None = None,
    **kwargs: Any,
) -> ResultRecord:
    """Build a schema-valid analysis result package with trace links."""
    return ResultRecord(
        result_id=result_id,
        analysis_id=analysis_id,
        protocol_version=protocol_version,
        run_ref=run_ref,
        input_artifact_ids=(
            [ARTIFACT_ID] if input_artifact_ids is None else list(input_artifact_ids)
        ),
        primary_or_exploratory=PrimaryOrExploratory.PRIMARY,
        acceptance_ref=acceptance_ref,
        requirement_refs=(
            [REQUIREMENT_ID] if requirement_refs is None else list(requirement_refs)
        ),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Workspace installers (real registration APIs, authoring order)
# ---------------------------------------------------------------------------


def install_valid_chain(
    root: Path,
    *,
    claim_id: str = CLAIM_ID,
    acceptance: AcceptanceCriteria | None = None,
    run: Run | None = None,
    manifest: ArtifactManifest | None = None,
    result: ResultRecord | None = None,
    evidence: ClaimSpecificEvidence | None = None,
    requirement: ReproductionRequirement | None = None,
) -> EvidenceRegistry:
    """Install a fully linked, valid SS7 report-traceability chain at ``root``.

    Registers, through the real registration APIs in authoring order: the
    project, the PRIMARY analysis protocol (``v1-draft`` registered, then
    frozen to ``v1`` with the fixed ``FROZEN_AT`` stamp), the goal
    contract, the acceptance criteria, the inventory item and the
    requirement, the run record (``runs/RUN-001.json``), the raw
    artifact manifest (``manifests/ART-001.json``) and the analysis
    result package linking run/artifact/acceptance/requirement. The
    returned evidence registry backs ``claim_id`` with one
    ``ClaimSpecificEvidence`` record whose ``used_by`` links the goal
    and the requirement. Every override argument replaces the
    corresponding default record.
    """
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    protocol = make_protocol()
    register_analysis_record(root, protocol)
    freeze_primary_protocol(root, protocol, timestamp=FROZEN_AT)
    register_goal(root, make_goal())
    register_acceptance(root, acceptance or make_acceptance())
    register_inventory_item(root, make_inventory_item())
    register_requirement(root, requirement or make_requirement())
    # The run store is a state backend over the workspace root, resolving
    # the canonical ``runs/`` tree directory (SCHEMA_TO_STATE_DIR).
    FilesystemStateBackend(root).write(
        "run", RUN_ID, (run or make_run()).to_dict()
    )
    ArtifactRegistry(root / "manifests").register(
        manifest or make_manifest()
    )
    register_result(root, result or make_result_record())
    return EvidenceRegistry.from_records(
        [evidence or make_evidence(claim_id=claim_id)]
    )


def install_chain_with_failed_run(
    root: Path,
    *,
    failed_run: Run | None = None,
    **chain: Any,
) -> tuple[EvidenceRegistry, Run]:
    """Install the valid chain plus an extra failed run (AC-03).

    The extra run (default ``RUN-002``, ``CANCELLED``, no artifacts) is
    written to the run store after the valid chain, so the audit package
    carries a failed run alongside the succeeded one while the claim
    trace stays untouched. Returns the evidence registry and the failed
    run record.
    """
    evidence = install_valid_chain(root, **chain)
    failed = failed_run or make_run(
        run_id=FAILED_RUN_ID,
        lifecycle_state=LifecycleState.CANCELLED,
        scientific_review=ScientificReview.UNREVIEWED,
        artifacts=[],
    )
    FilesystemStateBackend(root).write(
        "run", failed.run_id, failed.to_dict()
    )
    return evidence, failed
