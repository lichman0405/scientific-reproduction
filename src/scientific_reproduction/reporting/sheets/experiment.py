"""Experiment execution sheet renderer (issue #106).

One print-ready A4 sheet per dispatched lab package, rendered from the
**real outgoing handoff** the FilesystemLabAdapter wrote
(``lab/outgoing/<RUN_ID>/``: the ``DispatchRecord`` at ``dispatch.json``
and the schema-gated execution package manifest at ``manifest.json``,
``10-EXPERIMENT-SUBSYSTEM.md`` SS2) plus the project/goal context of the
planning registries. The manifest is the 1:1 content source: every item
of ``reagents`` / ``instruments`` / ``procedure`` /
``critical_control_variables`` is rendered with all of its keys
(unknown keys included, because the frozen schema allows
``additionalProperties`` -- the sheet never drops package content), and
unknown top-level manifest keys render in an "Additional package data"
section.

Sheet layout (operator-facing, print-ready)
-------------------------------------------
1. Header banner -- kind, run id, dispatch id.
2. Identity -- project/paper identity (``Project``), goal title and
   track (``GoalContract``), package ids.
3. Objective.
4. Reagents table (reagent, amount, role + any further keys).
5. Instruments table.
6. Numbered step-by-step procedure (every step key, verbatim command
   scripts in a monospace block).
7. Critical control variables with tolerances.
8. Prohibited changes -- the visually dominant red block, with the
   STRICT-track emphasis.
9. Safety notes -- the visually distinct amber block.
10. Operator record -- fill-in form fields.
11. Required return checklist -- every token of ``required_return``
    with a checkbox and a "returned as file" fill-in (cf. issue #85).
12. Additional package data (keys the v0.1 renderer does not display
    elsewhere), so manifest content matches the sheet 1:1.
13. Signature/date lines for operator and supervisor.
14. Fixed print footer.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the given
inputs: no wall clock (an optional ``generated_at`` stamp is caller-
injected), no randomness, no network. Every collection is sorted by
stable keys, so identical state always yields byte-identical HTML and
canonical JSON. ``TypeError`` at the public boundaries; stored-record
errors are re-raised as ``SheetCorruptError`` with the same message
(the ``report.py`` ``_wrap_corrupt`` discipline); a workspace without a
project state raises ``SheetNotInitializedError``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from scientific_reproduction.adapters.lab.base import DispatchRecord
from scientific_reproduction.adapters.lab.filesystem import (
    DISPATCH_RECORD_FILENAME,
    EXECUTION_MANIFEST_FILENAME,
    OUTGOING_DIR_NAME,
)
from scientific_reproduction.core.models import GoalTrack, Project
from scientific_reproduction.core.state_backend import SCHEMA_TO_STATE_DIR
from scientific_reproduction.planning.init import (
    ProjectNotInitializedError,
    read_project_state,
)
from scientific_reproduction.planning.plan import GoalNotFoundError, read_goal
from scientific_reproduction.reporting.sheets.html import (
    SHEET_CSS,
    html_document,
    html_escape,
    value_html,
)

if TYPE_CHECKING:
    from scientific_reproduction.core.models import GoalContract

__all__ = [
    "EXPERIMENT_SHEET_VERSION",
    "ExperimentSheet",
    "SheetCorruptError",
    "SheetError",
    "SheetNotInitializedError",
    "SheetNotFoundError",
    "build_experiment_sheet",
    "render_experiment_sheet",
]

#: Version of the experiment sheet serialization (``sheet_version`` key
#: of :class:`ExperimentSheet`).
EXPERIMENT_SHEET_VERSION: str = "1.0"

#: The manifest keys the renderer displays in dedicated sections. Any
#: other top-level key is displayed verbatim in the "Additional package
#: data" section so the sheet matches the manifest 1:1.
_KNOWN_MANIFEST_KEYS: frozenset[str] = frozenset(
    {
        "package_id",
        "project_id",
        "goal_id",
        "run_id",
        "objective",
        "procedure",
        "required_return",
        "track",
        "reagents",
        "instruments",
        "critical_control_variables",
        "prohibited_changes",
        "required_operator_records",
        "safety_notes",
    }
)

#: Procedure-step keys displayed in the designed step layout. Any other
#: step key is rendered as a generic detail row (never dropped).
_STEP_LAYOUT_KEYS: frozenset[str] = frozenset(
    {"step", "id", "title", "action", "inputs", "outputs", "trace_refs"}
)

#: Keys whose values are verbatim text/scripts, rendered in a monospace
#: block instead of inline text.
_VERBATIM_KEYS: frozenset[str] = frozenset(
    {"command", "script", "sbatch_script", "code"}
)

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2


# ---------------------------------------------------------------------------
# Errors (ValueError subclasses, stable messages)
# ---------------------------------------------------------------------------


class SheetError(ValueError):
    """Base class of the execution-sheet renderer errors."""


class SheetNotInitializedError(SheetError):
    """Raised when a sheet is requested for a workspace without a project
    state record (no ``project.yaml`` at the root)."""


class SheetNotFoundError(SheetError):
    """Raised when the requested handoff record or job record does not
    exist (nothing was dispatched / no durable job record)."""


class SheetCorruptError(SheetError):
    """Raised when a stored record the sheet reads is corrupt.

    The registered state is read through the real registry read APIs;
    those APIs surface corruption as ``ValueError``/``TypeError``, which
    this module re-raises as ``SheetCorruptError`` with the same message
    so the sheet's error surface stays stable.
    """


# ---------------------------------------------------------------------------
# The sheet surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentSheet:
    """One rendered experiment execution sheet.

    ``manifest`` is the schema-gated execution package manifest exactly
    as read from the outgoing handoff (the 1:1 content source); ``goal``
    is the registered goal contract of the package (``None`` when the
    goal id is not registered in the project registry -- the sheet then
    renders "not registered" markers instead of guessing).
    """

    run_id: str
    dispatch: DispatchRecord
    manifest: dict[str, Any]
    goal: GoalContract | None
    html: str

    def to_html(self) -> str:
        """The full self-contained A4 HTML document of the sheet."""
        return self.html

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the sheet surface (canonical field order)."""
        data: dict[str, Any] = {
            "sheet_version": EXPERIMENT_SHEET_VERSION,
            "kind": "experiment-execution-sheet",
            "run_id": self.run_id,
            "dispatch_id": self.dispatch.dispatch_id,
            "package_id": self.dispatch.package_id,
            "project_id": self.dispatch.project_id,
            "goal_id": self.dispatch.goal_id,
            "manifest": self.manifest,
        }
        if self.goal is not None:
            data["goal_title"] = self.goal.title
            data["goal_track"] = self.goal.track.value
        return data

    def to_canonical_json(self) -> str:
        """Canonical JSON of :meth:`to_dict` (sorted keys, 2-space indent,
        trailing newline) -- byte-identical for identical state."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_experiment_sheet(
    root: str | Path,
    run_id: str,
    *,
    generated_at: str | None = None,
) -> ExperimentSheet:
    """Build the experiment execution sheet of one dispatched lab package.

    The sheet is rendered from the real outgoing handoff of the
    ``FilesystemLabAdapter`` at ``<root>/lab/outgoing/<RUN_ID>/``
    (``dispatch.json`` + ``manifest.json``, read through the adapter's
    own constants) and the registered project/goal context. It is a pure
    function of that state plus ``generated_at`` (an optional
    caller-injected timestamp shown in the footer -- never read from a
    wall clock).

    Args:
        root: the project workspace root.
        run_id: the run id of the dispatched package (the handoff
            directory name ``lab/outgoing/<RUN_ID>/``).
        generated_at: optional caller-injected timestamp string rendered
            in the sheet footer.

    Returns:
        The rendered :class:`ExperimentSheet`.

    Raises:
        TypeError: ``root`` / ``run_id`` are not ``str``/``Path`` /
            ``str``, or ``generated_at`` is set but not a non-empty
            string.
        SheetNotInitializedError: the workspace has no project state
            record.
        SheetNotFoundError: no outgoing handoff exists for ``run_id``
            (nothing was dispatched for that run).
        SheetCorruptError: the dispatch record or manifest file is
            corrupt, or the registered goal record is corrupt.
    """
    root_path, run_id = _validate_inputs(root, run_id, generated_at)
    project = _read_project(root_path)
    dispatch, manifest = _read_handoff(root_path, run_id)
    goal = _read_goal_optional(root_path, dispatch.goal_id)
    body = _render_sheet(project, dispatch, manifest, goal, generated_at)
    return ExperimentSheet(
        run_id=run_id,
        dispatch=dispatch,
        manifest=manifest,
        goal=goal,
        html=html_document(
            f"Experiment execution sheet — {run_id}", body, stylesheet=SHEET_CSS
        ),
    )


def render_experiment_sheet(
    root: str | Path,
    run_id: str,
    *,
    generated_at: str | None = None,
) -> str:
    """Render the experiment execution sheet as a full HTML document.

    Convenience wrapper over :func:`build_experiment_sheet` returning
    the self-contained A4 HTML document (PDF-convertible through the
    browser print path).
    """
    return build_experiment_sheet(
        root, run_id, generated_at=generated_at
    ).to_html()


# ---------------------------------------------------------------------------
# Reading the registered state (real APIs, real adapter constants)
# ---------------------------------------------------------------------------


def _read_project(root: Path) -> Project:
    """Read the project state record (required context of every sheet)."""
    try:
        return read_project_state(root)
    except ProjectNotInitializedError as exc:
        raise SheetNotInitializedError(str(exc)) from exc


def _read_handoff(root: Path, run_id: str) -> tuple[DispatchRecord, dict[str, Any]]:
    """Read the outgoing handoff of one dispatch from the handoff dir.

    The handoff root is the canonical ``lab/`` tree directory of the
    workspace (``SCHEMA_TO_STATE_DIR["lab-execution-package"]`` -- the
    same mapping the state backend uses); the outgoing directory and
    record filenames are the adapter's own constants.

    Raises:
        SheetNotFoundError: no handoff directory/records exist for the
            run id.
        SheetCorruptError: a handoff record file is corrupt (unparseable
            or not a JSON object, or violates the ``DispatchRecord``
            contract).
    """
    lab_root = root / SCHEMA_TO_STATE_DIR["lab-execution-package"]
    outgoing_dir = lab_root / OUTGOING_DIR_NAME / run_id
    record_path = outgoing_dir / DISPATCH_RECORD_FILENAME
    manifest_path = outgoing_dir / EXECUTION_MANIFEST_FILENAME
    if not record_path.is_file() or not manifest_path.is_file():
        raise SheetNotFoundError(
            f"no outgoing lab handoff for run {run_id!r} at {outgoing_dir};"
            f" dispatch the execution package first (expected"
            f" {DISPATCH_RECORD_FILENAME} and"
            f" {EXECUTION_MANIFEST_FILENAME})"
        )
    record_raw = _read_json_object(record_path, "lab dispatch record")
    manifest_raw = _read_json_object(manifest_path, "lab execution manifest")
    try:
        dispatch = DispatchRecord.from_dict(record_raw)
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc
    return dispatch, manifest_raw


def _read_goal_optional(root: Path, goal_id: str) -> GoalContract | None:
    """Read the registered goal contract of the package, or ``None``.

    A goal id without a registered goal contract renders "not
    registered" markers on the sheet -- never guessed, never silently
    matched. A corrupt stored goal is a loud ``SheetCorruptError``.
    """
    try:
        return read_goal(root, goal_id)
    except GoalNotFoundError:
        return None
    except (TypeError, ValueError) as exc:
        raise SheetCorruptError(str(exc)) from exc


def _read_json_object(path: Path, kind: str) -> dict[str, Any]:
    """Load and type a handoff record file, rejecting corrupt state."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SheetCorruptError(f"corrupt {kind} record at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SheetCorruptError(
            f"corrupt {kind} record at {path}: expected a JSON object"
        )
    return raw


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_sheet(
    project: Project,
    dispatch: DispatchRecord,
    manifest: Mapping[str, Any],
    goal: GoalContract | None,
    generated_at: str | None,
) -> str:
    """Render the sheet body (deterministic: sorted, stable structure)."""
    track = _manifest_track(manifest, goal)
    sections = [
        _render_banner(dispatch.run_id, dispatch.dispatch_id),
        _render_identity(project, dispatch, goal, track),
        _render_objective(manifest),
        _render_reagents(manifest),
        _render_instruments(manifest),
        _render_procedure(manifest),
        _render_critical_controls(manifest),
        _render_prohibited(manifest, track),
        _render_safety(manifest),
        _render_operator_records(manifest),
        _render_return_checklist(manifest),
        _render_additional_data(manifest),
        _render_signatures(),
        _render_footer(generated_at),
    ]
    return "\n".join(section for section in sections if section)


