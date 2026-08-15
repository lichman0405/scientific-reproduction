"""Flow layout tests for the shared deterministic PDF renderer (issue #107).

``FlowLayout`` is the high-level layout engine the reproduction report
renderer (and the parallel plan/execution-sheet renderers) drive: it owns
pagination, headings with outline bookmarks, word-wrapped paragraphs,
tables and verdict callouts. Every rendered byte is a pure function of
the layout calls, so these tests double as the determinism contract.
"""
from __future__ import annotations

import pytest

from scientific_reproduction.rendering.layout import FlowLayout
from scientific_reproduction.rendering.pdf import PdfDocument

# ---------------------------------------------------------------------------
# headings -- bookmarks and page recording
# ---------------------------------------------------------------------------


def test_layout_heading_records_title_and_page() -> None:
    """A heading draws its text, registers an outline bookmark and records
    (title, creation-order page index) for the two-pass TOC."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.heading("Executive summary", level=1)

    assert [heading.title for heading in layout.headings] == [
        "Executive summary"
    ]
    assert layout.headings[0].page_index == 0
    assert layout.page_count == 1
    data = doc.render()
    assert b"(Executive summary)" in data
    assert b"/Title (Executive summary)" in data


# ---------------------------------------------------------------------------
# pagination -- wrapping flows onto fresh pages
# ---------------------------------------------------------------------------


def test_layout_paragraph_wraps_and_paginates() -> None:
    """A long paragraph wraps to multiple lines and flows onto new pages
    instead of overflowing."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.paragraph("lorem ipsum dolor sit amet " * 600)

    assert layout.page_count > 1
    data = doc.render()
    assert b"lorem" in data


def test_layout_page_break_forces_new_page() -> None:
    """An explicit page break always moves the cursor to a fresh page."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.heading("One", level=1)
    layout.page_break()
    layout.heading("Two", level=1)

    assert layout.page_count == 2
    assert layout.headings[1].page_index == 1


# ---------------------------------------------------------------------------
# tables -- header fill, borders, wrapped cells
# ---------------------------------------------------------------------------


def test_layout_table_renders_header_fill_and_rows() -> None:
    """A table draws its header (fill + bold text), its rows and border
    strokes."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.table(
        headers=["Requirement", "Outcome"],
        rows=[["REQ-001", "REPRODUCED"]],
        widths=[280.0, 160.0],
    )
    data = doc.render()

    assert b"REQ-001" in data
    assert b"REPRODUCED" in data
    assert b"re f" in data  # header fill rect
    assert b"re S" in data  # table border stroke


def test_layout_table_long_cell_wraps_without_overflow() -> None:
    """A long cell value wraps inside its column; the layout still fits
    one page and renders every word."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.table(
        headers=["Statement"],
        rows=[[("very " * 60).strip()]],
        widths=[440.0],
    )
    data = doc.render()

    assert layout.page_count == 1
    assert b"very" in data


# ---------------------------------------------------------------------------
# callouts -- verdict colors drive the visual
# ---------------------------------------------------------------------------


def test_layout_callout_renders_verdict_colors() -> None:
    """A PASS callout draws its title and body on the pass background
    with the pass foreground (border/title)."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.callout(
        "PASS", "Reproduced within tolerance", "All critical requirements met."
    )
    data = doc.render()

    assert b"(Reproduced within tolerance)" in data
    assert b"(All critical requirements met.)" in data
    assert b"0.15 0.52 0.26" in data  # pass foreground (border/title)
    assert b"0.91 0.97 0.92" in data  # pass background fill


def test_layout_callout_unknown_verdict_renders_neutral() -> None:
    """An unknown verdict renders neutral gray, never green or red."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.callout("SOMETHING_ELSE", "Unrated", "No verdict recorded.")
    data = doc.render()

    assert b"0.36 0.39 0.43" in data  # neutral foreground
    assert b"0.94 0.95 0.96" in data  # neutral background
    assert b"0.15 0.52 0.26" not in data  # no pass green
    assert b"0.72 0.16 0.14" not in data  # no fail red


# ---------------------------------------------------------------------------
# determinism -- identical calls, identical bytes
# ---------------------------------------------------------------------------


def test_layout_render_is_byte_identical() -> None:
    """Repeating the same layout calls yields byte-identical PDFs."""

    def build() -> bytes:
        doc = PdfDocument(title="T")
        layout = FlowLayout(doc)
        layout.heading("Section one", level=1)
        layout.paragraph("Some body text that wraps.")
        layout.table(headers=["A"], rows=[["1"]])
        layout.callout("INCONCLUSIVE", "Partial", "Not enough evidence yet.")
        return doc.render()

    assert build() == build()


# ---------------------------------------------------------------------------
# TOC support -- entry drawing and page-count arithmetic
# ---------------------------------------------------------------------------


def test_layout_toc_entry_draws_title_and_page_number() -> None:
    """A TOC entry renders its number, title and right-aligned page
    number on one line."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)
    layout.toc_entry(1, "Executive summary", 3)
    data = doc.render()

    assert b"Executive summary" in data
    assert b"(3)" in data  # the target page number


def test_layout_toc_page_count_is_pure() -> None:
    """The number of TOC pages is a pure function of the entry count."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)

    assert layout.toc_page_count(0) == 0
    assert layout.toc_page_count(10) == 1
    assert layout.toc_page_count(200) > 1


# ---------------------------------------------------------------------------
# boundaries -- stable errors at the public boundary
# ---------------------------------------------------------------------------


def test_layout_rejects_unknown_font_on_table_override() -> None:
    """An unknown face name raises a stable ValueError."""
    doc = PdfDocument(title="T")
    layout = FlowLayout(doc)

    with pytest.raises(ValueError, match="font"):
        layout.table(headers=["A"], rows=[["1"]], cell_font="Times")
