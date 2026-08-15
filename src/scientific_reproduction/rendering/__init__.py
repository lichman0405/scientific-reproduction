"""Shared deterministic PDF visual system for the report renderers (issue #107).

Public surface:

* ``PdfDocument`` / ``Page`` -- a minimal stdlib-only, deterministic
  PDF 1.4 writer (base-14 ``Helvetica`` fonts, uncompressed content
  streams, xref/trailer, outline bookmarks, page reordering for TOC
  layout, ``{page}``/``{total}`` footers resolved against the final
  page order);
* the visual-system tokens of ``rendering.style`` -- page geometry,
  colors (including the PASS/FAIL/INCONCLUSIVE verdict colors shared
  with the plan renderer and the execution-sheet renderers), base-14
  font faces, AFM text measurement and the ``verdict_style`` mapping;
* ``FlowLayout`` -- high-level flow layout (headings, wrapped
  paragraphs, tables, verdict callouts) with automatic pagination.

The same package serves the final reproduction report renderer and the
parallel plan/execution-sheet renderers, so all rendered artifacts share
one visual system.
"""

from scientific_reproduction.rendering.layout import FlowLayout
from scientific_reproduction.rendering.pdf import Page, PdfDocument
from scientific_reproduction.rendering.style import (
    ACCENT,
    BODY_LINE_HEIGHT,
    BODY_SIZE,
    CALLOUT_PADDING,
    FAIL_BG,
    FAIL_COLOR,
    FONT_BODY,
    FONT_BOLD,
    FONT_HELVETICA,
    FONT_HELVETICA_BOLD,
    FONT_HELVETICA_BOLD_OBLIQUE,
    FONT_HELVETICA_OBLIQUE,
    FONT_ITALIC,
    HEADING_FONT,
    HEADING_LINE_HEIGHTS,
    HEADING_SIZES,
    INCONCLUSIVE_BG,
    INCONCLUSIVE_COLOR,
    INK,
    MARGIN_BOTTOM,
    MARGIN_LEFT,
    MARGIN_RIGHT,
    MARGIN_TOP,
    MUTED,
    NEUTRAL_BG,
    NEUTRAL_COLOR,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    PASS_BG,
    PASS_COLOR,
    RULE,
    SMALL_SIZE,
    TABLE_HEADER_FILL,
    TABLE_ROW_PADDING,
    TABLE_RULE,
    VERDICT_STYLES,
    text_width,
    verdict_style,
)

__all__ = [
    "ACCENT",
    "BODY_LINE_HEIGHT",
    "BODY_SIZE",
    "CALLOUT_PADDING",
    "FAIL_BG",
    "FAIL_COLOR",
    "FlowLayout",
    "FONT_BODY",
    "FONT_BOLD",
    "FONT_HELVETICA",
    "FONT_HELVETICA_BOLD",
    "FONT_HELVETICA_BOLD_OBLIQUE",
    "FONT_HELVETICA_OBLIQUE",
    "FONT_ITALIC",
    "HEADING_FONT",
    "HEADING_LINE_HEIGHTS",
    "HEADING_SIZES",
    "INCONCLUSIVE_BG",
    "INCONCLUSIVE_COLOR",
    "INK",
    "MARGIN_BOTTOM",
    "MARGIN_LEFT",
    "MARGIN_RIGHT",
    "MARGIN_TOP",
    "MUTED",
    "NEUTRAL_BG",
    "NEUTRAL_COLOR",
    "PAGE_HEIGHT",
    "PAGE_WIDTH",
    "PASS_BG",
    "PASS_COLOR",
    "Page",
    "PdfDocument",
    "RULE",
    "SMALL_SIZE",
    "TABLE_HEADER_FILL",
    "TABLE_ROW_PADDING",
    "TABLE_RULE",
    "VERDICT_STYLES",
    "text_width",
    "verdict_style",
]
