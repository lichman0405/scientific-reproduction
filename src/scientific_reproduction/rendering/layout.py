"""Flow layout engine for the deterministic PDF renderers (issue #107).

``FlowLayout`` turns high-level content -- headings, paragraphs,
tables and verdict callouts -- into the page/operator calls of
``rendering.pdf``. It owns:

* **Pagination.** Content flows top-down; a block that does not fit on
  the current page starts a fresh page. Tables paginate per row-chunk
  with the header row repeated, so a table never overflows.
* **Determinism.** Layout consumes only its arguments and the visual
  tokens of ``rendering.style`` -- no wall clock, no randomness -- so
  identical calls render byte-identical PDFs.
* **Two-pass TOC support.** Every ``heading`` records its
  (title, creation-order page index) in ``headings`` and registers an
  outline bookmark; ``toc_page_count`` derives the number of TOC pages
  purely from the entry count, which is what lets the report renderer
  lay out the TOC *after* the content and move it to the front without
  changing the content pagination (the ``{page}``/``{total}`` footers
  resolve against the final page order at render time).

Text is word-wrapped with the AFM measurement of ``rendering.style``;
a single word wider than the line breaks at character level.
"""

from __future__ import annotations

import re
from typing import Final, NamedTuple

from scientific_reproduction.rendering.pdf import PdfDocument
from scientific_reproduction.rendering.style import (
    BODY_LINE_HEIGHT,
    BODY_SIZE,
    CALLOUT_PADDING,
    FONT_BODY,
    FONT_BOLD,
    FONT_FACES,
    HEADING_FONT,
    HEADING_LINE_HEIGHTS,
    HEADING_SIZES,
    INK,
    MARGIN_BOTTOM,
    MARGIN_LEFT,
    MARGIN_RIGHT,
    MARGIN_TOP,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    RULE,
    TABLE_HEADER_FILL,
    TABLE_ROW_PADDING,
    TABLE_RULE,
    text_width,
    verdict_style,
)

#: Gap after a level-1 heading's underline rule.
_HEADING_RULE_GAP: Final[float] = 6.0
_HEADING_GAP: Final[float] = 4.0
_PARAGRAPH_GAP: Final[float] = 4.0
_RULE_GAP: Final[float] = 6.0
_CALLOUT_BODY_GAP: Final[float] = 4.0


class Heading(NamedTuple):
    """A rendered heading: its title and the creation-order page index
    it starts on (the TOC entry page number is the final-order index,
    which the report renderer computes after reordering)."""

    title: str
    page_index: int


#: CJK ideographs / kana / punctuation / fullwidth forms: must be
#: breakable per character, otherwise a space-less CJK run becomes one
#: unbreakable "word" that blows up table columns and page margins.
_CJK_BREAK_RE = re.compile(
    r"([⺀-鿿豈-﫿　-〿＀-￯])"
)


