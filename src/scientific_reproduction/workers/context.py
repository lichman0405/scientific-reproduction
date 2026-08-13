"""Goal Execution Context Package generator (DEV-M6-G01).

Implements the **goal execution context package generator** deliverable:
a deterministic generator that builds the frozen
``core.models.GoalExecutionContextPackage`` (``schemas/worker-context.schema.yaml``)
for a dynamic worker from the **frozen Goal Contract** plus the goal's
explicitly relevant sources, evidence, upstream outputs and policies
(``05-GOAL-RUN-SCHEMA.md`` SS8). The module also implements the
**relevance-reference filter** deliverable: the ordered rule table that
decides, for every candidate registry document, whether it is exposed.

Normative grounding (locked readings)
-------------------------------------
* ``05-GOAL-RUN-SCHEMA.md`` SS8 ("Context Package"): each dynamic worker
  receives *only the minimum necessary project context* -- Goal Contract
  version/hash, relevant target-paper excerpts/source records, relevant
  evidence records, required upstream results, execution environment,
  resource information, frozen analysis/acceptance references, allowed
  engineering retries, required return artifacts, explicit prohibitions --
  and *workers must not read the entire repository unless specifically
  authorized*.
* ``05-GOAL-RUN-SCHEMA.md`` SS4: the Goal Contract carries an *automatic
  engineering retry policy* reference (``automatic_retry_policy_ref``) and
  declares its allowed outputs (``outputs``).
* ``06-EVIDENCE-SYSTEM.md`` SS6: ``ClaimSpecificEvidence.used_by`` holds
  the Goals/decisions using the evidence as opaque refs -- the frozen
  linkage that makes an evidence record *relevant* to a goal.
* ``schemas/retry-policy.schema.yaml`` (``core.models.AutomaticRetryPolicy``):
  the goal's required policy record; ``allowed_engineering_failures`` are
  the engineering retries the worker may take on its own,
  ``supervisor_required_changes`` the failures whose retry is explicitly
  prohibited without a Supervisor change (the "explicit prohibitions" of
  SS8).
* ``schemas/worker-context.schema.yaml``: the package model's exact fields.

"Frozen" Goal Contract (M4-G04/M4-G05 semantics)
------------------------------------------------
The context is generated from the **frozen** Goal Contract -- the record
the plan freeze produced (``planning.freeze.freeze_plan``'s ``goals``,
``frozen`` True, formal version ``v<N>``) -- never from a drifting copy:
the generator rejects any contract that is not the frozen variant
(``GoalNotFrozenError``) and the package references the frozen record
exactly by its registered ``goal_id`` and ``goal_version``. A draft
(``frozen`` False) or a frozen record without a formal version cannot
produce a worker context.

Relevance-reference filter (AC-02)
----------------------------------
Every candidate document (an item of a provided registry) is decided by
the ordered, versioned ``RELEVANCE_FILTER_RULES`` table: first match
wins, the trailing total default ``R-REL-D1`` *excludes* anything not
explicitly referenced by the goal, and ``matched_rule_id`` is never
``None`` (post-assert). The generator iterates the candidate registries
through the filter, so unrelated registered documents (evidence not used
by the goal, sources of other evidence, outputs of goals that are not
dependencies, protocols/resources/policies the goal does not reference)
are absent from the package by default -- the "minimal necessary" rule of
SS8.

Manifest (AC-03)
----------------
:class:`ContextManifest` records **exactly which references were
exposed**: every included item as ``(kind, ref_id, version)`` -- the
goal's frozen version for the goal entry, the registered record's
``protocol_version`` for the analysis-protocol entry, and ``None`` for
records whose frozen model declares no version field (``ResearchSource``,
``ClaimSpecificEvidence``, ``Resource``, ``AutomaticRetryPolicy``). The
package's reference lists are derived **from the manifest** (the manifest
is authoritative): ``source_refs`` / ``evidence_refs`` /
``upstream_result_refs`` / ``protocol_refs`` / ``resource_refs`` are the
manifest references of the corresponding kinds. ``context_hash`` is the
SHA-256 of the manifest's canonical JSON (sorted keys, 2-space indent,
trailing newline) -- a deterministic fingerprint of exactly what the
worker was exposed to.

Upstream outputs (locked reading)
---------------------------------
The goal's declared dependencies are the explicit upstream references
(``05-GOAL-RUN-SCHEMA.md`` SS5); every dependency goal's declared outputs
are the **required upstream results**. Each reference is the deterministic
string ``"<upstream_goal_id>#<output>"``, where the output name is the
``name`` key of an output object (the frozen ``GoalContract.outputs`` is
a list of objects per ``schemas/goal.schema.yaml``); output objects
without a string ``name`` are not returnable artifacts and are skipped.
Dependency goal records are read through the real registered registry
(``planning.plan.read_goal``); an unregistered dependency goal raises the
registry's stable ``GoalNotFoundError``.

Action vocabulary (locked reading)
----------------------------------
The frozen vocabulary defines no worker-action enum; actions are the
opaque strings of ``allowed_actions`` / ``forbidden_actions``. The locked
vocabulary here: each engineering failure kind maps to the action
``"retry:<failure>"``. ``allowed_actions`` are the retries the goal's
automatic retry policy permits (``allowed_engineering_failures``, sorted);
``forbidden_actions`` are the retries the policy routes to the Supervisor
(``supervisor_required_changes``, sorted -- the "explicit prohibitions").
A goal that references no automatic retry policy exposes no retry actions
at all (no policy, no retries -- minimal necessary).

Environment (locked reading)
----------------------------
``environment`` is execution-time configuration
(``15-ADAPTER-SPEC.md`` SS2: environment-specific details live in
project/user configuration, not Goal contracts); it is injectable
(``Mapping``) and defaults to the empty mapping. ``run_id`` is ``None``:
no Run exists at context generation time (a Run is created from the
context).

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the injectable
inputs: no randomness, no wall clock, no network. ``TypeError`` at the
public boundaries; errors follow the ``ValueError``-subclass convention
with stable messages; ``from __future__ import annotations``; ``__all__``.
Registry reads go through the real merged modules (``planning.plan``
``read_goal`` / ``list_goals`` / ``read_analysis_protocol``); the
in-memory evidence registry (``research.evidence.EvidenceRegistry``) and
the source/policy records have no file-backed public registry, so they
are injected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from scientific_reproduction.core.ids import generate_id
from scientific_reproduction.core.models import (
    AutomaticRetryPolicy,
    GoalContract,
    GoalExecutionContextPackage,
    ResearchSource,
    WorkerRole,
)
from scientific_reproduction.planning.init import (
    PROJECT_STATE_FILENAME,
    ProjectNotInitializedError,
)
from scientific_reproduction.planning.plan import (
    is_formal_version,
    list_goals,
    read_analysis_protocol,
    read_goal,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

__all__ = [
    "CONTEXT_MANIFEST_VERSION",
    "RELEVANCE_FILTER_RULES",
    "RELEVANCE_FILTER_RULESET_VERSION",
    "ContextBuildError",
    "ContextError",
    "ContextManifest",
    "ContextPackageResult",
    "ContextReference",
    "ExplicitReferences",
    "GoalNotFrozenError",
    "PolicyMismatchError",
    "ReferenceKind",
    "RelevanceAssessment",
    "RelevanceDecision",
    "RelevanceInput",
    "RelevanceRule",
    "evaluate_relevance",
    "generate_goal_context",
]

#: Version of the persisted context-manifest schema (``manifest_version``
#: key of :class:`ContextManifest`).
CONTEXT_MANIFEST_VERSION: str = "1.0"

#: Version of the relevance-reference filter rule table; recorded in every
#: assessment.
RELEVANCE_FILTER_RULESET_VERSION: str = "1.0"

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2

#: The manifest's reference-vocabulary: the kind of each included item.
#: Values match no frozen schema enum (the manifest is this module's own
#: auditable vocabulary); the GOAL kind is the frozen Goal Contract itself.
class ReferenceKind(StrEnum):
    GOAL = "goal"
    POLICY = "policy"
    SOURCE = "source"
    EVIDENCE = "evidence"
    UPSTREAM_OUTPUT = "upstream_output"
    PROTOCOL = "protocol"
    RESOURCE = "resource"


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class ContextError(ValueError):
    """Base class for all goal-execution-context generation errors."""


class ContextBuildError(ContextError):
    """Raised when the context cannot be built from the given state.

    Stable messages name the offending goal and the reason; nothing is
    silently dropped or fabricated.
    """


class GoalNotFrozenError(ContextError):
    """Raised when context generation receives a non-frozen Goal Contract.

    AC-01: the context is generated from the frozen Goal Contract only
    (the record the plan freeze produced); a draft contract would make the
    worker's context drift with the authoring state.
    """


class PolicyMismatchError(ContextError):
    """Raised when the provided retry policy does not match the goal's ref.

    The policy record is either absent while the goal references one, or
    its ``policy_id`` differs from ``automatic_retry_policy_ref`` -- a
    caller error that would otherwise expose the wrong policy to the
    worker.
    """


# ---------------------------------------------------------------------------
# The relevance-reference filter (AC-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplicitReferences:
    """The goal's explicit reference sets (what "relevant" means).

    ``goal_id`` is the goal the context is generated for; every other
    field is a set of refs the goal contract itself declares or that the
    frozen linkage derives from it: the analysis-protocol ref, the
    resource ids, the automatic-retry-policy ref, the claim-specific
    evidence records whose ``used_by`` references the goal
    (06-EVIDENCE-SYSTEM.md SS6), the source records of that evidence, and
    the dependency goals' declared outputs (``"<goal_id>#<output>"``).
    """

    goal_id: str
    protocol_refs: frozenset[str]
    resource_ids: frozenset[str]
    policy_refs: frozenset[str]
    evidence_ids: frozenset[str]
    source_ids: frozenset[str]
    upstream_outputs: frozenset[str]


@dataclass(frozen=True)
class RelevanceInput:
    """The state a relevance decision is a pure function of.

    ``kind`` / ``ref_id`` identify one candidate registry document;
    ``explicit_refs`` are the goal's explicit reference sets. The decision
    is a pure function of these three inputs only.
    """

    kind: ReferenceKind
    ref_id: str
    explicit_refs: ExplicitReferences


@dataclass(frozen=True)
class RelevanceRule:
    """One entry of the ordered relevance-reference filter rule table."""

    rule_id: str
    description: str
    include: bool
    predicate: Callable[[RelevanceInput], bool]


@dataclass(frozen=True)
class RelevanceDecision:
    """Record of one filter-rule evaluation for a given candidate."""

    rule_id: str
    description: str
    include: bool
    matched: bool


#: The ordered relevance-reference filter rule table. First match wins;
#: order is normative. The kind-specific rules are mutually exclusive (each
#: rule matches one kind), so the deciding rule is unambiguous; the
#: trailing total default ``R-REL-D1`` excludes anything not explicitly
#: referenced by the goal (AC-02: unrelated registry documents are absent
#: from the package by default -- 05-GOAL-RUN-SCHEMA.md SS8).
RELEVANCE_FILTER_RULES: tuple[RelevanceRule, ...] = (
    RelevanceRule(
        rule_id="R-REL-P1",
        description=(
            "the candidate is the analysis protocol the goal contract"
            " explicitly references (frozen analysis reference,"
            " 05-GOAL-RUN-SCHEMA.md SS4/SS8): include"
        ),
        include=True,
        predicate=lambda i: (
            i.kind is ReferenceKind.PROTOCOL
            and i.ref_id in i.explicit_refs.protocol_refs
        ),
    ),
    RelevanceRule(
        rule_id="R-REL-R1",
        description=(
            "the candidate is a resource the goal contract explicitly"
            " references (goal.resource_ids): include"
        ),
        include=True,
        predicate=lambda i: (
            i.kind is ReferenceKind.RESOURCE
            and i.ref_id in i.explicit_refs.resource_ids
        ),
    ),
    RelevanceRule(
        rule_id="R-REL-K1",
        description=(
            "the candidate is the automatic engineering retry policy the"
            " goal contract explicitly references"
            " (goal.automatic_retry_policy_ref, 05-GOAL-RUN-SCHEMA.md"
            " SS4): include"
        ),
        include=True,
        predicate=lambda i: (
            i.kind is ReferenceKind.POLICY
            and i.ref_id in i.explicit_refs.policy_refs
        ),
    ),
    RelevanceRule(
        rule_id="R-REL-E1",
        description=(
            "the candidate is a claim-specific evidence record whose"
            " used_by references this goal (06-EVIDENCE-SYSTEM.md SS6):"
            " include"
        ),
        include=True,
        predicate=lambda i: (
            i.kind is ReferenceKind.EVIDENCE
            and i.ref_id in i.explicit_refs.evidence_ids
        ),
    ),
    RelevanceRule(
        rule_id="R-REL-S1",
        description=(
            "the candidate is a source record of one of this goal's"
            " included evidence records: include"
        ),
        include=True,
        predicate=lambda i: (
            i.kind is ReferenceKind.SOURCE
            and i.ref_id in i.explicit_refs.source_ids
        ),
    ),
    RelevanceRule(
        rule_id="R-REL-U1",
        description=(
            "the candidate is an output declared by one of this goal's"
            " dependency (upstream) goals: include (the required upstream"
            " results)"
        ),
        include=True,
        predicate=lambda i: (
            i.kind is ReferenceKind.UPSTREAM_OUTPUT
            and i.ref_id in i.explicit_refs.upstream_outputs
        ),
    ),
    RelevanceRule(
        rule_id="R-REL-D1",
        description=(
            "the candidate is not explicitly referenced by this goal:"
            " exclude -- only the minimum necessary context is exposed"
            " (05-GOAL-RUN-SCHEMA.md SS8, AC-02)"
        ),
        include=False,
        predicate=lambda i: True,
    ),
)


@dataclass(frozen=True)
class RelevanceAssessment:
    """Full, auditable result of one relevance decision (AC-02).

    ``input`` is the exact candidate the decision was computed from;
    ``decisions`` records the outcome of every rule in the table (in
    evaluation order); ``matched_rule_id`` names the deciding rule
    (``None`` is impossible: the trailing total default ``R-REL-D1``
    always matches); ``ruleset_version`` records the rule table version
    (``RELEVANCE_FILTER_RULESET_VERSION``).
    """

    input: RelevanceInput
    include: bool
    decisions: tuple[RelevanceDecision, ...]
    matched_rule_id: str
    ruleset_version: str = RELEVANCE_FILTER_RULESET_VERSION


def evaluate_relevance(input_: RelevanceInput) -> RelevanceAssessment:
    """Decide whether one candidate document is relevant to the goal.

    Pure and deterministic: the decision is a pure function of the
    candidate's kind/ref and the goal's explicit reference sets, decided
    by the ordered ``RELEVANCE_FILTER_RULES`` table (first match wins; the
    trailing total default excludes anything not explicitly referenced).

    Raises:
        TypeError: ``input_`` is not a ``RelevanceInput``.
    """
    if not isinstance(input_, RelevanceInput):
        raise TypeError(
            "evaluate_relevance expects a RelevanceInput, got"
            f" {type(input_).__name__}"
        )
    decisions: list[RelevanceDecision] = []
    matched_rule_id: str | None = None
    matched_include = False  # unreachable default
    for rule in RELEVANCE_FILTER_RULES:
        matched = rule.predicate(input_)
        decisions.append(
            RelevanceDecision(
                rule_id=rule.rule_id,
                description=rule.description,
                include=rule.include,
                matched=matched,
            )
        )
        if matched and matched_rule_id is None:
            matched_rule_id = rule.rule_id
            matched_include = rule.include
    # R-REL-D1 (default) always matches, so this can never be None.
    assert matched_rule_id is not None
    return RelevanceAssessment(
        input=input_,
        include=matched_include,
        decisions=tuple(decisions),
        matched_rule_id=matched_rule_id,
    )


# ---------------------------------------------------------------------------
# The context manifest (AC-03: exactly which references were exposed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextReference:
    """One included item of a context package: kind + id + version.

    ``version`` is the item's own frozen version field where the model
    declares one (``GoalContract.version`` for the goal entry, the
    registered record's ``protocol_version`` for the analysis-protocol
    entry) and ``None`` otherwise -- ``ResearchSource``,
    ``ClaimSpecificEvidence``, ``Resource`` and ``AutomaticRetryPolicy``
    declare no version field in the frozen vocabulary.
    """

    kind: ReferenceKind
    ref_id: str
    version: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the reference (``version`` omitted when ``None``)."""
        data = {"kind": self.kind.value, "ref_id": self.ref_id}
        if self.version is not None:
            data["version"] = self.version
        return data


