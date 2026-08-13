"""Runtime agent role contracts -- platform-neutral descriptors (DEV-M10-G01).

Turn the locked role specification (``03-ROLE-AND-PERMISSION-SPEC.md``
SS1-SS9) into reusable, data-driven role contract records that platform
adapters (Claude Code, Codex) can serialize, validate and turn into
agent prompts. This module is deliberately a pure descriptor layer: no
runtime execution, no file I/O, no wall clock, no randomness -- only
frozen, typed, canonical records.

Grounding (AC-01 -- authority boundaries match the locked role spec)
--------------------------------------------------------------------
The frozen role-action permission matrix (``core.permissions.py``,
DEV-M6-G03) is the normative encoding of the locked spec's "may" /
"may not" lists. Every :class:`RoleContract` therefore declares its
authority in the *matrix vocabulary* (``core.permissions.Action``
members) and the *typed authority fields*:

* ``allowed_actions`` / ``forbidden_actions`` -- the spec's "may" and
  "may not" lists, expressed as matrix actions. ``validate_role_contracts``
  proves consistency with the matrix: an allowed action must be granted
  to the contract's matrix role(s), a forbidden action must be denied to
  them (least privilege, ``R-PRM-D1``);
* ``decision_authority`` -- who may make scientific Plan/Goal decisions
  (SS2: the Supervisor alone; everyone else ``NONE``);
* ``verdict_authority`` -- who may declare scientific PASS/FAIL and
  assign outcomes (SS4/SS5/SS7: workers and the Monitor report facts
  only; verdicts are Supervisor review decisions, 05-GOAL-RUN-SCHEMA.md
  SS7);
* ``retry_authority`` -- the resubmission boundary (SS2: the Supervisor
  decides retries beyond preauthorized engineering retries; SS4: the
  Monitor executes preauthorized engineering retries; SS5/SS6: workers
  may only use whitelisted engineering retries).

Core state as truth (AC-02)
---------------------------
Every contract names its truth sources as Core state APIs, never an
LLM's remembered conversation: the state backend
(``core.state_backend.StateBackend`` -- run records, plan/goal/analysis
objects, durable state) and the append-only project event log
(``core.events.ProjectEventLog``). ``state_object_types`` lists the
normative object types (``core.models.SCHEMA_NAMES``) the role reads or
writes; ``state_truth_rule`` states the AC-02 rule in prompt-facing
text. The serialized contract carries no conversation-memory reference.

Worker boundaries (AC-03)
-------------------------
The Worker contract forbids plan mutation (``PLAN_FREEZE``,
``GOAL_REVISION``, ``ACCEPTANCE_REVISION``, ``ANALYSIS_PROTOCOL_REVISION``,
the Goal-family mutations and the practice token ``plan_mutation``) and
self-acceptance (the practice tokens ``self_acceptance``,
``self_review``, ``self_merge`` and the prompt prohibition "never
accept your own output") -- a Worker reports facts and evidence; the
Supervisor accepts.

Determinism
-----------
Each contract carries a deterministic ``contract_id``
(``core.ids.generate_id("role_contract", role_id, contract_version)``),
a stable ``contract_version``, canonical ``to_dict`` / ``from_dict`` /
``to_json`` serialization (sorted, byte-identical for equal records),
``TypeError`` at the public boundaries and ``ValueError``-subclassed
errors with stable one-line messages -- the house conventions of the
adapter modules (``adapters/lab/base.py``, ``adapters/research/*``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import SCHEMA_NAMES, DecisionType
from scientific_reproduction.core.permissions import (
    ACTION_ORDER,
    MONITOR_ACTIONS,
    RESEARCH_ACTIONS,
    WORKER_COMMON_ACTIONS,
    Action,
    Role,
    action_for_decision_type,
    is_action_allowed,
)

__all__ = [
    "CONTRACT_ROLE_IDS",
    "CORE_STATE_API_PATHS",
    "DECISION_ACTIONS",
    "CoreStateApi",
    "DecisionAuthority",
    "RetryAuthority",
    "RoleContract",
    "RoleContractError",
    "ROLE_CONTRACTS",
    "ROLE_CONTRACTS_VERSION",
    "STATE_TRUTH_RULE",
    "UnknownRoleContractError",
    "VERDICT_ACTIONS",
    "contract_to_matrix_roles",
    "get_role_contract",
    "validate_role_contracts",
]

#: Version of the role contract descriptors. Bumped whenever a contract
#: rule or vocabulary changes; every contract record carries it and every
#: generated ``contract_id`` is a function of it.
ROLE_CONTRACTS_VERSION: str = "1.0"

#: The frozen role vocabulary of the contract layer (SS2-SS8): the four
#: role contracts this goal delivers. "worker" is the collective contract
#: of the four worker roles of ``core.permissions.Role``.
CONTRACT_ROLE_IDS: tuple[str, ...] = (
    "supervisor",
    "research",
    "execution_monitor",
    "worker",
)

#: The AC-02 truth rule, in prompt-facing text: Core state is the only
#: truth source; an agent's remembered conversation context is not.
STATE_TRUTH_RULE: str = (
    "The authoritative record of project truth is Core state: the state"
    " backend (run records, plan/goal/analysis objects, durable state) and"
    " the append-only project event log. An agent's memory or remembered"
    " conversation is never a truth source."
)

#: The Supervisor-decision actions -- the eleven ``DecisionType`` members
#: as matrix actions (SS2: the Supervisor alone decides them).
DECISION_ACTIONS: frozenset[Action] = frozenset(
    action_for_decision_type(decision_type) for decision_type in DecisionType
)

#: The verdict actions: classifying a Goal as scientifically PASS/FAIL
#: (05-GOAL-RUN-SCHEMA.md SS7), reviewing Goal acceptance and assigning the
#: final project outcome -- Supervisor review decisions, never worker or
#: Monitor observations (SS4/SS5/SS7).
VERDICT_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.SCIENTIFIC_INTERPRETATION,
        Action.GOAL_REVIEW,
        Action.PROJECT_OUTCOME,
    }
)

#: Valid ``role_id`` shape: a safe lowercase identifier usable as a file
#: stem, URL segment or lookup key on every platform.
_ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Valid practice-token shape (same safe identifier contract).
_PRACTICE_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

#: Valid version shape (``major.minor``, no leading zeros constraints).
_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class RoleContractError(ValueError):
    """Base error of the role contract layer (stable, one-line messages)."""


class UnknownRoleContractError(RoleContractError):
    """Raised by ``get_role_contract`` for a role id outside the frozen set."""


# ---------------------------------------------------------------------------
# Core state API vocabulary (AC-02)
# ---------------------------------------------------------------------------


class CoreStateApi(StrEnum):
    """The Core state APIs a contract may name as truth sources (AC-02).

    Only these two members exist -- the state backend (persisted run
    records, plan/goal/analysis objects, durable state) and the append-only
    project event log. A contract that names any other data source must be
    grounded in ``state_object_types`` (``core.models.SCHEMA_NAMES``)
    instead of an agent's memory.
    """

    STATE_BACKEND = "state_backend"
    EVENT_LOG = "event_log"


#: Import path of each Core state API member, for audit and tests.
CORE_STATE_API_PATHS: dict[CoreStateApi, str] = {
    CoreStateApi.STATE_BACKEND: "scientific_reproduction.core.state_backend.StateBackend",
    CoreStateApi.EVENT_LOG: "scientific_reproduction.core.events.ProjectEventLog",
}


# ---------------------------------------------------------------------------
# Authority vocabulary (AC-01)
# ---------------------------------------------------------------------------


class DecisionAuthority(StrEnum):
    """Who may make scientific Plan/Goal decisions (SS2).

    ``SUPERVISOR_ONLY`` -- the Supervisor alone creates/modifies/versions
    formal Goals, freezes Plans and decides transitions;
    ``NONE`` -- the role holds no scientific decision authority (every
    non-Supervisor role; the "may not" lists of SS3-SS8).
    """

    SUPERVISOR_ONLY = "supervisor_only"
    NONE = "none"


class VerdictAuthority(StrEnum):
    """Who may declare scientific verdicts (05-GOAL-RUN-SCHEMA.md SS7).

    ``SUPERVISOR_ONLY`` -- the Supervisor alone assigns final
    ``reproduction_outcome`` / ``method_reproducibility`` and reviews
    acceptance; ``REPORT_FACTS`` -- the role reports facts, results and
    observations and may never classify a Goal as PASS/FAIL.
    """

    SUPERVISOR_ONLY = "supervisor_only"
    REPORT_FACTS = "report_facts_only"


class RetryAuthority(StrEnum):
    """The resubmission boundary of each role (SS2/SS4/SS5/SS6).

    ``SUPERVISOR_ONLY`` -- retries beyond preauthorized engineering
    retries are Supervisor decisions; ``PREAUTHORIZED_ENGINEERING`` --
    the Monitor executes preauthorized engineering retries;
    ``WHITELISTED_ENGINEERING`` -- workers may only execute explicitly
    whitelisted engineering retries; ``NONE`` -- no retry authority.
    """

    SUPERVISOR_ONLY = "supervisor_only"
    PREAUTHORIZED_ENGINEERING = "preauthorized_engineering_only"
    WHITELISTED_ENGINEERING = "whitelisted_engineering_only"
    NONE = "none"


# ---------------------------------------------------------------------------
# The contract record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleContract:
    """One reusable, platform-neutral role contract (DEV-M10-G01).

    A frozen, validated descriptor of a runtime agent role: its authority
    boundaries in the matrix vocabulary (AC-01), its Core state truth
    sources (AC-02) and its prompt-facing obligations/prohibitions. It is
    a pure record -- no execution, no I/O, no wall clock -- so adapters
    can serialize it canonically (``to_dict`` / ``to_json``), transport
    it, and derive agent prompts from it.

    Attributes:
        role_id: canonical contract role id (one of ``CONTRACT_ROLE_IDS``).
        role_name: human-readable role name.
        spec_section: the locked spec section that defines this role.
        mission: the role's one-line mission statement.
        contract_version: version of this contract record
            (``ROLE_CONTRACTS_VERSION`` by default).
        allowed_actions: the role's "may" list as matrix actions.
        forbidden_actions: the role's "may not" list as matrix actions.
        forbidden_practices: boundary vocabulary not expressible as matrix
            actions (e.g. ``plan_mutation``, ``self_acceptance``).
        truth_sources: Core state APIs the role treats as authoritative
            (AC-02; subset of ``CoreStateApi``).
        state_object_types: normative object types (``SCHEMA_NAMES``) the
            role reads/writes as truth.
        decision_authority: scientific decision boundary.
        verdict_authority: scientific verdict boundary.
        retry_authority: resubmission/retry boundary.
        state_truth_rule: prompt-facing AC-02 rule text.
        prompt_obligations: prompt directives the role must follow.
        prompt_prohibitions: prompt directives the role must never do.
        contract_id: deterministic id
            ``sr_role_contract_<32 hex>`` = ``generate_id("role_contract",
            role_id, contract_version)`` (computed, immutable).
    """

    role_id: str
    role_name: str
    spec_section: str
    mission: str
    contract_version: str
    allowed_actions: frozenset[Action]
    forbidden_actions: frozenset[Action]
    forbidden_practices: frozenset[str]
    truth_sources: frozenset[CoreStateApi]
    state_object_types: frozenset[str]
    decision_authority: DecisionAuthority
    verdict_authority: VerdictAuthority
    retry_authority: RetryAuthority
    state_truth_rule: str
    prompt_obligations: tuple[str, ...]
    prompt_prohibitions: tuple[str, ...]
    contract_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "role_id",
            "role_name",
            "spec_section",
            "mission",
            "contract_version",
            "state_truth_rule",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(
                    f"RoleContract.{name} must be a str, got"
                    f" {type(value).__name__}"
                )
            if not value.strip():
                raise RoleContractError(
                    f"RoleContract.{name} must be a non-empty string, got"
                    f" {value!r}"
                )
        if not _ROLE_ID_PATTERN.fullmatch(self.role_id):
            raise RoleContractError(
                f"invalid role_id {self.role_id!r}: expected"
                " ^[a-z][a-z0-9_]*$ (safe on every platform)"
            )
        if self.role_id not in CONTRACT_ROLE_IDS:
            raise RoleContractError(
                f"unknown role_id {self.role_id!r}; expected one of:"
                f" {', '.join(CONTRACT_ROLE_IDS)}"
            )
        if not _VERSION_PATTERN.fullmatch(self.contract_version):
            raise RoleContractError(
                f"invalid contract_version {self.contract_version!r}: expected"
                " ^\\d+\\.\\d+$"
            )
        for name, actions in (
            ("allowed_actions", self.allowed_actions),
            ("forbidden_actions", self.forbidden_actions),
        ):
            if not isinstance(actions, frozenset):
                raise TypeError(
                    f"RoleContract.{name} must be a frozenset of Action, got"
                    f" {type(actions).__name__}"
                )
            for action in actions:
                if not isinstance(action, Action):
                    raise TypeError(
                        f"RoleContract.{name} entries must be Action members,"
                        f" got {type(action).__name__}"
                    )
        overlap = self.allowed_actions & self.forbidden_actions
        if overlap:
            raise RoleContractError(
                f"role contract {self.role_id!r} lists action(s) as both"
                " allowed and forbidden:"
                f" {', '.join(sorted(a.value for a in overlap))}"
            )
        if not isinstance(self.forbidden_practices, frozenset):
            raise TypeError(
                "RoleContract.forbidden_practices must be a frozenset of str,"
                f" got {type(self.forbidden_practices).__name__}"
            )
        for token in self.forbidden_practices:
            if not isinstance(token, str) or not _PRACTICE_TOKEN_PATTERN.fullmatch(
                token
            ):
                raise TypeError(
                    "RoleContract.forbidden_practices entries must be str"
                    f" tokens matching ^[a-z][a-z0-9_]*$, got {token!r}"
                )
        if not isinstance(self.truth_sources, frozenset):
            raise TypeError(
                "RoleContract.truth_sources must be a frozenset of CoreStateApi,"
                f" got {type(self.truth_sources).__name__}"
            )
        for source in self.truth_sources:
            if not isinstance(source, CoreStateApi):
                raise TypeError(
                    "RoleContract.truth_sources entries must be CoreStateApi"
                    f" members, got {type(source).__name__}"
                )
        if not self.truth_sources:
            raise RoleContractError(
                f"role contract {self.role_id!r} must name at least one Core"
                " state API as truth source (AC-02)"
            )
        if not isinstance(self.state_object_types, frozenset):
            raise TypeError(
                "RoleContract.state_object_types must be a frozenset of str,"
                f" got {type(self.state_object_types).__name__}"
            )
        if not self.state_object_types:
            raise RoleContractError(
                f"role contract {self.role_id!r} must name at least one"
                " normative state object type (AC-02)"
            )
        unknown_types = sorted(
            obj_type
            for obj_type in self.state_object_types
            if not isinstance(obj_type, str) or obj_type not in SCHEMA_NAMES
        )
        if unknown_types:
            raise RoleContractError(
                f"role contract {self.role_id!r} names non-normative state"
                " object type(s):"
                f" {', '.join(sorted(unknown_types))}; expected"
                f" core.models.SCHEMA_NAMES entries"
            )
        for name, authority in (
            ("decision_authority", self.decision_authority),
            ("verdict_authority", self.verdict_authority),
            ("retry_authority", self.retry_authority),
        ):
            if not isinstance(authority, StrEnum) or not isinstance(
                authority,
                (DecisionAuthority, VerdictAuthority, RetryAuthority),
            ):
                raise TypeError(
                    f"RoleContract.{name} must be an authority member, got"
                    f" {type(authority).__name__}"
                )
        for name, directives in (
            ("prompt_obligations", self.prompt_obligations),
            ("prompt_prohibitions", self.prompt_prohibitions),
        ):
            if not isinstance(directives, tuple):
                raise TypeError(
                    f"RoleContract.{name} must be a tuple of str, got"
                    f" {type(directives).__name__}"
                )
            for directive in directives:
                if not isinstance(directive, str) or not directive.strip():
                    raise RoleContractError(
                        f"RoleContract.{name} entries must be non-empty"
                        f" strings, got {directive!r}"
                    )
        if self.contract_id:
            raise RoleContractError(
                "RoleContract.contract_id is computed from role_id and"
                " contract_version; pass neither"
            )
        # Frozen dataclass: the computed id is set once at construction.
        object.__setattr__(
            self,
            "contract_id",
            generate_id("role_contract", self.role_id, self.contract_version),
        )

    # -- canonical serialization --------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the contract in canonical field order.

        Set members are sorted so equal contracts serialize byte-identically.
        """
        return {
            "contract_id": self.contract_id,
            "role_id": self.role_id,
            "role_name": self.role_name,
            "spec_section": self.spec_section,
            "contract_version": self.contract_version,
            "mission": self.mission,
            "allowed_actions": sorted(a.value for a in self.allowed_actions),
            "forbidden_actions": sorted(a.value for a in self.forbidden_actions),
            "forbidden_practices": sorted(self.forbidden_practices),
            "truth_sources": sorted(s.value for s in self.truth_sources),
            "state_object_types": sorted(self.state_object_types),
            "decision_authority": self.decision_authority.value,
            "verdict_authority": self.verdict_authority.value,
            "retry_authority": self.retry_authority.value,
            "state_truth_rule": self.state_truth_rule,
            "prompt_obligations": list(self.prompt_obligations),
            "prompt_prohibitions": list(self.prompt_prohibitions),
        }

    def to_json(self) -> str:
        """Canonical deterministic JSON of the contract (sorted keys)."""
        return json.dumps(
            self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RoleContract:
        """Build a contract from a plain dict (corrupt data is a stable
        RoleContractError -- contract records are adapter-transported state)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                "RoleContract.from_dict expects a mapping, got"
                f" {type(data).__name__}"
            )
        required = (
            "role_id",
            "role_name",
            "spec_section",
            "mission",
            "contract_version",
            "allowed_actions",
            "forbidden_actions",
            "forbidden_practices",
            "truth_sources",
            "state_object_types",
            "decision_authority",
            "verdict_authority",
            "retry_authority",
            "state_truth_rule",
            "prompt_obligations",
            "prompt_prohibitions",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise RoleContractError(
                "corrupt role contract record: missing required field(s):"
                f" {', '.join(sorted(missing))}"
            )
        try:
            return cls(
                role_id=data["role_id"],
                role_name=data["role_name"],
                spec_section=data["spec_section"],
                mission=data["mission"],
                contract_version=data["contract_version"],
                allowed_actions=frozenset(
                    Action(value) for value in data["allowed_actions"]
                ),
                forbidden_actions=frozenset(
                    Action(value) for value in data["forbidden_actions"]
                ),
                forbidden_practices=frozenset(data["forbidden_practices"]),
                truth_sources=frozenset(
                    CoreStateApi(value) for value in data["truth_sources"]
                ),
                state_object_types=frozenset(data["state_object_types"]),
                decision_authority=DecisionAuthority(data["decision_authority"]),
                verdict_authority=VerdictAuthority(data["verdict_authority"]),
                retry_authority=RetryAuthority(data["retry_authority"]),
                state_truth_rule=data["state_truth_rule"],
                prompt_obligations=tuple(data["prompt_obligations"]),
                prompt_prohibitions=tuple(data["prompt_prohibitions"]),
            )
        except (TypeError, ValueError) as exc:
            raise RoleContractError(
                f"corrupt role contract record for role {data.get('role_id')!r}:"
                f" {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Contract -> matrix role mapping (AC-01 grounding)
# ---------------------------------------------------------------------------


def contract_to_matrix_roles(role_id: str) -> tuple[Role, ...]:
    """Map one contract role id to its matrix role(s) (1:1 or 1:n).

    ``supervisor`` -> ``Role.SUPERVISOR``, ``research`` -> ``Role.RESEARCH``,
    ``execution_monitor`` -> ``Role.MONITOR``; ``worker`` maps to the four
    worker roles of ``core.permissions.Role`` (SS5-SS8).

    Raises:
        TypeError: ``role_id`` is not a str.
        UnknownRoleContractError: unknown contract role id.
    """
    if not isinstance(role_id, str):
        raise TypeError(
            f"contract_to_matrix_roles expects a role_id str, got"
            f" {type(role_id).__name__}"
        )
    mapping = {
        "supervisor": (Role.SUPERVISOR,),
        "research": (Role.RESEARCH,),
        "execution_monitor": (Role.MONITOR,),
        "worker": (
            Role.EXPERIMENT_WORKER,
            Role.COMPUTATION_WORKER,
            Role.ANALYSIS_WORKER,
            Role.DIAGNOSIS_WORKER,
        ),
    }
    if role_id not in mapping:
        raise UnknownRoleContractError(
            f"unknown contract role id {role_id!r}; expected one of:"
            f" {', '.join(CONTRACT_ROLE_IDS)}"
        )
    return mapping[role_id]


# ---------------------------------------------------------------------------
# The four frozen contracts (03-ROLE-AND-PERMISSION-SPEC.md SS2-SS8)
# ---------------------------------------------------------------------------

#: Actions the Worker contract may perform: the union of the four worker
#: roles' matrix "may" lists (SS5-SS8) -- read the context, prepare the
#: execution package, record metadata, ingest result packages, register
#: artifacts, report facts/deviations, run the frozen analysis, and
#: execute whitelisted engineering retries.
_WORKER_ALLOWED_ACTIONS: frozenset[Action] = (
    WORKER_COMMON_ACTIONS
    | frozenset(
        {
            Action.EXECUTION_PACKAGE_PREPARE,
            Action.RUN_PREPARE,
            Action.FACT_REPORT,
            Action.ANALYSIS_EXECUTE,
            Action.DIAGNOSIS_REPORT,
            Action.ENGINEERING_RETRY,
        }
    )
)

#: Actions the Worker contract forbids (SS5-SS8 "may not" lists): plan
#: mutation (freeze/revision of Plans, Goals, acceptance criteria and
#: analysis protocols), Goal creation, track changes, scientific
#: interpretation, acceptance and outcome decisions.
_WORKER_FORBIDDEN_ACTIONS: frozenset[Action] = frozenset(
    {
        Action.PLAN_FREEZE,
        Action.GOAL_REVISION,
        Action.ACCEPTANCE_REVISION,
        Action.ANALYSIS_PROTOCOL_REVISION,
        Action.GOAL_CREATE,
        Action.GOAL_MUTATE,
        Action.FROZEN_GOAL_MUTATE,
        Action.RECOVERY_GOAL_CREATE,
        Action.RECOVERY_ENTRY,
        Action.METHOD_REDESIGN_ENTRY,
        Action.SCIENTIFIC_INTERPRETATION,
        Action.GOAL_REVIEW,
        Action.REQUIREMENT_CLOSURE,
        Action.REQUIREMENT_CLOSE,
        Action.PROJECT_OUTCOME,
        Action.SCIENTIFIC_PARAMETER_CHANGE,
        Action.STATISTICAL_DESIGN_ALTER,
        Action.RESEARCH_REQUEST,
        Action.HUMAN_GATE_OPEN,
        Action.WORKER_DISPATCH,
    }
)

ROLE_CONTRACTS: tuple[RoleContract, ...] = (
    RoleContract(
        role_id="supervisor",
        role_name="Supervisor",
        spec_section="03-ROLE-AND-PERMISSION-SPEC.md SS2",
        mission=(
            "Own the scientific reproduction project from source acquisition"
            " through final outcome while preserving pre-registration-like"
            " governance, evidence traceability and separation of duties."
        ),
        contract_version=ROLE_CONTRACTS_VERSION,
        allowed_actions=frozenset(ACTION_ORDER),
        forbidden_actions=frozenset(),
        forbidden_practices=frozenset(
            {
                "silent_change_of_frozen_criteria",
                "significance_equivocated_as_equivalence",
                "failed_attempt_erasure",
                "premature_non_reproduction_claim",
                "scientific_authority_delegation",
            }
        ),
        truth_sources=frozenset(
            {CoreStateApi.STATE_BACKEND, CoreStateApi.EVENT_LOG}
        ),
        state_object_types=frozenset(
            {
                "project",
                "plan",
                "goal",
                "acceptance-criteria",
                "requirement",
                "run",
                "event",
                "decision",
                "human-gate",
                "research-request",
                "worker-context",
                "analysis",
                "artifact-manifest",
            }
        ),
        decision_authority=DecisionAuthority.SUPERVISOR_ONLY,
        verdict_authority=VerdictAuthority.SUPERVISOR_ONLY,
        retry_authority=RetryAuthority.SUPERVISOR_ONLY,
        state_truth_rule=STATE_TRUTH_RULE,
        prompt_obligations=(
            "read project state from the Core state backend before acting",
            "create inventory/plan/goals through the frozen schemas",
            "freeze acceptance criteria before execution",
            "issue Research Requests through the state records",
            "adjudicate Analysis results from the persisted analysis records",
            "create versioned Recovery/Redesign plans through the planning layer",
            "enforce Human Gates",
            "never hide failed Runs",
            "maintain final traceability through the project event log",
        ),
        prompt_prohibitions=(
            "never silently change frozen criteria after data are seen",
            "never treat lack of significance as equivalence",
            "never erase failed attempts",
            "never claim non-reproduction before the Closure Contract is"
            " satisfied",
            "never delegate final scientific authority to workers",
        ),
    ),
    RoleContract(
        role_id="research",
        role_name="Research",
        spec_section="03-ROLE-AND-PERMISSION-SPEC.md SS3",
        mission=(
            "Build and maintain the project evidence base using traceable"
            " sources and claim-specific evidence assessments."
        ),
        contract_version=ROLE_CONTRACTS_VERSION,
        allowed_actions=RESEARCH_ACTIONS,
        forbidden_actions=frozenset(
            {
                Action.GOAL_CREATE,
                Action.GOAL_MUTATE,
                Action.FROZEN_GOAL_MUTATE,
                Action.GOAL_REVISION,
                Action.PLAN_FREEZE,
                Action.ACCEPTANCE_REVISION,
                Action.ANALYSIS_PROTOCOL_REVISION,
                Action.RECOVERY_ENTRY,
                Action.RECOVERY_GOAL_CREATE,
                Action.METHOD_REDESIGN_ENTRY,
                Action.SCIENTIFIC_INTERPRETATION,
                Action.GOAL_REVIEW,
                Action.REQUIREMENT_CLOSURE,
                Action.REQUIREMENT_CLOSE,
                Action.PROJECT_OUTCOME,
                Action.RESEARCH_REQUEST,
                Action.HUMAN_GATE_OPEN,
                Action.WORKER_DISPATCH,
            }
        ),
        forbidden_practices=frozenset(
            {
                "author_contact_without_human_gate",
                "mirrored_copy_as_independent_evidence",
                "reliability_from_intuition",
            }
        ),
        truth_sources=frozenset(
            {CoreStateApi.STATE_BACKEND, CoreStateApi.EVENT_LOG}
        ),
        state_object_types=frozenset(
            {"source", "evidence", "research-request", "event"}
        ),
        decision_authority=DecisionAuthority.NONE,
        verdict_authority=VerdictAuthority.REPORT_FACTS,
        retry_authority=RetryAuthority.NONE,
        state_truth_rule=STATE_TRUTH_RULE,
        prompt_obligations=(
            "respond only to formal Supervisor Research Requests recorded in"
            " state",
            "store findings as source and evidence records in the state"
            " backend",
            "execute evidence checklists deterministically",
            "record search saturation cycles",
        ),
        prompt_prohibitions=(
            "never change Goals or acceptance criteria",
            "never decide Recovery actions",
            "never dispatch Workers directly",
            "never contact authors without a Human Gate",
            "never treat mirrored copies of one paper as independent evidence",
            "never assign Reliability from intuition instead of checklist/rule"
            " mapping",
        ),
    ),
    RoleContract(
        role_id="execution_monitor",
        role_name="Execution Monitor",
        spec_section="03-ROLE-AND-PERMISSION-SPEC.md SS4",
        mission=(
            "Maintain continuity of external Runs and translate deterministic"
            " execution events into project state transitions."
        ),
        contract_version=ROLE_CONTRACTS_VERSION,
        allowed_actions=MONITOR_ACTIONS,
        forbidden_actions=frozenset(
            {
                Action.SCIENTIFIC_PARAMETER_CHANGE,
                Action.SCIENTIFIC_INTERPRETATION,
                Action.GOAL_REVIEW,
                Action.PROJECT_OUTCOME,
                Action.RECOVERY_ENTRY,
                Action.RECOVERY_GOAL_CREATE,
                Action.STATISTICAL_DESIGN_ALTER,
                Action.GOAL_CREATE,
                Action.GOAL_MUTATE,
                Action.FROZEN_GOAL_MUTATE,
                Action.GOAL_REVISION,
                Action.PLAN_FREEZE,
                Action.ACCEPTANCE_REVISION,
                Action.ANALYSIS_PROTOCOL_REVISION,
                Action.METHOD_REDESIGN_ENTRY,
                Action.REQUIREMENT_CLOSURE,
                Action.REQUIREMENT_CLOSE,
                Action.RESEARCH_REQUEST,
                Action.HUMAN_GATE_OPEN,
                Action.WORKER_DISPATCH,
            }
        ),
        forbidden_practices=frozenset(
            {
                "retry_beyond_preauthorized_engineering",
                "autonomous_recovery_entry",
            }
        ),
        truth_sources=frozenset(
            {CoreStateApi.STATE_BACKEND, CoreStateApi.EVENT_LOG}
        ),
        state_object_types=frozenset(
            {"run", "event", "worker-context", "retry-policy", "artifact-manifest"}
        ),
        decision_authority=DecisionAuthority.NONE,
        verdict_authority=VerdictAuthority.REPORT_FACTS,
        retry_authority=RetryAuthority.PREAUTHORIZED_ENGINEERING,
        state_truth_rule=STATE_TRUTH_RULE,
        prompt_obligations=(
            "inspect external Run status from the persisted Run records",
            "transition Run operational lifecycle according to deterministic"
            " state rules",
            "validate arrival of Result Packages against the state records",
            "execute preauthorized engineering retries",
            "maintain heartbeat/checkpoint/event records in the event log",
            "reconcile shared state with external truth on restart",
        ),
        prompt_prohibitions=(
            "never change scientific parameters",
            "never classify a Goal as scientifically PASS/FAIL",
            "never enter Recovery autonomously",
            "never alter statistical design",
            "never decide retries beyond preauthorized engineering retries",
        ),
    ),
    RoleContract(
        role_id="worker",
        role_name="Experiment / Computation / Analysis / Diagnosis Worker",
        spec_section="03-ROLE-AND-PERMISSION-SPEC.md SS5-SS8",
        mission=(
            "Execute exactly one bounded frozen Goal/Run context, record"
            " what happened, register artifacts and report facts and"
            " deviations -- never decide scientific outcomes."
        ),
        contract_version=ROLE_CONTRACTS_VERSION,
        allowed_actions=_WORKER_ALLOWED_ACTIONS,
        forbidden_actions=_WORKER_FORBIDDEN_ACTIONS,
        forbidden_practices=frozenset(
            {
                "plan_mutation",
                "self_acceptance",
                "self_review",
                "self_merge",
                "retry_beyond_whitelisted_engineering",
                "root_cause_as_formal_decision",
            }
        ),
        truth_sources=frozenset(
            {CoreStateApi.STATE_BACKEND, CoreStateApi.EVENT_LOG}
        ),
        state_object_types=frozenset(
            {
                "worker-context",
                "run",
                "event",
                "lab-execution-package",
                "analysis",
                "artifact-manifest",
            }
        ),
        decision_authority=DecisionAuthority.NONE,
        verdict_authority=VerdictAuthority.REPORT_FACTS,
        retry_authority=RetryAuthority.WHITELISTED_ENGINEERING,
        state_truth_rule=STATE_TRUTH_RULE,
        prompt_obligations=(
            "read exactly one frozen Goal Execution Context Package from"
            " state",
            "prepare the execution package and record actual"
            " reagent/sample/instrument/procedure metadata",
            "ingest the returned Result Package and register raw artifacts",
            "report deviations and anomalies to the Supervisor",
            "execute deterministic engineering steps that do not alter"
            " scientific meaning",
        ),
        prompt_prohibitions=(
            "never propose or implement scientific protocol changes",
            "never mutate the frozen Plan, acceptance criteria or protocol",
            "never create or change Goals",
            "never change the track (STRICT/RECOVERY/METHOD_REDESIGN)",
            "never decide retries except whitelisted engineering retries",
            "never declare PASS/FAIL or accept your own output",
        ),
    ),
)


# ---------------------------------------------------------------------------
# Lookup and integrity
# ---------------------------------------------------------------------------


def get_role_contract(role_id: str) -> RoleContract:
    """Return the frozen contract for ``role_id``.

    Raises:
        TypeError: ``role_id`` is not a str.
        UnknownRoleContractError: unknown contract role id (stable,
            one-line message).
    """
    if not isinstance(role_id, str):
        raise TypeError(
            f"get_role_contract expects a role_id str, got"
            f" {type(role_id).__name__}"
        )
    for contract in ROLE_CONTRACTS:
        if contract.role_id == role_id:
            return contract
    raise UnknownRoleContractError(
        f"unknown contract role id {role_id!r}; expected one of:"
        f" {', '.join(CONTRACT_ROLE_IDS)}"
    )


def validate_role_contracts(
    contracts: Sequence[RoleContract] | None = None,
) -> tuple[str, ...]:
    """Validate the frozen role contract table's integrity; return role ids.

    The frozen module table ``ROLE_CONTRACTS`` is validated by default; an
    explicit candidate table can be passed. A valid table:

    * is non-empty and carries only ``RoleContract`` records;
    * has unique ``role_id`` and unique ``contract_id`` values, all drawn
      from the frozen contract role vocabulary (no invented roles);
    * is consistent with the locked role-action matrix (AC-01): every
      ``allowed_action`` is granted to at least one of the contract's
      matrix roles and every ``forbidden_action`` is denied to all of them
      (least privilege -- a contract never crosses its boundary);
    * respects the typed authority boundaries: a contract without
      Supervisor verdict authority forbids the verdict actions, and a
      contract without Supervisor decision authority forbids every
      Supervisor-decision action.

    Raises:
        TypeError: ``contracts`` is neither a sequence of ``RoleContract``
            nor None, or an entry is not a ``RoleContract``.
        RoleContractError: the table violates an integrity rule (stable
            messages).
    """
    table = ROLE_CONTRACTS if contracts is None else contracts
    if not isinstance(table, Sequence) or isinstance(table, (str, bytes)):
        raise TypeError(
            "contracts must be a sequence of RoleContract or None, got"
            f" {type(table).__name__}"
        )
    contracts_tuple = tuple(table)
    for contract in contracts_tuple:
        if not isinstance(contract, RoleContract):
            raise TypeError(
                "role contract table entries must be RoleContract instances,"
                f" got {type(contract).__name__}"
            )
    if not contracts_tuple:
        raise RoleContractError(
            "the role contract table must not be empty: at least the four"
            " frozen role contracts are required"
        )
    role_ids = tuple(contract.role_id for contract in contracts_tuple)
    duplicates = sorted(
        {role_id for role_id in role_ids if role_ids.count(role_id) > 1}
    )
    if duplicates:
        raise RoleContractError(
            "duplicate role id(s) in the role contract table:"
            f" {', '.join(duplicates)}"
        )
    contract_ids = tuple(contract.contract_id for contract in contracts_tuple)
    duplicate_ids = sorted(
        {cid for cid in contract_ids if contract_ids.count(cid) > 1}
    )
    if duplicate_ids:
        raise RoleContractError(
            "duplicate contract id(s) in the role contract table:"
            f" {', '.join(duplicate_ids)}"
        )
    unknown_roles = sorted(set(role_ids) - set(CONTRACT_ROLE_IDS))
    if unknown_roles:
        raise RoleContractError(
            "role contract table names unknown role id(s):"
            f" {', '.join(unknown_roles)}; expected one of:"
            f" {', '.join(CONTRACT_ROLE_IDS)}"
        )
    for contract in contracts_tuple:
        matrix_roles = contract_to_matrix_roles(contract.role_id)
        granted = {
            action
            for action in ACTION_ORDER
            if any(is_action_allowed(role, action) for role in matrix_roles)
        }
        not_allowed = sorted(
            action.value
            for action in contract.allowed_actions
            if action not in granted
        )
        if not_allowed:
            raise RoleContractError(
                f"role contract {contract.role_id!r} allows action(s) the"
                " locked role-action matrix denies for its role(s):"
                f" {', '.join(not_allowed)}"
            )
        not_forbidden = sorted(
            action.value
            for action in contract.forbidden_actions
            if action in granted
        )
        if not_forbidden:
            raise RoleContractError(
                f"role contract {contract.role_id!r} forbids action(s) the"
                " locked role-action matrix grants for its role(s):"
                f" {', '.join(not_forbidden)}"
            )
        if contract.verdict_authority is not VerdictAuthority.SUPERVISOR_ONLY:
            missing_verdict = sorted(
                action.value
                for action in VERDICT_ACTIONS
                if action not in contract.forbidden_actions
            )
            if missing_verdict:
                raise RoleContractError(
                    f"role contract {contract.role_id!r} has no verdict"
                    " authority but does not forbid the verdict action(s):"
                    f" {', '.join(missing_verdict)}"
                )
        if contract.decision_authority is not DecisionAuthority.SUPERVISOR_ONLY:
            missing_decisions = sorted(
                action.value
                for action in DECISION_ACTIONS
                if action not in contract.forbidden_actions
            )
            if missing_decisions:
                raise RoleContractError(
                    f"role contract {contract.role_id!r} has no decision"
                    " authority but does not forbid the Supervisor-decision"
                    f" action(s): {', '.join(missing_decisions)}"
                )
    return role_ids
