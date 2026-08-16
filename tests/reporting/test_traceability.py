"""Key-claim traceability checker tests (DEV-M13-G01).

Every test name contains "trace" so ``python -m pytest -q tests/reporting
-k "audit or trace"`` selects the whole reporting suite. The
``ac01``/``ac02``/``ac03`` sections map one-to-one to the acceptance
criteria of DEV-M13-G01:

* ``ac01`` -- a key report claim resolves through the full
  report-traceability chain (``14-STATE-GIT-ARTIFACTS.md`` SS7): claim ->
  evidence -> acceptance / requirement outcome -> analysis result -> run
  -> raw artifact manifest, with every hop recorded as a ``TraceLink``
  naming the exact real record field (``via``) and every node carrying
  the typed record read through the real registry API;
* ``ac02`` -- a missing link never crashes the checker: every dangling
  reference resolves to a ``TraceGap`` of the returned trace, and a
  claim with no registered evidence records resolves to a trace with
  only the CLAIM node;
* ``ac03`` -- the trace is a pure deterministic function of the
  registered state: repeated resolution yields identical nodes, links
  and gaps and byte-identical canonical JSON.

The deterministic path mirrors ``reporting_helpers``: every fixture uses fixed
identities/timestamps (``FROZEN_AT``), so all records are deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from reporting_helpers import (
    ACCEPTANCE_ID,
    ARTIFACT_ID,
    CLAIM_ID,
    EVIDENCE_ID,
    GOAL_ID,
    REQUIREMENT_ID,
    RESULT_ID,
    RUN_ID,
    install_valid_chain,
    make_acceptance,
    make_evidence,
    make_result_record,
)

from scientific_reproduction.analysis.results import ResultRecord
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ArtifactManifest,
    ClaimSpecificEvidence,
    LifecycleState,
    ReproductionRequirement,
    Run,
)
from scientific_reproduction.reporting.traceability import (
    ClaimTrace,
    TraceCorruptError,
    TraceKind,
    TraceNotInitializedError,
    trace_claim,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

#: The second claim and its evidence record: a registered evidence record
#: of a *different* claim that a trace acceptance cites (the cross-claim
#: evidence_refs case, 06-EVIDENCE-SYSTEM.md SS1/SS6).
CROSS_CLAIM_ID: str = "CLAIM-002"
CROSS_EVIDENCE_ID: str = "EVID-002"

# ---------------------------------------------------------------------------
# ac01 -- the full SS7 chain resolves
# ---------------------------------------------------------------------------


def test_trace_claim_resolves_full_chain_ac01(tmp_path: Path) -> None:
    """A key claim resolves to every node kind of the SS7 chain (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert isinstance(trace, ClaimTrace)
    assert trace.claim_id == CLAIM_ID
    assert trace.gaps == ()
    kinds = {node.kind for node in trace.nodes}
    assert kinds == {
        TraceKind.CLAIM,
        TraceKind.EVIDENCE,
        TraceKind.ACCEPTANCE,
        TraceKind.REQUIREMENT,
        TraceKind.ANALYSIS,
        TraceKind.RUN,
        TraceKind.ARTIFACT,
    }
    # AC-01: the claim is traceable to Analysis -> Run -> Artifact/Evidence.
    assert trace.has_node(TraceKind.ANALYSIS)
    assert trace.has_node(TraceKind.RUN)
    assert trace.has_node(TraceKind.ARTIFACT)
    assert trace.has_node(TraceKind.EVIDENCE)


def test_trace_claim_records_every_hop_ac01(tmp_path: Path) -> None:
    """Every hop is a link naming the exact real record field (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    vias = {link.via for link in trace.links}
    assert {
        "ClaimSpecificEvidence.used_by",
        "AcceptanceCriteria.evidence_refs",
        "ResultRecord.acceptance_ref",
        "ResultRecord.requirement_refs",
        "ResultRecord.run_ref",
        "ResultRecord.input_artifact_ids",
        "Run.artifacts",
        "ArtifactManifest.run_id",
        "ArtifactManifest.analysis_id",
    } <= vias


def test_trace_claim_nodes_carry_typed_records_ac01(
    tmp_path: Path,
) -> None:
    """Every node carries the typed record read through the real API."""
    evidence = install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    records = {node.ref_id: node.record for node in trace.nodes}
    assert isinstance(records[EVIDENCE_ID], ClaimSpecificEvidence)
    assert isinstance(records[ACCEPTANCE_ID], AcceptanceCriteria)
    assert isinstance(records[REQUIREMENT_ID], ReproductionRequirement)
    assert isinstance(records[RESULT_ID], ResultRecord)
    assert isinstance(records[RUN_ID], Run)
    assert records[RUN_ID].lifecycle_state is LifecycleState.CLOSED
    assert isinstance(records[ARTIFACT_ID], ArtifactManifest)


def test_trace_claim_links_claim_evidence_to_acceptance_ac01(
    tmp_path: Path,
) -> None:
    """The acceptance enters through its evidence_refs naming the evidence."""
    evidence = install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    links = {
        (link.source.ref_id, link.via, link.target.ref_id)
        for link in trace.links
    }
    assert (
        EVIDENCE_ID,
        "AcceptanceCriteria.evidence_refs",
        ACCEPTANCE_ID,
    ) in links
    assert (
        ACCEPTANCE_ID,
        "ResultRecord.acceptance_ref",
        RESULT_ID,
    ) in links
    assert (RESULT_ID, "ResultRecord.run_ref", RUN_ID) in links
    assert (
        RESULT_ID,
        "ResultRecord.input_artifact_ids",
        ARTIFACT_ID,
    ) in links
    assert (RUN_ID, "Run.artifacts", ARTIFACT_ID) in links


def test_trace_claim_enters_via_result_evidence_refs_ac01(
    tmp_path: Path,
) -> None:
    """An acceptance whose evidence_refs names only the result id still
    resolves (the scenario-suite pattern): the requirement-outcome hop
    enters the chain and hop 5 links the acceptance back to the result."""
    evidence = install_valid_chain(
        tmp_path, acceptance=make_acceptance(evidence_refs=[RESULT_ID])
    )
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert trace.gaps == ()
    assert trace.has_node(TraceKind.ACCEPTANCE)
    assert trace.has_node(TraceKind.ANALYSIS)
    assert trace.has_node(TraceKind.RUN)
    assert trace.has_node(TraceKind.ARTIFACT)
    links = {
        (link.source.ref_id, link.via, link.target.ref_id)
        for link in trace.links
    }
    assert (
        ACCEPTANCE_ID,
        "AcceptanceCriteria.evidence_refs",
        RESULT_ID,
    ) in links
    assert (
        EVIDENCE_ID,
        "ClaimSpecificEvidence.used_by",
        REQUIREMENT_ID,
    ) in links
    assert (
        REQUIREMENT_ID,
        "ResultRecord.requirement_refs",
        RESULT_ID,
    ) in links


def test_trace_claim_cross_claim_evidence_ref_resolves_ac01(
    tmp_path: Path,
) -> None:
    """An acceptance citing an evidence record of a different claim resolves
    the ref (evidence is Source x Claim, 06-EVIDENCE-SYSTEM.md SS1/SS6): the
    registered cross-claim record becomes an EVIDENCE node carrying its
    typed record and links from the acceptance with no gap (AC-01)."""
    evidence = install_valid_chain(
        tmp_path,
        acceptance=make_acceptance(
            evidence_refs=[EVIDENCE_ID, CROSS_EVIDENCE_ID]
        ),
    ).register(
        make_evidence(evidence_id=CROSS_EVIDENCE_ID, claim_id=CROSS_CLAIM_ID)
    )
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert trace.gaps == ()
    records = {node.ref_id: node.record for node in trace.nodes}
    cross_claim = records[CROSS_EVIDENCE_ID]
    assert isinstance(cross_claim, ClaimSpecificEvidence)
    assert cross_claim.claim_id == CROSS_CLAIM_ID
    links = {
        (link.source.ref_id, link.via, link.target.ref_id)
        for link in trace.links
    }
    assert (
        CROSS_EVIDENCE_ID,
        "AcceptanceCriteria.evidence_refs",
        ACCEPTANCE_ID,
    ) in links


# ---------------------------------------------------------------------------
# ac02 -- missing links are gaps, never exceptions
# ---------------------------------------------------------------------------


def test_trace_claim_missing_run_is_a_gap_ac02(tmp_path: Path) -> None:
    """A run_ref that resolves to no Run record is a gap (AC-02): the
    exact hop fails even when the manifest's producer link still resolves
    a run."""
    evidence = install_valid_chain(
        tmp_path, result=make_result_record(run_ref="GHOST-RUN-001")
    )
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert len(trace.gaps) == 1
    gap = trace.gaps[0]
    assert gap.source.ref_id == RESULT_ID
    assert gap.via == "ResultRecord.run_ref"
    assert gap.ref_id == "GHOST-RUN-001"
    assert "GHOST-RUN-001" in gap.reason
    # No run node is reachable through the analysis result's own hop.
    run_nodes = {
        node.ref_id
        for node in trace.nodes_for(TraceKind.RUN)
        if any(
            link.source.ref_id == RESULT_ID
            and link.via == "ResultRecord.run_ref"
            and link.target.ref_id == node.ref_id
            for link in trace.links
        )
    }
    assert "GHOST-RUN-001" not in run_nodes


def test_trace_claim_missing_artifact_is_a_gap_ac02(tmp_path: Path) -> None:
    """A manifest that vanishes from the registry is a gap on every link
    that references it (AC-02): the result's input refs and the run's
    artifact list both fail validation."""
    evidence = install_valid_chain(tmp_path)
    (tmp_path / "manifests" / f"{ARTIFACT_ID}.json").unlink()
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert not trace.has_node(TraceKind.ARTIFACT)
    vias = {gap.via for gap in trace.gaps}
    assert "ResultRecord.input_artifact_ids" in vias
    assert "Run.artifacts" in vias
    assert all(gap.ref_id == ARTIFACT_ID for gap in trace.gaps)


def test_trace_claim_dangling_used_by_ref_is_a_gap_ac02(
    tmp_path: Path,
) -> None:
    """A used_by ref that names no registered requirement or goal is a gap
    (AC-02); the requirement hop stays absent."""
    evidence = install_valid_chain(
        tmp_path,
        evidence=make_evidence(used_by=[GOAL_ID, "GHOST-REQ"]),
    )
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert not trace.has_node(TraceKind.REQUIREMENT)
    assert len(trace.gaps) == 1
    gap = trace.gaps[0]
    assert gap.source.ref_id == EVIDENCE_ID
    assert gap.via == "ClaimSpecificEvidence.used_by"
    assert gap.ref_id == "GHOST-REQ"


def test_trace_claim_dangling_acceptance_evidence_ref_is_a_gap_ac02(
    tmp_path: Path,
) -> None:
    """An acceptance evidence_refs entry that resolves to no evidence record
    and no result record is a gap (AC-02)."""
    evidence = install_valid_chain(
        tmp_path,
        acceptance=make_acceptance(
            evidence_refs=[EVIDENCE_ID, "GHOST-EVID"]
        ),
    )
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert trace.has_node(TraceKind.ACCEPTANCE)
    assert len(trace.gaps) == 1
    gap = trace.gaps[0]
    assert gap.source.ref_id == ACCEPTANCE_ID
    assert gap.via == "AcceptanceCriteria.evidence_refs"
    assert gap.ref_id == "GHOST-EVID"


def test_trace_claim_cross_claim_evidence_ref_is_not_a_gap_ac02(
    tmp_path: Path,
) -> None:
    """A trace acceptance citing a registered evidence record of a different
    claim produces no trace_gap, while a genuinely unresolved evidence_refs
    entry of the same acceptance still does (AC-02)."""
    evidence = install_valid_chain(
        tmp_path,
        acceptance=make_acceptance(
            evidence_refs=[EVIDENCE_ID, CROSS_EVIDENCE_ID, "GHOST-EVID"]
        ),
    ).register(
        make_evidence(evidence_id=CROSS_EVIDENCE_ID, claim_id=CROSS_CLAIM_ID)
    )
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert trace.has_node(TraceKind.ACCEPTANCE)
    assert trace.has_node(TraceKind.EVIDENCE)
    assert [gap.ref_id for gap in trace.gaps] == ["GHOST-EVID"]
    gap = trace.gaps[0]
    assert gap.source.ref_id == ACCEPTANCE_ID
    assert gap.via == "AcceptanceCriteria.evidence_refs"


def test_trace_claim_without_evidence_has_only_claim_node_ac02(
    tmp_path: Path,
) -> None:
    """A claim with no registered evidence resolves to a trace with only
    the CLAIM node -- absence is data, never an exception (AC-02)."""
    install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, CLAIM_ID, EvidenceRegistry())

    assert len(trace.nodes) == 1
    assert trace.nodes[0].kind is TraceKind.CLAIM
    assert trace.nodes[0].ref_id == CLAIM_ID
    assert trace.links == ()
    assert trace.gaps == ()


def test_trace_claim_unregistered_claim_id_has_only_claim_node_ac02(
    tmp_path: Path,
) -> None:
    """A key claim the registry does not back at all behaves exactly like a
    claim with no evidence: only the CLAIM node (AC-02)."""
    evidence = install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, "GHOST-CLAIM", evidence)

    assert [node.ref_id for node in trace.nodes] == ["GHOST-CLAIM"]
    assert trace.links == ()
    assert trace.gaps == ()


# ---------------------------------------------------------------------------
# ac03 -- determinism
# ---------------------------------------------------------------------------


def test_trace_claim_is_deterministic_ac03(tmp_path: Path) -> None:
    """Repeated resolution yields identical traces and byte-identical
    canonical JSON (AC-03)."""
    evidence = install_valid_chain(tmp_path)
    first = trace_claim(tmp_path, CLAIM_ID, evidence)
    second = trace_claim(tmp_path, CLAIM_ID, evidence)

    assert first == second
    assert first.to_canonical_json() == second.to_canonical_json()
    # Collections are deduplicated and sorted by stable keys: nodes by
    # (kind, ref_id), gaps by (source, via, ref_id).
    keys = [(node.kind.value, node.ref_id) for node in first.nodes]
    assert keys == sorted(keys)
    gap_keys = [
        (gap.source.ref_id, gap.via, gap.ref_id) for gap in first.gaps
    ]
    assert gap_keys == sorted(gap_keys)


def test_trace_claim_canonical_json_sections_ac03(tmp_path: Path) -> None:
    """The canonical JSON carries version, claim id, nodes, links and gaps."""
    evidence = install_valid_chain(tmp_path)
    trace = trace_claim(tmp_path, CLAIM_ID, evidence)

    data = json.loads(trace.to_canonical_json())
    assert data["version"] == "1.0"
    assert data["claim_id"] == CLAIM_ID
    assert {node["kind"] for node in data["nodes"]} == {
        "claim",
        "evidence",
        "acceptance",
        "requirement",
        "analysis",
        "run",
        "artifact",
    }
    assert data["gaps"] == []


# ---------------------------------------------------------------------------
# boundaries -- structural failures raise
# ---------------------------------------------------------------------------


def test_trace_claim_uninitialized_workspace_raises(tmp_path: Path) -> None:
    """Tracing without a project state record raises (stable message)."""
    with pytest.raises(TraceNotInitializedError, match="project state"):
        trace_claim(tmp_path, CLAIM_ID, EvidenceRegistry())


def test_trace_claim_corrupt_run_record_raises(tmp_path: Path) -> None:
    """A corrupt stored run record surfaces as TraceCorruptError."""
    evidence = install_valid_chain(tmp_path)
    run_path = tmp_path / "runs" / f"{RUN_ID}.json"
    run_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(TraceCorruptError, match="run"):
        trace_claim(tmp_path, CLAIM_ID, evidence)


def test_trace_claim_type_errors(tmp_path: Path) -> None:
    """Wrong argument types raise TypeError at the boundary."""
    evidence = EvidenceRegistry()
    with pytest.raises(TypeError, match="root"):
        trace_claim(42, CLAIM_ID, evidence)
    with pytest.raises(TypeError, match="claim_id"):
        trace_claim(tmp_path, None, evidence)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="evidence"):
        trace_claim(tmp_path, CLAIM_ID, None)  # type: ignore[arg-type]
