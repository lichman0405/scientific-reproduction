"""Experiment execution sheet renderer (issues #106, #122).

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
2. Identity -- project/paper identity (``Project``), goal title, track
   and frozen goal version (``GoalContract``), package ids.
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

Language packs (issue #122)
---------------------------
The renderer takes an explicit ``language`` key (default ``"en"``)
resolved to its :class:`TemplatePack` by :func:`resolve_pack`; every
template string above comes from the pack. Language is an explicit
input -- there is **no runtime locale auto-detection** -- so
``(state, language)`` still maps to byte-identical output
(``14-STATE-GIT-ARTIFACTS.md`` SS7); the default ``"en"`` pack renders
byte-identically to the pre-pack sheet. Manifest content (reagent
names, procedure steps, recorded notes) is data and is never
translated.

Determinism and boundaries
--------------------------
Everything is a pure function of the registered state and the given
inputs: no wall clock (an optional ``generated_at`` stamp is caller-
injected), no randomness, no network. Every collection is sorted by
stable keys, so identical state always yields byte-identical HTML and
canonical JSON. ``TypeError`` at the public boundaries; ``ValueError``
for an unknown ``language``; stored-record errors are re-raised as
``SheetCorruptError`` with the same message (the ``report.py``
``_wrap_corrupt`` discipline); a workspace without a project state
raises ``SheetNotInitializedError``.
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
from scientific_reproduction.reporting.language import TemplatePack, resolve_pack
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
        "goal_version",
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
    renders "not registered" markers instead of guessing); ``project``
    is the registered project state record (the identity context, also
    consumed by the sheet PDF renderer of ``reporting.sheet_pdf``).
    """

    run_id: str
    dispatch: DispatchRecord
    manifest: dict[str, Any]
    goal: GoalContract | None
    project: Project
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
    language: str = "en",
) -> ExperimentSheet:
    """Build the experiment execution sheet of one dispatched lab package.

    The sheet is rendered from the real outgoing handoff of the
    ``FilesystemLabAdapter`` at ``<root>/lab/outgoing/<RUN_ID>/``
    (``dispatch.json`` + ``manifest.json``, read through the adapter's
    own constants) and the registered project/goal context. It is a pure
    function of that state plus ``generated_at`` (an optional
    caller-injected timestamp shown in the footer -- never read from a
    wall clock) and ``language`` (an explicit pack key -- never
    auto-detected; see :func:`resolve_pack`).

    Args:
        root: the project workspace root.
        run_id: the run id of the dispatched package (the handoff
            directory name ``lab/outgoing/<RUN_ID>/``).
        generated_at: optional caller-injected timestamp string rendered
            in the sheet footer.
        language: the explicit render language key (default ``"en"``);
            resolves to the matching :class:`TemplatePack`.

    Returns:
        The rendered :class:`ExperimentSheet`.

    Raises:
        TypeError: ``root`` / ``run_id`` are not ``str``/``Path`` /
            ``str``, ``generated_at`` is set but not a non-empty string,
            or ``language`` is not a non-empty string.
        ValueError: ``language`` has no shipped pack (stable message
            listing the available languages).
        SheetNotInitializedError: the workspace has no project state
            record.
        SheetNotFoundError: no outgoing handoff exists for ``run_id``
            (nothing was dispatched for that run).
        SheetCorruptError: the dispatch record or manifest file is
            corrupt, or the registered goal record is corrupt.
    """
    root_path, run_id, pack = _validate_inputs(root, run_id, generated_at, language)
    project = _read_project(root_path)
    dispatch, manifest = _read_handoff(root_path, run_id)
    goal = _read_goal_optional(root_path, dispatch.goal_id)
    body = _render_sheet(project, dispatch, manifest, goal, generated_at, pack)
    return ExperimentSheet(
        run_id=run_id,
        dispatch=dispatch,
        manifest=manifest,
        goal=goal,
        project=project,
        html=html_document(
            pack.experiment.doc_title_tpl.format(run_id=run_id),
            body,
            stylesheet=SHEET_CSS,
            lang=pack.html_lang,
        ),
    )


