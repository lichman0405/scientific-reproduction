"""Deterministic designed Plan document renderer (issue #105) -- HTML intermediate.

Renders the frozen Plan record (``plans/v<N>.json``) and its registered
goal-contract family into a self-contained, print-ready HTML document that
converts offline to a designed A4 PDF: cover page (project id, primary
target DOI/title, freeze stamp ``frozen_at``/``frozen_commit``, domain
pack), scope declaration with the inventory coverage summary (ADR 5),
the requirement table (criticality + outcome + checklist refs), the goal
DAG diagram (deterministic SVG, edges carrying the six gate kinds) and
one section per plan goal with its replication design, acceptance
criteria including every margin and its SS8 provenance
(``07-STATISTICS-AND-ACCEPTANCE.md`` SS8 -- no unexplained numbers),
analysis protocol summary and closure contract summary.

Why HTML (and not a direct PDF writer or a LaTeX/Typst pipeline)
----------------------------------------------------------------
The runtime is intentionally stdlib-only (``pyproject.toml`` has no
dependencies) and the render must be byte-identical across repeated runs
on any platform. A self-contained HTML document with embedded print CSS
is the pure intermediate that satisfies both: it is stdlib-serializable
(``html`` + ``json`` only), deterministic by construction, and converts
to a designed PDF offline -- open in a browser and "Print to PDF" (A4),
or ``weasyprint`` / ``chromium --headless --print-to-pdf``, with no
network access at render time. No heavy third-party PDF dependency is
introduced.

Determinism
-----------
The document is a pure function of the registered state at ``root``:
every record is read through the real registration APIs (``planning.plan``
/ ``planning.inventory`` / ``planning.init`` / ``planning.audit`` /
``planning.dag`` -- the same registries the freeze flow writes), every
list is sorted by record id, the DAG layout is a deterministic layered
placement, and the generation timestamp is injected (``generated_at``;
the production default is now-UTC, the deterministic path passes a fixed
value). There is no wall clock, randomness or network anywhere on the
render path.

Design system
-------------
``_PLAN_DOC_CSS`` is the shared visual system of the reporting renderer
family (issues #105 / #106 / #107): self-contained design tokens, A4
``@page`` rules, styled tables, chips and the DAG SVG vocabulary. Sibling
renderers (execution sheet, report) reuse it verbatim so every designed
document of the skill shares one typography and layout language. The
document carries the page footer (project id + plan version) and a table
of contents, and prints on A4 with ``@page`` rules.

Output
------
``write_plan_document`` stores the document under ``reports/`` (the
canonical tree directory of ``templates/PROJECT-TREE.template.txt`` /
``planning.init.INIT_DIRECTORIES``) with a SHA-256 checksum sidecar
(``reports/plan-<version>.html.sha256``, coreutils format), so the
artifact is machine-verifiable.

Error conventions follow ``reporting/report.py``: ``TypeError`` at the
public boundaries, ``PlanDocNotInitializedError`` when no project exists
at ``root``, ``PlanDocCorruptError`` (stable message) for corrupt
registered records, and ``PlanNotFoundError`` / ``InvalidPlanVersionError``
propagated unchanged from ``planning.plan`` (the requested plan version is
a renderer input, not registered state).
"""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scientific_reproduction.core.atomic import atomic_write
from scientific_reproduction.core.models import (
    AcceptanceCriteria,
    AnalysisProtocolOrResult,
    ClosureContract,
    Criticality,
    DependencyType,
    GoalContract,
    Plan,
    Project,
    ReproductionRequirement,
    StatisticalDesign,
)
from scientific_reproduction.planning.audit import (
    CompletenessAudit,
    audit_inventory_registry,
)
from scientific_reproduction.planning.dag import (
    DAGEdge,
    PlanningDAG,
    UnresolvedDependencyRef,
    build_plan_dag,
)
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.inventory import load_inventory_registry
from scientific_reproduction.planning.plan import (
    InvalidPlanVersionError,
    PlanNotFoundError,
    list_acceptance,
    list_analysis_protocols,
    list_closure_contracts,
    list_goals,
    list_statistical_designs,
    read_plan,
)

__all__ = [
    "PLAN_DOC_VERSION",
    "PlanDocCorruptError",
    "PlanDocError",
    "PlanDocNotInitializedError",
    "PlanDocSection",
    "PlanDocument",
    "PlanDocumentResult",
    "render_plan_document",
    "write_plan_document",
]

#: Version of the plan document serialization (``version`` key of
#: :class:`PlanDocument`). Bumped whenever the document schema changes.
PLAN_DOC_VERSION: str = "1.0"

#: Section titles in fixed document order (the goals follow, sorted).
_SECTION_TITLES: tuple[str, ...] = (
    "Scope declaration",
    "Requirements",
    "Goal DAG",
)

