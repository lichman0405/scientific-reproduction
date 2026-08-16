"""Experiment execution sheet -- deterministic PDF renderer (issue #122).

The experiment execution sheet gets first-class PDF treatment on top of
the HTML sheet of ``reporting.sheets.experiment``: one deterministic
PDF 1.4 document per dispatched lab package, rendered from the same
state and the same injected ``generated_at``/``language`` inputs and
through the same shared ``rendering`` visual system the reproduction
report PDF renderer (``reporting.pdf_report``) builds on
(``rendering.layout.FlowLayout`` over ``rendering.pdf``).

Document structure
------------------
The PDF mirrors the designed HTML sheet section for section: header
banner (kind, title, run/dispatch ids), identity block (project/paper
identity, goal title/track/frozen version, package ids), objective,
reagents / instruments / critical-control tables (columns = sorted
union of item keys -- the ``additionalProperties``-fidelity rule of the
sheets), the numbered procedure, the red prohibited-changes block
(STRICT emphasis on the strict track) and the amber safety block (as
verdict callouts of the shared palette), the operator record fields,
the required-return checklist with the "returned as file" fill-in, the
additional package data, the signature lines and the fixed print
footer.

Boundaries and determinism
--------------------------
The renderer calls the public ``build_experiment_sheet`` for the state
read and the HTML body, so it shares that error surface unchanged
(``TypeError`` at the public boundaries, ``ValueError`` for an unknown
``language``, ``SheetNotInitializedError`` / ``SheetNotFoundError`` /
``SheetCorruptError`` for the state). It is a pure function of
``(state, generated_at, language)``: no wall clock, no randomness, no
network -- the default ``"en"`` rendering is byte-identical across
repeated builds, and the language is an explicit pack key, never
locale-detected (``14-STATE-GIT-ARTIFACTS.md`` SS7).

PDF output note
---------------
The deterministic PDF writer (``rendering.pdf``) encodes text as
WinAnsi (cp1252); characters cp1252 cannot represent -- including the
CJK glyphs of the ``zh`` pack -- render as ``?`` (deterministic). The
``zh`` pack therefore targets the HTML sheet (the operator-facing
artifact); ``language="zh"`` PDFs render deterministically with the
writer's documented ``?`` fallback.

Machine-auditable registration
------------------------------
With ``out_dir`` set, the renderer writes
``experiment-sheet-<RUN_ID>.pdf`` plus a canonical JSON sidecar
carrying the PDF's SHA-256 -- the same pattern as
``reporting.pdf_report``. Written under ``reports/``, both files are
registered by the audit package's report-file scan with checksums.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from scientific_reproduction.core.models import GoalTrack
from scientific_reproduction.rendering import FlowLayout, PdfDocument
from scientific_reproduction.reporting.language import TemplatePack, resolve_pack
from scientific_reproduction.reporting.sheets.experiment import (
    build_experiment_sheet,
)

if TYPE_CHECKING:
    from scientific_reproduction.core.models import GoalContract
    from scientific_reproduction.reporting.sheets.experiment import ExperimentSheet

__all__ = [
    "JSON_FILENAME_TPL",
    "PDF_FILENAME_TPL",
    "SHEET_PDF_VERSION",
    "ExperimentSheetPdf",
    "build_experiment_sheet_pdf",
    "render_experiment_sheet_pdf",
]

#: Version of the sheet PDF serialization (``sheet_pdf_version`` key of
#: :class:`ExperimentSheetPdf`).
SHEET_PDF_VERSION: str = "1.0"

#: Sheet PDF file names written to the out dir (``{run_id}`` is the run
#: id of the dispatched package; under ``reports/`` the audit package's
#: report-file scan registers them with checksums).
PDF_FILENAME_TPL: str = "experiment-sheet-{run_id}.pdf"
JSON_FILENAME_TPL: str = "experiment-sheet-{run_id}.json"

#: The manifest keys the HTML renderer displays in dedicated sections;
#: mirrored here (no private cross-module imports -- house rule) so the
#: "Additional package data" section matches the sheet 1:1.
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

#: Procedure-step keys displayed in the designed step layout (mirrored
#: from the HTML renderer).
_STEP_LAYOUT_KEYS: frozenset[str] = frozenset(
    {"step", "id", "title", "action", "inputs", "outputs", "trace_refs"}
)

#: Serialization: canonical JSON (sorted keys, 2-space indent, trailing
#: newline) -- the house registry convention.
_JSON_INDENT: int = 2


# ---------------------------------------------------------------------------
# The sheet PDF surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentSheetPdf:
    """One rendered deterministic experiment-sheet PDF.

    ``pdf_sha256`` / ``pdf_size_bytes`` allow the audit package to
    register the written file without re-hashing it.
    """

    sheet_pdf_version: str
    run_id: str
    dispatch_id: str
    package_id: str
    project_id: str
    goal_id: str
    language: str
    pages: int
    pdf_bytes: bytes
    pdf_sha256: str
    pdf_size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        """Plain dict of the sheet PDF (the JSON sidecar content)."""
        return {
            "sheet_pdf_version": self.sheet_pdf_version,
            "kind": "experiment-execution-sheet-pdf",
            "run_id": self.run_id,
            "dispatch_id": self.dispatch_id,
            "package_id": self.package_id,
            "project_id": self.project_id,
            "goal_id": self.goal_id,
            "language": self.language,
            "pages": self.pages,
            "pdf_file": PDF_FILENAME_TPL.format(run_id=self.run_id),
            "pdf_sha256": self.pdf_sha256,
            "pdf_size_bytes": self.pdf_size_bytes,
        }

    def to_canonical_json(self) -> str:
        """Canonical JSON of the sidecar (indent=2, sorted keys, newline)."""
        return json.dumps(self.to_dict(), indent=_JSON_INDENT, sort_keys=True) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_experiment_sheet_pdf(
    root: str | Path,
    run_id: str,
    *,
    generated_at: str | None = None,
    language: str = "en",
    out_dir: str | Path | None = None,
) -> ExperimentSheetPdf:
    """Build the deterministic PDF of one experiment execution sheet.

    The document is rendered from the same real outgoing handoff and
    project/goal context as :func:`build_experiment_sheet`
    (``lab/outgoing/<RUN_ID>/``, read through the adapter's own
    constants) -- the state read and the error surface are shared with
    the HTML sheet renderer. It is a pure function of that state plus
    ``generated_at`` (an optional caller-injected timestamp shown in the
    footer -- never read from a wall clock) and ``language`` (an
    explicit pack key -- never auto-detected; see
    :func:`resolve_pack`).

    Args:
        root: the project workspace root.
        run_id: the run id of the dispatched package (the handoff
            directory name ``lab/outgoing/<RUN_ID>/``).
        generated_at: optional caller-injected timestamp string rendered
            in the sheet footer.
        language: the explicit render language key (default ``"en"``);
            resolves to the matching :class:`TemplatePack`.
        out_dir: optional directory to write
            ``experiment-sheet-<RUN_ID>.pdf`` and its canonical JSON
            sidecar (which carries the PDF's SHA-256, so the audit
            package can register the files).

    Returns:
        The rendered :class:`ExperimentSheetPdf`.

    Raises:
        TypeError: ``root`` / ``run_id`` are not ``str``/``Path`` /
            ``str``, ``generated_at`` is set but not a non-empty string,
            ``language`` is not a non-empty string, or ``out_dir`` is
            not ``str``/``Path``/``None``.
        ValueError: ``language`` has no shipped pack (stable message
            listing the available languages).
        SheetNotInitializedError: the workspace has no project state
            record.
        SheetNotFoundError: no outgoing handoff exists for ``run_id``
            (nothing was dispatched for that run).
        SheetCorruptError: the dispatch record or manifest file is
            corrupt, or the registered goal record is corrupt.
    """
    pack = resolve_pack(language)
    sheet = build_experiment_sheet(
        root, run_id, generated_at=generated_at, language=language
    )
    pdf_bytes, pages = _render_pdf(sheet, pack, generated_at)
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    result = ExperimentSheetPdf(
        sheet_pdf_version=SHEET_PDF_VERSION,
        run_id=sheet.run_id,
        dispatch_id=sheet.dispatch.dispatch_id,
        package_id=sheet.dispatch.package_id,
        project_id=sheet.dispatch.project_id,
        goal_id=sheet.dispatch.goal_id,
        language=pack.language,
        pages=pages,
        pdf_bytes=pdf_bytes,
        pdf_sha256=sha256,
        pdf_size_bytes=len(pdf_bytes),
    )
    if out_dir is not None:
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        (out_path / PDF_FILENAME_TPL.format(run_id=sheet.run_id)).write_bytes(
            pdf_bytes
        )
        (out_path / JSON_FILENAME_TPL.format(run_id=sheet.run_id)).write_text(
            result.to_canonical_json(), encoding="utf-8"
        )
    return result


def render_experiment_sheet_pdf(
    root: str | Path,
    run_id: str,
    *,
    generated_at: str | None = None,
    language: str = "en",
) -> bytes:
    """Render the experiment execution sheet as PDF bytes.

    Convenience wrapper over :func:`build_experiment_sheet_pdf`
    returning the raw PDF 1.4 document bytes. Same boundaries as
    :func:`build_experiment_sheet_pdf`.
    """
    return build_experiment_sheet_pdf(
        root, run_id, generated_at=generated_at, language=language
    ).pdf_bytes


# ---------------------------------------------------------------------------
# Manifest helpers (mirrors of the HTML renderer's local rules)
# ---------------------------------------------------------------------------


def _manifest_track(
    manifest: Mapping[str, Any], goal: GoalContract | None
) -> GoalTrack | None:
    """The track of the package: the manifest's own track if present,
    else the registered goal's track (same rule as the HTML sheet)."""
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


