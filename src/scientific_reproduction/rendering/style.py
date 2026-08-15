"""Shared deterministic visual system for the PDF renderers (issue #107).

The final reproduction report renderer, the plan renderer and the
execution-sheet renderers share one visual system so every rendered
artifact looks like one product family: the same page geometry, the same
fonts (base-14 ``Helvetica`` family, stdlib-only PDF -- nothing is
embedded), the same color tokens and the same verdict colors. This module
is the single source of those tokens, so the parallel renderers
(``feat/plan-pdf-renderer``, ``feat/execution-sheet-renderers``) reuse it
instead of redefining colors or widths.

Everything here is a constant or a pure function of its inputs -- no wall
clock, no randomness -- so any renderer built on it is deterministic by
construction.

Text measurement (``text_width``) uses the standard Adobe Helvetica AFM
widths for the printable ASCII range (exact, so wrapped layouts are
predictable) and a fixed fallback for every other character (including
cp1252 punctuation such as en/em dashes or the degree sign); the fallback
is deterministic, which is what the byte-identical-render guarantee
needs. The width tables below transcribe the public Helvetica /
Helvetica-Bold AFM values.

The verdict mapping (``verdict_style``) normalizes a verdict string --
``PASS`` / ``FAIL`` / ``INCONCLUSIVE`` are the frozen scientific-review
vocabulary values the report renderer and the execution sheets print --
to a (foreground, background) color pair; anything else falls back to the
neutral gray, so an unknown value never renders green or red.
"""

from __future__ import annotations

from typing import Final

#: A4 portrait page geometry, in PDF points (1/72 inch).
PAGE_WIDTH: Final[float] = 595.28
PAGE_HEIGHT: Final[float] = 841.89

#: Content margins, in points.
MARGIN_LEFT: Final[float] = 54.0
MARGIN_RIGHT: Final[float] = 54.0
MARGIN_TOP: Final[float] = 54.0
MARGIN_BOTTOM: Final[float] = 54.0

#: Base-14 font faces (no embedding: every PDF viewer supplies them).
FONT_HELVETICA: Final[str] = "Helvetica"
FONT_HELVETICA_BOLD: Final[str] = "Helvetica-Bold"
FONT_HELVETICA_OBLIQUE: Final[str] = "Helvetica-Oblique"
FONT_HELVETICA_BOLD_OBLIQUE: Final[str] = "Helvetica-BoldOblique"

#: All base-14 faces the renderers accept, in canonical object order.
FONT_FACES: Final[tuple[str, ...]] = (
    FONT_HELVETICA,
    FONT_HELVETICA_BOLD,
    FONT_HELVETICA_OBLIQUE,
    FONT_HELVETICA_BOLD_OBLIQUE,
)

#: Canonical face names the layout code refers to.
FONT_BODY: Final[str] = FONT_HELVETICA
FONT_BOLD: Final[str] = FONT_HELVETICA_BOLD
FONT_ITALIC: Final[str] = FONT_HELVETICA_OBLIQUE

#: Body text size and line height multiplier (line height = size * ratio).
BODY_SIZE: Final[float] = 9.5
BODY_LINE_HEIGHT: Final[float] = 1.35

#: Heading sizes (level 1..3) and their line-height multipliers.
HEADING_SIZES: Final[tuple[float, float, float]] = (15.0, 12.0, 10.5)
HEADING_LINE_HEIGHTS: Final[tuple[float, float, float]] = (1.25, 1.3, 1.35)
HEADING_FONT: Final[str] = FONT_HELVETICA_BOLD

#: Small label/caption size.
SMALL_SIZE: Final[float] = 8.0

#: Table geometry.
TABLE_HEADER_FILL: Final[tuple[float, float, float]] = (0.90, 0.92, 0.94)
TABLE_RULE: Final[tuple[float, float, float]] = (0.80, 0.82, 0.85)
TABLE_ROW_PADDING: Final[float] = 3.0