def _manifest_track(
    manifest: Mapping[str, Any], goal: GoalContract | None
) -> GoalTrack | None:
    """The track of the package: the manifest's own track if present,
    else the registered goal's track."""
    raw = manifest.get("track")
    if isinstance(raw, str):
        try:
            return GoalTrack(raw)
        except ValueError:
            pass
    if goal is not None:
        return goal.track
    return None


def _render_banner(run_id: str, dispatch_id: str) -> str:
    """The header banner: kind, run id, dispatch id."""
    return (
        '<div class="sheet-banner">'
        '<div class="sheet-kind">Operator execution sheet</div>'
        "<h1>Experiment Execution Sheet</h1>"
        '<div class="banner-ids">run '
        f"{html_escape(run_id)} &middot; dispatch {html_escape(dispatch_id)}"
        "</div></div>"
    )


def _render_identity(
    project: Project,
    dispatch: DispatchRecord,
    goal: GoalContract | None,
    track: GoalTrack | None,
) -> str:
    """Identity block: project/paper identity, goal, package ids."""
    paper = project.primary_target
    paper_parts: list[str] = []
    if paper.title:
        paper_parts.append(paper.title)
    if paper.doi:
        paper_parts.append(f"DOI {paper.doi}")
    if paper.identifier:
        paper_parts.append(paper.identifier)
    if not paper_parts:
        paper_parts.append("not recorded")
    goal_title = goal.title if goal is not None else "not registered in the project registry"
    goal_track = track.value if track is not None else "not recorded"
    rows: list[tuple[str, str]] = [
        ("Project", str(project.project_id)),
        ("Paper", "; ".join(paper_parts)),
        ("Goal", f"{dispatch.goal_id} — {goal_title}"),
        ("Track", goal_track),
        ("Package", dispatch.package_id),
        ("Run", dispatch.run_id),
        ("Dispatch", dispatch.dispatch_id),
        ("Dispatched at", dispatch.dispatched_at or "not recorded"),
    ]
    cells = "\n".join(
        f'<tr><td class="label">{html_escape(label)}</td>'
        f'<td class="value">{value_html(value)}</td></tr>'
        for label, value in rows
    )
    return (
        '<h2 class="sheet-section"><span class="section-index">1</span>'
        "Identity</h2>\n"
        f'<table class="meta">{cells}</table>'
    )


