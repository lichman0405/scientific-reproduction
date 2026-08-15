"""Deterministic PDF writer tests (issue #107 rendering foundation).

The ``rendering`` package is the shared deterministic PDF visual system
that the final reproduction report renderer (and, per the issue, the plan
renderer and execution-sheet renderers) build on. These tests pin the
writer contract:

* deterministic output -- ``render()`` derives every byte from the API
  calls only: no wall clock (no ``/CreationDate``, ``/ModDate`` or
  ``/ID`` keys), no randomness, no font embedding, byte-identical across
  repeated renders of the same call sequence;
* stdlib-only PDF 1.4 -- ``%PDF-1.4`` header, ``startxref``/``%%EOF``
  trailer, xref table, base-14 Type1 fonts (``Helvetica`` family) shared
  across pages via a single resources dictionary, uncompressed content
  streams so the rendered text is greppable;
* text handling -- WinAnsi (cp1252) encoding with octal escapes for
  ``(``, ``)`` and the backslash, plus control bytes, with ``?`` as the
  fallback for characters cp1252 cannot represent;
* layout support -- text measurement from the standard Helvetica AFM
  width tables, page reordering (TOC pages are laid out last and moved
  to the front), outline bookmarks that track the final page order and
  footers whose ``{page}``/``{total}`` placeholders resolve to the final
  page order at render time.
"""
from __future__ import annotations

import pytest

from scientific_reproduction.rendering.pdf import PdfDocument
from scientific_reproduction.rendering.style import (
    FONT_BODY,
    FONT_BOLD,
    text_width,
)

FONT_NAMES = ["Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"]


def _render_with_text(texts: list[tuple[str, float, float, str]]) -> bytes:
    """Render a one-page document drawing each (text, x, y, font) tuple."""
    doc = PdfDocument(title="fixture")
    page = doc.add_page()
    page.set_font(FONT_BODY, 10)
    for text, x, y, font in texts:
        page.set_font(font, 10)
        page.text(x, y, text)
    return doc.render()


# ---------------------------------------------------------------------------
# document structure -- PDF 1.4 with xref/trailer
# ---------------------------------------------------------------------------


def test_pdf_renders_header_trailer_and_catalog() -> None:
    """The document is a PDF 1.4 with a catalog, page tree and trailer."""
    data = PdfDocument(title="T").render()

    assert data.startswith(b"%PDF-1.4\n")
    assert data.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in data
    assert b"/Type /Pages" in data
    assert b"startxref" in data


def test_pdf_empty_document_renders_with_empty_page_tree() -> None:
    """A document with no pages still renders a valid empty PDF."""
    data = PdfDocument(title="T").render()

    assert data.startswith(b"%PDF-1.4")
    assert b"%%EOF" in data
    assert b"/Kids []" in data


def test_pdf_carries_no_wall_clock_keys() -> None:
    """Determinism: the render never consults the clock, so no timestamp
    or random document id keys appear."""
    data = PdfDocument(title="T").render()

    assert b"/CreationDate" not in data
    assert b"/ModDate" not in data
    assert b"/ID" not in data


# ---------------------------------------------------------------------------
# determinism -- byte-identical repeated renders
# ---------------------------------------------------------------------------


def test_pdf_render_is_byte_identical_across_repeated_renders() -> None:
    """Re-rendering the same call sequence yields identical bytes."""
    first = _render_with_text([("Hello, PDF", 72.0, 100.0, FONT_BODY)])
    second = _render_with_text([("Hello, PDF", 72.0, 100.0, FONT_BODY)])

    assert first == second


def test_pdf_two_page_document_render_is_byte_identical() -> None:
    """A multi-page document with outline entries is byte-identical too."""

    def build() -> bytes:
        doc = PdfDocument(title="T")
        for index in range(2):
            page = doc.add_page()
            page.set_font(FONT_BODY, 10)
            page.text(72.0, 100.0, f"page {index}")
        doc.add_bookmark("First", 0)
        doc.add_bookmark("Second", 1)
        return doc.render()

    assert build() == build()


# ---------------------------------------------------------------------------
# text -- uncompressed streams, escaping, measurement
# ---------------------------------------------------------------------------


def test_pdf_text_appears_verbatim_in_uncompressed_stream() -> None:
    """The content stream is uncompressed, so the rendered text is
    greppable in the file and framed by the text operators."""
    data = _render_with_text([("Hello, PDF", 72.0, 100.0, FONT_BODY)])

    assert b"Hello, PDF" in data
    assert b"BT" in data
    assert b"Tj" in data
    assert b"ET" in data


def test_pdf_escapes_parens_and_backslash_as_octals() -> None:
    """``(`` ``)`` and ``\\`` are escaped as octal escapes so the literal
    characters never unbalance the string syntax."""
    data = _render_with_text([("a(b)\\c", 72.0, 100.0, FONT_BODY)])

    assert b"\\050" in data  # (
    assert b"\\051" in data  # )
    assert b"\\134" in data  # backslash


