"""Typed core object models for all normative project objects (DEV-M1-G01).

Every model is a frozen dataclass whose field names are the **exact keys**
of the corresponding frozen product schema in ``schemas/<name>.schema.yaml``
(see ``schema_name`` for the mapping). Each model:

* mirrors the schema's REQUIRED and OPTIONAL fields with correct types and
  defaults (required fields carry no default; optional fields default to
  ``None`` or to the schema-declared default);
* has ``to_dict()`` returning a plain dict with the schema key names;
* has ``from_dict()`` a classmethod that round-trips ``to_dict()`` output;
* uses ``Enum``/``StrEnum`` members whose values match the schema enums
  exactly -- nothing is invented here.

Constructors only hold data and enforce shape/type (missing required
fields raise, unknown enum values raise, nested objects are coerced to
their typed dataclasses). No workflow decisions -- phase/outcome choice,
gate evaluation, retry logic -- live in these models; that belongs to the
workflow rules in later milestones.

Note on ``ProjectEvent``: the schema key ``from`` is a Python keyword, so
the dataclass field is ``from_`` with a serialization alias -- ``to_dict()``
emits ``"from"`` and ``from_dict()`` accepts ``"from"`` (or ``"from_"``).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from types import UnionType
from typing import (
    Any,
    ClassVar,
    Literal,
    Mapping,
    Self,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

__all__ = [
    # base
    "CoreModel",
    "MODEL_REGISTRY",
    "SCHEMA_NAMES",
    # enums
    "ProjectPhase",
    "ReproductionOutcome",
    "TargetSourceType",
    "RunType",
    "LifecycleState",
    "ScientificReview",
    "PlanStatus",
    "AuditStatus",
    "GoalTrack",
    "DependencyType",
    "AssumptionClassification",
    "StrictStatusEffect",
    "InventoryItemType",
    "MappingStatus",
    "ResourceType",
    "AvailabilityState",
    "SourceType",
    "AccessClass",
    "WorkerRole",
    "AnalysisKind",
    "AnalysisProfile",
    "PrimaryOrExploratory",
    "DecisionType",
    "GateType",
    "GateStatus",
    "Criticality",
    "RequirementOutcome",
    "MethodReproducibility",
    "DecisionMode",
    "Confidence",
    "MarginBasis",
    "ResearchRequestStatus",
    # models
    "Project",
    "Run",
    "Plan",
    "GoalContract",
    "ClaimSpecificEvidence",
    "Assumption",
    "ClosureContract",
    "ReproductionInventoryItem",
    "Resource",
    "ResearchSource",
    "GoalExecutionContextPackage",
    "LabExecutionPackage",
    "AnalysisProtocolOrResult",
    "SupervisorDecision",
    "HumanGate",
    "ProjectEvent",
    "AutomaticRetryPolicy",
    "ReproductionRequirement",
    "ArtifactManifest",
    "AcceptanceCriteria",
    "StatisticalDesign",
    "ResearchRequest",
]


# ---------------------------------------------------------------------------
# Enums -- values are frozen from schemas/*.schema.yaml, do not invent.
# ---------------------------------------------------------------------------


class ProjectPhase(StrEnum):
    INITIALIZING = "INITIALIZING"
    SOURCE_ACQUISITION = "SOURCE_ACQUISITION"
    REPRODUCTION_INVENTORY = "REPRODUCTION_INVENTORY"
    PLANNING = "PLANNING"
    PLAN_AUDIT = "PLAN_AUDIT"
    PLAN_FROZEN = "PLAN_FROZEN"
    EXECUTING = "EXECUTING"
    REPLANNING = "REPLANNING"
    FINAL_VALIDATION = "FINAL_VALIDATION"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_RESOURCE = "WAITING_RESOURCE"


class ReproductionOutcome(StrEnum):
    UNDETERMINED = "UNDETERMINED"
    FULLY_REPRODUCED = "FULLY_REPRODUCED"
    PARTIALLY_REPRODUCED = "PARTIALLY_REPRODUCED"
    NOT_REPRODUCED_WITHIN_DEFINED_SCOPE = "NOT_REPRODUCED_WITHIN_DEFINED_SCOPE"
    INCONCLUSIVE = "INCONCLUSIVE"


class TargetSourceType(StrEnum):
    PDF = "pdf"
    DOI = "doi"
    URL = "url"


class RunType(StrEnum):
    INDEPENDENT_REPLICATE = "independent_replicate"
    TECHNICAL_REPLICATE = "technical_replicate"
    INSTRUMENT_REPEAT = "instrument_repeat"
    RETRY = "retry"
    ADDITIONAL_REPLICATE = "additional_replicate"


class LifecycleState(StrEnum):
    CREATED = "CREATED"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    RUNNING_EXTERNAL = "RUNNING_EXTERNAL"
    RESULT_AVAILABLE = "RESULT_AVAILABLE"
    ANALYZING = "ANALYZING"
    SUBMITTED_FOR_REVIEW = "SUBMITTED_FOR_REVIEW"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    INVALIDATED = "INVALIDATED"


class ScientificReview(StrEnum):
    UNREVIEWED = "UNREVIEWED"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class PlanStatus(StrEnum):
    DRAFT = "DRAFT"
    UNDER_AUDIT = "UNDER_AUDIT"
    FROZEN = "FROZEN"
    SUPERSEDED = "SUPERSEDED"


class AuditStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class GoalTrack(StrEnum):
    STRICT_REPRODUCTION = "STRICT_REPRODUCTION"
    RECOVERY = "RECOVERY"
    METHOD_REDESIGN = "METHOD_REDESIGN"


class DependencyType(StrEnum):
    HARD_GATE = "hard_gate"
    SOFT_DEPENDENCY = "soft_dependency"
    INFORMATIONAL = "informational"


class AssumptionClassification(StrEnum):
    A0_TECHNICAL_DEFAULT = "A0_TECHNICAL_DEFAULT"
    A1_METHODOLOGICAL_DEFAULT = "A1_METHODOLOGICAL_DEFAULT"
    A2_SCIENTIFIC_ASSUMPTION = "A2_SCIENTIFIC_ASSUMPTION"


class StrictStatusEffect(StrEnum):
    NONE = "NONE"
    STRICT_WITH_ASSUMPTIONS = "STRICT_WITH_ASSUMPTIONS"
    DISQUALIFIES_PURE_STRICT = "DISQUALIFIES_PURE_STRICT"


class InventoryItemType(StrEnum):
    EXPERIMENT = "experiment"
    CONTROL = "control"
    CHARACTERIZATION = "characterization"
    COMPUTATION = "computation"
    ANALYSIS = "analysis"
    DATASET = "dataset"
    STRUCTURE = "structure"
    FIGURE = "figure"
    TABLE = "table"
    SUPPLEMENTARY_RESULT = "supplementary_result"
    OTHER = "other"


class MappingStatus(StrEnum):
    UNMAPPED = "UNMAPPED"
    MAPPED = "MAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    EXCLUDED_NONFORMAL = "EXCLUDED_NONFORMAL"


class ResourceType(StrEnum):
    REAGENT = "reagent"
    CONSUMABLE = "consumable"
    INSTRUMENT = "instrument"
    EXTERNAL_SERVICE = "external_service"
    COMPUTE_ACCESS = "compute_access"
    DATABASE_ACCESS = "database_access"
    SAFETY_CAPABILITY = "safety_capability"
    OTHER = "other"


class AvailabilityState(StrEnum):
    AVAILABLE = "AVAILABLE"
    PROCURE = "PROCURE"
    OUTSOURCE = "OUTSOURCE"
    CAPABILITY_GAP = "CAPABILITY_GAP"


class SourceType(StrEnum):
    TARGET_PAPER = "target_paper"
    SUPPLEMENTARY_INFORMATION = "supplementary_information"
    DATASET = "dataset"
    STRUCTURE_DEPOSITION = "structure_deposition"
    PEER_REVIEWED_PAPER = "peer_reviewed_paper"
    REVIEW = "review"
    THESIS = "thesis"
    PREPRINT = "preprint"
    STANDARD = "standard"
    OFFICIAL_DOCUMENTATION = "official_documentation"
    VENDOR_NOTE = "vendor_note"
    INFORMAL = "informal"
    DATABASE_RECORD = "database_record"
    OTHER = "other"


class AccessClass(StrEnum):
    PUBLIC = "PUBLIC"
    OPTIONAL_COMMERCIAL = "OPTIONAL_COMMERCIAL"
    USER_PROVIDED = "USER_PROVIDED"
    UNKNOWN = "UNKNOWN"


class WorkerRole(StrEnum):
    EXPERIMENT_WORKER = "experiment_worker"
    COMPUTATION_WORKER = "computation_worker"
    ANALYSIS_WORKER = "analysis_worker"
    DIAGNOSIS_WORKER = "diagnosis_worker"


class AnalysisKind(StrEnum):
    PROTOCOL = "protocol"
    RESULT = "result"


class AnalysisProfile(StrEnum):
    ROUTINE_ANALYSIS = "ROUTINE_ANALYSIS"
    STATISTICAL_VALIDATION = "STATISTICAL_VALIDATION"
    FAILURE_DIAGNOSIS = "FAILURE_DIAGNOSIS"


class PrimaryOrExploratory(StrEnum):
    PRIMARY = "PRIMARY"
    EXPLORATORY = "EXPLORATORY"


class DecisionType(StrEnum):
    PLAN_FREEZE = "PLAN_FREEZE"
    GOAL_REVISION = "GOAL_REVISION"
    ACCEPTANCE_REVISION = "ACCEPTANCE_REVISION"
    ANALYSIS_PROTOCOL_REVISION = "ANALYSIS_PROTOCOL_REVISION"
    RESEARCH_REQUEST = "RESEARCH_REQUEST"
    RECOVERY_ENTRY = "RECOVERY_ENTRY"
    METHOD_REDESIGN_ENTRY = "METHOD_REDESIGN_ENTRY"
    GOAL_REVIEW = "GOAL_REVIEW"
    REQUIREMENT_CLOSURE = "REQUIREMENT_CLOSURE"
    HUMAN_GATE_OPEN = "HUMAN_GATE_OPEN"
    PROJECT_OUTCOME = "PROJECT_OUTCOME"


class GateType(StrEnum):
    RESOURCE_GATE = "RESOURCE_GATE"
    ACCESS_GATE = "ACCESS_GATE"
    SAFETY_GATE = "SAFETY_GATE"
    SCOPE_GATE = "SCOPE_GATE"
    TERMINATION_GATE = "TERMINATION_GATE"
    EXTERNAL_CONTACT_GATE = "EXTERNAL_CONTACT_GATE"


class GateStatus(StrEnum):
    OPEN = "OPEN"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class Criticality(StrEnum):
    CRITICAL = "CRITICAL"
    REQUIRED = "REQUIRED"
    SUPPORTING = "SUPPORTING"


class RequirementOutcome(StrEnum):
    OPEN = "OPEN"
    REPRODUCED = "REPRODUCED"
    REPRODUCED_WITH_RECOVERY = "REPRODUCED_WITH_RECOVERY"
    NOT_REPRODUCED = "NOT_REPRODUCED"
    INCONCLUSIVE = "INCONCLUSIVE"


class MethodReproducibility(StrEnum):
    UNDETERMINED = "UNDETERMINED"
    DIRECTLY_REPRODUCIBLE = "DIRECTLY_REPRODUCIBLE"
    REPRODUCIBLE_WITH_MINOR_RECOVERY = "REPRODUCIBLE_WITH_MINOR_RECOVERY"
    REPRODUCIBLE_WITH_METHOD_ADJUSTMENT = "REPRODUCIBLE_WITH_METHOD_ADJUSTMENT"
    ONLY_REPRODUCIBLE_AFTER_REDESIGN = "ONLY_REPRODUCIBLE_AFTER_REDESIGN"
    NOT_REPRODUCIBLE = "NOT_REPRODUCIBLE"
    INCONCLUSIVE = "INCONCLUSIVE"


class DecisionMode(StrEnum):
    EQUIVALENCE = "equivalence"
    BOUNDED_INTERVAL = "bounded_interval"
    CATEGORICAL = "categorical"
    TREND = "trend"
    STRUCTURAL_MATCH = "structural_match"
    CONVERGENCE = "convergence"
    CUSTOM = "custom"


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class MarginBasis(StrEnum):
    """The frozen margin-basis vocabulary (07-STATISTICS-AND-ACCEPTANCE.md
    SS8: every numeric margin or decision threshold records its basis).

    The five sanctioned basis categories of SS8 -- target paper
    error/variation, independent reproduction literature, standard
    method/instrument uncertainty, a domain-specific accepted threshold,
    or an explicit scientific equivalence judgment with documented
    rationale. Values are the exact SS8 category names; no global fixed
    percent rule exists anywhere.
    """

    TARGET_PAPER_ERROR = "target_paper_error"
    REPRODUCTION_LITERATURE = "reproduction_literature"
    INSTRUMENT_UNCERTAINTY = "instrument_uncertainty"
    DOMAIN_THRESHOLD = "domain_threshold"
    SCIENTIFIC_JUDGMENT = "scientific_judgment"


class ResearchRequestStatus(StrEnum):
    OPEN = "OPEN"
    SEARCHING = "SEARCHING"
    COMPLETE = "COMPLETE"
    EXHAUSTED = "EXHAUSTED"


# ---------------------------------------------------------------------------
# Generic (de)serialization machinery
# ---------------------------------------------------------------------------


class CoreModel:
    """Base class for all typed core models.

    Subclasses are frozen dataclasses whose field names are the schema keys.
    """

    #: Mapping of dataclass field name -> schema key, for fields that cannot
    #: be named after their schema key in Python (e.g. ``from_`` -> ``from``).
    _FIELD_ALIASES: ClassVar[dict[str, str]] = {}

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict with the exact schema key names.

        Fields whose value is ``None`` are omitted: several schemas declare
        optional properties as non-nullable (``{type: string}`` without
        ``"null"``), so emitting ``null`` would make serialized objects
        fail their own schema. Absent keys are exactly what those schemas
        accept for unset optional fields.
        """
        aliases = type(self)._FIELD_ALIASES
        # Every CoreModel subclass is a frozen dataclass; mypy cannot see
        # that from the base class, hence the arg-type suppression.
        fields_ = dataclasses.fields(self)  # type: ignore[arg-type]
        return {
            aliases.get(f.name, f.name): _to_plain(getattr(self, f.name))
            for f in fields_
            if getattr(self, f.name) is not None
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Self:
        """Build a model from a plain dict (schema key names).

        Raises:
            TypeError: if ``data`` is not a mapping or required fields are
                missing.
            ValueError: if an enum field value is not one of the schema enum
                values.
        """
        if not isinstance(data, Mapping):
            raise TypeError(
                f"{cls.__name__}.from_dict expects a mapping, got {type(data).__name__}"
            )
        hints = get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        missing: list[str] = []
        # Same rationale as to_dict(): subclasses are frozen dataclasses.
        fields_ = dataclasses.fields(cls)  # type: ignore[arg-type]
        for f in fields_:
            # Schema keys are the source of truth; the raw python field name
            # is tolerated as a fallback.
            key = cls._FIELD_ALIASES.get(f.name, f.name)
            if key in data:
                value = data[key]
            elif f.name in data:
                value = data[f.name]
            elif f.default is not dataclasses.MISSING:
                kwargs[f.name] = f.default
                continue
            elif f.default_factory is not dataclasses.MISSING:
                kwargs[f.name] = f.default_factory()
                continue
            else:
                missing.append(f.name)
                continue
            kwargs[f.name] = _from_plain(value, hints[f.name])
        if missing:
            raise TypeError(
                f"{cls.__name__}.from_dict missing required field(s): "
                f"{', '.join(sorted(missing))}"
            )
        return cls(**kwargs)


def _to_plain(value: Any) -> Any:
    """Recursively convert models/enums/collections to plain JSON-able data."""
    if isinstance(value, CoreModel):
        return value.to_dict()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    return value


def _from_plain(value: Any, hint: Any) -> Any:
    """Coerce ``value`` to the annotated type ``hint`` (recursively)."""
    if hint is Any or value is None:
        return value
    origin = get_origin(hint)
    if origin is list:
        return [_from_plain(v, get_args(hint)[0]) for v in value]
    if origin is dict or origin is Literal:
        return value
    if origin in (UnionType, Union):
        non_none = [a for a in get_args(hint) if a is not type(None)]
        if len(non_none) == 1:
            return _from_plain(value, non_none[0])
        return value
    if isinstance(hint, type) and issubclass(hint, CoreModel):
        return hint.from_dict(value)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(value)
    return value


# ---------------------------------------------------------------------------
# Nested objects (schema sub-objects that are themselves typed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrimaryTarget(CoreModel):
    source_type: TargetSourceType
    identifier: str
    doi: str | None = None
    title: str | None = None


@dataclass(frozen=True)
class RunExternal(CoreModel):
    backend: str | None = None
    job_id: str | None = None
    dispatch_id: str | None = None
    working_directory: str | None = None


@dataclass(frozen=True)
class PlanInventoryAudit(CoreModel):
    formally_reported_items: int
    mapped_items: int
    unmapped_items: int
    ambiguous_items: int
    coverage: float
    status: AuditStatus | None = None


@dataclass(frozen=True)
class GoalDependency(CoreModel):
    goal_id: str
    type: DependencyType
    execution_gate: bool = False
    acceptance_gate: bool = False


@dataclass(frozen=True)
class GoalReplication(CoreModel):
    independent_required: bool
    planned_n_policy: str
    minimum_n: int | None = None
    technical_repeats: int | None = None


@dataclass(frozen=True)
class GoalAcceptance(CoreModel):
    criteria_ref: str
    frozen: bool


@dataclass(frozen=True)
class EvidenceAssessment(CoreModel):
    authority: int
    reliability: int
    directness: int
    reliability_checklist_ref: str
    ranking_score: float | None = None


@dataclass(frozen=True)
class ClosureRecovery(CoreModel):
    eligibility_rule: dict[str, Any] = field(default_factory=dict)
    eligible_hypotheses_total: int | None = None
    tested_or_ruled_out: int | None = None
    remaining: int | None = None


@dataclass(frozen=True)
class ClosureLiterature(CoreModel):
    required_search_families_completed: bool | None = None
    consecutive_zero_novelty_cycles: int | None = None
    required_zero_novelty_cycles: int = 2


# ---------------------------------------------------------------------------
# The 22 normative object models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project(CoreModel):
    schema_name: ClassVar[str] = "project"

    project_id: str
    primary_target: PrimaryTarget
    project_phase: ProjectPhase
    reproduction_outcome: ReproductionOutcome
    current_plan_version: str
    title: str | None = None
    domain_pack: str | None = None
    state_backend: Literal["filesystem"] | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class Run(CoreModel):
    schema_name: ClassVar[str] = "run"

    run_id: str
    goal_id: str
    run_type: RunType
    lifecycle_state: LifecycleState
    goal_version: str
    scientific_review: ScientificReview = ScientificReview.UNREVIEWED
    worker_session_ref: str | None = None
    external: RunExternal | None = None
    artifacts: list[str] = field(default_factory=list)
    deviations: list[dict[str, Any]] = field(default_factory=list)
    engineering_retries: list[dict[str, Any]] = field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class Plan(CoreModel):
    schema_name: ClassVar[str] = "plan"

    plan_id: str
    version: str
    status: PlanStatus
    inventory_audit: PlanInventoryAudit
    goal_ids: list[str]
    requirement_ids: list[str]
    parent_plan_version: str | None = None
    work_packages: list[dict[str, Any]] = field(default_factory=list)
    resource_ids: list[str] = field(default_factory=list)
    frozen_at: str | None = None
    frozen_commit: str | None = None


@dataclass(frozen=True)
class GoalContract(CoreModel):
    schema_name: ClassVar[str] = "goal"

    goal_id: str
    title: str
    unit_process_type: str
    track: GoalTrack
    objective: str
    requirement_ids: list[str]
    dependencies: list[GoalDependency]
    acceptance: GoalAcceptance
    analysis_protocol_ref: str
    replication: GoalReplication
    version: str
    frozen: bool
    parent_goal_id: str | None = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    resource_ids: list[str] = field(default_factory=list)
    assumption_ids: list[str] = field(default_factory=list)
    closure_contract_ref: str | None = None
    evidence_requirements: list[dict[str, Any]] = field(default_factory=list)
    automatic_retry_policy_ref: str | None = None
    frozen_at: str | None = None
    frozen_commit: str | None = None


@dataclass(frozen=True)
class ClaimSpecificEvidence(CoreModel):
    schema_name: ClassVar[str] = "evidence"

    evidence_id: str
    source_id: str
    claim_id: str
    finding: str
    assessment: EvidenceAssessment
    source_location: str | None = None
    limitations: list[str] = field(default_factory=list)
    role: str | None = None
    used_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Assumption(CoreModel):
    schema_name: ClassVar[str] = "assumption"

    assumption_id: str
    parameter: str
    classification: AssumptionClassification
    rationale: str
    source_refs: list[str]
    value: Any = None
    affected_goal_ids: list[str] = field(default_factory=list)
    strict_status_effect: StrictStatusEffect | None = None


@dataclass(frozen=True)
class ClosureContract(CoreModel):
    schema_name: ClassVar[str] = "closure-contract"

    closure_id: str
    frozen: bool
    statistical_sufficiency: dict[str, Any]
    execution_validity: dict[str, Any]
    diagnosis: dict[str, Any]
    recovery: ClosureRecovery
    literature: ClosureLiterature
    closure_allowed: bool = False


@dataclass(frozen=True)
class ReproductionInventoryItem(CoreModel):
    schema_name: ClassVar[str] = "inventory-item"

    inventory_id: str
    source_id: str
    item_type: InventoryItemType
    formal_report: bool
    description: str
    mapping_status: MappingStatus
    source_location: str | None = None
    conditions: dict[str, Any] = field(default_factory=dict)
    linked_inventory_ids: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)
    ambiguity_notes: str | None = None