def _render_objective(manifest: Mapping[str, Any]) -> str:
    """The package objective paragraph."""
    return (
        '<h2 class="sheet-section"><span class="section-index">2</span>'
        "Objective</h2>\n"
        f'<p>{value_html(manifest.get("objective"))}</p>'
    )


def _render_reagents(manifest: Mapping[str, Any]) -> str:
    """Reagents table: one row per reagent, columns = sorted union of
    item keys (amount/role plus anything else the package carries)."""
    return _table_section(3, "Reagents", _item_list(manifest, "reagents"))


def _render_instruments(manifest: Mapping[str, Any]) -> str:
    """Instruments table: one row per instrument, all item keys."""
    items = _item_list(manifest, "instruments")
    return _table_section(4, "Instruments", items)


def _render_procedure(manifest: Mapping[str, Any]) -> str:
    """Numbered step-by-step procedure.

    Every step renders with the designed layout (step number/id/title/
    action prominent; inputs/outputs lists; trace refs; verbatim scripts
    in a monospace block) plus any further step keys as generic detail
    rows -- no step content is dropped.
    """
    procedure = manifest.get("procedure")
    if not isinstance(procedure, list):
        return (
            '<h2 class="sheet-section"><span class="section-index">5</span>'
            "Procedure</h2>\n"
            '<p><span class="missing">not recorded</span></p>'
        )
    steps: list[str] = []
    for index, step in enumerate(procedure, start=1):
        if not isinstance(step, Mapping):
            steps.append(
                f'<li class="step"><div class="step-detail">{value_html(step)}</div></li>'
            )
            continue
        step_id = step.get("id") or step.get("step") or str(index)
        title = step.get("title")
        head = (
            f'<span class="step-head">Step {index}</span>'
            f' <span class="step-id">{html_escape(str(step_id))}</span>'
        )
        if title:
            head += f" &mdash; {html_escape(str(title))}"
        details: list[str] = []
        if "action" in step:
            details.append(
                f'<div class="step-detail">{value_html(step["action"])}</div>'
            )
        for key in sorted(step):
            if key in _STEP_LAYOUT_KEYS or key == "action":
                continue
            if key in _VERBATIM_KEYS:
                details.append(
                    f'<div class="step-detail"><span class="step-label">'
                    f"{html_escape(key)}</span></div>"
                    f'<pre class="command">{html_escape(str(step[key]))}</pre>'
                )
                continue
            details.append(
                f'<div class="step-detail"><span class="step-label">'
                f"{html_escape(key)}</span>: {value_html(step[key])}</div>"
            )
        for key in ("inputs", "outputs", "trace_refs"):
            if key in step:
                details.append(
                    f'<div class="step-detail"><span class="step-label">'
                    f"{html_escape(key)}</span>: {value_html(step[key])}</div>"
                )
        steps.append(
            f'<li class="step">{head}\n' + "\n".join(details) + "</li>"
        )
    body = "\n".join(steps)
    return (
        '<h2 class="sheet-section"><span class="section-index">5</span>'
        "Procedure</h2>\n"
        f'<ol class="procedure">{body}</ol>'
    )