#: The shared renderer-family design system (issues #105/#106/#107):
#: self-contained print CSS over A4 with design tokens. Appended per
#: document: the ``@page`` footer rule carrying the project id and plan
#: version (``_footer_css``).
_PLAN_DOC_CSS: str = """\
:root {
  --ink: #111827;
  --muted: #6b7280;
  --rule: #d1d5db;
  --paper: #ffffff;
  --panel: #f9fafb;
  --accent: #1d4ed8;
  --accent-soft: #eff6ff;
  --hard: #111827;
  --soft: #4b5563;
  --info: #9ca3af;
  --pass: #065f46;
  --pass-soft: #ecfdf5;
  --fail: #991b1b;
  --fail-soft: #fef2f2;
  --serif: Georgia, "Times New Roman", serif;
  --sans: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --mono: Consolas, Menlo, monospace;
}

@page {
  size: A4;
  margin: 22mm 18mm 20mm 18mm;
  @bottom-right {
    content: counter(page);
    font-family: var(--sans);
    font-size: 8.5pt;
    color: var(--muted);
  }
}

html {
  color: var(--ink);
  font-family: var(--sans);
  font-size: 10pt;
  line-height: 1.45;
}

body {
  margin: 0;
  padding: 0;
}

h1, h2, h3 {
  font-family: var(--serif);
  line-height: 1.2;
}

/* ---- cover page ---- */
.cover {
  page-break-after: always;
  padding-top: 40mm;
}
.cover-kicker {
  font-size: 9pt;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 6pt;
}
.cover-title {
  font-size: 24pt;
  margin: 0 0 6pt 0;
}
.cover-subtitle {
  font-size: 12pt;
  color: var(--muted);
  margin: 0 0 24pt 0;
}
.cover-meta {
  max-width: 150mm;
}

/* ---- table of contents ---- */
.toc {
  page-break-after: always;
}
.toc-title {
  font-size: 16pt;
  border-bottom: 1.5pt solid var(--accent);
  padding-bottom: 4pt;
}
.toc ol {
  padding-left: 16pt;
}
.toc li {
  margin: 3pt 0;
}
.toc a {
  color: var(--ink);
  text-decoration: none;
}
.toc a:hover {
  color: var(--accent);
}

/* ---- sections ---- */
.section {
  page-break-before: always;
}
.section-title {
  font-size: 16pt;
  border-bottom: 1.5pt solid var(--accent);
  padding-bottom: 4pt;
  margin: 0 0 10pt 0;
}
.section-number {
  color: var(--accent);
  margin-right: 6pt;
}

/* ---- tables ---- */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 9pt;
  margin: 6pt 0 12pt 0;
}
th {
  text-align: left;
  background: var(--accent-soft);
  border: 0.5pt solid var(--rule);
  padding: 4pt 6pt;
  font-weight: 600;
}
td {
  border: 0.5pt solid var(--rule);
  padding: 3pt 6pt;
  vertical-align: top;
  word-wrap: break-word;
}
.kv-table td:first-child {
  width: 30%;
  font-weight: 600;
  background: var(--panel);
}
.muted {
  color: var(--muted);
}
.mono {
  font-family: var(--mono);
  font-size: 8.5pt;
}

/* ---- chips ---- */
.chip {
  display: inline-block;
  padding: 0.5pt 6pt;
  border-radius: 8pt;
  border: 0.5pt solid var(--rule);
  font-size: 8pt;
  margin: 0 3pt 2pt 0;
  white-space: nowrap;
}
.chip-critical { background: var(--fail-soft); color: var(--fail); }
.chip-required { background: var(--accent-soft); color: var(--accent); }
.chip-supporting { background: var(--panel); }
.chip-pass { background: var(--pass-soft); color: var(--pass); }
.chip-fail { background: var(--fail-soft); color: var(--fail); }
.chip-hard { background: var(--paper); color: var(--hard); border-color: var(--hard); }
.chip-soft { background: var(--paper); color: var(--soft); border-color: var(--soft); }
.chip-info { background: var(--paper); color: var(--info); border-color: var(--info); }

/* ---- goal sections ---- */
.goal-head {
  display: flex;
  align-items: baseline;
  gap: 8pt;
  flex-wrap: wrap;
}
.goal-head h2 {
  font-size: 13pt;
  margin: 0;
}
.subhead {
  font-size: 10.5pt;
  font-weight: 600;
  color: var(--accent);
  margin: 12pt 0 4pt 0;
  font-family: var(--serif);
}
.note {
  border-left: 2.5pt solid var(--accent);
  background: var(--accent-soft);
  padding: 5pt 8pt;
  font-size: 9pt;
  margin: 6pt 0;
}
.note-warn {
  border-left-color: var(--fail);
  background: var(--fail-soft);
}

/* ---- DAG ---- */
.dag-figure {
  text-align: center;
  margin: 8pt 0;
  overflow-x: auto;
}
.dag-legend {
  font-size: 9pt;
  margin: 6pt 0;
}
.dag-id {
  font-family: var(--mono);
  font-size: 9pt;
  font-weight: 700;
}
.dag-title {
  font-size: 8pt;
  fill: var(--muted);
}
.dag-node {
  fill: var(--paper);
  stroke: var(--ink);
  stroke-width: 1;
}
.dag-node-out {
  stroke-dasharray: "4 3";
  fill: var(--panel);
}
.dag-edge-label {
  font-family: var(--mono);
  font-size: 7pt;
}

/* ---- JSON blocks ---- */
.json-block {
  font-family: var(--mono);
  font-size: 8pt;
  background: var(--panel);
  border: 0.5pt solid var(--rule);
  padding: 6pt 8pt;
  white-space: pre-wrap;
  margin: 4pt 0 10pt 0;
}
"""

#: DAG SVG geometry (deterministic layered layout constants).
_DAG_NODE_W: int = 168
_DAG_NODE_H: int = 46
_DAG_H_GAP: int = 48
_DAG_V_GAP: int = 26
_DAG_MARGIN: int = 16
_DAG_TITLE_MAX: int = 28

#: Dependency strength -> (stroke color, extra line attributes). The
#: fixed palette of the DAG vocabulary: solid = hard gate, dashed = soft
#: dependency, dotted = informational.
_STRENGTH_STYLE: dict[DependencyType, tuple[str, str]] = {
    DependencyType.HARD_GATE: ("#111827", ""),
    DependencyType.SOFT_DEPENDENCY: ("#4b5563", ' stroke-dasharray="7 4"'),
    DependencyType.INFORMATIONAL: ("#9ca3af", ' stroke-dasharray="2 4"'),
}

#: Arrow marker fill per strength (must match ``_STRENGTH_STYLE``).
_MARKER_COLORS: dict[str, str] = {
    "hard": "#111827",
    "soft": "#4b5563",
    "info": "#9ca3af",
}

#: Serialization: canonical JSON (indent + sorted keys + trailing newline).
_JSON_INDENT: int = 2

#: Timestamp format: git-style UTC ISO-8601 (``Z``), like
#: ``planning.init``.
_ISO_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlanDocError(ValueError):
    """Base class for all plan document renderer errors."""


class PlanDocNotInitializedError(PlanDocError):
    """Raised when the renderer needs an initialized project and none exists."""


class PlanDocCorruptError(PlanDocError):
    """Raised when a stored record the renderer reads is corrupt."""


# ---------------------------------------------------------------------------
# Document records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanDocSection:
    """One section of the plan document: a title and its HTML body."""

    title: str
    body: str

    def to_dict(self) -> dict[str, str]:
        """Plain dict of the section."""
        return {"title": self.title, "body": self.body}


