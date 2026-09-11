"""Deterministic stdlib-only PDF 1.4 writer (issue #107 rendering foundation).

This module is the byte-level foundation of the reproduction report
renderer (and, per the issue, of the plan and execution-sheet renderers):
a minimal PDF 1.4 document writer built only on the standard library.

Design choices and why
----------------------
* **Stdlib only.** The runtime is intentionally stdlib-only (no network,
  no third-party packages at render time), so the writer emits the PDF
  primitives directly: uncompressed content streams, base-14 Type1 fonts
  (``Helvetica`` family) that every viewer supplies, xref table and
  trailer.
* **Deterministic bytes.** ``render()`` derives every byte from the API
  calls only. The document never consults the wall clock: no
  ``/CreationDate``, ``/ModDate`` or document ``/ID`` keys are emitted,
  and the producer string is a fixed constant. Repeated renders of the
  same call sequence are byte-identical, which is what the issue's
  "byte-identical for identical state" acceptance requires.
* **Uncompressed content streams.** Text stays greppable in the file
  (tests assert on it directly) and there is no compression state to
  leak into the bytes.
* **Two-pass TOC support.** The report's table of contents is laid out
  last and moved to the front via ``reorder_pages``: TOC page count is a
  pure function of the entry count, so the content pagination is
  identical with or without the TOC pages. Footers resolve their
  ``{page}``/``{total}`` placeholders against the *final* page order at
  render time, and outline bookmarks track pre-reorder page indices and
  are remapped to the final order.

Text encoding is a *font backend* decision (see ``rendering.fonts``):
the default ``Base14Backend`` keeps the historical WinAnsi (cp1252)
path byte-identical -- characters cp1252 cannot represent render as
``?`` (deterministic) -- while ``TrueTypeBackend`` embeds subsetted
TrueType fonts and renders arbitrary Unicode (CJK, Greek, symbols) with
a ``ToUnicode`` CMap so the text stays extractable and searchable.
Measurement uses the same backend metrics, so wrapped layouts never
overflow. ``language`` selects labels only; the *content* decides which
backend a renderer must use.
"""

from __future__ import annotations

from typing import Final

from scientific_reproduction.rendering import fonts as _fonts
from scientific_reproduction.rendering.fonts import Base14Backend, FontBackend
from scientific_reproduction.rendering.style import (
    FONT_BODY,
    FONT_FACES,
    MARGIN_LEFT,
    MARGIN_RIGHT,
    MUTED,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    SMALL_SIZE,
    text_width,
)

#: Fixed producer string -- part of the deterministic byte contract.
PRODUCER: Final[str] = "scientific-reproduction rendering"

#: Font faces in canonical object order (F1..F4).
_FACES: Final[tuple[str, ...]] = FONT_FACES

#: Object numbers (1-based) assigned at render time.
_CATALOG_OBJ: Final[int] = 1
_PAGES_OBJ: Final[int] = 2


def _num(value: float) -> str:
    """Format a PDF number deterministically (always two decimals)."""
    return f"{value:.2f}"


def _escape_text(text: str) -> bytes:
    """Encode text as a WinAnsi PDF string with octal escapes.

    Characters cp1252 cannot represent become ``?`` (0x3F) via
    ``errors="replace"``. Every byte outside the printable ASCII range --
    ``(``, ``)``, backslash, control bytes and the non-ASCII cp1252
    bytes -- is octal-escaped, so the content stream is pure ASCII and
    nothing can unbalance the literal string syntax.
    """
    encoded = text.encode("cp1252", errors="replace")
    out = bytearray()
    for byte in encoded:
        if byte < 0x20 or byte >= 0x7F or byte in (0x28, 0x29, 0x5C):
            out += b"\\" + f"{byte:03o}".encode("ascii")
        else:
            out.append(byte)
    return bytes(out)