def _render_critical_controls(manifest: Mapping[str, Any]) -> str:
    """Critical control variables: one row per variable with its value
    and tolerance (plus any further keys)."""
    items = _item_list(manifest, "critical_control_variables")
    return _table_section(6, "Critical control variables", items)


def _render_prohibited(
    manifest: Mapping[str, Any], track: GoalTrack | None
) -> str:
    """Prohibited changes: the visually dominant red block.

    On the STRICT track the block carries the explicit track emphasis
    (``08-STRICT-RECOVERY-CLOSURE.md``: Strict Reproduction permits no
    deviation without a supervisor decision).
    """
    changes = manifest.get("prohibited_changes")
    if not isinstance(changes, list) or not changes:
        return (
            '<h2 class="sheet-section"><span class="section-index">7</span>'
            "Prohibited changes</h2>\n"
            '<div class="prohibited"><h3>Prohibited changes</h3>'
            "<ul><li>none recorded in the execution package</li></ul>"
            "</div>"
        )
    items = "\n".join(
        f"<li>{html_escape(str(change))}</li>" for change in changes
    )
    emphasis = ""
    if track is GoalTrack.STRICT_REPRODUCTION:
        emphasis = (
            '<div class="track-emphasis">Strict reproduction track:'
            " every listed change is prohibited and requires a supervisor"
            " decision before it may be made</div>"
        )
    return (
        '<h2 class="sheet-section"><span class="section-index">7</span>'
        "Prohibited changes</h2>\n"
        '<div class="prohibited"><h3>Prohibited changes</h3>'
        f"<ul>{items}</ul>{emphasis}</div>"
    )


