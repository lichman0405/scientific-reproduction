"""Audit package validator tests (DEV-M13-G01).

Every test name contains "audit" so ``python -m pytest -q tests/reporting
-k "audit or trace"`` selects the whole reporting suite. The
``ac01``/``ac02``/``ac03`` sections map one-to-one to the acceptance
criteria of DEV-M13-G01:

* ``ac01`` -- a key report claim is traceable through Analysis -> Run ->
  Artifact/Evidence: a fully linked SS7 chain (``14-STATE-GIT-ARTIFACTS.md``
  SS7, installed by ``reporting_helpers``) validates with no errors, and the
  machine-auditable package carries the claim trace and the run table;
* ``ac02`` -- every missing link fails validation: a claim backed by no
  registered evidence record fails with ``CLAIM_NOT_FOUND``, every trace
  gap fails with ``TRACE_GAP``, an unreachable Analysis/Run/Artifact hop
  fails with ``TRACE_INCOMPLETE`` and an unresolvable run-artifact ref
  fails with ``UNRESOLVED_RUN_ARTIFACT``;
* ``ac03`` -- failed Runs remain visible: every run of the run store is
  assembled into the package with its real ``lifecycle_state`` /
  ``scientific_review`` and the derived status, so a ``CANCELLED`` /
  ``INVALIDATED`` or FAIL-reviewed run appears in the run table with
  status ``"failed"`` while a valid claim chain still passes.

The deterministic path mirrors ``reporting_helpers``: every fixture uses fixed
identities/timestamps (``FROZEN_AT``), so all records are deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from reporting_helpers import (
    ARTIFACT_ID,
    CLAIM_ID,
    FAILED_RUN_ID,
    RUN_ID,
    install_chain_with_failed_run,
    install_valid_chain,
    make_result_record,
    make_run,
)

from scientific_reproduction.artifacts.checksum import compute_sha256
from scientific_reproduction.core.models import (
    LifecycleState,
    Run,
    RunType,
    ScientificReview,
)
from scientific_reproduction.reporting.audit import (
    AuditCorruptError,
    AuditNotInitializedError,
    AuditPackage,
    AuditValidationResult,
    RunStatus,
    ValidationErrorKind,
    build_audit_package,
    run_status,
    validate_package,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

# ---------------------------------------------------------------------------
# ac01 -- a valid chain validates
# ---------------------------------------------------------------------------


def test_audit_validate_passes_valid_chain_ac01(tmp_path: Path) -> None:
    """A fully linked SS7 chain validates with no errors (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    result = validate_package(tmp_path, evidence, [CLAIM_ID])

    assert isinstance(result, AuditValidationResult)
    assert result.passed is True
    assert result.errors == ()
    assert [trace.claim_id for trace in result.package.claims] == [CLAIM_ID]
    assert [entry.run.run_id for entry in result.package.runs] == [RUN_ID]
    entry = result.package.runs[0]
    assert entry.status is RunStatus.SUCCEEDED
    assert entry.unresolved_artifact_ids == ()
    assert [manifest.artifact_id for manifest in entry.artifacts] == [
        ARTIFACT_ID
    ]


def test_audit_build_package_assembles_all_registries_ac01(
    tmp_path: Path,
) -> None:
    """The package assembles claims, analyses, runs, artifacts, evidence,
    acceptances and requirements from the real registries (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    package = build_audit_package(tmp_path, evidence, [CLAIM_ID])

    assert isinstance(package, AuditPackage)
    data = package.to_dict()
    assert data["claims"][0]["claim_id"] == CLAIM_ID
    assert data["runs"][0]["run_id"] == RUN_ID
    assert data["analyses"] == ["RES-001"]
    assert data["artifacts"] == [ARTIFACT_ID]
    assert data["evidence"] == ["EVID-001"]
    assert data["acceptances"] == ["ACC-001"]
    assert data["requirements"] == ["REQ-001"]
    assert data["runs"][0]["status"] == "succeeded"


def test_audit_validation_result_dict_shape_ac01(tmp_path: Path) -> None:
    """The validation result serializes with version, passed, errors and
    the full package."""
    evidence = install_valid_chain(tmp_path)
    data = validate_package(tmp_path, evidence, [CLAIM_ID]).to_dict()

    assert set(data) == {"package_version", "passed", "errors", "package"}
    assert data["package_version"] == "1.1"
    assert data["passed"] is True
    assert data["errors"] == []
    assert set(data["package"]) == {
        "claims",
        "runs",
        "analyses",
        "artifacts",
        "evidence",
        "acceptances",
        "requirements",
        "report_files",
    }


def test_audit_report_files_registered_with_checksums(tmp_path: Path) -> None:
    """Report files in the workspace ``reports/`` dir are registered in
    the package with SHA-256 checksums and sizes, sorted by name (issue
    #107: report files must be registered with checksums)."""
    evidence = install_valid_chain(tmp_path)
    # A workspace without a reports/ dir registers no report files.
    assert build_audit_package(
        tmp_path, evidence, [CLAIM_ID]
    ).report_files == ()

    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "reproduction-report.pdf").write_bytes(b"%PDF-1.4 fake")
    # write_bytes (not write_text): the platform-independent byte count
    # matters here — text mode would translate ``\n`` to ``\r\n`` on
    # Windows and the expected sizes below would differ on CI (POSIX).
    (reports / "reproduction-report.json").write_bytes(b'{"a": 1}\n')

    package = build_audit_package(tmp_path, evidence, [CLAIM_ID])

    assert [
        (file.file_name, file.size_bytes) for file in package.report_files
    ] == [
        ("reproduction-report.json", 9),
        ("reproduction-report.pdf", 13),
    ]
    assert package.report_files[0].sha256 == compute_sha256(
        reports / "reproduction-report.json"
    )
    assert package.report_files[1].sha256 == compute_sha256(
        reports / "reproduction-report.pdf"
    )
    data = package.to_dict()
    assert data["report_files"][0]["file_name"] == "reproduction-report.json"


def test_audit_empty_key_claims_passes_vacuously_ac01(
    tmp_path: Path,
) -> None:
    """An empty key-claim set validates nothing and passes, while the
    package still assembles every registry (AC-01)."""
    evidence = install_valid_chain(tmp_path)
    result = validate_package(tmp_path, evidence, [])

    assert result.passed is True
    assert result.package.claims == ()
    assert [entry.run.run_id for entry in result.package.runs] == [RUN_ID]
    assert [record.evidence_id for record in result.package.evidence] == [
        "EVID-001"
    ]


# ---------------------------------------------------------------------------
# ac02 -- every missing link fails validation
# ---------------------------------------------------------------------------


def test_audit_missing_run_fails_ac02(tmp_path: Path) -> None:
    """A run_ref resolving to no Run record is a TRACE_GAP error (AC-02)."""
    evidence = install_valid_chain(
        tmp_path, result=make_result_record(run_ref="GHOST-RUN-001")
    )
    result = validate_package(tmp_path, evidence, [CLAIM_ID])

    assert result.passed is False
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.kind is ValidationErrorKind.TRACE_GAP
    assert error.claim_id == CLAIM_ID
    assert "GHOST-RUN-001" in error.message
    assert error.to_dict() == {
        "kind": "trace_gap",
        "claim_id": CLAIM_ID,
        "message": error.message,
    }


def test_audit_missing_artifact_fails_ac02(tmp_path: Path) -> None:
    """A manifest that vanishes from the registry fails the trace (TRACE_GAP)
    and the run entry (UNRESOLVED_RUN_ARTIFACT) -- AC-02."""
    evidence = install_valid_chain(tmp_path)
    (tmp_path / "manifests" / f"{ARTIFACT_ID}.json").unlink()
    result = validate_package(tmp_path, evidence, [CLAIM_ID])

    assert result.passed is False
    kinds = {error.kind for error in result.errors}
    assert ValidationErrorKind.TRACE_GAP in kinds
    assert ValidationErrorKind.UNRESOLVED_RUN_ARTIFACT in kinds
    assert all(
        error.claim_id == CLAIM_ID
        for error in result.errors
        if error.kind is ValidationErrorKind.TRACE_GAP
    )


def test_audit_claim_without_evidence_fails_ac02(tmp_path: Path) -> None:
    """A key claim backed by no evidence fails with CLAIM_NOT_FOUND and
    TRACE_INCOMPLETE for the unreachable Analysis/Run/Artifact hops
    (AC-02)."""
    install_valid_chain(tmp_path)
    result = validate_package(tmp_path, EvidenceRegistry(), [CLAIM_ID])

    assert result.passed is False
    kinds = {error.kind for error in result.errors}
    assert ValidationErrorKind.CLAIM_NOT_FOUND in kinds
    assert kinds == {
        ValidationErrorKind.CLAIM_NOT_FOUND,
        ValidationErrorKind.TRACE_INCOMPLETE,
    }
    incomplete = [
        error
        for error in result.errors
        if error.kind is ValidationErrorKind.TRACE_INCOMPLETE
    ]
    assert len(incomplete) == 3
    assert "Analysis" in incomplete[0].message
    assert "Run" in incomplete[1].message
    assert "Artifact" in incomplete[2].message


def test_audit_unknown_claim_fails_ac02(tmp_path: Path) -> None:
    """A key claim the evidence registry does not back fails even when the
    workspace chain is valid (AC-02)."""
    evidence = install_valid_chain(tmp_path)
    result = validate_package(tmp_path, evidence, ["GHOST-CLAIM"])

    assert result.passed is False
    assert any(
        error.kind is ValidationErrorKind.CLAIM_NOT_FOUND
        for error in result.errors
    )
    assert any(
        error.kind is ValidationErrorKind.TRACE_INCOMPLETE
        for error in result.errors
    )


def test_audit_unresolved_run_artifact_fails_ac02(tmp_path: Path) -> None:
    """A run-artifact ref resolving to no registered manifest fails with
    UNRESOLVED_RUN_ARTIFACT even when no claim is validated (AC-02)."""
    evidence = install_valid_chain(
        tmp_path,
        run=make_run(artifacts=["GHOST-ART-001"]),
    )
    result = validate_package(tmp_path, evidence, [])

    assert result.passed is False
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.kind is ValidationErrorKind.UNRESOLVED_RUN_ARTIFACT
    assert error.claim_id == RUN_ID
    assert "GHOST-ART-001" in error.message


# ---------------------------------------------------------------------------
# ac03 -- failed runs remain visible
# ---------------------------------------------------------------------------


def test_audit_failed_run_remains_visible_ac03(tmp_path: Path) -> None:
    """A failed run stays in the package and its run table while the valid
    claim chain still passes (AC-03)."""
    evidence, failed = install_chain_with_failed_run(tmp_path)
    assert failed.lifecycle_state is LifecycleState.CANCELLED
    result = validate_package(tmp_path, evidence, [CLAIM_ID])

    # The failed run does not break a valid chain.
    assert result.passed is True
    assert [entry.run.run_id for entry in result.package.runs] == [
        RUN_ID,
        FAILED_RUN_ID,
    ]
    rows = {
        row["run_id"]: row for row in result.to_dict()["package"]["runs"]
    }
    assert rows[FAILED_RUN_ID]["lifecycle_state"] == "CANCELLED"
    assert rows[FAILED_RUN_ID]["status"] == "failed"
    assert rows[FAILED_RUN_ID]["unresolved_artifact_ids"] == []
    assert rows[RUN_ID]["lifecycle_state"] == "CLOSED"
    assert rows[RUN_ID]["status"] == "succeeded"


def test_audit_failed_review_run_is_failed_ac03(tmp_path: Path) -> None:
    """A closed run with a FAIL scientific review is classified FAILED and
    stays visible without failing the chain (AC-03)."""
    evidence = install_valid_chain(
        tmp_path,
        run=make_run(scientific_review=ScientificReview.FAIL),
    )
    result = validate_package(tmp_path, evidence, [CLAIM_ID])

    assert result.passed is True
    entry = result.package.runs[0]
    assert entry.run.scientific_review is ScientificReview.FAIL
    assert entry.status is RunStatus.FAILED
    row = result.to_dict()["package"]["runs"][0]
    assert row["scientific_review"] == "FAIL"
    assert row["status"] == "failed"


def test_audit_run_status_classification_ac03() -> None:
    """The run-status classification is a pure function of the frozen Run
    fields (AC-03): failed lifecycle states and FAIL reviews are FAILED;
    closed runs SUCCEEDED; anything else UNRESOLVED."""
    cases: list[tuple[LifecycleState, ScientificReview, RunStatus]] = [
        (LifecycleState.CLOSED, ScientificReview.PASS, RunStatus.SUCCEEDED),
        (LifecycleState.CLOSED, ScientificReview.UNREVIEWED, RunStatus.SUCCEEDED),
        (LifecycleState.CLOSED, ScientificReview.FAIL, RunStatus.FAILED),
        (LifecycleState.CANCELLED, ScientificReview.UNREVIEWED, RunStatus.FAILED),
        (LifecycleState.INVALIDATED, ScientificReview.PASS, RunStatus.FAILED),
        (LifecycleState.CREATED, ScientificReview.UNREVIEWED, RunStatus.UNRESOLVED),
        (
            LifecycleState.RESULT_AVAILABLE,
            ScientificReview.INCONCLUSIVE,
            RunStatus.UNRESOLVED,
        ),
    ]
    for state, review, expected in cases:
        run = Run(
            run_id=f"RUN-{state.value}",
            goal_id="GOAL-001",
            run_type=RunType.INDEPENDENT_REPLICATE,
            lifecycle_state=state,
            goal_version="v1-draft",
            scientific_review=review,
        )
        assert run_status(run) is expected, (state, review)


# ---------------------------------------------------------------------------
# ac03 -- determinism and ordering
# ---------------------------------------------------------------------------


def test_audit_package_is_deterministic_ac03(tmp_path: Path) -> None:
    """Repeated assembly yields identical packages, byte-identical
    canonical JSON and stable (sorted) tables (AC-03)."""
    evidence = install_chain_with_failed_run(tmp_path)[0]
    first = build_audit_package(tmp_path, evidence, [CLAIM_ID])
    second = build_audit_package(tmp_path, evidence, [CLAIM_ID])

    assert first == second
    assert first.to_canonical_json() == second.to_canonical_json()
    assert [entry.run.run_id for entry in first.runs] == sorted(
        entry.run.run_id for entry in first.runs
    )


def test_audit_errors_are_sorted_deterministically_ac03(
    tmp_path: Path,
) -> None:
    """Validation errors are sorted by claim, kind and message (AC-03)."""
    evidence = install_chain_with_failed_run(tmp_path)[0]
    # A package with several failures: an unknown claim and a ghost
    # run artifact.
    ghost_run = make_run(run_id="RUN-003", artifacts=["GHOST-ART-001"])
    from scientific_reproduction.core.state_backend import (
        FilesystemStateBackend,
    )

    # The run store is a state backend over the workspace root, resolving
    # the canonical runs/ tree directory.
    FilesystemStateBackend(tmp_path).write(
        "run", ghost_run.run_id, ghost_run.to_dict()
    )
    result = validate_package(tmp_path, evidence, ["GHOST-CLAIM"])

    assert result.passed is False
    keys = [
        (error.claim_id or "", error.kind.value, error.message)
        for error in result.errors
    ]
    assert keys == sorted(keys)
    # Every failure names the offending record.
    assert all(error.claim_id is not None for error in result.errors)


# ---------------------------------------------------------------------------
# boundaries -- structural failures raise
# ---------------------------------------------------------------------------


def test_audit_uninitialized_workspace_raises(tmp_path: Path) -> None:
    """Validation without a project state record raises (stable message)."""
    with pytest.raises(AuditNotInitializedError, match="project state"):
        build_audit_package(tmp_path, EvidenceRegistry(), [CLAIM_ID])
    with pytest.raises(AuditNotInitializedError, match="project state"):
        validate_package(tmp_path, EvidenceRegistry(), [CLAIM_ID])


def test_audit_corrupt_run_record_raises(tmp_path: Path) -> None:
    """A corrupt stored run record surfaces as AuditCorruptError."""
    evidence = install_valid_chain(tmp_path)
    run_path = tmp_path / "runs" / f"{RUN_ID}.json"
    run_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(AuditCorruptError, match="run"):
        build_audit_package(tmp_path, evidence, [CLAIM_ID])


def test_audit_type_errors(tmp_path: Path) -> None:
    """Wrong argument types raise TypeError at the boundary."""
    evidence = EvidenceRegistry()
    with pytest.raises(TypeError, match="root"):
        build_audit_package(42, evidence, [CLAIM_ID])
    with pytest.raises(TypeError, match="evidence"):
        build_audit_package(tmp_path, None, [CLAIM_ID])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="key_claims"):
        build_audit_package(tmp_path, evidence, "CLAIM-001")
    with pytest.raises(TypeError, match="key_claims"):
        build_audit_package(tmp_path, evidence, [1])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="run_status"):
        run_status("not a run")  # type: ignore[arg-type]