@dataclass(frozen=True)
class ContextManifest:
    """The deterministic reference manifest of one context package (AC-03).

    The manifest records **exactly** which references were exposed, as
    ``(kind, ref_id, version)`` entries sorted by ``(kind, ref_id)``; it
    is authoritative -- the package's reference lists are derived from it.
    ``context_hash()`` fingerprints the manifest's canonical JSON, so the
    hash changes iff the exposed reference set changes (unrelated registry
    documents do not move it).
    """

    manifest_version: str
    goal_id: str
    goal_version: str
    worker_role: WorkerRole
    references: tuple[ContextReference, ...]

    def references_for(
        self, kind: ReferenceKind
    ) -> tuple[ContextReference, ...]:
        """The manifest's references of one kind, in stored (sorted) order.

        Raises:
            TypeError: ``kind`` is not a ``ReferenceKind``.
        """
        if not isinstance(kind, ReferenceKind):
            raise TypeError(
                f"kind must be a ReferenceKind, got {type(kind).__name__}"
            )
        return tuple(r for r in self.references if r.kind is kind)

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the manifest in canonical field order."""
        return {
            "manifest_version": self.manifest_version,
            "goal_id": self.goal_id,
            "goal_version": self.goal_version,
            "worker_role": self.worker_role.value,
            "references": [r.to_dict() for r in self.references],
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"

    def context_hash(self) -> str:
        """SHA-256 hex digest of the manifest's canonical JSON (deterministic)."""
        return hashlib.sha256(
            self.to_canonical_json().encode("utf-8")
        ).hexdigest()


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPackageResult:
    """The outcome of one context generation.

    ``package`` is the frozen ``GoalExecutionContextPackage`` handed to
    the worker (its reference lists are derived from ``manifest``, so the
    manifest is authoritative); ``manifest`` records exactly which
    references were exposed (AC-03) and is the fingerprint source of
    ``package.context_hash``.
    """

    package: GoalExecutionContextPackage
    manifest: ContextManifest

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the result (package + manifest)."""
        return {
            "package": self.package.to_dict(),
            "manifest": self.manifest.to_dict(),
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text of the whole result."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


def generate_goal_context(
    root: str | Path,
    goal: GoalContract,
    *,
    worker_role: WorkerRole,
    evidence_registry: EvidenceRegistry | None = None,
    sources: Mapping[str, ResearchSource] | None = None,
    retry_policy: AutomaticRetryPolicy | None = None,
    environment: Mapping[str, Any] | None = None,
) -> ContextPackageResult:
    """Generate the Goal Execution Context Package for one frozen goal.

    Pure and deterministic: the package is a pure function of the
    registered state at ``root`` and the injectable inputs. The frozen
    goal contract (AC-01) identifies the package (``goal_id`` /
    ``goal_version`` = the frozen record's id and version); the required
    policies come from the goal's automatic engineering retry policy
    (AC-01); sources/evidence/upstream outputs are decided by the
    relevance-reference filter from the candidate registries (AC-02); the
    manifest records exactly which references were exposed and the package
    reference lists are derived from it (AC-03).

    Args:
        root: the initialized workspace root.
        goal: the **frozen** Goal Contract (the record the plan freeze
            produced: ``frozen`` True, formal version ``v<N>``).
        worker_role: the role the context is generated for (the schema's
            ``worker_role`` enum).
        evidence_registry: the project's claim-specific evidence registry
            (the candidate set for the relevance filter; the in-memory
            ``research.evidence.EvidenceRegistry`` -- no file-backed
            registry exists). Records whose ``used_by`` references the
            goal are the goal's relevant evidence.
        sources: the project's source records by source id (candidate set
            for the relevance filter; ``ResearchSource`` records have no
            file-backed registry -- they are injected). The goal's
            relevant sources are the sources of its relevant evidence.
        retry_policy: the goal's automatic engineering retry policy record
            (``core.models.AutomaticRetryPolicy``; no file-backed registry
            exists). Required iff ``goal.automatic_retry_policy_ref`` is
            set, and its ``policy_id`` must equal that ref.
        environment: injectable execution environment (default empty);
            execution-time configuration lives in project/user
            configuration, not Goal contracts (``15-ADAPTER-SPEC.md`` SS2).

    Returns:
        The :class:`ContextPackageResult` with the worker package and the
        authoritative reference manifest.

    Raises:
        TypeError: ``root`` is not a str/Path, ``goal`` is not a
            ``GoalContract``, ``worker_role`` is not a ``WorkerRole``,
            ``evidence_registry`` is neither an ``EvidenceRegistry`` nor
            None, ``sources`` is neither a mapping nor None,
            ``retry_policy`` is neither an ``AutomaticRetryPolicy`` nor
            None, or ``environment`` is neither a mapping nor None.
        ProjectNotInitializedError: no ``project.yaml`` exists at ``root``.
        GoalNotFrozenError: ``goal`` is not the frozen Goal Contract
            (AC-01); stable message.
        ContextBuildError: the frozen goal carries no formal version, or
            the goal references an automatic retry policy but no matching
            policy record was provided; stable messages.
        PolicyMismatchError: the provided policy's ``policy_id`` does not
            match the goal's ``automatic_retry_policy_ref``, or a policy
            is provided for a goal that references none; stable messages.
        GoalNotFoundError: a dependency goal has no registered contract at
            ``root`` (the frozen plan guarantees registration; raised
            loudly, never silently dropped).
        AnalysisProtocolNotFoundError: ``goal.analysis_protocol_ref`` has
            no registered record at ``root``.
        ValueError: a stored registry record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(goal, GoalContract):
        raise TypeError(f"goal must be a GoalContract, got {type(goal).__name__}")
    if not isinstance(worker_role, WorkerRole):
        raise TypeError(
            f"worker_role must be a WorkerRole, got {type(worker_role).__name__}"
        )
    if evidence_registry is not None and not isinstance(
        evidence_registry, EvidenceRegistry
    ):
        raise TypeError(
            "evidence_registry must be an EvidenceRegistry or None, got"
            f" {type(evidence_registry).__name__}"
        )
    if sources is not None and not isinstance(sources, Mapping):
        raise TypeError(
            f"sources must be a mapping or None, got {type(sources).__name__}"
        )
    if retry_policy is not None and not isinstance(retry_policy, AutomaticRetryPolicy):
        raise TypeError(
            "retry_policy must be an AutomaticRetryPolicy or None, got"
            f" {type(retry_policy).__name__}"
        )
    if environment is not None and not isinstance(environment, Mapping):
        raise TypeError(
            f"environment must be a mapping or None, got {type(environment).__name__}"
        )

    project_root = Path(root).resolve()
    _require_initialized(project_root)
    _require_frozen_goal(goal)
    policy = _resolve_policy(goal, retry_policy)

    registry = evidence_registry if evidence_registry is not None else EvidenceRegistry()
    sources_map = dict(sources) if sources is not None else {}

    # Upstream outputs: every dependency goal's declared outputs are the
    # required upstream results; dependency records are read through the
    # real registry (an unregistered dependency is surfaced loudly).
    dependency_ids = tuple(sorted(d.goal_id for d in goal.dependencies))
    explicit_upstream: set[str] = set()
    for dependency_id in dependency_ids:
        upstream = read_goal(project_root, dependency_id)
        explicit_upstream.update(
            f"{dependency_id}#{name}" for name in _output_names(upstream.outputs)
        )

    # Evidence linkage (06-EVIDENCE-SYSTEM.md SS6): a record is relevant
    # to the goal iff its used_by references the goal; the goal's relevant
    # sources are the sources of that evidence.
    relevant_evidence = tuple(
        record
        for record in registry.records
        if goal.goal_id in record.used_by
    )
    evidence_ids = frozenset(record.evidence_id for record in relevant_evidence)
    source_ids = frozenset(record.source_id for record in relevant_evidence)

    explicit_refs = ExplicitReferences(
        goal_id=goal.goal_id,
        protocol_refs=frozenset({goal.analysis_protocol_ref}),
        resource_ids=frozenset(goal.resource_ids),
        policy_refs=frozenset({policy.policy_id}) if policy is not None else frozenset(),
        evidence_ids=evidence_ids,
        source_ids=source_ids,
        upstream_outputs=frozenset(explicit_upstream),
    )

    # Candidate registries run through the relevance filter (AC-02): the
    # default excludes every document the goal does not explicitly
    # reference, so unrelated registered documents never reach the package.
    included_sources = {
        candidate
        for candidate in (set(sources_map) | source_ids)
        if evaluate_relevance(
            RelevanceInput(ReferenceKind.SOURCE, candidate, explicit_refs)
        ).include
    }
    included_evidence = {
        record.evidence_id
        for record in registry.records
        if evaluate_relevance(
            RelevanceInput(
                ReferenceKind.EVIDENCE, record.evidence_id, explicit_refs
            )
        ).include
    }
    candidate_upstream = {
        f"{g.goal_id}#{name}"
        for g in list_goals(project_root)
        for name in _output_names(g.outputs)
    }
    included_upstream = {
        candidate
        for candidate in candidate_upstream
        if evaluate_relevance(
            RelevanceInput(ReferenceKind.UPSTREAM_OUTPUT, candidate, explicit_refs)
        ).include
    }

    # The analysis protocol reference is part of the frozen contract; the
    # manifest records the registered record's protocol_version.
    protocol_version = read_analysis_protocol(
        project_root, goal.analysis_protocol_ref
    ).protocol_version

    references: list[ContextReference] = [
        ContextReference(ReferenceKind.GOAL, goal.goal_id, goal.version),
    ]
    if policy is not None:
        references.append(ContextReference(ReferenceKind.POLICY, policy.policy_id))
    references.append(
        ContextReference(ReferenceKind.PROTOCOL, goal.analysis_protocol_ref, protocol_version)
    )
    references.extend(
        ContextReference(ReferenceKind.RESOURCE, resource_id)
        for resource_id in sorted(goal.resource_ids)
    )
    references.extend(
        ContextReference(ReferenceKind.EVIDENCE, ref_id)
        for ref_id in sorted(included_evidence)
    )
    references.extend(
        ContextReference(ReferenceKind.SOURCE, ref_id)
        for ref_id in sorted(included_sources)
    )
    references.extend(
        ContextReference(ReferenceKind.UPSTREAM_OUTPUT, ref_id)
        for ref_id in sorted(included_upstream)
    )
    manifest = ContextManifest(
        manifest_version=CONTEXT_MANIFEST_VERSION,
        goal_id=goal.goal_id,
        goal_version=goal.version,
        worker_role=worker_role,
        references=tuple(
            sorted(references, key=lambda r: (r.kind.value, r.ref_id))
        ),
    )
    package = _package_from_manifest(
        goal, worker_role, manifest, policy, environment
    )
    return ContextPackageResult(package=package, manifest=manifest)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _package_from_manifest(
    goal: GoalContract,
    worker_role: WorkerRole,
    manifest: ContextManifest,
    policy: AutomaticRetryPolicy | None,
    environment: Mapping[str, Any] | None,
) -> GoalExecutionContextPackage:
    """Build the worker package from the manifest (the manifest is authoritative)."""
    refs_by_kind: dict[ReferenceKind, tuple[str, ...]] = {}
    for kind in ReferenceKind:
        refs_by_kind[kind] = tuple(
            r.ref_id for r in manifest.references if r.kind is kind
        )
    if policy is None:
        allowed_actions: list[str] = []
        forbidden_actions: list[str] = []
    else:
        allowed_actions = sorted(
            f"retry:{failure}" for failure in policy.allowed_engineering_failures
        )
        forbidden_actions = sorted(
            f"retry:{failure}" for failure in policy.supervisor_required_changes
        )
    return GoalExecutionContextPackage(
        context_id=generate_id("context", goal.goal_id, goal.version, worker_role.value),
        worker_role=worker_role,
        goal_id=goal.goal_id,
        goal_version=goal.version,
        allowed_actions=allowed_actions,
        forbidden_actions=forbidden_actions,
        source_refs=list(refs_by_kind[ReferenceKind.SOURCE]),
        evidence_refs=list(refs_by_kind[ReferenceKind.EVIDENCE]),
        upstream_result_refs=list(refs_by_kind[ReferenceKind.UPSTREAM_OUTPUT]),
        protocol_refs=list(refs_by_kind[ReferenceKind.PROTOCOL]),
        resource_refs=list(refs_by_kind[ReferenceKind.RESOURCE]),
        environment=dict(environment) if environment is not None else {},
        required_outputs=list(_output_names(goal.outputs)),
        context_hash=manifest.context_hash(),
    )


