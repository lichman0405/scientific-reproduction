"""Shared print-stylesheet and HTML helpers of the execution sheets.

The execution-sheet renderers of ``reporting.sheets`` produce designed,
print-ready A4 HTML documents (operator-facing execution sheets for the
wet-lab handoff and the compute handoff; runtime is intentionally
stdlib-only). This module is the **shared visual system** of those
sheets -- the single A4 print stylesheet (:data:`SHEET_CSS`), the
document skeleton (:func:`html_document`) and the escaping/JSON-value
helpers every renderer uses. A future plan renderer (feat/plan-pdf-
renderer) must reuse this system instead of duplicating it.

Print design
------------
The stylesheet targets A4 ``@page`` geometry with 13-16 mm margins, a
serif reading face for prose, a sans face for headings and tables and a
monospace face for commands. Semantically distinct blocks carry their
own visual language: red for prohibited changes (the STRICT-track
emphasis), amber for safety notes, underlined fill-in fields and
checkboxes for the operator record, and a fixed footer that repeats on
every printed page. Colors are printed as designed
(``print-color-adjust: exact``) so the sheet stays legible on paper.

Determinism
-----------
Everything here is a pure function of its inputs: no wall clock, no
randomness, no network. All output is byte-stable for identical inputs.
"""

from __future__ import annotations

import html
import json
from typing import Any, Mapping

__all__ = [
    "SHEET_CSS",
    "html_document",
    "html_escape",
    "value_html",
]

#: The shared A4 print stylesheet of the execution sheets (and of any
#: future renderer that reuses this visual system). Pure CSS, no external
#: resources: the document renders identically offline and prints through
#: the browser's print-to-PDF path.
SHEET_CSS: str = """
@page { size: A4; margin: 14mm 13mm 16mm 13mm; }
* { box-sizing: border-box; }
body {
  font-family: "Liberation Serif", "Times New Roman", serif;
  font-size: 10.5pt; line-height: 1.45; color: #1a1a1a; margin: 0;
}
h1, h2, h3, h4 {
  font-family: "Liberation Sans", "Arial", "Helvetica", sans-serif;
  margin: 0; font-weight: bold;
}
/* ---- header banner -------------------------------------------------- */
.sheet-banner {
  background: #1f3b57; color: #ffffff;
  padding: 10pt 12pt; margin-bottom: 10pt;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.sheet-banner .sheet-kind {
  font-size: 8.5pt; text-transform: uppercase; letter-spacing: 2pt;
  opacity: 0.85;
}
.sheet-banner h1 { font-size: 15pt; letter-spacing: 0.5pt; margin-top: 2pt; }
.sheet-banner .banner-ids {
  font-family: "Consolas", "Liberation Mono", "Courier New", monospace;
  font-size: 8.5pt; margin-top: 4pt; color: #dbe4ee;
}
/* ---- section titles -------------------------------------------------- */
.sheet-section {
  margin-top: 12pt; margin-bottom: 5pt; font-size: 11.5pt;
  color: #1f3b57; border-bottom: 1pt solid #1f3b57; padding-bottom: 2pt;
  page-break-after: avoid;
}
.sheet-section .section-index {
  display: inline-block; min-width: 2em; color: #5a7084; font-size: 9.5pt;
}
/* ---- tables ---------------------------------------------------------- */
table.data {
  border-collapse: collapse; width: 100%; margin: 5pt 0 8pt 0;
  font-size: 9.5pt; page-break-inside: auto;
}
table.data th, table.data td {
  border: 0.5pt solid #7a8794; padding: 3pt 5pt; vertical-align: top;
  text-align: left; word-wrap: break-word;
}
table.data th {
  background: #eef1f6; font-family: "Liberation Sans", "Arial", sans-serif;
  font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.4pt;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
table.meta { width: 100%; border-collapse: collapse; margin: 4pt 0 8pt 0; }
table.meta td { padding: 2.5pt 6pt; font-size: 10pt; vertical-align: top; }
table.meta td.label {
  width: 21%; font-family: "Liberation Sans", "Arial", sans-serif;
  font-size: 8.5pt; text-transform: uppercase; letter-spacing: 0.4pt;
  color: #3d536b; border-bottom: 0.4pt solid #c3ccd6;
}
table.meta td.value {
  font-family: "Consolas", "Liberation Mono", "Courier New", monospace;
  font-size: 9.5pt; border-bottom: 0.4pt solid #c3ccd6;
}
/* ---- procedure steps ------------------------------------------------- */
ol.procedure { list-style: none; margin: 4pt 0 8pt 0; padding: 0; }
ol.procedure li.step {
  margin: 6pt 0; padding: 5pt 7pt 5pt 9pt; border-left: 2.5pt solid #2c5f8a;
  background: #f7f9fb; page-break-inside: avoid;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
ol.procedure li.step .step-head { font-weight: bold; font-size: 10.5pt; }
ol.procedure li.step .step-id {
  font-family: "Consolas", "Liberation Mono", "Courier New", monospace;
  font-size: 8.5pt; color: #5a7084;
}
ol.procedure li.step .step-detail { margin-top: 2pt; font-size: 9.5pt; }
ol.procedure li.step .step-label {
  font-family: "Liberation Sans", "Arial", sans-serif; font-size: 8pt;
  text-transform: uppercase; letter-spacing: 0.4pt; color: #3d536b;
}
/* ---- verbatim command blocks ---------------------------------------- */
pre.command {
  font-family: "Consolas", "Liberation Mono", "Courier New", monospace;
  font-size: 8.5pt; line-height: 1.35; white-space: pre-wrap;
  word-wrap: break-word; background: #f0f3f7; border: 0.5pt solid #b9c4d0;
  padding: 5pt 7pt; margin: 3pt 0 6pt 0; page-break-inside: avoid;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
/* ---- prohibited / safety blocks ------------------------------------- */
.prohibited {
  background: #fdecea; border: 1.5pt solid #b3261e; border-left: 5pt solid #b3261e;
  padding: 6pt 9pt; margin: 5pt 0 8pt 0; page-break-inside: avoid;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.prohibited h3 { color: #b3261e; font-size: 11pt; text-transform: uppercase; letter-spacing: 0.6pt; }
.prohibited ul { margin: 4pt 0 0 14pt; padding: 0; font-size: 10pt; }
.prohibited ul li { margin: 2pt 0; }
.prohibited .track-emphasis { font-size: 8.5pt; text-transform: uppercase; letter-spacing: 1pt; color: #b3261e; margin-top: 3pt; }
.safety {
  background: #fff8e1; border: 1.2pt solid #b07d00; border-left: 5pt solid #b07d00;
  padding: 6pt 9pt; margin: 5pt 0 8pt 0; page-break-inside: avoid;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.safety h3 { color: #7a5500; font-size: 11pt; text-transform: uppercase; letter-spacing: 0.6pt; }
.safety ul { margin: 4pt 0 0 14pt; padding: 0; font-size: 10pt; }
.safety ul li { margin: 2pt 0; }
/* ---- fill-in form fields -------------------------------------------- */
.field {
  display: inline-block; min-width: 18em; border-bottom: 0.9pt solid #1a1a1a;
  height: 1.25em; vertical-align: baseline;
}
.field.short { min-width: 9em; }
.form-row { margin: 4pt 0; font-size: 10pt; }
.form-row .form-label { font-weight: bold; }
.checkbox {
  display: inline-block; width: 9.5pt; height: 9.5pt; border: 1.2pt solid #1a1a1a;
  margin-right: 5pt; vertical-align: middle;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
/* ---- signatures ------------------------------------------------------ */
.signatures { margin-top: 16pt; }
.signatures .signature-block {
  display: inline-block; width: 46%; margin-right: 4%; vertical-align: top;
}
.signatures .signature-line {
  border-top: 0.9pt solid #1a1a1a; margin-top: 26pt; padding-top: 3pt;
  font-size: 9pt; color: #3d536b;
}
/* ---- fixed print footer --------------------------------------------- */
.footer {
  position: fixed; bottom: 0; left: 0; right: 0; font-size: 7.5pt;
  color: #6a7684; text-align: center; border-top: 0.4pt solid #c3ccd6;
  padding-top: 2pt; background: #ffffff;
}
@media print {
  .prohibited, .safety, .sheet-banner, table.data th,
  ol.procedure li.step, pre.command {
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
  }
}
"""


