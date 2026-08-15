"""Human-readable report generator tests (DEV-M13-G02).

Every test name contains "report" so ``python -m pytest -q
tests/reporting -k report`` selects the whole suite. The
``ac01``/``ac02``/``ac03`` sections map one-to-one to the acceptance
criteria of DEV-M13-G02:

* ``ac01`` -- the report distinguishes the scientific outcome from the
  method reproducibility outcome: the "Outcomes" section renders the
  requirement outcomes (``RequirementOutcome``, ``05-GOAL-RUN-SCHEMA.md``
  SS2) and the run scientific reviews, while the "Method
  reproducibility" section renders ``MethodReproducibility``, protocol
  adherence and run lifecycle coverage;
* ``ac02`` -- material failed Runs are summarized, never hidden: every
  run of the run store appears in the "Failures and deviations" section
  with its derived ``RunStatus`` (``reporting.audit``), and failed runs
  (``CANCELLED`` / ``INVALIDATED`` or FAIL scientific review) are
  listed explicitly with their deviations and retries;
* ``ac03`` -- the report references auditable object ids for its key
  claims: the "Key claims and traceability" section (and the structured
  ``ClaimReport`` surface) cites the real ``evidence_id`` /
  ``requirement_id`` / ``acceptance_id`` / ``result_id`` / ``run_id`` /
  ``artifact_id`` of the claim trace
  (``14-STATE-GIT-ARTIFACTS.md`` SS7).

The deterministic path mirrors ``reporting_helpers``: every fixture uses
fixed identities/timestamps (``FROZEN_AT``), so all records are
deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from reporting_helpers import (
    ACCEPTANCE_ID,
    ANALYSIS_ID,
    ARTIFACT_ID,
    CLAIM_ID,
    EVIDENCE_ID,
    FAILED_RUN_ID,
    GOAL_ID,
    PROTOCOL_VERSION,
    REQUIREMENT_ID,
    RESULT_ID,
    RUN_ID,
    install_chain_with_failed_run,
    install_valid_chain,
    make_evidence,
    make_requirement,
    make_result_record,
    make_run,
)

from scientific_reproduction.core.models import (
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    MethodReproducibility,
    ScientificReview,
)
from scientific_reproduction.planning.plan import register_closure_contract
from scientific_reproduction.reporting.report import (
    ClaimReport,
    Report,
    ReportCorruptError,
    ReportNotInitializedError,
    ReportSection,
    build_report,
)
from scientific_reproduction.research.evidence import EvidenceRegistry


def _overwrite_requirement(root: Path, requirement: object) -> None:
    """Overwrite the registered requirement record (canonical JSON)."""
    path = root / "requirements" / f"{REQUIREMENT_ID}.json"
    path.write_text(
        json.dumps(requirement.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sections(report: Report) -> dict[str, ReportSection]:
    """Map the report sections by title."""
    return {section.title: section for section in report.sections}


# ---------------------------------------------------------------------------
# ac01 -- scientific outcome vs method reproducibility
# ---------------------------------------------------------------------------


def test_report_sections_cover_objective_in_fixed_order_ac01(
    tmp_path: Path,
) -> None:
    """The report covers the objective's scope in a fixed section order:
    scope, methods, statistics, strict/recovery history, failures,
    outcomes, method reproducibility and limitations (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])

    assert isinstance(report, Report)
    assert [section.title for section in report.sections] == [
        "Scope",
        "Methods",
        "Statistics",
        "Strict/recovery history",
        "Failures and deviations",
        "Outcomes",
        "Method reproducibility",
        "Key claims and traceability",
        "Limitations",
    ]
    header = report.to_markdown().splitlines()[:6]
    assert header[0] == "# Reproduction Report"
    assert any(line.startswith("Project: ") for line in header)
    assert any(line.startswith("Primary target: ") for line in header)