def _render_safety(manifest: Mapping[str, Any]) -> str:
    """Safety notes: the visually distinct amber block."""
    notes = manifest.get("safety_notes")
    if not isinstance(notes, list) or not notes:
        return (
            '<h2 class="sheet-section"><span class="section-index">8</span>'
            "Safety notes</h2>\n"
            '<div class="safety"><h3>Safety notes</h3>'
            "<ul><li>none recorded in the execution package</li></ul>"
            "</div>"
        )
    items = "\n".join(f"<li>{html_escape(str(note))}</li>" for note in notes)
    return (
        '<h2 class="sheet-section"><span class="section-index">8</span>'
        "Safety notes</h2>\n"
        f'<div class="safety"><h3>Safety notes</h3><ul>{items}</ul></div>'
    )


def _render_operator_records(manifest: Mapping[str, Any]) -> str:
    """Operator record fields as fill-in form fields (handwriting)."""
    records = manifest.get("required_operator_records")
    if not isinstance(records, list) or not records:
        return ""
    rows = "\n".join(
        '<div class="form-row"><span class="form-label">'
        f"{html_escape(str(record))}</span>: "
        '<span class="field"></span></div>'
        for record in records
    )
    return (
        '<h2 class="sheet-section"><span class="section-index">9</span>'
        "Operator record</h2>\n" + rows
    )


