"""Goal Execution Context Package generator (DEV-M6-G01).

Every test name contains "context" so ``python -m pytest -q
tests/workers -k context`` selects the whole suite. The ``ac0N`` sections
map one-to-one to the acceptance criteria of DEV-M6-G01:

* ``ac01`` -- the context package carries the **frozen** Goal Contract
  (the record the plan freeze produced: ``frozen`` True, formal version)
  and the goal's required automatic retry policy (allowed engineering
  retries and explicit prohibitions);
* ``ac02`` -- unrelated registry documents are **not** exposed by
  default: the relevance-reference filter's trailing total default
  excludes every candidate the goal does not explicitly reference;
* ``ac03`` -- the deterministic manifest records exactly which references
  were exposed (ids + kinds + versions), the package reference lists are
  derived from it (the manifest is authoritative), and the context hash
  fingerprints the exposed set.

The deterministic path mirrors ``context_helpers``: every fixture uses
fixed identities/timestamps (``FROZEN_AT``) and the frozen Goal Contract
is produced by the real plan freeze flow, so every context record is
deterministic. Helpers are imported read-only from ``context_helpers``.
Note the freeze is one-shot per workspace (a second formal freeze raises
``PlanAlreadyFrozenError``), so each test freezes its workspace exactly
once and reuses the frozen Goal Contract for every generation call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from context_helpers import (
    ROLE,
    build_complete_workspace,
    frozen_goal,
    init_project,
    make_acceptance,
    make_analysis_protocol,
    make_closure,
    make_evidence,
    make_goal,
    make_item,
    make_requirement,
    make_retry_policy,
    make_source,
)

from scientific_reproduction.core.models import GoalContract, WorkerRole
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import ProjectNotInitializedError
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    GoalNotFoundError,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
)
from scientific_reproduction.research.evidence import EvidenceRegistry
from scientific_reproduction.workers.context import (
    CONTEXT_MANIFEST_VERSION,
    RELEVANCE_FILTER_RULES,
    RELEVANCE_FILTER_RULESET_VERSION,
    ContextBuildError,
    ContextPackageResult,
    ContextReference,
    ExplicitReferences,
    GoalNotFrozenError,
    PolicyMismatchError,
    ReferenceKind,
    RelevanceInput,
    evaluate_relevance,
    generate_goal_context,
)

#: The canonical reference set of the standard workspace (GOAL-1): every
#: reference the generated context exposes, in manifest (sorted) order.
EXPECTED_REFERENCES = (
    ContextReference(ReferenceKind.EVIDENCE, "EVID-1"),
    ContextReference(ReferenceKind.GOAL, "GOAL-1", "v1"),
    ContextReference(ReferenceKind.POLICY, "RETRY-ENGINEERING-DEFAULT"),
    ContextReference(ReferenceKind.PROTOCOL, "ANP-1", "v1-draft"),
    ContextReference(ReferenceKind.RESOURCE, "RES-1"),
    ContextReference(ReferenceKind.SOURCE, "SRC-1"),
    ContextReference(ReferenceKind.UPSTREAM_OUTPUT, "GOAL-2#raw_isotherm_data"),
)


def standard_evidence() -> EvidenceRegistry:
    """The standard candidate evidence registry: one record used by the
    goal (``EVID-1``) and one used by another goal (``EVID-2``)."""
    return EvidenceRegistry.from_records(
        [
            make_evidence("EVID-1", "SRC-1", used_by=("GOAL-1",)),
            make_evidence("EVID-2", "SRC-2", used_by=("GOAL-2",)),
        ]
    )


def standard_sources() -> dict[str, object]:
    """The standard candidate source registry: the evidence-linked source
    (``SRC-1``) plus an unrelated source (``SRC-3``)."""
    return {"SRC-1": make_source("SRC-1"), "SRC-3": make_source("SRC-3")}


#: Sentinel distinguishing "no policy passed" from an explicit None.
_POLICY_UNSET = object()


def generate_context(
    root: Path,
    goal: GoalContract,
    *,
    evidence: EvidenceRegistry | None = None,
    sources: dict[str, object] | None = None,
    policy: object = _POLICY_UNSET,
    **kwargs,
) -> ContextPackageResult:
    """Generate the standard context for an already-frozen goal.

    Defaults mirror the standard workspace: the goal's own evidence/source
    candidates plus the matching retry policy record. Pass ``policy=None``
    to generate without a policy record. Callers freeze their workspace
    exactly once (``frozen_goal``) and pass the result.
    """
    if evidence is None:
        evidence = standard_evidence()
    if sources is None:
        sources = standard_sources()
    if policy is _POLICY_UNSET:
        policy = make_retry_policy()
    return generate_goal_context(
        root,
        goal,
        worker_role=ROLE,
        evidence_registry=evidence,
        sources=sources,
        retry_policy=policy,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# AC-01: the context carries the frozen Goal Contract and required policies
# ---------------------------------------------------------------------------


def test_context_ac01_package_identifies_the_frozen_goal_contract(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    assert goal.frozen is True
    assert goal.version == "v1"
    result = generate_context(root, goal)
    package = result.package
    # The package references the frozen record exactly: same id, same
    # frozen version -- never a drifting copy.
    assert package.goal_id == goal.goal_id
    assert package.goal_version == goal.version
    assert result.manifest.goal_id == goal.goal_id
    assert result.manifest.goal_version == goal.version
    goal_entry = result.manifest.references_for(ReferenceKind.GOAL)
    assert goal_entry == (ContextReference(ReferenceKind.GOAL, "GOAL-1", "v1"),)
    # The context id is the deterministic, kind-scoped fingerprint of the
    # frozen record -- never a goal id or a bare hash.
    assert package.context_id.startswith("sr_context_")
    assert package.context_id != goal.goal_id


def test_context_ac01_context_id_is_deterministic(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    first = generate_context(root, goal)
    second = generate_context(root, goal)
    assert first.package.context_id == second.package.context_id
    # The same frozen goal for another role is a different context.
    other = generate_goal_context(
        root,
        goal,
        worker_role=WorkerRole.ANALYSIS_WORKER,
        evidence_registry=standard_evidence(),
        sources=standard_sources(),
        retry_policy=make_retry_policy(),
    )
    assert other.package.context_id != first.package.context_id


def test_context_ac01_draft_goal_rejected(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    draft = replace(goal, frozen=False)
    with pytest.raises(GoalNotFrozenError) as exc:
        generate_goal_context(root, draft, worker_role=ROLE)
    assert "frozen" in str(exc.value)
    assert "GOAL-1" in str(exc.value)


def test_context_ac01_frozen_contract_without_formal_version_rejected(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    draft_version = replace(goal, version="v1-draft")
    with pytest.raises(ContextBuildError) as exc:
        generate_goal_context(root, draft_version, worker_role=ROLE)
    assert "'v<N>'" in str(exc.value)
    assert "v1-draft" in str(exc.value)


def test_context_ac01_required_policy_is_in_context(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    package = result.package
    # Allowed engineering retries from the goal's automatic retry policy;
    # retries that require a Supervisor change are the explicit
    # prohibitions (05-GOAL-RUN-SCHEMA.md SS8).
    assert package.allowed_actions == [
        "retry:instrument_drift",
        "retry:power_cycle",
    ]
    assert package.forbidden_actions == ["retry:protocol_deviation"]
    policy_entries = result.manifest.references_for(ReferenceKind.POLICY)
    assert policy_entries == (
        ContextReference(ReferenceKind.POLICY, "RETRY-ENGINEERING-DEFAULT"),
    )


def test_context_ac01_policy_record_required_when_referenced(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    assert goal.automatic_retry_policy_ref is not None
    with pytest.raises(ContextBuildError) as exc:
        generate_goal_context(
            root,
            goal,
            worker_role=ROLE,
            evidence_registry=standard_evidence(),
            sources=standard_sources(),
        )
    assert "RETRY-ENGINEERING-DEFAULT" in str(exc.value)
    assert "no policy record" in str(exc.value)


def test_context_ac01_policy_id_mismatch_rejected(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    with pytest.raises(PolicyMismatchError) as exc:
        generate_context(root, goal, policy=make_retry_policy("RETRY-OTHER"))
    assert "RETRY-OTHER" in str(exc.value)
    assert "RETRY-ENGINEERING-DEFAULT" in str(exc.value)


def test_context_ac01_policy_for_unreferencing_goal_rejected(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    no_policy_goal = replace(goal, automatic_retry_policy_ref=None)
    with pytest.raises(PolicyMismatchError) as exc:
        generate_context(root, no_policy_goal)
    assert "references no automatic retry policy" in str(exc.value)


def test_context_ac01_goal_without_policy_exposes_no_retry_actions(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    no_policy_goal = replace(goal, automatic_retry_policy_ref=None)
    result = generate_context(root, no_policy_goal, policy=None)
    assert result.package.allowed_actions == []
    assert result.package.forbidden_actions == []
    assert result.manifest.references_for(ReferenceKind.POLICY) == ()


# ---------------------------------------------------------------------------
# AC-02: unrelated project documents are not included by default
# ---------------------------------------------------------------------------


def test_context_ac02_unrelated_evidence_excluded(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    # EVID-2 is used by GOAL-2, not by the context goal: absent.
    assert result.package.evidence_refs == ["EVID-1"]
    assert [r.ref_id for r in result.manifest.references_for(ReferenceKind.EVIDENCE)] == [
        "EVID-1"
    ]


def test_context_ac02_unrelated_source_excluded(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    # SRC-3 is registered but is not a source of this goal's evidence:
    # absent.
    assert result.package.source_refs == ["SRC-1"]
    assert [r.ref_id for r in result.manifest.references_for(ReferenceKind.SOURCE)] == [
        "SRC-1"
    ]


def test_context_ac02_unrelated_upstream_outputs_excluded(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    # GOAL-UNRELATED is registered with an output, but is not a dependency
    # of the context goal: its output must not be exposed as an upstream
    # result.
    assert result.package.upstream_result_refs == ["GOAL-2#raw_isotherm_data"]
    upstream = result.manifest.references_for(ReferenceKind.UPSTREAM_OUTPUT)
    assert [r.ref_id for r in upstream] == ["GOAL-2#raw_isotherm_data"]
    assert "GOAL-UNRELATED#unrelated_artifact" not in [r.ref_id for r in upstream]


def test_context_ac02_empty_registries_yield_empty_reference_lists(tmp_path):
    root = build_complete_workspace(tmp_path)
    result = generate_goal_context(
        root,
        frozen_goal(root),
        worker_role=ROLE,
        retry_policy=make_retry_policy(),
    )
    # Defaults: no evidence/source candidates -> no evidence/source refs.
    assert result.package.evidence_refs == []
    assert result.package.source_refs == []
    # The goal's own explicit references are still exposed.
    assert result.package.protocol_refs == ["ANP-1"]
    assert result.package.resource_refs == ["RES-1"]


def test_context_ac02_unrelated_documents_do_not_change_the_context(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    only_relevant = generate_goal_context(
        root,
        goal,
        worker_role=ROLE,
        evidence_registry=EvidenceRegistry.from_records(
            [make_evidence("EVID-1", "SRC-1", used_by=("GOAL-1",))]
        ),
        sources={"SRC-1": make_source("SRC-1")},
        retry_policy=make_retry_policy(),
    )
    with_extras = generate_context(root, goal)
    # The extra candidates (EVID-2, SRC-3) are filtered out, so the exposed
    # set -- and the context hash -- are identical with or without them.
    assert with_extras.manifest == only_relevant.manifest
    assert with_extras.package.context_hash == only_relevant.package.context_hash
    assert with_extras.package.evidence_refs == ["EVID-1"]
    assert with_extras.package.source_refs == ["SRC-1"]


def test_context_ac02_relevance_default_rule_excludes():
    explicit = ExplicitReferences(
        goal_id="GOAL-1",
        protocol_refs=frozenset({"ANP-1"}),
        resource_ids=frozenset({"RES-1"}),
        policy_refs=frozenset({"RETRY-ENGINEERING-DEFAULT"}),
        evidence_ids=frozenset({"EVID-1"}),
        source_ids=frozenset({"SRC-1"}),
        upstream_outputs=frozenset({"GOAL-2#raw_isotherm_data"}),
    )
    # A candidate that is not explicitly referenced is excluded by the
    # trailing total default (R-REL-D1) -- this is what keeps unrelated
    # registry documents out of the package.
    assessment = evaluate_relevance(
        RelevanceInput(ReferenceKind.SOURCE, "SRC-999", explicit)
    )
    assert assessment.include is False
    assert assessment.matched_rule_id == "R-REL-D1"
    assert assessment.ruleset_version == RELEVANCE_FILTER_RULESET_VERSION
    assert len(assessment.decisions) == len(RELEVANCE_FILTER_RULES)
    assert assessment.decisions[-1].rule_id == "R-REL-D1"
    assert assessment.decisions[-1].matched is True
    assert assessment.decisions[-1].include is False
    # The goal's own refs are included, one rule per kind.
    for kind, ref_id in (
        (ReferenceKind.PROTOCOL, "ANP-1"),
        (ReferenceKind.RESOURCE, "RES-1"),
        (ReferenceKind.POLICY, "RETRY-ENGINEERING-DEFAULT"),
        (ReferenceKind.EVIDENCE, "EVID-1"),
        (ReferenceKind.SOURCE, "SRC-1"),
        (ReferenceKind.UPSTREAM_OUTPUT, "GOAL-2#raw_isotherm_data"),
    ):
        decision = evaluate_relevance(RelevanceInput(kind, ref_id, explicit))
        assert decision.include is True
        assert decision.matched_rule_id != "R-REL-D1"


# ---------------------------------------------------------------------------
# AC-03: the manifest records exactly which references were exposed
# ---------------------------------------------------------------------------


def test_context_ac03_manifest_records_exactly_the_exposed_references(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    assert result.manifest.manifest_version == CONTEXT_MANIFEST_VERSION
    assert result.manifest.worker_role is ROLE
    assert result.manifest.references == EXPECTED_REFERENCES
    # Every included item carries id + kind + version (None where the
    # frozen model declares no version field).
    for ref in result.manifest.references:
        assert ref.ref_id
        assert isinstance(ref.kind, ReferenceKind)


def test_context_ac03_package_reference_lists_match_the_manifest(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    package = result.package
    manifest = result.manifest
    # The manifest is authoritative: the package's reference lists are
    # exactly the manifest's references of the corresponding kinds.
    assert package.source_refs == [
        r.ref_id for r in manifest.references_for(ReferenceKind.SOURCE)
    ]
    assert package.evidence_refs == [
        r.ref_id for r in manifest.references_for(ReferenceKind.EVIDENCE)
    ]
    assert package.upstream_result_refs == [
        r.ref_id for r in manifest.references_for(ReferenceKind.UPSTREAM_OUTPUT)
    ]
    assert package.protocol_refs == [
        r.ref_id for r in manifest.references_for(ReferenceKind.PROTOCOL)
    ]
    assert package.resource_refs == [
        r.ref_id for r in manifest.references_for(ReferenceKind.RESOURCE)
    ]
    # The goal entry is the package's identity fields.
    (goal_entry,) = manifest.references_for(ReferenceKind.GOAL)
    assert goal_entry.ref_id == package.goal_id
    assert goal_entry.version == package.goal_version
    # The policy entry is the retry actions' source: allowed actions are
    # the policy's permitted retries and the manifest records the policy.
    (policy_entry,) = manifest.references_for(ReferenceKind.POLICY)
    assert policy_entry.ref_id == "RETRY-ENGINEERING-DEFAULT"
    assert package.allowed_actions == [
        "retry:instrument_drift",
        "retry:power_cycle",
    ]


def test_context_ac03_manifest_and_package_deterministic(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    first = generate_context(root, goal)
    second = generate_context(root, goal)
    assert first.manifest == second.manifest
    assert first.package == second.package
    assert first.package.context_hash == second.package.context_hash
    # Different workspaces with identical state produce identical contexts.
    other = build_complete_workspace(tmp_path / "other")
    third = generate_context(other, frozen_goal(other))
    assert third.manifest == first.manifest
    assert third.package == first.package


def test_context_ac03_context_hash_fingerprints_the_exposed_set(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    baseline = generate_context(root, goal)
    # A relevant evidence record changes the exposed set -> the hash moves.
    extra = EvidenceRegistry.from_records(
        [
            make_evidence("EVID-1", "SRC-1", used_by=("GOAL-1",)),
            make_evidence("EVID-2", "SRC-2", used_by=("GOAL-2",)),
            make_evidence("EVID-3", "SRC-3", used_by=("GOAL-1",)),
        ]
    )
    changed = generate_context(root, goal, evidence=extra)
    assert changed.package.evidence_refs == ["EVID-1", "EVID-3"]
    assert changed.package.context_hash != baseline.package.context_hash
    # The hash is a real SHA-256 of the manifest's canonical JSON.
    assert len(baseline.package.context_hash) == 64
    expected = hashlib.sha256(
        baseline.manifest.to_canonical_json().encode("utf-8")
    ).hexdigest()
    assert baseline.package.context_hash == expected


def test_context_ac03_manifest_is_sorted(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    # The manifest groups references by kind (in kind order) and sorts the
    # refs of each kind.
    kinds = [r.kind.value for r in result.manifest.references]
    assert kinds == sorted(kinds)
    for kind in ReferenceKind:
        refs = [r.ref_id for r in result.manifest.references if r.kind is kind]
        assert refs == sorted(refs)


def test_context_ac03_manifest_serialization_is_canonical(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    data = json.loads(result.manifest.to_canonical_json())
    assert data == json.loads(json.dumps(result.manifest.to_dict(), sort_keys=True))
    # Canonical JSON: sorted keys, 2-space indent, trailing newline.
    text = result.manifest.to_canonical_json()
    assert text.endswith("\n")
    assert "  \"kind\": \"evidence\"" in text


# ---------------------------------------------------------------------------
# Paradigm boundaries: types, stable errors, frozen records, schema shape
# ---------------------------------------------------------------------------


def test_context_generator_type_error_boundaries(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    with pytest.raises(TypeError, match="root must be a str or Path"):
        generate_goal_context(42, goal, worker_role=ROLE)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="goal must be a GoalContract"):
        generate_goal_context(root, {"goal_id": "GOAL-1"}, worker_role=ROLE)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="worker_role must be a WorkerRole"):
        generate_goal_context(root, goal, worker_role="experiment_worker")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="evidence_registry must be an EvidenceRegistry"):
        generate_goal_context(  # type: ignore[call-overload]
            root,
            goal,
            worker_role=ROLE,
            evidence_registry=["EVID-1"],
            retry_policy=make_retry_policy(),
        )
    with pytest.raises(TypeError, match="sources must be a mapping"):
        generate_goal_context(  # type: ignore[call-overload]
            root,
            goal,
            worker_role=ROLE,
            sources=["SRC-1"],
            retry_policy=make_retry_policy(),
        )
    with pytest.raises(TypeError, match="retry_policy must be an AutomaticRetryPolicy"):
        generate_goal_context(  # type: ignore[call-overload]
            root,
            goal,
            worker_role=ROLE,
            retry_policy={"policy_id": "RETRY"},
        )
    with pytest.raises(TypeError, match="environment must be a mapping"):
        generate_goal_context(  # type: ignore[call-overload]
            root,
            goal,
            worker_role=ROLE,
            environment=["env"],
            retry_policy=make_retry_policy(),
        )


def test_context_relevance_filter_type_error_boundary(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    explicit = ExplicitReferences(
        goal_id="GOAL-1",
        protocol_refs=frozenset({"ANP-1"}),
        resource_ids=frozenset({"RES-1"}),
        policy_refs=frozenset({"RETRY-ENGINEERING-DEFAULT"}),
        evidence_ids=frozenset({"EVID-1"}),
        source_ids=frozenset({"SRC-1"}),
        upstream_outputs=frozenset({"GOAL-2#raw_isotherm_data"}),
    )
    with pytest.raises(TypeError, match="evaluate_relevance expects a RelevanceInput"):
        evaluate_relevance((ReferenceKind.SOURCE, "SRC-1", explicit))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="kind must be a ReferenceKind"):
        goal_manifest = generate_context(root, goal).manifest
        goal_manifest.references_for("source")  # type: ignore[arg-type]


def test_context_stable_error_messages(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    with pytest.raises(GoalNotFrozenError) as exc:
        generate_goal_context(root, replace(goal, frozen=False), worker_role=ROLE)
    assert str(exc.value) == (
        "context generation requires the frozen goal contract, got"
        " frozen=False for goal 'GOAL-1'; re-read the frozen"
        " contract from the plan freeze result"
        " (planning.freeze.freeze_plan)"
    )
    with pytest.raises(ContextBuildError) as exc:
        generate_goal_context(root, replace(goal, version="v1-draft"), worker_role=ROLE)
    assert str(exc.value) == (
        "frozen goal contract 'GOAL-1' must carry a formal"
        " version 'v<N>', got 'v1-draft'"
    )
    with pytest.raises(PolicyMismatchError) as exc:
        generate_context(root, goal, policy=make_retry_policy("RETRY-OTHER"))
    assert str(exc.value) == (
        "retry policy 'RETRY-OTHER' does not match goal 'GOAL-1'"
        "'s automatic_retry_policy_ref 'RETRY-ENGINEERING-DEFAULT'"
    )


def test_context_frozen_package_and_manifest_reject_mutation(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    with pytest.raises(FrozenInstanceError):
        result.package.goal_id = "tampered"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.manifest.goal_version = "v2"  # type: ignore[misc]
    # Mutating a package reference list cannot change the authoritative
    # manifest or the recorded fingerprint: the hash is the manifest's.
    original_hash = result.package.context_hash
    result.package.source_refs.append("SRC-9")
    assert result.package.context_hash == original_hash
    assert all(r.ref_id != "SRC-9" for r in result.manifest.references)


def test_context_package_is_schema_valid(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    validate_and_reject("worker-context", result.package.to_dict())
    assert isinstance(result.package.context_hash, str)
    assert result.package.run_id is None


def test_context_required_outputs_derive_from_goal_outputs(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    assert result.package.required_outputs == ["analysis_input_manifest"]


def test_context_required_outputs_handle_mapping_and_unnamed_outputs(tmp_path):
    root = init_project(tmp_path)
    register_inventory_item(root, make_item("ITEM-1", requirement_ids=("REQ-1",)))
    register_requirement(
        root,
        make_requirement(
            "REQ-1",
            goal_ids=("GOAL-1",),
            inventory_items=("ITEM-1",),
        ),
    )
    register_goal(
        root,
        make_goal(
            "GOAL-1",
            outputs=(
                {"name": "raw_run_data"},
                {"name": "plain_output"},
                {"kind": "no_name_output"},
            ),
        ),
    )
    register_acceptance(root, make_acceptance())
    register_analysis_protocol(root, make_analysis_protocol("ANP-1"))
    register_closure_contract(root, make_closure())
    frozen = frozen_goal(root)
    result = generate_goal_context(
        root,
        frozen,
        worker_role=ROLE,
        retry_policy=make_retry_policy(),
    )
    # String outputs and named mapping outputs are returnable artifacts;
    # mapping outputs without a name are not and are skipped.
    assert result.package.required_outputs == ["plain_output", "raw_run_data"]


def test_context_worker_role_is_recorded(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    result = generate_context(root, goal)
    assert result.package.worker_role is ROLE
    assert result.manifest.worker_role is ROLE
    data = result.manifest.to_dict()
    assert data["worker_role"] == ROLE.value


def test_context_upstream_goal_without_registered_contract_raises(tmp_path):
    root = build_complete_workspace(tmp_path)
    goal = frozen_goal(root)
    # Simulate an upstream goal whose registered contract vanished after
    # the plan freeze: generation reads dependency records through the
    # real registry and must surface the missing record loudly (never
    # silently drop a required upstream result).
    (root / "goals" / "GOAL-2.json").unlink()
    with pytest.raises(GoalNotFoundError) as exc:
        generate_context(root, goal)
    assert "GOAL-2" in str(exc.value)


def test_context_generation_requires_initialized_workspace(tmp_path):
    goal = make_goal("GOAL-1")
    with pytest.raises(ProjectNotInitializedError) as exc:
        generate_goal_context(
            tmp_path,
            replace(goal, frozen=True, version="v1"),
            worker_role=ROLE,
        )
    assert "no project state" in str(exc.value)