@dataclass(frozen=True)
class Resource(CoreModel):
    schema_name: ClassVar[str] = "resource"

    resource_id: str
    name: str
    resource_type: ResourceType
    availability_state: AvailabilityState
    blocks_goal_ids: list[str] = field(default_factory=list)
    estimated_cost: float | None = None
    currency: str | None = None
    human_gate_required: bool = False
    notes: str | None = None


@dataclass(frozen=True)
class ResearchSource(CoreModel):
    schema_name: ClassVar[str] = "source"

    source_id: str
    source_type: SourceType
    title: str
    provenance: str
    doi: str | None = None
    stable_identifier: str | None = None
    url_or_locator: str | None = None
    publication_year: int | None = None
    acquired_at: str | None = None
    local_artifact_id: str | None = None
    access_class: AccessClass | None = None


@dataclass(frozen=True)
class GoalExecutionContextPackage(CoreModel):
    schema_name: ClassVar[str] = "worker-context"

    context_id: str
    worker_role: WorkerRole
    goal_id: str
    goal_version: str
    allowed_actions: list[str]
    forbidden_actions: list[str]
    run_id: str | None = None
    source_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    upstream_result_refs: list[str] = field(default_factory=list)
    protocol_refs: list[str] = field(default_factory=list)
    resource_refs: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    required_outputs: list[str] = field(default_factory=list)
    context_hash: str | None = None