def _output_names(outputs: Sequence[Any]) -> tuple[str, ...]:
    """The deterministic output names of a goal's declared outputs.

    ``GoalContract.outputs`` is a list of objects
    (``schemas/goal.schema.yaml``); an output object contributes its
    ``name`` key when that key is a string. Output objects that carry no
    string ``name`` are not returnable artifacts and are skipped
    (documented). Sorted, distinct, deterministic.
    """
    names: list[str] = []
    for output in outputs:
        if isinstance(output, Mapping) and isinstance(output.get("name"), str):
            names.append(output["name"])
    return tuple(sorted(set(names)))


def _require_frozen_goal(goal: GoalContract) -> None:
    """Reject context generation from anything but the frozen Goal Contract.

    AC-01: the frozen variant is the record the plan freeze produced
    (``frozen`` True, formal version ``v<N>``); a draft or a frozen record
    without a formal version cannot produce a worker context.
    """
    if not goal.frozen:
        raise GoalNotFrozenError(
            f"context generation requires the frozen goal contract, got"
            f" frozen=False for goal {goal.goal_id!r}; re-read the frozen"
            " contract from the plan freeze result"
            " (planning.freeze.freeze_plan)"
        )
    if not is_formal_version(goal.version):
        raise ContextBuildError(
            f"frozen goal contract {goal.goal_id!r} must carry a formal"
            f" version 'v<N>', got {goal.version!r}"
        )