class Page:
    """One page: an accumulating content stream plus presentation state.

    All coordinates are PDF points with the origin at the bottom-left;
    ``text`` takes the *baseline* position. State (font, size, fill,
    stroke, line width) applies to subsequent operators until changed.
    """

    def __init__(self, doc: PdfDocument, width: float, height: float) -> None:
        self._doc = doc
        self.width = width
        self.height = height
        self._ops: list[bytes] = []
        self._font_name = FONT_BODY
        self._font_size = 10.0
        self._fill: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._stroke: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._line_width = 0.75
        self.footer_left = ""
        self.footer_right = ""

    # -- presentation state ------------------------------------------------

    def set_font(self, font: str, size: float) -> None:
        """Select a base-14 face (``Helvetica`` family) and size."""
        if font not in _FACES:
            raise ValueError(f"unknown base-14 font: {font!r}")
        self._font_name = font
        self._font_size = size

    def set_fill(self, red: float, green: float, blue: float) -> None:
        """Set the fill color (components in 0..1)."""
        self._fill = (red, green, blue)

    def set_stroke(self, red: float, green: float, blue: float) -> None:
        """Set the stroke color (components in 0..1)."""
        self._stroke = (red, green, blue)

    def set_line_width(self, width: float) -> None:
        """Set the stroke line width in points."""
        self._line_width = width

    # -- operators ----------------------------------------------------------

    def text(self, x: float, y: float, text: str) -> None:
        """Draw ``text`` with its baseline at ``(x, y)`` (current font,
        size and fill color). The string bytes are produced by the
        document's font backend."""
        tag = f"F{_FACES.index(self._font_name) + 1}"
        encoded = self._doc._backend.encode_text(text, self._font_name)
        op = (
            f"BT /{tag} {_num(self._font_size)} Tf "
            f"{_num(self._fill[0])} {_num(self._fill[1])} {_num(self._fill[2])} rg "
            f"{_num(x)} {_num(y)} Td ".encode("ascii")
            + encoded
            + b" Tj ET"
        )
        self._ops.append(op)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        """Stroke a line segment with the current stroke color/width."""
        op = (
            f"{_num(self._stroke[0])} {_num(self._stroke[1])} "
            f"{_num(self._stroke[2])} RG {_num(self._line_width)} w "
            f"{_num(x1)} {_num(y1)} m {_num(x2)} {_num(y2)} l S"
        ).encode("ascii")
        self._ops.append(op)

    def rect(self, x: float, y: float, width: float, height: float) -> None:
        """Stroke a rectangle with the current stroke color/width."""
        op = (
            f"{_num(self._stroke[0])} {_num(self._stroke[1])} "
            f"{_num(self._stroke[2])} RG {_num(self._line_width)} w "
            f"{_num(x)} {_num(y)} {_num(width)} {_num(height)} re S"
        ).encode("ascii")
        self._ops.append(op)

    def fill_rect(
        self, x: float, y: float, width: float, height: float
    ) -> None:
        """Fill a rectangle with the current fill color."""
        op = (
            f"{_num(self._fill[0])} {_num(self._fill[1])} "
            f"{_num(self._fill[2])} rg "
            f"{_num(x)} {_num(y)} {_num(width)} {_num(height)} re f"
        ).encode("ascii")
        self._ops.append(op)

    # -- footers ------------------------------------------------------------

    def footer(
        self, left: str = "", right: str = "Page {page} of {total}"
    ) -> None:
        """Set the footer texts; ``{page}``/``{total}`` placeholders are
        substituted against the final page order at render time."""
        self.footer_left = left
        self.footer_right = right

    # -- render-time helpers -------------------------------------------------

    def _content(self, footer_left: str, footer_right: str) -> bytes:
        """Assemble the content stream: page operators plus the footer."""
        parts: list[bytes] = list(self._ops)
        if footer_left or footer_right:
            parts.append(b"q")
            parts.append(
                (
                    f"{_num(MUTED[0])} {_num(MUTED[1])} {_num(MUTED[2])} rg "
                    f"/F1 {_num(SMALL_SIZE)} Tf"
                ).encode("ascii")
            )
            baseline = 30.0
            if footer_left:
                parts.append(
                    (
                        f"BT {_num(MARGIN_LEFT)} {_num(baseline)} Td "
                    ).encode("ascii")
                    + self._doc._backend.encode_text(footer_left, FONT_BODY)
                    + b" Tj ET"
                )
            if footer_right:
                right_x = (
                    self.width
                    - MARGIN_RIGHT
                    - text_width(footer_right, FONT_BODY, SMALL_SIZE)
                )
                parts.append(
                    (
                        f"BT {_num(right_x)} {_num(baseline)} Td "
                    ).encode("ascii")
                    + self._doc._backend.encode_text(footer_right, FONT_BODY)
                    + b" Tj ET"
                )
            parts.append(b"Q")
        return b"\n".join(parts) + b"\n"