@dataclass(frozen=True)
class LabExecutionPackage(CoreModel):
    schema_name: ClassVar[str] = "lab-execution-package"

    package_id: str
    project_id: str
    goal_id: str
    run_id: str
    objective: str
    procedure: list[dict[str, Any]]
    required_return: list[str]
    track: GoalTrack | None = None
    reagents: list[dict[str, Any]] = field(default_factory=list)
    instruments: list[dict[str, Any]] = field(default_factory=list)
    critical_control_variables: list[dict[str, Any]] = field(default_factory=list)
    prohibited_changes: list[str] = field(default_factory=list)
    required_operator_records: list[str] = field(default_factory=list)
    safety_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AnalysisProtocolOrResult(CoreModel):
    schema_name: ClassVar[str] = "analysis"

    analysis_id: str
    kind: AnalysisKind
    protocol_version: str
    primary_or_exploratory: PrimaryOrExploratory
    profile: AnalysisProfile | None = None
    frozen: bool = False
    input_artifact_ids: list[str] = field(default_factory=list)
    methods: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    uncertainty: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SupervisorDecision(CoreModel):
    schema_name: ClassVar[str] = "decision"

    decision_id: str
    decision_type: DecisionType
    actor: Literal["supervisor"]
    timestamp: str
    affected_refs: list[str]
    rationale: str
    evidence_refs: list[str] = field(default_factory=list)
    analysis_refs: list[str] = field(default_factory=list)
    previous_version_refs: list[str] = field(default_factory=list)
    resulting_version_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HumanGate(CoreModel):
    schema_name: ClassVar[str] = "human-gate"

    gate_id: str
    gate_type: GateType
    status: GateStatus
    trigger: str
    affected_refs: list[str]
    requested_decision: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    default_safe_action: str | None = None


