"""Final reproduction report -- deterministic PDF renderer (issue #107).

Implements the **final reproduction report** deliverable of issue #107:
a review-ready, deterministic PDF document rendered from the persisted
project state only (no agent prose, no wall clock, no network). The
report builds on the shared ``rendering`` visual system (the same one
the plan renderer and the execution-sheet renderers use).

What is assembled, and from where
---------------------------------
Every figure in the report comes from a recorded record or a registry
read-through -- never from free text:

* the **executive summary** -- the verdict callout renders
  ``ReproductionOutcome`` / ``MethodReproducibility`` verbatim from the
  outcome aggregation of ``reporting.summary.build_summary`` (the frozen
  vocabularies of ``05-GOAL-RUN-SCHEMA.md`` SS2); the finding paragraph
  is assembled from the recorded requirement outcomes and the run
  statistics of ``reporting.audit`` (``RunStatus``); the single most
  important number is the first metric of the first analysis result
  (sorted by id) that references a CRITICAL requirement, with its
  recorded confidence interval and the frozen acceptance band
  (``AcceptanceCriteria.criteria`` tolerance), falling back to an
  explicit "not recorded" line;
* the **scope / pipeline / requirement outcome** sections -- counts and
  tables from the planning/inventory registries (``list_goals``,
  ``list_acceptance``, ``list_analysis_protocols``,
  ``list_statistical_designs``, ``list_closure_contracts``,
  ``list_requirements``, ``list_inventory_items``), the analysis
  registry (``list_results``), the run store through the audit package
  and the artifact manifests (``ArtifactRegistry``);
* the **core findings** per CRITICAL requirement -- the evidence trail
  of ``14-STATE-GIT-ARTIFACTS.md`` SS7: analysis results whose
  ``requirement_refs`` name the requirement, evidence records whose
  ``used_by`` names it, and supervisor decisions whose ``affected_refs``
  name it (decisions are read through ``FilesystemStateBackend``
  obj_type ``"decision"`` -- no registry API exists for them);
* the **governance** section -- the recovery ladder maps the recorded
  ``MethodReproducibility`` onto the L1-L4 vocabulary of
  ``08-STRICT-RECOVERY-CLOSURE.md`` (per the mapping documented in
  ``reporting.summary``: minor recovery ~ L1/L2, method adjustment ~
  L3, redesign ~ L4), the recorded statistical designs render the
  n-policy / margin decisions, supervisor decisions render resolutions
  and revision/rejection decisions, and the monitor reconciliations
  render the ``external_status_change`` events of the event log;
* the **audit trail** -- git HEAD/commit count (``audit.git`` read
  APIs), the frozen plan refs (``frozen_at`` / ``frozen_commit`` of the
  plan records), the recorded event log (checkpoint kinds via
  ``EVENT_TYPE_TO_CHECKPOINT``) and the artifact manifest table with
  full SHA-256 checksums;
* the **simulation / real-data labeling** -- derived from the recorded
  inventory item types (a ``COMPUTATION`` item labels the project as
  containing simulation/computation; never silently real-data).

Determinism
-----------
``build_pdf_report`` takes ``generated_at`` as a required string
argument -- the renderer never consults the clock. Every collection is
sorted, and the document renders through the deterministic
``rendering.pdf`` writer, so identical state renders byte-identical
PDFs and canonical JSON.

The renderer also writes ``reproduction-report.pdf`` and a canonical
JSON sidecar (``reproduction-report.json``) carrying the PDF's SHA-256
checksum, so the audit package can register the report files (the
issue requires report files to be registered with checksums).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Sequence

from scientific_reproduction.analysis.results import ResultRecord, list_results
from scientific_reproduction.artifacts.registry import ArtifactRegistry
from scientific_reproduction.audit.git import (
    EVENT_TYPE_TO_CHECKPOINT,
    count_commits,
    current_head,
)
from scientific_reproduction.core.events import ProjectEventLog
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    ClosureContract,
    Criticality,
    GoalContract,
    InventoryItemType,
    MethodReproducibility,
    Project,
    ProjectEvent,
    ReproductionRequirement,
    StatisticalDesign,
    SupervisorDecision,
)
from scientific_reproduction.core.state_backend import FilesystemStateBackend
from scientific_reproduction.monitoring.reconcile import (
    EXTERNAL_STATUS_CHANGE_EVENT_TYPE,
)
from scientific_reproduction.planning.init import read_project_state
from scientific_reproduction.planning.inventory import (
    list_inventory_items,
    list_requirements,
)
from scientific_reproduction.planning.plan import (
    list_acceptance,
    list_analysis_protocols,
    list_closure_contracts,
    list_goals,
    list_plans,
    list_statistical_designs,
)
from scientific_reproduction.rendering import FlowLayout, PdfDocument
from scientific_reproduction.rendering.style import FONT_BOLD, HEADING_SIZES
from scientific_reproduction.reporting.audit import (
    AuditCorruptError,
    AuditNotInitializedError,
    AuditPackage,
    build_audit_package,
)
from scientific_reproduction.reporting.summary import (
    OutcomeSummary,
    SummaryCorruptError,
    SummaryNotInitializedError,
    build_summary,
)
from scientific_reproduction.research.evidence import EvidenceRegistry

#: Version of the rendered report document (independent of the markdown
#: report's ``REPORT_VERSION``).
REPORT_VERSION: Final[str] = "1.0"

#: Report file names written to the out dir (registered by the audit
#: package with checksums).
PDF_FILENAME: Final[str] = "reproduction-report.pdf"
JSON_FILENAME: Final[str] = "reproduction-report.json"

#: Section titles in render order.
SECTION_TITLES: Final[tuple[str, ...]] = (
    "Executive summary",
    "Target paper identity and reproduction scope",
    "Pipeline summary",
    "Requirement outcomes",
    "Core findings",
    "Governance exercised",
    "Audit trail",
    "Simulation and real-data labeling",
)

#: Decision types that record an acceptance/goal/protocol revision or
#: rejection -- the "AC-02 collection rejections" of the governance
#: section.
REVISION_DECISION_TYPES: Final[frozenset[str]] = frozenset(
    {
        "ACCEPTANCE_REVISION",
        "GOAL_REVISION",
        "ANALYSIS_PROTOCOL_REVISION",
        "RESEARCH_REQUEST",
    }
)

#: Recovery ladder labels (08-STRICT-RECOVERY-CLOSURE.md L1-L4, mapped
#: per the documentation of ``reporting.summary``).
_RECOVERY_LABELS: Final[dict[MethodReproducibility, str]] = {
    MethodReproducibility.DIRECTLY_REPRODUCIBLE: "L1 direct",
    MethodReproducibility.REPRODUCIBLE_WITH_MINOR_RECOVERY: "L1/L2 minor recovery",
    MethodReproducibility.REPRODUCIBLE_WITH_METHOD_ADJUSTMENT: "L3 method adjustment",
    MethodReproducibility.ONLY_REPRODUCIBLE_AFTER_REDESIGN: "L4 redesign",
    MethodReproducibility.NOT_REPRODUCIBLE: "not reproducible",
    MethodReproducibility.UNDETERMINED: "undetermined",
    MethodReproducibility.INCONCLUSIVE: "inconclusive",
}

#: Inventory item types that represent real (non-computed) data.
_REAL_DATA_TYPES: Final[frozenset[InventoryItemType]] = frozenset(
    {
        InventoryItemType.EXPERIMENT,
        InventoryItemType.CONTROL,
        InventoryItemType.CHARACTERIZATION,
        InventoryItemType.DATASET,
        InventoryItemType.STRUCTURE,
        InventoryItemType.FIGURE,
        InventoryItemType.TABLE,
        InventoryItemType.SUPPLEMENTARY_RESULT,
        InventoryItemType.ANALYSIS,
        InventoryItemType.OTHER,
    }
)


class PdfReportError(ValueError):
    """Base error of the PDF report renderer."""


class PdfReportNotInitializedError(PdfReportError):
    """The workspace has no project state to report on."""


class PdfReportCorruptError(PdfReportError):
    """A stored record is corrupt and the report cannot be rendered."""


@dataclass(frozen=True)
class PdfReportSection:
    """One rendered section with its final-order page number."""

    title: str
    page_number: int


@dataclass(frozen=True)
class PdfReport:
    """The rendered deterministic report document.

    ``pdf_bytes`` is the full PDF; ``pdf_sha256`` / ``pdf_size_bytes``
    allow the audit package to register the written file without
    re-hashing it.
    """

    report_version: str
    project_id: str
    primary_target_doi: str | None
    primary_target_title: str | None
    project_phase: str
    current_plan_version: str
    reproduction_outcome: str
    generated_at: str
    sections: tuple[PdfReportSection, ...]
    pages: int
    pdf_bytes: bytes
    pdf_sha256: str
    pdf_size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the report (the JSON sidecar content)."""
        return {
            "report_version": self.report_version,
            "project_id": self.project_id,
            "primary_target": {
                "doi": self.primary_target_doi,
                "title": self.primary_target_title,
            },
            "project_phase": self.project_phase,
            "current_plan_version": self.current_plan_version,
            "reproduction_outcome": self.reproduction_outcome,
            "generated_at": self.generated_at,
            "sections": [
                {"title": section.title, "page_number": section.page_number}
                for section in self.sections
            ],
            "pages": self.pages,
            "pdf_file": PDF_FILENAME,
            "pdf_sha256": self.pdf_sha256,
            "pdf_size_bytes": self.pdf_size_bytes,
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON of the sidecar (indent=2, sorted keys, newline)."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# pure helpers (deterministic)
# ---------------------------------------------------------------------------


def _fmt_number(value: Any) -> str:
    """Format a recorded metric value deterministically."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.2f}"
    return str(value)


def _verdict_for_outcome(outcome: str) -> str:
    """Map a frozen outcome value onto the verdict callout vocabulary."""
    if outcome in ("FULLY_REPRODUCED", "REPRODUCED", "REPRODUCED_WITH_RECOVERY"):
        return "PASS"
    if outcome in ("NOT_REPRODUCED", "NOT_REPRODUCED_WITHIN_DEFINED_SCOPE"):
        return "FAIL"
    if outcome in ("INCONCLUSIVE", "PARTIALLY_REPRODUCED"):
        return "INCONCLUSIVE"
    return outcome  # OPEN/UNDETERMINED/unknown -> neutral via verdict_style


def _data_label(item_types: frozenset[InventoryItemType]) -> str:
    """Derive the simulation/real-data label from recorded item types."""
    has_computation = InventoryItemType.COMPUTATION in item_types
    has_real = bool(item_types & _REAL_DATA_TYPES)
    if has_computation and has_real:
        return "mixed: real experimental data and computation/simulation"
    if has_computation:
        return "simulation/computation"
    if has_real:
        return "real experimental data"
    return "no inventory recorded"


def _headline_metric(
    results: Sequence[ResultRecord],
    requirements: Sequence[ReproductionRequirement],
    acceptance: Sequence[AcceptanceCriteria],
) -> str:
    """Assemble the single most important number with its band.

    The first analysis result (sorted by result id) referencing a
    CRITICAL requirement supplies the value, its recorded confidence
    interval and the frozen acceptance tolerance; everything falls back
    to explicit "not recorded" text -- never invented.
    """
    critical_ids = {
        requirement.requirement_id
        for requirement in requirements
        if requirement.criticality is Criticality.CRITICAL
    }
    hits = sorted(
        (
            result
            for result in results
            if critical_ids & set(result.requirement_refs)
        ),
        key=lambda result: result.result_id,
    )
    if not hits:
        return "No recorded analysis result references a CRITICAL requirement."
    result = hits[0]
    if not result.metrics:
        return (
            f"{result.result_id}: no recorded metrics "
            "(analysis ran without a metrics record)."
        )
    metric = result.metrics[0]
    name = str(metric.get("metric", "metric"))
    value = _fmt_number(metric.get("value"))
    uncertainty = result.uncertainty or {}
    confidence = uncertainty.get("confidence_level")
    lower = uncertainty.get("lower")
    upper = uncertainty.get("upper")
    if confidence is not None and lower is not None and upper is not None:
        interval = (
            f" ({_fmt_number(confidence)}% CI "
            f"{_fmt_number(lower)} to {_fmt_number(upper)})"
        )
    else:
        interval = " (confidence interval not recorded)"
    tolerance: Any = None
    band_ref = ""
    for criteria in acceptance:
        if criteria.acceptance_id == result.acceptance_ref:
            band_ref = criteria.acceptance_id
            if criteria.criteria:
                tolerance = criteria.criteria[0].get("tolerance")
            break
    if tolerance is not None:
        band = f" vs frozen acceptance band +/-{_fmt_number(tolerance)}"
        if band_ref:
            band += f" ({band_ref})"
    else:
        band = " (acceptance band not recorded)"
    return (
        f"Most important number: {name} = {value}{interval}{band} "
        f"({result.result_id}, protocol {result.protocol_version})."
    )


def _finding_paragraph(
    summary: OutcomeSummary, package: AuditPackage
) -> str:
    """Assemble the executive-summary finding from recorded records."""
    counts: dict[str, int] = {}
    for entry in summary.requirements:
        counts[entry.outcome.value] = counts.get(entry.outcome.value, 0) + 1
    total = len(summary.requirements)
    by_outcome = ", ".join(
        f"{value} {count}" for value, count in sorted(counts.items())
    )
    runs = {"succeeded": 0, "failed": 0, "unresolved": 0}
    for run_entry in package.runs:
        runs[run_entry.status.value.lower()] += 1
    return (
        f"Recorded outcomes across {total} requirements: {by_outcome}. "
        f"runs: {runs['succeeded']} succeeded, {runs['failed']} failed, "
        f"{runs['unresolved']} unresolved."
    )


# ---------------------------------------------------------------------------
# record gathering
# ---------------------------------------------------------------------------


def _read_decisions(root: Path) -> tuple[SupervisorDecision, ...]:
    """Read the recorded supervisor decisions through the state backend
    (no registry API exists for decisions), sorted by id."""
    backend = FilesystemStateBackend(root)
    decisions: list[SupervisorDecision] = []
    for decision_id in sorted(backend.list_ids("decision")):
        data = backend.read("decision", decision_id)
        decisions.append(SupervisorDecision.from_dict(data))
    return tuple(decisions)


def _events_in_order(root: Path) -> tuple[ProjectEvent, ...]:
    """Every recorded event in deterministic (log) order."""
    events = ProjectEventLog(root).list_events()
    return tuple(
        record.event
        for record in sorted(events, key=lambda record: record.sequence)
    )


# ---------------------------------------------------------------------------
# the renderer
# ---------------------------------------------------------------------------


def _render_pdf(
    *,
    project: Project,
    summary: OutcomeSummary,
    package: AuditPackage,
    goals: Sequence[GoalContract],
    acceptance: Sequence[AcceptanceCriteria],
    protocols: Sequence[Any],
    designs: Sequence[StatisticalDesign],
    closures: Sequence[ClosureContract],
    plans: Sequence[Any],
    requirements: Sequence[ReproductionRequirement],
    inventory: Sequence[Any],
    results: Sequence[ResultRecord],
    manifests: Sequence[Any],
    decisions: Sequence[SupervisorDecision],
    events: Sequence[ProjectEvent],
    evidence: EvidenceRegistry,
    sources: Sequence[str],
    head: str | None,
    commits: int | None,
    generated_at: str,
) -> tuple[bytes, tuple[PdfReportSection, ...], int]:
    """Render the report document and return (pdf bytes, sections, pages).

    The content sections are laid out first (recording their
    creation-order page indices), then the table of contents is laid out
    and moved to the front via ``reorder_pages`` -- TOC page count is a
    pure function of the entry count, so the content pagination is
    identical with or without the TOC pages, and the footers/bookmarks
    resolve against the final page order at render time.
    """
    doc = PdfDocument(title=f"Reproduction Report - {project.project_id}")
    layout = FlowLayout(
        doc,
        footer_left=project.project_id,
        footer_right="Page {page} of {total}",
    )
    laid_out: list[tuple[str, int]] = []

    def section(title: str) -> None:
        layout.heading(title, level=1)
        laid_out.append((title, layout.headings[-1].page_index))

    # Cover header.
    layout.heading("Reproduction Report", level=1)
    layout.paragraph(
        f"Project {project.project_id} - generated {generated_at} - "
        f"report version {REPORT_VERSION}"
    )

    # -- 1. executive summary ------------------------------------------------
    section("Executive summary")
    outcome = summary.reproduction_outcome.value
    callout_body = (
        f"Method reproducibility: {summary.method_reproducibility.value} "
        f"(ruleset {summary.method_ruleset_version}, rule "
        f"{summary.method_matched_rule_id})."
    )
    if summary.outcome_blocking_reasons:
        callout_body += (
            " Blocking reasons: " + "; ".join(summary.outcome_blocking_reasons)
        )
    layout.callout(_verdict_for_outcome(outcome), outcome, callout_body)
    layout.paragraph(_finding_paragraph(summary, package))
    layout.paragraph(_headline_metric(results, requirements, acceptance))

    # -- 2. target paper identity and scope -----------------------------------
    section("Target paper identity and reproduction scope")
    target = project.primary_target
    layout.table(
        headers=["Identity", "Value"],
        rows=[
            ["DOI", target.doi or "not recorded"],
            ["Title", target.title or "not recorded"],
            ["Source type", target.source_type.value],
            ["Project phase", project.project_phase.value],
            ["Current plan version", project.current_plan_version],
            ["Project id", project.project_id],
        ],
        widths=[200.0, 287.28],
    )
    layout.paragraph("Reproduction scope:", font=FONT_BOLD)
    layout.table(
        headers=["Record", "Count"],
        rows=[
            ["Goals", str(len(goals))],
            ["Requirements", str(len(requirements))],
            ["Inventory items", str(len(inventory))],
            ["Acceptance criteria", str(len(acceptance))],
            ["Analysis protocols", str(len(protocols))],
            ["Statistical designs", str(len(designs))],
            ["Closure contracts", str(len(closures))],
        ],
        widths=[200.0, 287.28],
    )
    layout.paragraph("Frozen acceptance criteria:", font=FONT_BOLD)
    if acceptance:
        layout.table(
            headers=["Acceptance", "Goal", "Frozen", "Mode", "Tolerance"],
            rows=[
                [
                    criteria.acceptance_id,
                    criteria.goal_id,
                    "yes" if criteria.frozen else "no",
                    criteria.decision_mode.value,
                    (
                        _fmt_number(criteria.criteria[0].get("tolerance"))
                        if criteria.criteria
                        and criteria.criteria[0].get("tolerance") is not None
                        else "-"
                    ),
                ]
                for criteria in acceptance
            ],
            widths=[90.0, 70.0, 50.0, 100.0, 177.28],
        )
    else:
        layout.paragraph("no recorded acceptance criteria.")

    # -- 3. pipeline summary ---------------------------------------------------
    section("Pipeline summary")
    succeeded = sum(1 for entry in package.runs if entry.status.value == "succeeded")
    failed = sum(1 for entry in package.runs if entry.status.value == "failed")
    unresolved = len(package.runs) - succeeded - failed
    layout.table(
        headers=["Stage", "Recorded state"],
        rows=[
            ["Research", f"{len(sources)} sources, {len(evidence)} evidence records"],
            ["Inventory", f"{len(inventory)} items, {len(requirements)} requirements"],
            [
                "Planning",
                f"{len(goals)} goals, {len(acceptance)} acceptance criteria, "
                f"{len(protocols)} protocols, {len(designs)} designs, "
                f"{len(closures)} closure contracts",
            ],
            [
                "Execution",
                f"{len(package.runs)} runs "
                f"({succeeded} succeeded, {failed} failed, {unresolved} unresolved)",
            ],
            ["Analysis", f"{len(results)} result packages"],
            ["Artifacts", f"{len(manifests)} manifests"],
        ],
        widths=[110.0, 377.28],
    )
    layout.paragraph("Runs:", font=FONT_BOLD)
    if package.runs:
        layout.table(
            headers=["Run", "Goal", "Type", "Lifecycle", "Review", "Status"],
            rows=[
                [
                    entry.run.run_id,
                    entry.run.goal_id,
                    entry.run.run_type.value,
                    entry.run.lifecycle_state.value,
                    entry.run.scientific_review.value,
                    entry.status.value,
                ]
                for entry in package.runs
            ],
            widths=[60.0, 70.0, 90.0, 90.0, 90.0, 87.28],
        )
    else:
        layout.paragraph("no recorded runs.")

    # -- 4. requirement outcomes ------------------------------------------------
    section("Requirement outcomes")
    if requirements:
        layout.table(
            headers=["Requirement", "Statement", "Criticality", "Outcome", "Method"],
            rows=[
                [
                    requirement.requirement_id,
                    requirement.statement,
                    requirement.criticality.value,
                    requirement.outcome.value,
                    (
                        requirement.method_reproducibility.value
                        if requirement.method_reproducibility is not None
                        else "UNDETERMINED"
                    ),
                ]
                for requirement in requirements
            ],
            widths=[70.0, 157.28, 60.0, 90.0, 110.0],
        )
    else:
        layout.paragraph("no recorded requirements.")
    layout.paragraph(
        f"runs ({len(package.runs)} total): {succeeded} succeeded, "
        f"{failed} failed, {unresolved} unresolved."
    )

    # -- 5. core findings (per CRITICAL requirement) -----------------------------
    section("Core findings")
    critical = [
        requirement
        for requirement in requirements
        if requirement.criticality is Criticality.CRITICAL
    ]
    if not critical:
        layout.paragraph("No CRITICAL requirements recorded.")
    for requirement in critical:
        layout.heading(
            f"{requirement.requirement_id} - {requirement.outcome.value}",
            level=2,
        )
        layout.callout(
            _verdict_for_outcome(requirement.outcome.value),
            requirement.outcome.value,
            requirement.statement,
        )
        layout.paragraph("Analysis results:", font=FONT_BOLD)
        requirement_results = [
            result
            for result in results
            if requirement.requirement_id in result.requirement_refs
        ]
        if requirement_results:
            layout.table(
                headers=["Result", "Analysis", "Protocol", "Metric", "Value"],
                rows=[
                    [
                        result.result_id,
                        result.analysis_id,
                        result.protocol_version,
                        str(result.metrics[0].get("metric"))
                        if result.metrics
                        else "-",
                        _fmt_number(result.metrics[0].get("value"))
                        if result.metrics
                        else "-",
                    ]
                    for result in requirement_results
                ],
                widths=[60.0, 70.0, 70.0, 140.0, 147.28],
            )
        else:
            layout.paragraph("no recorded analysis results.")
        layout.paragraph("Evidence records:", font=FONT_BOLD)
        evidence_records = [
            record
            for record in evidence
            if requirement.requirement_id in record.used_by
        ]
        if evidence_records:
            layout.table(
                headers=["Evidence", "Finding"],
                rows=[
                    [record.evidence_id, record.finding]
                    for record in evidence_records
                ],
                widths=[80.0, 407.28],
            )
        else:
            layout.paragraph("no recorded evidence records.")
        layout.paragraph("Decisions:", font=FONT_BOLD)
        requirement_decisions = [
            decision
            for decision in decisions
            if requirement.requirement_id in decision.affected_refs
        ]
        if requirement_decisions:
            layout.table(
                headers=["Decision", "Type", "Rationale"],
                rows=[
                    [
                        decision.decision_id,
                        decision.decision_type.value,
                        decision.rationale,
                    ]
                    for decision in requirement_decisions
                ],
                widths=[80.0, 130.0, 277.28],
            )
        else:
            layout.paragraph("no recorded decisions.")

    # -- 6. governance exercised -------------------------------------------------
    section("Governance exercised")
    layout.paragraph("Recovery ladder:", font=FONT_BOLD)
    layout.paragraph(
        f"Recorded method reproducibility: {summary.method_reproducibility.value} "
        f"-> {_RECOVERY_LABELS.get(summary.method_reproducibility, 'undetermined')} "
        f"(ruleset {summary.method_ruleset_version})."
    )
    recovery = summary.recovery
    layout.paragraph(
        f"Recovered requirements: "
        f"{', '.join(recovery.recovered_requirements) or 'none'}. "
        f"Recovery goals: {', '.join(recovery.recovery_goals) or 'none'}. "
        f"Method redesign goals: "
        f"{', '.join(recovery.method_redesign_goals) or 'none'}."
    )
    layout.paragraph("Closure contracts:", font=FONT_BOLD)
    if closures:
        layout.table(
            headers=["Closure", "Frozen", "Allowed", "Recovery progress"],
            rows=[
                [
                    closure.closure_id,
                    "yes" if closure.frozen else "no",
                    "yes" if closure.closure_allowed else "no",
                    (
                        f"eligible {closure.recovery.eligible_hypotheses_total}, "
                        f"tested or ruled out {closure.recovery.tested_or_ruled_out}, "
                        f"remaining {closure.recovery.remaining}"
                    ),
                ]
                for closure in closures
            ],
            widths=[90.0, 40.0, 40.0, 317.28],
        )
    else:
        layout.paragraph("no recorded closure contracts.")
    layout.paragraph("Statistical designs (recorded n/margin decisions):", font=FONT_BOLD)
    if designs:
        layout.table(
            headers=["Design", "Metrics", "n policy", "Margin", "Basis", "Alpha"],
            rows=[
                [
                    design.design_id,
                    ", ".join(design.metrics),
                    design.replication.planned_n_policy or "-",
                    _fmt_number(design.margin)
                    if isinstance(design.margin, (int, float))
                    else str(design.margin or "-"),
                    design.margin_basis.value if design.margin_basis else "-",
                    _fmt_number(design.alpha) if design.alpha is not None else "-",
                ]
                for design in designs
            ],
            widths=[60.0, 90.0, 100.0, 60.0, 80.0, 97.28],
        )
    else:
        layout.paragraph("no recorded statistical designs.")
    layout.paragraph("Supervisor decisions:", font=FONT_BOLD)
    if decisions:
        layout.table(
            headers=["Decision", "Type", "Rationale"],
            rows=[
                [decision.decision_id, decision.decision_type.value, decision.rationale]
                for decision in decisions
            ],
            widths=[80.0, 130.0, 277.28],
        )
    else:
        layout.paragraph("No recorded supervisor decisions.")
    layout.paragraph("AC-02 collection rejections:", font=FONT_BOLD)
    revisions = [
        decision
        for decision in decisions
        if decision.decision_type.value in REVISION_DECISION_TYPES
    ]
    if revisions:
        layout.paragraph(
            f"{len(revisions)} recorded revision/rejection decisions: "
            + ", ".join(decision.decision_id for decision in revisions)
            + "."
        )
    else:
        layout.paragraph("no recorded collection rejections.")
    layout.paragraph("Monitor reconciliations:", font=FONT_BOLD)
    reconciliations = [
        event for event in events if event.event_type == EXTERNAL_STATUS_CHANGE_EVENT_TYPE
    ]
    if reconciliations:
        layout.table(
            headers=["Event", "Timestamp", "Actor", "Transition", "Reason"],
            rows=[
                [
                    event.event_id,
                    event.timestamp,
                    event.actor,
                    f"{event.from_ or '-'} -> {event.to or '-'}",
                    event.reason or "-",
                ]
                for event in reconciliations
            ],
            widths=[60.0, 100.0, 80.0, 100.0, 147.28],
        )
    else:
        layout.paragraph("No recorded reconciliation events.")

    # -- 7. audit trail -----------------------------------------------------------
    section("Audit trail")
    layout.paragraph("Git state:", font=FONT_BOLD)
    if head is not None:
        layout.paragraph(f"HEAD {head}, {commits} commits.")
    else:
        layout.paragraph(
            "Git state not recorded (workspace is not a git repository)."
        )
    layout.paragraph("Frozen plan refs:", font=FONT_BOLD)
    if plans:
        layout.table(
            headers=["Version", "Status", "Frozen at", "Frozen commit"],
            rows=[
                [
                    plan.version,
                    getattr(plan, "status", "-"),
                    plan.frozen_at or "not frozen",
                    plan.frozen_commit or "not recorded",
                ]
                for plan in plans
            ],
            # The commit column must fit a full 40-hex SHA-1 on one line
            # (a wrapped SHA breaks its token and the audit ref).
            widths=[60.0, 90.0, 120.0, 217.28],
        )
    else:
        layout.paragraph("no recorded plans.")
    layout.paragraph("Checkpoint events:", font=FONT_BOLD)
    if events:
        layout.table(
            headers=["Event", "Timestamp", "Actor", "Type", "Object", "Checkpoint"],
            rows=[
                [
                    event.event_id,
                    event.timestamp,
                    event.actor,
                    event.event_type,
                    event.object_id or "-",
                    EVENT_TYPE_TO_CHECKPOINT.get(event.event_type, "-"),
                ]
                for event in events
            ],
            widths=[55.0, 90.0, 70.0, 115.0, 65.0, 92.28],
        )
    else:
        layout.paragraph("no recorded events.")
    layout.paragraph("Artifact manifests:", font=FONT_BOLD)
    if manifests:
        layout.table(
            headers=["Artifact", "SHA-256", "Size", "Run", "Analysis", "Producer"],
            rows=[
                [
                    manifest.artifact_id,
                    manifest.sha256,
                    str(manifest.size_bytes),
                    manifest.run_id or "-",
                    manifest.analysis_id or "-",
                    manifest.producer or "-",
                ]
                for manifest in manifests
            ],
            widths=[65.0, 150.0, 45.0, 70.0, 70.0, 87.28],
        )
    else:
        layout.paragraph("no recorded artifact manifests.")

    # -- 8. simulation / real-data labeling -----------------------------------------
    section("Simulation and real-data labeling")
    item_types = frozenset(item.item_type for item in inventory)
    layout.paragraph(f"Label: {_data_label(item_types)}")
    if item_types:
        layout.paragraph(
            "Recorded inventory item types: "
            + ", ".join(sorted(item_type.value for item_type in item_types))
            + "."
        )
    if inventory:
        layout.paragraph(
            "Recorded inventory items: "
            + ", ".join(item.inventory_id for item in inventory)
            + "."
        )

    # -- table of contents (laid out last, moved to the front) ----------------------
    layout.page_break()
    toc_start = len(doc.pages)
    layout.paragraph("Table of contents", font=FONT_BOLD, size=HEADING_SIZES[1])
    toc_pages = layout.toc_page_count(len(laid_out))
    for index, (title, page_index) in enumerate(laid_out, 1):
        layout.toc_entry(index, title, toc_pages + page_index + 1)
    actual_toc_pages = len(doc.pages) - toc_start
    if actual_toc_pages:
        doc.reorder_pages(
            list(range(toc_start, len(doc.pages))) + list(range(0, toc_start))
        )
    sections = tuple(
        PdfReportSection(title, actual_toc_pages + page_index + 1)
        for title, page_index in laid_out
    )
    return doc.render(), sections, len(doc.pages)


def build_pdf_report(
    root: str | Path,
    evidence: EvidenceRegistry | None = None,
    key_claims: list[str] | tuple[str, ...] = (),
    *,
    generated_at: str,
    out_dir: str | Path | None = None,
) -> PdfReport:
    """Render the final reproduction report PDF from the project state.

    Args:
        root: workspace root (initialized project state).
        evidence: the claim-specific evidence registry (optional; the
            audit package and the evidence trail read it).
        key_claims: claim ids to trace (optional; mirrored into the
            audit package).
        generated_at: the generation timestamp, **injected** -- the
            renderer never consults the wall clock, so identical state
            renders byte-identical output.
        out_dir: optional directory to write ``reproduction-report.pdf``
            and ``reproduction-report.json`` (the JSON sidecar carries
            the PDF's SHA-256 so the audit package can register the
            files).

    Raises:
        TypeError: a boundary argument has the wrong type.
        PdfReportNotInitializedError: the workspace has no project state.
        PdfReportCorruptError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(
            f"root must be a workspace path, got {type(root).__name__}"
        )
    if evidence is not None and not isinstance(evidence, EvidenceRegistry):
        raise TypeError(
            f"evidence must be an EvidenceRegistry, got {type(evidence).__name__}"
        )
    if not isinstance(key_claims, (list, tuple)) or not all(
        isinstance(claim, str) for claim in key_claims
    ):
        raise TypeError("key_claims must be a sequence of claim id strings")
    if not isinstance(generated_at, str):
        raise TypeError(
            "generated_at must be a string (the injected generation "
            f"timestamp), got {type(generated_at).__name__}"
        )
    root_path = Path(root)
    evidence = evidence or EvidenceRegistry()

    try:
        summary = build_summary(root_path)
        package = build_audit_package(root_path, evidence, key_claims)
        project = read_project_state(root_path)
    except (SummaryNotInitializedError, AuditNotInitializedError) as exc:
        raise PdfReportNotInitializedError(str(exc)) from exc
    except (SummaryCorruptError, AuditCorruptError, ValueError) as exc:
        raise PdfReportCorruptError(
            f"corrupt project state while assembling the report: {exc}"
        ) from exc

    goals = sorted(list_goals(root_path), key=lambda goal: goal.goal_id)
    acceptance = sorted(
        list_acceptance(root_path), key=lambda criteria: criteria.acceptance_id
    )
    protocols = sorted(
        list_analysis_protocols(root_path),
        key=lambda protocol: protocol.analysis_id,
    )
    designs = sorted(
        list_statistical_designs(root_path),
        key=lambda design: design.design_id,
    )
    closures = sorted(
        list_closure_contracts(root_path),
        key=lambda closure: closure.closure_id,
    )
    plans = sorted(list_plans(root_path), key=lambda plan: plan.version)
    requirements = sorted(
        list_requirements(root_path),
        key=lambda requirement: requirement.requirement_id,
    )
    inventory = sorted(
        list_inventory_items(root_path),
        key=lambda item: item.inventory_id,
    )
    results = sorted(list_results(root_path), key=lambda result: result.result_id)
    manifests = sorted(
        ArtifactRegistry(root_path / "manifests").list(),
        key=lambda manifest: manifest.artifact_id,
    )
    decisions = _read_decisions(root_path)
    events = _events_in_order(root_path)
    sources = tuple(
        sorted(FilesystemStateBackend(root_path).list_ids("source"))
    )

    try:
        head = current_head(root_path)
        commits = count_commits(root_path)
    except (ValueError, OSError):
        head = None
        commits = None

    pdf_bytes, sections, pages = _render_pdf(
        project=project,
        summary=summary,
        package=package,
        goals=goals,
        acceptance=acceptance,
        protocols=protocols,
        designs=designs,
        closures=closures,
        plans=plans,
        requirements=requirements,
        inventory=inventory,
        results=results,
        manifests=manifests,
        decisions=decisions,
        events=events,
        evidence=evidence,
        sources=sources,
        head=head,
        commits=commits,
        generated_at=generated_at,
    )

    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    report = PdfReport(
        report_version=REPORT_VERSION,
        project_id=summary.project_id,
        primary_target_doi=project.primary_target.doi,
        primary_target_title=project.primary_target.title,
        project_phase=summary.project_phase.value,
        current_plan_version=project.current_plan_version,
        reproduction_outcome=summary.reproduction_outcome.value,
        generated_at=generated_at,
        sections=sections,
        pages=pages,
        pdf_bytes=pdf_bytes,
        pdf_sha256=sha256,
        pdf_size_bytes=len(pdf_bytes),
    )
    if out_dir is not None:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        (out_path / PDF_FILENAME).write_bytes(pdf_bytes)
        (out_path / JSON_FILENAME).write_text(
            report.to_canonical_json(), encoding="utf-8"
        )
    return report
