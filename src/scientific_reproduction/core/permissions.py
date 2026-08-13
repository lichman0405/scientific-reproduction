"""Frozen role-action permission matrix with ruleset versioning (DEV-M6-G03).

Implements the **role-action matrix** deliverable of DEV-M6-G03: the
deterministic, pure authority model that decides, for every (role,
action) pair of the frozen vocabulary, whether the action is permitted --
the normative core the runtime guards of ``workers/permissions.py``
enforce. Grounded in:

* ``03-ROLE-AND-PERMISSION-SPEC.md`` SS1: *"Workers report facts.
  Supervisor makes scientific decisions."* -- the governance principle
  the matrix encodes: no worker may lower acceptance criteria,
  reinterpret a failure into a pass, or silently modify a scientific
  protocol;
* ``03-ROLE-AND-PERMISSION-SPEC.md`` SS2-SS8 -- the per-role "may" /
  "may not" lists: the Supervisor alone may create/modify/version formal
  Goals, freeze Plan versions, define acceptance criteria and primary
  analysis protocols, classify Requirements, decide
  strict/recovery/redesign transitions, close Goals/Requirements and
  assign final outcomes (SS2); Research is a project-persistent evidence
  service that searches/acquires/indexes sources and executes evidence
  checklists but may not change Goals, change acceptance criteria,
  decide Recovery actions or directly dispatch Workers (SS3); the
  Execution Monitor inspects Runs, maintains heartbeat/checkpoint/event
  records and executes preauthorized engineering retries but may not
  change scientific parameters, classify a Goal as scientifically
  PASS/FAIL or enter Recovery autonomously (SS4); the Experiment /
  Computation / Analysis / Diagnosis Workers execute the frozen work and
  report facts but may not create Goals or decide PASS/FAIL (SS5-SS8);
* ``core/models.py`` -- the frozen vocabulary: ``WorkerRole``
  (``experiment_worker`` / ``computation_worker`` / ``analysis_worker`` /
  ``diagnosis_worker``) grounds the four worker roles 1:1
  (:func:`role_from_worker_role`); ``DecisionType`` (``PLAN_FREEZE``
  through ``PROJECT_OUTCOME``) grounds the eleven Supervisor-decision
  actions 1:1 (:func:`action_for_decision_type`); ``SupervisorDecision``
  is the record those decisions produce -- it exists only where the
  matrix grants the corresponding action;
* ``05-GOAL-RUN-SCHEMA.md`` SS7: *"Scientific PASS/FAIL is not a Run
  lifecycle state; it is a review decision stored separately"* -- the
  basis of the ``SCIENTIFIC_INTERPRETATION`` action: a PASS/FAIL
  classification is a decision, never a worker/monitor observation;
* ``11-COMPUTATION-SUBSYSTEM.md`` SS5 -- the engineering/scientific
  retry split (``ENGINEERING_RETRY`` vs the Supervisor-required
  parameter changes) and ``09-RESEARCH-SUBSYSTEM.md`` SS3 ("Only
  Supervisor may issue formal Research Requests").

Rule model (determinism)
------------------------
The matrix is the ordered, versioned ``PERMISSION_RULES`` table: first
match wins; the trailing total default ``R-PRM-D1`` denies every
(role, action) pair no earlier rule granted (least privilege -- the
"may not" lists of SS3-SS8 need no rule of their own, they are denials
by default). The Supervisor rule ``R-PRM-SUP1`` heads the table (SS2:
the Supervisor holds every governance action); the Research / Monitor /
worker rules grant exactly the "may" lists of SS3-SS8. Evaluation is a
pure function of the :class:`PermissionInput`: no randomness, no wall
clock, no network, no file I/O. ``matched_rule_id`` is recorded and
post-asserted in every assessment and can never be ``None``; every
assessment records the ``ROLE_ACTION_RULESET_VERSION`` so old decisions
stay interpretable when the table is versioned. The table is frozen:
the module-level tuple of frozen ``PermissionRule`` dataclasses and the
per-role allowed-action ``frozenset`` constants cannot be mutated, and
:func:`validate_permission_ruleset` checks a table's integrity (unique
rule ids, non-empty, total default last) before it is trusted.

Determinism and boundaries
--------------------------
``TypeError`` at the public boundaries (never ``ValueError`` for wrong
types); value/rule violations follow the ``ValueError``-subclass
convention with stable one-line messages; ``from __future__ import
annotations``; ``__all__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Sequence

from scientific_reproduction.core.models import DecisionType, WorkerRole

__all__ = [
    "ACTION_ORDER",
    "Action",
    "ANALYSIS_WORKER_ACTIONS",
    "COMPUTATION_WORKER_ACTIONS",
    "DIAGNOSIS_WORKER_ACTIONS",
    "EXPERIMENT_WORKER_ACTIONS",
    "MONITOR_ACTIONS",
    "PERMISSION_RULES",
    "ROLE_ACTION_RULESET_VERSION",
    "ROLE_ORDER",
    "RESEARCH_ACTIONS",
    "Role",
    "RolePermissionError",
    "PermissionAssessment",
    "PermissionDecision",
    "PermissionDeniedError",
    "PermissionInput",
    "PermissionRule",
    "PermissionRulesetError",
    "WORKER_COMMON_ACTIONS",
    "action_for_decision_type",
    "check_action_allowed",
    "is_action_allowed",
    "role_from_worker_role",
    "validate_permission_ruleset",
]

# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class RolePermissionError(ValueError):
    """Base class for all role-permission errors."""


class PermissionDeniedError(RolePermissionError):
    """Raised when a role attempts an action the role-action matrix denies.

    ``assessment`` carries the full decision record (input, matched
    rule, ruleset version) so the caller can persist the audit trail.
    """

    def __init__(self, message: str, assessment: PermissionAssessment) -> None:
        super().__init__(message)
        self.assessment: PermissionAssessment = assessment


class PermissionRulesetError(RolePermissionError):
    """Raised when a role-action rule table violates the frozen ruleset shape.

    Covers empty tables, duplicate rule ids and a trailing rule that is
    not a total default (stable messages naming the violation).
    """


# ---------------------------------------------------------------------------
# Frozen constants
# ---------------------------------------------------------------------------

#: Version of the role-action permission rule table; recorded in every
#: assessment so old decisions stay interpretable. Bumped whenever a rule
#: changes.
ROLE_ACTION_RULESET_VERSION: str = "1.0"


class Role(StrEnum):
    """The frozen role vocabulary of the permission matrix.

    The four worker members map 1:1 onto ``core.models.WorkerRole``
    (verbatim values, :func:`role_from_worker_role`); ``SUPERVISOR``
    grounds ``core.models.SupervisorDecision.actor`` ("supervisor") and
    ``03-ROLE-AND-PERMISSION-SPEC.md`` SS2; ``RESEARCH`` and ``MONITOR``
    ground the SS3 / SS4 role descriptions (no model enum exists for
    them -- the role spec is the frozen vocabulary).
    """

    SUPERVISOR = "supervisor"
    RESEARCH = "research"
    MONITOR = "execution_monitor"
    EXPERIMENT_WORKER = "experiment_worker"
    COMPUTATION_WORKER = "computation_worker"
    ANALYSIS_WORKER = "analysis_worker"
    DIAGNOSIS_WORKER = "diagnosis_worker"


#: The normative role order of the matrix (``03-ROLE-AND-PERMISSION-SPEC.md``
#: SS2-SS8): Supervisor, Research, Monitor, then the four worker roles.
ROLE_ORDER: tuple[Role, ...] = tuple(Role)


class Action(StrEnum):
    """The frozen action vocabulary of the permission matrix.

    The eleven decision actions mirror ``core.models.DecisionType``
    values verbatim (:func:`action_for_decision_type`); the remaining
    actions ground the mutation surfaces and the "may" lists of
    ``03-ROLE-AND-PERMISSION-SPEC.md`` SS2-SS8.
    """

    # Supervisor-decision actions -- one per core.models.DecisionType member.
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
    # Goal-family mutation actions (SS2 "create, modify and version formal
    # Goals"; SS5/SS6 workers may not "create Goals").
    GOAL_CREATE = "GOAL_CREATE"
    GOAL_MUTATE = "GOAL_MUTATE"
    FROZEN_GOAL_MUTATE = "FROZEN_GOAL_MUTATE"
    RECOVERY_GOAL_CREATE = "RECOVERY_GOAL_CREATE"
    # Requirement closure (SS2 "close Goals/Requirements").
    REQUIREMENT_CLOSE = "REQUIREMENT_CLOSE"
    # Worker dispatch (SS3 research "may not ... directly dispatch Workers").
    WORKER_DISPATCH = "WORKER_DISPATCH"
    # Scientific interpretation (05 SS7 PASS/FAIL is a review decision
    # stored separately; SS4 monitor may not "classify a Goal as
    # scientifically PASS/FAIL").
    SCIENTIFIC_INTERPRETATION = "SCIENTIFIC_INTERPRETATION"
    # Scientific parameter / statistical design changes (11-COMPUTATION
    # SS5 Supervisor-required changes; SS4 monitor may not "change
    # scientific parameters" / "alter statistical design").
    SCIENTIFIC_PARAMETER_CHANGE = "SCIENTIFIC_PARAMETER_CHANGE"
    STATISTICAL_DESIGN_ALTER = "STATISTICAL_DESIGN_ALTER"
    # Research activities (SS3 "may" list).
    SOURCE_SEARCH = "SOURCE_SEARCH"
    SOURCE_ACQUIRE = "SOURCE_ACQUIRE"
    SOURCE_RECORD_CREATE = "SOURCE_RECORD_CREATE"
    EVIDENCE_EXTRACT = "EVIDENCE_EXTRACT"
    EVIDENCE_CHECKLIST = "EVIDENCE_CHECKLIST"
    EVIDENCE_ASSESS = "EVIDENCE_ASSESS"
    SATURATION_RECORD = "SATURATION_RECORD"
    RESEARCH_REQUEST_RESPOND = "RESEARCH_REQUEST_RESPOND"
    # Monitor activities (SS4 "may" list).
    RUN_STATUS_INSPECT = "RUN_STATUS_INSPECT"
    RUN_LIFECYCLE_TRANSITION = "RUN_LIFECYCLE_TRANSITION"
    RESULT_PACKAGE_VALIDATE = "RESULT_PACKAGE_VALIDATE"
    ENGINEERING_RETRY = "ENGINEERING_RETRY"
    FOLLOWUP_WORKER_SPAWN = "FOLLOWUP_WORKER_SPAWN"
    EVENT_RECORD_MAINTAIN = "EVENT_RECORD_MAINTAIN"
    MONITOR_RESUME = "MONITOR_RESUME"
    # Worker activities (SS5/SS6/SS7/SS8 "may" lists).
    CONTEXT_READ = "CONTEXT_READ"
    EXECUTION_PACKAGE_PREPARE = "EXECUTION_PACKAGE_PREPARE"
    METADATA_RECORD = "METADATA_RECORD"
    RESULT_INGEST = "RESULT_INGEST"
    ARTIFACT_REGISTER = "ARTIFACT_REGISTER"
    DEVIATION_REPORT = "DEVIATION_REPORT"
    RUN_PREPARE = "RUN_PREPARE"
    FACT_REPORT = "FACT_REPORT"
    ANALYSIS_EXECUTE = "ANALYSIS_EXECUTE"
    DIAGNOSIS_REPORT = "DIAGNOSIS_REPORT"


#: The normative action order of the matrix (enum definition order).
ACTION_ORDER: tuple[Action, ...] = tuple(Action)

#: Actions granted to the Research role (``03-ROLE-AND-PERMISSION-SPEC.md``
#: SS3 "may" list): the evidence-service activities.
RESEARCH_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.SOURCE_SEARCH,
        Action.SOURCE_ACQUIRE,
        Action.SOURCE_RECORD_CREATE,
        Action.EVIDENCE_EXTRACT,
        Action.EVIDENCE_CHECKLIST,
        Action.EVIDENCE_ASSESS,
        Action.SATURATION_RECORD,
        Action.RESEARCH_REQUEST_RESPOND,
    }
)

#: Actions granted to the Execution Monitor role (SS4 "may" list):
#: observation/operation only -- never scientific decisions.
MONITOR_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.RUN_STATUS_INSPECT,
        Action.RUN_LIFECYCLE_TRANSITION,
        Action.RESULT_PACKAGE_VALIDATE,
        Action.ENGINEERING_RETRY,
        Action.FOLLOWUP_WORKER_SPAWN,
        Action.EVENT_RECORD_MAINTAIN,
        Action.MONITOR_RESUME,
    }
)

#: Actions common to every worker role (SS5-SS8 "may" lists): read the
#: context, record facts/metadata, register artifacts, report deviations.
WORKER_COMMON_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.CONTEXT_READ,
        Action.METADATA_RECORD,
        Action.RESULT_INGEST,
        Action.ARTIFACT_REGISTER,
        Action.DEVIATION_REPORT,
    }
)

#: Actions granted to the Experiment Worker role (SS5 "may" list).
EXPERIMENT_WORKER_ACTIONS: frozenset[Action] = (
    WORKER_COMMON_ACTIONS
    | frozenset({Action.EXECUTION_PACKAGE_PREPARE, Action.ENGINEERING_RETRY})
)

#: Actions granted to the Computation Worker role (SS6: same governance as
#: the Experiment Worker, plus the computation-specific fact reporting).
COMPUTATION_WORKER_ACTIONS: frozenset[Action] = (
    WORKER_COMMON_ACTIONS
    | frozenset(
        {
            Action.EXECUTION_PACKAGE_PREPARE,
            Action.RUN_PREPARE,
            Action.FACT_REPORT,
            Action.ENGINEERING_RETRY,
        }
    )
)

#: Actions granted to the Analysis Worker role (SS7 "may" list).
ANALYSIS_WORKER_ACTIONS: frozenset[Action] = (
    WORKER_COMMON_ACTIONS | frozenset({Action.ANALYSIS_EXECUTE})
)

#: Actions granted to the Diagnosis Worker role (SS8 "may" list).
DIAGNOSIS_WORKER_ACTIONS: frozenset[Action] = (
    WORKER_COMMON_ACTIONS | frozenset({Action.DIAGNOSIS_REPORT})
)


# ---------------------------------------------------------------------------
# The ordered rule table (first match wins, total default)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionInput:
    """The state a permission decision is a pure function of.

    ``role`` identifies the caller, ``action`` the attempted action;
    the decision is a pure function of these two inputs only.
    """

    role: Role
    action: Action

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            raise TypeError(
                f"PermissionInput.role must be a Role member, got"
                f" {type(self.role).__name__}"
            )
        if not isinstance(self.action, Action):
            raise TypeError(
                f"PermissionInput.action must be an Action member, got"
                f" {type(self.action).__name__}"
            )


@dataclass(frozen=True)
class PermissionRule:
    """One entry of the ordered role-action permission rule table."""

    rule_id: str
    description: str
    allowed: bool
    predicate: Callable[[PermissionInput], bool]

    def __post_init__(self) -> None:
        for field_name, value in (
            ("rule_id", self.rule_id),
            ("description", self.description),
        ):
            if not isinstance(value, str):
                raise TypeError(
                    f"PermissionRule.{field_name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise PermissionRulesetError(
                    f"PermissionRule.{field_name} must be a non-empty"
                    f" string, got {value!r}"
                )
        if not isinstance(self.allowed, bool):
            raise TypeError(
                f"PermissionRule.allowed must be a bool, got"
                f" {type(self.allowed).__name__}"
            )
        if not callable(self.predicate):
            raise TypeError(
                "PermissionRule.predicate must be callable, got"
                f" {type(self.predicate).__name__}"
            )


@dataclass(frozen=True)
class PermissionDecision:
    """Record of one rule-table evaluation for a given (role, action) pair."""

    rule_id: str
    description: str
    allowed: bool
    matched: bool


#: The ordered role-action permission rule table. First match wins; order
#: is normative. The Supervisor rule heads the table (03-ROLE-AND-PERMISSION-SPEC.md
#: SS2: the Supervisor holds every governance action); the Research /
#: Monitor / worker rules grant exactly the "may" lists of SS3-SS8; the
#: trailing total default ``R-PRM-D1`` denies everything no rule granted
#: (least privilege -- the "may not" lists are denials by default).
PERMISSION_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        rule_id="R-PRM-SUP1",
        description=(
            "the caller is the Supervisor: every governance action is"
            " permitted (03-ROLE-AND-PERMISSION-SPEC.md SS2 -- the"
            " Supervisor alone creates/modifies/versions formal Goals,"
            " freezes Plans, defines acceptance criteria and primary"
            " analysis protocols, decides transitions, closes"
            " Goals/Requirements and assigns final outcomes)"
        ),
        allowed=True,
        predicate=lambda i: i.role is Role.SUPERVISOR,
    ),
    PermissionRule(
        rule_id="R-PRM-RES1",
        description=(
            "the caller is Research and the action is a research activity"
            " (03-ROLE-AND-PERMISSION-SPEC.md SS3 'may' list -- search/"
            " acquire/index sources, extract and assess evidence, record"
            " saturation cycles, respond to Supervisor Research Requests):"
            " permit"
        ),
        allowed=True,
        predicate=lambda i: (
            i.role is Role.RESEARCH and i.action in RESEARCH_ACTIONS
        ),
    ),
    PermissionRule(
        rule_id="R-PRM-MON1",
        description=(
            "the caller is the Execution Monitor and the action is a"
            " monitor activity (03-ROLE-AND-PERMISSION-SPEC.md SS4 'may'"
            " list -- inspect Runs, transition operational lifecycle,"
            " validate result packages, preauthorized engineering"
            " retries, spawn follow-up workers, maintain event records,"
            " resume itself): permit"
        ),
        allowed=True,
        predicate=lambda i: (
            i.role is Role.MONITOR and i.action in MONITOR_ACTIONS
        ),
    ),
    PermissionRule(
        rule_id="R-PRM-EW1",
        description=(
            "the caller is an Experiment Worker and the action is an"
            " experiment worker activity (03-ROLE-AND-PERMISSION-SPEC.md"
            " SS5 'may' list -- read the context package, prepare the"
            " Experiment Execution Package, record metadata, ingest"
            " result packages, register artifacts, report deviations,"
            " whitelisted engineering retries): permit"
        ),
        allowed=True,
        predicate=lambda i: (
            i.role is Role.EXPERIMENT_WORKER
            and i.action in EXPERIMENT_WORKER_ACTIONS
        ),
    ),
    PermissionRule(
        rule_id="R-PRM-CW1",
        description=(
            "the caller is a Computation Worker and the action is a"
            " computation worker activity (03-ROLE-AND-PERMISSION-SPEC.md"
            " SS6: same governance as the Experiment Worker, plus"
            " materializing the frozen scientific input, preparing and"
            " submitting Runs and reporting convergence/runtime facts):"
            " permit"
        ),
        allowed=True,
        predicate=lambda i: (
            i.role is Role.COMPUTATION_WORKER
            and i.action in COMPUTATION_WORKER_ACTIONS
        ),
    ),
    PermissionRule(
        rule_id="R-PRM-AW1",
        description=(
            "the caller is an Analysis Worker and the action is an"
            " analysis worker activity (03-ROLE-AND-PERMISSION-SPEC.md"
            " SS7 'may' list -- run the frozen Primary Analysis Protocol,"
            " calculate statistics, produce diagnostics and the Analysis"
            " Result Package): permit"
        ),
        allowed=True,
        predicate=lambda i: (
            i.role is Role.ANALYSIS_WORKER
            and i.action in ANALYSIS_WORKER_ACTIONS
        ),
    ),
    PermissionRule(
        rule_id="R-PRM-DW1",
        description=(
            "the caller is a Diagnosis Worker and the action is a"
            " diagnosis worker activity (03-ROLE-AND-PERMISSION-SPEC.md"
            " SS8 'may' list -- inspect failed/abnormal Runs, identify"
            " failure patterns, evaluate and rank candidate causes,"
            " report missing information): permit"
        ),
        allowed=True,
        predicate=lambda i: (
            i.role is Role.DIAGNOSIS_WORKER
            and i.action in DIAGNOSIS_WORKER_ACTIONS
        ),
    ),
    PermissionRule(
        rule_id="R-PRM-D1",
        description=(
            "the (role, action) pair was not granted by any earlier rule:"
            " deny (least privilege -- the 'may not' lists of"
            " 03-ROLE-AND-PERMISSION-SPEC.md SS3-SS8 are denials by"
            " default, AC-01/AC-02/AC-03/AC-04)"
        ),
        allowed=False,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class PermissionAssessment:
    """Full, auditable result of one role-action decision.

    ``input`` is the exact (role, action) the decision was computed
    from; ``decisions`` records the outcome of every rule in the table
    (in evaluation order); ``matched_rule_id`` names the deciding rule
    (``None`` is impossible: the trailing total default ``R-PRM-D1``
    always matches); ``ruleset_version`` records the rule table version
    (``ROLE_ACTION_RULESET_VERSION``).
    """

    input: PermissionInput
    allowed: bool
    decisions: tuple[PermissionDecision, ...]
    matched_rule_id: str
    ruleset_version: str = ROLE_ACTION_RULESET_VERSION


# ---------------------------------------------------------------------------
# Evaluation (pure and deterministic)
# ---------------------------------------------------------------------------


def check_action_allowed(role: Role, action: Action) -> PermissionAssessment:
    """Decide whether ``role`` may perform ``action``, by the rule table.

    Pure and deterministic: the decision is a pure function of the
    (role, action) pair, decided by the ordered ``PERMISSION_RULES``
    table (first match wins; the trailing total default ``R-PRM-D1``
    denies anything no rule granted). The full assessment records every
    rule evaluation, the matched rule id and the ruleset version.

    Raises:
        TypeError: ``role`` is not a ``Role`` member, or ``action`` is
            not an ``Action`` member.
    """
    if not isinstance(role, Role):
        raise TypeError(f"role must be a Role member, got {type(role).__name__}")
    if not isinstance(action, Action):
        raise TypeError(
            f"action must be an Action member, got {type(action).__name__}"
        )
    permission_input = PermissionInput(role=role, action=action)
    decisions: list[PermissionDecision] = []
    matched_rule_id: str | None = None
    matched_allowed = False  # unreachable default
    for rule in PERMISSION_RULES:
        matched = rule.predicate(permission_input)
        decisions.append(
            PermissionDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                allowed=rule.allowed,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_allowed = rule.allowed
    # R-PRM-D1 (the total default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return PermissionAssessment(
        input=permission_input,
        allowed=matched_allowed,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


def is_action_allowed(role: Role, action: Action) -> bool:
    """Return True iff the matrix permits ``role`` to perform ``action``.

    Convenience view of :func:`check_action_allowed` (same boundary:
    ``TypeError`` for non-``Role``/non-``Action`` inputs).
    """
    return check_action_allowed(role, action).allowed


# ---------------------------------------------------------------------------
# Vocabulary bridges (grounding in core.models)
# ---------------------------------------------------------------------------


def action_for_decision_type(decision_type: DecisionType) -> Action:
    """Map one Supervisor decision type to its matrix action (1:1).

    Every ``core.models.DecisionType`` member maps to the ``Action``
    member with the verbatim value (``PLAN_FREEZE`` -> ``Action.PLAN_FREEZE``,
    ..., ``PROJECT_OUTCOME`` -> ``Action.PROJECT_OUTCOME``).

    Raises:
        TypeError: ``decision_type`` is not a ``DecisionType`` member.
    """
    if not isinstance(decision_type, DecisionType):
        raise TypeError(
            "decision_type must be a DecisionType member, got"
            f" {type(decision_type).__name__}"
        )
    return Action(decision_type.value)


_ROLE_BY_WORKER_ROLE: dict[WorkerRole, Role] = {
    WorkerRole.EXPERIMENT_WORKER: Role.EXPERIMENT_WORKER,
    WorkerRole.COMPUTATION_WORKER: Role.COMPUTATION_WORKER,
    WorkerRole.ANALYSIS_WORKER: Role.ANALYSIS_WORKER,
    WorkerRole.DIAGNOSIS_WORKER: Role.DIAGNOSIS_WORKER,
}


def role_from_worker_role(worker_role: WorkerRole) -> Role:
    """Map one ``core.models.WorkerRole`` member to its matrix role (1:1).

    The four worker roles of the matrix are grounded in the frozen
    ``core.models.WorkerRole`` vocabulary with verbatim values.

    Raises:
        TypeError: ``worker_role`` is not a ``WorkerRole`` member.
    """
    if not isinstance(worker_role, WorkerRole):
        raise TypeError(
            f"worker_role must be a WorkerRole member, got"
            f" {type(worker_role).__name__}"
        )
    return _ROLE_BY_WORKER_ROLE[worker_role]


# ---------------------------------------------------------------------------
# Ruleset integrity (frozen, complete, total)
# ---------------------------------------------------------------------------


def validate_permission_ruleset(
    rules: Sequence[PermissionRule] | None = None,
) -> tuple[str, ...]:
    """Validate a role-action rule table's integrity; return its rule ids.

    The frozen module table ``PERMISSION_RULES`` is validated by default;
    an explicit candidate table can be passed (e.g. a versioned
    replacement). A valid table is non-empty, has unique rule ids, and
    its trailing rule matches every (role, action) pair of the frozen
    product -- the total default that guarantees first-match evaluation
    is total (a decision always exists). ``check_action_allowed``
    post-asserts this invariant; this validator surfaces it loudly and
    early.

    Raises:
        TypeError: ``rules`` is neither a sequence of ``PermissionRule``
            nor None, or an entry is not a ``PermissionRule``.
        PermissionRulesetError: the table is empty, carries duplicate
            rule ids, or its trailing rule is not a total default
            (stable messages).
    """
    table = PERMISSION_RULES if rules is None else rules
    if not isinstance(table, Sequence) or isinstance(table, (str, bytes)):
        raise TypeError(
            "rules must be a sequence of PermissionRule or None, got"
            f" {type(table).__name__}"
        )
    rules_tuple = tuple(table)
    for rule in rules_tuple:
        if not isinstance(rule, PermissionRule):
            raise TypeError(
                "permission rule table entries must be PermissionRule"
                f" instances, got {type(rule).__name__}"
            )
    if not rules_tuple:
        raise PermissionRulesetError(
            "the role-action rule table must not be empty: at least the"
            " total default rule is required"
        )
    rule_ids = tuple(rule.rule_id for rule in rules_tuple)
    duplicates = sorted(
        {rule_id for rule_id in rule_ids if rule_ids.count(rule_id) > 1}
    )
    if duplicates:
        raise PermissionRulesetError(
            "duplicate rule id(s) in the role-action rule table:"
            f" {', '.join(duplicates)}"
        )
    default_rule = rules_tuple[-1]
    for role in ROLE_ORDER:
        for action in ACTION_ORDER:
            if not default_rule.predicate(PermissionInput(role, action)):
                raise PermissionRulesetError(
                    f"the trailing rule {default_rule.rule_id!r} is not a"
                    f" total default: it does not match role {role.value!r}"
                    f" action {action.value!r}"
                )
    return rule_ids