@dataclass(frozen=True)
class ProjectEvent(CoreModel):
    schema_name: ClassVar[str] = "event"
    # Schema key "from" is a Python keyword; serialized under the alias.
    _FIELD_ALIASES: ClassVar[dict[str, str]] = {"from_": "from"}

    event_id: str
    timestamp: str
    actor: str
    event_type: str
    object_id: str | None = None
    run_id: str | None = None
    from_: str | None = None
    to: str | None = None
    reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutomaticRetryPolicy(CoreModel):
    schema_name: ClassVar[str] = "retry-policy"

    policy_id: str
    allowed_engineering_failures: list[str]
    supervisor_required_changes: list[str]
    max_identical_retries: int | None = None
    invalidate_run_on: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReproductionRequirement(CoreModel):
    schema_name: ClassVar[str] = "requirement"

    requirement_id: str
    statement: str
    inventory_items: list[str]
    criticality: Criticality
    goal_ids: list[str]
    outcome: RequirementOutcome
    criticality_assessment_ref: str | None = None
    method_reproducibility: MethodReproducibility | None = None


@dataclass(frozen=True)
class ArtifactManifest(CoreModel):
    schema_name: ClassVar[str] = "artifact-manifest"

    artifact_id: str
    uri: str
    sha256: str
    size_bytes: int
    created_at: str
    run_id: str | None = None
    analysis_id: str | None = None
    mime_type: str | None = None
    producer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcceptanceCriteria(CoreModel):
    schema_name: ClassVar[str] = "acceptance-criteria"

    acceptance_id: str
    goal_id: str
    version: str
    frozen: bool
    decision_mode: DecisionMode
    criteria: list[dict[str, Any]]
    target: Any = None
    statistical_design_ref: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str | None = None
    confidence: Confidence | None = None
    revision_reason: str | None = None