#: Callout geometry.
CALLOUT_PADDING: Final[float] = 8.0

# ---------------------------------------------------------------------------
# Color tokens -- (r, g, b) in 0..1, matching the execution-sheet palette
# ---------------------------------------------------------------------------

#: Ink (body text) and muted secondary text.
INK: Final[tuple[float, float, float]] = (0.13, 0.14, 0.16)
MUTED: Final[tuple[float, float, float]] = (0.42, 0.44, 0.47)

#: Hairline rules and accents.
RULE: Final[tuple[float, float, float]] = (0.80, 0.82, 0.85)
ACCENT: Final[tuple[float, float, float]] = (0.16, 0.35, 0.55)

#: Verdict colors: PASS green, FAIL red, INCONCLUSIVE amber, neutral gray
#: for everything unknown. Each verdict carries a strong foreground and a
#: pale background tint for its callout.
PASS_COLOR: Final[tuple[float, float, float]] = (0.15, 0.52, 0.26)
PASS_BG: Final[tuple[float, float, float]] = (0.91, 0.97, 0.92)
FAIL_COLOR: Final[tuple[float, float, float]] = (0.72, 0.16, 0.14)
FAIL_BG: Final[tuple[float, float, float]] = (0.98, 0.92, 0.91)
INCONCLUSIVE_COLOR: Final[tuple[float, float, float]] = (0.74, 0.53, 0.06)
INCONCLUSIVE_BG: Final[tuple[float, float, float]] = (1.0, 0.97, 0.89)
NEUTRAL_COLOR: Final[tuple[float, float, float]] = (0.36, 0.39, 0.43)
NEUTRAL_BG: Final[tuple[float, float, float]] = (0.94, 0.95, 0.96)

#: Verdict string -> (foreground, background) callout colors. Keys are the
#: uppercase frozen vocabulary values; lookup goes through ``verdict_style``
#: which normalizes the input.
VERDICT_STYLES: Final[dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]]] = {
    "PASS": (PASS_COLOR, PASS_BG),
    "FAIL": (FAIL_COLOR, FAIL_BG),
    "INCONCLUSIVE": (INCONCLUSIVE_COLOR, INCONCLUSIVE_BG),
}


