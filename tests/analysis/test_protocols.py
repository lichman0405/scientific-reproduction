"""Frozen Primary Analysis Protocol records (DEV-M9-G01).

Every test name contains "protocol" so ``python -m pytest -q
tests/analysis -k protocol`` selects the whole suite. The ``ac0N``
sections map one-to-one to the acceptance criteria of DEV-M9-G01:

* ``ac01`` -- data analysis acceptance is gated on a frozen PRIMARY
  analysis protocol: the acceptance-gate rule table (``R-ACC-*``) blocks
  acceptance without a registered protocol, with only exploratory
  records, and with an unfrozen primary draft; the freeze
  (``freeze_primary_protocol``) produces the frozen versioned record
  (formal ``protocol_version``, ``frozen`` True, freeze metadata) and the
  gate allows afterwards;
* ``ac02`` -- the primary record is immutable and authoritative: the
  primary-authority rule table (``R-AUTH-*``) rejects any write that
  would overwrite or replace it, while EXPLORATORY records are accepted
  but isolated (their own files, never the primary's);
* ``ac03`` -- formal protocol revision is versioned: ``revise_protocol``
  creates ``v<N+1>-draft`` with ``parent_protocol_version``, the old
  record stays byte untouched, and the versioned ``PROTOCOL_STATUS_RULES``
  table computes FROZEN/SUPERSEDED without any stored mutation.

The deterministic path mirrors ``planning/inventory_helpers``: every
fixture uses fixed identities/timestamps (``FROZEN_AT``), so all records
are deterministic. Helpers come from ``protocol_helpers``.
"""

from __future__ import annotations

import json
import os
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path

import pytest
from protocol_helpers import FROZEN_AT, init_project, make_protocol, make_result

from scientific_reproduction.analysis.protocols import (
    ACCEPTANCE_GATE_RULES,
    ACCEPTANCE_GATE_RULESET_VERSION,
    PRIMARY_AUTHORITY_RULES,
    PRIMARY_AUTHORITY_RULESET_VERSION,
    PROTOCOL_METADATA_VERSION,
    PROTOCOL_STATUS_RULES,
    PROTOCOL_STATUS_RULESET_VERSION,
    AcceptanceGateProhibitedError,
    AnalysisProtocolError,
    DuplicateProtocolVersionError,
    InvalidProtocolIdError,
    InvalidProtocolVersionError,
    PrimaryAuthorityInput,
    PrimaryRecordReplaceProhibitedError,
    ProtocolAlreadyFrozenError,
    ProtocolNotDraftError,
    ProtocolNotFoundError,
    ProtocolNotFrozenError,
    ProtocolNotPrimaryError,
    ProtocolStateMismatchError,
    ProtocolStatusInput,
    ProtocolVersionMetadata,
    assert_acceptance_eligible,
    evaluate_acceptance_gate,
    evaluate_primary_authority,
    evaluate_protocol_status,
    freeze_primary_protocol,
    list_protocol_versions,
    protocol_lineage,
    read_protocol_version,
    register_analysis_record,
    revise_protocol,
)
from scientific_reproduction.audit.git import current_head
from scientific_reproduction.core.models import (
    AnalysisKind,
    AnalysisProtocolOrResult,
    PlanStatus,
    PrimaryOrExploratory,
)
from scientific_reproduction.core.schema_validation import validate_and_reject
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    ProjectNotInitializedError,
)
from scientific_reproduction.planning.plan import (
    list_analysis_protocols,
    read_analysis_protocol,
    register_analysis_protocol,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def build_primary_workspace(tmp_path: Path) -> Path:
    """Initialize a project with a registered PRIMARY protocol draft."""
    root = init_project(tmp_path)
    register_analysis_record(root, make_protocol("ANL-1"))
    return root


def freeze_primary(root: Path):
    """Freeze the registered ANL-1 draft deterministically."""
    draft = read_analysis_protocol(root, "ANL-1")
    return freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)


def _canonical(data: dict) -> str:
    """The registry's canonical JSON serialization."""
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# AC-01: primary protocol frozen before data analysis acceptance
# ---------------------------------------------------------------------------


def test_protocol_ac01_gate_blocks_acceptance_when_no_protocol_registered(
    tmp_path,
):
    root = init_project(tmp_path)
    assessment = evaluate_acceptance_gate(root, "ANL-1")
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-ACC-N1"
    assert assessment.input.protocol_registered is False
    with pytest.raises(AcceptanceGateProhibitedError) as exc:
        assert_acceptance_eligible(root, "ANL-1")
    assert "ANL-1" in str(exc.value)
    assert "R-ACC-N1" in str(exc.value)


def test_protocol_ac01_gate_blocks_unfrozen_primary_draft(tmp_path):
    root = build_primary_workspace(tmp_path)
    assessment = evaluate_acceptance_gate(root, "ANL-1")
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-ACC-U1"
    assert assessment.input.protocol_registered is True
    assert assessment.input.primary_protocol_registered is True
    assert assessment.input.primary_protocol_frozen is False