def _value_text(value: Any, missing_text: str) -> str:
    """Render one JSON-ish value as plain PDF text.

    The PDF counterpart of the HTML sheet's ``value_html``: ``None``
    renders the language-pack marker, lists a ``"; "``-joined
    enumeration (mappings inside compact canonical JSON), mappings
    compact canonical JSON, everything else its string form. All values
    are data and are never translated.
    """
    if value is None:
        return missing_text
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(_flatten_entries(value, missing_text))
    if isinstance(value, Mapping):
        return json.dumps(value, indent=None, sort_keys=True, ensure_ascii=False)
    return str(value)


def _flatten_entries(value: list[Any], missing_text: str) -> list[str]:
    """Flatten one list level of JSON-ish entries into display strings
    (mirror of the HTML sheet helper)."""
    flattened: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            flattened.append(entry)
        elif isinstance(entry, Mapping):
            flattened.append(
                json.dumps(entry, indent=None, sort_keys=True, ensure_ascii=False)
            )
        elif entry is None:
            flattened.append(missing_text)
        elif isinstance(entry, list):
            flattened.extend(_flatten_entries(entry, missing_text))
        else:
            flattened.append(str(entry))
    return flattened


def _item_list(
    manifest: Mapping[str, Any], key: str
) -> list[Mapping[str, Any]]:
    """The items of one package list field (mappings only, as stored)."""
    raw = manifest.get(key)
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