def render_experiment_sheet(
    root: str | Path,
    run_id: str,
    *,
    generated_at: str | None = None,
    language: str = "en",
) -> str:
    """Render the experiment execution sheet as a full HTML document.

    Convenience wrapper over :func:`build_experiment_sheet` returning
    the self-contained A4 HTML document (PDF-convertible through the
    browser print path). Same boundaries as :func:`build_experiment_sheet`.
    """
    return build_experiment_sheet(
        root, run_id, generated_at=generated_at, language=language
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
    pack: TemplatePack,
) -> str:
    """Render the sheet body (deterministic: sorted, stable structure)."""
    track = _manifest_track(manifest, goal)
    goal_version = _manifest_goal_version(manifest, goal)
    sections = [
        _render_banner(dispatch.run_id, dispatch.dispatch_id, pack),
        _render_identity(project, dispatch, goal, track, goal_version, pack),
        _render_objective(manifest, pack),
        _render_reagents(manifest, pack),
        _render_instruments(manifest, pack),
        _render_procedure(manifest, pack),
        _render_critical_controls(manifest, pack),
        _render_prohibited(manifest, track, pack),
        _render_safety(manifest, pack),
        _render_operator_records(manifest, pack),
        _render_return_checklist(manifest, pack),
        _render_additional_data(manifest, pack),
        _render_signatures(pack),
        _render_footer(generated_at, pack),
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


def _manifest_goal_version(
    manifest: Mapping[str, Any], goal: GoalContract | None
) -> str | None:
    """The frozen Goal version of the package: the manifest's own
    ``goal_version`` if present, else the registered goal's version."""
    raw = manifest.get("goal_version")
    if isinstance(raw, str) and raw.strip():
        return raw
    if goal is not None:
        return goal.version
    return None


def _render_banner(run_id: str, dispatch_id: str, pack: TemplatePack) -> str:
    """The header banner: kind, run id, dispatch id."""
    experiment = pack.experiment
    return (
        '<div class="sheet-banner">'
        f'<div class="sheet-kind">{html_escape(experiment.banner_kind)}</div>'
        f"<h1>{html_escape(experiment.title)}</h1>"
        '<div class="banner-ids">'
        f"{html_escape(experiment.banner_run)} {html_escape(run_id)}"
        " &middot; "
        f"{html_escape(experiment.banner_dispatch)} {html_escape(dispatch_id)}"
        "</div></div>"
    )


def _render_identity(
    project: Project,
    dispatch: DispatchRecord,
    goal: GoalContract | None,
    track: GoalTrack | None,
    goal_version: str | None,
    pack: TemplatePack,
) -> str:
    """Identity block: project/paper identity, goal (title, track,
    frozen version), package ids."""
    experiment = pack.experiment
    paper = project.primary_target
    paper_parts: list[str] = []
    if paper.title:
        paper_parts.append(paper.title)
    if paper.doi:
        paper_parts.append(f"DOI {paper.doi}")
    if paper.identifier:
        paper_parts.append(paper.identifier)
    if not paper_parts:
        paper_parts.append(pack.not_recorded)
    goal_title = goal.title if goal is not None else pack.not_registered
    goal_track = track.value if track is not None else pack.not_recorded
    rows: list[tuple[str, str]] = [
        (experiment.label_project, str(project.project_id)),
        (experiment.label_paper, "; ".join(paper_parts)),
        (experiment.label_goal, f"{dispatch.goal_id} — {goal_title}"),
        (experiment.label_track, goal_track),
        (
            experiment.label_goal_version,
            goal_version if goal_version is not None else pack.not_recorded,
        ),
        (experiment.label_package, dispatch.package_id),
        (experiment.label_run, dispatch.run_id),
        (experiment.label_dispatch, dispatch.dispatch_id),
        (
            experiment.label_dispatched_at,
            dispatch.dispatched_at or pack.not_recorded,
        ),
    ]
    cells = "\n".join(
        f'<tr><td class="label">{html_escape(label)}</td>'
        f'<td class="value">{value_html(value, missing_text=pack.not_recorded)}</td></tr>'
        for label, value in rows
    )
    return (
        f'<h2 class="sheet-section"><span class="section-index">1</span>'
        f"{html_escape(experiment.section_identity)}</h2>\n"
        f'<table class="meta">{cells}</table>'
    )


def _render_objective(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """The package objective paragraph."""
    return (
        f'<h2 class="sheet-section"><span class="section-index">2</span>'
        f"{html_escape(pack.experiment.section_objective)}</h2>\n"
        f"<p>{value_html(manifest.get('objective'), missing_text=pack.not_recorded)}</p>"
    )


def _render_reagents(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Reagents table: one row per reagent, columns = sorted union of
    item keys (amount/role plus anything else the package carries)."""
    return _table_section(
        3, pack.experiment.section_reagents, _item_list(manifest, "reagents"), pack
    )


def _render_instruments(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Instruments table: one row per instrument, all item keys."""
    items = _item_list(manifest, "instruments")
    return _table_section(4, pack.experiment.section_instruments, items, pack)


def _render_procedure(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Numbered step-by-step procedure.

    Every step renders with the designed layout (step number/id/title/
    action prominent; inputs/outputs lists; trace refs; verbatim scripts
    in a monospace block) plus any further step keys as generic detail
    rows -- no step content is dropped.
    """
    experiment = pack.experiment
    procedure = manifest.get("procedure")
    if not isinstance(procedure, list):
        return (
            f'<h2 class="sheet-section"><span class="section-index">5</span>'
            f"{html_escape(experiment.section_procedure)}</h2>\n"
            f"<p>{value_html(None, missing_text=pack.not_recorded)}</p>"
        )
    steps: list[str] = []
    for index, step in enumerate(procedure, start=1):
        if not isinstance(step, Mapping):
            steps.append(
                f'<li class="step"><div class="step-detail">{value_html(step, missing_text=pack.not_recorded)}</div></li>'
            )
            continue
        step_id = step.get("id") or step.get("step") or str(index)
        title = step.get("title")
        head = (
            f'<span class="step-head">{html_escape(experiment.step_tpl.format(index=index))}</span>'
            f' <span class="step-id">{html_escape(str(step_id))}</span>'
        )
        if title:
            head += f" &mdash; {html_escape(str(title))}"
        details: list[str] = []
        if "action" in step:
            details.append(
                f'<div class="step-detail">{value_html(step["action"], missing_text=pack.not_recorded)}</div>'
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
                f"{html_escape(key)}</span>: {value_html(step[key], missing_text=pack.not_recorded)}</div>"
            )
        for key in ("inputs", "outputs", "trace_refs"):
            if key in step:
                details.append(
                    f'<div class="step-detail"><span class="step-label">'
                    f"{html_escape(key)}</span>: {value_html(step[key], missing_text=pack.not_recorded)}</div>"
                )
        steps.append(
            f'<li class="step">{head}\n' + "\n".join(details) + "</li>"
        )
    body = "\n".join(steps)
    return (
        f'<h2 class="sheet-section"><span class="section-index">5</span>'
        f"{html_escape(experiment.section_procedure)}</h2>\n"
        f'<ol class="procedure">{body}</ol>'
    )


def _render_critical_controls(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Critical control variables: one row per variable with its value
    and tolerance (plus any further keys)."""
    items = _item_list(manifest, "critical_control_variables")
    return _table_section(
        6, pack.experiment.section_critical_controls, items, pack
    )


def _render_prohibited(
    manifest: Mapping[str, Any], track: GoalTrack | None, pack: TemplatePack
) -> str:
    """Prohibited changes: the visually dominant red block.

    On the STRICT track the block carries the explicit track emphasis
    (``08-STRICT-RECOVERY-CLOSURE.md``: Strict Reproduction permits no
    deviation without a supervisor decision).
    """
    experiment = pack.experiment
    changes = manifest.get("prohibited_changes")
    if not isinstance(changes, list) or not changes:
        return (
            f'<h2 class="sheet-section"><span class="section-index">7</span>'
            f"{html_escape(experiment.section_prohibited)}</h2>\n"
            f'<div class="prohibited"><h3>{html_escape(experiment.section_prohibited)}</h3>'
            f"<ul><li>{html_escape(experiment.none_recorded_in_package)}</li></ul>"
            "</div>"
        )
    items = "\n".join(
        f"<li>{html_escape(str(change))}</li>" for change in changes
    )
    emphasis = ""
    if track is GoalTrack.STRICT_REPRODUCTION:
        emphasis = (
            f'<div class="track-emphasis">{html_escape(experiment.strict_emphasis)}</div>'
        )
    return (
        f'<h2 class="sheet-section"><span class="section-index">7</span>'
        f"{html_escape(experiment.section_prohibited)}</h2>\n"
        f'<div class="prohibited"><h3>{html_escape(experiment.section_prohibited)}</h3>'
        f"<ul>{items}</ul>{emphasis}</div>"
    )


def _render_safety(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Safety notes: the visually distinct amber block."""
    experiment = pack.experiment
    notes = manifest.get("safety_notes")
    if not isinstance(notes, list) or not notes:
        return (
            f'<h2 class="sheet-section"><span class="section-index">8</span>'
            f"{html_escape(experiment.section_safety)}</h2>\n"
            f'<div class="safety"><h3>{html_escape(experiment.section_safety)}</h3>'
            f"<ul><li>{html_escape(experiment.none_recorded_in_package)}</li></ul>"
            "</div>"
        )
    items = "\n".join(f"<li>{html_escape(str(note))}</li>" for note in notes)
    return (
        f'<h2 class="sheet-section"><span class="section-index">8</span>'
        f"{html_escape(experiment.section_safety)}</h2>\n"
        f'<div class="safety"><h3>{html_escape(experiment.section_safety)}</h3><ul>{items}</ul></div>'
    )


def _render_operator_records(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
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
        f'<h2 class="sheet-section"><span class="section-index">9</span>'
        f"{html_escape(pack.experiment.section_operator_record)}</h2>\n" + rows
    )


def _render_return_checklist(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Required return checklist: every exact token of ``required_return``
    with a checkbox and a "returned as file" fill-in (issue #85 tokens
    must appear verbatim)."""
    experiment = pack.experiment
    required = manifest.get("required_return")
    if not isinstance(required, list) or not required:
        return (
            f'<h2 class="sheet-section"><span class="section-index">10</span>'
            f"{html_escape(experiment.section_required_returns)}</h2>\n"
            f"<p>{value_html(None, missing_text=pack.not_recorded)}</p>"
        )
    rows = "\n".join(
        '<div class="form-row"><span class="checkbox"></span>'
        f"{html_escape(str(token))} — {html_escape(experiment.returned_as_file_label)}: "
        '<span class="field short"></span></div>'
        for token in required
    )
    return (
        f'<h2 class="sheet-section"><span class="section-index">10</span>'
        f"{html_escape(experiment.section_required_returns)}</h2>\n" + rows
    )


def _render_additional_data(manifest: Mapping[str, Any], pack: TemplatePack) -> str:
    """Any top-level manifest key the sheet does not display elsewhere,
    rendered verbatim -- the 1:1 fidelity rule of the execution sheets."""
    extra = sorted(key for key in manifest if key not in _KNOWN_MANIFEST_KEYS)
    if not extra:
        return ""
    rows = "\n".join(
        '<tr><td class="label">{0}</td><td class="value">{1}</td></tr>'.format(
            html_escape(key), value_html(manifest[key], missing_text=pack.not_recorded)
        )
        for key in extra
    )
    return (
        f'<h2 class="sheet-section"><span class="section-index">11</span>'
        f"{html_escape(pack.experiment.section_additional_data)}</h2>\n"
        f'<table class="meta">{rows}</table>'
    )


def _render_signatures(pack: TemplatePack) -> str:
    """Signature/date lines for operator and supervisor."""
    experiment = pack.experiment
    return (
        '<div class="signatures">'
        '<div class="signature-block"><div class="signature-line">'
        f"{html_escape(experiment.operator_signature)}</div></div>"
        '<div class="signature-block"><div class="signature-line">'
        f"{html_escape(experiment.supervisor_signature)}</div></div>"
        "</div>"
    )


def _render_footer(generated_at: str | None, pack: TemplatePack) -> str:
    """The fixed print footer (repeats on every printed page)."""
    stamp = generated_at if generated_at is not None else pack.deterministic_render
    footer = pack.experiment.footer_html_tpl.format(
        version=EXPERIMENT_SHEET_VERSION, stamp=html_escape(stamp)
    )
    return f'<div class="footer">{footer}</div>'


# ---------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------


def _table_section(
    index: int,
    title: str,
    items: list[Mapping[str, Any]],
    pack: TemplatePack,
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
            f'<p><span class="missing">{html_escape(pack.experiment.no_items_recorded)}</span></p>'
        )
    columns = sorted({key for item in items for key in item})
    header = "\n".join(
        f"<th>{html_escape(column)}</th>" for column in columns
    )
    body = "\n".join(
        "<tr>"
        + "".join(
            f"<td>{value_html(item.get(column), missing_text=pack.not_recorded)}</td>"
            for column in columns
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
    root: str | Path, run_id: str, generated_at: str | None, language: str
) -> tuple[Path, str, TemplatePack]:
    """Validate the public-boundary types (TypeError with stable
    messages), resolving the language pack; return the normalized
    inputs."""
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
    return Path(root), run_id, resolve_pack(language)