def html_escape(value: str) -> str:
    """Escape ``value`` for safe HTML text content (quotes included).

    Every user/registry-derived string reaches the document only through
    this helper: the sheets never inject unescaped content.
    """
    return html.escape(value, quote=True)


def value_html(value: Any) -> str:
    """Render one JSON-ish value as escaped inline HTML text.

    ``None`` renders as the house "not recorded" marker (the ``report.py``
    ``_maybe`` discipline -- never guessed, never silently matched);
    lists render as a ``"; "``-joined enumeration; mappings render as
    compact canonical JSON; everything else renders as its string form.
    """
    if value is None:
        return '<span class="missing">not recorded</span>'
    if isinstance(value, str):
        return html_escape(value)
    if isinstance(value, list):
        return html_escape("; ".join(_flatten_entries(value)))
    if isinstance(value, Mapping):
        return html_escape(
            json.dumps(value, indent=None, sort_keys=True, ensure_ascii=False)
        )
    return html_escape(str(value))


def html_document(title: str, body: str, *, stylesheet: str | None = None) -> str:
    """Wrap ``body`` into a complete, self-contained HTML document.

    The document carries ``SHEET_CSS`` (or ``stylesheet`` when injected)
    inline -- no external resources, so it renders offline and converts
    to PDF through the browser print path. Returns byte-stable markup for
    identical inputs.
    """
    css = SHEET_CSS if stylesheet is None else stylesheet
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{html_escape(title)}</title>\n"
        f"<style>{css}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _flatten_entries(value: list[Any]) -> list[str]:
    """Flatten one list level of JSON-ish entries into display strings.

    Nested mappings render as compact canonical JSON so their content
    stays visible and deterministic (the ``additionalProperties``-friendly
    fidelity rule of the execution sheets).
    """
    flattened: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            flattened.append(entry)
        elif isinstance(entry, Mapping):
            flattened.append(
                json.dumps(entry, indent=None, sort_keys=True, ensure_ascii=False)
            )
        elif entry is None:
            flattened.append("not recorded")
        elif isinstance(entry, list):
            flattened.extend(_flatten_entries(entry))
        else:
            flattened.append(str(entry))
    return flattened