def _render_pdf(
    sheet: ExperimentSheet,
    pack: TemplatePack,
    generated_at: str | None,
) -> tuple[bytes, int]:
    """Render the sheet PDF and return (pdf bytes, page count).

    Deterministic: every collection is sorted, the document draws
    through the shared ``FlowLayout`` (which paginates and records
    outline bookmarks), and the only time-ish text is the injected
    ``generated_at`` stamp in the fixed footer.
    """
    experiment = pack.experiment
    stamp = generated_at if generated_at is not None else pack.deterministic_render
    doc = PdfDocument(
        title=experiment.doc_title_tpl.format(run_id=sheet.run_id)
    )
    layout = FlowLayout(
        doc,
        footer_left=experiment.footer_pdf_tpl.format(
            version=SHEET_PDF_VERSION, stamp=stamp
        ),
        footer_right="Page {page} of {total}",
    )
    manifest = sheet.manifest

    # -- header banner -------------------------------------------------------
    layout.heading(experiment.title, level=1)
    layout.paragraph(
        f"{experiment.banner_kind} · {experiment.banner_run}"
        f" {sheet.run_id} · {experiment.banner_dispatch}"
        f" {sheet.dispatch.dispatch_id}"
    )

    # -- identity -------------------------------------------------------------
    layout.heading(experiment.section_identity, level=2)
    target = sheet.project.primary_target
    paper_parts: list[str] = []
    if target.title:
        paper_parts.append(target.title)
    if target.doi:
        paper_parts.append(f"DOI {target.doi}")
    if target.identifier:
        paper_parts.append(target.identifier)
    if not paper_parts:
        paper_parts.append(pack.not_recorded)
    goal_title = sheet.goal.title if sheet.goal is not None else pack.not_registered
    track = _manifest_track(manifest, sheet.goal)
    goal_track = track.value if track is not None else pack.not_recorded
    goal_version = _manifest_goal_version(manifest, sheet.goal)
    identity_rows: list[tuple[str, str]] = [
        (experiment.label_project, str(sheet.project.project_id)),
        (experiment.label_paper, "; ".join(paper_parts)),
        (
            experiment.label_goal,
            f"{sheet.dispatch.goal_id} — {goal_title}",
        ),
        (experiment.label_track, goal_track),
        (
            experiment.label_goal_version,
            goal_version if goal_version is not None else pack.not_recorded,
        ),
        (experiment.label_package, sheet.dispatch.package_id),
        (experiment.label_run, sheet.dispatch.run_id),
        (experiment.label_dispatch, sheet.dispatch.dispatch_id),
        (
            experiment.label_dispatched_at,
            sheet.dispatch.dispatched_at or pack.not_recorded,
        ),
    ]
    for label, value in identity_rows:
        layout.paragraph(f"{label}: {_value_text(value, pack.not_recorded)}")

    # -- objective -------------------------------------------------------------
    layout.heading(experiment.section_objective, level=2)
    layout.paragraph(
        _value_text(manifest.get("objective"), pack.not_recorded)
    )

    # -- reagents / instruments / critical controls ------------------------------
    _render_item_table(
        layout, experiment.section_reagents, _item_list(manifest, "reagents"), pack
    )
    _render_item_table(
        layout, experiment.section_instruments, _item_list(manifest, "instruments"), pack
    )

    # -- procedure ---------------------------------------------------------------
    layout.heading(experiment.section_procedure, level=2)
    procedure = manifest.get("procedure")
    if not isinstance(procedure, list):
        layout.paragraph(pack.not_recorded)
    else:
        for index, step in enumerate(procedure, start=1):
            if not isinstance(step, Mapping):
                layout.paragraph(_value_text(step, pack.not_recorded))
                continue
            step_id = step.get("id") or step.get("step") or str(index)
            title = step.get("title")
            head = experiment.step_tpl.format(index=index)
            if title:
                head += f" — {str(title)}"
            layout.heading(f"{head}  [{step_id}]", level=3)
            if "action" in step:
                layout.paragraph(_value_text(step["action"], pack.not_recorded))
            for key in sorted(step):
                if key in _STEP_LAYOUT_KEYS or key == "action":
                    continue
                layout.paragraph(
                    f"{key}: {_value_text(step[key], pack.not_recorded)}"
                )
            for key in ("inputs", "outputs", "trace_refs"):
                if key in step:
                    layout.paragraph(
                        f"{key}: {_value_text(step[key], pack.not_recorded)}"
                    )

    # -- critical control variables ------------------------------------------------
    _render_item_table(
        layout,
        experiment.section_critical_controls,
        _item_list(manifest, "critical_control_variables"),
        pack,
    )

    # -- prohibited changes (red callout, STRICT emphasis) ---------------------------
    layout.heading(experiment.section_prohibited, level=2)
    changes = manifest.get("prohibited_changes")
    if not isinstance(changes, list) or not changes:
        body = experiment.none_recorded_in_package
    else:
        body = "; ".join(str(change) for change in changes)
    if track is GoalTrack.STRICT_REPRODUCTION:
        body = f"{body} {experiment.strict_emphasis}".strip()
    layout.callout("FAIL", experiment.section_prohibited, body)

    # -- safety notes (amber callout) --------------------------------------------------
    layout.heading(experiment.section_safety, level=2)
    notes = manifest.get("safety_notes")
    if not isinstance(notes, list) or not notes:
        body = experiment.none_recorded_in_package
    else:
        body = "; ".join(str(note) for note in notes)
    layout.callout("INCONCLUSIVE", experiment.section_safety, body)

    # -- operator record --------------------------------------------------------------
    records = manifest.get("required_operator_records")
    if isinstance(records, list) and records:
        layout.heading(experiment.section_operator_record, level=2)
        for record in records:
            layout.paragraph(f"{str(record)}: ______")

    # -- required returns --------------------------------------------------------------
    layout.heading(experiment.section_required_returns, level=2)
    required = manifest.get("required_return")
    if not isinstance(required, list) or not required:
        layout.paragraph(pack.not_recorded)
    else:
        for token in required:
            layout.paragraph(
                f"- {str(token)} — {experiment.returned_as_file_label}: ______"
            )

    # -- additional package data ----------------------------------------------------------
    extra = sorted(key for key in manifest if key not in _KNOWN_MANIFEST_KEYS)
    if extra:
        layout.heading(experiment.section_additional_data, level=2)
        for key in extra:
            layout.paragraph(
                f"{key}: {_value_text(manifest[key], pack.not_recorded)}"
            )

    # -- signatures -----------------------------------------------------------------------
    layout.spacer(8.0)
    layout.paragraph(experiment.operator_signature)
    layout.paragraph(experiment.supervisor_signature)

    return doc.render(), len(doc.pages)


def _render_item_table(
    layout: FlowLayout,
    title: str,
    items: list[Mapping[str, Any]],
    pack: TemplatePack,
) -> None:
    """One item table: columns = sorted union of keys across the items
    (the sheet's ``additionalProperties`` fidelity rule)."""
    if not items:
        layout.heading(title, level=2)
        layout.paragraph(pack.experiment.no_items_recorded)
        return
    columns = sorted({key for item in items for key in item})
    layout.heading(title, level=2)
    layout.table(
        headers=columns,
        rows=[
            [
                _value_text(item.get(column), pack.not_recorded)
                for column in columns
            ]
            for item in items
        ],
    )