@dataclass(frozen=True)
class PlanDocument:
    """The designed plan document (HTML intermediate of the PDF render).

    Attributes:
        project_id: the registered project id (``project.yaml``).
        title: the document title (project title, else the primary
            target title, else a derived default).
        primary_target: the registered primary target (DOI, else the
            identifier).
        target_title: the registered primary target title (may be
            ``None``).
        domain_pack: the registered domain pack.
        plan_version: the rendered plan version (``v<N>``).
        plan_status: the rendered plan status.
        frozen_at: the plan freeze stamp (``frozen_at``, may be
            ``None``).
        frozen_commit: the plan freeze stamp (``frozen_commit``, may be
            ``None``).
        generated_at: the injected generation timestamp (UTC ISO-8601).
        sections: the document sections in fixed order (scope,
            requirements, goal DAG, then one "Goal <id>" section per
            plan goal, sorted by goal id).
    """

    project_id: str
    title: str
    primary_target: str
    target_title: str | None
    domain_pack: str
    plan_version: str
    plan_status: str
    frozen_at: str | None
    frozen_commit: str | None
    generated_at: str
    sections: tuple[PlanDocSection, ...]

    def section(self, title: str) -> PlanDocSection | None:
        """The section with ``title``, or None when absent.

        Raises:
            TypeError: ``title`` is not a str.
        """
        if not isinstance(title, str):
            raise TypeError(f"title must be a str, got {type(title).__name__}")
        for section in self.sections:
            if section.title == title:
                return section
        return None

    def to_html(self) -> str:
        """Render the document as deterministic, self-contained HTML."""
        footer = f"Project {self.project_id} · Plan {self.plan_version}"
        parts: list[str] = [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8"/>',
            f"<title>{html.escape(self.title)}</title>",
            f"<style>{_PLAN_DOC_CSS}{_footer_css(footer)}</style>",
            "</head>",
            "<body>",
            self._cover_html(),
            self._toc_html(),
        ]
        for number, section in enumerate(self.sections, start=1):
            parts.append(self._section_html(number, section))
        parts.append("</body>")
        parts.append("</html>")
        return "\n".join(parts) + "\n"

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the document in canonical field order."""
        return {
            "version": PLAN_DOC_VERSION,
            "project_id": self.project_id,
            "title": self.title,
            "primary_target": self.primary_target,
            "target_title": self.target_title,
            "domain_pack": self.domain_pack,
            "plan_version": self.plan_version,
            "plan_status": self.plan_status,
            "frozen_at": self.frozen_at,
            "frozen_commit": self.frozen_commit,
            "generated_at": self.generated_at,
            "sections": [section.to_dict() for section in self.sections],
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON text: sorted keys, 2-space indent, trailing newline."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"

    # ------------------------------------------------------------------
    # Document furniture (deterministic HTML)
    # ------------------------------------------------------------------

    def _cover_html(self) -> str:
        """The cover page: identity, freeze stamp, domain pack."""
        rows: list[tuple[str, str]] = [
            ("Project ID", html.escape(self.project_id)),
            ("Primary target", html.escape(self.primary_target)),
            ("Target title", _maybe(self.target_title)),
            ("Domain pack", html.escape(self.domain_pack)),
            ("Plan version", html.escape(self.plan_version)),
            ("Plan status", html.escape(self.plan_status)),
            ("Frozen at", _maybe(self.frozen_at)),
            ("Frozen commit", _maybe(self.frozen_commit)),
            ("Generated at", html.escape(self.generated_at)),
            ("Renderer version", PLAN_DOC_VERSION),
        ]
        return (
            '<section id="cover" class="cover">\n'
            '<p class="cover-kicker">Scientific Reproduction Skill · '
            "Plan document</p>\n"
            f"<h1 class=\"cover-title\">{html.escape(self.title)}</h1>\n"
            '<p class="cover-subtitle">Reproduction Plan '
            f"{html.escape(self.plan_version)} · {html.escape(self.plan_status)}</p>\n"
            f"{_kv_table(rows, cls='cover-meta')}"
            "</section>"
        )

    def _toc_html(self) -> str:
        """The table of contents: every section in document order."""
        entries: list[str] = []
        for number, section in enumerate(self.sections, start=1):
            anchor = _section_anchor(section.title)
            entries.append(
                f'<li><a href="#{anchor}">{number}. '
                f"{html.escape(section.title)}</a></li>"
            )
        return (
            '<nav class="toc" aria-label="Table of contents">\n'
            '<h1 class="toc-title">Table of contents</h1>\n'
            "<ol>\n"
            + "\n".join(entries)
            + "\n</ol>\n</nav>"
        )

    def _section_html(self, number: int, section: PlanDocSection) -> str:
        """One numbered content section with its body."""
        anchor = _section_anchor(section.title)
        return (
            f'<section id="{anchor}" class="section">\n'
            '<h1 class="section-title">'
            f'<span class="section-number">{number}</span>'
            f"{html.escape(section.title)}</h1>\n"
            f'<div class="section-body">\n{section.body}\n</div>\n'
            "</section>"
        )


@dataclass(frozen=True)
class PlanDocumentResult:
    """The persisted render result of :func:`write_plan_document`.

    ``html_path`` / ``checksum_path`` are the written files under
    ``reports/`` and ``sha256`` the hex digest of the document bytes
    (the checksum sidecar carries the same digest, coreutils format).
    """

    document: PlanDocument
    html_path: Path
    checksum_path: Path
    sha256: str


# ---------------------------------------------------------------------------
# Rendering (pure, deterministic)
# ---------------------------------------------------------------------------


def render_plan_document(
    root: str | Path,
    version: str = "v1",
    *,
    generated_at: datetime | None = None,
) -> PlanDocument:
    """Render the designed plan document from the real registered state.

    Reads every record through the real registration APIs: the project
    state (``planning.init.read_project_state``), the plan record at
    ``version`` (``planning.plan.read_plan``), the goal-contract family
    registries (``list_goals`` / ``list_acceptance`` /
    ``list_analysis_protocols`` / ``list_closure_contracts`` /
    ``list_statistical_designs``), the inventory registry
    (``planning.inventory.load_inventory_registry``), the completeness
    audit recomputed from the registered state
    (``planning.audit.audit_inventory_registry``) and the plan DAG
    (``planning.dag.build_plan_dag``). Every list is sorted by record id,
    so the document is a pure function of the registered state plus the
    injected ``generated_at``.

    Args:
        root: the initialized workspace root.
        version: the plan version to render (``v<N>`` or ``v<N>-draft``;
            default ``v1`` -- the frozen plan of issue #105).
        generated_at: the generation timestamp stamped on the cover;
            defaults to now-UTC (the deterministic path passes a fixed
            value). Naive datetimes are rejected.

    Returns:
        The deterministic :class:`PlanDocument`.

    Raises:
        TypeError: ``root`` is not a str/Path, ``version`` is not a str,
            or ``generated_at`` is not a datetime/None.
        ValueError: ``generated_at`` is naive.
        PlanDocNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        PlanNotFoundError: no plan record with ``version`` is registered
            (propagated unchanged from ``planning.plan``).
        InvalidPlanVersionError: ``version`` is not ``v<N>`` /
            ``v<N>-draft`` (propagated unchanged).
        PlanDocCorruptError: a stored record the document reads is
            corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    if generated_at is not None and not isinstance(generated_at, datetime):
        raise TypeError(
            "generated_at must be a datetime or None, got"
            f" {type(generated_at).__name__}"
        )
    if generated_at is not None and generated_at.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")
    generated = _format_iso(
        generated_at if generated_at is not None else datetime.now(timezone.utc)
    )

    project_root = Path(root).resolve()
    project = _read_project_wrapped(project_root)
    plan = _read_plan_wrapped(project_root, version)
    goals = _read_goals_wrapped(project_root)
    acceptances = _read_acceptances_wrapped(project_root)
    protocols = _read_protocols_wrapped(project_root)
    closures = _read_closures_wrapped(project_root)
    designs = _read_designs_wrapped(project_root)
    requirements = _read_requirements_wrapped(project_root)
    audit = _read_audit_wrapped(project_root)
    dag = _read_dag_wrapped(project_root, version)

    goals_by_id = {goal.goal_id: goal for goal in goals}
    requirements_by_id = {
        requirement.requirement_id: requirement
        for requirement in requirements.values()
    }
    edges_by_pair = {
        (edge.dependency_goal_id, edge.dependent_goal_id): edge
        for edge in dag.edges
    }

    sections: list[PlanDocSection] = [
        PlanDocSection(
            title=_SECTION_TITLES[0],
            body=_scope_section_html(project, plan, audit),
        ),
        PlanDocSection(
            title=_SECTION_TITLES[1],
            body=_requirements_section_html(plan, requirements_by_id),
        ),
        PlanDocSection(
            title=_SECTION_TITLES[2],
            body=_dag_section_html(dag, goals_by_id),
        ),
    ]
    for goal_id in sorted(plan.goal_ids):
        goal = goals_by_id.get(goal_id)
        if goal is None:
            # Missing contracts are surfaced in the DAG section
            # (``missing_goal_contracts``); never render a fabricated goal.
            continue
        sections.append(
            PlanDocSection(
                title=f"Goal {goal.goal_id}",
                body=_goal_section_html(
                    goal,
                    acceptances,
                    protocols,
                    closures,
                    designs,
                    edges_by_pair,
                    dag.unresolved_dependency_refs,
                ),
            )
        )

    return PlanDocument(
        project_id=project.project_id,
        title=_document_title(project),
        primary_target=project.primary_target.doi
        or project.primary_target.identifier,
        target_title=project.primary_target.title,
        domain_pack=_maybe(project.domain_pack),
        plan_version=plan.version,
        plan_status=plan.status.value,
        frozen_at=plan.frozen_at,
        frozen_commit=plan.frozen_commit,
        generated_at=generated,
        sections=tuple(sections),
    )


def write_plan_document(
    root: str | Path,
    version: str = "v1",
    *,
    generated_at: datetime | None = None,
    out_dir: str | Path | None = None,
) -> PlanDocumentResult:
    """Render and persist the plan document under ``reports/``.

    Writes ``plan-<version>.html`` and its SHA-256 checksum sidecar
    ``plan-<version>.html.sha256`` (coreutils format:
    ``<hexdigest>  <filename>``) into the workspace ``reports/`` tree
    directory by default (``out_dir`` overrides), using atomic writes.
    The checksum lets the artifact be machine-verified against the
    document bytes.

    Args:
        root: the initialized workspace root.
        version: the plan version to render.
        generated_at: the injected generation timestamp (see
            :func:`render_plan_document`).
        out_dir: output directory; defaults to ``<root>/reports``.

    Returns:
        The :class:`PlanDocumentResult` with the document and the paths
        of the written files.

    Raises:
        TypeError: any argument has the wrong type.
        ValueError: ``generated_at`` is naive.
        PlanDocNotInitializedError: no ``project.yaml`` exists at
            ``root``.
        PlanNotFoundError / InvalidPlanVersionError: the plan version is
            not registered / malformed (propagated unchanged).
        PlanDocCorruptError: a stored record is corrupt.
    """
    if not isinstance(root, (str, Path)):
        raise TypeError(f"root must be a str or Path, got {type(root).__name__}")
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    if out_dir is not None and not isinstance(out_dir, (str, Path)):
        raise TypeError(
            f"out_dir must be a str or Path or None, got {type(out_dir).__name__}"
        )
    document = render_plan_document(root, version, generated_at=generated_at)
    html_text = document.to_html()
    digest = hashlib.sha256(html_text.encode("utf-8")).hexdigest()

    project_root = Path(root).resolve()
    target_dir = (
        project_root / "reports"
        if out_dir is None
        else Path(out_dir).resolve()
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"plan-{document.plan_version}"
    html_path = target_dir / f"{stem}.html"
    checksum_path = target_dir / f"{stem}.html.sha256"
    atomic_write(html_path, html_text)
    atomic_write(checksum_path, f"{digest}  {html_path.name}\n")
    return PlanDocumentResult(
        document=document,
        html_path=html_path,
        checksum_path=checksum_path,
        sha256=digest,
    )


# ---------------------------------------------------------------------------
# Section renderers (deterministic HTML bodies)
# ---------------------------------------------------------------------------


def _scope_section_html(
    project: Project, plan: Plan, audit: CompletenessAudit
) -> str:
    """Scope declaration (ADR 5) + inventory coverage summary + plan meta."""
    audit_view = plan.inventory_audit
    coverage_rows: list[tuple[str, str]] = [
        ("Formally reported items", str(audit_view.formally_reported_items)),
        ("Mapped items", str(audit_view.mapped_items)),
        ("Unmapped items", str(audit_view.unmapped_items)),
        ("Ambiguous items", str(audit_view.ambiguous_items)),
        ("Coverage", f"{audit_view.coverage:.2f}"),
        ("Audit status", audit_view.status.value if audit_view.status else "—"),
    ]
    recomputed = (
        f"Recomputed verdict: {audit.verdict.value}"
        f" (rule {audit.matched_rule_id})"
    )
    plan_rows: list[tuple[str, str]] = [
        ("Plan ID", html.escape(plan.plan_id)),
        ("Plan version", html.escape(plan.version)),
        ("Plan status", html.escape(plan.status.value)),
        ("Parent plan version", _maybe(plan.parent_plan_version)),
        ("Goals in plan", str(len(plan.goal_ids))),
        ("Requirements in plan", str(len(plan.requirement_ids))),
        ("Work packages", str(len(plan.work_packages))),
        ("Resource IDs", _id_list(plan.resource_ids)),
    ]
    return (
        '<p>The reproduction scope (ADR 5, '
        '<code>20-ARCHITECTURE-DECISIONS.md</code>) covers every '
        "formally reported main-text, supplementary-information and "
        "public-data item of the primary target paper; non-formal items "
        "carry no coverage obligation. The plan is the frozen "
        "declaration of that scope and its goal structure.</p>\n"
        '<p class="subhead">Inventory coverage summary</p>\n'
        f"{_kv_table(coverage_rows)}\n"
        f'<p class="note">{html.escape(recomputed)}</p>\n'
        '<p class="subhead">Plan record</p>\n'
        f"{_kv_table(plan_rows)}"
    )


def _requirements_section_html(
    plan: Plan, requirements: dict[str, ReproductionRequirement]
) -> str:
    """The requirement table: criticality, outcome and checklist refs."""
    rows: list[str] = []
    for requirement_id in sorted(plan.requirement_ids):
        requirement = requirements.get(requirement_id)
        if requirement is None:
            rows.append(
                "<tr>"
                f"<td class=\"mono\">{html.escape(requirement_id)}</td>"
                '<td colspan="3"><span class="muted">no registered '
                "requirement record</span></td>"
                "</tr>"
            )
            continue
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{html.escape(requirement.requirement_id)}</td>"
            f"<td>{html.escape(requirement.statement)}</td>"
            f"<td>{_criticality_chip(requirement.criticality)}</td>"
            f"<td>{html.escape(requirement.outcome.value)}</td>"
            f"<td>{_id_list(requirement.inventory_items)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            '<tr><td colspan="5" class="muted">no requirements are '
            "registered for this plan</td></tr>"
        )
    header = (
        "<thead><tr>"
        "<th>Requirement</th>"
        "<th>Statement</th>"
        "<th>Criticality</th>"
        "<th>Outcome</th>"
        "<th>Checklist refs</th>"
        "</tr></thead>"
    )
    return (
        "<p>Every requirement of the frozen plan with its criticality "
        "(CRITICAL / REQUIRED / SUPPORTING), outcome and the reproduction "
        "checklist items it maps to.</p>\n"
        f"<table>{header}<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody></table>"
    )


def _dag_section_html(
    dag: PlanningDAG, goals_by_id: dict[str, GoalContract]
) -> str:
    """The goal DAG: deterministic SVG diagram + legend + edge table."""
    titles = {goal_id: goal.title for goal_id, goal in goals_by_id.items()}
    parts: list[str] = [
        "<p>Nodes are the plan's goals (plus every registered goal "
        "reachable through dependency edges); edges render dependency-"
        "first with their six-kind gate (strength × execution/acceptance "
        "axis, "
        "<code>planning/dag.py</code>).</p>",
        '<p class="subhead">Goal DAG</p>',
        f'<div class="dag-figure">{_dag_svg(dag, titles)}</div>',
        '<p class="dag-legend">Legend: '
        '<span class="chip chip-hard">hard_gate</span>'
        '<span class="chip chip-soft">soft_dependency</span>'
        '<span class="chip chip-info">informational</span>'
        "solid = hard gate · dashed = soft dependency · dotted = "
        "informational; edge labels carry the gate kind "
        "(e.g. <code>hard_execution</code>).</p>",
    ]
    if not dag.acyclic:
        parts.append(
            '<p class="note note-warn">Cycle detected in the dependency '
            f"graph: {html.escape(', '.join(dag.cyclic_goal_ids))}; the "
            "topological order is empty and the layered layout falls back "
            "to id order.</p>"
        )
    if dag.missing_goal_contracts:
        parts.append(
            '<p class="note note-warn">Plan goal ids without a registered '
            "goal contract (never silently dropped): "
            f"{html.escape(', '.join(dag.missing_goal_contracts))}</p>"
        )
    if dag.unresolved_dependency_refs:
        refs = [
            f"{ref.dependent_goal_id} → {ref.dependency_goal_id}"
            for ref in dag.unresolved_dependency_refs
        ]
        parts.append(
            '<p class="note note-warn">Dependency targets without a '
            "registered goal contract (edges that cannot render): "
            f"{html.escape(', '.join(refs))}</p>"
        )
    edge_rows: list[str] = []
    for edge in dag.edges:
        edge_rows.append(
            "<tr>"
            f"<td class=\"mono\">{html.escape(edge.dependency_goal_id)}</td>"
            f"<td class=\"mono\">{html.escape(edge.dependent_goal_id)}</td>"
            f"<td>{html.escape(edge.dependency_type.value)}</td>"
            f"<td>{_yes_no(edge.execution_gate)}</td>"
            f"<td>{_yes_no(edge.acceptance_gate)}</td>"
            f"<td>{html.escape(edge.gate_kind.value)}</td>"
            "</tr>"
        )
    if not edge_rows:
        edge_rows.append(
            '<tr><td colspan="6" class="muted">no dependency edges '
            "registered</td></tr>"
        )
    header = (
        "<thead><tr>"
        "<th>Dependency</th>"
        "<th>Dependent</th>"
        "<th>Type</th>"
        "<th>Execution gate</th>"
        "<th>Acceptance gate</th>"
        "<th>Gate kind</th>"
        "</tr></thead>"
    )
    parts.extend(
        (
            '<p class="subhead">Dependency edges</p>',
            f"<table>{header}<tbody>\n"
            + "\n".join(edge_rows)
            + "\n</tbody></table>",
        )
    )
    return "\n".join(parts)


def _goal_section_html(
    goal: GoalContract,
    acceptances: dict[str, AcceptanceCriteria],
    protocols: dict[str, AnalysisProtocolOrResult],
    closures: dict[str, ClosureContract],
    designs: dict[str, StatisticalDesign],
    edges_by_pair: dict[tuple[str, str], DAGEdge],
    unresolved: tuple[UnresolvedDependencyRef, ...],
) -> str:
    """One per-goal section: design, acceptance with SS8 provenance,
    analysis protocol summary, closure contract summary."""
    criteria = acceptances.get(goal.acceptance.criteria_ref)
    design: StatisticalDesign | None = None
    design_missing = False
    if criteria is not None and criteria.statistical_design_ref is not None:
        design = designs.get(criteria.statistical_design_ref)
        design_missing = design is None
    protocol = protocols.get(goal.analysis_protocol_ref)
    closure = (
        closures.get(goal.closure_contract_ref)
        if goal.closure_contract_ref is not None
        else None
    )

    head = (
        '<div class="goal-head">'
        f"<h2>{html.escape(goal.goal_id)} — {html.escape(goal.title)}</h2>"
        f"<span class=\"chip\">{html.escape(goal.track.value)}</span>"
        f"<span class=\"chip\">{'frozen' if goal.frozen else 'draft'}</span>"
        "</div>"
    )
    meta_rows: list[tuple[str, str]] = [
        ("Unit process type", html.escape(goal.unit_process_type)),
        ("Track", html.escape(goal.track.value)),
        ("Objective", html.escape(goal.objective)),
        ("Goal version", html.escape(goal.version)),
        ("Frozen", _yes_no(goal.frozen)),
        ("Frozen at", _maybe(goal.frozen_at)),
        ("Frozen commit", _maybe(goal.frozen_commit)),
        ("Requirement IDs", _id_list(goal.requirement_ids)),
        ("Analysis protocol ref", _maybe_ref(goal.analysis_protocol_ref)),
        ("Closure contract ref", _maybe_ref(goal.closure_contract_ref)),
    ]
    if goal.resource_ids:
        meta_rows.append(("Resource IDs", _id_list(goal.resource_ids)))

    dependency_rows: list[str] = []
    for dependency in goal.dependencies:
        edge = edges_by_pair.get((dependency.goal_id, goal.goal_id))
        kind = edge.gate_kind.value if edge is not None else "—"
        strength = dependency.type.value
        dependency_rows.append(
            "<tr>"
            f"<td class=\"mono\">{html.escape(dependency.goal_id)}</td>"
            f"<td>{html.escape(strength)}</td>"
            f"<td>{_yes_no(dependency.execution_gate)}</td>"
            f"<td>{_yes_no(dependency.acceptance_gate)}</td>"
            f"<td>{html.escape(kind)}</td>"
            "</tr>"
        )
    for ref in unresolved:
        if ref.dependent_goal_id == goal.goal_id:
            dependency_rows.append(
                "<tr>"
                f"<td class=\"mono\">{html.escape(ref.dependency_goal_id)}</td>"
                '<td colspan="4"><span class="muted">no registered goal '
                "contract</span></td>"
                "</tr>"
            )
    if not dependency_rows:
        dependency_rows.append(
            '<tr><td colspan="5" class="muted">no dependencies '
            "declared</td></tr>"
        )
    dep_header = (
        "<thead><tr>"
        "<th>Dependency</th>"
        "<th>Type</th>"
        "<th>Execution gate</th>"
        "<th>Acceptance gate</th>"
        "<th>Gate kind</th>"
        "</tr></thead>"
    )

    replication_rows: list[tuple[str, str]] = [
        ("Independent required", _yes_no(goal.replication.independent_required)),
        ("Planned n policy", html.escape(goal.replication.planned_n_policy)),
        ("Minimum n", _maybe_int(goal.replication.minimum_n)),
        ("Technical repeats", _maybe_int(goal.replication.technical_repeats)),
    ]

    acceptance_parts = _acceptance_html(
        criteria,
        goal.acceptance.criteria_ref,
        design,
        design_missing,
    )
    protocol_parts = _protocol_html(protocol, goal.analysis_protocol_ref)
    closure_parts = _closure_html(closure, goal.closure_contract_ref)

    return (
        head
        + f"{_kv_table(meta_rows)}"
        + '<p class="subhead">Dependencies</p>\n'
        + f"<table>{dep_header}<tbody>\n"
        + "\n".join(dependency_rows)
        + "\n</tbody></table>"
        + '<p class="subhead">Replication design</p>\n'
        + _kv_table(replication_rows)
        + '<p class="subhead">Acceptance criteria</p>\n'
        + "\n".join(acceptance_parts)
        + '<p class="subhead">Analysis protocol summary</p>\n'
        + "\n".join(protocol_parts)
        + '<p class="subhead">Closure contract summary</p>\n'
        + "\n".join(closure_parts)
    )


def _acceptance_html(
    criteria: AcceptanceCriteria | None,
    criteria_ref: str,
    design: StatisticalDesign | None,
    design_missing: bool,
) -> list[str]:
    """Acceptance criteria with every margin and its SS8 provenance.

    ``07-STATISTICS-AND-ACCEPTANCE.md`` SS8: no unexplained numbers --
    every numeric margin renders with its basis, and a missing basis is
    surfaced explicitly (never invented).
    """
    if criteria is None:
        return [
            '<p class="note note-warn">No registered acceptance criteria '
            f"for criteria ref {html.escape(criteria_ref)} (the goal "
            "contract's acceptance points at an unregistered record).</p>"
        ]
    basis_label = _basis_label(design, design_missing)
    margin_rows: list[str] = []
    for index, criterion in enumerate(criteria.criteria, start=1):
        for key, value in criterion.items():
            is_margin = isinstance(value, (int, float)) and not isinstance(
                value, bool
            )
            note = (
                '<span class="chip chip-required">margin</span>' if is_margin else ""
            )
            margin_rows.append(
                "<tr>"
                f"<td class=\"mono\">criteria[{index}].{html.escape(key)}</td>"
                f"<td>{note}{html.escape(_format_value(value))}</td>"
                f"<td>{basis_label}</td>"
                "</tr>"
            )
    if not margin_rows:
        margin_rows.append(
            '<tr><td colspan="3" class="muted">no criteria entries '
            "registered</td></tr>"
        )
    header = (
        "<thead><tr>"
        "<th>Margin / parameter</th>"
        "<th>Value</th>"
        "<th>Provenance (07 SS8)</th>"
        "</tr></thead>"
    )
    decision_table = _kv_table(
        [
            ("Decision mode", html.escape(criteria.decision_mode.value)),
            (
                "Confidence",
                _maybe(
                    criteria.confidence.value
                    if criteria.confidence is not None
                    else None
                ),
            ),
            (
                "Target",
                html.escape(_format_value(criteria.target))
                if criteria.target is not None
                else "—",
            ),
            ("Rationale", _maybe(criteria.rationale)),
            ("Evidence refs", _id_list(criteria.evidence_refs)),
            ("Statistical design ref", _maybe_ref(criteria.statistical_design_ref)),
        ]
    )
    parts: list[str] = [
        decision_table,
        f"<table>{header}<tbody>\n"
        + "\n".join(margin_rows)
        + "\n</tbody></table>",
    ]
    if criteria.statistical_design_ref is not None and design is not None:
        parts.append(_design_block(design))
    elif design_missing:
        parts.append(
            '<p class="note note-warn">Statistical design ref '
            f"{html.escape(criteria.statistical_design_ref or '')} has no "
            "registered statistical design record; the SS8 margin "
            "provenance cannot be resolved.</p>"
        )
    else:
        parts.append(
            '<p class="note">No statistical design record: the criteria '
            "margins carry no SS8 basis on record ("
            "<code>07-STATISTICS-AND-ACCEPTANCE.md</code> SS8).</p>"
        )
    return parts


def _design_block(design: StatisticalDesign) -> str:
    """The statistical design block: margin + basis + rules (SS8/SS9)."""
    rows: list[tuple[str, str]] = [
        ("Design ID", html.escape(design.design_id)),
        ("Goal ID", html.escape(design.goal_id)),
        ("Version", html.escape(design.version)),
        ("Frozen", _yes_no(design.frozen)),
        ("Metrics", _id_list(design.metrics)),
        ("Primary method", html.escape(design.primary_method)),
        ("Margin", html.escape(_format_value(design.margin))),
        (
            "Margin basis (07 SS8)",
            _maybe(
                design.margin_basis.value if design.margin_basis else None
            ),
        ),
        ("Alpha", _maybe_float(design.alpha)),
        ("Confidence level", _maybe_float(design.confidence_level)),
        ("Failed-run handling", _maybe(design.failed_run_handling)),
        ("Rationale", _maybe(design.rationale)),
        ("Evidence refs", _id_list(design.evidence_refs)),
    ]
    parts: list[str] = [
        "<p>The registered statistical design ("
        "<code>07-STATISTICS-AND-ACCEPTANCE.md</code> SS9: the design is "
        "a first-class record frozen before data).</p>",
        _kv_table(rows),
    ]
    if design.preprocessing_exclusion_rules:
        parts.append(
            "<p>Preprocessing / exclusion rules:</p>\n"
            f"{_json_block(design.preprocessing_exclusion_rules)}"
        )
    if design.outlier_rules:
        parts.append(
            "<p>Outlier rules:</p>\n"
            f"{_json_block(design.outlier_rules)}"
        )
    return "\n".join(parts)


def _protocol_html(
    protocol: AnalysisProtocolOrResult | None, protocol_ref: str
) -> list[str]:
    """The analysis protocol summary of the goal."""
    if protocol is None:
        return [
            '<p class="note note-warn">No registered analysis protocol '
            f"for the goal's analysis protocol ref "
            f"{html.escape(protocol_ref)}.</p>"
        ]
    rows: list[tuple[str, str]] = [
        ("Analysis ID", html.escape(protocol.analysis_id)),
        ("Kind", html.escape(protocol.kind.value)),
        ("Profile", _maybe(protocol.profile.value if protocol.profile else None)),
        (
            "Primary / exploratory",
            html.escape(protocol.primary_or_exploratory.value),
        ),
        ("Protocol version", html.escape(protocol.protocol_version)),
        ("Frozen", _yes_no(protocol.frozen)),
        ("Methods", _json_block(protocol.methods)),
        ("Uncertainty", _json_block(protocol.uncertainty)),
        ("Warnings", _json_block(protocol.warnings)),
    ]
    return [_kv_table(rows)]


def _closure_html(
    closure: ClosureContract | None, closure_ref: str | None
) -> list[str]:
    """The closure contract summary of the goal."""
    if closure is None and closure_ref is not None:
        return [
            '<p class="note note-warn">No registered closure contract for '
            f"the goal's closure contract ref "
            f"{html.escape(closure_ref)}.</p>"
        ]
    if closure is None:
        return [
            "<p>No closure contract declared for this goal.</p>"
        ]
    rows: list[tuple[str, str]] = [
        ("Closure ID", html.escape(closure.closure_id)),
        ("Frozen", _yes_no(closure.frozen)),
        ("Closure allowed", _yes_no(closure.closure_allowed)),
        (
            "Statistical sufficiency",
            _json_block(closure.statistical_sufficiency),
        ),
        ("Execution validity", _json_block(closure.execution_validity)),
        ("Diagnosis", _json_block(closure.diagnosis)),
        (
            "Recovery",
            _json_block(closure.recovery.to_dict()),
        ),
        ("Literature", _json_block(closure.literature.to_dict())),
    ]
    return [_kv_table(rows)]


# ---------------------------------------------------------------------------
# DAG SVG (deterministic layered layout)
# ---------------------------------------------------------------------------


def _dag_svg(dag: PlanningDAG, titles: dict[str, str]) -> str:
    """The deterministic SVG diagram of the plan DAG.

    Layering is the longest-path placement over the sorted edges (a
    dependency's level is one below its dependents'); nodes within a
    level are ordered by goal id, so the same DAG always produces the
    same coordinates. Edge line style encodes the dependency strength
    (solid / dashed / dotted) and the edge label carries the six-kind
    gate. For cyclic graphs the level computation still converges to a
    deterministic fixed point and the cycle note is rendered separately.
    """
    node_ids = [node.goal.goal_id for node in dag.nodes]
    levels = _dag_levels(dag.edges, node_ids)
    by_level: dict[int, list[str]] = {}
    for gid in node_ids:
        by_level.setdefault(levels[gid], []).append(gid)
    positions: dict[str, tuple[int, int]] = {}
    for level in sorted(by_level):
        for index, gid in enumerate(sorted(by_level[level])):
            x = _DAG_MARGIN + level * (_DAG_NODE_W + _DAG_H_GAP)
            y = _DAG_MARGIN + index * (_DAG_NODE_H + _DAG_V_GAP)
            positions[gid] = (x, y)

    max_level = max(levels.values()) if levels else 0
    max_nodes = max((len(v) for v in by_level.values()), default=1)
    width = (
        _DAG_MARGIN * 2
        + (max_level + 1) * _DAG_NODE_W
        + max_level * _DAG_H_GAP
    )
    height = (
        _DAG_MARGIN * 2
        + max_nodes * _DAG_NODE_H
        + (max_nodes - 1) * _DAG_V_GAP
    )

    edge_parts: list[str] = []
    for edge in dag.edges:
        if (
            edge.dependency_goal_id not in positions
            or edge.dependent_goal_id not in positions
        ):
            continue
        x1, y1 = positions[edge.dependency_goal_id]
        x2, y2 = positions[edge.dependent_goal_id]
        cx1 = x1 + _DAG_NODE_W / 2
        cy1 = y1 + _DAG_NODE_H
        cx2 = x2 + _DAG_NODE_W / 2
        cy2 = y2
        color, dash = _STRENGTH_STYLE[edge.dependency_type]
        marker = _marker_id(edge.dependency_type)
        edge_parts.append(
            f'<line x1="{cx1:.1f}" y1="{cy1:.1f}" x2="{cx2:.1f}" '
            f'y2="{cy2:.1f}" stroke="{color}"{dash} '
            f'marker-end="url(#{marker})"/>'
        )
        label_x = (cx1 + cx2) / 2 + 6
        label_y = (cy1 + cy2) / 2 - 5
        edge_parts.append(
            f'<text x="{label_x:.1f}" y="{label_y:.1f}" '
            f'class="dag-edge-label" fill="{color}">'
            f"{html.escape(edge.gate_kind.value)}</text>"
        )

    node_parts: list[str] = []
    for node in dag.nodes:
        gid = node.goal.goal_id
        x, y = positions[gid]
        title = titles.get(gid, "")
        display = (
            title
            if len(title) <= _DAG_TITLE_MAX
            else title[: _DAG_TITLE_MAX - 1] + "…"
        )
        cls = "dag-node" if node.in_plan else "dag-node dag-node-out"
        node_parts.append(
            f'<rect x="{x}" y="{y}" width="{_DAG_NODE_W}" '
            f'height="{_DAG_NODE_H}" rx="4" class="{cls}"/>'
        )
        node_parts.append(
            f'<text x="{x + _DAG_NODE_W / 2:.1f}" y="{y + 17}" '
            f'text-anchor="middle" class="dag-id">'
            f"{html.escape(gid)}</text>"
        )
        node_parts.append(
            f'<text x="{x + _DAG_NODE_W / 2:.1f}" y="{y + 33}" '
            f'text-anchor="middle" class="dag-title">'
            f"{html.escape(display)}</text>"
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" '
        'role="img" aria-label="Plan goal DAG">\n'
        f"{_dag_markers()}\n"
        + "\n".join(edge_parts)
        + "\n"
        + "\n".join(node_parts)
        + "\n</svg>"
    )


def _dag_levels(
    edges: tuple[DAGEdge, ...], node_ids: list[str]
) -> dict[str, int]:
    """Deterministic longest-path layering over the sorted edges.

    ``level[dependent] = max(level[dependency] + 1)`` iterated to a
    fixed point; the iteration terminates because every level is bounded
    by the node count, and it is deterministic because the edges are
    already sorted. A dependency with no incoming edge sits at level 0.
    """
    levels = {gid: 0 for gid in node_ids}
    changed = True
    while changed:
        changed = False
        for edge in edges:
            dependency = edge.dependency_goal_id
            dependent = edge.dependent_goal_id
            if dependency not in levels or dependent not in levels:
                continue
            target = levels[dependency] + 1
            if target > levels[dependent]:
                levels[dependent] = target
                changed = True
    return levels


def _dag_markers() -> str:
    """The arrowhead markers of the DAG vocabulary (one per strength)."""
    parts = ["<defs>"]
    for kind, color in _MARKER_COLORS.items():
        parts.append(
            f'<marker id="arrow-{kind}" viewBox="0 0 10 10" refX="9" '
            'refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/>'
            "</marker>"
        )
    parts.append("</defs>")
    return "\n".join(parts)


def _marker_id(dependency_type: DependencyType) -> str:
    """The marker id for one dependency strength."""
    return {
        DependencyType.HARD_GATE: "arrow-hard",
        DependencyType.SOFT_DEPENDENCY: "arrow-soft",
        DependencyType.INFORMATIONAL: "arrow-info",
    }[dependency_type]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _wrap_corrupt(exc: ValueError | TypeError) -> PlanDocCorruptError:
    """Re-raise a stored-record corruption as ``PlanDocCorruptError``."""
    return PlanDocCorruptError(f"corrupt registered state: {exc}")


def _read_project_wrapped(root: Path) -> Project:
    """Read the project state record (real API, wrapped)."""
    try:
        return read_project_state(root)
    except ProjectNotInitializedError as exc:
        raise PlanDocNotInitializedError(str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise _wrap_corrupt(exc) from exc


def _read_plan_wrapped(root: Path, version: str) -> Plan:
    """Read the plan record (real API, wrapped).

    ``PlanNotFoundError`` / ``InvalidPlanVersionError`` are input errors
    (the requested version is a renderer argument) and propagate
    unchanged; corrupt records become ``PlanDocCorruptError``.
    """
    try:
        return read_plan(root, version)
    except ProjectNotInitializedError as exc:
        raise PlanDocNotInitializedError(str(exc)) from exc
    except (PlanNotFoundError, InvalidPlanVersionError):
        raise
    except (TypeError, ValueError) as exc:
        raise _wrap_corrupt(exc) from exc


def _read_goals_wrapped(root: Path) -> tuple[GoalContract, ...]:
    """List the registered goal contracts (real API, wrapped, sorted)."""
    try:
        return tuple(sorted(list_goals(root), key=lambda g: g.goal_id))
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc


def _read_acceptances_wrapped(
    root: Path,
) -> dict[str, AcceptanceCriteria]:
    """Index the registered acceptance criteria by id (wrapped, sorted)."""
    try:
        records = tuple(sorted(list_acceptance(root), key=lambda a: a.acceptance_id))
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc
    return {record.acceptance_id: record for record in records}


def _read_protocols_wrapped(
    root: Path,
) -> dict[str, AnalysisProtocolOrResult]:
    """Index the registered analysis protocols by id (wrapped, sorted)."""
    try:
        records = tuple(
            sorted(
                list_analysis_protocols(root), key=lambda a: a.analysis_id
            )
        )
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc
    return {record.analysis_id: record for record in records}


def _read_closures_wrapped(root: Path) -> dict[str, ClosureContract]:
    """Index the registered closure contracts by id (wrapped, sorted)."""
    try:
        records = tuple(
            sorted(list_closure_contracts(root), key=lambda c: c.closure_id)
        )
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc
    return {record.closure_id: record for record in records}


def _read_designs_wrapped(root: Path) -> dict[str, StatisticalDesign]:
    """Index the registered statistical designs by id (wrapped, sorted)."""
    try:
        records = tuple(
            sorted(list_statistical_designs(root), key=lambda d: d.design_id)
        )
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc
    return {record.design_id: record for record in records}


def _read_requirements_wrapped(
    root: Path,
) -> dict[str, ReproductionRequirement]:
    """Index the registered requirements by id (wrapped, sorted)."""
    try:
        registry = load_inventory_registry(root)
        records = tuple(
            sorted(
                registry.requirements,
                key=lambda r: r.requirement_id,
            )
        )
    except ProjectNotInitializedError as exc:
        raise PlanDocNotInitializedError(str(exc)) from exc
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc
    return {record.requirement_id: record for record in records}


def _read_audit_wrapped(root: Path) -> CompletenessAudit:
    """Recompute the completeness audit from the registered state."""
    try:
        return audit_inventory_registry(root)
    except ProjectNotInitializedError as exc:
        raise PlanDocNotInitializedError(str(exc)) from exc
    except ValueError as exc:
        raise _wrap_corrupt(exc) from exc


def _read_dag_wrapped(root: Path, version: str) -> PlanningDAG:
    """Build the plan DAG export from the registered state (wrapped)."""
    try:
        return build_plan_dag(root, version)
    except ProjectNotInitializedError as exc:
        raise PlanDocNotInitializedError(str(exc)) from exc
    except (PlanNotFoundError, InvalidPlanVersionError):
        raise
    except (TypeError, ValueError) as exc:
        raise _wrap_corrupt(exc) from exc


def _document_title(project: Project) -> str:
    """The document title: project title, target title, or a default."""
    if project.title:
        return project.title
    if project.primary_target.title:
        return project.primary_target.title
    return f"Reproduction Plan {project.current_plan_version}"


def _section_anchor(title: str) -> str:
    """The deterministic HTML anchor id of a section title."""
    if title in ("Scope declaration", "Requirements", "Goal DAG"):
        return {
            "Scope declaration": "scope",
            "Requirements": "requirements",
            "Goal DAG": "dag",
        }[title]
    return "goal-" + title.replace(" ", "-").lower()


def _footer_css(footer: str) -> str:
    """The ``@page`` footer rule (project id + plan version)."""
    escaped = footer.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "\n@page {\n"
        "  @bottom-center {\n"
        f"    content: \"{escaped}\";\n"
        "    font-family: var(--sans);\n"
        "    font-size: 8.5pt;\n"
        "    color: var(--muted);\n"
        "  }\n"
        "}\n"
    )


def _basis_label(
    design: StatisticalDesign | None, design_missing: bool
) -> str:
    """The SS8 provenance label of one numeric margin."""
    if design_missing:
        return '<span class="muted">design ref unresolved</span>'
    if design is None:
        return '<span class="muted">no basis on record (07 SS8)</span>'
    if design.margin_basis is not None:
        return (
            f"basis: {html.escape(design.margin_basis.value)}"
            + (
                f" ({html.escape(design.rationale)})"
                if design.rationale
                else ""
            )
        )
    return '<span class="muted">basis not recorded (07 SS8)</span>'


def _criticality_chip(criticality: Criticality) -> str:
    """The criticality chip of one requirement."""
    cls = {
        Criticality.CRITICAL: "chip-critical",
        Criticality.REQUIRED: "chip-required",
        Criticality.SUPPORTING: "chip-supporting",
    }[criticality]
    return f'<span class="chip {cls}">{html.escape(criticality.value)}</span>'


def _kv_table(rows: list[tuple[str, str]], *, cls: str = "") -> str:
    """A deterministic key-value table of pre-escaped HTML cells."""
    body_rows: list[str] = []
    for key, value in rows:
        body_rows.append(
            f"<tr><td>{key}</td><td>{value}</td></tr>"
        )
    cls_attr = f' class="{cls}"' if cls else ""
    return f'<table{cls_attr}><tbody>\n' + "\n".join(body_rows) + "\n</tbody></table>"


def _json_block(value: Any) -> str:
    """A deterministic compact-JSON block (escaped, pre-formatted)."""
    return (
        f'<div class="json-block">{html.escape(_compact_json(value))}</div>'
    )


def _compact_json(value: Any) -> str:
    """Compact canonical JSON of a record value (sorted keys)."""
    return json.dumps(value, sort_keys=True)


def _format_value(value: Any) -> str:
    """Deterministic display form of one record value."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return _compact_json(value)
    return str(value)


def _format_iso(value: datetime) -> str:
    """Format a timezone-aware datetime as UTC ISO-8601 (``Z``)."""
    return value.astimezone(timezone.utc).strftime(_ISO_FORMAT)


def _yes_no(value: bool) -> str:
    """Deterministic boolean display."""
    return "yes" if value else "no"


def _maybe(value: str | None) -> str:
    """Render an optional string (``None`` as a muted placeholder)."""
    if value is None:
        return '<span class="muted">—</span>'
    return html.escape(value)


def _maybe_ref(value: str | None) -> str:
    """Render an optional record ref (``None`` as a muted placeholder)."""
    if value is None:
        return '<span class="muted">—</span>'
    return f'<span class="mono">{html.escape(value)}</span>'


def _maybe_int(value: int | None) -> str:
    """Render an optional integer."""
    if value is None:
        return '<span class="muted">—</span>'
    return str(value)


def _maybe_float(value: float | None) -> str:
    """Render an optional float deterministically."""
    if value is None:
        return '<span class="muted">—</span>'
    return f"{value:.4g}"


def _id_list(ids: list[str]) -> str:
    """Render a record id list as mono chips."""
    if not ids:
        return '<span class="muted">—</span>'
    return " ".join(
        f'<span class="mono">{html.escape(item)}</span>' for item in sorted(ids)
    )