def test_pdf_encodes_cp1252_and_replaces_unmappable() -> None:
    """WinAnsi characters encode as their cp1252 bytes; characters cp1252
    cannot represent render as ``?`` -- deterministically."""
    data = _render_with_text(
        [("caf\xe9", 72.0, 100.0, FONT_BODY), ("☃", 72.0, 90.0, FONT_BODY)]
    )

    assert b"caf\\351" in data  # é -> cp1252 0xE9, octal
    assert b"\\050\\051" not in data  # no stray unbalanced parens
    # the snowman has no cp1252 byte; it falls back to '?'
    assert b"\x3f" in data


def test_pdf_text_width_uses_afm_ascii_widths() -> None:
    """Text measurement uses the standard Helvetica AFM widths: exact for
    ASCII, a fixed fallback for anything else (deterministic)."""
    # H=0.722 e=0.556 l=0.222 l=0.222 o=0.556 em -> 22.78 pt at 10 pt
    assert text_width("Hello", FONT_BODY, 10) == pytest.approx(22.78)
    assert text_width("0", FONT_BODY, 10) == pytest.approx(5.56)
    assert text_width("WWW", FONT_BOLD, 10) == pytest.approx(28.32)
    assert text_width("l", FONT_BODY, 10) == pytest.approx(2.22)
    # non-ASCII falls back to a fixed positive width
    assert text_width("☃", FONT_BODY, 10) > 0
    assert text_width("☃", FONT_BODY, 10) == text_width(
        "☃", FONT_BODY, 10
    )


# ---------------------------------------------------------------------------
# fonts -- shared base-14 Type1 resources, no embedding
# ---------------------------------------------------------------------------


def test_pdf_fonts_are_base14_type1_without_embedding() -> None:
    """Every used font resolves to a shared base-14 Type1 font object; no
    font file is embedded anywhere in the document."""
    data = _render_with_text([("x", 72.0, 100.0, FONT_BOLD)])

    assert b"/Type /Font" in data
    assert b"/Subtype /Type1" in data
    assert b"/BaseFont /Helvetica-Bold" in data
    assert b"/FontFile" not in data


# ---------------------------------------------------------------------------
# outline, page order, footers
# ---------------------------------------------------------------------------


def test_pdf_outline_bookmarks_reference_pages() -> None:
    """Outline entries carry their titles and destination page objects."""
    doc = PdfDocument(title="T")
    first = doc.add_page()
    first.set_font(FONT_BODY, 10)
    first.text(72.0, 100.0, "one")
    second = doc.add_page()
    second.set_font(FONT_BODY, 10)
    second.text(72.0, 100.0, "two")
    doc.add_bookmark("First section", 0)
    doc.add_bookmark("Second section", 1)
    data = doc.render()

    assert b"/Type /Outlines" in data
    assert b"/Title (First section)" in data
    assert b"/Title (Second section)" in data
    assert b"/Dest" in data


def test_pdf_reorder_pages_changes_content_order() -> None:
    """Reordering pages (TOC pages are laid out last, then moved to the
    front) changes the final content order deterministically."""
    doc = PdfDocument(title="T")
    first = doc.add_page()
    first.set_font(FONT_BODY, 10)
    first.text(72.0, 100.0, "PAGE ONE")
    second = doc.add_page()
    second.set_font(FONT_BODY, 10)
    second.text(72.0, 100.0, "PAGE TWO")
    doc.reorder_pages([1, 0])
    data = doc.render()

    assert data.index(b"PAGE TWO") < data.index(b"PAGE ONE")


def test_pdf_footer_substitutes_final_page_numbers() -> None:
    """Footer placeholders resolve against the *final* page order, so
    footers stay correct after reordering."""
    doc = PdfDocument(title="T")
    first = doc.add_page()
    first.set_font(FONT_BODY, 10)
    first.text(72.0, 100.0, "one")
    first.footer(right="Page {page} of {total}")
    second = doc.add_page()
    second.set_font(FONT_BODY, 10)
    second.text(72.0, 100.0, "two")
    second.footer(right="Page {page} of {total}")
    doc.reorder_pages([1, 0])
    data = doc.render()

    assert b"Page 1 of 2" in data
    assert b"Page 2 of 2" in data


def test_pdf_reorder_rejects_bad_order() -> None:
    """An invalid reorder (wrong length, duplicate or out-of-range
    indices) raises a stable error instead of corrupting the document."""
    doc = PdfDocument(title="T")
    doc.add_page()
    doc.add_page()

    with pytest.raises(ValueError, match="order"):
        doc.reorder_pages([0])
    with pytest.raises(ValueError, match="order"):
        doc.reorder_pages([0, 0])
    with pytest.raises(ValueError, match="order"):
        doc.reorder_pages([0, 2])