@dataclass(frozen=True)
class StatisticalDesign(CoreModel):
    """The frozen statistical design of a goal (07-STATISTICS-AND-ACCEPTANCE.md
    SS9: the design -- target metrics, equivalence margin, replication
    design, primary statistical method, alpha/confidence level,
    preprocessing/exclusion criteria, outlier rules, failed-Run handling --
    is frozen BEFORE data generation).

    The first-class record behind ``AcceptanceCriteria.statistical_design_ref``
    (``schemas/statistical-design.schema.yaml``): one record per goal,
    registered in the goal-contract family registry (``planning.plan``
    ``register_statistical_design``, ``designs/<design_id>.json``) and
    frozen by the plan freeze. ``margin_basis`` records the SS8 basis
    category of the recorded margin (target paper error, reproduction
    literature, instrument uncertainty, domain threshold, or an explicit
    scientific judgment) so margin provenance is machine-checkable.
    """

    schema_name: ClassVar[str] = "statistical-design"

    design_id: str
    goal_id: str
    version: str
    frozen: bool
    metrics: list[str]
    replication: GoalReplication
    primary_method: str
    margin: Any = None
    margin_basis: MarginBasis | None = None
    alpha: float | None = None
    confidence_level: float | None = None
    preprocessing_exclusion_rules: list[str] = field(default_factory=list)
    outlier_rules: list[str] = field(default_factory=list)
    failed_run_handling: str | None = None
    evidence_refs: list[str] = field(default_factory=list)
    rationale: str | None = None
    revision_reason: str | None = None