def verdict_style(
    verdict: str,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return the (foreground, background) color pair for a verdict.

    The lookup is case-insensitive over the frozen vocabulary
    (``PASS``/``FAIL``/``INCONCLUSIVE``); any other value -- including the
    empty string -- falls back to the neutral gray pair, so an unrecorded
    verdict never renders as success or failure.
    """
    return VERDICT_STYLES.get(verdict.upper(), (NEUTRAL_COLOR, NEUTRAL_BG))


# ---------------------------------------------------------------------------
# Font widths -- standard Helvetica AFM, printable ASCII (exact)
# ---------------------------------------------------------------------------

#: Helvetica (and Helvetica-Oblique) widths in 1/1000 em, ASCII 32-126,
#: transcribed from the Adobe AFM file.
HELVETICA_WIDTHS: Final[dict[str, float]] = {
    " ": 0.278, "!": 0.278, '"': 0.355, "#": 0.556, "$": 0.556,
    "%": 0.889, "&": 0.667, "'": 0.191, "(": 0.333, ")": 0.333,
    "*": 0.389, "+": 0.584, ",": 0.278, "-": 0.333, ".": 0.278,
    "/": 0.278, "0": 0.556, "1": 0.556, "2": 0.556, "3": 0.556,
    "4": 0.556, "5": 0.556, "6": 0.556, "7": 0.556, "8": 0.556,
    "9": 0.556, ":": 0.278, ";": 0.278, "<": 0.584, "=": 0.584,
    ">": 0.584, "?": 0.556, "@": 1.015, "A": 0.667, "B": 0.667,
    "C": 0.722, "D": 0.722, "E": 0.667, "F": 0.611, "G": 0.778,
    "H": 0.722, "I": 0.278, "J": 0.5, "K": 0.667, "L": 0.556,
    "M": 0.833, "N": 0.722, "O": 0.778, "P": 0.667, "Q": 0.778,
    "R": 0.722, "S": 0.667, "T": 0.611, "U": 0.722, "V": 0.667,
    "W": 0.944, "X": 0.667, "Y": 0.667, "Z": 0.611, "[": 0.278,
    "\\": 0.278, "]": 0.278, "^": 0.469, "_": 0.556, "`": 0.333,
    "a": 0.556, "b": 0.556, "c": 0.5, "d": 0.556, "e": 0.556,
    "f": 0.278, "g": 0.556, "h": 0.556, "i": 0.222, "j": 0.222,
    "k": 0.5, "l": 0.222, "m": 0.833, "n": 0.556, "o": 0.556,
    "p": 0.556, "q": 0.556, "r": 0.333, "s": 0.5, "t": 0.278,
    "u": 0.556, "v": 0.5, "w": 0.722, "x": 0.5, "y": 0.5,
    "z": 0.5, "{": 0.334, "|": 0.26, "}": 0.334, "~": 0.584,
}

#: Helvetica-Bold (and Helvetica-BoldOblique) widths, ASCII 32-126.
HELVETICA_BOLD_WIDTHS: Final[dict[str, float]] = {
    " ": 0.278, "!": 0.333, '"': 0.474, "#": 0.556, "$": 0.556,
    "%": 0.889, "&": 0.722, "'": 0.238, "(": 0.333, ")": 0.333,
    "*": 0.389, "+": 0.584, ",": 0.278, "-": 0.333, ".": 0.278,
    "/": 0.278, "0": 0.556, "1": 0.556, "2": 0.556, "3": 0.556,
    "4": 0.556, "5": 0.556, "6": 0.556, "7": 0.556, "8": 0.556,
    "9": 0.556, ":": 0.333, ";": 0.333, "<": 0.584, "=": 0.584,
    ">": 0.584, "?": 0.611, "@": 0.975, "A": 0.722, "B": 0.722,
    "C": 0.722, "D": 0.722, "E": 0.667, "F": 0.611, "G": 0.778,
    "H": 0.722, "I": 0.278, "J": 0.556, "K": 0.722, "L": 0.611,
    "M": 0.833, "N": 0.722, "O": 0.778, "P": 0.667, "Q": 0.778,
    "R": 0.722, "S": 0.667, "T": 0.611, "U": 0.722, "V": 0.667,
    "W": 0.944, "X": 0.667, "Y": 0.667, "Z": 0.611, "[": 0.333,
    "\\": 0.278, "]": 0.333, "^": 0.584, "_": 0.556, "`": 0.333,
    "a": 0.556, "b": 0.611, "c": 0.556, "d": 0.611, "e": 0.556,
    "f": 0.333, "g": 0.611, "h": 0.611, "i": 0.278, "j": 0.278,
    "k": 0.556, "l": 0.278, "m": 0.889, "n": 0.611, "o": 0.611,
    "p": 0.611, "q": 0.611, "r": 0.389, "s": 0.556, "t": 0.333,
    "u": 0.611, "v": 0.556, "w": 0.778, "x": 0.556, "y": 0.556,
    "z": 0.5, "{": 0.389, "|": 0.28, "}": 0.389, "~": 0.584,
}

#: Common cp1252 punctuation/symbols with fixed approximate widths
#: (1/1000 em) for both faces; every other non-ASCII character falls back
#: to ``EXTENDED_FALLBACK_WIDTH``. Values are deterministic, which is what
#: byte-identical rendering requires.
EXTENDED_WIDTHS: Final[dict[str, float]] = {
    "\x80": 0.556, "\x85": 0.556, "\x8e": 0.556, "\x9f": 0.556,  # control-ish slots
    "\x91": 0.222, "\x92": 0.222, "\x93": 0.333, "\x94": 0.333,  # quotes
    "\x95": 0.556,  # bullet
    "\x96": 0.556,  # en dash
    "\x97": 1.0,  # em dash
    "\xa0": 0.278,  # nbsp
    "\xa1": 0.333,  # inverted !
    "\xa9": 0.737,  # copyright
    "\xab": 0.556,  # left guillemet
    "\xae": 0.737,  # registered
    "\xb0": 0.4,  # degree
    "\xb1": 0.584,  # plusminus
    "\xb4": 0.333,  # acute
    "\xb6": 0.537,  # pilcrow
    "\xb7": 0.278,  # middot
    "\xb8": 0.333,  # cedilla
    "\xbb": 0.556,  # right guillemet
    "\xbc": 0.834,  # 1/4
    "\xbd": 0.834,  # 1/2
    "\xbe": 0.834,  # 3/4
    "\xc0": 0.667, "\xc1": 0.667, "\xc2": 0.667, "\xc3": 0.667,  # A accents
    "\xc4": 0.667, "\xc5": 0.667, "\xc7": 0.722, "\xc8": 0.667,
    "\xc9": 0.667, "\xca": 0.667, "\xcb": 0.667, "\xcc": 0.278,
    "\xcd": 0.278, "\xce": 0.278, "\xcf": 0.278, "\xd1": 0.722,
    "\xd2": 0.778, "\xd3": 0.778, "\xd4": 0.778, "\xd5": 0.778,
    "\xd6": 0.778, "\xd7": 0.611,  # multiply
    "\xd8": 0.778, "\xd9": 0.722, "\xda": 0.722, "\xdb": 0.722,
    "\xdc": 0.722, "\xdd": 0.667, "\xdf": 0.611,  # sharp s
    "\xe0": 0.556, "\xe1": 0.556, "\xe2": 0.556, "\xe3": 0.556,
    "\xe4": 0.556, "\xe5": 0.556, "\xe7": 0.556, "\xe8": 0.556,
    "\xe9": 0.556, "\xea": 0.556, "\xeb": 0.556, "\xec": 0.278,
    "\xed": 0.278, "\xee": 0.278, "\xef": 0.278, "\xf1": 0.556,
    "\xf2": 0.556, "\xf3": 0.556, "\xf4": 0.556, "\xf5": 0.556,
    "\xf6": 0.556, "\xf7": 0.611,  # divide
    "\xf8": 0.611,  # o slash
    "\xf9": 0.556, "\xfa": 0.556, "\xfb": 0.556, "\xfc": 0.556,
    "\xfd": 0.5, "\xff": 0.5, "μ": 0.556,  # micro sign
}

#: Width (1/1000 em) used for any character without a recorded width.
EXTENDED_FALLBACK_WIDTH: Final[float] = 0.556

#: Face -> width table (italic faces share their upright widths).
_FACE_WIDTHS: Final[dict[str, dict[str, float]]] = {
    FONT_HELVETICA: HELVETICA_WIDTHS,
    FONT_HELVETICA_BOLD: HELVETICA_BOLD_WIDTHS,
    FONT_HELVETICA_OBLIQUE: HELVETICA_WIDTHS,
    FONT_HELVETICA_BOLD_OBLIQUE: HELVETICA_BOLD_WIDTHS,
}


def text_width(text: str, font: str, size: float) -> float:
    """Width of ``text`` in points at ``size`` using the AFM tables.

    Unknown fonts and unknown characters degrade deterministically:
    characters outside the width table use ``EXTENDED_FALLBACK_WIDTH``.
    """
    table = _FACE_WIDTHS.get(font, HELVETICA_WIDTHS)
    total = 0.0
    for char in text:
        width = table.get(char)
        if width is None:
            width = EXTENDED_WIDTHS.get(char, EXTENDED_FALLBACK_WIDTH)
        total += width
    return total * size