def test_report_distinguishes_outcome_from_method_reproducibility_ac01(
    tmp_path: Path,
) -> None:
    """The scientific outcome (requirement outcome) and the method
    reproducibility outcome are rendered in separate sections with their
    own frozen vocabularies (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    _overwrite_requirement(
        tmp_path,
        make_requirement(
            method_reproducibility=MethodReproducibility.DIRECTLY_REPRODUCIBLE
        ),
    )
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    sections = _sections(report)

    outcomes = sections["Outcomes"].body
    reproducibility = sections["Method reproducibility"].body
    assert "REPRODUCED" in outcomes
    assert "DIRECTLY_REPRODUCIBLE" in reproducibility
    # The vocabularies stay separated: the scientific outcome section
    # never claims method reproducibility and vice versa.
    assert "DIRECTLY_REPRODUCIBLE" not in outcomes
    assert "REPRODUCED" not in reproducibility


def test_report_outcomes_render_real_requirement_records_ac01(
    tmp_path: Path,
) -> None:
    """The outcomes section renders the real requirement records through
    the registered state: outcome, criticality and id (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Outcomes"].body

    assert REQUIREMENT_ID in body
    assert "[CRITICAL]" in body
    assert "REPRODUCED" in body
    assert "Run scientific reviews:" in body
    assert "PASS 1" in body


def test_report_method_reproducibility_protocol_adherence_ac01(
    tmp_path: Path,
) -> None:
    """The method reproducibility section cites the frozen protocol
    version each analysis result executed against (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Method reproducibility"].body

    assert RESULT_ID in body
    assert f"{ANALYSIS_ID} {PROTOCOL_VERSION}" in body
    assert "frozen: yes" in body
    assert "closed runs:" in body


# ---------------------------------------------------------------------------
# ac02 -- failed runs are summarized, never hidden
# ---------------------------------------------------------------------------


def test_report_failed_runs_summarized_ac02(tmp_path: Path) -> None:
    """A failed run stays visible in the report: the run table carries
    every run with its derived status and the failed run is summarized
    explicitly with its lifecycle state (AC-02)."""
    evidence, _failed = install_chain_with_failed_run(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Failures and deviations"].body

    # Every run of the run store is in the table, failed run included.
    assert RUN_ID in body
    assert FAILED_RUN_ID in body
    assert "Runs (2 total): failed 1, succeeded 1, unresolved 0" in body
    # The failed run is summarized explicitly with its real state.
    assert f"{FAILED_RUN_ID} [lifecycle state CANCELLED]" in body
    assert "CANCELLED" in body
    assert "FAILED" not in body  # the frozen lifecycle vocabulary has no FAILED


def test_report_failed_review_run_summarized_ac02(tmp_path: Path) -> None:
    """A closed run with a FAIL scientific review is summarized as failed
    with the review as the reason (AC-02)."""
    evidence = install_valid_chain(
        tmp_path, run=make_run(scientific_review=ScientificReview.FAIL)
    )
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Failures and deviations"].body

    assert "Runs (1 total): failed 1, succeeded 0, unresolved 0" in body
    assert f"{RUN_ID} [FAIL scientific review]" in body


def test_report_deviations_summarized_ac02(tmp_path: Path) -> None:
    """Material run deviations and engineering retries are summarized in
    the failures section and the recovery history section (AC-02)."""
    evidence = install_valid_chain(
        tmp_path,
        run=make_run(
            deviations=[{"note": "instrument drift"}],
            engineering_retries=[{"reason": "timeout"}],
        ),
    )
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    failures = _sections(report)["Failures and deviations"].body
    recovery = _sections(report)["Strict/recovery history"].body

    assert "Deviations: 1 total; engineering retries: 1 total" in failures
    assert "Runs with engineering retries: 1" in recovery
    assert "runs with deviations: 1" in recovery
    assert f"{RUN_ID}: 1 engineering retries" in recovery
    assert f"{RUN_ID}: 1 deviations" in recovery


def test_report_scope_renders_real_planning_records_ac02(tmp_path: Path) -> None:
    """The scope section renders the real planning records: goals with
    their track, requirements, inventory items and acceptance criteria
    (AC-02: nothing material is hidden from the report)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Scope"].body

    assert GOAL_ID in body
    assert "[STRICT_REPRODUCTION]" in body
    assert REQUIREMENT_ID in body
    assert "INV-001" in body
    assert ACCEPTANCE_ID in body


# ---------------------------------------------------------------------------
# ac03 -- auditable object ids for key claims
# ---------------------------------------------------------------------------


def test_report_key_claims_cite_auditable_object_ids_ac03(
    tmp_path: Path,
) -> None:
    """The report's key-claims surface cites the real object ids of the
    registered records the claim traces resolve to (AC-03)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])

    assert len(report.claims) == 1
    claim = report.claims[0]
    assert isinstance(claim, ClaimReport)
    assert claim.claim_id == CLAIM_ID
    assert claim.evidence_ids == (EVIDENCE_ID,)
    assert claim.requirement_ids == (REQUIREMENT_ID,)
    assert claim.acceptance_ids == (ACCEPTANCE_ID,)
    assert claim.result_ids == (RESULT_ID,)
    assert claim.run_ids == (RUN_ID,)
    assert claim.artifact_ids == (ARTIFACT_ID,)
    assert claim.gap_count == 0


def test_report_key_claims_section_renders_ids_ac03(tmp_path: Path) -> None:
    """The key-claims section of the markdown renders the auditable
    object ids of every key claim (AC-03)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Key claims and traceability"].body

    for object_id in (
        CLAIM_ID,
        EVIDENCE_ID,
        REQUIREMENT_ID,
        ACCEPTANCE_ID,
        RESULT_ID,
        RUN_ID,
        ARTIFACT_ID,
    ):
        assert object_id in body
    assert "trace gaps: 0" in body


def test_report_empty_key_claims_ac03(tmp_path: Path) -> None:
    """An empty key-claim set renders a report with no claims and the
    key-claims section states it (AC-03)."""
    evidence = install_valid_chain(tmp_path)
    report = build_report(tmp_path, evidence, [])

    assert report.claims == ()
    body = _sections(report)["Key claims and traceability"].body
    assert "no key claims specified" in body
    # The rest of the report still renders every registered record.
    assert RUN_ID in _sections(report)["Failures and deviations"].body


def test_report_multiple_claims_sorted_deterministic_ac03(
    tmp_path: Path,
) -> None:
    """Multiple key claims are cited sorted by claim id, deduplicated and
    byte-identical across repeated builds (AC-03)."""
    evidence = install_valid_chain(tmp_path)
    claims = [CLAIM_ID, "GHOST-CLAIM-2", CLAIM_ID]
    first = build_report(tmp_path, evidence, claims)
    second = build_report(tmp_path, evidence, claims)

    assert [claim.claim_id for claim in first.claims] == sorted(
        {CLAIM_ID, "GHOST-CLAIM-2"}
    )
    assert first == second
    assert first.to_markdown() == second.to_markdown()
    assert first.to_canonical_json() == second.to_canonical_json()


def test_report_render_is_deterministic_ac03(tmp_path: Path) -> None:
    """Repeated report generation from the same registered state yields
    byte-identical markdown and canonical JSON (AC-03)."""
    evidence = install_chain_with_failed_run(tmp_path)[0]
    first = build_report(tmp_path, evidence, [CLAIM_ID])
    second = build_report(tmp_path, evidence, [CLAIM_ID])

    assert first.to_markdown() == second.to_markdown()
    assert first.to_canonical_json() == second.to_canonical_json()
    data = first.to_dict()
    assert data["version"] == "1.0"
    assert data["project_id"]
    assert data["sections"][4]["title"] == "Failures and deviations"


def test_report_limitations_surface_registered_state_ac03(
    tmp_path: Path,
) -> None:
    """The limitations section renders recorded limitations: evidence
    limitations, trace gaps, unresolved artifacts and result warnings --
    nothing material is hidden (AC-03)."""
    evidence = install_valid_chain(
        tmp_path,
        evidence=make_evidence(limitations=["single batch studied"]),
        result=make_result_record(
            run_ref="GHOST-RUN", warnings=["small sample size"]
        ),
    )
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Limitations"].body

    assert "single batch studied" in body
    assert "GHOST-RUN" in body
    assert "small sample size" in body
    assert "gap(s)" in body


def test_report_recovery_history_renders_real_records_ac03(
    tmp_path: Path,
) -> None:
    """The strict/recovery history section renders the real recovery
    records: goal tracks, recovery-outcome requirements and closure
    contract recovery progress (AC-03)."""
    evidence = install_valid_chain(tmp_path)
    register_closure_contract(
        tmp_path,
        ClosureContract(
            closure_id="CLOS-001",
            frozen=True,
            statistical_sufficiency={"power": 0.8},
            execution_validity={"valid": True},
            diagnosis={"cause": "drift"},
            recovery=ClosureRecovery(
                eligible_hypotheses_total=3,
                tested_or_ruled_out=1,
                remaining=2,
            ),
            literature=ClosureLiterature(),
        ),
    )
    report = build_report(tmp_path, evidence, [CLAIM_ID])
    body = _sections(report)["Strict/recovery history"].body

    assert "Goal tracks: method_redesign 0, recovery 0, strict_reproduction 1" in body
    assert "CLOS-001" in body
    assert "recovery hypotheses eligible 3" in body
    assert "tested or ruled out 1" in body
    assert "remaining 2" in body


# ---------------------------------------------------------------------------
# boundaries -- structural failures raise
# ---------------------------------------------------------------------------


def test_report_uninitialized_workspace_raises(tmp_path: Path) -> None:
    """Report generation without a project state record raises (stable
    message)."""
    with pytest.raises(ReportNotInitializedError, match="project state"):
        build_report(tmp_path, EvidenceRegistry(), [CLAIM_ID])


def test_report_corrupt_run_record_raises(tmp_path: Path) -> None:
    """A corrupt stored run record surfaces as ReportCorruptError."""
    evidence = install_valid_chain(tmp_path)
    run_path = tmp_path / "runs" / f"{RUN_ID}.json"
    run_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ReportCorruptError, match="run"):
        build_report(tmp_path, evidence, [CLAIM_ID])


def test_report_type_errors(tmp_path: Path) -> None:
    """Wrong argument types raise TypeError at the boundary."""
    evidence = EvidenceRegistry()
    with pytest.raises(TypeError, match="root"):
        build_report(42, evidence, [CLAIM_ID])
    with pytest.raises(TypeError, match="evidence"):
        build_report(tmp_path, None, [CLAIM_ID])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="key_claims"):
        build_report(tmp_path, evidence, "CLAIM-001")
    with pytest.raises(TypeError, match="key_claims"):
        build_report(tmp_path, evidence, [1])  # type: ignore[list-item]