@dataclass(frozen=True)
class ResearchRequest(CoreModel):
    schema_name: ClassVar[str] = "research-request"

    request_id: str
    requested_by: Literal["supervisor"]
    question: str
    origin_refs: list[str]
    status: ResearchRequestStatus
    required_search_families: list[str] = field(default_factory=list)
    minimum_reliability: int | None = None
    minimum_directness: int | None = None
    result_evidence_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry: schema stem (schemas/<name>.schema.yaml) -> model class
# ---------------------------------------------------------------------------

MODEL_REGISTRY: dict[str, type[CoreModel]] = {
    "project": Project,
    "run": Run,
    "plan": Plan,
    "goal": GoalContract,
    "evidence": ClaimSpecificEvidence,
    "assumption": Assumption,
    "closure-contract": ClosureContract,
    "inventory-item": ReproductionInventoryItem,
    "resource": Resource,
    "source": ResearchSource,
    "worker-context": GoalExecutionContextPackage,
    "lab-execution-package": LabExecutionPackage,
    "analysis": AnalysisProtocolOrResult,
    "decision": SupervisorDecision,
    "human-gate": HumanGate,
    "event": ProjectEvent,
    "retry-policy": AutomaticRetryPolicy,
    "requirement": ReproductionRequirement,
    "artifact-manifest": ArtifactManifest,
    "acceptance-criteria": AcceptanceCriteria,
    "statistical-design": StatisticalDesign,
    "research-request": ResearchRequest,
}

SCHEMA_NAMES: tuple[str, ...] = tuple(MODEL_REGISTRY)