def _breakable_tokens(text: str) -> list[str]:
    """Tokenize text for line layout: space-separated ASCII words stay
    intact; every CJK character/punctuation becomes its own token."""
    tokens: list[str] = []
    for word in text.split():
        for part in _CJK_BREAK_RE.split(word):
            if part:
                tokens.append(part)
    return tokens


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Word-wrap ``text`` into lines that fit ``max_width``.

    ASCII text breaks on spaces; CJK text additionally breaks per
    character (no visible gaps are inserted). A single ASCII word wider
    than the line breaks at character level; the empty string wraps to
    no lines.
    """
    if not text:
        return []
    lines: list[str] = []
    current = ""
    last_cjk: bool | None = None
    for tok in _breakable_tokens(text):
        cjk = bool(_CJK_BREAK_RE.match(tok))
        glue = "" if not current or last_cjk or cjk else " "
        candidate = current + glue + tok
        if text_width(candidate, font, size) <= max_width:
            current = candidate
            last_cjk = cjk
            continue
        if not current:
            # One ASCII word wider than the whole line: hard-char break.
            chunk = ""
            for char in tok:
                trial = chunk + char
                if chunk and text_width(trial, font, size) > max_width:
                    lines.append(chunk)
                    chunk = char
                else:
                    chunk = trial
            current = chunk
            last_cjk = False
        else:
            lines.append(current)
            current = tok
            last_cjk = cjk
    if current:
        lines.append(current)
    return lines


class FlowLayout:
    """High-level flow layout over a :class:`PdfDocument`."""

    def __init__(
        self,
        doc: PdfDocument,
        *,
        margin_left: float = MARGIN_LEFT,
        margin_right: float = MARGIN_RIGHT,
        margin_top: float = MARGIN_TOP,
        margin_bottom: float = MARGIN_BOTTOM,
        footer_left: str = "",
        footer_right: str = "Page {page} of {total}",
    ) -> None:
        self._doc = doc
        self._margin_left = margin_left
        self._margin_right = margin_right
        self._margin_top = margin_top
        self._margin_bottom = margin_bottom
        self._footer_left = footer_left
        self._footer_right = footer_right
        #: Distance from the top margin line to the next content block.
        self._top = 0.0
        self._headings: list[Heading] = []

    # -- state ---------------------------------------------------------------

    @property
    def headings(self) -> tuple[Heading, ...]:
        """Every heading rendered so far, in order."""
        return tuple(self._headings)

    @property
    def page_count(self) -> int:
        """Number of pages created so far."""
        return len(self._doc.pages)

    @property
    def content_width(self) -> float:
        """Usable width between the margins."""
        return PAGE_WIDTH - self._margin_left - self._margin_right

    # -- pagination helpers ----------------------------------------------------

    def _new_page(self) -> None:
        page = self._doc.add_page()
        page.footer(left=self._footer_left, right=self._footer_right)
        page.set_font(FONT_BODY, BODY_SIZE)
        self._top = 0.0

    def _ensure(self, block_height: float) -> None:
        """Start a fresh page when the next block does not fit."""
        if not self._doc.pages:
            self._new_page()
            return
        if self._top > 0 and (
            self._margin_top + self._top + block_height
            > PAGE_HEIGHT - self._margin_bottom
        ):
            self._new_page()

    def _baseline(self, size: float) -> float:
        """PDF y of the next line's baseline (top-down cursor)."""
        return PAGE_HEIGHT - self._margin_top - self._top - size

    def _line(
        self,
        text: str,
        font: str,
        size: float,
        x: float,
        color: tuple[float, float, float],
        *,
        line_height: float,
    ) -> None:
        """Draw one line and advance the cursor."""
        self._ensure(line_height)
        page = self._doc.pages[-1]
        page.set_font(font, size)
        page.set_fill(*color)
        page.text(x, self._baseline(size), text)
        self._top += line_height

    # -- content blocks ---------------------------------------------------------

    def heading(self, text: str, level: int = 1) -> None:
        """Draw a heading (bold, sizes from ``HEADING_SIZES``), record it
        in ``headings`` and register its outline bookmark."""
        level = max(1, min(level, 3))
        size = HEADING_SIZES[level - 1]
        line_height = size * HEADING_LINE_HEIGHTS[level - 1]
        self._line(
            text,
            HEADING_FONT,
            size,
            self._margin_left,
            INK,
            line_height=line_height,
        )
        page_index = len(self._doc.pages) - 1
        if level == 1:
            page = self._doc.pages[-1]
            rule_y = PAGE_HEIGHT - self._margin_top - self._top - 2.0
            page.set_stroke(*RULE)
            page.set_line_width(0.75)
            page.line(
                self._margin_left,
                rule_y,
                self._margin_left + self.content_width,
                rule_y,
            )
            self._top += _HEADING_RULE_GAP
        else:
            self._top += _HEADING_GAP
        self._headings.append(Heading(text, page_index))
        self._doc.add_bookmark(text, page_index)

    def paragraph(
        self,
        text: str,
        *,
        font: str = FONT_BODY,
        size: float = BODY_SIZE,
    ) -> None:
        """Draw a word-wrapped paragraph of body text."""
        normalized = " ".join(text.split())
        line_height = size * BODY_LINE_HEIGHT
        for line in _wrap(normalized, font, size, self.content_width):
            self._line(
                line,
                font,
                size,
                self._margin_left,
                INK,
                line_height=line_height,
            )
        self._top += _PARAGRAPH_GAP

    def rule(self) -> None:
        """Draw a full-width hairline rule."""
        self._ensure(_RULE_GAP)
        page = self._doc.pages[-1]
        y = PAGE_HEIGHT - self._margin_top - self._top - 1.0
        page.set_stroke(*RULE)
        page.set_line_width(0.5)
        page.line(
            self._margin_left, y, self._margin_left + self.content_width, y
        )
        self._top += _RULE_GAP

    def spacer(self, height: float) -> None:
        """Advance the cursor by ``height`` points (no drawing)."""
        self._ensure(height)
        self._top += height

    def page_break(self) -> None:
        """Start a fresh page (no-op when already at the top of one)."""
        if self._top > 0:
            self._new_page()

    def table(
        self,
        headers: list[str] | tuple[str, ...],
        rows: list[list[str]] | tuple[tuple[str, ...], ...],
        *,
        widths: list[float] | tuple[float, ...] | None = None,
        cell_font: str = FONT_BODY,
    ) -> None:
        """Draw a table: filled header row (bold), wrapped cells and
        border/column rules. Rows paginate across pages with the header
        repeated; a table never overflows a page."""
        if cell_font not in FONT_FACES:
            raise ValueError(f"unknown font: {cell_font!r}")
        ncols = len(headers)
        if ncols == 0:
            return
        for index, row in enumerate(rows):
            if len(row) != ncols:
                raise ValueError(
                    f"row {index} has {len(row)} cells, expected {ncols}"
                )
        if widths is None:
            widths = [self.content_width / ncols] * ncols
        column_widths = [float(width) for width in widths]
        # ``widths`` are absolute points, never fractions. A fractional
        # value would silently collapse every column to its token
        # minimum (a column of 0.30 pt widens no one), turning the table
        # into a narrow vertical run -- fail loudly instead.
        if any(width < 1.0 for width in column_widths):
            raise ValueError(
                "table widths must be absolute points (got "
                f"fraction-like widths {widths!r}); scale your "
                "fractions by ``layout.content_width``"
            )
        # Every column must hold its widest unbreakable token so
        # identifiers (goal/run/artifact ids) never break mid-token:
        # satisfy the token minimums first, then hand the leftover width
        # out proportionally to the requested widths. Only when the
        # minimums together exceed the page width do columns shrink
        # below a token minimum (breaking is then unavoidable).
        minimums: list[float] = []
        for column in range(ncols):
            widest = text_width(headers[column], FONT_BOLD, BODY_SIZE)
            for row in rows:
                for token in _breakable_tokens(row[column]):
                    widest = max(
                        widest, text_width(token, cell_font, BODY_SIZE)
                    )
            minimums.append(widest + 2 * TABLE_ROW_PADDING)
        # Start from the requested widths (the report renderer tunes
        # them so phrases and identifiers stay intact) and grow each
        # column that needs more room for its widest unbreakable token.
        column_widths = [
            max(requested_width, minimum)
            for requested_width, minimum in zip(column_widths, minimums)
        ]
        total = sum(column_widths)
        if total > self.content_width:
            # Overflow: first give back slack, widest first, shrinking
            # columns toward their token minimums -- no token breaks
            # while any slack remains.
            deficit = total - self.content_width
            for index in sorted(
                range(ncols),
                key=lambda i: column_widths[i] - minimums[i],
                reverse=True,
            ):
                if deficit <= 0:
                    break
                slack = column_widths[index] - minimums[index]
                if slack <= 0:
                    break
                cut = min(slack, deficit)
                column_widths[index] -= cut
                deficit -= cut
            if deficit > 0:
                # Even the bare minimums exceed the page: not every
                # token can stay intact. Sacrifice the widest columns
                # first -- cut the largest minimums down to a floor of
                # a few characters per line -- so short meaningful
                # tokens (ids, timestamps, event types) keep their full
                # width and only pathological oversized columns break.
                floor = min(
                    self.content_width / 12, self.content_width / ncols
                )
                for index in sorted(
                    range(ncols),
                    key=lambda i: column_widths[i],
                    reverse=True,
                ):
                    if deficit <= 0:
                        break
                    if column_widths[index] <= floor:
                        break
                    cut = min(column_widths[index] - floor, deficit)
                    column_widths[index] -= cut
                    deficit -= cut
        x_edges = [self._margin_left]
        for width in column_widths:
            x_edges.append(x_edges[-1] + width)

        cell_line_height = BODY_SIZE * BODY_LINE_HEIGHT

        def wrapped_rows(
            font: str, cells: list[str]
        ) -> list[list[str]]:
            return [
                _wrap(
                    cell,
                    font,
                    BODY_SIZE,
                    column_widths[index] - 2 * TABLE_ROW_PADDING,
                )
                for index, cell in enumerate(cells)
            ]

        header_cells = wrapped_rows(FONT_BOLD, list(headers))
        header_height = (
            max((len(lines) for lines in header_cells), default=1)
            * cell_line_height
            + 2 * TABLE_ROW_PADDING
        )
        row_cells = [
            wrapped_rows(cell_font, list(row)) for row in rows
        ]
        row_heights = [
            max((len(lines) for lines in cells), default=1)
            * cell_line_height
            + 2 * TABLE_ROW_PADDING
            for cells in row_cells
        ]
        usable_height = PAGE_HEIGHT - self._margin_top - self._margin_bottom

        def draw_cell_text(
            cells: list[list[str]], top: float
        ) -> None:
            page = self._doc.pages[-1]
            for column, lines in enumerate(cells):
                baseline = top - TABLE_ROW_PADDING - BODY_SIZE
                for line in lines:
                    page.text(
                        x_edges[column] + TABLE_ROW_PADDING, baseline, line
                    )
                    baseline -= cell_line_height

        def draw_header(top: float) -> None:
            page = self._doc.pages[-1]
            page.set_fill(*TABLE_HEADER_FILL)
            page.fill_rect(
                self._margin_left, top - header_height, self.content_width, header_height
            )
            page.set_font(FONT_BOLD, BODY_SIZE)
            page.set_fill(*INK)
            draw_cell_text(header_cells, top)
            for edge in x_edges[1:-1]:
                page.set_stroke(*TABLE_RULE)
                page.set_line_width(0.5)
                page.line(edge, top, edge, top - header_height)

        # Greedily chunk rows so header + chunk fits one page.
        chunks: list[list[int]] = [[]]
        used = header_height
        for index, height in enumerate(row_heights):
            if chunks[-1] and used + height > usable_height:
                chunks.append([])
                used = header_height
            chunks[-1].append(index)
            used += height

        for chunk in chunks:
            block_height = header_height + sum(
                row_heights[index] for index in chunk
            )
            self._ensure(block_height)
            page = self._doc.pages[-1]
            top = PAGE_HEIGHT - self._margin_top - self._top
            draw_header(top)
            row_top = top - header_height
            for index in chunk:
                height = row_heights[index]
                row_top -= height
                page.set_font(cell_font, BODY_SIZE)
                page.set_fill(*INK)
                draw_cell_text(row_cells[index], row_top + height)
                page.set_stroke(*TABLE_RULE)
                page.set_line_width(0.5)
                page.line(
                    self._margin_left,
                    row_top,
                    self._margin_left + self.content_width,
                    row_top,
                )
            page.set_stroke(*TABLE_RULE)
            page.set_line_width(0.75)
            page.rect(
                self._margin_left,
                top - block_height,
                self.content_width,
                block_height,
            )
            self._top += block_height

    def callout(self, verdict: str, title: str, body: str) -> None:
        """Draw a verdict callout: pale background fill, colored border
        and title, wrapped body. The colors come from
        ``verdict_style`` (PASS/FAIL/INCONCLUSIVE; unknown -> neutral)."""
        foreground, background = verdict_style(verdict)
        width = self.content_width
        title_height = BODY_SIZE * BODY_LINE_HEIGHT
        body_lines = _wrap(
            " ".join(body.split()),
            FONT_BODY,
            BODY_SIZE,
            width - 2 * CALLOUT_PADDING,
        )
        body_height = len(body_lines) * BODY_SIZE * BODY_LINE_HEIGHT
        height = (
            2 * CALLOUT_PADDING + title_height + _CALLOUT_BODY_GAP + body_height
        )
        self._ensure(height)
        page = self._doc.pages[-1]
        block_top = PAGE_HEIGHT - self._margin_top - self._top
        page.set_fill(*background)
        page.fill_rect(self._margin_left, block_top - height, width, height)
        page.set_stroke(*foreground)
        page.set_line_width(1.0)
        page.rect(self._margin_left, block_top - height, width, height)
        baseline = block_top - CALLOUT_PADDING - BODY_SIZE
        page.set_font(FONT_BOLD, BODY_SIZE)
        page.set_fill(*foreground)
        page.text(self._margin_left + CALLOUT_PADDING, baseline, title)
        baseline -= title_height + _CALLOUT_BODY_GAP
        page.set_font(FONT_BODY, BODY_SIZE)
        page.set_fill(*INK)
        for line in body_lines:
            page.text(self._margin_left + CALLOUT_PADDING, baseline, line)
            baseline -= BODY_SIZE * BODY_LINE_HEIGHT
        self._top += height

    # -- table of contents ------------------------------------------------------

    def toc_entry(self, index: int, title: str, page_number: int) -> None:
        """Draw one TOC line: ``index.  title`` with the target page
        number right-aligned on the same line."""
        line_height = BODY_SIZE * BODY_LINE_HEIGHT
        self._ensure(line_height)
        page = self._doc.pages[-1]
        page.set_font(FONT_BODY, BODY_SIZE)
        page.set_fill(*INK)
        baseline = self._baseline(BODY_SIZE)
        page.text(self._margin_left, baseline, f"{index}.  {title}")
        page_number_text = str(page_number)
        right = (
            self._margin_left
            + self.content_width
            - text_width(page_number_text, FONT_BODY, BODY_SIZE)
        )
        page.text(right, baseline, page_number_text)
        self._top += line_height

    def toc_page_count(self, entries: int) -> int:
        """Number of TOC pages for the title paragraph plus ``entries``
        entries -- a pure function of the entry count, so the report
        renderer knows the TOC size before laying it out (two-pass
        rendering)."""
        if entries <= 0:
            return 0
        usable_height = PAGE_HEIGHT - self._margin_top - self._margin_bottom
        body_line = BODY_SIZE * BODY_LINE_HEIGHT
        title_height = HEADING_SIZES[1] * BODY_LINE_HEIGHT + _PARAGRAPH_GAP
        first_capacity = max(int((usable_height - title_height) // body_line), 0)
        if entries <= first_capacity:
            return 1
        remaining = entries - first_capacity
        per_page = int(usable_height // body_line)
        return 1 + (remaining + per_page - 1) // per_page