def test_protocol_ac01_gate_blocks_exploratory_only_protocol(tmp_path):
    root = init_project(tmp_path)
    register_analysis_record(
        root, make_protocol("ANL-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
    )
    assessment = evaluate_acceptance_gate(root, "ANL-1")
    assert assessment.allowed is False
    assert assessment.matched_rule_id == "R-ACC-E1"
    assert assessment.input.primary_protocol_registered is False


def test_protocol_ac01_gate_allows_only_after_frozen_primary_protocol(
    tmp_path,
):
    root = build_primary_workspace(tmp_path)
    # AC-01: before the freeze, acceptance is prohibited.
    before = evaluate_acceptance_gate(root, "ANL-1")
    assert before.allowed is False
    assert before.matched_rule_id == "R-ACC-U1"
    with pytest.raises(AcceptanceGateProhibitedError):
        assert_acceptance_eligible(root, "ANL-1")
    # The freeze produces the frozen primary protocol record.
    result = freeze_primary(root)
    assert result.frozen_record.frozen is True
    # AC-01: with the frozen primary protocol registered, acceptance is
    # allowed -- the gate is a pure function of the registered state.
    after = evaluate_acceptance_gate(root, "ANL-1")
    assert after.allowed is True
    assert after.matched_rule_id == "R-ACC-A1"
    assert after.input.primary_protocol_frozen is True
    assert_acceptance_eligible(root, "ANL-1")  # returns silently


def test_protocol_ac01_freeze_creates_frozen_record_with_metadata(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    frozen = result.frozen_record
    assert frozen.analysis_id == "ANL-1"
    assert frozen.kind is AnalysisKind.PROTOCOL
    assert frozen.primary_or_exploratory is PrimaryOrExploratory.PRIMARY
    assert frozen.protocol_version == "v1"
    assert frozen.frozen is True
    assert frozen.methods == [{"name": "isotherm_fit"}]
    assert result.frozen_at == "2026-06-01T00:00:00Z"
    assert result.frozen_commit == current_head(root)
    assert result.frozen_commit is not None  # workspace is a Git repository
    assert result.parent_protocol_version is None
    # The frozen record is persisted at the versioned registry path.
    state_path = root / "protocols" / "versions" / "ANL-1@v1.json"
    assert state_path.is_file()
    stored = read_protocol_version(root, "ANL-1", "v1")
    assert stored.record == frozen
    assert stored.metadata.frozen_at == "2026-06-01T00:00:00Z"
    assert stored.metadata.frozen_commit == result.frozen_commit
    assert stored.metadata.parent_protocol_version is None
    assert stored.metadata.metadata_version == PROTOCOL_METADATA_VERSION


def test_protocol_ac01_freeze_persists_schema_valid_canonical_record(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    state_path = root / "protocols" / "versions" / "ANL-1@v1.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    # The versioned file is schema-validated, including the extra
    # schema-permitted freeze metadata keys (additionalProperties: true).
    validate_and_reject("analysis", raw)
    assert raw["protocol_version"] == "v1"
    assert raw["frozen"] is True
    assert raw["frozen_at"] == "2026-06-01T00:00:00Z"
    assert raw["frozen_commit"] == result.frozen_commit
    assert raw["metadata_version"] == PROTOCOL_METADATA_VERSION
    # Canonical serialization: sorted keys, 2-space indent, trailing newline.
    assert state_path.read_text(encoding="utf-8") == _canonical(raw)
    # The model layer round-trips the schema fields only.
    assert AnalysisProtocolOrResult.from_dict(raw) == result.frozen_record


def test_protocol_ac01_freeze_does_not_touch_draft_file(tmp_path):
    root = build_primary_workspace(tmp_path)
    draft_path = root / "protocols" / "ANL-1.json"
    before = draft_path.read_bytes()
    freeze_primary(root)
    assert draft_path.read_bytes() == before
    draft = read_analysis_protocol(root, "ANL-1")
    assert draft.frozen is False
    assert draft.protocol_version == INITIAL_PLAN_VERSION


def test_protocol_ac01_freeze_requires_primary_protocol(tmp_path):
    root = init_project(tmp_path)
    register_analysis_record(
        root, make_protocol("ANL-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
    )
    with pytest.raises(ProtocolNotPrimaryError) as exc:
        freeze_primary_protocol(root, read_analysis_protocol(root, "ANL-1"), timestamp=FROZEN_AT)
    assert "EXPLORATORY" in str(exc.value)
    register_analysis_record(root, make_result("ANL-2"))
    with pytest.raises(ProtocolNotPrimaryError):
        freeze_primary_protocol(root, read_analysis_protocol(root, "ANL-2"), timestamp=FROZEN_AT)


def test_protocol_ac01_freeze_rejects_already_frozen_and_second_freeze(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    # Freezing the frozen record itself is rejected (not a draft).
    with pytest.raises(ProtocolNotDraftError):
        freeze_primary_protocol(root, result.frozen_record, timestamp=FROZEN_AT)
    # A second freeze of the same draft (same formal version) is rejected.
    with pytest.raises(ProtocolAlreadyFrozenError) as exc:
        freeze_primary(root)
    assert "v1" in str(exc.value)
    # A frozen record registered through the draft registry is rejected too.
    register_analysis_protocol(root, make_protocol("ANL-2", frozen=True))
    with pytest.raises(ProtocolNotDraftError):
        freeze_primary_protocol(root, read_analysis_protocol(root, "ANL-2"), timestamp=FROZEN_AT)


def test_protocol_ac01_freeze_rejects_non_draft_version(tmp_path):
    root = init_project(tmp_path)
    register_analysis_protocol(root, make_protocol("ANL-1", protocol_version="v1"))
    with pytest.raises(InvalidProtocolVersionError) as exc:
        freeze_primary_protocol(root, read_analysis_protocol(root, "ANL-1"), timestamp=FROZEN_AT)
    assert "v<N>-draft" in str(exc.value)
    assert "v1" in str(exc.value)


def test_protocol_ac01_freeze_rejects_unregistered_and_stale_drafts(tmp_path):
    root = init_project(tmp_path)
    draft = make_protocol("ANL-1")
    with pytest.raises(ProtocolNotFoundError):
        freeze_primary_protocol(root, draft, timestamp=FROZEN_AT)
    register_analysis_record(root, draft)
    tampered = replace(draft, methods=[{"name": "tampered_fit"}])
    with pytest.raises(ProtocolStateMismatchError):
        freeze_primary_protocol(root, tampered, timestamp=FROZEN_AT)
    # A failed freeze writes no record.
    assert not (root / "protocols" / "versions").exists()


def test_protocol_ac01_frozen_commit_none_outside_git_repository(tmp_path):
    root = build_primary_workspace(tmp_path)
    # Suspend the Git repository (rename, not delete: Git object files are
    # read-only on Windows) so current_head reports "not a repository".
    os.rename(root / ".git", root / ".git-suspended")
    try:
        result = freeze_primary_protocol(root, read_analysis_protocol(root, "ANL-1"), timestamp=FROZEN_AT)
    finally:
        os.rename(root / ".git-suspended", root / ".git")
    assert result.frozen_commit is None
    assert result.frozen_record.frozen is True
    assert result.frozen_at == "2026-06-01T00:00:00Z"
    stored = read_protocol_version(root, "ANL-1", "v1")
    assert stored.metadata.frozen_commit is None


def test_protocol_ac01_gate_decision_trace_and_ruleset_version(tmp_path):
    root = build_primary_workspace(tmp_path)
    assessment = evaluate_acceptance_gate(root, "ANL-1")
    assert assessment.ruleset_version == ACCEPTANCE_GATE_RULESET_VERSION
    assert len(assessment.decisions) == len(ACCEPTANCE_GATE_RULES)
    assert [d.rule_id for d in assessment.decisions] == [
        r.rule_id for r in ACCEPTANCE_GATE_RULES
    ]
    assert [d.matched for d in assessment.decisions] == [False, False, True, True]
    assert assessment.input.analysis_id == "ANL-1"
    assert assessment.allowed is False


def test_protocol_ac01_deterministic_freeze_across_workspaces(tmp_path):
    root_a = build_primary_workspace(tmp_path / "a")
    root_b = build_primary_workspace(tmp_path / "b")
    result_a = freeze_primary(root_a)
    result_b = freeze_primary(root_b)
    assert result_a.frozen_record == result_b.frozen_record
    assert result_a.frozen_at == result_b.frozen_at
    assert result_a.frozen_commit == result_b.frozen_commit
    assert result_a.frozen_record.to_dict() == result_b.frozen_record.to_dict()


# ---------------------------------------------------------------------------
# AC-02: exploratory analysis cannot overwrite/replace the primary result
# ---------------------------------------------------------------------------


def test_protocol_ac02_exploratory_record_accepted_and_isolated(tmp_path):
    root = build_primary_workspace(tmp_path)
    primary_draft = read_analysis_protocol(root, "ANL-1")
    primary_path = root / "protocols" / "ANL-1.json"
    before = primary_path.read_bytes()
    # An exploratory record under its own id is accepted.
    exploratory = make_protocol(
        "ANL-EX-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY
    )
    registered = register_analysis_record(root, exploratory)
    assert registered == exploratory
    # Isolated: its own file, the primary file byte-untouched.
    assert (root / "protocols" / "ANL-EX-1.json").is_file()
    assert primary_path.read_bytes() == before
    assert read_analysis_protocol(root, "ANL-1") == primary_draft
    # The exploratory record never appears in the primary lineage.
    assert [v.record.analysis_id for v in list_protocol_versions(root, "ANL-1")] == ["ANL-1"]
    assert [v.record.analysis_id for v in list_protocol_versions(root, "ANL-EX-1")] == ["ANL-EX-1"]


def test_protocol_ac02_replacing_primary_protocol_rejected(tmp_path):
    root = build_primary_workspace(tmp_path)
    with pytest.raises(PrimaryRecordReplaceProhibitedError) as exc:
        register_analysis_record(root, make_protocol("ANL-1"))
    assert "R-AUTH-P2" in str(exc.value)
    assert "immutable" in str(exc.value)
    # The failed write leaves no second record and the draft intact.
    assert read_analysis_protocol(root, "ANL-1").frozen is False


def test_protocol_ac02_exploratory_write_to_primary_id_rejected(tmp_path):
    root = build_primary_workspace(tmp_path)
    with pytest.raises(PrimaryRecordReplaceProhibitedError) as exc:
        register_analysis_record(
            root,
            make_protocol("ANL-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY),
        )
    assert "R-AUTH-P1" in str(exc.value)


def test_protocol_ac02_primary_result_immutable_and_authoritative(tmp_path):
    root = init_project(tmp_path)
    primary_result = make_result("ANL-1", primary_or_exploratory=PrimaryOrExploratory.PRIMARY)
    register_analysis_record(root, primary_result)
    result_path = root / "protocols" / "ANL-1.json"
    before = result_path.read_bytes()
    # The primary result record cannot be replaced by another primary.
    with pytest.raises(PrimaryRecordReplaceProhibitedError) as exc:
        register_analysis_record(root, primary_result)
    assert "R-AUTH-P2" in str(exc.value)
    # Nor by an exploratory record claiming the same id.
    with pytest.raises(PrimaryRecordReplaceProhibitedError) as exc:
        register_analysis_record(
            root, make_result("ANL-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
        )
    assert "R-AUTH-P1" in str(exc.value)
    # Exploratory results live alongside, under their own id.
    register_analysis_record(
        root, make_result("ANL-EX-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
    )
    assert result_path.read_bytes() == before
    assert (root / "protocols" / "ANL-EX-1.json").is_file()
    assert read_analysis_protocol(root, "ANL-1") == primary_result


def test_protocol_ac02_exploratory_write_after_freeze_isolated(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    frozen_path = root / "protocols" / "versions" / "ANL-1@v1.json"
    before = frozen_path.read_bytes()
    register_analysis_record(
        root, make_protocol("ANL-EX-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
    )
    assert frozen_path.read_bytes() == before
    lineage = protocol_lineage(root, "ANL-1")
    assert [e.record.protocol_version for e in lineage] == ["v1-draft", "v1"]
    assert all(e.record.analysis_id == "ANL-1" for e in lineage)
    # The frozen primary remains the authoritative acceptance basis.
    assert evaluate_acceptance_gate(root, "ANL-1").allowed is True
    # Any write that would touch the primary record is still rejected.
    with pytest.raises(PrimaryRecordReplaceProhibitedError):
        register_analysis_record(
            root, make_protocol("ANL-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
        )
    # The frozen primary record itself cannot be re-registered: the
    # registry holds pre-freeze records only (the frozen record is written
    # by the freeze flow, exactly once per version).
    with pytest.raises(ProtocolAlreadyFrozenError):
        register_analysis_record(root, result.frozen_record)


def test_protocol_ac02_frozen_primary_rejects_direct_mutation(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    with pytest.raises(FrozenInstanceError):
        result.frozen_record.frozen = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.frozen_record.protocol_version = "v2"  # type: ignore[misc]
    stored = read_protocol_version(root, "ANL-1", "v1").record
    with pytest.raises(FrozenInstanceError):
        stored.methods = []  # type: ignore[misc]


def test_protocol_ac02_no_clobber_any_analysis_id(tmp_path):
    root = init_project(tmp_path)
    register_analysis_record(
        root, make_protocol("ANL-EX-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
    )
    with pytest.raises(PrimaryRecordReplaceProhibitedError) as exc:
        register_analysis_record(
            root, make_protocol("ANL-EX-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
        )
    assert "R-AUTH-D1" in str(exc.value)
    with pytest.raises(PrimaryRecordReplaceProhibitedError) as exc:
        register_analysis_record(root, make_protocol("ANL-EX-1"))
    assert "R-AUTH-D1" in str(exc.value)


def test_protocol_ac02_primary_authority_rule_table_first_match_and_total():
    # R-AUTH-P1: an exploratory write where a primary record is registered.
    blocked_p1 = evaluate_primary_authority(
        PrimaryOrExploratory.EXPLORATORY, True, True
    )
    assert blocked_p1.allowed is False
    assert blocked_p1.matched_rule_id == "R-AUTH-P1"
    # R-AUTH-P2: a primary write where a primary record is registered.
    blocked_p2 = evaluate_primary_authority(
        PrimaryOrExploratory.PRIMARY, True, True
    )
    assert blocked_p2.allowed is False
    assert blocked_p2.matched_rule_id == "R-AUTH-P2"
    # R-AUTH-D1: any second write to a taken (non-primary) id.
    blocked_d1 = evaluate_primary_authority(
        PrimaryOrExploratory.EXPLORATORY, False, True
    )
    assert blocked_d1.allowed is False
    assert blocked_d1.matched_rule_id == "R-AUTH-D1"
    # R-AUTH-A1 (the total default): first isolated registrations.
    assert evaluate_primary_authority(
        PrimaryOrExploratory.PRIMARY, False, False
    ).allowed is True
    assert evaluate_primary_authority(
        PrimaryOrExploratory.EXPLORATORY, False, False
    ).allowed is True
    # The trailing default matches every input (totality).
    default = PRIMARY_AUTHORITY_RULES[-1]
    assert default.rule_id == "R-AUTH-A1"
    assert default.allowed is True
    assert default.predicate(PrimaryAuthorityInput(PrimaryOrExploratory.PRIMARY, True, True)) is True
    assert default.predicate(PrimaryAuthorityInput(PrimaryOrExploratory.EXPLORATORY, False, True)) is True
    assert PRIMARY_AUTHORITY_RULESET_VERSION == "1.0"


def test_protocol_ac02_primary_still_authoritative_after_revision(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    v1_path = root / "protocols" / "versions" / "ANL-1@v1.json"
    before = v1_path.read_bytes()
    revise_protocol(root, result.frozen_record)
    # The old record is preserved and still authoritative: no write can
    # replace or clobber it after the revision either.
    assert v1_path.read_bytes() == before
    with pytest.raises(PrimaryRecordReplaceProhibitedError):
        register_analysis_record(root, make_protocol("ANL-1"))
    with pytest.raises(PrimaryRecordReplaceProhibitedError):
        register_analysis_record(
            root, make_protocol("ANL-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
        )


# ---------------------------------------------------------------------------
# AC-03: formal protocol revision is versioned
# ---------------------------------------------------------------------------


def test_protocol_ac03_revision_creates_next_draft_with_parent_link(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    revised = revise_protocol(root, result.frozen_record)
    assert revised.record.protocol_version == "v2-draft"
    assert revised.record.frozen is False
    assert revised.metadata.parent_protocol_version == "v1"
    assert revised.metadata.frozen_at is None
    assert revised.metadata.frozen_commit is None
    # Content baseline is copied from the frozen record.
    assert revised.record.methods == result.frozen_record.methods
    assert revised.record.analysis_id == result.frozen_record.analysis_id
    assert revised.record.kind is AnalysisKind.PROTOCOL
    assert revised.record.primary_or_exploratory is PrimaryOrExploratory.PRIMARY
    # The revision draft is persisted at the versioned path.
    state_path = root / "protocols" / "versions" / "ANL-1@v2-draft.json"
    assert state_path.is_file()
    stored = read_protocol_version(root, "ANL-1", "v2-draft")
    assert stored.record == revised.record
    assert stored.metadata.parent_protocol_version == "v1"


def test_protocol_ac03_revision_preserves_old_record_untouched(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    frozen_path = root / "protocols" / "versions" / "ANL-1@v1.json"
    before = frozen_path.read_bytes()
    draft_path = root / "protocols" / "ANL-1.json"
    draft_before = draft_path.read_bytes()
    revise_protocol(root, result.frozen_record)
    assert frozen_path.read_bytes() == before
    assert draft_path.read_bytes() == draft_before
    assert read_protocol_version(root, "ANL-1", "v1").record == result.frozen_record
    assert read_protocol_version(root, "ANL-1", "v1").record.frozen is True
    assert [v.record.protocol_version for v in list_protocol_versions(root, "ANL-1")] == [
        "v1-draft",
        "v1",
        "v2-draft",
    ]


def test_protocol_ac03_old_version_reported_superseded_in_lineage(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    revise_protocol(root, result.frozen_record)
    entries = protocol_lineage(root, "ANL-1")
    assert [e.record.protocol_version for e in entries] == [
        "v1-draft",
        "v1",
        "v2-draft",
    ]
    assert [e.status for e in entries] == [
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
    ]
    assert entries[1].assessment.matched_rule_id == "R-PROT-P1"
    assert entries[2].assessment.matched_rule_id == "R-PROT-D1"
    # The stored record was never rewritten: SUPERSEDED is a computed
    # lineage status, not a stored mutation.
    assert read_protocol_version(root, "ANL-1", "v1").record.frozen is True


def test_protocol_ac03_lineage_shows_frozen_until_revision(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    entries = protocol_lineage(root, "ANL-1")
    assert [e.record.protocol_version for e in entries] == ["v1-draft", "v1"]
    assert [e.status for e in entries] == [PlanStatus.DRAFT, PlanStatus.FROZEN]
    assert entries[1].assessment.matched_rule_id == "R-PROT-F1"
    assert entries[1].record == result.frozen_record
    assert entries[1].metadata.frozen_at == "2026-06-01T00:00:00Z"


def test_protocol_ac03_revision_requires_registered_frozen_protocol(tmp_path):
    root = build_primary_workspace(tmp_path)
    draft = read_analysis_protocol(root, "ANL-1")  # never frozen
    with pytest.raises(ProtocolNotFrozenError) as exc:
        revise_protocol(root, draft)
    assert "FROZEN" in str(exc.value)
    tampered = make_protocol("ANL-1", protocol_version="v1", frozen=True)
    with pytest.raises(ProtocolNotFoundError):
        revise_protocol(root, tampered)
    root2 = init_project(tmp_path / "bare")
    ghost = make_protocol("ANL-1", protocol_version="v1", frozen=True)
    with pytest.raises(ProtocolNotFoundError):
        revise_protocol(root2, ghost)


def test_protocol_ac03_revision_rejects_stale_plan_object(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    tampered = replace(result.frozen_record, methods=[])
    with pytest.raises(ProtocolStateMismatchError):
        revise_protocol(root, tampered)


def test_protocol_ac03_revision_of_revision_extends_lineage(tmp_path):
    root = build_primary_workspace(tmp_path)
    v1 = freeze_primary(root).frozen_record
    v2_draft = revise_protocol(root, v1)
    v2 = freeze_primary_protocol(root, v2_draft.record, timestamp=FROZEN_AT).frozen_record
    assert v2.protocol_version == "v2"
    assert v2.frozen is True
    v3_draft = revise_protocol(root, v2)
    assert v3_draft.record.protocol_version == "v3-draft"
    assert v3_draft.metadata.parent_protocol_version == "v2"
    entries = protocol_lineage(root, "ANL-1")
    assert [e.record.protocol_version for e in entries] == [
        "v1-draft",
        "v1",
        "v2-draft",
        "v2",
        "v3-draft",
    ]
    assert [e.status for e in entries] == [
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
    ]
    # Bytes untouched across the whole revision chain.
    assert read_protocol_version(root, "ANL-1", "v1").record == v1
    assert read_protocol_version(root, "ANL-1", "v2").record == v2
    assert read_protocol_version(root, "ANL-1", "v2").metadata.parent_protocol_version == "v1"


def test_protocol_ac03_revision_freeze_produces_formal_next_version(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    v2_draft = revise_protocol(root, result.frozen_record)
    v2 = freeze_primary_protocol(root, v2_draft.record, timestamp=FROZEN_AT)
    assert v2.frozen_record.protocol_version == "v2"
    assert v2.frozen_record.frozen is True
    assert v2.parent_protocol_version == "v1"
    assert v2.frozen_at == "2026-06-01T00:00:00Z"
    stored = read_protocol_version(root, "ANL-1", "v2")
    assert stored.metadata.parent_protocol_version == "v1"
    assert stored.metadata.frozen_at == "2026-06-01T00:00:00Z"
    # v2 is the current frozen version; v1 is superseded in the lineage.
    entries = protocol_lineage(root, "ANL-1")
    assert [e.status for e in entries] == [
        PlanStatus.DRAFT,
        PlanStatus.SUPERSEDED,
        PlanStatus.DRAFT,
        PlanStatus.FROZEN,
    ]


def test_protocol_ac03_revision_rejects_duplicate_next_version(tmp_path):
    root = build_primary_workspace(tmp_path)
    frozen = freeze_primary(root).frozen_record
    # Pre-register the v2-draft version by hand (canonical JSON).
    data = replace(frozen, protocol_version="v2-draft", frozen=False).to_dict()
    data["metadata_version"] = PROTOCOL_METADATA_VERSION
    data["parent_protocol_version"] = "v1"
    versions_dir = root / "protocols" / "versions"
    versions_dir.mkdir(exist_ok=True)
    (versions_dir / "ANL-1@v2-draft.json").write_text(_canonical(data), encoding="utf-8")
    with pytest.raises(DuplicateProtocolVersionError) as exc:
        revise_protocol(root, frozen)
    assert "v2-draft" in str(exc.value)


def test_protocol_ac03_revision_rejects_drafts_and_exploratory(tmp_path):
    root = build_primary_workspace(tmp_path)
    draft = read_analysis_protocol(root, "ANL-1")
    with pytest.raises(ProtocolNotFrozenError):
        revise_protocol(root, draft)
    # Exploratory records are never frozen and never revised.
    register_analysis_record(
        root, make_protocol("ANL-EX-1", primary_or_exploratory=PrimaryOrExploratory.EXPLORATORY)
    )
    with pytest.raises(ProtocolNotPrimaryError):
        revise_protocol(root, read_analysis_protocol(root, "ANL-EX-1"))
    register_analysis_record(root, make_result("ANL-R-1"))
    with pytest.raises(ProtocolNotPrimaryError):
        revise_protocol(root, read_analysis_protocol(root, "ANL-R-1"))


def test_protocol_ac03_protocol_status_ruleset_versioned_and_total():
    assert PROTOCOL_STATUS_RULESET_VERSION == "1.0"
    assert [r.rule_id for r in PROTOCOL_STATUS_RULES] == [
        "R-PROT-D1",
        "R-PROT-P1",
        "R-PROT-F1",
    ]
    # The trailing rule is a total default: it matches every input.
    default = PROTOCOL_STATUS_RULES[-1]
    assert default.rule_id == "R-PROT-F1"
    assert default.predicate(ProtocolStatusInput(True, True)) is True
    assert default.predicate(ProtocolStatusInput(True, False)) is True


def test_protocol_ac03_protocol_status_first_match_wins():
    draft = evaluate_protocol_status(False, True)
    assert draft.status is PlanStatus.DRAFT
    assert draft.matched_rule_id == "R-PROT-D1"
    superseded = evaluate_protocol_status(True, True)
    assert superseded.status is PlanStatus.SUPERSEDED
    assert superseded.matched_rule_id == "R-PROT-P1"
    still_frozen = evaluate_protocol_status(True, False)
    assert still_frozen.status is PlanStatus.FROZEN
    assert still_frozen.matched_rule_id == "R-PROT-F1"


def test_protocol_ac03_protocol_status_assessment_records_decision_trace():
    assessment = evaluate_protocol_status(True, True)
    assert len(assessment.decisions) == len(PROTOCOL_STATUS_RULES)
    assert [d.rule_id for d in assessment.decisions] == [
        r.rule_id for r in PROTOCOL_STATUS_RULES
    ]
    assert [d.matched for d in assessment.decisions] == [False, True, True]
    assert assessment.input == ProtocolStatusInput(True, True)
    assert assessment.ruleset_version == PROTOCOL_STATUS_RULESET_VERSION
    assert assessment.status is PlanStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# Paradigm: purity, determinism, boundaries, error hierarchy
# ---------------------------------------------------------------------------


def test_protocol_register_record_mapping_input_defaults(tmp_path):
    root = init_project(tmp_path)
    registered = register_analysis_record(
        root,
        {
            "analysis_id": "ANL-1",
            "kind": "protocol",
            "primary_or_exploratory": "PRIMARY",
        },
    )
    assert registered.protocol_version == INITIAL_PLAN_VERSION
    assert registered.frozen is False
    assert registered.profile is None
    stored = read_analysis_protocol(root, "ANL-1")
    assert stored == registered


def test_protocol_registry_canonical_json_and_deterministic_listing(tmp_path):
    root = build_primary_workspace(tmp_path)
    draft = read_analysis_protocol(root, "ANL-1")
    raw = (root / "protocols" / "ANL-1.json").read_text(encoding="utf-8")
    assert raw == _canonical(draft.to_dict())
    assert json.loads(raw) == draft.to_dict()
    assert list_protocol_versions(root, "ANL-1")[0].record == draft
    assert list_protocol_versions(root, "ANL-1")[0].metadata == ProtocolVersionMetadata()


def test_protocol_read_roundtrip_and_deterministic_order(tmp_path):
    root = build_primary_workspace(tmp_path)
    result = freeze_primary(root)
    revised = revise_protocol(root, result.frozen_record)
    assert read_protocol_version(root, "ANL-1", "v1").record == result.frozen_record
    assert read_protocol_version(root, "ANL-1", "v1-draft").record == read_analysis_protocol(root, "ANL-1")
    assert read_protocol_version(root, "ANL-1", "v2-draft").record == revised.record
    assert [v.record.protocol_version for v in list_protocol_versions(root, "ANL-1")] == [
        "v1-draft",
        "v1",
        "v2-draft",
    ]


def test_protocol_versioned_records_invisible_to_planning_registry(tmp_path):
    root = build_primary_workspace(tmp_path)
    freeze_primary(root)
    revise_protocol(root, read_protocol_version(root, "ANL-1", "v1").record)
    # The DEV-M4-G04 plan-freeze registry sees exactly the id-keyed
    # drafts: versioned records live in the sibling subdirectory, which
    # its non-recursive ``*.json`` glob does not descend into.
    assert [a.analysis_id for a in list_analysis_protocols(root)] == ["ANL-1"]
    draft = list_analysis_protocols(root)[0]
    assert draft.protocol_version == INITIAL_PLAN_VERSION
    assert draft.frozen is False


def test_protocol_type_error_boundaries(tmp_path):
    root = init_project(tmp_path)
    draft = make_protocol("ANL-1")
    with pytest.raises(TypeError):
        register_analysis_record(123, draft)
    with pytest.raises(TypeError):
        register_analysis_record(root, "not-a-record")
    with pytest.raises(TypeError):
        freeze_primary_protocol(root, "not-a-protocol")
    with pytest.raises(TypeError):
        freeze_primary_protocol(root, draft, timestamp="2026-06-01")
    with pytest.raises(TypeError):
        revise_protocol(root, {"analysis_id": "ANL-1"})
    with pytest.raises(TypeError):
        evaluate_acceptance_gate(root, 5)
    with pytest.raises(TypeError):
        evaluate_acceptance_gate(3, "ANL-1")
    with pytest.raises(TypeError):
        assert_acceptance_eligible(root, None)
    with pytest.raises(TypeError):
        read_protocol_version(root, "ANL-1", 5)
    with pytest.raises(TypeError):
        read_protocol_version(root, 5, "v1")
    with pytest.raises(TypeError):
        list_protocol_versions(3, "ANL-1")
    with pytest.raises(TypeError):
        protocol_lineage(root, 7)
    with pytest.raises(TypeError):
        evaluate_protocol_status("yes", False)
    with pytest.raises(TypeError):
        evaluate_protocol_status(False, "yes")
    with pytest.raises(TypeError):
        evaluate_primary_authority("PRIMARY", False, False)
    with pytest.raises(TypeError):
        evaluate_primary_authority(PrimaryOrExploratory.PRIMARY, "yes", False)
    with pytest.raises(TypeError):
        evaluate_primary_authority(PrimaryOrExploratory.PRIMARY, False, 1)


def test_protocol_naive_timestamp_rejected(tmp_path):
    root = build_primary_workspace(tmp_path)
    with pytest.raises(ValueError):
        freeze_primary_protocol(
            root,
            read_analysis_protocol(root, "ANL-1"),
            timestamp=datetime(2026, 1, 1),
        )


def test_protocol_error_hierarchy_and_stable_messages(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(ProtocolNotFoundError) as first:
        read_protocol_version(root, "ANL-1", "v1")
    with pytest.raises(ProtocolNotFoundError) as second:
        read_protocol_version(root, "ANL-1", "v1")
    assert str(first.value) == str(second.value)
    assert isinstance(first.value, AnalysisProtocolError)
    assert isinstance(first.value, ValueError)
    for error_type in (
        AnalysisProtocolError,
        ProtocolNotFoundError,
        DuplicateProtocolVersionError,
        InvalidProtocolVersionError,
        InvalidProtocolIdError,
        ProtocolNotDraftError,
        ProtocolStateMismatchError,
        ProtocolAlreadyFrozenError,
        ProtocolNotFrozenError,
        ProtocolNotPrimaryError,
        PrimaryRecordReplaceProhibitedError,
        AcceptanceGateProhibitedError,
    ):
        assert issubclass(error_type, ValueError)


def test_protocol_requires_initialized_project(tmp_path):
    bare = tmp_path / "bare"
    draft = make_protocol("ANL-1")
    with pytest.raises(ProjectNotInitializedError):
        register_analysis_record(bare, draft)
    with pytest.raises(ProjectNotInitializedError):
        freeze_primary_protocol(bare, draft, timestamp=FROZEN_AT)
    with pytest.raises(ProjectNotInitializedError):
        revise_protocol(bare, draft)
    with pytest.raises(ProjectNotInitializedError):
        read_protocol_version(bare, "ANL-1", "v1")
    with pytest.raises(ProjectNotInitializedError):
        list_protocol_versions(bare, "ANL-1")
    with pytest.raises(ProjectNotInitializedError):
        protocol_lineage(bare, "ANL-1")
    with pytest.raises(ProjectNotInitializedError):
        evaluate_acceptance_gate(bare, "ANL-1")
    with pytest.raises(ProjectNotInitializedError):
        assert_acceptance_eligible(bare, "ANL-1")


def test_protocol_corrupt_registry_records_rejected(tmp_path):
    root = init_project(tmp_path)
    protocols_dir = root / "protocols"
    (protocols_dir / "ANL-1.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt analysis protocol record"):
        list_protocol_versions(root, "ANL-1")
    (protocols_dir / "ANL-1.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt analysis protocol record"):
        read_protocol_version(root, "ANL-1", "v1-draft")
    # A versioned file whose stored protocol_version contradicts its
    # filename version is corrupt.
    (protocols_dir / "ANL-1.json").write_text(
        _canonical(make_protocol("ANL-1").to_dict()), encoding="utf-8"
    )
    versions_dir = protocols_dir / "versions"
    versions_dir.mkdir(exist_ok=True)
    mismatched = make_protocol("ANL-1", protocol_version="v2", frozen=True).to_dict()
    (versions_dir / "ANL-1@v1.json").write_text(_canonical(mismatched), encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt analysis protocol record"):
        list_protocol_versions(root, "ANL-1")
    # A versioned file with a malformed version is rejected.
    (versions_dir / "ANL-1@v1.json").write_text(
        _canonical(make_protocol("ANL-1", protocol_version="v1", frozen=True).to_dict()),
        encoding="utf-8",
    )
    (versions_dir / "ANL-1@bad.json").write_text(
        _canonical(make_protocol("ANL-1", protocol_version="bad").to_dict()),
        encoding="utf-8",
    )
    with pytest.raises(InvalidProtocolVersionError):
        list_protocol_versions(root, "ANL-1")


def test_protocol_rejects_unsafe_ids_and_versions(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(InvalidProtocolIdError):
        register_analysis_record(root, make_protocol("../escape"))
    with pytest.raises(InvalidProtocolIdError):
        register_analysis_record(root, make_protocol("a/b"))
    with pytest.raises(InvalidProtocolIdError):
        register_analysis_record(root, make_protocol("a@b"))
    with pytest.raises(InvalidProtocolIdError):
        read_protocol_version(root, "../escape", "v1")
    with pytest.raises(InvalidProtocolVersionError):
        read_protocol_version(root, "ANL-1", "plan-v1")
    with pytest.raises(InvalidProtocolVersionError):
        read_protocol_version(root, "ANL-1", "v1.0")
    register_analysis_record(root, make_protocol("ANL-1"))
    with pytest.raises(InvalidProtocolIdError):
        freeze_primary_protocol(root, make_protocol("a@b"), timestamp=FROZEN_AT)


@pytest.mark.parametrize("unsafe", ["ANL*", "ANL?", "ANL[1", "ANL]1"])
def test_protocol_glob_metacharacter_ids_rejected_at_every_boundary(tmp_path, unsafe):
    """Glob metacharacters are rejected before any registry read or write.

    The versioned listing is built with ``glob("<analysis_id>@*.json")``,
    so a wildcard id must never reach the glob (FND-M9-G01-01): every
    public boundary validates the id first and raises the stable
    ``InvalidProtocolIdError`` on ``*``, ``?``, ``[`` and ``]`` on every
    platform (no raw ``OSError`` on Windows).
    """
    root = build_primary_workspace(tmp_path)
    with pytest.raises(InvalidProtocolIdError):
        register_analysis_record(root, make_protocol(unsafe))
    with pytest.raises(InvalidProtocolIdError):
        read_protocol_version(root, unsafe, "v1")
    with pytest.raises(InvalidProtocolIdError):
        list_protocol_versions(root, unsafe)
    with pytest.raises(InvalidProtocolIdError):
        freeze_primary_protocol(root, make_protocol(unsafe), timestamp=FROZEN_AT)


def test_protocol_wildcard_query_cannot_leak_foreign_records(tmp_path):
    """A wildcard id can never select another analysis' records or a gate.

    After ANL-1 is registered and frozen, a wildcard query must raise at
    the boundary instead of globbing ANL-1's versioned record -- neither
    the lineage listing nor the acceptance gate (which evaluates over the
    listing) may silently change decisions for a never-registered id.
    """
    root = build_primary_workspace(tmp_path)
    freeze_primary(root)
    assert list_protocol_versions(root, "ANL-1")
    for unsafe in ("ANL*", "*", "ANL-?@*"):
        with pytest.raises(InvalidProtocolIdError):
            list_protocol_versions(root, unsafe)
        with pytest.raises(InvalidProtocolIdError):
            evaluate_acceptance_gate(root, unsafe)
        with pytest.raises(InvalidProtocolIdError):
            assert_acceptance_eligible(root, unsafe)


def test_protocol_records_pass_their_schemas(tmp_path):
    root = build_primary_workspace(tmp_path)
    draft = read_analysis_protocol(root, "ANL-1")
    validate_and_reject("analysis", draft.to_dict())
    result = freeze_primary(root)
    validate_and_reject("analysis", result.frozen_record.to_dict())
    validate_and_reject("analysis", read_protocol_version(root, "ANL-1", "v1").record.to_dict())
    revised = revise_protocol(root, result.frozen_record)
    validate_and_reject("analysis", revised.record.to_dict())
    register_analysis_record(
        root, make_result("ANL-R-1", primary_or_exploratory=PrimaryOrExploratory.PRIMARY)
    )
    validate_and_reject("analysis", read_analysis_protocol(root, "ANL-R-1").to_dict())


def test_protocol_registration_and_freeze_deterministic_same_state(tmp_path):
    root_a = build_primary_workspace(tmp_path / "a")
    root_b = build_primary_workspace(tmp_path / "b")
    # Same registered state -> identical drafts, identical freezes, and
    # identical lineage in every workspace.
    assert read_analysis_protocol(root_a, "ANL-1") == read_analysis_protocol(root_b, "ANL-1")
    freeze_primary(root_a)
    freeze_primary(root_b)
    assert protocol_lineage(root_a, "ANL-1") == protocol_lineage(root_b, "ANL-1")
    assert list_protocol_versions(root_a, "ANL-1") == list_protocol_versions(root_b, "ANL-1")


def test_protocol_registration_rejects_frozen_records(tmp_path):
    root = init_project(tmp_path)
    with pytest.raises(ProtocolAlreadyFrozenError) as exc:
        register_analysis_record(root, make_protocol("ANL-1", frozen=True))
    assert "freeze_primary_protocol" in str(exc.value)
    assert not (root / "protocols" / "ANL-1.json").exists()
