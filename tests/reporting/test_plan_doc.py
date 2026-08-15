"""Deterministic designed Plan document renderer tests (issue #105).

Every test renders the frozen Plan v1 of a fully linked two-goal
workspace installed through the **real** registration APIs and the real
freeze gate (``planning.freeze.freeze_plan``), so the renderer is
exercised against the same registered state the freeze flow writes.
The ``ac01``/``ac02``/``ac03`` sections map to the issue requirements:

* ``ac01`` -- the document is a designed, print-ready A4 document:
  cover page (project id, primary target, freeze stamp, domain pack),
  scope declaration with the inventory coverage summary (ADR 5),
  requirement table (criticality + outcome + checklist refs), the goal
  DAG diagram (SVG, six-kind gates) and one section per goal with the
  replication design, acceptance criteria incl. every margin and its
  SS8 provenance (07 SS8 -- no unexplained numbers), analysis protocol
  summary and closure contract summary;
* ``ac02`` -- determinism: repeated renders from the same registered
  state are byte-identical, the generation timestamp is injected, and
  ``write_plan_document`` persists ``reports/plan-<version>.html``
  with a SHA-256 checksum sidecar;
* ``ac03`` -- boundaries: uninitialized workspace, missing plan /
  invalid version, corrupt records, dangling refs and type errors all
  surface with stable errors -- missing records are surfaced in the
  document, never silently dropped.

The deterministic path mirrors ``reporting_helpers``: fixed identities
and timestamps (``TIMESTAMP`` / ``FROZEN_AT``), so every fixture is
deterministic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from reporting_helpers import (
    ACCEPTANCE_ID,
    ANALYSIS_ID,
    DOI,
    FROZEN_AT,
    GOAL_ID,
    IDENTITY,
    INVENTORY_ID,
    SOURCE_ID,
    TIMESTAMP,
    make_acceptance,
    make_goal,
    make_protocol,
    make_requirement,
)

from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ClosureContract,
    ClosureLiterature,
    ClosureRecovery,
    DecisionMode,
    DependencyType,
    GoalAcceptance,
    GoalContract,
    GoalDependency,
    GoalReplication,
    GoalTrack,
    InventoryItemType,
    MappingStatus,
    MarginBasis,
    ReproductionInventoryItem,
    StatisticalDesign,
)
from scientific_reproduction.planning.freeze import freeze_plan
from scientific_reproduction.planning.init import (
    INITIAL_PLAN_VERSION,
    initialize_project,
)
from scientific_reproduction.planning.inventory import (
    register_inventory_item,
    register_requirement,
)
from scientific_reproduction.planning.plan import (
    InvalidPlanVersionError,
    PlanNotFoundError,
    build_plan_v1,
    register_acceptance,
    register_analysis_protocol,
    register_closure_contract,
    register_goal,
    register_plan,
    register_statistical_design,
)
from scientific_reproduction.reporting.plan_doc import (
    PLAN_DOC_VERSION,
    PlanDocCorruptError,
    PlanDocNotInitializedError,
    PlanDocument,
    render_plan_document,
    write_plan_document,
)

#: Fixed generation timestamp injected into every render (no wall clock).
GENERATED_AT = datetime(2026, 6, 2, tzinfo=timezone.utc)


def make_statistical_design(
    design_id: str = "STAT-001",
    *,
    goal_id: str = GOAL_ID,
    margin_basis: MarginBasis = MarginBasis.REPRODUCTION_LITERATURE,
) -> StatisticalDesign:
    """Build a schema-valid statistical design draft (SS9)."""
    return StatisticalDesign(
        design_id=design_id,
        goal_id=goal_id,
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        metrics=["batch_level_uptake"],
        replication=GoalReplication(
            independent_required=True, planned_n_policy="n=1 per condition"
        ),
        primary_method="equivalence_test",
        margin={"type": "relative_tolerance", "value": 0.05},
        margin_basis=margin_basis,
        alpha=0.05,
        confidence_level=0.95,
        rationale="Independent reproduction literature of the FDM-201 case.",
    )


def make_mapped_item(
    inventory_id: str = INVENTORY_ID,
    *,
    requirement_ids: list[str] | None = None,
) -> ReproductionInventoryItem:
    """Build a schema-valid inventory item linked to registered
    requirements (the R-MAP-M1 mapped pattern: the recomputed audit
    passes, so the freeze gate opens)."""
    return ReproductionInventoryItem(
        inventory_id=inventory_id,
        source_id=SOURCE_ID,
        item_type=InventoryItemType.EXPERIMENT,
        formal_report=True,
        description="Batch adsorption experiment of the FDM-201 case",
        mapping_status=MappingStatus.MAPPED,
        requirement_ids=[] if requirement_ids is None else list(requirement_ids),
    )


def make_dependent_goal() -> GoalContract:
    """The dependent goal of the fixture: hard-gated on GOAL-001 with
    execution + acceptance gates (the FDM-201 pattern)."""
    return GoalContract(
        goal_id="GOAL-002",
        title="Reproduce the FDM-201 batch-level uptake",
        unit_process_type="batch_adsorption",
        track=GoalTrack.STRICT_REPRODUCTION,
        objective=(
            "Reproduce the batch-level uptake results of the FDM-201"
            " reference case"
        ),
        requirement_ids=["REQ-002"],
        dependencies=[
            GoalDependency(
                goal_id=GOAL_ID,
                type=DependencyType.HARD_GATE,
                execution_gate=True,
                acceptance_gate=True,
            )
        ],
        acceptance=GoalAcceptance(criteria_ref="ACC-002", frozen=True),
        analysis_protocol_ref=ANALYSIS_ID,
        replication=GoalReplication(
            independent_required=True, planned_n_policy="n=1 per condition"
        ),
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        closure_contract_ref=None,
    )


def make_goal_two_acceptance() -> AcceptanceCriteria:
    """The acceptance criteria of the dependent goal (GOAL-002)."""
    return AcceptanceCriteria(
        acceptance_id="ACC-002",
        goal_id="GOAL-002",
        version=INITIAL_PLAN_VERSION,
        frozen=False,
        decision_mode=DecisionMode.EQUIVALENCE,
        criteria=[{"metric": "batch_level_uptake", "tolerance": 0.05}],
        evidence_refs=[],
    )


def make_closure(closure_id: str = "CLOS-001") -> ClosureContract:
    """Build a schema-valid closure contract draft."""
    return ClosureContract(
        closure_id=closure_id,
        frozen=False,
        statistical_sufficiency={"criterion": "equivalence bound covered"},
        execution_validity={"criterion": "no instrument drift recorded"},
        diagnosis={"rule": "recovery_diagnosis"},
        recovery=ClosureRecovery(
            eligible_hypotheses_total=4, tested_or_ruled_out=3, remaining=1
        ),
        literature=ClosureLiterature(
            required_search_families_completed=True,
            consecutive_zero_novelty_cycles=2,
        ),
        closure_allowed=False,
    )


def install_plan_workspace(
    root: Path,
    *,
    statistical_design: StatisticalDesign | None = None,
    design_ref: str | None = None,
    closure_contract: ClosureContract | None = None,
    closure_ref: str | None = None,
) -> None:
    """Install a freeze-ready two-goal workspace with a frozen plan v1.

    Registers, through the real registration APIs in authoring order:
    the project, two mapped inventory items, two requirements, the
    analysis protocol, the (optional) statistical design, the (optional)
    closure contract, two goals (GOAL-002 hard-gated on GOAL-001 with
    execution + acceptance gates, the FDM-201 pattern), their acceptance
    criteria, then the Plan v1 draft via ``build_plan_v1`` +
    ``register_plan`` and freezes it through the real audit gate with the
    fixed ``FROZEN_AT`` stamp.
    """
    initialize_project(root, DOI, timestamp=TIMESTAMP, identity=IDENTITY)
    register_inventory_item(root, make_mapped_item(requirement_ids=["REQ-001"]))
    register_inventory_item(
        root,
        make_mapped_item(inventory_id="INV-002", requirement_ids=["REQ-002"]),
    )
    register_requirement(root, make_requirement())
    register_requirement(
        root,
        make_requirement(
            requirement_id="REQ-002",
            inventory_items=["INV-002"],
            goal_ids=["GOAL-002"],
        ),
    )
    register_analysis_protocol(root, make_protocol())
    if statistical_design is not None:
        register_statistical_design(root, statistical_design)
    if closure_contract is not None:
        register_closure_contract(root, closure_contract)
    effective_design_ref = (
        design_ref
        if design_ref is not None
        else (statistical_design.design_id if statistical_design is not None else None)
    )
    effective_closure_ref = (
        closure_ref
        if closure_ref is not None
        else (closure_contract.closure_id if closure_contract is not None else None)
    )
    register_goal(root, make_goal(closure_contract_ref=effective_closure_ref))
    register_goal(root, make_dependent_goal())
    register_acceptance(
        root, make_acceptance(statistical_design_ref=effective_design_ref)
    )
    register_acceptance(root, make_goal_two_acceptance())
    plan = build_plan_v1(root)
    register_plan(root, plan)
    freeze_plan(root, plan, timestamp=FROZEN_AT)


def _render(root: Path) -> PlanDocument:
    """Render the frozen plan v1 with the fixed generation timestamp."""
    return render_plan_document(root, "v1", generated_at=GENERATED_AT)


def _sections(doc: PlanDocument) -> dict[str, str]:
    """Map the document sections by title (body strings)."""
    return {section.title: section.body for section in doc.sections}


# ---------------------------------------------------------------------------
# ac01 -- designed document covering the frozen plan
# ---------------------------------------------------------------------------


def test_plan_doc_sections_cover_plan_in_fixed_order_ac01(
    tmp_path: Path,
) -> None:
    """The document covers the frozen plan in a fixed section order:
    scope declaration, requirements, goal DAG, then one goal section per
    plan goal sorted by goal id (AC-01)."""
    install_plan_workspace(tmp_path)
    doc = _render(tmp_path)

    assert isinstance(doc, PlanDocument)
    assert [section.title for section in doc.sections] == [
        "Scope declaration",
        "Requirements",
        "Goal DAG",
        "Goal GOAL-001",
        "Goal GOAL-002",
    ]
    html_text = doc.to_html()
    assert html_text.startswith("<!DOCTYPE html>")
    assert "Scientific Reproduction Skill · Plan document" in html_text
    # Page footer: project id + plan version.
    assert f"Project {doc.project_id} · Plan v1" in html_text
    # Table of contents lists every section (numbered entries).
    assert "Table of contents" in html_text
    for section in doc.sections:
        assert f"{section.title}</a>" in html_text


def test_plan_doc_cover_renders_identity_and_freeze_stamp_ac01(
    tmp_path: Path,
) -> None:
    """The cover renders the project id, the primary target (DOI), the
    domain pack and the freeze stamp ``frozen_at``/``frozen_commit``
    (AC-01)."""
    install_plan_workspace(tmp_path)
    doc = _render(tmp_path)
    text = doc.to_html()

    assert doc.project_id
    assert doc.primary_target == DOI
    assert doc.domain_pack == "materials-chemistry"
    assert doc.plan_version == "v1"
    assert doc.plan_status == "FROZEN"
    assert doc.frozen_at == "2026-06-01T00:00:00Z"
    assert doc.frozen_commit  # the pre-freeze git HEAD of the workspace
    assert doc.generated_at == "2026-06-02T00:00:00Z"
    assert doc.title.startswith("Reproduction Plan")
    for label in (
        "Project ID",
        "Primary target",
        "Target title",
        "Domain pack",
        "Plan version",
        "Plan status",
        "Frozen at",
        "Frozen commit",
        "Generated at",
        "Renderer version",
    ):
        assert f"<td>{label}</td>" in text
    assert doc.project_id in text
    assert DOI in text
    assert "2026-06-01T00:00:00Z" in text
    assert "materials-chemistry" in text
    assert PLAN_DOC_VERSION in text


def test_plan_doc_scope_renders_inventory_coverage_ac01(tmp_path: Path) -> None:
    """The scope declaration renders the ADR 5 scope statement and the
    inventory coverage summary of the frozen plan (AC-01)."""
    install_plan_workspace(tmp_path)
    body = _sections(_render(tmp_path))["Scope declaration"]

    assert "ADR 5" in body
    assert "formally reported" in body
    assert "Formally reported items" in body
    assert "<td>2</td>" in body  # both items formally reported
    assert "Mapped items" in body
    assert "Coverage" in body
    assert "1.00" in body
    assert "PASS" in body
    # The audit is recomputed from the registered state at render time.
    assert "Recomputed verdict: PASS (rule R-AUD-P1)" in body
    # The plan record table cites the auditable plan id.
    assert "Plan record" in body
    assert "Plan ID" in body


def test_plan_doc_requirements_table_renders_criticality_and_refs_ac01(
    tmp_path: Path,
) -> None:
    """The requirements section renders every plan requirement with its
    criticality, outcome and checklist refs (AC-01)."""
    install_plan_workspace(tmp_path)
    body = _sections(_render(tmp_path))["Requirements"]

    assert "REQ-001" in body
    assert "REQ-002" in body
    assert "Batch-level uptake must be reproduced within tolerance" in body
    assert "CRITICAL" in body
    assert "chip-required" not in body  # both requirements are CRITICAL
    assert "REPRODUCED" in body
    assert "INV-001" in body
    assert "INV-002" in body
    for header in (
        "Requirement",
        "Statement",
        "Criticality",
        "Outcome",
        "Checklist refs",
    ):
        assert f"<th>{header}</th>" in body


def test_plan_doc_dag_renders_svg_and_gate_kinds_ac01(tmp_path: Path) -> None:
    """The goal DAG section renders the deterministic SVG diagram with
    strength-coded edges, the legend and the edge table carrying the
    six-kind gates (AC-01)."""
    install_plan_workspace(tmp_path)
    body = _sections(_render(tmp_path))["Goal DAG"]

    assert '<svg xmlns="http://www.w3.org/2000/svg"' in body
    assert 'aria-label="Plan goal DAG"' in body
    assert "GOAL-001" in body
    assert "GOAL-002" in body
    assert "arrow-hard" in body  # arrowhead marker of the hard gate
    assert "hard_gate" in body  # legend chip
    # The edge table: dependency-first row with both gate flags and the
    # classified gate kind (R-AX-B1: both gates -> execution axis).
    assert "hard_gate" in body
    assert "hard_execution" in body
    assert "Execution gate" in body
    assert "Acceptance gate" in body
    assert "Gate kind" in body
    assert "Dependency edges" in body


def test_plan_doc_goal_sections_render_design_and_closure_ac01(
    tmp_path: Path,
) -> None:
    """Each per-goal section renders the unit process type, objective,
    dependencies with gate kinds, replication design, analysis protocol
    summary and closure contract summary (AC-01)."""
    install_plan_workspace(tmp_path, closure_contract=make_closure())
    doc = _render(tmp_path)
    goal = _sections(doc)["Goal GOAL-001"]

    # Goal head + meta: unit process type, objective, track, refs.
    assert "GOAL-001 — Reproduce the FDM-201 batch-level uptake" in goal
    assert "Unit process type" in goal
    assert "batch_adsorption" in goal
    assert "Reproduce the batch-level uptake results" in goal
    assert "STRICT_REPRODUCTION" in goal
    assert "Goal version" in goal
    assert "Analysis protocol ref" in goal
    assert "Closure contract ref" in goal
    # Replication design (SS2 of 05-GOAL-RUN-SCHEMA).
    assert "Independent required" in goal
    assert "yes" in goal
    assert "n=1 per condition" in goal
    # Analysis protocol summary.
    assert "ANAL-001" in goal
    assert "isotherm_fit" in goal
    # Closure contract summary.
    assert "CLOS-001" in goal
    assert "equivalence bound covered" in goal
    assert "Closure allowed" in goal

    # The dependent goal renders its dependency row with the gate kind.
    goal2 = _sections(doc)["Goal GOAL-002"]
    assert "Reproduce the FDM-201 batch-level uptake" in goal2
    assert "GOAL-001" in goal2
    assert "hard_gate" in goal2
    assert "hard_execution" in goal2
    assert "no dependencies declared" not in goal2


def test_plan_doc_acceptance_margins_render_ss8_provenance_ac01(
    tmp_path: Path,
) -> None:
    """The acceptance criteria render every margin with its SS8
    provenance: the registered statistical design names the margin basis
    and rationale -- no unexplained numbers (AC-01)."""
    install_plan_workspace(tmp_path, statistical_design=make_statistical_design())
    goal = _sections(_render(tmp_path))["Goal GOAL-001"]

    # The kv summary of the criteria record.
    assert "Acceptance criteria" in goal
    assert "Decision mode" in goal
    assert "equivalence" in goal
    assert "Statistical design ref" in goal
    # The margin table flags the numeric margin and cites its basis.
    assert "Margin / parameter" in goal
    assert "Provenance (07 SS8)" in goal
    assert "criteria[1].metric" in goal
    assert "margin" in goal  # the numeric-value chip
    assert "basis: reproduction_literature" in goal
    assert "Independent reproduction literature of the FDM-201 case." in goal
    # The design block (SS9: the design is a first-class frozen record).
    assert "STAT-001" in goal
    assert "equivalence_test" in goal
    assert "0.05" in goal  # margin value and alpha
    assert "0.95" in goal  # confidence level
    assert "Margin basis (07 SS8)" in goal
    assert "Confidence" in goal


def test_plan_doc_acceptance_without_design_surfaces_missing_basis_ac01(
    tmp_path: Path,
) -> None:
    """A plan without a registered statistical design renders the margins
    with an explicit "no basis on record (07 SS8)" label -- the missing
    basis is surfaced, never invented (AC-01)."""
    install_plan_workspace(tmp_path)
    goal = _sections(_render(tmp_path))["Goal GOAL-001"]

    assert "no basis on record (07 SS8)" in goal
    assert "No statistical design record: the criteria margins carry no SS8" in goal


# ---------------------------------------------------------------------------
# ac02 -- determinism and the persisted artifact
# ---------------------------------------------------------------------------


def test_plan_doc_render_is_byte_identical_ac02(tmp_path: Path) -> None:
    """Repeated renders from the same registered state are byte-identical
    and the generation timestamp is injected (no wall clock) (AC-02)."""
    install_plan_workspace(tmp_path)
    first = _render(tmp_path).to_html()
    second = _render(tmp_path).to_html()

    assert first == second
    assert _render(tmp_path).to_canonical_json() == _render(
        tmp_path
    ).to_canonical_json()
    # A different injected timestamp changes the cover stamp only.
    later = render_plan_document(
        tmp_path,
        "v1",
        generated_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    ).to_html()
    assert later != first
    assert "2026-06-03T00:00:00Z" in later
    assert first.replace("2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z") == later


def test_plan_doc_write_persists_reports_with_checksum_ac02(
    tmp_path: Path,
) -> None:
    """``write_plan_document`` persists the document under ``reports/``
    with a SHA-256 checksum sidecar and repeated writes are
    byte-identical (AC-02)."""
    install_plan_workspace(tmp_path)
    result = write_plan_document(tmp_path, "v1", generated_at=GENERATED_AT)

    assert result.html_path == tmp_path / "reports" / "plan-v1.html"
    assert result.checksum_path == tmp_path / "reports" / "plan-v1.html.sha256"
    html_text = result.html_path.read_text(encoding="utf-8")
    assert html_text == result.document.to_html()
    checksum = result.checksum_path.read_text(encoding="utf-8")
    assert checksum == f"{result.sha256}  plan-v1.html\n"
    assert hashlib.sha256(html_text.encode("utf-8")).hexdigest() == result.sha256
    # The checksum is machine-verifiable against the stored bytes.
    second = write_plan_document(tmp_path, "v1", generated_at=GENERATED_AT)
    assert second.sha256 == result.sha256
    assert second.html_path.read_text(encoding="utf-8") == html_text


def test_plan_doc_dangling_design_ref_is_surfaced_ac02(tmp_path: Path) -> None:
    """A dangling statistical design ref (constructible only by an
    after-the-fact record rewrite -- the freeze gate itself requires the
    ref to resolve) is surfaced with its SS8 provenance unresolved,
    never silently dropped (AC-02)."""
    install_plan_workspace(tmp_path)
    path = tmp_path / "acceptance" / f"{ACCEPTANCE_ID}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["statistical_design_ref"] = "STAT-MISSING"
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    goal = _sections(_render(tmp_path))["Goal GOAL-001"]

    assert "STAT-MISSING" in goal
    assert "design ref unresolved" in goal
    assert "has no registered statistical design record" in goal


# ---------------------------------------------------------------------------
# ac03 -- boundaries: missing records are surfaced, corrupt state raises
# ---------------------------------------------------------------------------


def test_plan_doc_dangling_acceptance_ref_is_surfaced_ac03(
    tmp_path: Path,
) -> None:
    """A dangling acceptance criteria ref is surfaced in the goal section
    with a stable note (AC-03)."""
    install_plan_workspace(tmp_path)
    path = tmp_path / "goals" / f"{GOAL_ID}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["acceptance"]["criteria_ref"] = "ACC-MISSING"
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    goal = _sections(_render(tmp_path))["Goal GOAL-001"]

    assert "No registered acceptance criteria for criteria ref ACC-MISSING" in goal


def test_plan_doc_missing_goal_contract_is_surfaced_ac03(tmp_path: Path) -> None:
    """A plan goal without a registered goal contract is surfaced in the
    DAG section (never silently dropped) and no fabricated goal section
    is rendered (AC-03)."""
    install_plan_workspace(tmp_path)
    (tmp_path / "goals" / f"{GOAL_ID}.json").unlink()
    doc = _render(tmp_path)

    assert "Goal GOAL-001" not in _sections(doc)
    assert "Goal GOAL-002" in _sections(doc)
    dag = _sections(doc)["Goal DAG"]
    assert "never silently dropped" in dag
    assert "GOAL-001" in dag
    # The dependent edge cannot render either: unresolved ref, surfaced.
    assert "Dependency targets without a registered goal contract" in dag
    assert "GOAL-002 → GOAL-001" in dag


def test_plan_doc_missing_requirement_record_is_surfaced_ac03(
    tmp_path: Path,
) -> None:
    """A plan requirement without a registered record renders as an
    explicit "no registered requirement record" row (AC-03)."""
    install_plan_workspace(tmp_path)
    (tmp_path / "requirements" / "REQ-002.json").unlink()
    body = _sections(_render(tmp_path))["Requirements"]

    assert "REQ-002" in body
    assert "no registered requirement record" in body
    assert "REQ-001" in body


def test_plan_doc_uninitialized_workspace_raises_ac03(tmp_path: Path) -> None:
    """Rendering without an initialized project raises the stable
    not-initialized error."""
    with pytest.raises(PlanDocNotInitializedError, match="project"):
        render_plan_document(tmp_path, generated_at=GENERATED_AT)


def test_plan_doc_plan_not_found_propagates_ac03(tmp_path: Path) -> None:
    """A plan version with no registered record propagates
    ``PlanNotFoundError`` unchanged (input error, not corrupt state)."""
    install_plan_workspace(tmp_path)
    with pytest.raises(PlanNotFoundError):
        render_plan_document(tmp_path, "v9", generated_at=GENERATED_AT)


def test_plan_doc_invalid_plan_version_propagates_ac03(tmp_path: Path) -> None:
    """A malformed plan version propagates ``InvalidPlanVersionError``
    unchanged."""
    install_plan_workspace(tmp_path)
    with pytest.raises(InvalidPlanVersionError):
        render_plan_document(tmp_path, "not-a-version", generated_at=GENERATED_AT)


def test_plan_doc_corrupt_record_raises_ac03(tmp_path: Path) -> None:
    """A corrupt stored goal record surfaces as ``PlanDocCorruptError``
    with the stable message."""
    install_plan_workspace(tmp_path)
    (tmp_path / "goals" / f"{GOAL_ID}.json").write_text(
        "{not json", encoding="utf-8"
    )

    with pytest.raises(PlanDocCorruptError, match="corrupt registered state"):
        render_plan_document(tmp_path, "v1", generated_at=GENERATED_AT)


def test_plan_doc_type_and_naive_timestamp_errors_ac03(tmp_path: Path) -> None:
    """Wrong argument types raise TypeError at the boundary and a naive
    ``generated_at`` is rejected (AC-03)."""
    with pytest.raises(TypeError, match="root"):
        render_plan_document(42, generated_at=GENERATED_AT)
    with pytest.raises(TypeError, match="version"):
        render_plan_document(tmp_path, 42, generated_at=GENERATED_AT)
    with pytest.raises(TypeError, match="generated_at"):
        render_plan_document(tmp_path, generated_at="2026-06-02")
    with pytest.raises(ValueError, match="timezone-aware"):
        render_plan_document(
            tmp_path, generated_at=datetime(2026, 6, 2)
        )
    with pytest.raises(TypeError, match="out_dir"):
        write_plan_document(tmp_path, "v1", out_dir=42)