def _render_return_checklist(manifest: Mapping[str, Any]) -> str:
    """Required return checklist: every exact token of ``required_return``
    with a checkbox and a "returned as file" fill-in (issue #85 tokens
    must appear verbatim)."""
    required = manifest.get("required_return")
    if not isinstance(required, list) or not required:
        return (
            '<h2 class="sheet-section"><span class="section-index">10</span>'
            "Required returns</h2>\n"
            '<p><span class="missing">not recorded</span></p>'
        )
    rows = "\n".join(
        '<div class="form-row"><span class="checkbox"></span>'
        f"{html_escape(str(token))} — returned as file: "
        '<span class="field short"></span></div>'
        for token in required
    )
    return (
        '<h2 class="sheet-section"><span class="section-index">10</span>'
        "Required returns</h2>\n" + rows
    )


def _render_additional_data(manifest: Mapping[str, Any]) -> str:
    """Any top-level manifest key the sheet does not display elsewhere,
    rendered verbatim -- the 1:1 fidelity rule of the execution sheets."""
    extra = sorted(key for key in manifest if key not in _KNOWN_MANIFEST_KEYS)
    if not extra:
        return ""
    rows = "\n".join(
        '<tr><td class="label">{0}</td><td class="value">{1}</td></tr>'.format(
            html_escape(key), value_html(manifest[key])
        )
        for key in extra
    )
    return (
        '<h2 class="sheet-section"><span class="section-index">11</span>'
        "Additional package data</h2>\n"
        f'<table class="meta">{rows}</table>'
    )


def _render_signatures() -> str:
    """Signature/date lines for operator and supervisor."""
    return (
        '<div class="signatures">'
        '<div class="signature-block"><div class="signature-line">'
        "Operator signature &amp; date</div></div>"
        '<div class="signature-block"><div class="signature-line">'
        "Supervisor signature &amp; date</div></div>"
        "</div>"
    )


def _render_footer(generated_at: str | None) -> str:
    """The fixed print footer (repeats on every printed page)."""
    stamp = generated_at if generated_at is not None else "deterministic render"
    return (
        f'<div class="footer">scientific-reproduction &middot;'
        f" experiment execution sheet v{EXPERIMENT_SHEET_VERSION}"
        f" &middot; {html_escape(stamp)}</div>"
    )


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------


def _table_section(
    index: int, title: str, items: list[Mapping[str, Any]]
) -> str:
    """One item table: columns = sorted union of keys across the items.

    The frozen lab-execution-package schema allows extra keys per item
    (``additionalProperties``), so the columns are derived from the data
    itself -- the sheet never drops item content.
    """
    if not items:
        return (
            f'<h2 class="sheet-section"><span class="section-index">{index}</span>'
            f"{html_escape(title)}</h2>\n"
            '<p><span class="missing">no items recorded</span></p>'
        )
    columns = sorted({key for item in items for key in item})
    header = "\n".join(
        f"<th>{html_escape(column)}</th>" for column in columns
    )
    body = "\n".join(
        "<tr>"
        + "".join(
            f"<td>{value_html(item.get(column))}</td>" for column in columns
        )
        + "</tr>"
        for item in items
    )
    return (
        f'<h2 class="sheet-section"><span class="section-index">{index}</span>'
        f"{html_escape(title)}</h2>\n"
        f'<table class="data"><tr>{header}</tr>{body}</table>'
    )


def _item_list(
    manifest: Mapping[str, Any], key: str
) -> list[Mapping[str, Any]]:
    """The items of one package list field (mappings only, as stored)."""
    raw = manifest.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _validate_inputs(
    root: str | Path, run_id: str, generated_at: str | None
) -> tuple[Path, str]:
    """Validate the public-boundary types (TypeError with stable
    messages), returning the normalized inputs."""
    if not isinstance(root, (str, Path)):
        raise TypeError(
            f"root must be a str or Path, got {type(root).__name__}"
        )
    if not isinstance(run_id, str) or not run_id.strip():
        raise TypeError(
            f"run_id must be a non-empty string, got {run_id!r}"
        )
    if generated_at is not None and (
        not isinstance(generated_at, str) or not generated_at.strip()
    ):
        raise TypeError(
            "generated_at must be a non-empty string when set"
        )
    return Path(root), run_id