def _resolve_policy(
    goal: GoalContract,
    retry_policy: AutomaticRetryPolicy | None,
) -> AutomaticRetryPolicy | None:
    """Resolve the goal's required retry policy, or None when unreferenced.

    A goal that references an automatic retry policy must be paired with
    the matching policy record (its ``policy_id`` equals
    ``automatic_retry_policy_ref``); a policy provided for a goal that
    references none is a caller error. Stable messages.
    """
    ref = goal.automatic_retry_policy_ref
    if ref is None:
        if retry_policy is not None:
            raise PolicyMismatchError(
                f"retry policy {retry_policy.policy_id!r} was provided but"
                f" goal {goal.goal_id!r} references no automatic retry"
                " policy"
            )
        return None
    if retry_policy is None:
        raise ContextBuildError(
            f"goal {goal.goal_id!r} references automatic retry policy"
            f" {ref!r} but no policy record was provided"
        )
    if retry_policy.policy_id != ref:
        raise PolicyMismatchError(
            f"retry policy {retry_policy.policy_id!r} does not match goal"
            f" {goal.goal_id!r}'s automatic_retry_policy_ref {ref!r}"
        )
    return retry_policy


def _require_initialized(root: Path) -> None:
    """Reject operations on a workspace without a project state record."""
    if not (root / PROJECT_STATE_FILENAME).is_file():
        raise ProjectNotInitializedError(
            f"no project state at {root} ({PROJECT_STATE_FILENAME} missing);"
            " initialize the project first"
        )