class PdfDocument:
    """A deterministic PDF document: pages, outline and byte render.

    ``font_backend`` selects the text encoding path: the default
    ``Base14Backend`` keeps the historical WinAnsi bytes, while
    ``TrueTypeBackend`` embeds subsetted TrueType fonts and renders
    arbitrary Unicode with a ToUnicode CMap.
    """

    def __init__(
        self,
        title: str = "",
        producer: str = PRODUCER,
        font_backend: FontBackend | None = None,
    ) -> None:
        self.title = title
        self.producer = producer
        self._backend: FontBackend = font_backend or Base14Backend()
        self._render_programs: list[bytes] = []
        self._pages: list[Page] = []
        #: Bookmark (title, pre-reorder page index); remapped at render.
        self._bookmarks: list[tuple[str, int]] = []
        #: Final page order (pre-reorder indices); identity until set.
        self._order: list[int] = []

    @property
    def pages(self) -> tuple[Page, ...]:
        """The pages in creation order (read-only)."""
        return tuple(self._pages)

    def add_page(
        self,
        width: float = PAGE_WIDTH,
        height: float = PAGE_HEIGHT,
    ) -> Page:
        """Append a page and return it for drawing."""
        page = Page(self, width, height)
        self._pages.append(page)
        return page

    def add_bookmark(self, title: str, page_index: int) -> None:
        """Register an outline entry targeting the page at
        ``page_index`` *in creation order*; the destination is remapped
        to the final page order at render time."""
        if page_index < 0 or page_index >= len(self._pages):
            raise ValueError(
                f"bookmark page_index {page_index} out of range "
                f"(0..{len(self._pages) - 1})"
            )
        self._bookmarks.append((title, page_index))

    def reorder_pages(self, order: list[int] | tuple[int, ...]) -> None:
        """Set the final page order as a permutation of creation-order
        indices (used to move TOC pages laid out last to the front)."""
        if list(order) == list(range(len(self._pages))):
            self._order = list(order)
            return
        if len(order) != len(self._pages) or sorted(order) != list(
            range(len(self._pages))
        ):
            raise ValueError(
                "order must be a permutation of page indices "
                f"0..{len(self._pages) - 1}"
            )
        self._order = list(order)

    # -- rendering ----------------------------------------------------------

    def render(self) -> bytes:
        """Serialize the document: objects, xref table, trailer.

        Every object (catalog, page tree, pages, content streams, fonts,
        outline) is emitted in a fixed order with fixed numbers, so the
        output is a pure function of the API calls made.
        """
        order = self._order or list(range(len(self._pages)))
        pages = [self._pages[i] for i in order]
        total = len(pages)

        # Embedded font programs are subset once per render (their bytes
        # are a pure function of the draw sequence); object-number
        # helpers below depend on the program count.
        self._render_programs: list[bytes] = (
            [] if self._backend.is_base14 else self._backend.pdf_program_objects()
        )

        parts: list[bytes] = []
        offsets: list[int] = []

        def obj(body: bytes) -> int:
            number = len(offsets) + 1
            offsets.append(_offset())
            parts.append(
                f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
            )
            return number

        def _offset() -> int:
            # Absolute position in the final output: the "%PDF-1.4\n"
            # header (9 bytes) precedes the emitted objects.
            return len(b"%PDF-1.4\n") + sum(len(part) for part in parts)

        # 1: catalog, 2: page tree.
        obj(
            (
                f"<< /Type /Catalog /Pages {_PAGES_OBJ} 0 R "
                f"/Outlines {self._outline_root_number()} 0 R "
                f"/PageMode /UseOutlines >>"
            ).encode("ascii")
        )
        kids = " ".join(
            f"{_PAGES_OBJ + 1 + index} 0 R" for index in range(total)
        )
        obj(
            (
                f"<< /Type /Pages /Kids [{kids}] /Count {total} >>"
            ).encode("ascii")
        )

        # Page objects and content streams (streams follow their pages so
        # page N references stream N in the same order).
        for index, page in enumerate(pages):
            obj(
                (
                    f"<< /Type /Page /Parent {_PAGES_OBJ} 0 R "
                    f"/MediaBox [0 0 {_num(page.width)} {_num(page.height)}] "
                    f"/Resources << /Font << "
                    + " ".join(
                        f"/F{face_index + 1} {self._font_object_number(face_index)} 0 R"
                        for face_index in range(len(_FACES))
                    )
                    + f" >> >> /Contents {self._content_stream_number(index)} 0 R >>"
                ).encode("ascii")
            )

        for index, page in enumerate(pages):
            content = page._content(
                footer_left=page.footer_left,
                footer_right=(
                    page.footer_right.format(
                        page=index + 1, total=total
                    )
                    if page.footer_right
                    else ""
                ),
            )
            obj(
                f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                + content
                + b"endstream"
            )

        # Font resources. The base-14 path emits the four Type1 objects
        # (byte-identical to the historical writer); the Unicode path
        # emits embedded programs, Type0 fonts, CIDFontType2 descendants,
        # descriptors and ToUnicode CMaps in a fixed order.
        if self._backend.is_base14:
            for body in self._backend.pdf_font_objects():
                obj(body)
        else:
            for body in self._render_programs:
                obj(body)
            desc_numbers = [
                self._descendant_object_number(i) for i in range(len(_FACES))
            ]
            touni_numbers = [
                self._tounicode_object_number(i) for i in range(len(_FACES))
            ]
            for body in self._backend.pdf_font_objects(
                desc_numbers, touni_numbers
            ):
                obj(body)
            for index, body in enumerate(
                self._backend.face_descendant_bodies()
            ):
                obj(
                    body.replace(
                        b"@@FD@@",
                        str(self._descriptor_object_number(index)).encode(
                            "ascii"
                        ),
                    )
                )
            prog_numbers = [
                self._program_object_number(k)
                for k in range(len(self._render_programs))
            ]
            for body in self._backend.face_descriptor_bodies(prog_numbers):
                obj(body)
            for body in self._backend.pdf_tounicode_objects():
                obj(body)

        # Outline root + items.
        if self._bookmarks:
            first_item = self._outline_item_number(0)
            last_item = self._outline_item_number(len(self._bookmarks) - 1)
            outline_root = obj(
                (
                    f"<< /Type /Outlines /First {first_item} 0 R "
                    f"/Last {last_item} 0 R /Count {len(self._bookmarks)} >>"
                ).encode("ascii")
            )
            for index, (title, page_index) in enumerate(self._bookmarks):
                prev = (
                    f"/Prev {self._outline_item_number(index - 1)} 0 R"
                    if index > 0
                    else ""
                )
                next_ = (
                    f"/Next {self._outline_item_number(index + 1)} 0 R"
                    if index < len(self._bookmarks) - 1
                    else ""
                )
                final_page = order.index(page_index)
                obj(
                    b"<< /Title "
                    + self._backend.encode_literal(title)
                    + (
                        f" /Parent {outline_root} 0 R {prev} {next_} "
                        f"/Dest [{_PAGES_OBJ + 1 + final_page} 0 R /FitH null] >>"
                    ).encode("ascii")
                )
        else:
            obj(b"<< /Type /Outlines /Count 0 >>")

        # Cross-reference table and trailer. ``_offset()`` already counts
        # the "%PDF-1.4\n" header, so the pointer names the byte where
        # "xref" begins in the final output.
        startxref = _offset()
        xref = bytearray(
            f"xref\n0 {len(offsets) + 1}\n".encode("ascii")
        )
        xref += b"0000000000 65535 f \n"
        for offset in offsets:
            xref += f"{offset:010d} 00000 n \n".encode("ascii")
        trailer = (
            f"trailer\n<< /Size {len(offsets) + 1} /Root {_CATALOG_OBJ} 0 R >>\n"
            f"startxref\n{startxref}\n%%EOF\n"
        ).encode("ascii")

        return b"%PDF-1.4\n" + b"".join(parts) + bytes(xref) + trailer

    # -- object-number helpers (all layout is fixed) --------------------------

    def _program_count(self) -> int:
        """Number of embedded font-program objects (0 on base-14)."""
        return len(self._render_programs)

    def _font_objects_start(self) -> int:
        """Object number of the first font dictionary (F1)."""
        return 2 + 2 * len(self._pages) + 1 + self._program_count()

    def _outline_root_number(self) -> int:
        """Object number of the outline root dict.

        Base-14: catalog, pages, page dicts, content streams, 4 fonts,
        then the root. Unicode adds programs (P), 4 descendants, 4
        descriptors and 4 ToUnicode CMaps between the fonts and the
        root.
        """
        extra = 0 if self._backend.is_base14 else 12
        return self._font_objects_start() + len(_FACES) + extra

    def _content_stream_number(self, page_index: int) -> int:
        """Object number of page ``page_index``'s content stream."""
        return 2 + len(self._pages) + 1 + page_index

    def _font_object_number(self, face_index: int) -> int:
        """Object number of font ``face_index``."""
        return self._font_objects_start() + face_index

    def _program_object_number(self, index: int) -> int:
        """Object number of embedded font program ``index``."""
        return 2 + 2 * len(self._pages) + 1 + index

    def _descendant_object_number(self, face_index: int) -> int:
        """Object number of the CIDFontType2 descendant for ``face_index``."""
        return self._font_objects_start() + len(_FACES) + face_index

    def _descriptor_object_number(self, face_index: int) -> int:
        """Object number of the FontDescriptor for ``face_index``."""
        return self._font_objects_start() + 2 * len(_FACES) + face_index

    def _tounicode_object_number(self, face_index: int) -> int:
        """Object number of the ToUnicode CMap for ``face_index``."""
        return self._font_objects_start() + 3 * len(_FACES) + face_index

    def _outline_item_number(self, index: int) -> int:
        """Object number of outline item ``index``."""
        return self._outline_root_number() + 1 + index
